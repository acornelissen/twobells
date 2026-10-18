"""Time windows as rows on the settings pages: the board's checking times
and the dashboard's quiet hours, without the "Mon-Fri 06:30-09:30" grammar.

Each saved window is a row of its own, a fieldset ("Checking time 1"):
the days as seven checkboxes, then From and To boxes; two empty rows
follow for new windows, and a row left empty is dropped. A To before its
From runs past midnight, as core.polling has it. Saved, the rows are the
same list of window strings as before (the days run together, like
Mon-Fri), and a row left as it was keeps the words it was saved in. A
saved window that isn't days and times parse_window reads is shown as it
is, and has to be removed before the page saves.

A row's controls are named after the field, the row and the part, like
train_windows-2-from; errors are keyed the same way (the days by
train_windows-2-days), and the field's own name keys an error of the
whole field, like no window at all.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..core.polling import Window, parse_window
from ..settings.schema import CONTROL

if TYPE_CHECKING:
    from ..settings.schema import Field

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SPARE = 2  # empty rows after the saved ones, for new windows
# A time as people type it: 06:30, 6:30, 6.30 or 0630.
_TIME = re.compile(r"^(\d{1,2})[:.]?(\d{2})$")
_PARTS = (*(day.lower() for day in DAYS), "from", "to", "text", "remove")
_SAID = {"days": "Days", "from": "From", "to": "To"}
_MAX_TEXT = 60


def days_spec(days: frozenset[int]) -> str:
    """Days of the week (0 is Monday) as a window says them: three or more
    in a row as a range, like Mon-Fri; others one by one, like Sat,Sun."""
    runs: list[list[int]] = []
    for day in sorted(days):
        if runs and day == runs[-1][-1] + 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    said = []
    for run in runs:
        if len(run) >= 3:
            said.append(f"{DAYS[run[0]]}-{DAYS[run[-1]]}")
        else:
            said.extend(DAYS[day] for day in run)
    return ",".join(said)


def _time(raw: str) -> str | None:
    """A time typed in a box as HH:MM, or None if it isn't one."""
    match = _TIME.fullmatch(raw.strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


def _window(text: str) -> Window | None:
    try:
        return parse_window(text)
    except ValueError:
        return None


def _rows_sent(name: str, fields: Mapping[str, str]) -> list[int]:
    """The rows of field `name` a form sent, by number, in order."""
    pattern = re.compile(rf"^{re.escape(name)}-(\d{{1,3}})-({'|'.join(_PARTS)})$")
    found = {int(match.group(1)) for key in fields if (match := pattern.match(key))}
    return sorted(found)


def read(name: str, fields: Mapping[str, str], saved: str) -> tuple[str, dict[str, str]]:
    """The windows a form's rows for field `name` make, one per line, and
    the rows' mistakes, keyed by row and part. `saved` is the windows shown,
    one per line: a row that makes the same window keeps its words. A form
    with none of the field's rows leaves `saved` as it is."""
    rows = _rows_sent(name, fields)
    if not rows:
        return saved, {}
    kept: dict[Window, str] = {}
    for line in (line.strip() for line in saved.splitlines()):
        window = _window(line)
        if window is not None:
            kept.setdefault(window, line)
    windows: list[str] = []
    errors: dict[str, str] = {}
    for row in rows:
        part = f"{name}-{row}-"
        if part + "text" in fields:  # a window shown as it was saved
            if fields.get(part + "remove") == "on":
                continue
            text = fields[part + "text"].strip()
            if len(text) > _MAX_TEXT or CONTROL.search(text) or _window(text) is None:
                errors[part + "remove"] = "This window can't be read as days and times. Tick Remove this window."
            else:
                windows.append(text)
            continue
        ticked = [index for index, day in enumerate(DAYS) if fields.get(part + day.lower(), "")]
        start, end = fields.get(part + "from", "").strip(), fields.get(part + "to", "").strip()
        if not ticked and not start and not end:
            continue  # a spare row, or one emptied to remove it
        if any(fields.get(part + DAYS[index].lower()) != "on" for index in ticked):
            errors[part + "days"] = "Tick the days, or untick them all to remove the row"
        elif not ticked:
            errors[part + "days"] = "Tick at least one day"
        times = []
        for box, typed, missing in (("from", start, "Enter a time to start, like 06:30"),
                                    ("to", end, "Enter a time to end, like 09:30")):
            time = _time(typed)
            if not typed:
                errors[part + box] = missing
            elif time is None:
                errors[part + box] = "Enter a 24-hour time, like 06:30"
            times.append(time)
        if any(key.startswith(part) for key in errors):
            continue
        text = f"{days_spec(frozenset(ticked))} {times[0]}-{times[1]}"
        windows.append(kept.get(parse_window(text), text))
    return "\n".join(windows), errors


def anchor(name: str, key: str) -> str:
    """The id of the control an error of field `name`, keyed `key`, links to:
    a row's first day for its days, the first row's for the whole field."""
    if key == name:
        return f"{name}-1-mon"
    if key.endswith("-days"):
        return key.removesuffix("-days") + "-mon"
    return key


def said(f: Field, key: str) -> str:
    """What the error summary calls the part of field `f` an error is keyed by."""
    match = re.match(rf"^{re.escape(f.name)}-(\d+)-(\w+)$", key)
    if not match:
        return f"{f.heading} – {f.label}"
    row, part = match.groups()
    where = f"{f.heading} – {f.item} {row}"
    return f"{where}, {_SAID[part]}" if part in _SAID else where


def _rows_shown(f: Field, shown: Mapping[str, str]) -> list[tuple[int, dict[str, str]]]:
    """Each row's controls' values, by row number: as the form sent them, if
    it did, else from the saved windows with the spare rows after them."""
    sent = _rows_sent(f.name, shown)
    if sent:
        return [(row, {part: shown[f"{f.name}-{row}-{part}"] for part in _PARTS
                       if f"{f.name}-{row}-{part}" in shown}) for row in sent]
    rows = []
    for line in (line.strip() for line in shown.get(f.name, "").splitlines() if line.strip()):
        window = _window(line)
        if window is None:
            rows.append({"text": line})
            continue
        values = {DAYS[day].lower(): "on" for day in window.days}
        rows.append(values | {"from": window.start.strftime("%H:%M"), "to": window.end.strftime("%H:%M")})
    rows.extend({} for _ in range(SPARE))
    return list(enumerate(rows, start=1))


def render(f: Field, shown: Mapping[str, str], errors: Mapping[str, str]) -> str:
    """Field `f` as rows: its saved windows, one per line in shown[f.name],
    or the rows the form sent back in `shown`, with their `errors`."""
    e = html.escape
    times = f"{f.name}-times"
    out = [f'<div class="field windows{" has-error" if f.name in errors else ""}"><h3>{e(f.label)}</h3>']
    if f.hint:
        out.append(f'<p class="hint" id="{f.name}-hint">{e(f.hint)}</p>')
    out.append(f'<p class="hint" id="{times}">From and To are 24-hour, like 06:30; a To earlier than its From '
               "runs past midnight. Leave a row empty to remove it.</p>")
    whole = ""
    if f.name in errors:
        whole = f"{f.name}-error"
        out.append(_error(whole, errors[f.name]))
    for row, values in _rows_shown(f, shown):
        part = f"{f.name}-{row}-"
        out.append(f'<fieldset class="window"><legend>{e(f.item)} {row}</legend>')
        if "text" in values:
            out.append(_saved_as(part, values, errors))
        else:
            out.append(_days(part, values, errors, whole if row == 1 else ""))
            out.append('<div class="times">')
            out.extend(_box(part + box, words, values.get(box, ""), times, errors.get(part + box))
                       for box, words in (("from", "From"), ("to", "To")))
            out.append("</div>")
        out.append("</fieldset>")
    out.append("</div>")
    return "".join(out)


def _error(id_: str, text: str) -> str:
    return f'<p class="error" id="{id_}"><span class="tag">Error:</span> {html.escape(text)}</p>'


def _days(part: str, values: Mapping[str, str], errors: Mapping[str, str], also: str) -> str:
    """A row's days: a checkbox each in a group of their own; `also` is the
    id of another error about them (the whole field's, on the first row)."""
    error = errors.get(part + "days")
    ids = [id_ for id_ in (also, f"{part}days-error" if error else "") if id_]
    described = f' aria-describedby="{" ".join(ids)}"' if ids else ""
    out = [f'<fieldset class="days{" has-error" if error else ""}"{described}><legend>Days</legend>']
    if error:
        out.append(_error(f"{part}days-error", error))
    out.append('<div class="day-boxes">')
    for day in DAYS:
        box = part + day.lower()
        checked = " checked" if values.get(day.lower()) == "on" else ""
        out.append(f'<div class="day"><input id="{box}" name="{box}" type="checkbox" value="on"{checked}>'
                   f'<label for="{box}">{day}</label></div>')
    out.append("</div></fieldset>")
    return "".join(out)


def _box(box: str, words: str, value: str, hint: str, error: str | None) -> str:
    """A From or To box, described by how to write a time and by its error."""
    described = f"{hint} {box}-error" if error else hint
    invalid = ' aria-invalid="true"' if error else ""
    return (f'<div class="field time{" has-error" if error else ""}"><label for="{box}">{words}</label>'
            f'{_error(f"{box}-error", error) if error else ""}'
            f'<input id="{box}" name="{box}" type="text" value="{html.escape(value)}" class="w-5" autocomplete="off" '
            f'spellcheck="false" aria-describedby="{described}"{invalid}></div>')


def _saved_as(part: str, values: Mapping[str, str], errors: Mapping[str, str]) -> str:
    """A saved window that isn't days and times: shown as it is, sent back as
    it is unless Remove is ticked."""
    e = html.escape
    text = values.get("text", "")
    error = errors.get(part + "remove")
    described = f"{part}saved {part}remove-error" if error else f"{part}saved"
    checked = " checked" if values.get("remove") == "on" else ""
    invalid = ' aria-invalid="true"' if error else ""
    return (f'<p class="hint" id="{part}saved">Saved as <code>{e(text)}</code>, which can\'t be shown as days and '
            "times. Remove it to save this page, or put it right in config.toml.</p>"
            f'<input type="hidden" name="{part}text" value="{e(text)}">'
            f'<div class="field check{" has-error" if error else ""}">'
            f'<input id="{part}remove" name="{part}remove" type="checkbox" value="on"{checked} '
            f'aria-describedby="{described}"{invalid}>'
            f'<label for="{part}remove">Remove this window</label>'
            f'{_error(f"{part}remove-error", error) if error else ""}</div>')
