"""The kinds of install, and what differs between them.

A Pi runs the board from systemd, Docker runs the dashboard in a container,
and a desktop (a Mac or a Linux laptop) runs the dashboard by hand. Every
difference between them belongs in this table and nowhere else. For now
that is only where each keeps its store (see store.find_folder).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Install:
    name: str
    # The store when nothing else says where it is; None: the platform's own
    # folder for an app's state (store.platform_folder).
    default_folder: Path | None


INSTALLS = {
    install.name: install
    for install in (
        Install("pi", Path("/var/lib/departureboard")),
        Install("docker", Path("/data")),
        Install("desktop", None),
    )
}
DEFAULT = "desktop"


def install_named(name: str) -> Install:
    """The install called `name`; ValueError if there is none."""
    try:
        return INSTALLS[name]
    except KeyError:
        raise ValueError(f"Unknown install {name!r}; choose from {', '.join(INSTALLS)}") from None
