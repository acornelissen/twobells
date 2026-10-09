import hashlib
import json
import threading

import pytest

from departureboard.auth import (
    FAILURE_WINDOW_S,
    FORM_PAGE_HEADERS,
    MAX_CHECKS,
    MAX_FAILURES,
    MAX_SESSIONS,
    MIN_PASSWORD,
    SCRYPT,
    SESSION_SECONDS,
    Lockout,
    PasswordCheck,
    PasswordStore,
    Session,
    Sessions,
    SignInResult,
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
        store.set("x" * (MIN_PASSWORD - 1))
    assert not store.is_set()
    store.set("x" * MIN_PASSWORD)
    assert store.is_set()


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


@pytest.mark.parametrize("name, path", [("a;b", "/"), ("", "/"), ("s", "settings"), ("s", "/x; Domain=evil"), ("s", "/a b")])
def test_cookie_names_and_paths_that_could_bend_the_header_are_refused(name, path):
    with pytest.raises(ValueError):
        Sessions(cookie_name=name, cookie_path=path)


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
    assert compared == [(b"guess", b"token")]  # as bytes, so any text compares safely


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


@pytest.mark.parametrize("host", ["192.168.1.20:8080", "departureboard:8080", "raspberrypi.local", "pi.lan:8080",
                                  "board.home.arpa", "[fe80::1]:8080", "[::1]", "localhost:8080", "my-pi", "PI.LOCAL"])
def test_local_host_names_pass(host):
    assert local_host(host)


@pytest.mark.parametrize("host", ["evil.example.com", "8.8.8.8", "[2a00:1450::1]:80", "board.local.evil.com", "",
                                  None, "a..local", "x_y.local", "[fe80::1", ":8080", "pi.local.", "a:b:c"])
def test_other_host_names_fail(host):
    assert not local_host(host)


@pytest.mark.parametrize("address, host, refused", [
    ("192.168.1.5", "pi.local:8080", False),
    ("8.8.8.8", "pi.local:8080", True),
    ("192.168.1.5", "evil.example.com", True),
    ("192.168.1.5", None, True),
])
def test_outsiders_are_requests_from_the_internet_or_to_another_host(address, host, refused):
    assert outsider(address, host) is refused


def test_form_pages_carry_strict_headers():
    csp = FORM_PAGE_HEADERS["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "form-action 'self'" in csp
    assert FORM_PAGE_HEADERS["X-Frame-Options"] == "DENY"
    assert FORM_PAGE_HEADERS["Cache-Control"] == "no-store"
    assert FORM_PAGE_HEADERS["Referrer-Policy"] == "same-origin"


def test_a_non_ascii_csrf_token_is_a_mismatch_not_a_crash():
    # hmac.compare_digest raises TypeError on non-ASCII str; a form can send anything.
    from departureboard.auth import Session, csrf_matches

    session = Session(expires=0.0, csrf="abc123")
    assert csrf_matches(session, "abc12é") is False
    assert csrf_matches(session, "ünïcode") is False
    assert csrf_matches(session, "abc123") is True
