"""The kinds of install, and what differs between them.

A Pi runs the board from systemd, Docker runs the dashboard in a container,
and a desktop (a Mac or a Linux laptop) runs the dashboard by hand. Every
difference between them belongs in this table and nowhere else: the views
each can run, where each keeps its store (see store.find_folder), how
`departureboard serve` runs there, and what the settings pages tell the
owner about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Install:
    name: str
    # The views this install can run, and those on when [app] views doesn't
    # say (as for every install from before views); see schema.views.
    offered: tuple[str, ...]
    default_views: tuple[str, ...]
    # The store when nothing else says where it is; None: the platform's own
    # folder for an app's state (store.platform_folder).
    default_folder: Path | None
    # What shows the departures, in the pages' words: "board" or "dashboard".
    noun: str = "dashboard"
    # Where `serve` listens unless told: every address, or this computer only.
    host: str = "127.0.0.1"
    # "files": the internet work (the setup wizard's lookups) is another
    # service's, asked through files (setupspool.py); "process": serve's own.
    fetch: str = "process"
    # Open the browser at start; and, with no --port, take the next free port
    # after the default if it is busy.
    browser: bool = False
    # Where the log is, as trusted HTML, for a reset code put there.
    log_help: str = ""
    # The commands that set the settings password, or print a reset code, there.
    password_command: str = ""
    reset_command: str = ""


_PI_COMMAND = "sudo -u departureboard /opt/departureboard/venv/bin/departureboard serve --install pi"
_DOCKER_COMMAND = "docker exec -it departureboard python -m departureboard.cli serve {} --state-dir /data"

INSTALLS = {
    install.name: install
    for install in (
        Install("pi", ("board", "dashboard"), ("board",), Path("/var/lib/departureboard"), noun="board", host="0.0.0.0",
                fetch="files", log_help="On the Pi: <code>sudo journalctl -u departureboard-web</code>.",
                password_command=f"{_PI_COMMAND} --set-password", reset_command=f"{_PI_COMMAND} --reset-code"),
        Install("docker", ("dashboard",), ("dashboard",), Path("/data"), host="0.0.0.0",
                log_help=("In Docker: <code>docker logs departureboard</code>, or in Container Manager, "
                          "Container, departureboard, Log."),
                password_command=_DOCKER_COMMAND.format("--set-password"),
                reset_command=_DOCKER_COMMAND.format("--reset-code")),
        Install("desktop", ("dashboard",), ("dashboard",), None, browser=True,
                log_help="In the Terminal window running departureboard.",
                password_command="departureboard serve --set-password",
                reset_command="departureboard serve --reset-code"),
    )
}
DEFAULT = "desktop"


def install_named(name: str) -> Install:
    """The install called `name`; ValueError if there is none."""
    try:
        return INSTALLS[name]
    except KeyError:
        raise ValueError(f"Unknown install {name!r}; choose from {', '.join(INSTALLS)}") from None
