"""The package's areas import downwards only, so no import cycle runs between them.

Each module's imports are read with ast at module level, as Python runs them
on import: those inside functions and under `if TYPE_CHECKING:` don't count.
"""

import ast
from pathlib import Path

from tests.paths import ROOT

PACKAGE = ROOT / "src" / "twobells"

# Lowest first. An area may import from its own layer and those below it;
# within a layer, the areas' imports must not go round in a circle.
LAYERS = (
    {"core", "version"},
    {"settings"},
    {"sources", "timetable"},
    {"board", "dashboard", "setup", "fetch"},
    {"web"},
    {"cli", "emulator", "__main__"},
)

# Imports that go against LAYERS on purpose, by module, each with why.
ALLOWED = {
    # The display and look names the settings offer and check, and how many
    # modes each panel has room for: data the board owns, in two modules that
    # import nothing of the app's but each other (DATA_ONLY, below).
    ("twobells.settings.schema", "twobells.board.names"),
    ("twobells.settings.schema", "twobells.board.panel"),
}

# The board's modules the settings may import, which must stay data alone.
DATA_ONLY = {"twobells.board.names": {"twobells.board.panel"}, "twobells.board.panel": set()}


def _module(path: Path) -> str:
    parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


MODULES = {_module(path): path for path in PACKAGE.rglob("*.py")}


def _area(module: str) -> str:
    return module.split(".")[1] if "." in module else ""


def _type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _statements(body: list[ast.stmt]):
    """The statements Python runs on import: not in functions or classes, nor under TYPE_CHECKING."""
    for node in body:
        if isinstance(node, ast.If):
            if not _type_checking(node):
                yield from _statements(node.body)
            yield from _statements(node.orelse)
        elif isinstance(node, ast.Try):
            yield from _statements(node.body)
            for handler in node.handlers:
                yield from _statements(handler.body)
            yield from _statements(node.orelse)
            yield from _statements(node.finalbody)
        else:
            yield node


def imports(module: str) -> set[str]:
    """The twobells modules `module` imports at module level."""
    path = MODULES[module]
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found = set()
    for node in _statements(ast.parse(path.read_text(encoding="utf-8")).body):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = ".".join(package.split(".")[: len(package.split(".")) - node.level + 1])
                base = f"{base}.{node.module}" if node.module else base
            else:
                base = node.module or ""
            names = [f"{base}.{alias.name}" if f"{base}.{alias.name}" in MODULES else base for alias in node.names]
        else:
            continue
        for name in names:
            while name and name not in MODULES:
                name = name.rpartition(".")[0]
            if name.startswith("twobells.") and name != module:
                found.add(name)
    return found


def _layer(area: str) -> int:
    return next(i for i, layer in enumerate(LAYERS) if area in layer)


def test_every_area_has_a_layer():
    assert {_area(module) for module in MODULES} - {""} <= set().union(*LAYERS)


def test_no_area_imports_from_a_higher_layer():
    upwards = sorted(
        (module, target)
        for module in MODULES
        for target in imports(module)
        if _layer(_area(target)) > _layer(_area(module)) and (module, target) not in ALLOWED
    )
    assert upwards == []


def test_the_areas_imports_never_go_round_in_a_circle():
    edges: dict[str, set[str]] = {}
    for module in MODULES:
        for target in imports(module):
            if _area(target) != _area(module) and (module, target) not in ALLOWED:
                edges.setdefault(_area(module), set()).add(_area(target))
    done: set[str] = set()

    def circle(area: str, path: tuple[str, ...]) -> tuple[str, ...] | None:
        if area in path:
            return (*path, area)
        if area in done:
            return None
        for target in sorted(edges.get(area, ())):
            found = circle(target, (*path, area))
            if found:
                return found
        done.add(area)
        return None

    assert [found for area in sorted(edges) if (found := circle(area, ()))] == []


def test_every_allowed_import_is_still_made():
    assert {(module, target) for module, target in ALLOWED if target not in imports(module)} == set()


def test_the_boards_modules_the_settings_import_stay_data_alone():
    assert {module: imports(module) for module in DATA_ONLY} == DATA_ONLY
