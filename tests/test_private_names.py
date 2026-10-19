"""No module uses another's leading-underscore names, so a private name is
private to its module: changing one breaks nothing elsewhere.

Each module is read with ast, anywhere in it (inside functions too): a
`from module import _name`, and a `module._name` where `module` is one of
the package's modules it imported, by its own name or a dotted one
(twobells.board.dotfont._GAP, board.dotfont._GAP). Dunder names
(__version__) don't count.
The scripts in tools/ are held to the same, for the package's names.
"""

import ast
from pathlib import Path

from tests.paths import ROOT

PACKAGE = ROOT / "src" / "twobells"


def _module(path: Path) -> str:
    parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


PATHS = {_module(path): path for path in PACKAGE.rglob("*.py")}


def _private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def reach_ins(module: str, source: str, package: str | None = None) -> list[str]:
    """Where `module`'s source uses another module's private names, as
    "line: module.name". `package` is the one its relative imports are
    from: its own parent, or itself for an __init__."""
    package = package or module.rpartition(".")[0]
    tree = ast.parse(source)
    imported: dict[str, str] = {}  # a name bound to one of the package's modules -> that module
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{base}.{node.module}" if node.module else base
            else:
                base = node.module or ""
            for alias in node.names:
                if f"{base}.{alias.name}" in PATHS:
                    imported[alias.asname or alias.name] = f"{base}.{alias.name}"
                elif base in PATHS and base != module and _private(alias.name):
                    found.append(f"{node.lineno}: {base}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in PATHS and alias.asname:
                    imported[alias.asname] = alias.name
                elif alias.name in PATHS:  # `import twobells.board.dotfont` binds twobells
                    top = alias.name.partition(".")[0]
                    imported[top] = top
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _private(node.attr):
            # The module the name is read from, by its full dotted name:
            # board.dotfont._GAP after `from twobells import board`.
            first, _, rest = _dotted(node.value).partition(".")
            owner = imported.get(first, "")
            owner = f"{owner}.{rest}" if owner and rest else owner
            if owner in PATHS and owner != module:
                found.append(f"{node.lineno}: {owner}.{node.attr}")
    return sorted(found)


def _dotted(node: ast.expr) -> str:
    """A name or a dotted name's text (a.b.c), else ""."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (value := _dotted(node.value)):
        return f"{value}.{node.attr}"
    return ""


def test_no_module_uses_another_modules_private_names():
    found = {}
    for module, path in sorted(PATHS.items()):
        package = module if path.name == "__init__.py" else None
        if hits := reach_ins(module, path.read_text(encoding="utf-8"), package):
            found[module] = hits
    assert found == {}


def test_both_ways_of_reaching_in_are_found():
    source = (
        "from ..settings.overlay import _toml_key, OverlayError\n"
        "from ..timetable import build as timetable\n"
        "from . import lookups\n"
        "from .lookups import __doc__\n"
        "timetable._group(lookups.Place, lookups._DIVISIONS)\n"
    )
    assert reach_ins("twobells.setup.discover", source) == [
        "1: twobells.settings.overlay._toml_key",
        "5: twobells.setup.lookups._DIVISIONS",
        "5: twobells.timetable.build._group",
    ]


def test_a_reach_in_by_the_full_dotted_name_is_found():
    source = (
        "import twobells.board.dotfont\n"
        "from twobells import board\n"
        "from .. import setup\n"
        "twobells.board.dotfont._GAP, board.dotfont._GLYPHS, setup.lookups._DIVISIONS\n"
        "twobells.board.dotfont.GAP, board.dotfont.__doc__\n"
    )
    assert reach_ins("twobells.cli.main", source) == [
        "4: twobells.board.dotfont._GAP",
        "4: twobells.board.dotfont._GLYPHS",
        "4: twobells.setup.lookups._DIVISIONS",
    ]
    assert reach_ins("twobells.board.dotfont", "import twobells.board.dotfont\ntwobells.board.dotfont._GAP\n") == []


def test_a_module_may_use_its_own_private_names():
    assert reach_ins("twobells.setup.lookups", "from . import lookups\nlookups._DIVISIONS\n") == []


def test_no_tool_uses_the_package_s_private_names():
    found = {}
    for path in sorted((ROOT / "tools").glob("*.py")):
        if hits := reach_ins(f"tools.{path.stem}", path.read_text(encoding="utf-8")):
            found[path.name] = hits
    assert found == {}
