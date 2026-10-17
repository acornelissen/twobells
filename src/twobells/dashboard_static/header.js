// The page header: the place as its heading, and the one status pill that
// says whether the page is live and whether the trains and buses are current.
// The weather and the disruption checks say their own problems in their
// sections, so one optional feed failing doesn't make the whole page stale.

import { str, obj } from "./dom.js";
import { STATE } from "./api.js";
import { age, hhmm, parseAt } from "./time.js";
import { announce } from "./live.js";

const DEFAULT_TITLE = "Departures";

// The place, for the page's heading, from the board's title: "Glossop" from
// "Glossop > Manchester Picc" (the trains' caption says where they go, in
// full). A title with no ">" is all place; the default title gives way to
// the station's name. Never cut inside a word.
export function placeName(title, station) {
  const text = str(title).trim();
  const at = text.indexOf(">");
  const place = (at < 0 ? text : text.slice(0, at)).trim();
  if (place && place !== DEFAULT_TITLE) return place;
  return str(station).trim() || place || text.slice(at + 1).trim() || DEFAULT_TITLE;
}

// The departures' status in the snapshot: the trains', the buses' and the trams'.
function statuses(snapshot) {
  const s = obj(snapshot) || {};
  return [obj(s.trains)?.status, obj(s.buses)?.status, obj(s.metro)?.status].map(obj).filter(Boolean);
}

// "Not updated since 17:42" when the trains or buses are stale or failing,
// from the oldest good update among them; "Not updated yet" when one has
// never had one; "" when both are current.
export function staleText(snapshot) {
  const behind = statuses(snapshot).filter((st) => str(st.error) || st.stale);
  if (!behind.length) return "";
  const times = behind.map((st) => str(st.last_success)).filter((iso) => parseAt(iso) !== null);
  if (times.length < behind.length) return "Not updated yet";
  const oldest = times.reduce((a, b) => (parseAt(b) < parseAt(a) ? b : a));
  return `Not updated since ${hhmm(oldest)}`;
}

// Connection state, as an icon plus words.
const CONNECTION = {
  [STATE.connecting]: ["circle-dashed", "Connecting…"],
  [STATE.live]: ["circle", "Live"],
  [STATE.reconnecting]: ["circle-dashed", "Reconnecting…"],
  [STATE.polling]: ["refresh-cw", "Updating every 30 s"],
  [STATE.offline]: ["x", "Offline"],
};

// The pill as [state, icon, words]. While the page hears from the server,
// trains or buses that have fallen behind say so instead; when it can't, that comes
// first, with how long since it last did (`lastOk`, local ms, 0 for never).
// Polling says no age: it is never more than 30 s, as a poll that fails
// goes to offline, so the seconds would only tick.
export function pill(state, snapshot, lastOk, nowMs) {
  const reaching = state === STATE.live || state === STATE.polling;
  const stale = reaching ? staleText(snapshot) : "";
  if (stale) return ["stale", "triangle-alert", stale];
  const [name, words] = CONNECTION[state] || CONNECTION[STATE.offline];
  const since = !reaching && lastOk ? `, last update ${age((nowMs - lastOk) / 1000)}` : "";
  return [CONNECTION[state] ? state : STATE.offline, name, words + since];
}

let lastState; // the pill's state when last noted: undefined before the first

// Says a change of the pill's state (from pill()) through the live region,
// as the pill changes with nothing focused on it. Its words ticking on ("2
// min ago") are not a change, nor are connecting and the first connection.
export function noteConnection(state, words) {
  const before = lastState;
  lastState = state;
  if (before === undefined || state === before || state === STATE.connecting) return;
  if (before === STATE.connecting && (state === STATE.live || state === STATE.polling)) return;
  const text = state === STATE.live ? "Live again" : words.replace("…", "");
  announce("connection", `${text}.`);
}
