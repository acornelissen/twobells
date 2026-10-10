"""The installed package's version. pyproject.toml is the one place it is set;
this reads it back from the installed metadata, so a release tag, the wheel and
`departureboard --version` can't disagree."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _metadata_version

DIST_NAME = "departureboard"


def package_version() -> str:
    """The version pip installed, or "unknown" when running from a source tree
    that was never installed (the Docker image runs from src/ like that)."""
    try:
        return _metadata_version(DIST_NAME)
    except PackageNotFoundError:
        return "unknown"
