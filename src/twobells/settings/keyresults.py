"""The last test of each key: whether it worked, and when.

The settings' Keys page tests a saved key with one small request to its
service (setup/keytest.py). What the test found is kept in the store, in
keytests.json, so the Keys page and the Overview can say "Works, tested
10 Oct 10:42" later, after a restart too. Only a test that says either way
is kept: "works", or "refused" for a wrong key or one for another product.
One that couldn't tell (the service down, say) leaves what was there.

The file holds, for each key, its state and the time of the test in whole
seconds since 1970: never the key, nor the test's message. TransportAPI's
app ID and key are tested together, so their one result is kept under
TAPI_APP_KEY. Replacing a key forgets its result, as it was for the old key.

The file is 0600 and replaced whole on every change, like sessions.json. It
is read strictly: anything not as written, a link in its place, or a file
too big, means no results, and the keys show as not tested. One server per
store writes it, from more than one thread, so changes are made under a lock.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..core import files
from .schema import KEYS

log = logging.getLogger(__name__)

FILE_NAME = "keytests.json"
STATES = ("works", "refused")
# What each test result (setup/keytest.py's states) says about the key, when it says either way.
FOUND = {"works": "works", "wrong_key": "refused", "not_subscribed": "refused"}
# A key whose result is kept under another's name: tested as a pair.
_KEPT_AS = {"TAPI_APP_ID": "TAPI_APP_KEY"}
NAMES = frozenset(k.name for k in KEYS) - set(_KEPT_AS)
_MAX_BYTES = 4096  # a few results; anything bigger isn't one we wrote
_MAX_TIME = 2**33  # seconds since 1970: past the year 2200, so not a time we wrote

_lock = threading.Lock()


@dataclass(frozen=True)
class LastTest:
    state: str  # one of STATES
    time: int  # when, in whole seconds since 1970


def path_in(folder: Path) -> Path:
    return Path(folder) / FILE_NAME


def load(folder: Path) -> dict[str, LastTest]:
    """Each key's last result, by the name it is kept under; none if the
    file is missing, unreadable or not as written."""
    path = path_in(folder)
    try:
        fd = os.open(path, os.O_RDONLY | files.NOFOLLOW | files.CLOEXEC)
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError as error:
        log.warning("Can't read the keys' test results (%s); the keys show as not tested", type(error).__name__)
        return {}
    found = _parsed(raw)
    if found is None:
        log.warning("The keys' test results file isn't as written; the keys show as not tested")
        return {}
    return found


def _parsed(raw: bytes) -> dict[str, LastTest] | None:
    if len(raw) > _MAX_BYTES:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    results = data.get("results") if isinstance(data, dict) and set(data) == {"results"} else None
    if not isinstance(results, dict):
        return None
    found = {}
    for name, result in results.items():
        if name not in NAMES or not isinstance(result, dict) or set(result) != {"state", "time"}:
            return None
        state, time = result["state"], result["time"]
        if state not in STATES or not isinstance(time, int) or isinstance(time, bool) or not 0 < time < _MAX_TIME:
            return None
        found[name] = LastTest(state, time)
    return found


def record(folder: Path, name: str, state: str, time: float) -> None:
    """Keep `state` (STATES) as key `name`'s last result, tested at `time`
    (seconds since 1970). A file that can't be written is said in the log;
    the page still shows the result just found."""
    name = _KEPT_AS.get(name, name)
    if name not in NAMES or state not in STATES:
        raise ValueError("Not a key and a result that can be kept")
    with _lock:
        results = load(folder)
        results[name] = LastTest(state, int(time))
        _write(folder, results)


def forget(folder: Path, names: Iterable[str]) -> None:
    """Forget the results of keys `names`, as they have been replaced."""
    names = {_KEPT_AS.get(name, name) for name in names}
    with _lock:
        results = load(folder)
        if not names & set(results):
            return
        _write(folder, {name: result for name, result in results.items() if name not in names})


def _write(folder: Path, results: dict[str, LastTest]) -> None:
    """Replace the file. Called with the lock held."""
    path = path_in(folder)
    text = json.dumps({"results": {name: {"state": r.state, "time": r.time} for name, r in sorted(results.items())}})
    try:
        files.write_atomic(path, text, 0o600)
    except OSError as error:
        log.warning("Can't save the keys' test results (%s)", type(error).__name__)
        # Whatever it held may be for a key since replaced: nothing in it may come back.
        try:
            path.unlink(missing_ok=True)
        except OSError as gone:
            log.warning("Can't remove the old keys' test results either (%s)", type(gone).__name__)
