"""The pieces both settings pages share, tested on their own. The hosts'
own tests (test_web.py, test_dashsettings.py) cover them end to end."""

import html
import json
import logging
import re
from email.message import Message

import pytest

from departureboard.auth import MAX_FAILURES, FormTokens, Lockout, PasswordStore, ResetCodes, Sessions
from departureboard.formpages import (
    HTML,
    TEXT,
    WIZARD_TYPES,
    WRONG_CODE,
    FormPages,
    Reply,
    Wording,
    compact_json,
    document,
    parse_form,
    read_body,
)


def test_a_form_keeps_the_last_value_of_each_field_and_blank_ones():
    assert parse_form(b"a=1&b=&a=2&c=x+y%21") == {"a": "2", "b": "", "c": "x y!"}


def test_bytes_that_are_not_utf_8_cannot_break_a_form():
    assert parse_form(b"name=%ff%fe&other=\xff") == {"name": "��", "other": "�"}


def test_a_form_with_too_many_fields_is_refused():
    assert len(parse_form(b"&".join(b"f%d=1" % n for n in range(300)))) == 300
    with pytest.raises(ValueError):
        parse_form(b"&".join(b"f%d=1" % n for n in range(301)))


def framed(*pairs):
    """Request headers with exactly these (name, value) pairs, repeats and all."""
    message = Message()
    for name, value in pairs:
        message[name] = value
    return message


class Stream:
    """A request body that remembers whether anyone read it."""

    def __init__(self, data=b"a=1"):
        self.data = data
        self.read_from = False

    def read(self, size):
        self.read_from = True
        return self.data[:size]


def test_a_body_with_one_plain_length_is_read_to_that_length():
    stream = Stream(b"a=1&b=2")
    assert read_body(framed(("Content-Length", "3")), stream, 10) == b"a=1"
    assert read_body(framed(("Content-Length", "0")), Stream(), 10) == b""
    assert read_body(framed(("Content-Length", "10")), Stream(), 10) == b"a=1"


@pytest.mark.parametrize("pairs, status", [
    ([("Transfer-Encoding", "chunked")], 400),
    ([("Transfer-Encoding", "chunked"), ("Content-Length", "3")], 400),
    ([("Content-Length", "3"), ("Content-Length", "3")], 400),
    ([], 411),
    ([("Content-Length", "")], 411),
    ([("Content-Length", "three")], 411),
    ([("Content-Length", "-1")], 411),
    ([("Content-Length", "+3")], 411),
    ([("Content-Length", "3.0")], 411),
    ([("Content-Length", "\u00b2")], 411),  # a digit to str.isdigit, not to int()
    ([("Content-Length", "11")], 413),
    ([("Content-Length", "9" * 5000)], 413),  # past int()'s own digit limit
])
def test_a_body_framed_any_other_way_is_refused_unread(pairs, status):
    stream = Stream()
    assert read_body(framed(*pairs), stream, 10) == status
    assert not stream.read_from


def test_the_document_escapes_its_title_and_names_its_stylesheet():
    page = document("<Board> & co", "<p>body</p>", "/settings/style.css")
    assert "<title>&lt;Board&gt; &amp; co</title>" in page
    assert '<link rel="stylesheet" href="/settings/style.css">' in page
    assert page.startswith("<!doctype html>") and "<main><p>body</p></main>" in page
    assert '<html lang="en-GB">' in page


def test_the_wizard_serves_only_pages_scripts_and_styles():
    assert set(WIZARD_TYPES) == {".html", ".js", ".css"}
    assert all(value.endswith("; charset=utf-8") for value in WIZARD_TYPES.values())


# --- FormPages, with a made-up host under /area ---

PASSWORD = "correct horse battery"
NEW_PASSWORD = "a brand new password"
CLIENT = "192.168.1.20"

WIDGET = Wording(
    title="Widget settings",
    claim_title="Claim this widget",
    claim_intro="Claim intro.",
    reset_intro="Reset intro.",
    asked="Look at the widget.",
    ask="Show a code",
    ask_again="Show another code",
    code_label="Code from the widget",
    no_code="No code? Look harder.",
    claim_hint='<p class="hint">Or use the widget tool.</p>',
    back='<p class="after"><a href="/">Back to the widget</a></p>',
    no_code_made="That form had run out, so no code was shown.",
    cant_check="The code can't be checked: use the tool.",
    saved="Saved for the widget.",
    reset_logged="Widget password reset",
    wrong_step_token="Reload the widget.",
)


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


class FakePage:
    def __init__(self):
        self.saved = []

    def render(self, csrf, message="", errors=None, values=None):
        return f"<form csrf={csrf}>{message}|{errors or ''}|{values or ''}</form>"

    def save(self, fields):
        if fields.get("title") == "bad":
            return {"title": "Too bad"}
        self.saved.append(fields)
        return {}


class FakeSteps:
    """setupapi.SetupApi's handle(), recording what reached it."""

    def __init__(self):
        self.calls = []

    def handle(self, name, payload):
        self.calls.append((name, payload))
        return (200, {"step": name}) if name == "begin" else (404, {"error": "No such setup step"})


def headers(cookie=None, **more):
    """Request headers as http.server gives them."""
    message = Message()
    if cookie:
        message["Cookie"] = cookie
    for name, value in more.items():
        message[name.replace("_", "-")] = value
    return message


def cookie_from(reply, name):
    """The `name=value` part of the reply's Set-Cookie, if it sets `name`."""
    value = reply.headers.get("Set-Cookie", "")
    return value.split(";")[0] if value.startswith(name + "=") else None


class Host:
    """A FormPages as a host builds it, and what it was built from."""

    def __init__(self, folder, resume_setup="", json_reply=compact_json):
        folder.mkdir(exist_ok=True)
        self.clock = Clock()
        self.page = FakePage()
        self.password = PasswordStore(folder / "web.json")
        self.sessions = Sessions(self.clock, cookie_name="widget_session", cookie_path="/area",
                                 password=self.password)
        self.lockout = Lockout(self.clock)
        self.shown = []
        self.codes = ResetCodes(folder, show=lambda code, expires: self.shown.append(code))
        forms = FormTokens(self.clock, cookie_name="widget_form", cookie_path="/area")
        self.steps = FakeSteps()
        self.pages = FormPages(self.page, self.password, self.sessions, self.lockout, self.codes, forms, WIDGET,
                               prefix="/area", shell=lambda title, body: f"<title>{title}</title>{body}",
                               home=lambda: "/area", log=logging.getLogger("tests.widget"),
                               resume_setup=resume_setup, setup=self.steps, json_reply=json_reply,
                               clock=self.clock)

    def signed_in(self):
        """A session cookie and its form token, made directly."""
        token, session = self.sessions.create()
        return f"widget_session={token}", session.csrf

    def pre_session(self):
        """A pre-session form's cookie and hidden token, as the reset page hands them out."""
        reply = self.pages.reset_page(200, headers())
        token = re.search(r'name="form" value="([^"]+)"', reply.body.decode()).group(1)
        return cookie_from(reply, "widget_form"), html.unescape(token)

    def reset(self, code, new=NEW_PASSWORD, again=None):
        cookie, token = self.pre_session()
        fields = {"form": token, "code": code, "password": new, "again": new if again is None else again}
        return self.pages.reset(fields, headers(cookie), CLIENT)


@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path)
    made.password.set(PASSWORD)
    return made


def test_the_settings_page_needs_a_session(host):
    reply = host.pages.settings_page(headers())
    assert reply.status == 303 and reply.headers["Location"] == "/area/login"
    cookie, csrf = host.signed_in()
    reply = host.pages.settings_page(headers(cookie))
    assert reply.status == 200 and reply.content_type == HTML
    assert f"<title>Widget settings</title><form csrf={csrf}>" in reply.body.decode()


def test_the_sign_in_page_posts_under_the_prefix_in_the_hosts_words(host):
    reply = host.pages.login_page(200, headers())
    page = reply.body.decode()
    assert "<h1>Widget settings</h1>" in page and 'action="/area/login"' in page and 'action="/area/forgot"' in page
    assert page.endswith(WIDGET.back) and 'name="next"' not in page
    cookie = reply.headers["Set-Cookie"]
    assert cookie.startswith("widget_form=") and "Path=/area;" in cookie and "HttpOnly" in cookie
    assert 'name="next" value="setup"' in host.pages.login_page(200, headers(), to_setup=True).body.decode()


def test_an_error_on_the_sign_in_page_is_escaped_and_announced(host):
    page = host.pages.login_page(401, headers(), "<b>no</b>").body.decode()
    assert '<p class="error" role="alert" id="login-error">&lt;b&gt;no&lt;/b&gt;</p>' in page
    assert 'aria-describedby="login-error" aria-invalid="true"' in page


def test_without_a_password_the_sign_in_page_is_the_first_claim(tmp_path):
    host = Host(tmp_path)
    page = host.pages.login_page(200, headers()).body.decode()
    assert "<h1>Claim this widget</h1>" in page and "Claim intro." in page and WIDGET.claim_hint in page
    assert 'action="/area/reset"' in page and "Back to sign in" not in page and page.endswith(WIDGET.back)
    assert "Set password and sign in" in page
    assert host.pages.sign_in({"password": NEW_PASSWORD}, headers(), CLIENT).status == 403


def test_the_reset_page_says_where_the_code_is(host):
    page = host.pages.reset_page(200, headers(), asked=True).body.decode()
    assert "<h1>Set a new password</h1>" in page and "Reset intro." in page and WIDGET.claim_hint not in page
    assert '<p class="notice" role="status">Look at the widget.</p>' in page and "Show another code" in page
    assert '<label for="code">Code from the widget</label>' in page and "No code? Look harder." in page
    assert '<a href="/area/login">Back to sign in</a>' in page and "Set new password and sign in" in page
    assert ">Show a code<" in host.pages.reset_page(200, headers()).body.decode()


def test_a_reset_page_error_marks_its_field(host):
    page = host.pages.reset_page(422, headers(), "Too short", "password").body.decode()
    assert 'aria-describedby="new-password-hint reset-error" aria-invalid="true"' in page
    assert page.count('aria-invalid="true"') == 1


def test_a_right_password_starts_a_session_and_goes_home(host):
    reply = host.pages.sign_in({"password": PASSWORD}, headers(), CLIENT)
    assert reply.status == 303 and reply.headers["Location"] == "/area"
    cookie = reply.headers["Set-Cookie"]
    assert cookie.startswith("widget_session=") and "Path=/area;" in cookie and "SameSite=Strict" in cookie
    assert host.pages.settings_page(headers(cookie.split(";")[0])).status == 200


def test_a_wrong_password_gets_no_session(host):
    reply = host.pages.sign_in({"password": "nope nope nope"}, headers(), CLIENT)
    assert reply.status == 401 and "That password isn't right." in reply.body.decode()
    assert cookie_from(reply, "widget_session") is None


def test_repeated_wrong_passwords_lock_the_address_out(host):
    for _ in range(MAX_FAILURES):
        assert host.pages.sign_in({"password": "nope nope nope"}, headers(), CLIENT).status == 401
    reply = host.pages.sign_in({"password": PASSWORD}, headers(), CLIENT)
    assert reply.status == 429 and "Too many attempts" in reply.body.decode()
    assert host.pages.sign_in({"password": PASSWORD}, headers(), "192.168.1.21").status == 303


def test_a_sign_in_from_the_wizard_goes_back_to_it_only_where_offered(tmp_path):
    offered = Host(tmp_path / "a", resume_setup="/area/setup/")
    plain = Host(tmp_path / "b")
    for host in (offered, plain):
        host.password.set(PASSWORD)
    reply = offered.pages.sign_in({"password": PASSWORD, "next": "setup"}, headers(), CLIENT)
    assert reply.headers["Location"] == "/area/setup/"
    wrong = offered.pages.sign_in({"password": "nope nope nope", "next": "setup"}, headers(), CLIENT)
    assert 'name="next" value="setup"' in wrong.body.decode()
    reply = offered.pages.sign_in({"password": PASSWORD, "next": "https://evil.example.com/"}, headers(), CLIENT)
    assert reply.headers["Location"] == "/area"
    reply = plain.pages.sign_in({"password": PASSWORD, "next": "setup"}, headers(), CLIENT)
    assert reply.headers["Location"] == "/area"
    wrong = plain.pages.sign_in({"password": "nope nope nope", "next": "setup"}, headers(), CLIENT)
    assert 'name="next"' not in wrong.body.decode()


def test_asking_for_a_code_needs_this_browsers_form_token(host):
    cookie, token = host.pre_session()
    _, other = host.pre_session()
    for fields, sent in [({"form": token}, None), ({}, cookie), ({"form": other}, cookie)]:
        reply = host.pages.ask_for_code(fields, headers(sent))
        assert reply.status == 403 and WIDGET.no_code_made in reply.body.decode()
    assert host.shown == []
    reply = host.pages.ask_for_code({"form": token}, headers(cookie))
    assert reply.status == 303 and reply.headers["Location"] == "/area/reset?asked"
    assert len(host.shown) == 1


def test_asking_again_too_soon_reads_the_same_and_keeps_the_code(host):
    cookie, token = host.pre_session()
    first = host.pages.ask_for_code({"form": token}, headers(cookie))
    again = host.pages.ask_for_code({"form": token}, headers(cookie))
    assert first == again and len(host.shown) == 1


def test_a_reset_checks_the_new_password_before_spending_the_code(host):
    host.codes.issue(host.clock())
    code = host.shown[-1]
    reply = host.reset(code, "short")
    assert reply.status == 422 and 'aria-describedby="new-password-hint reset-error"' in reply.body.decode()
    assert host.reset(code, NEW_PASSWORD, NEW_PASSWORD + "!").status == 422
    assert host.reset(code).status == 303


def test_a_wrong_code_changes_nothing(host):
    reply = host.reset("000000")
    assert reply.status == 401 and WRONG_CODE in reply.body.decode()
    assert host.password.check(PASSWORD)


def test_a_reset_needs_the_form_token(host):
    host.codes.issue(host.clock())
    fields = {"form": "1.bad", "code": host.shown[-1], "password": NEW_PASSWORD, "again": NEW_PASSWORD}
    assert host.pages.reset(fields, headers(), CLIENT).status == 403
    assert host.password.check(PASSWORD)


def test_a_reset_ends_every_session_clears_the_lockout_and_signs_this_browser_in(host, caplog):
    caplog.set_level("INFO")
    old, _ = host.signed_in()
    for _ in range(MAX_FAILURES):
        host.pages.sign_in({"password": "nope nope nope"}, headers(), CLIENT)
    host.codes.issue(host.clock())
    reply = host.reset(host.shown[-1])
    assert reply.status == 303 and reply.headers["Location"] == "/area"
    new = cookie_from(reply, "widget_session")
    assert host.pages.settings_page(headers(new)).status == 200
    assert host.pages.settings_page(headers(old)).status == 303
    assert host.password.check(NEW_PASSWORD)
    assert host.pages.sign_in({"password": NEW_PASSWORD}, headers(), CLIENT).status == 303  # not locked out
    assert ("tests.widget", "Widget password reset") in [(r.name, r.getMessage()) for r in caplog.records]


def test_a_code_that_cannot_be_checked_says_so_in_the_hosts_words(host, caplog, monkeypatch):
    def broken(typed, now):
        raise PermissionError("reset.json")

    monkeypatch.setattr(host.codes, "verify", broken)
    reply = host.reset("123456")
    assert reply.status == 503 and WIDGET.cant_check in reply.body.decode()
    assert [(r.name, r.getMessage()) for r in caplog.records if r.levelname == "WARNING"] == [
        ("tests.widget", "Can't check a settings reset code: PermissionError")]
    assert host.password.check(PASSWORD)


def test_signing_out_needs_the_sessions_token(host):
    cookie, csrf = host.signed_in()
    reply = host.pages.sign_out({"csrf": "forged"}, headers(cookie))
    assert reply.status == 303 and reply.headers["Location"] == "/area/login"
    expired = reply.headers["Set-Cookie"]
    assert expired.startswith("widget_session=;") and "Path=/area;" in expired and "Max-Age=0" in expired
    assert host.pages.settings_page(headers(cookie)).status == 200  # a forged sign-out ends nothing
    host.pages.sign_out({"csrf": csrf}, headers(cookie))
    assert host.pages.settings_page(headers(cookie)).status == 303


def test_a_save_needs_a_session_and_its_token(host):
    reply = host.pages.save({"csrf": "x", "title": "x"}, headers())
    assert reply.status == 303 and reply.headers["Location"] == "/area/login"
    cookie, _ = host.signed_in()
    reply = host.pages.save({"csrf": "forged", "title": "x"}, headers(cookie))
    assert reply.status == 403 and reply.content_type == TEXT
    assert host.page.saved == []


def test_a_save_says_what_happened_in_the_hosts_words(host):
    cookie, csrf = host.signed_in()
    reply = host.pages.save({"csrf": csrf, "title": "Glossop"}, headers(cookie))
    assert reply.status == 200 and "<title>Saved – Widget settings</title>" in reply.body.decode()
    assert "Saved for the widget." in reply.body.decode()
    assert host.page.saved == [{"title": "Glossop"}]  # the token isn't one of the settings
    reply = host.pages.save({"csrf": csrf, "title": "bad"}, headers(cookie))
    assert reply.status == 422 and "<title>Widget settings: fix the errors</title>" in reply.body.decode()
    assert "Too bad" in reply.body.decode() and "'title': 'bad'" in reply.body.decode()


# --- The setup wizard's checks ---


def test_the_wizard_gets_the_sessions_form_token_only_when_signed_in(host):
    reply = host.pages.wizard_session(headers())
    assert reply.status == 401 and json.loads(reply.body) == {"error": "Sign in first"}
    assert reply.content_type == "application/json"
    cookie, csrf = host.signed_in()
    reply = host.pages.wizard_session(headers(cookie))
    assert reply.status == 200 and reply.body == f'{{"csrf":"{csrf}"}}'.encode()


def test_a_step_needs_the_session_its_token_in_the_header_and_json(host):
    cookie, csrf = host.signed_in()
    _, other = host.signed_in()
    for sent, status in [
        (headers(X_CSRF_Token=csrf, Content_Type="application/json"), 401),  # no session
        (headers(cookie, Content_Type="application/json"), 403),  # no token
        (headers(cookie, X_CSRF_Token="", Content_Type="application/json"), 403),
        (headers(cookie, X_CSRF_Token=other, Content_Type="application/json"), 403),  # another session's
        (headers(cookie, X_CSRF_Token=csrf), 415),
        (headers(cookie, X_CSRF_Token=csrf, Content_Type="application/x-www-form-urlencoded"), 415),
    ]:
        reply = host.pages.wizard_refusal(sent)
        assert reply is not None and reply.status == status, dict(sent)
    assert json.loads(host.pages.wizard_refusal(headers(cookie)).body) == {"error": "Reload the widget."}
    assert host.pages.wizard_refusal(headers(cookie, X_CSRF_Token=csrf, Content_Type="application/json")) is None


def test_a_step_runs_with_its_decoded_body(host):
    reply = host.pages.wizard_step("begin", b'{"x": [1, 2]}')
    assert reply.status == 200 and json.loads(reply.body) == {"step": "begin"}
    assert host.steps.calls == [("begin", {"x": [1, 2]})]
    reply = host.pages.wizard_step("nosuch", b"{}")
    assert reply.status == 404 and host.steps.calls[-1] == ("nosuch", {})


@pytest.mark.parametrize("body", [b"not json", b"[" * 50 + b"]" * 50, b'{"n": NaN}', b"{" + b" " * 20000 + b"}"])
def test_a_step_that_is_not_sensible_json_never_runs(host, body):
    reply = host.pages.wizard_step("begin", body)
    assert reply.status == 400 and "error" in json.loads(reply.body)
    assert host.steps.calls == []


def test_a_host_answers_the_wizard_in_its_own_json(tmp_path):
    host = Host(tmp_path, json_reply=lambda status, value: Reply(status, b"own", "application/json; charset=utf-8"))
    assert host.pages.wizard_session(headers()).body == b"own"
    assert host.pages.wizard_step("begin", b"{}").body == b"own"


def test_compact_json_is_ascii_and_refuses_nan():
    reply = compact_json(200, {"name": "Café", "n": [1, 2]})
    assert reply.body == b'{"name":"Caf\\u00e9","n":[1,2]}' and reply.content_type == "application/json"
    with pytest.raises(ValueError):
        compact_json(200, {"n": float("nan")})
