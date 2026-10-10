"""What the board has been doing, for the settings site's status panel."""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from ..core.errors import safe_message
from ..core.polling import PollPolicy, parse_window

# The settings site's status panel. It reads only the state file the board
# saves (never writes it) and config data, so it is quick and needs no network.
# Hand edits and older versions can leave anything in either, so every value
# is checked as it is read, and everything shown is escaped.

_SOURCES = (
    # (state entry, config section, heading, minutes before "not updating")
    ("train", "train", "Trains", 6),
    ("bus", "bus", "Buses", 30),
)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _moment(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _ago(gap: timedelta) -> str:
    minutes = int(gap.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 24 * 60:
        hours, rest = divmod(minutes, 60)
        return f"{hours} h {rest} min ago" if rest else f"{hours} h ago"
    days = minutes // (24 * 60)
    return f"{days} day ago" if days == 1 else f"{days} days ago"


def _when(moment: datetime, now: datetime) -> str:
    """Like '07:58, 3 min ago', with the day too if it wasn't today, and the year if not this one."""
    if moment.date() == now.date():
        clock = f"{moment:%H:%M}"
    elif moment.year == now.year:
        clock = f"{moment:%a} {moment.day} {moment:%b %H:%M}"
    else:
        clock = f"{moment:%a} {moment.day} {moment:%b %Y %H:%M}"
    if moment > now:
        return clock  # the clock went back since; "ago" would be nonsense
    return f"{clock}, {_ago(now - moment)}"


def _policy(section: dict, holidays: frozenset[date]) -> PollPolicy | None:
    windows = section.get("windows")
    if not isinstance(windows, list):
        return None
    try:
        return PollPolicy(tuple(parse_window(w) for w in windows), timedelta(minutes=1), holidays)
    except (TypeError, ValueError):
        return None


def _switched_off(data: dict, name: str) -> bool:
    """Whether the section says enabled = false; anything else, saving reports."""
    return _dict(data.get(name)).get("enabled") is False


def _positive(section: dict, key: str, default: int) -> int:
    value = section.get(key, default)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


def _problem(text: str) -> str:
    return f'<span class="problem"><strong>Problem:</strong> {html.escape(text)}</span>'


def _item(term: str, detail: str) -> str:
    """A <dt>/<dd> pair; `detail` is already HTML."""
    return f"<dt>{html.escape(term)}</dt><dd>{detail}</dd>"


def _source(heading: str, entry: dict, policy: PollPolicy | None, stale_after: int | None,
            now: datetime, extra: list[str]) -> str:
    e = html.escape
    items = []
    success = _moment(entry.get("last_success"))
    active = policy is not None and policy.is_active(now)
    updated = e(_when(success, now)) if success else "Never"
    if active and stale_after is not None and (success is None or now - success > timedelta(minutes=stale_after)):
        updated += " " + _problem(f"not updating; nothing new for over {stale_after} min.")
    items.append(_item("Last update", updated))
    if policy is None:
        checking = "Can’t tell: its checking times don’t read"
    elif active:
        checking = "Yes"
    else:
        checking = "No, outside its checking times"
    items.append(_item("Checking now", e(checking)))
    error = _dict(entry.get("last_error"))
    failed = _moment(error.get("at"))
    if failed and (success is None or failed > success):
        message = safe_message(str(error.get("message") or "")) or "no details"
        items.append(_item("Last error", _problem(f"{_when(failed, now)}: {message}")))
    note = entry.get("note")
    if isinstance(note, str) and note:
        items.append(_item("Note", _problem(safe_message(note))))
    items.extend(extra)
    return f"<h3>{e(heading)}</h3><dl>{''.join(items)}</dl>"


def _budget(data: dict, state: dict, now: datetime) -> list[str]:
    limit = _positive(_dict(data.get("bus")), "daily_budget", 28)
    spent = _dict(_dict(state.get("budgets")).get("bus"))
    used = spent.get("used", 0) if spent.get("day") == now.date().isoformat() else 0
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        return []
    detail = html.escape(f"{used} of {limit}")
    if used >= limit:
        detail += " " + _problem("all used; no more bus checks until midnight.")
    return [_item("TransportAPI lookups today", detail)]


def render_status(data: dict, state: dict | None, now: datetime,
                  holidays: frozenset[date] = frozenset()) -> str:
    """The status section: for each source when it last updated, whether it's
    being checked now, and any error since; then the bus allowance and the last
    redraw. `state` is None if the state file couldn't be read."""
    out = ['<section class="status" aria-labelledby="status-title"><h2 id="status-title">Status</h2>']
    out.append(f'<p class="hint">As of {now:%H:%M}. Reload the page to update.</p>')
    if state is None:
        out.append(f"<p>{_problem('the board’s state file can’t be read.')}</p></section>")
        return "".join(out)
    if not state:
        out.append("<p>No status yet: the board hasn’t saved anything. It does on its first run, "
                   "within a minute or two of starting.</p></section>")
        return "".join(out)
    if data.get("bank_holidays", True) is not True:
        holidays = frozenset()
    if now.date() in holidays:
        out.append("<p>Today is a bank holiday, so Sunday’s checking times apply.</p>")
    entries = _dict(state.get("sources"))
    for name, section_name, heading, stale_default in _SOURCES:
        section = _dict(data.get(section_name))
        if section_name not in data and name not in entries:
            continue
        if _switched_off(data, section_name):
            continue  # not checked at all, so whatever it last saved is old news
        stale_after = _positive(section, "stale_after_minutes", stale_default)
        extra = _budget(data, state, now) if name == "bus" else []
        out.append(_source(heading, _dict(entries.get(name)), _policy(section, holidays), stale_after, now, extra))
    vehicles = _dict(state.get("vehicles"))
    if vehicles and not _switched_off(data, "bus"):
        # Saved only alongside fetched departures, so too old to judge "not updating" by.
        out.append(_source("Bus positions", vehicles, _policy(_dict(data.get("bus")), holidays), None, now, []))
    drawn = _moment(_dict(state.get("last_render")).get("at"))
    out.append("<h3>Display</h3><dl>")
    out.append(_item("Last redrawn", html.escape(_when(drawn, now)) if drawn else "Not yet"))
    out.append("</dl></section>")
    return "".join(out)
