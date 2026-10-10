"""The Pi's settings form, at / on the home network: the general and the
board's settings (a groupform.GroupForm), with the board's status.

Saved values go to settings.toml in the store (layered over config.toml)
and keys to the secrets.env beside it, merged into what is there, so
settings the page doesn't show survive. See groupform.py.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .groupform import GroupForm
from .installs import install_named
from .overlay import _toml_key, _toml_string  # noqa: F401 - discover.py imports these from here
from .schema import keys

if TYPE_CHECKING:
    from .store import Store

KEYS = keys("general", "board")


def _header(csrf: str) -> str:
    e = html.escape
    return (
        '<header class="bar"><h1>Board settings</h1>'
        '<form method="post" action="/logout">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<button type="submit" class="quiet">Sign out</button></form></header>'
        '<div class="setup-link"><p><a href="/setup/">Set up home, station and stops</a></p>'
        '<p class="hint">Finds them near home step by step, with walk times and the bus companies.</p></div>'
    )


class SettingsForm(GroupForm):
    def __init__(self, store: Store | Path | str, load_secrets: Callable[[], dict[str, str]],
                 clock: Callable[[], datetime] | None = None) -> None:
        """`store` is where settings are saved (or the store of a config.toml
        path). `clock` gives the board's local time, as a naive datetime; by
        default the time now in the configured timezone."""
        from .store import Store

        super().__init__(Store.of(store), ("general", "board"), "/", load_secrets, install=install_named("pi"),
                         noun="board", header=_header, legends={None: "Board", "weather": "Rain warning"},
                         clock=clock)
