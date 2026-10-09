// Talks to the dashboard server: server-sent events first, polling as the
// fallback, and plain GETs for the dialogs.

import { observeHeader, observeSnapshot } from "./time.js";

const POLL_MS = 30_000;
const CHECK_MS = 120_000; // while live, a cheap conditional GET catches a silently dead stream
const SSE_RETRY_MS = 5 * 60_000; // while polling, how often to try the stream again

// Connection states, each shown as a glyph plus words.
export const STATE = {
  connecting: "connecting",
  live: "live",
  reconnecting: "reconnecting",
  polling: "polling",
  offline: "offline",
};

function looksLikeSnapshot(data) {
  return data && typeof data === "object" && Number.isInteger(data.version) && typeof data.generated_at === "string";
}

// GETs JSON from our own server. Returns null for 404; throws on other failures.
export async function getJSON(path, options = {}) {
  const res = await fetch(path, { cache: "no-store", credentials: "same-origin", ...options });
  observeHeader(res.headers.get("Date"));
  if (res.status === 404) return null;
  if (res.status === 304) return { notModified: true };
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const type = res.headers.get("Content-Type") || "";
  if (!type.includes("application/json")) throw new Error("Not JSON");
  return res.json();
}

// Keeps one snapshot current and calls onSnapshot with each new version, and
// onState with each connection state change. Returns lastOk(), the local time
// of the last good contact, for "updated 2 min ago".
export function connect({ onSnapshot, onState }) {
  let version = -1;
  let etag = null;
  let source = null;
  let pollTimer = 0;
  let checkTimer = 0;
  let sseRetryTimer = 0;
  let state = null;
  let lastOk = 0;

  const setState = (next) => {
    if (next !== state) { state = next; onState(state); }
  };

  const accept = (data, fromEvent) => {
    if (!looksLikeSnapshot(data)) return;
    observeSnapshot(data.generated_at);
    lastOk = Date.now();
    if (data.version !== version) {
      version = data.version;
      etag = `"v${version}"`;
      onSnapshot(data, fromEvent);
    }
  };

  async function poll() {
    clearTimeout(pollTimer);
    try {
      const headers = etag ? { "If-None-Match": etag } : {};
      const data = await getJSON("/api/snapshot", { headers });
      if (data && data.notModified) lastOk = Date.now();
      else if (data) accept(data, false);
      setState(STATE.polling);
    } catch {
      setState(STATE.offline);
    }
    if (!source) pollTimer = setTimeout(poll, POLL_MS);
  }

  function startPolling() {
    if (source) { source.close(); source = null; }
    clearTimeout(checkTimer);
    setState(lastOk ? STATE.polling : STATE.connecting);
    poll();
    clearTimeout(sseRetryTimer);
    sseRetryTimer = setTimeout(openStream, SSE_RETRY_MS);
  }

  function scheduleCheck() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(async () => {
      if (!source) return;
      try {
        const data = await getJSON("/api/snapshot", { headers: etag ? { "If-None-Match": etag } : {} });
        if (data && !data.notModified) accept(data, false);
      } catch { /* the stream's own error handling covers this */ }
      scheduleCheck();
    }, CHECK_MS);
  }

  function openStream() {
    if (!("EventSource" in window)) { startPolling(); return; }
    clearTimeout(pollTimer);
    clearTimeout(sseRetryTimer);
    if (source) source.close();
    source = new EventSource("/api/events");
    if (!lastOk) setState(STATE.connecting);
    source.addEventListener("open", () => { setState(STATE.live); scheduleCheck(); });
    source.addEventListener("snapshot", (event) => {
      let data = null;
      try { data = JSON.parse(event.data); } catch { return; }
      accept(data, true);
      setState(STATE.live);
    });
    source.addEventListener("error", () => {
      // CONNECTING means the browser retries by itself (after `retry`). CLOSED
      // means it gave up, as on a 503 when the server has too many streams.
      if (source && source.readyState === EventSource.CLOSED) startPolling();
      else setState(STATE.reconnecting);
    });
  }

  // A laptop waking up or a phone tab coming back: check at once.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (source && source.readyState === EventSource.CLOSED) openStream();
    else if (!source) poll();
  });

  openStream();
  return { lastOk: () => lastOk };
}
