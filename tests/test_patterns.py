"""No check lets a trailing newline through: a value is checked against a
pattern with fullmatch, never .match or .search.

`$` matches before a final newline, so `^[0-9]{6}$` with .match or .search
takes "123456\\n"; an unanchored pattern with .match takes anything after a
good start. So every .match and .search in the package and the tools is
flagged, whatever its pattern, however it was made (at module level, in a
function, from an f-string, with flags), unless it is listed in ALLOWED
with the reason it really is a search for part of the text. A listed call is
keyed by its file, the function it is in and the call as written, so the
same call added somewhere else is flagged again."""

import ast
import re

from tests.paths import ROOT

FOLDERS = (ROOT / "src" / "twobells", ROOT / "tools")

_CONTROL = "looks for a control character anywhere in the text, to refuse it"

ALLOWED = {
    "src/twobells/settings/schema.py::text.check: CONTROL.search": _CONTROL,
    "src/twobells/settings/schema.py::directions: CONTROL.search": _CONTROL,
    "src/twobells/settings/schema.py::at_raildata: CONTROL.search": _CONTROL,
    "src/twobells/web/windowrows.py::read: CONTROL.search": _CONTROL,
    "src/twobells/setup/inputs.py::text: CONTROL.search": _CONTROL,
    "src/twobells/setup/spool/checks.py::check_str: _CONTROL.search": _CONTROL,
    "src/twobells/sources/disruptions.py::_mentions: re.search":
        "looks for the station's name or CRS as a word anywhere in an incident's text",
    "src/twobells/sources/tfl.py::_arrival: _PLATFORM.search":
        "reads the platform from the end of a platform name; the part kept is [0-9A-Za-z]{1,4}",
    "src/twobells/sources/tfl.py::_arrival: _BOUND.match":
        "reads the direction from the start of a platform name; the part kept is one of six words",
    "src/twobells/sources/tfl.py::parse_station_notes: _STATION_WORDS.search":
        "looks for words about lifts and step-free access anywhere in a notice",
    "src/twobells/sources/darwin.py::message_kind: _SERVICE_WORDS.search":
        "looks for words about the trains anywhere in a message",
    "src/twobells/sources/darwin.py::message_kind: _FACILITY_WORDS.search":
        "looks for words about the station's facilities anywhere in a message",
    "src/twobells/timetable/model.py::_Reader.next: _SPACE.match":
        "skips the white space at the reader's place in the JSON, a prefix by design",
}


def loose_checks(where: str, source: str) -> list[str]:
    """Each .match or .search in `source`, as "where::function: call", with
    the line it is on: (key, line). The function is the dotted name of the
    def (and class) it is in, or <module>."""
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
        if isinstance(node, ast.Attribute) and node.attr in ("match", "search"):
            found.append((f"{where}::{'.'.join(scope) or '<module>'}: {ast.unparse(node)}", node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(ast.parse(source), ())
    return [f"{key} (line {line})" for key, line in found]


def _key(found: str) -> str:
    return found.rpartition(" (line ")[0]


def _all_checks() -> list[str]:
    found = []
    for folder in FOLDERS:
        for path in sorted(folder.rglob("*.py")):
            found += loose_checks(path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8"))
    return found


def test_no_check_uses_match_or_search_unless_it_is_a_real_search():
    assert [found for found in _all_checks() if _key(found) not in ALLOWED] == []


def test_every_allowed_search_is_still_there():
    # A stale entry would let the same call back in unnoticed.
    assert set(ALLOWED) - {_key(found) for found in _all_checks()} == set()


def _flagged(source: str) -> list[str]:
    return [_key(found) for found in loose_checks("x.py", source) if _key(found) not in ALLOWED]


def test_the_sweep_finds_each_kind_of_loose_check():
    assert re.compile(r"^[0-9]{6}$").match("123456\n")  # why the sweep exists
    planted = {
        "module-level": "_PIN = re.compile(r'^[0-9]{6}$')\ndef ok(v):\n    return _PIN.match(v)\n",
        "local": "def rows(name, keys):\n    p = re.compile(rf'^{name}-(\\d+)$')\n    return [p.match(k) for k in keys]\n",
        "f-string": "def said(name, key):\n    return re.match(rf'^{re.escape(name)}-(\\d+)$', key)\n",
        "flags": "_ID = re.compile(r'(?i)^[a-z]{3}$')\ndef ok(v):\n    return _ID.search(v)\n",
        "unanchored, was fullmatch": "_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')\ndef ok(v):\n    return _TOKEN.match(v)\n",
        "passed, not called": "def keep(values):\n    return list(filter(_EVENT_ID.match, values))\n",
        "in a method": "class Form:\n    def ok(self, v):\n        return self._pattern.search(v)\n",
    }
    for kind, source in planted.items():
        assert len(_flagged(source)) == 1, kind
    assert _flagged("def ok(v):\n    return _TOKEN.fullmatch(v)\n") == []


def test_an_allowed_search_is_allowed_only_where_it_is_listed():
    def ok_in(where: str, function: str) -> bool:
        source = f"def {function}(text):\n    return re.search(rf'(?<!\\w){{name}}', text)\n"
        return [_key(found) for found in loose_checks(where, source)] == [f"{where}::{function}: re.search"] and (
            f"{where}::{function}: re.search" in ALLOWED)

    assert ok_in("src/twobells/sources/disruptions.py", "_mentions")
    assert not ok_in("src/twobells/sources/disruptions.py", "train_disruptions")
    assert not ok_in("src/twobells/sources/tfl.py", "_mentions")
