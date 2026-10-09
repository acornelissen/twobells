// The weather panel: now, the next 12 hours, rain and daylight. Every glyph
// is decoration beside words that say the same thing.

import { $, h, fill, setText, str, num, list, obj } from "./dom.js";
import { hhmm, now, parseAt } from "./time.js";

const HOURS = 12;
const COMPASS = ["north", "north-north-east", "north-east", "east-north-east", "east", "east-south-east",
  "south-east", "south-south-east", "south", "south-south-west", "south-west", "west-south-west", "west",
  "west-north-west", "north-west", "north-north-west"];

// A text glyph for a WMO weather code. ︎ asks for text, not emoji, style.
function glyph(code, isDay) {
  if (code === null) return "";
  if (code === 0 || code === 1) return isDay === false ? "☾" : "☀︎";
  if (code === 2 || code === 3) return "☁︎";
  if (code === 45 || code === 48) return "≋";
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "❄︎";
  if (code >= 95) return "⚡︎";
  if (code >= 51) return "☂︎";
  return "";
}

const icon = (code, isDay) => h("span", { class: "wx-glyph", "aria-hidden": "true" }, glyph(code, isDay));
const degrees = (t) => `${Math.round(t)} °C`;

function windText(now) {
  const wind = num(now.wind);
  if (wind === null) return "";
  const dir = num(now.direction);
  const from = dir !== null ? ` from the ${COMPASS[Math.round((((dir % 360) + 360) % 360) / 22.5) % 16]}` : "";
  const gust = num(now.gust);
  return `Wind ${Math.round(wind)} mph${from}${gust !== null && gust > wind ? `, gusts ${Math.round(gust)} mph` : ""}`;
}

function renderNow(current) {
  const el = $("weather-now");
  if (!current) { el.replaceChildren(h("p", {}, "No current conditions.")); return; }
  const temp = num(current.temperature);
  const feels = num(current.feels_like);
  fill(el,
    h("p", { class: "wx-now" },
      icon(num(current.code), current.is_day),
      h("span", { class: "wx-temp" }, temp !== null ? degrees(temp) : ""),
      " ",
      h("span", { class: "wx-text" }, str(current.text))),
    h("p", { class: "wx-detail" },
      [feels !== null ? `Feels like ${degrees(feels)}` : "", windText(current)].filter(Boolean).join(". ")));
}

function renderHours(hours) {
  const t = now();
  const upcoming = hours.filter((hr) => {
    const start = parseAt(hr.start);
    return start !== null && start + 3_600_000 > t;
  }).slice(0, HOURS);
  const el = $("weather-hours");
  if (!upcoming.length) { el.replaceChildren(); return; }
  el.replaceChildren(h("ol", { class: "wx-hours" }, upcoming.map((hr) => {
    const temp = num(hr.temperature);
    const prob = num(hr.probability);
    const wind = num(hr.wind);
    return h("li", {},
      h("span", { class: "wx-hour" }, hhmm(hr.start)),
      icon(num(hr.code), hr.is_day),
      h("span", { class: "wx-text" }, str(hr.text)),
      h("span", { class: "wx-temp" }, temp !== null ? degrees(temp) : ""),
      prob !== null ? h("span", { class: "wx-rain" }, `${Math.round(prob)}% rain`) : null,
      wind !== null ? h("span", { class: "wx-wind" }, `${Math.round(wind)} mph wind`) : null);
  })));
}

export function renderWeather(snapshot) {
  const section = $("weather");
  const weather = obj(snapshot.weather);
  section.hidden = !weather;
  if (!weather) return;
  const status = obj(weather.status) || {};
  const last = hhmm(status.last_success);
  const statusEl = $("weather-status");
  const problem = !!(str(status.error) || status.stale);
  setText(statusEl, problem
    ? `Problem: ${str(status.error) || "not updated lately"}.${last ? ` Showing the forecast from ${last}.` : ""}`
    : last ? `Updated ${last}.` : "");
  statusEl.classList.toggle("problem", problem);
  renderNow(obj(weather.now));
  setText($("weather-rain"), str(weather.rain));
  $("weather-rain").hidden = !str(weather.rain);
  const sun = obj(weather.sun);
  const rise = sun ? hhmm(sun.sunrise) : "";
  const set = sun ? hhmm(sun.sunset) : "";
  setText($("weather-sun"), [rise && `Sunrise ${rise}.`, set && `Sunset ${set}.`].filter(Boolean).join(" "));
  renderHours(list(weather.hours).filter(obj));
}
