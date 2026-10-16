// The Split-flap look (<html data-theme="flap">): the boards' times,
// destinations and statuses as letters on tiles, like a Solari board. When
// one changes, each tile whose letter changes flips forward through the
// letters between, in the order a tile's drum holds them, and settles on the
// new one: FLAP_STEPS frames FLAP_MS apart at most, under half a second.
// With prefers-reduced-motion the new letters show at once.
//
// Screen readers get the words, never the flipping letters: the tiles are
// aria-hidden beside a visually hidden copy of the words. In any other look
// the fields are plain text.
//
// The boards (board.js) mark each field with data-flap, a key saying which
// board, which row and which field it is, and call applyFlaps once drawn;
// a field whose key comes back with other words flips. A field changed
// between drawings (a bus's countdown) goes through flapText.

import { h, vh, setText } from "./dom.js";

export const FLAP_LOOK = "flap";
// The same drum as the HDMI board's (board/flaps.py): a blank, the
// alphabet, the digits and the marks a board shows. A lower-case letter
// turns with its capital; anything else counts from the blank.
export const DRUM = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:.-'&()/!?,>…";
export const FLAP_STEPS = 8;
export const FLAP_MS = 60;

const drumIndex = (letter) => {
  const at = DRUM.indexOf(letter.toUpperCase());
  return at < 0 ? null : at;
};

// What one tile shows, frame by frame, as it turns from `from` to `to`.
function turns(from, to, steps) {
  if (from === to) return [];
  const start = drumIndex(from) ?? 0;
  const end = drumIndex(to);
  // On the drum: forward round to the new letter. Off it: a turn of every step.
  const distance = end === null ? steps : (end - start + DRUM.length) % DRUM.length;
  const frames = Math.min(Math.max(distance, 1), steps);
  const shown = [];
  for (let frame = 1; frame < frames; frame++) {
    // Evenly round the drum, halves rounded up (as the HDMI board's flaps.py does).
    const letter = DRUM[(start + Math.floor((2 * frame * distance + frames) / (2 * frames))) % DRUM.length];
    shown.push(to !== to.toUpperCase() ? letter.toLowerCase() : letter);
  }
  return [...shown, to];
}

// `to` flipping over from `from`, frame by frame: each frame as long as
// `to`, the last `to` itself; none when they are the same. `from` lines up
// with `to` from its start, padded with blanks or cut to its length. A tile
// settles once it reaches its letter, so the frames are as many as the
// farthest turn needs, `steps` at most.
export function flapFrames(from, to, steps = FLAP_STEPS) {
  if (from === to || steps < 1) return [];
  const lined = from.padEnd(to.length).slice(0, to.length);
  const tiles = [...to].map((letter, i) => turns(lined[i], letter, steps));
  const count = Math.max(0, ...tiles.map((t) => t.length));
  if (count === 0) return [to]; // the same letters, shorter: the new ones at once
  return Array.from({ length: count }, (_, frame) =>
    tiles.map((t, i) => (t.length ? t[Math.min(frame, t.length - 1)] : to[i])).join(""));
}

// The words last shown for each field key, across drawings.
const shown = new Map();

const isFlapLook = () => document.documentElement?.dataset.theme === FLAP_LOOK;
const reduceMotion = () => globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;

// `words` for screen readers and `letters` on tiles: a word's tiles kept
// together on a line, a space a tile of its own where a line may break.
function drawTiles(el, words, letters) {
  const parts = [];
  for (const [i, word] of letters.split(" ").entries()) {
    if (i) parts.push(h("span", { class: "flap-tile" }, " "));
    if (word) parts.push(h("span", { class: "flap-word" }, [...word].map((letter) => h("span", { class: "flap-tile" }, letter))));
  }
  el.replaceChildren(vh(words), h("span", { class: "flap-tiles", "aria-hidden": "true" }, parts));
}

// Shows a field's words: on tiles in the Split-flap look, flipping from
// what its key last showed; as plain text in any other.
function show(el, words) {
  const key = el.dataset.flap;
  const before = shown.get(key);
  shown.set(key, words);
  el.dataset.flapText = words;
  if (!isFlapLook()) {
    setText(el, words);
    return;
  }
  const frames = before === undefined || reduceMotion() ? [] : flapFrames(before, words);
  drawTiles(el, words, frames.length > 1 ? frames[0] : words);
  let frame = 1;
  const next = () => {
    // Drawn again meanwhile (a new snapshot, or another look): this one is done.
    if (!el.isConnected || el.dataset.flapText !== words || !isFlapLook()) return;
    drawTiles(el, words, frames[frame]);
    frame += 1;
    if (frame < frames.length) setTimeout(next, FLAP_MS);
  };
  if (frames.length > 1) setTimeout(next, FLAP_MS);
}

// Every field under `root` with data-flap, in the look now on: after the
// boards are drawn, and again when the look changes.
export function applyFlaps(root = document) {
  for (const el of root.querySelectorAll("[data-flap]")) show(el, el.dataset.flapText ?? el.textContent);
}

// Sets one field's words between drawings, as setText does, flipping in the
// Split-flap look; a field without a key is plain text.
export function flapText(el, words) {
  if (!el?.dataset?.flap) {
    setText(el, words);
    return;
  }
  if (el.dataset.flapText !== words) show(el, words);
}
