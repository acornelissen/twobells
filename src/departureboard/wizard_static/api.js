// Talking to the server: its setup API, by relative URLs, with the
// session's CSRF token.

import { str, obj } from "../dashboard_static/dom.js";
import { state } from "./state.js";

export const POLL_MS = 2000;
export const POLL_FAILURES = 5; // network blips while the search runs, in a row, before giving up

export class ApiError extends Error {
  constructor(message, status, field) {
    super(message);
    this.status = status;
    this.field = field;
  }
}

async function readJSON(res) {
  if (!(res.headers.get("Content-Type") || "").includes("application/json")) return null;
  try { return obj(await res.json()); } catch { return null; }
}

function failure(res, data) {
  if (data && str(data.error)) return new ApiError(str(data.error), res.status, str(data.field) || null);
  if (res.status === 401 || res.status === 403) {
    return new ApiError("You've been signed out. Reload the page and sign in again.", res.status, null);
  }
  return new ApiError(`The ${state.noun} couldn't do that (error ${res.status}). Try again.`, res.status, null);
}

export async function call(name, body = {}) {
  let res;
  try {
    res = await fetch(`api/${name}`, {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(`The ${state.noun} didn't answer. Check it is on and connected, then try again.`, 0, null);
  }
  const data = await readJSON(res);
  if (!res.ok) throw failure(res, data);
  return data || {};
}

export async function signIn() {
  let res;
  try {
    res = await fetch("api/session", { cache: "no-store", credentials: "same-origin" });
  } catch {
    // Before begin, so the host's noun isn't known yet.
    throw new ApiError("Nothing answered. Check you're connected, then reload.", 0, null);
  }
  const data = await readJSON(res);
  if (!res.ok || !data || !str(data.csrf)) throw failure(res, data);
  state.csrf = str(data.csrf);
}

export const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });
