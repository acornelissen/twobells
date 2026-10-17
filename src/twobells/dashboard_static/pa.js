// Station announcements, as a station's public address says them: a chime of
// three tones, then the words, shown on the page while they are said. The
// server words each train's announcements (the row's `announce`, from
// core/announce.py); this module only says them.
//
// A train's sheet has Announce, which says its announcements at once. A wall
// tablet at "/?kiosk&announce" says some by itself (due()): the next train to
// depart, a few minutes before it goes, the delay of a train late with no time
// to go by, a cancellation and a platform alteration, each once, and never in
// a quiet window. Which train is next is the server's to say (its "next"
// announcement), so the tablet and a train's sheet never disagree. Browsers play no sound
// until the page has been touched, so the tablet asks for one tap, on screen.
// When the settings leave announcements out (show.js), neither happens.
//
// The chime is our own, three tones made with Web Audio; the words go through
// the browser's speech synthesis, in a British English voice where there is
// one. Neither touches the network, so the CSP needs nothing more. The words
// on screen are in no live region (aria-live is left off): a screen reader
// would read them over the speech.

import { $, setText, str, list, obj } from "./dom.js";
import { parseAt } from "./time.js";
import { shows } from "./show.js";

// The chime: three tones, each [Hz, seconds after the first], ringing out.
export const CHIME = [[523.25, 0], [659.25, 0.45], [783.99, 0.9]]; // C5, E5, G5
const RING_S = 1.3;
export const CHIME_S = CHIME[CHIME.length - 1][1] + RING_S;

// How long before it goes the next train is announced, and how far ahead a
// cancellation or platform alteration is.
export const NEXT_AHEAD_MS = 4 * 60_000;
export const CHANGE_AHEAD_MS = 60 * 60_000;

const KINDS = ["platform", "cancelled", "delayed", "next", "departs"];
const MOST_CHARS = 2000; // far beyond any announcement; anything longer isn't one
// What the next-train announcement says: the delay first, when there is one.
const LEAVING = ["delayed", "next"];

// A row's announcements, [{ kind, text }] in the order to say them, with
// anything that isn't one left out.
export function announcements(row) {
  return list(obj(row)?.announce).map(obj).filter((item) => item && KINDS.includes(item.kind)
    && str(item.text).trim() && str(item.text).length <= MOST_CHARS)
    .map((item) => ({ kind: item.kind, text: item.text.trim() }));
}

// All of a row's announcements as one text, "" for none.
export const announcementText = (row) => announcements(row).map((item) => item.text).join(" ");

// The voice to speak in: a British English one if the browser has any (an
// installed one before one over the network), else null for its default.
export function pickVoice(voices) {
  const british = list(voices).filter((v) => str(v?.lang).replace("_", "-").toLowerCase() === "en-gb");
  return british.find((v) => v.localService === true) || british[0] || null;
}

// What a wall tablet should say now of the train rows (in the board's
// order), at `nowMs` on the server's clock, as [{ key, text }]. `said` is a
// Set of the keys already said, added to here, so each is said once: a
// cancellation, a platform alteration (again if the platform changes again),
// the next train's announcement (the row the server words as next, while it
// is still to go), and the delay of a train late with no time to go by, a
// few minutes before it was due. Nothing in a quiet window.
export function due(rows, nowMs, said, quiet = false) {
  if (quiet) return [];
  const found = [];
  const add = (key, text) => {
    if (!text || said.has(key)) return;
    said.add(key);
    found.push({ key, text });
  };
  for (const row of list(rows)) {
    const id = str(row?.id);
    if (!id) continue;
    const items = announcements(row);
    const text = (kinds) => items.filter((item) => kinds.includes(item.kind)).map((item) => item.text).join(" ");
    const cancelled = row.status === "cancelled";
    const expected = parseAt(row.expected_at);
    // Late with no time to go by: it could go any moment, so it stays.
    const untimed = !cancelled && row.status === "late" && expected === null;
    const leaves = cancelled ? parseAt(row.scheduled_at) : expected ?? parseAt(row.scheduled_at);
    if (leaves === null) continue;
    const ahead = leaves - nowMs;
    if (ahead < 0 && !untimed) continue; // gone
    if (ahead <= CHANGE_AHEAD_MS) {
      add(`cancelled:${id}`, text(["cancelled"]));
      add(`platform:${id}:${str(row.platform)}`, text(["platform"]));
    }
    if (cancelled || ahead > NEXT_AHEAD_MS) continue;
    if (untimed) add(`delayed:${id}`, text(["delayed"])); // never the next: it has no time to go by
    else if (items.some((item) => item.kind === "next")) add(`next:${id}`, text(LEAVING));
  }
  return found;
}

// Drops the keys of trains no longer on the board, so `said` stays small.
export function forget(said, ids) {
  for (const key of [...said]) if (!ids.has(key.split(":")[1])) said.delete(key);
}

// --- Saying it.

let audio = null; // the AudioContext, made on a tap or press
let queue = []; // [{ text, show }] waiting
let current = null; // { text, show, done } being said

// Plays the chime on `context` from now; returns how long it takes, in
// seconds, or 0 without one. Each tone is a sine with a quiet octave above
// it, struck and left to ring out.
export function chime(context) {
  if (!context) return 0;
  const start = context.currentTime + 0.05;
  for (const [hz, after] of CHIME) {
    for (const [mult, level] of [[1, 0.25], [2, 0.05]]) {
      const tone = context.createOscillator();
      const gain = context.createGain();
      const at = start + after;
      tone.type = "sine";
      tone.frequency.setValueAtTime(hz * mult, at);
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(level, at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + RING_S);
      tone.connect(gain);
      gain.connect(context.destination);
      tone.start(at);
      tone.stop(at + RING_S + 0.05);
    }
  }
  return CHIME_S + 0.05;
}

// Lets the page make sound: browsers allow it only from a tap or key press,
// so this runs in one. Safe to call again.
export function unlock() {
  try {
    const Context = globalThis.AudioContext || globalThis.webkitAudioContext;
    if (!audio && Context) audio = new Context();
    if (audio && audio.state === "suspended") audio.resume();
  } catch {
    audio = null; // no sound: the words are still said and shown
  }
  const synth = globalThis.speechSynthesis;
  // Some browsers speak later only once something has been spoken in a gesture.
  if (synth && globalThis.SpeechSynthesisUtterance) synth.speak(new globalThis.SpeechSynthesisUtterance(""));
}

// About how long the words take to say, for when nothing tells us they're done.
const sayingMs = (text) => Math.max(5_000, text.split(/\s+/).length * 450);

// The words, a sentence an utterance: a browser may cut off one long one.
const sentences = (text) => text.split(/(?<=[.!?])\s+/).filter(Boolean);

function speakWords(item) {
  const synth = globalThis.speechSynthesis;
  const Utterance = globalThis.SpeechSynthesisUtterance;
  // If the speech never says it has ended, move on anyway.
  item.timer = setTimeout(item.done, sayingMs(item.text) * (synth && Utterance ? 2 : 1));
  if (!synth || !Utterance) return;
  const voice = pickVoice(synth.getVoices());
  const parts = sentences(item.text);
  parts.forEach((part, i) => {
    const words = new Utterance(part);
    words.lang = "en-GB";
    if (voice) words.voice = voice;
    words.rate = 0.95;
    if (i === parts.length - 1) {
      words.onend = item.done;
      words.onerror = item.done;
    }
    synth.speak(words);
  });
}

function sayNext() {
  current = queue.shift() || null;
  if (!current) return;
  const item = current;
  let finished = false;
  item.done = () => {
    if (finished) return;
    finished = true;
    clearTimeout(item.timer);
    clearTimeout(item.chimed);
    item.show("");
    if (current === item) sayNext();
  };
  item.show(item.text);
  const wait = chime(audio);
  item.chimed = setTimeout(() => speakWords(item), wait * 1000);
}

// Says `text` after the chime, once whatever is being said has finished.
// `show(words)` puts the words on the page, and "" takes them off.
export function say(text, show) {
  queue.push({ text, show });
  if (!current) sayNext();
}

// Whether anything is being said or waiting to be.
export const busy = () => current !== null;

// Stops at once and forgets what was waiting.
export function hush() {
  const was = current;
  queue = [];
  current = null;
  try { globalThis.speechSynthesis?.cancel(); } catch { /* nothing to stop */ }
  if (was) was.done();
}

// --- A wall tablet's own announcements, at "/?kiosk&announce".

export const wantsAnnouncements = () => new URLSearchParams(globalThis.location?.search || "").has("announce");

let unlocked = false;
const said = new Set();
const showOnTablet = (words) => {
  const el = $("pa-words");
  setText(el, words);
  el.hidden = !words;
};

// Shows the note asking for a tap, and turns the sound on with it.
export function startAnnouncements() {
  $("pa").hidden = false;
  setText($("pa-note"), "Tap the screen once to turn on station announcements.");
  const on = () => {
    if (unlocked) return;
    unlocked = true;
    unlock();
    $("pa-note").hidden = true;
    document.removeEventListener("click", on);
    document.removeEventListener("keydown", on);
  };
  // A click, not a pointerdown: a touch lets a page make sound only once it ends.
  document.addEventListener("click", on);
  document.addEventListener("keydown", on);
}

// Each second: says what is due of the train rows, once the tablet has been
// tapped; in a quiet window, stops.
export function tickAnnouncements(snapshot, rows, nowMs) {
  // Left out by the settings (show.js): the tablet says nothing, nor asks for a tap.
  const on = shows("announce");
  $("pa").hidden = !on;
  if (!on && busy()) hush();
  if (!on || !unlocked) return;
  const quiet = snapshot?.quiet === true;
  if (quiet && busy()) hush();
  forget(said, new Set(list(rows).map((row) => str(row?.id))));
  for (const { text } of due(rows, nowMs, said, quiet)) say(text, showOnTablet);
}
