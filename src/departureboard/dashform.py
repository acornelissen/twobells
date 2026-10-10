"""The dashboard's everyday settings form, under /settings: the general and
the dashboard's settings (a groupform.GroupForm).

The board's form (settingsform.py) edits the board: its look and checking
windows, and keys for TransportAPI, which the dashboard never uses. This
one edits what the dashboard runs on: the stations and stops, walk times,
how many rows and how often to check ([dashboard]), the weather, and the
dashboard's keys. Saving merges the fields into the store's settings, as
the board's form does; see groupform.py.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Callable, Mapping

from .groupform import GroupForm
from .installs import DEFAULT, install_named
from .schema import keys

if TYPE_CHECKING:
    from .store import Store

KEYS = keys("general", "dashboard")


def _header(csrf: str) -> str:
    e = html.escape
    return (
        '<header class="bar"><h1>Dashboard settings</h1>'
        '<form method="post" action="/settings/logout">'
        f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        '<button type="submit" class="quiet">Sign out</button></form></header>'
        '<nav aria-label="Settings pages"><p><a href="/">Back to the dashboard</a></p>'
        '<p><a href="/settings/setup/">Set up with a map: find your station, stops and bus companies</a></p>'
        "</nav>"
    )


class DashboardForm(GroupForm):
    """The form for a signed-in owner: drawn from the settings in force, and
    saved into the store. `load_secrets` gives the environment with every key
    the dashboard would use, saved ones included. `install` is the one the
    dashboard runs on: a desktop's unless said."""

    def __init__(self, store: Store, load_secrets: Callable[[], Mapping[str, str]],
                 install: str = DEFAULT) -> None:
        super().__init__(store, ("general", "dashboard"), "/settings", load_secrets,
                         install=install_named(install), noun="dashboard", header=_header,
                         legends={None: "Dashboard"})
