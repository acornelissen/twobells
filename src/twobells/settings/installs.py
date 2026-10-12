"""The kinds of install, and what differs between them.

A Pi runs the board from systemd, Docker runs the dashboard in a container,
and a desktop (a Mac or a Linux laptop) runs the dashboard by hand. Every
difference between them belongs in this table and nowhere else: the views
each can run, where each keeps its store (see store.find_folder), how
`twobells serve` runs there, how much of each bus timetable file it
reads, and what the settings pages tell the owner about it.
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
    # service's, asked through files (setup/spool/); "process": serve's own.
    fetch: str = "process"
    # Whether the web server is kept from connecting out (a Pi's: its unit's
    # firewall and deploy/pi/twobells-web.nft), so serve checks at start
    # that it is (web/netguard.py) and the Overview says.
    network_guard: bool = False
    # Open the browser at start; and, with no --port, take the next free port
    # after the default if it is busy.
    browser: bool = False
    # Where the log is, as trusted HTML, for a reset code put there.
    log_help: str = ""
    # The commands that set the settings password, or print a reset code, there.
    password_command: str = ""
    reset_command: str = ""
    # The most of one bus timetable file (TransXChange) read, to look for your
    # stops in, and parsed if it mentions them (held, with up to as much again
    # for what is read from it); a bigger one is skipped. Beside it, the most
    # journeys calling at your stops held, past which the build fails, and
    # half as many inbound-only ones, each up to build.JOURNEY_BYTES
    # (routesetup.DocumentLimits). A Pi's fit its units' MemoryMax=256M,
    # Docker's its container's 512 MB, shared with the web server
    # (tests/test_deploy.py); a desktop's are the timetable's own. The
    # largest file seen is 48 MB, and a busy stop's week low thousands of
    # journeys.
    timetable_read_bytes: int = 200_000_000
    timetable_parse_bytes: int = 200_000_000
    timetable_journeys: int = 20_000


_PI_COMMAND = "sudo -u twobells /opt/twobells/venv/bin/twobells serve --install pi"
_DOCKER_COMMAND = "docker exec -it twobells python -m twobells serve {} --state-dir /data"

INSTALLS = {
    install.name: install
    for install in (
        Install("pi", ("board", "dashboard"), ("board",), Path("/var/lib/twobells"), noun="board", host="0.0.0.0",  # noqa: S104 - LAN; systemd and nftables allow only the home network
                fetch="files", network_guard=True, timetable_read_bytes=61_000_000, timetable_parse_bytes=61_000_000,
                # 61 MB leaves a quarter more than the largest file seen (48 MB). With it, the
                # memory model allows about 8,800 journeys: 8,500 holds two of Sheffield's
                # busiest stops while their replaced versions are read, and a real build held
                # 15,000 under the units' 256M (tests/test_deploy.py).
                timetable_journeys=8_500,
                log_help="On the Pi: <code>sudo journalctl -u twobells-web</code>.",
                password_command=f"{_PI_COMMAND} --set-password", reset_command=f"{_PI_COMMAND} --reset-code"),
        Install("docker", ("dashboard",), ("dashboard",), Path("/data"), host="0.0.0.0",  # noqa: S104 - inside the container
                # Its dashboard keeps its own copy of the timetable through a rebuild, so the
                # buses don't blank: 8,000 leaves room for both (tests/test_deploy.py).
                timetable_read_bytes=100_000_000, timetable_parse_bytes=100_000_000, timetable_journeys=8_000,
                log_help=("In Docker: <code>docker logs twobells</code>, or in Container Manager, "
                          "Container, twobells, Log."),
                password_command=_DOCKER_COMMAND.format("--set-password"),
                reset_command=_DOCKER_COMMAND.format("--reset-code")),
        Install("desktop", ("dashboard",), ("dashboard",), None, browser=True,
                log_help=("In the Terminal window running twobells; started at login, in "
                          "<code>~/Library/Logs/twobells.log</code> on a Mac, or "
                          "<code>journalctl --user -u twobells</code> on Linux."),
                password_command="twobells serve --set-password",  # noqa: S106 - a command, not a password
                reset_command="twobells serve --reset-code"),
    )
}
DEFAULT = "desktop"


def install_named(name: str) -> Install:
    """The install called `name`; ValueError if there is none."""
    try:
        return INSTALLS[name]
    except KeyError:
        raise ValueError(f"Unknown install {name!r}; choose from {', '.join(INSTALLS)}") from None
