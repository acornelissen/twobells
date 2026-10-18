"""No check with a pattern anchored at both ends lets a trailing newline through.

`$` matches before a final newline, so `^[0-9]{6}$` with .match or .search
takes "123456\\n". Each module's own patterns, and those it imports, are found
by importing it, so a name passed between modules is caught too."""

import ast
import importlib
import re

from tests.paths import ROOT

SRC = ROOT / "src"


def _anchored(value) -> bool:
    return isinstance(value, re.Pattern) and isinstance(value.pattern, str) and (
        value.pattern.startswith("^") and value.pattern.endswith("$") and not value.pattern.endswith("\\$"))


def _receiver(module, node: ast.expr):
    """What a call's receiver is in `module`, if it is a module-level name or
    an attribute of one; None for anything else."""
    if isinstance(node, ast.Name):
        return getattr(module, node.id, None)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return getattr(getattr(module, node.value.id, None), node.attr, None)
    return None


def _loose_checks() -> list[str]:
    found = []
    for path in sorted((SRC / "twobells").rglob("*.py")):
        name = ".".join(path.relative_to(SRC).with_suffix("").parts).removesuffix(".__init__")
        module = importlib.import_module(name)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("match", "search")):
                continue
            pattern = _receiver(module, node.func.value)
            if pattern is re and node.args and isinstance(node.args[0], ast.Constant):
                pattern = re.compile(node.args[0].value)  # re.match(r"^...$", text), written in place
            if _anchored(pattern):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def test_no_anchored_pattern_is_checked_with_match_or_search():
    assert _loose_checks() == []


def test_the_sweep_finds_a_loose_check():
    # The finder itself: a pattern held at module level and checked with .match.
    assert _anchored(re.compile(r"^[0-9]{6}$"))
    assert not _anchored(re.compile(r"^[0-9]{6}\$"))
    assert not _anchored(re.compile(r"[0-9]{6}$"))
    assert re.compile(r"^[0-9]{6}$").match("123456\n")  # why the sweep exists
