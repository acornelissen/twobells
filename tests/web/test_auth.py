import hashlib
import hmac
import json
import threading

import pytest

from tests.paths import ROOT
from twobells.web.auth import (
    FAILURE_WINDOW_S,
    FORM_PAGE_HEADERS,
    FORM_TOKEN_SECONDS,
    MAX_CHECKS,
    MAX_FAILURES,
    MAX_SESSIONS,
    MAX_SLOW_S,
    MAX_WAITING,
    MIN_PASSWORD,
    SCRYPT,
    SESSION_SECONDS,
    TOO_COMMON,
    TOTAL_FAILURES,
    WAITING_KEEP_S,
    FormTokens,
    Lockout,
    PasswordCheck,
    PasswordStore,
    Session,
    Sessions,
    SignInResult,
    _common_passwords,
    common_password,
    csrf_matches,
    local_host,
    outsider,
    private_address,
    same_origin,
)

PASSWORD = "correct horse battery"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- PasswordStore ---


def test_the_password_is_hashed_with_scrypt_at_the_agreed_cost(tmp_path):
    PasswordStore(tmp_path / "web.json").set(PASSWORD)
    record = json.loads((tmp_path / "web.json").read_text())
    assert SCRYPT == {"n": 2**14, "r": 8, "p": 1}
    assert {k: record[k] for k in ("n", "r", "p")} == SCRYPT
    salt = bytes.fromhex(record["salt"])
    assert len(salt) == 16
    expected = hashlib.scrypt(PASSWORD.encode(), salt=salt, dklen=32, n=2**14, r=8, p=1)
    assert record["hash"] == expected.hex()


def test_the_password_is_compared_in_constant_time(tmp_path, monkeypatch):
    store = PasswordStore(tmp_path / "web.json")
    store.set(PASSWORD)
    compared = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: compared.append((a, b)) or real(a, b))
    assert store.check(PASSWORD)
    assert not store.check("wrong")
    assert len(compared) == 2


def test_each_save_gets_a_fresh_salt(tmp_path):
    store = PasswordStore(tmp_path / "web.json")
    store.set(PASSWORD)
    first = json.loads((tmp_path / "web.json").read_text())
    store.set(PASSWORD)
    second = json.loads((tmp_path / "web.json").read_text())
    assert first["salt"] != second["salt"] and first["hash"] != second["hash"]


def test_the_password_file_is_private_and_replaced_whole(tmp_path):
    store = PasswordStore(tmp_path / "state" / "web.json")
    store.set(PASSWORD)
    store.set("another long password")
    assert (tmp_path / "state" / "web.json").stat().st_mode & 0o777 == 0o600
    assert [p.name for p in (tmp_path / "state").iterdir()] == ["web.json"]
    assert store.check("another long password") and not store.check(PASSWORD)


def test_a_password_file_from_an_existing_install_still_checks(tmp_path):
    # The format the Pi already has on disk: hex salt and hash, and the cost.
    salt = bytes(range(16))
    digest = hashlib.scrypt(PASSWORD.encode(), salt=salt, dklen=32, n=2**14, r=8, p=1)
    (tmp_path / "web.json").write_text(json.dumps({"salt": salt.hex(), "hash": digest.hex(), "n": 2**14, "r": 8, "p": 1}))
    assert PasswordStore(tmp_path / "web.json").check(PASSWORD)


def test_passwords_shorter_than_the_minimum_are_refused(tmp_path):
    store = PasswordStore(tmp_path / "web.json")
    with pytest.raises(ValueError):
        store.set("k9" * (MIN_PASSWORD // 2 - 1) + "k")
    assert not store.is_set()
    store.set("k9" * (MIN_PASSWORD // 2))
    assert store.is_set()


@pytest.mark.parametrize("common", ["password123", "PASSWORD123", "Manchester1", "1q2w3e4r5t"])
def test_common_passwords_are_refused_whatever_their_case(tmp_path, common):
    store = PasswordStore(tmp_path / "web.json")
    with pytest.raises(ValueError, match=TOO_COMMON):
        store.set(common)
    assert not store.is_set()
    store.set(PASSWORD)
    with pytest.raises(ValueError, match=TOO_COMMON):
        store.set(common)
    assert store.check(PASSWORD) and not store.check(common)


def test_the_common_password_list_is_long_lower_case_passwords_only():
    words = _common_passwords()
    assert 3000 <= len(words) <= 10_000
    assert all(len(word) >= MIN_PASSWORD and word == word.lower() and word.isprintable() for word in words)
    assert not any(word.startswith("#") for word in words)  # the header's comments stay out
    assert common_password("password123") and not common_password(PASSWORD)


def test_the_common_password_list_goes_into_the_package_and_the_ci_check():
    import tomllib
    from fnmatch import fnmatch

    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["twobells"]
    assert any(fnmatch("data/common_passwords.txt", pattern) for pattern in patterns)
    assert '"data/common_passwords.txt"' in (ROOT / ".github" / "workflows" / "ci.yml").read_text()


@pytest.mark.parametrize("content", ["{nope", "{}", '{"salt": "zz", "hash": "", "n": 16384, "r": 8, "p": 1}'])
def test_a_broken_password_file_never_matches(tmp_path, content):
    (tmp_path / "web.json").write_text(content)
    assert not PasswordStore(tmp_path / "web.json").check(PASSWORD)


# --- Sessions ---


def test_a_session_lasts_until_its_expiry_and_no_longer():
    clock = Clock()
    sessions = Sessions(clock)
    token, session = sessions.create()
    assert session.expires == clock.now + SESSION_SECONDS
    clock.now += SESSION_SECONDS - 1
    assert sessions.get(token) is session
    clock.now += 1
    assert sessions.get(token) is None
    clock.now -= 1
    assert sessions.get(token) is None  # an expired session is gone, not just hidden


def test_ending_a_session_signs_it_out():
    sessions = Sessions(Clock())
    token, _ = sessions.create()
    sessions.end(token)
    assert sessions.get(token) is None
    sessions.end(None)  # nothing to end is fine
    assert sessions.get(None) is None and sessions.get("") is None


def test_there_are_never_more_than_the_most_sessions_and_the_oldest_goes_first():
    clock = Clock()
    sessions = Sessions(clock)
    tokens = []
    for _ in range(MAX_SESSIONS + 1):
        tokens.append(sessions.create()[0])
        clock.now += 1
    assert sessions.get(tokens[0]) is None
    assert all(sessions.get(t) is not None for t in tokens[1:])


def test_the_cookie_is_http_only_strict_and_scoped():
    sessions = Sessions()
    token, _ = sessions.create()
    assert sessions.cookie(token) == (
        f"session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
    )
    assert sessions.expired_cookie() == "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
    scoped = Sessions(cookie_name="dash_session", cookie_path="/settings")
    assert scoped.cookie("abc") == (
        f"dash_session=abc; HttpOnly; SameSite=Strict; Path=/settings; Max-Age={SESSION_SECONDS}"
    )
    assert scoped.expired_cookie() == "dash_session=; HttpOnly; SameSite=Strict; Path=/settings; Max-Age=0"


@pytest.mark.parametrize("name, path", [("a;b", "/"), ("", "/"), ("s", "settings"), ("s", "/x; Domain=evil"), ("s", "/a b"),
                                        ("s\n", "/"), ("s", "/settings\n")])
def test_cookie_names_and_paths_that_could_bend_the_header_are_refused(name, path):
    with pytest.raises(ValueError):
        Sessions(cookie_name=name, cookie_path=path)
    with pytest.raises(ValueError):
        FormTokens(cookie_name=name, cookie_path=path)


def test_the_token_is_read_from_this_stores_own_cookie():
    sessions = Sessions(cookie_name="dash_session")
    assert sessions.token_from("session=other; dash_session=mine") == "mine"
    assert sessions.token_from("session=other") is None
    assert sessions.token_from(None) is None
    assert sessions.token_from('dash_session="unterminated') is None


# --- Form tokens ---


def test_the_form_token_must_match_the_sessions_own():
    sessions = Sessions()
    _, session = sessions.create()
    _, other = sessions.create()
    assert csrf_matches(session, session.csrf)
    assert not csrf_matches(session, other.csrf)
    assert not csrf_matches(session, "")
    assert not csrf_matches(session, session.csrf[:-1])
    assert not csrf_matches(None, session.csrf)


def test_tokens_are_long_and_random():
    sessions = Sessions()
    (a, sa), (b, sb) = sessions.create(), sessions.create()
    assert len(a) >= 40 and len(sa.csrf) >= 40
    assert len({a, b, sa.csrf, sb.csrf}) == 4


def test_the_form_token_is_compared_in_constant_time(monkeypatch):
    import hmac

    compared = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: compared.append((a, b)) or real(a, b))
    csrf_matches(Session(0, "token"), "guess")
    assert [set(pair) for pair in compared] == [{b"guess", b"token"}]  # as bytes, so any text compares safely


# --- Lockout ---


def test_lockout_locks_an_address_after_the_most_failures_for_the_window():
    clock = Clock()
    lockout = Lockout(clock)
    for _ in range(MAX_FAILURES):
        assert lockout.attempt("10.0.0.5")
        clock.now += 1
    assert not lockout.attempt("10.0.0.5")
    assert lockout.attempt("10.0.0.6")  # other addresses are unaffected
    # Locked until the first failure leaves the window, then one try frees up.
    clock.now = 1000.0 + FAILURE_WINDOW_S - 0.5
    assert not lockout.attempt("10.0.0.5")
    clock.now = 1000.0 + FAILURE_WINDOW_S + 0.5
    assert lockout.attempt("10.0.0.5")
    assert not lockout.attempt("10.0.0.5")


def test_a_refused_try_does_not_extend_the_lockout():
    clock = Clock()
    lockout = Lockout(clock)
    for _ in range(MAX_FAILURES):
        lockout.attempt("10.0.0.5")
    for _ in range(100):
        clock.now += 1
        lockout.attempt("10.0.0.5")
    clock.now = 1000.0 + FAILURE_WINDOW_S + 1
    assert lockout.attempt("10.0.0.5")


def test_a_success_clears_the_count():
    lockout = Lockout(Clock())
    for _ in range(MAX_FAILURES - 1):
        lockout.attempt("10.0.0.5")
    lockout.succeeded("10.0.0.5")
    assert all(lockout.attempt("10.0.0.5") for _ in range(MAX_FAILURES))


def test_simultaneous_attempts_are_counted_exactly():
    lockout = Lockout()
    allowed = []
    threads = [threading.Thread(target=lambda: allowed.append(lockout.attempt("10.0.0.5"))) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert allowed.count(True) == MAX_FAILURES



def test_ipv6_addresses_in_one_network_share_a_count():
    lockout = Lockout(Clock())
    for i in range(MAX_FAILURES):  # a new address for every guess
        assert lockout.attempt(f"fd12:3456:789a:1::{i + 1:x}")
    assert not lockout.attempt("fd12:3456:789a:1:ffff:ffff:ffff:ffff")
    assert lockout.attempt("fd12:3456:789a:2::1")  # another network is another client


def test_an_ipv4_address_counts_the_same_mapped_or_not():
    lockout = Lockout(Clock())
    for _ in range(MAX_FAILURES):
        assert lockout.attempt("::ffff:10.0.0.5")
    assert not lockout.attempt("10.0.0.5")
    lockout.succeeded("10.0.0.5")
    assert lockout.attempt("::ffff:10.0.0.5")


def test_failures_past_the_total_slow_everyone_with_a_growing_wait():
    clock = Clock()
    lockout = Lockout(clock)
    assert MAX_FAILURES < TOTAL_FAILURES
    assert MAX_SLOW_S <= 60  # the sign-in page says to try again in a minute
    for i in range(TOTAL_FAILURES):  # one guess from each of many addresses
        assert lockout.admit(f"10.0.1.{i}") is None
    # A fresh address may not try at once: everyone waits a second after the
    # last try, then 2, 4... with each failure past the total.
    assert lockout.admit("10.0.2.1") is SignInResult.SLOWED
    wait = 1
    while wait < MAX_SLOW_S:
        clock.now += wait - 0.5
        assert lockout.admit(f"10.0.2.{wait}") is SignInResult.SLOWED, wait
        clock.now += 0.5
        assert lockout.admit(f"10.0.2.{wait}") is None, wait
        wait *= 2
    # The wait stops growing at MAX_SLOW_S.
    for i in range(10):
        clock.now += MAX_SLOW_S - 0.5
        assert lockout.admit(f"10.0.3.{i}") is SignInResult.SLOWED
        clock.now += 0.5
        assert lockout.admit(f"10.0.3.{i}") is None


def test_the_slow_down_lets_go_as_failures_leave_the_window():
    clock = Clock()
    lockout = Lockout(clock)
    for i in range(TOTAL_FAILURES + 3):
        lockout.admit(f"10.0.1.{i}")
    clock.now += FAILURE_WINDOW_S + 1
    assert all(lockout.admit("10.0.2.1") is None for _ in range(MAX_FAILURES))


def test_a_locked_out_client_is_told_locked_not_slowed():
    clock = Clock()
    lockout = Lockout(clock)
    for _ in range(MAX_FAILURES):
        lockout.admit("10.0.0.5")
    for i in range(TOTAL_FAILURES):
        lockout.admit(f"10.0.1.{i}")
    assert lockout.admit("10.0.0.5") is SignInResult.LOCKED


def test_a_refused_try_does_not_extend_the_slow_down():
    clock = Clock()
    lockout = Lockout(clock)
    for i in range(TOTAL_FAILURES):
        lockout.admit(f"10.0.1.{i}")
    for _ in range(100):
        clock.now += 0.009
        assert lockout.admit("10.0.2.1") is SignInResult.SLOWED
    clock.now = 1000.0 + 1
    assert lockout.admit("10.0.2.1") is None


def test_a_success_takes_that_clients_failures_off_the_total():
    lockout = Lockout(Clock())
    for _ in range(MAX_FAILURES - 1):  # the owner's typos
        lockout.admit("10.0.0.5")
    lockout.succeeded("10.0.0.5")
    for i in range(TOTAL_FAILURES):
        assert lockout.admit(f"10.0.1.{i}") is None


def test_the_owner_signing_in_while_slowed_gives_guessers_no_extra_turn():
    clock = Clock()
    lockout = Lockout(clock)
    for i in range(TOTAL_FAILURES + 3):  # guesses enough for a 8 s wait
        clock.now += 30
        lockout.admit(f"10.0.1.{i}")
    clock.now += 60
    assert lockout.admit("10.0.0.5") is None  # the owner's turn
    lockout.succeeded("10.0.0.5")
    # The wait still counts from the owner's try, not from the last guess.
    assert lockout.admit("10.0.1.99") is SignInResult.SLOWED
    clock.now += 7.5
    assert lockout.admit("10.0.1.99") is SignInResult.SLOWED
    clock.now += 1
    assert lockout.admit("10.0.1.99") is None


def test_quiet_clients_are_dropped():
    clock = Clock()
    lockout = Lockout(clock)
    for i in range(TOTAL_FAILURES):
        lockout.admit(f"fd12:3456:789a:{i:x}::1")
    clock.now += FAILURE_WINDOW_S + 1
    lockout.admit("10.0.0.5")
    assert list(lockout._failures) == ["10.0.0.5"]  # what the table holds is the point


def test_the_clock_going_back_locks_no_one_out_for_long():
    clock = Clock()
    lockout = Lockout(clock)
    for i in range(TOTAL_FAILURES + 5):
        clock.now += 1
        lockout.admit(f"10.0.1.{i}")
    clock.now -= 3600  # failures now an hour ahead: from before the clock went back, not counted
    assert lockout.admit("10.0.1.0") is None
    assert lockout.admit("10.0.2.1") is None


# Every wait the lockout makes is whole seconds, so a script polling twice a
# second still tries first the moment a turn opens, as one polling faster would.
POLL_HZ = 2


def _poll(lockout, clock, addresses, seconds, owner=None):
    """A script polling POLL_HZ times a second for `seconds`, trying its
    addresses in turn until one is let in (a wrong password, so it stays a
    failure). The owner, if given, tries every 30 s after the script with the
    right password until let in, rests 5 minutes, and starts again. Returns
    how many of the script's tries were let in, and how long each of the
    owner's waits took from its first try, one still going at the end as long
    as it has taken so far."""
    start = clock.now
    let_in = 0
    waits = []
    began, next_try = None, 0
    for tick in range(seconds * POLL_HZ):
        clock.now = start + tick / POLL_HZ
        for address in addresses:
            if lockout.admit(address) is None:
                let_in += 1
                break
        if owner is None or tick < next_try:
            continue
        if began is None:
            began = tick
        if lockout.admit(owner) is None:
            lockout.succeeded(owner)
            waits.append((tick - began) / POLL_HZ)
            began, next_try = None, tick + 300 * POLL_HZ
        else:
            next_try = tick + 30 * POLL_HZ
    if owner is not None and began is not None:
        waits.append((seconds * POLL_HZ - began) / POLL_HZ)
    return let_in, waits


@pytest.mark.parametrize("count", [6, 7])
def test_a_script_polling_from_many_addresses_cant_keep_the_owner_out(count):
    # The reviewer's case: before turns, 6 or more addresses took every slot.
    script = [f"192.168.1.{10 + i}" for i in range(count)]
    let_in = {}
    for owner in (None, "192.168.1.2"):
        clock = Clock()
        lockout = Lockout(clock)
        _poll(lockout, clock, script, 30 * 60)  # long enough to slow to under a try a minute
        let_in[owner], waits = _poll(lockout, clock, script, 90 * 60, owner=owner)
    # Each of the script's addresses is ahead of the owner at most once, and
    # the owner tries every 30 s, so it gets in within count + 1 turns and a try.
    assert len(waits) > 5
    assert all(wait <= (count + 1) * MAX_SLOW_S + 30 for wait in waits), waits
    # Turns change who tries, not how often: the script gets as many as alone.
    assert let_in["192.168.1.2"] <= let_in[None]


def _slowed(lockout):
    """Just past the total: the next try waits a second after the last."""
    for i in range(TOTAL_FAILURES):
        assert lockout.admit(f"10.0.1.{i}") is None


def test_a_lone_client_waits_only_for_the_slow_down():
    clock = Clock()
    lockout = Lockout(clock)
    _slowed(lockout)
    assert lockout.admit("10.0.2.1") is SignInResult.SLOWED
    clock.now += 1
    assert lockout.admit("10.0.2.1") is None
    clock.now += 5  # past the next wait, of 2 s: let in at once
    assert lockout.admit("10.0.2.2") is None


def test_turns_go_to_whoever_has_waited_longest():
    clock = Clock()
    lockout = Lockout(clock)
    _slowed(lockout)
    for address in ("10.0.2.1", "10.0.2.2", "10.0.2.3"):
        assert lockout.admit(address) is SignInResult.SLOWED
    clock.now += 1  # the wait is over, and the turn is the first waiter's
    assert lockout.admit("10.0.2.3") is SignInResult.SLOWED
    assert lockout.admit("10.0.2.2") is SignInResult.SLOWED
    assert lockout.admit("10.0.2.1") is None
    clock.now += 2
    assert lockout.admit("10.0.2.3") is SignInResult.SLOWED
    assert lockout.admit("10.0.2.2") is None
    clock.now += 4
    assert lockout.admit("10.0.2.1") is SignInResult.SLOWED  # let in last time: goes behind 10.0.2.3
    assert lockout.admit("10.0.2.3") is None


def test_a_client_that_stops_trying_loses_its_place():
    clock = Clock()
    lockout = Lockout(clock)
    assert WAITING_KEEP_S > MAX_SLOW_S  # the sign-in page says to try again in a minute
    _slowed(lockout)
    assert lockout.admit("10.0.2.1") is SignInResult.SLOWED  # then goes quiet
    clock.now += 0.5
    assert lockout.admit("10.0.2.2") is SignInResult.SLOWED
    clock.now = 1000.0 + WAITING_KEEP_S - 0.1
    assert lockout.admit("10.0.2.2") is SignInResult.SLOWED  # the turn is held for 10.0.2.1
    clock.now = 1000.0 + WAITING_KEEP_S + 0.1
    assert lockout.admit("10.0.2.2") is None
    assert lockout.admit("10.0.2.1") is SignInResult.SLOWED  # back, at the end of the line
    assert list(lockout._waiting) == ["10.0.2.1"]


def test_the_waiting_line_is_bounded_and_newcomers_cant_push_out_those_waiting():
    clock = Clock()
    lockout = Lockout(clock)
    _slowed(lockout)
    assert lockout.admit("10.0.0.5") is SignInResult.SLOWED  # the owner, first
    flood = [f"10.1.{i // 200}.{i % 200}" for i in range(2 * MAX_WAITING)]
    for address in flood:
        assert lockout.admit(address) is SignInResult.SLOWED
    assert len(lockout._waiting) == MAX_WAITING
    assert flood[-1] not in lockout._waiting  # the newest are refused a place
    clock.now += 1
    for address in flood:
        assert lockout.admit(address) is SignInResult.SLOWED
    assert lockout.admit("10.0.0.5") is None


def test_a_locked_out_client_takes_no_turn():
    clock = Clock()
    lockout = Lockout(clock)
    for _ in range(MAX_FAILURES):
        lockout.admit("10.0.0.5")
    for i in range(TOTAL_FAILURES - MAX_FAILURES):
        lockout.admit(f"10.0.1.{i}")
    assert lockout.admit("10.0.0.5") is SignInResult.LOCKED
    assert "10.0.0.5" not in lockout._waiting
    clock.now += 1
    assert lockout.admit("10.0.2.1") is None


def test_a_success_clears_only_that_clients_failures_and_place():
    clock = Clock()
    lockout = Lockout(clock)
    for _ in range(3):
        lockout.admit("10.0.0.5")
    for _ in range(2):
        lockout.admit("10.0.0.6")
    for i in range(TOTAL_FAILURES - 5):
        lockout.admit(f"10.0.1.{i}")
    assert lockout.admit("10.0.0.5") is SignInResult.SLOWED
    assert lockout.admit("10.0.0.6") is SignInResult.SLOWED
    clock.now += 1
    assert lockout.admit("10.0.0.5") is None
    lockout.succeeded("10.0.0.5")
    assert "10.0.0.5" not in lockout._failures and "10.0.0.5" not in lockout._waiting
    assert len(lockout._failures["10.0.0.6"]) == 2
    assert list(lockout._waiting) == ["10.0.0.6"]
    # Below the total again, by the owner's failures alone: no one waits.
    assert lockout.admit("10.0.0.6") is None


def test_the_clock_going_back_holds_no_turn():
    clock = Clock()
    lockout = Lockout(clock)
    _slowed(lockout)
    clock.now += 0.5
    assert lockout.admit("10.0.2.1") is SignInResult.SLOWED
    clock.now -= 0.3  # 10.0.2.1's try is now ahead: from before the clock went back
    assert lockout.admit("10.0.2.2") is SignInResult.SLOWED
    clock.now = 1001.0
    assert lockout.admit("10.0.2.2") is None


# --- PasswordCheck ---


class FakePassword:
    def __init__(self, right=PASSWORD):
        self.right = right
        self.checks = 0
        self.inside = threading.Semaphore(0)
        self.go = threading.Event()
        self.go.set()

    def check(self, password):
        self.checks += 1
        self.inside.release()
        self.go.wait(10)
        return password == self.right


def test_the_right_password_signs_in_and_clears_the_count():
    lockout = Lockout(Clock())
    check = PasswordCheck(FakePassword(), lockout)
    for _ in range(MAX_FAILURES - 1):
        assert check.sign_in("10.0.0.5", "wrong") is SignInResult.WRONG
    assert check.sign_in("10.0.0.5", PASSWORD) is SignInResult.SIGNED_IN
    assert all(lockout.attempt("10.0.0.5") for _ in range(MAX_FAILURES))


def test_a_slowed_sign_in_is_not_checked():
    password = FakePassword()
    check = PasswordCheck(password, Lockout(Clock()))
    for i in range(TOTAL_FAILURES):
        assert check.sign_in(f"10.0.1.{i}", "wrong") is SignInResult.WRONG
    assert check.sign_in("10.0.2.1", PASSWORD) is SignInResult.SLOWED
    assert password.checks == TOTAL_FAILURES


def test_a_locked_out_address_is_not_checked_even_with_the_right_password():
    password = FakePassword()
    check = PasswordCheck(password, Lockout(Clock()))
    for _ in range(MAX_FAILURES):
        check.sign_in("10.0.0.5", "wrong")
    assert check.sign_in("10.0.0.5", PASSWORD) is SignInResult.LOCKED
    assert password.checks == MAX_FAILURES


def test_only_so_many_passwords_are_checked_at_once():
    password = FakePassword()
    password.go.clear()
    check = PasswordCheck(password, Lockout())
    results = []
    threads = [
        threading.Thread(target=lambda i=i: results.append(check.sign_in(f"10.0.0.{i}", PASSWORD)))
        for i in range(MAX_CHECKS)
    ]
    for thread in threads:
        thread.start()
    for _ in range(MAX_CHECKS):
        assert password.inside.acquire(timeout=5)
    assert check.sign_in("10.0.0.99", PASSWORD) is SignInResult.BUSY
    password.go.set()
    for thread in threads:
        thread.join(10)
    assert results == [SignInResult.SIGNED_IN] * MAX_CHECKS
    assert check.sign_in("10.0.0.99", PASSWORD) is SignInResult.SIGNED_IN


# --- Same-origin check ---


@pytest.mark.parametrize("headers", [
    {"Host": "pi.local:8080", "Origin": "http://pi.local:8080"},
    {"Host": "pi.local:8080", "Origin": "https://pi.local:8080"},
    {"Host": "pi.local:8080", "Referer": "http://pi.local:8080/settings?x=1"},
    {"Host": "pi.local:8080", "Origin": "", "Referer": "http://pi.local:8080/"},
])
def test_a_post_from_the_same_host_passes(headers):
    assert same_origin(headers)


@pytest.mark.parametrize("headers", [
    {"Host": "pi.local:8080"},
    {"Host": "pi.local:8080", "Origin": "null"},
    {"Host": "pi.local:8080", "Origin": "http://evil.example.com"},
    {"Host": "pi.local:8080", "Origin": "http://pi.local:8081"},
    {"Host": "pi.local:8080", "Origin": "http://pi.local"},
    {"Host": "pi.local:8080", "Origin": "ftp://pi.local:8080"},
    {"Host": "pi.local:8080", "Origin": "http://evil.example.com", "Referer": "http://pi.local:8080/"},
    {"Origin": "http://pi.local:8080"},
])
def test_a_post_from_anywhere_else_fails(headers):
    assert not same_origin(headers)


# --- Addresses and hosts ---


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.20", "10.1.2.3", "172.16.0.9", "169.254.1.1",
                                     "::1", "fe80::1%eth0", "fd00::5", "::ffff:192.168.1.2"])
def test_home_network_addresses_are_private(address):
    assert private_address(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "172.32.0.1", "2a00:1450::1", "::ffff:8.8.8.8", "nonsense", ""])
def test_internet_addresses_are_not_private(address):
    assert not private_address(address)


@pytest.mark.parametrize("host", ["192.168.1.20:8080", "twobells:8080", "raspberrypi.local", "pi.lan:8080",
                                  "board.home.arpa", "[fe80::1]:8080", "[::1]", "localhost:8080", "my-pi", "PI.LOCAL"])
def test_local_host_names_pass(host):
    assert local_host(host)


@pytest.mark.parametrize("host", ["evil.example.com", "8.8.8.8", "[2a00:1450::1]:80", "board.local.evil.com", "",
                                  None, "a..local", "x_y.local", "[fe80::1", ":8080", "pi.local.", "a:b:c"])
def test_other_host_names_fail(host):
    assert not local_host(host)


# The web server's own check: every request it serves passes it.
@pytest.mark.parametrize("address, hosts, refused", [
    ("192.168.1.5", ["pi.local:8080"], False),
    ("8.8.8.8", ["pi.local:8080"], True),
    ("192.168.1.5", ["evil.example.com"], True),
    ("192.168.1.5", [], True),
    ("192.168.1.5", ["pi.local", "pi.local"], True),
    ("192.168.1.5", ["pi.local", "evil.example.com"], True),
    ("192.168.1.5", [""], True),
    ("::1", ["[::1]:8090"], False),
    ("::ffff:8.8.8.8", ["pi.local"], True),
    ("192.168.1.5", ["departures.example.com"], True),  # not allowed unless named
])
def test_outsiders_are_requests_from_the_internet_or_to_another_host(address, hosts, refused):
    assert outsider(address, hosts) is refused


ALLOWED = frozenset({"departures.example.com"})


@pytest.mark.parametrize("hosts, refused", [
    (["departures.example.com"], False),
    (["Departures.Example.com:8090"], False),
    (["pi.local"], False),  # a local host still passes
    (["evil.departures.example.com"], True),
    (["departures.example.com.evil.com"], True),
    (["departures.example.com:8090:1"], True),
    (["[departures.example.com]"], True),  # an IPv6 literal is never an allowed name
    (["departures.example.com", "departures.example.com"], True),
])
def test_an_allowed_host_passes_by_its_exact_name(hosts, refused):
    assert outsider("192.168.1.5", hosts, ALLOWED) is refused


def test_an_allowed_host_still_needs_an_allowed_client():
    assert outsider("8.8.8.8", ["departures.example.com"], ALLOWED)
    assert outsider("10.0.0.2", ["pi.local"], client_allowed=lambda address: False)
    assert not outsider("8.8.8.8", ["pi.local"], client_allowed=lambda address: True)


def test_form_pages_carry_strict_headers():
    csp = FORM_PAGE_HEADERS["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "form-action 'self'" in csp
    assert FORM_PAGE_HEADERS["X-Frame-Options"] == "DENY"
    assert FORM_PAGE_HEADERS["Cache-Control"] == "no-store"
    assert FORM_PAGE_HEADERS["Referrer-Policy"] == "same-origin"


def test_a_non_ascii_csrf_token_is_a_mismatch_not_a_crash():
    # hmac.compare_digest raises TypeError on non-ASCII str; a form can send anything.
    from twobells.web.auth import Session, csrf_matches

    session = Session(expires=0.0, csrf="abc123")
    assert csrf_matches(session, "abc12é") is False
    assert csrf_matches(session, "ünïcode") is False
    assert csrf_matches(session, "abc123") is True


# --- Ending every session ---


def test_ending_all_sessions_signs_everyone_out():
    sessions = Sessions(Clock())
    tokens = [sessions.create()[0] for _ in range(3)]
    sessions.end_all()
    assert all(sessions.get(t) is None for t in tokens)
    assert sessions.get(sessions.create()[0]) is not None


# --- Form tokens before a session ---


def browser(tokens):
    """A new browser's Cookie header and the form value made for it."""
    nonce = tokens.nonce(None)
    return tokens.cookie(nonce).split(";")[0], tokens.form_value(nonce)


def test_a_form_value_checks_only_with_its_own_cookie():
    tokens = FormTokens(Clock())
    cookie, value = browser(tokens)
    other_cookie, other_value = browser(tokens)
    assert tokens.check(cookie, value)
    assert tokens.check(f"session=x; {cookie}", value)
    assert not tokens.check(cookie, other_value)
    assert not tokens.check(other_cookie, value)
    assert not tokens.check(None, value)
    assert not tokens.check(cookie, "")


def test_a_form_value_runs_out():
    clock = Clock()
    tokens = FormTokens(clock)
    cookie, value = browser(tokens)
    clock.now += FORM_TOKEN_SECONDS - 1
    assert tokens.check(cookie, value)
    clock.now += 1
    assert not tokens.check(cookie, value)


def test_a_form_value_cannot_be_made_or_stretched_without_the_key():
    clock = Clock()
    tokens = FormTokens(clock)
    cookie, value = browser(tokens)
    expires, mac = value.split(".")
    assert not tokens.check(cookie, f"{int(expires) + 3600}.{mac}")
    assert not FormTokens(clock).check(cookie, value)  # another process (or after a restart)
    nonce = cookie.split("=", 1)[1]
    forged = hmac.new(b"guess", f"{nonce}.{expires}".encode(), hashlib.sha256).hexdigest()
    assert not tokens.check(cookie, f"{expires}.{forged}")


@pytest.mark.parametrize("value", ["", ".", "abc.def", "-1.00", "1e9.00", "١٢٣.00", "9" * 40 + ".00", "é" * 10])
def test_odd_form_values_are_refused_not_crashed_on(value):
    tokens = FormTokens(Clock())
    cookie, _ = browser(tokens)
    assert not tokens.check(cookie, value)


def test_a_form_value_with_a_newline_after_its_expiry_is_refused():
    tokens = FormTokens(Clock())
    cookie, value = browser(tokens)
    expires, mac = value.split(".")
    assert not tokens.check(cookie, f"{expires}\n.{mac}")


def test_a_nonce_with_a_newline_after_it_is_refused(monkeypatch):
    # The cookie parser refuses control characters today; the nonce's own
    # check must not lean on it, as the nonce goes back into a header.
    from twobells.web import auth

    planted = "a" * 43 + "\n"
    monkeypatch.setattr(auth, "_cookie_value", lambda header, name: planted)
    tokens = FormTokens(Clock())
    assert tokens.nonce("form=anything") != planted
    assert not tokens.check("form=anything", tokens.form_value(planted))


@pytest.mark.parametrize("cookie", ["form=short", "form=" + "a" * 42 + "!", 'form="unterminated', "form="])
def test_a_planted_odd_cookie_gets_a_fresh_value(cookie):
    tokens = FormTokens(Clock())
    nonce = tokens.nonce(cookie)
    assert len(nonce) == 43 and nonce != cookie.split("=", 1)[1]


def test_the_browser_keeps_its_value_across_pages():
    tokens = FormTokens(Clock())
    cookie, _ = browser(tokens)
    assert tokens.nonce(cookie) == cookie.split("=", 1)[1]


def test_the_form_cookie_is_strict_http_only_and_scoped():
    tokens = FormTokens(cookie_name="dash_form", cookie_path="/settings")
    assert tokens.cookie("n" * 43) == (
        f"dash_form={'n' * 43}; HttpOnly; SameSite=Strict; Path=/settings; Max-Age={FORM_TOKEN_SECONDS}"
    )
    with pytest.raises(ValueError):
        FormTokens(cookie_name="a;b")


def test_a_password_changed_elsewhere_signs_everyone_out(tmp_path):
    # `twobells serve --set-password` runs in its own process: the page's
    # sessions must notice the new password through the file, not memory.
    from twobells.web.auth import PasswordStore, Sessions

    store = PasswordStore(tmp_path / "web.json")
    store.set("first password here")
    sessions = Sessions(password=store)
    token, _ = sessions.create()
    assert sessions.get(token) is not None
    PasswordStore(tmp_path / "web.json").set("second password here")  # another process
    assert sessions.get(token) is None
    fresh, _ = sessions.create()
    assert sessions.get(fresh) is not None


# --- Sessions kept in the store, to outlive a restart ---


def kept(folder, clock=None, password=None):
    """Sessions over the store's file, as a server (or the one after it) makes them."""
    return Sessions(clock or Clock(), password=password, file=folder / "sessions.json")


def test_a_session_outlives_the_process_that_made_it(tmp_path):
    clock = Clock()
    token, session = kept(tmp_path, clock).create()
    again = kept(tmp_path, clock).get(token)  # a new process over the same store
    assert again is not None
    assert again.expires == session.expires and again.csrf == session.csrf  # so an open form still posts


def test_the_file_holds_a_hash_of_each_token_never_the_token_or_its_form_token(tmp_path):
    token, session = kept(tmp_path).create()
    raw = (tmp_path / "sessions.json").read_bytes()
    assert token.encode() not in raw and session.csrf.encode() not in raw
    assert hashlib.sha256(token.encode()).hexdigest().encode() in raw


def test_the_sessions_file_is_private(tmp_path):
    kept(tmp_path).create()
    assert (tmp_path / "sessions.json").stat().st_mode & 0o777 == 0o600


def test_signing_out_takes_the_session_out_of_the_file(tmp_path):
    clock = Clock()
    sessions = kept(tmp_path, clock)
    gone, _ = sessions.create()
    stays, _ = sessions.create()
    sessions.end(gone)
    again = kept(tmp_path, clock)
    assert again.get(gone) is None and again.get(stays) is not None
    sessions.end_all()
    assert kept(tmp_path, clock).get(stays) is None


def test_an_expired_session_is_not_brought_back_and_is_dropped_from_the_file(tmp_path):
    clock = Clock()
    sessions = kept(tmp_path, clock)
    old, _ = sessions.create()
    clock.now += SESSION_SECONDS
    assert kept(tmp_path, clock).get(old) is None
    sessions.create()  # any write prunes
    entries = json.loads((tmp_path / "sessions.json").read_text())["sessions"]
    assert len(entries) == 1 and hashlib.sha256(old.encode()).hexdigest() not in json.dumps(entries)


def test_the_file_keeps_no_more_than_the_most_sessions_the_oldest_dropped(tmp_path):
    clock = Clock()
    sessions = kept(tmp_path, clock)
    tokens = []
    for _ in range(MAX_SESSIONS + 1):
        tokens.append(sessions.create()[0])
        clock.now += 1
    assert len(json.loads((tmp_path / "sessions.json").read_text())["sessions"]) == MAX_SESSIONS
    again = kept(tmp_path, clock)
    assert again.get(tokens[0]) is None and all(again.get(t) is not None for t in tokens[1:])


def test_a_new_password_ends_every_kept_session(tmp_path):
    password = PasswordStore(tmp_path / "web.json")
    password.set("first password here")
    clock = Clock()
    token, _ = kept(tmp_path, clock, password).create()
    PasswordStore(tmp_path / "web.json").set("second password here")  # the command line, say
    assert kept(tmp_path, clock, password).get(token) is None
    kept(tmp_path, clock, password).create()
    raw = (tmp_path / "sessions.json").read_text()
    assert hashlib.sha256(token.encode()).hexdigest() not in raw  # pruned on the next write


@pytest.mark.parametrize("content", [
    b"", b"{", b"\xff\xfe", b"[]", b"null", b'{"sessions": {}}', b'{"sessions": [1, "x", null]}',
    b"[" * 100000,
])
def test_a_broken_sessions_file_signs_everyone_out_without_a_crash(tmp_path, content):
    (tmp_path / "sessions.json").write_bytes(content)
    sessions = kept(tmp_path)
    assert sessions.get("a" * 43) is None
    token, _ = sessions.create()  # and a new sign-in still works
    assert kept(tmp_path).get(token) is not None


def test_entries_that_are_not_as_written_are_never_accepted(tmp_path):
    clock = Clock()
    token = "t" * 43
    digest = hashlib.sha256(token.encode()).hexdigest()
    for entry in [
        {"hash": digest, "expires": "9999999999", "password": None},  # not a number
        {"hash": digest, "expires": True, "password": None},
        {"hash": digest, "expires": float("nan"), "password": None},
        {"hash": digest, "expires": clock.now + SESSION_SECONDS + 1, "password": None},  # longer than any session
        {"hash": digest, "expires": clock.now - 1, "password": None},
        {"hash": digest.upper(), "expires": clock.now + 60, "password": None},
        {"hash": digest + "\n", "expires": clock.now + 60, "password": None},
        {"hash": token, "expires": clock.now + 60, "password": None},  # the token itself is no hash
        {"hash": digest, "expires": clock.now + 60, "password": "an old salt"},
    ]:
        (tmp_path / "sessions.json").write_text(json.dumps({"sessions": [entry]}))
        assert kept(tmp_path, clock).get(token) is None, entry
    (tmp_path / "sessions.json").write_text(
        json.dumps({"sessions": [{"hash": digest, "expires": clock.now + 60, "password": None}]}))
    assert kept(tmp_path, clock).get(token) is not None  # the same entry, as written, does


@pytest.mark.parametrize("token", ["", "a" * 42, "a" * 44, "a" * 42 + "\n", "a" * 42 + "!", "é" * 43])
def test_a_cookie_that_is_no_token_finds_nothing(tmp_path, token):
    sessions = kept(tmp_path)
    sessions.create()
    assert sessions.get(token) is None
    sessions.end(token)  # and ends nothing


def test_a_sessions_file_that_is_a_link_is_not_followed(tmp_path):
    clock = Clock()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    token, _ = kept(elsewhere, clock).create()
    (tmp_path / "sessions.json").symlink_to(elsewhere / "sessions.json")
    assert kept(tmp_path, clock).get(token) is None


def test_the_form_token_is_not_kept_but_made_again_from_the_session_token(tmp_path):
    clock = Clock()
    token, session = kept(tmp_path, clock).create()
    _, other = kept(tmp_path, clock).create()
    again = kept(tmp_path, clock).get(token)
    assert csrf_matches(again, session.csrf) and not csrf_matches(again, other.csrf)


def test_an_empty_form_token_never_matches():
    assert not csrf_matches(Session(0, ""), "")


def test_when_the_file_cant_be_written_a_signed_out_session_cannot_come_back(tmp_path, monkeypatch):
    from twobells.core import files

    clock = Clock()
    sessions = kept(tmp_path, clock)
    token, _ = sessions.create()

    def fail(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(files, "write_atomic", fail)
    sessions.end(token)
    assert sessions.get(token) is None
    monkeypatch.undo()
    assert kept(tmp_path, clock).get(token) is None


# --- Form tokens before a session, kept in the store ---


def test_a_form_opened_before_a_restart_still_posts_after_it(tmp_path):
    clock = Clock()
    tokens = FormTokens(clock, key_file=tmp_path / "form.key")
    cookie, value = browser(tokens)
    assert FormTokens(clock, key_file=tmp_path / "form.key").check(cookie, value)
    assert not FormTokens(clock, key_file=tmp_path / "other.key").check(cookie, value)


def test_the_form_key_is_made_when_first_needed_private_random_and_32_bytes(tmp_path):
    tokens, other = FormTokens(key_file=tmp_path / "form.key"), FormTokens(key_file=tmp_path / "other.key")
    assert not list(tmp_path.iterdir())  # starting the server writes nothing
    browser(tokens)
    browser(other)
    key = (tmp_path / "form.key").read_bytes()
    assert len(key) == 32 and key != (tmp_path / "other.key").read_bytes()
    assert (tmp_path / "form.key").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("content", [b"", b"short", b"x" * 33])
def test_a_broken_form_key_is_replaced(tmp_path, content):
    (tmp_path / "form.key").write_bytes(content)
    tokens = FormTokens(Clock(), key_file=tmp_path / "form.key")
    cookie, value = browser(tokens)
    key = (tmp_path / "form.key").read_bytes()
    assert len(key) == 32 and key != content
    assert FormTokens(Clock(), key_file=tmp_path / "form.key").check(cookie, value)
