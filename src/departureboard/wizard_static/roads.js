// After the search: the roads for the bus maps. The search is done once the
// timetable is saved; the roads are found after it, for up to five minutes.
// The person may wait for them or carry on, and either way they finish on
// their own. Said politely at the start and each quarter of the way, never
// on every road; finish.js's save() says when they are found.

import { $, num, obj } from "../dashboard_static/dom.js";
import { POLL_FAILURES, POLL_MS, call, sleep } from "./api.js";
import { say } from "./say.js";
import { state } from "./state.js";

const ROADS = "Finding the roads for the bus maps";
const roadsRunning = (job) => (obj(job && job.roads) || {}).state === "running";

function showRoads(roads) {
  const done = num(roads.done);
  const total = num(roads.total);
  const bar = $("roads-progress");
  if (total && done !== null) {
    bar.max = total;
    bar.value = Math.min(done, total);
    $("roads-count").textContent = `${ROADS}: ${Math.min(done, total)} of ${total}`;
  } else {
    bar.removeAttribute("value"); // indeterminate: still working out how many there are
    $("roads-count").textContent = ROADS;
  }
}

// 0 to 4: how many quarters of the roads are found.
function quarters(roads) {
  const done = num(roads.done);
  const total = num(roads.total);
  return total && done !== null ? Math.floor((4 * Math.min(done, total)) / total) : 0;
}

// Follows the roads of a finished search until they are found or the person
// carries on. "found" once they are; "carried" if the person carried on, or
// they stopped or can't be followed (they finish on their own, or the next
// timetable build finds them); "none" if there were none to find.
export async function followRoads(job, pollMs = POLL_MS) {
  if (!roadsRunning(job)) return "none";
  let carried = false;
  const carry = new Promise((resolve) => {
    state.carryOn = () => { carried = true; resolve(); };
  });
  let roads = obj(job.roads);
  $("job-box").hidden = true;
  for (const id of ["roads-wait", "roads-on"]) $(id).removeAttribute("aria-disabled");
  $("roads-box").hidden = false;
  showRoads(roads);
  let said = quarters(roads);
  say(`The timetable is built. ${$("roads-count").textContent}. Wait for them, or carry on.`);
  $("roads-intro").focus();
  let outcome = "carried";
  let failures = 0;
  while (!carried) {
    await Promise.race([sleep(pollMs), carry]);
    if (carried) break;
    let latest;
    try {
      latest = obj((await call("job")).job);
      failures = 0;
    } catch {
      if (++failures >= POLL_FAILURES) break;
      continue;
    }
    if (carried || !latest || latest.state !== "done") break;
    roads = obj(latest.roads) || {};
    if (roads.state === "done") {
      outcome = "found";
      break;
    }
    if (roads.state !== "running") break;
    showRoads(roads);
    const now = quarters(roads);
    if (now > said && now < 4) {
      said = now;
      say($("roads-count").textContent);
    }
  }
  state.carryOn = null;
  $("roads-box").hidden = true;
  return outcome;
}

// The two choices. Waiting is what happens anyway; the button says so and
// stays where it is, so focus does too. Carrying on saves now.
export function setUpRoads() {
  $("roads-wait").addEventListener("click", () => {
    if ($("roads-wait").getAttribute("aria-disabled") === "true") return;
    $("roads-wait").setAttribute("aria-disabled", "true");
    say("Waiting for the roads. Setup saves once they are found.");
  });
  $("roads-on").addEventListener("click", () => {
    if (!state.carryOn || $("roads-on").getAttribute("aria-disabled") === "true") return;
    $("roads-on").setAttribute("aria-disabled", "true");
    state.carryOn();
  });
}
