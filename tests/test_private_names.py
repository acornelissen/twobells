"""No module uses another's leading-underscore names, so a private name is
private to its module: changing one breaks nothing elsewhere.

Each module is read with ast, anywhere in it (inside functions too): a
`from module import _name`, and a `module._name` where `module` is one of
the package's modules it imported. Dunder names (__version__) don't count.
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
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and _private(node.attr)
                and imported.get(node.value.id, module) != module):
            found.append(f"{node.lineno}: {imported[node.value.id]}.{node.attr}")
    return sorted(found)


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


def test_a_module_may_use_its_own_private_names():
    assert reach_ins("twobells.setup.lookups", "from . import lookups\nlookups._DIVISIONS\n") == []
