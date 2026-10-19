"""No check lets a trailing newline through: a value is checked against a
pattern with fullmatch, never .match or .search.

`$` matches before a final newline, so `^[0-9]{6}$` with .match or .search
takes "123456\\n"; an unanchored pattern with .match takes anything after a
good start. So every .match and .search in the package and the tools is
flagged, whatever its pattern, however it was made (at module level, in a
function, from an f-string, with flags), unless it is listed in ALLOWED
with the reason it really is a search for part of the text. So is every
pattern's findall and finditer, which a check could use the same way, and
the ways round the sweep: getattr(pattern, "match") and `from re import
match`. A listed call is keyed by its file, the function it is in and the
call as written, and a call to re's own functions by its pattern too, so the
same call added somewhere else, or another pattern beside it, is flagged
again."""

import ast
import re

from tests.paths import ROOT

FOLDERS = (ROOT / "src" / "twobells", ROOT / "tools")

LOOSE = ("match", "search")
FINDS = ("findall", "finditer")  # a pattern's: an XML element has a findall too

_CONTROL = "looks for a control character anywhere in the text, to refuse it"

ALLOWED = {
    "src/twobells/settings/schema.py::text.check: CONTROL.search": _CONTROL,
    "src/twobells/settings/schema.py::directions: CONTROL.search": _CONTROL,
    "src/twobells/settings/schema.py::at_raildata: CONTROL.search": _CONTROL,
    "src/twobells/web/windowrows.py::read: CONTROL.search": _CONTROL,
    "src/twobells/setup/inputs.py::text: CONTROL.search": _CONTROL,
    "src/twobells/setup/spool/checks.py::check_str: _CONTROL.search": _CONTROL,
    r"src/twobells/sources/disruptions.py::_mentions: re.search(f'(?<!\\w){re.escape(name)}(?!\\w)')":
        "looks for the station's name as a word anywhere in an incident's text",
    r"src/twobells/sources/disruptions.py::_mentions: re.search(f'(?<![A-Za-z]){crs}(?![A-Za-z])')":
        "looks for the station's CRS as a word anywhere in an incident's text",
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
    """Each loose check in `source`, as "where::function: call", with the
    line it is on: (key, line). The function is the dotted name of the def
    (and class) it is in, or <module>. A loose check is any .match or
    .search; a pattern's .findall or .finditer (re's own, re.compile's, or
    one on a name or attribute given re.compile); getattr for one of those
    by name; and importing one from re. A call to re's own is keyed with its
    pattern: re.search(f'...')."""
    tree = ast.parse(source)
    names_for_re = {"re"} | {alias.asname for node in ast.walk(tree) if isinstance(node, ast.Import)
                             for alias in node.names if alias.name == "re" and alias.asname}

    def is_re(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in names_for_re

    def is_compile(node: ast.AST | None) -> bool:
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "compile" and is_re(node.func.value))

    compiled = {ast.unparse(target) for node in ast.walk(tree)
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and is_compile(node.value)
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])}

    def is_pattern(node: ast.AST) -> bool:
        return is_re(node) or is_compile(node) or ast.unparse(node) in compiled

    found: list[tuple[str, int]] = []
    keyed: set[int] = set()  # the re.<function> of each call already keyed with its pattern

    def is_loose(node: ast.AST) -> bool:
        return isinstance(node, ast.Attribute) and id(node) not in keyed and (
            node.attr in LOOSE or (node.attr in FINDS and is_pattern(node.value)))

    def is_getattr(node: ast.AST) -> bool:
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in LOOSE + FINDS)

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
        call = None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and is_re(node.func.value)
                and node.func.attr in LOOSE + FINDS):
            pattern = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "pattern"), None)
            call = f"{ast.unparse(node.func)}({ast.unparse(pattern) if pattern else ''})"
            keyed.add(id(node.func))
        elif is_loose(node) or is_getattr(node):
            call = ast.unparse(node)
        elif isinstance(node, ast.ImportFrom) and node.module == "re":
            call = ", ".join(f"from re import {alias.name}" for alias in node.names if alias.name in LOOSE + FINDS)
        if call:
            found.append((f"{where}::{'.'.join(scope) or '<module>'}: {call}", node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, ())
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
        "fetched by name": "def ok(v):\n    return getattr(_PIN, 'match')(v)\n",
        "imported by name": "from re import match\ndef ok(v):\n    return match(r'^[0-9]{6}$', v)\n",
        "imported under another name": "from re import search as find\n",
        "re.findall": "def ok(v):\n    return bool(re.findall(r'^[0-9]{6}$', v))\n",
        "re.finditer": "def ok(v):\n    return any(re.finditer(r'^[0-9]{6}$', v))\n",
        "a compiled findall": "_PIN = re.compile(r'^[0-9]{6}$')\ndef ok(v):\n    return bool(_PIN.findall(v))\n",
        "an attribute's findall": (
            "class Form:\n    def __init__(self):\n        self._pin = re.compile(r'^[0-9]{6}$')\n"
            "    def ok(self, v):\n        return bool(self._pin.findall(v))\n"),
        "re under another name": "import re as regex\ndef ok(v):\n    return regex.findall(r'^[0-9]{6}$', v)\n",
    }
    for kind, source in planted.items():
        assert len(_flagged(source)) == 1, kind
    assert _flagged("def ok(v):\n    return _TOKEN.fullmatch(v)\n") == []
    # An XML element's findall isn't a pattern's.
    assert _flagged("def refs(node):\n    return node.findall('t:Ref', _NS)\n") == []


def test_an_allowed_search_is_allowed_only_where_it_is_listed():
    # A call to re's own functions is keyed by its pattern as well, so a
    # different pattern in a function with an allowed search is flagged.
    search = "re.search(rf'(?<!\\w){re.escape(name)}(?!\\w)', text, re.IGNORECASE)"

    def ok_in(where: str, function: str, call: str = search) -> bool:
        flagged = [_key(found) for found in loose_checks(where, f"def {function}(text, name):\n    return {call}\n")]
        return len(flagged) == 1 and flagged[0] in ALLOWED

    assert ok_in("src/twobells/sources/disruptions.py", "_mentions")
    assert not ok_in("src/twobells/sources/disruptions.py", "train_disruptions")
    assert not ok_in("src/twobells/sources/tfl.py", "_mentions")
    assert not ok_in("src/twobells/sources/disruptions.py", "_mentions", "re.search(r'^[0-9]{6}$', text)")
    assert not ok_in("src/twobells/sources/disruptions.py", "_mentions", "re.match(rf'{name}', text)")
