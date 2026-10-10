// The weather panel: now, the next few hours as a strip with rain bars, and
// the rest of the 12 hours behind "More hours". Every icon and bar is
// decoration beside words that say the same thing.

import { $, h, fill, patch, setText, vh, str, num, list, obj } from "./dom.js";
import { hhmm, now, parseAt } from "./time.js";
import { icon } from "./icons.js";

const HOURS = 12;
const SHOWN = 4; // hours in the strip before "More hours"
const RAIN_WRITTEN = 30; // the chance of rain is written from this percentage up
const COMPASS = ["north", "north-north-east", "north-east", "east-north-east", "east", "east-south-east",
  "south-east", "south-south-east", "south", "south-south-west", "south-west", "west-south-west", "west",
  "west-north-west", "north-west", "north-north-west"];

// The icon for a WMO weather code, or null for one with no picture.
export function weatherIcon(code, isDay) {
  const night = isDay === false;
  if (code === null) return null;
  if (code === 0 || code === 1) return night ? "moon" : "sun";
  if (code === 2) return night ? "cloud-moon" : "cloud-sun";
  if (code === 3) return "cloud";
  if (code === 45 || code === 48) return "cloud-fog";
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "cloud-snow";
  if (code >= 95) return "cloud-lightning";
  if (code >= 51 && code <= 57) return "cloud-drizzle";
  if (code >= 61) return "cloud-rain";
  return null;
}

const wxIcon = (code, isDay) => h("span", { class: "wx-glyph", "aria-hidden": "true" }, icon(weatherIcon(code, isDay)));

// "west-south-west" for a bearing in degrees, or "" for none.
const compass = (dir) => (dir !== null ? COMPASS[Math.round((((dir % 360) + 360) % 360) / 22.5) % 16] : "");

// "WSW" from "west-south-west".
const compassShort = (name) => name.split("-").map((word) => word[0].toUpperCase()).join("");

// The sunrise or sunset that comes next, as [shown, said], or null after
// sunset: the snapshot has only today's.
function nextSun(sun) {
  if (!sun) return null;
  const t = now();
  const rise = parseAt(sun.sunrise);
  const set = parseAt(sun.sunset);
  const text = rise !== null && t < rise ? `Sunrise ${hhmm(sun.sunrise)}`
    : set !== null && t < set ? `Sunset ${hhmm(sun.sunset)}` : "";
  return text ? [text, `${text}.`] : null;
}

// "Feels 6° · W 12 mph · Sunset 18:23", read out in full words.
function detailLine(current, sun) {
  const parts = [];
  const feels = num(current.feels_like);
  if (feels !== null) parts.push([`Feels ${Math.round(feels)}°`, `Feels like ${Math.round(feels)} degrees.`]);
  const wind = num(current.wind);
  if (wind !== null) {
    const from = compass(num(current.direction));
    const gust = num(current.gust);
    const gusts = gust !== null && gust > wind ? `, gusts ${Math.round(gust)} mph` : "";
    parts.push([
      `${from ? compassShort(from) : "Wind"} ${Math.round(wind)} mph`,
      `Wind ${Math.round(wind)} mph${from ? ` from the ${from}` : ""}${gusts}.`,
    ]);
  }
  const daylight = nextSun(sun);
  if (daylight) parts.push(daylight);
  if (!parts.length) return null;
  return h("p", { class: "wx-detail" },
    h("span", { "aria-hidden": "true" }, parts.map(([shown]) => shown).join(" · ")),
    vh(parts.map(([, said]) => said).join(" ")));
}

function renderNow(current, sun) {
  const el = $("weather-now");
  if (!current) { el.replaceChildren(h("p", {}, "No current conditions.")); return; }
  const temp = num(current.temperature);
  fill(el,
    h("p", { class: "wx-now" },
      wxIcon(num(current.code), current.is_day),
      h("span", { class: "wx-temp" }, temp !== null ? `${Math.round(temp)}°` : ""),
      " ",
      h("span", { class: "wx-text" }, str(current.text))),
    detailLine(current, sun));
}

// One column of the strip: the hour, an icon, the temperature and a rain bar,
// with the percentage written only when it is worth noticing. A screen reader
// hears one sentence instead: "19:00, light drizzle, 9 degrees, 47% chance of rain".
function hourItem(hr) {
  const time = hhmm(hr.start);
  const temp = num(hr.temperature);
  const prob = num(hr.probability);
  const chance = prob !== null ? Math.min(100, Math.max(0, Math.round(prob))) : null;
  let bar = null;
  if (chance !== null) {
    // The bar's length goes through the CSSOM, as the CSP forbids style attributes.
    const part = h("span", { class: "wx-bar-fill" });
    part.style.setProperty("--rain", `${chance}%`);
    bar = h("span", { class: "wx-bar" }, part);
  }
  const said = [time, str(hr.text).toLowerCase(), temp !== null ? `${Math.round(temp)} degrees` : "",
    chance !== null ? `${chance}% chance of rain` : ""].filter(Boolean).join(", ");
  return h("li", {},
    h("span", { class: "wx-hour", "aria-hidden": "true" }, time),
    wxIcon(num(hr.code), hr.is_day),
    h("span", { class: "wx-temp", "aria-hidden": "true" }, temp !== null ? `${Math.round(temp)}°` : ""),
    h("span", { class: "wx-rain", "aria-hidden": "true" },
      bar,
      chance !== null && chance >= RAIN_WRITTEN ? h("span", { class: "wx-pct" }, `${chance}%`) : null),
    vh(said));
}

// A row of hours that scrolls sideways when it doesn't fit, so it is a named
// region keyboard users can reach and scroll.
const strip = (hours, name) => h("div", { class: "wx-strip", role: "region", tabindex: "0", ...name },
  h("ol", { class: "wx-hours" }, hours.map(hourItem)));

function renderHours(hours) {
  const t = now();
  const upcoming = hours.filter((hr) => {
    const start = parseAt(hr.start);
    return start !== null && start + 3_600_000 > t;
  }).slice(0, HOURS);
  const el = $("weather-hours");
  if (!upcoming.length) { el.replaceChildren(); return; }
  const later = upcoming.slice(SHOWN);
  const more = $("weather-more");
  // patch() keeps the nodes, so an open "More hours" stays open and a
  // scrolled or focused strip stays put when a new snapshot comes in.
  patch(el,
    strip(upcoming.slice(0, SHOWN), { "aria-labelledby": "weather-hours-heading" }),
    later.length ? h("details", { id: "weather-more", class: "wx-more", open: !!(more && more.open) },
      h("summary", {}, "More hours"),
      strip(later, { "aria-label": "Later hours" })) : null);
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
  renderNow(obj(weather.now), obj(weather.sun));
  setText($("weather-rain"), str(weather.rain));
  $("weather-rain").hidden = !str(weather.rain);
  renderHours(list(weather.hours).filter(obj));
}
