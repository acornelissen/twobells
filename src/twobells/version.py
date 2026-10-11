"""The package's version. pyproject.toml is the one place it is set; this
reads it back from the installed metadata, so a release tag, the wheel and
`twobells --version` can't disagree. Code run from a source tree
without installing it (the Docker image copies src/ and pyproject.toml, and
nothing more) reads pyproject.toml itself."""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version
from pathlib import Path

DIST_NAME = "twobells"
UNKNOWN = "unknown"
# The source tree's pyproject.toml, beside src/: <root>/src/twobells/version.py.
SOURCE_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
# A version as PEP 440 writes one, with room for a local label; anything else is not shown.
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!_-]{0,63}")


def package_version() -> str:
    """The version pip installed; else the source tree's, when running from
    one that was never installed; else "unknown"."""
    try:
        return _metadata_version(DIST_NAME)
    except PackageNotFoundError:
        return source_version(SOURCE_PYPROJECT)


def source_version(pyproject: Path) -> str:
    """The version in `pyproject`, if it is this package's and looks like a
    version; "unknown" if it is missing, broken or another project's."""
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return UNKNOWN
    if not isinstance(project, dict) or project.get("name") != DIST_NAME:
        return UNKNOWN
    found = project.get("version")
    if not isinstance(found, str) or not _VERSION.fullmatch(found):
        return UNKNOWN
    return found


def about(found: str) -> str:
    """How the dashboard and the settings name what is running, given its
    version: "twobells 0.1.0", or "twobells, version unknown"."""
    return f"{DIST_NAME}, version {UNKNOWN}" if found == UNKNOWN else f"{DIST_NAME} {found}"
