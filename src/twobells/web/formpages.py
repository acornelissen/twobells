"""The settings site's pages and forms (web/settingsite.py, under /settings).

FormPages draws the sign-in and reset pages and answers their forms: sign
in, ask for a reset code, reset the password with it (which is also the
first claim, when no password is set), sign out, and save a settings form.
It also checks a setup wizard step (the session, its token in
X-CSRF-Token, JSON) and runs it. The host routes its own requests, refuses
outsiders, checks the origin, reads bodies (with read_body, under its own
limits), serves the wizard's files and sends its own security headers; it
hands the parsed form (or the step's body) here and sends back the Reply.

What the host decides is passed in: the path prefix, the sessions and form
tokens (each with its own cookie name and path), the reset codes (shown on
the board, or put in the log), the wording (asked for afresh on every page,
as where codes go can change while it runs), the settings forms, the
wizard's API and its JSON, and where a new session lands.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BufferedIOBase
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import parse_qs

from ..setup import api
from ..setup.keytest import KeyResult
from .auth import (
    MIN_PASSWORD,
    TOO_COMMON,
    FormTokens,
    Lockout,
    PasswordCheck,
    PasswordStore,
    ResetCodes,
    ResetLimited,
    ResetShutOut,
    Sessions,
    SignInResult,
    common_password,
    csrf_matches,
)

if TYPE_CHECKING:
    from .keyspage import Tested

TEXT = "text/plain; charset=utf-8"
HTML = "text/html; charset=utf-8"
# The largest sign-in or settings form either page takes.
MAX_FORM_BODY = 64 * 1024
# The only kinds of file the setup wizard serves, each with its type said outright.
WIZARD_TYPES = {
    ".html": HTML,
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
}
# The same words whether the code was wrong, used, run out, voided or never
# issued, so a guesser learns nothing about whether there is one.
WRONG_CODE = "That code isn't right, or it has run out. Ask for a new code and try again."
# Asking again from an address with no tries left at the code that stands:
# it can't use that code, so it is told when it may ask for a new one.
SHUT_OUT = ("Too many wrong tries from this device, so it can't use the code that is out now. "
            "That code runs out at {until}; ask for a new one then.")
# Signing in while every client together has had too many wrong passwords:
# each try waits up to a minute (auth.MAX_SLOW_S) after the last, and clients take
# turns, keeping their place for two minutes (auth.WAITING_KEEP_S) after each try.
SLOWED = ("Lots of wrong passwords have been tried lately, so signing in is slowed for everyone. "
          "Try again in a minute.")
_LENGTH = re.compile(r"[0-9]{1,15}")
# A setup wizard step's name, as it comes in the path.
_STEP_NAME = re.compile(r"[a-z]{1,20}")
# The most sessions with a saved notice waiting for their next page.
_MAX_NOTICES = 64


def saved_notice(changed: list[str], then: str) -> str:
    """What a save says on the page after it: what changed, in words, and
    when it takes effect (`then`)."""
    if not changed:
        return "Saved. Nothing had changed."
    count = f"{len(changed)} change{'' if len(changed) == 1 else 's'}"
    return f"Saved {count}: {'; '.join(changed)}. {then}".strip()


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes = b""
    content_type: str = TEXT
    headers: dict[str, str] = field(default_factory=dict)  # Location, Set-Cookie


class SettingsPage(Protocol):
    """An everyday form (a groupform.GroupForm): drawn for a signed-in
    visitor, and saved from their fields."""

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None) -> str: ...

    # field -> error, empty when saved; what was saved is added to `changed` in words
    def save(self, fields: dict[str, str], changed: list[str] | None = None) -> dict[str, str]: ...


class KeyForms(Protocol):
    """The Keys page (a keyspage.KeysPage): a form per key, each saved and
    tested on its own, and drawn with a test's result."""

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None, tested: Tested | None = None) -> str: ...

    def knows(self, name: str) -> bool: ...  # whether form `name` is on the page

    # field -> error, empty when saved; as SettingsPage.save, for the form fields["key"]
    def save(self, fields: dict[str, str], changed: list[str] | None = None) -> dict[str, str]: ...

    def test_key(self, fields: dict[str, str]) -> Tested | None: ...  # the saved key's result, or None: no such test


@dataclass(frozen=True)
class Notice:
    """What a page says once, after a save or a key test redirected to it:
    the page, its title's first word, what changed, and a key test's result."""

    page: SettingsPage | KeyForms
    title: str  # "Saved", "Key tested"
    message: str = ""
    tested: Tested | None = None


class SetupSteps(Protocol):
    """setupapi.SetupApi: a step's (status, JSON-ready reply) for its decoded
    body, and a key test under the wizard's limit and runner."""

    def handle(self, name: str, payload: object) -> tuple[int, dict]: ...

    def test_key(self, name: str, value: str | None = None, app_id: str | None = None) -> KeyResult: ...


class Headers(Protocol):
    """A request's headers, as http.server gives them."""

    def get(self, name: str, /) -> str | None: ...

    def get_all(self, name: str, /) -> list[str] | None: ...

    def get_content_type(self) -> str: ...


@dataclass(frozen=True)
class Wording:
    """What a host calls itself, and how it tells the owner where reset codes
    go. Each is trusted text, HTML where it says so."""

    title: str  # "Board settings": the settings site's title, and the sign-in page's heading
    claim_title: str  # "Claim this board"
    claim_intro: str  # the first claim's opening paragraph
    reset_intro: str  # a forgotten password's opening paragraph
    asked: str  # HTML: where to look, once a code has been asked for
    ask: str  # the button asking for a code
    ask_again: str  # the same button once one has been asked for
    code_label: str  # "Code from the board"
    no_code: str  # HTML: where else to get a code, and how often
    claim_hint: str  # HTML paragraph on the first claim's page, or ""
    back: str  # HTML paragraph closing every pre-session page, or ""
    no_code_made: str  # the error when the form asking for a code had run out
    cant_check: str  # the error when the code can't be checked
    saved: str  # when what was saved takes effect, said after a save
    reset_logged: str  # the log line after a reset
    wrong_step_token: str  # the error when a wizard step's X-CSRF-Token is wrong


def document(title: str, body: str, stylesheet: str, script: str = "") -> str:
    """The HTML document around a page's content, with `stylesheet` its CSS's
    path and `script`, if given, the path of the one script it runs, deferred."""
    deferred = f'<script src="{html.escape(script)}" defer></script>' if script else ""
    return (
        '<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f'<link rel="stylesheet" href="{stylesheet}">{deferred}</head>'
        f"<body><main>{body}</main></body></html>"
    )


MAX_FORM_FIELDS = 300


def parse_form(body: bytes) -> dict[str, str]:
    """An urlencoded form's fields, the last value of each. Bytes that aren't
    UTF-8 become U+FFFD; past MAX_FORM_FIELDS fields it is refused (ValueError),
    which the hosts answer with 413."""
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS)
    return {key: values[-1] for key, values in parsed.items()}


def read_body(headers: Headers, stream: BufferedIOBase, limit: int) -> bytes | int:
    """A POST's body, or the status refusing it unread: 400 for any
    Transfer-Encoding or more than one Content-Length, 411 for a missing one
    or one that isn't plain digits, 413 for more than `limit` bytes. Only one
    plain length is taken, so nothing in front of the host can read the body
    as ending somewhere else. A refusal leaves the body on the wire, so the
    host sends it with Connection: close."""
    if headers.get("Transfer-Encoding") is not None or len(headers.get_all("Content-Length") or []) > 1:
        return 400
    length = (headers.get("Content-Length") or "").strip()
    if not _LENGTH.fullmatch(length):
        return 413 if length.isascii() and length.isdigit() else 411
    if int(length) > limit:
        return 413
    return stream.read(int(length))


def redirect(where: str, extra: dict[str, str] | None = None) -> Reply:
    return Reply(303, b"", TEXT, {"Location": where, **(extra or {})})


# What a reply that isn't strict JSON (a NaN, say) becomes, rather than a
# dropped connection or NaN on the wire.
JSON_FAILED = {"error": "Something went wrong; try again"}


def compact_json(status: int, value: dict) -> Reply:
    """Compact ASCII JSON, as the dashboard's API sends it."""
    try:
        body = json.dumps(value, separators=(",", ":"), allow_nan=False)
    except ValueError:
        status, body = 500, json.dumps(JSON_FAILED)
    return Reply(status, body.encode("ascii"), "application/json")


class FormPages:
    """The sign-in, reset and settings pages and forms, under `prefix`, and
    the setup wizard's session and step checks.

    `page` is the settings form drawn and saved when no other is named.
    `wording()` gives the words for each page. `home()` is where a new
    session lands. `resume_setup`, if set, is the wizard's page: a sign-in
    started there (`?next=setup`) goes back to it, at the shortcut's step
    if it was one of `setup_steps` (`&step=stops`). `shell(title, body)`
    wraps a page in the HTML document; `page_shell`, if given, wraps a
    signed-in visitor's settings pages instead (with their script), so the
    sign-in and reset pages never have one. Log lines go to `log`, so each
    host's keep their own name. `setup` runs the wizard's steps, answered
    with `json_reply`."""

    def __init__(self, page: SettingsPage, password: PasswordStore, sessions: Sessions, lockout: Lockout,
                 codes: ResetCodes, forms: FormTokens, wording: Callable[[], Wording], *, prefix: str,
                 shell: Callable[[str, str], str], home: Callable[[], str], log: logging.Logger,
                 page_shell: Callable[[str, str], str] | None = None, resume_setup: str = "",
                 setup_steps: tuple[str, ...] = (), setup: SetupSteps | None = None,
                 json_reply: Callable[[int, dict], Reply] = compact_json,
                 clock: Callable[[], float] = time.time) -> None:
        self._page = page
        self._password = password
        self._sessions = sessions
        self._lockout = lockout
        self._checker = PasswordCheck(password, lockout)
        self._codes = codes
        self._forms = forms
        self._wording = wording
        self._prefix = prefix
        self._shell = shell
        self._page_shell = page_shell or shell
        self._home = home
        self._log = log
        self._resume_setup = resume_setup
        self._setup_steps = setup_steps
        self._setup = setup
        self._json = json_reply
        self._clock = clock
        # Each save's or key test's notice, by its session's form token, until
        # the page after it (a GET, as both answer with a redirect) shows it.
        self._notices: dict[str, Notice] = {}
        self._notices_lock = threading.Lock()

    # Pages

    def settings_page(self, headers: Headers, page: SettingsPage | KeyForms | None = None,
                      title: str | None = None, saved: bool = False) -> Reply:
        """A settings form (`page`, or the one given at the start) for a
        signed-in visitor, under `title` (or the wording's); sign-in for
        anyone else. `saved` (the address's ?saved, or ?tested) shows what
        the session's last save on that page changed, or its key test found, once."""
        session = self._sessions.get(self._token(headers))
        if session is None:
            return redirect(self._prefix + "/login")
        page = page or self._page
        title = title or self._wording().title
        notice = None
        if saved:
            with self._notices_lock:
                notice = self._notices.get(session.csrf)
                if notice is not None and notice.page is page:  # said on its own page only
                    del self._notices[session.csrf]
                else:
                    notice = None
        if notice is None:
            return self._signed_in_html(200, title, page.render(session.csrf))
        if notice.tested is not None:  # only the Keys page tests, so this is it
            body = cast("KeyForms", page).render(session.csrf, message=notice.message, tested=notice.tested)
        else:
            body = page.render(session.csrf, message=notice.message)
        return self._signed_in_html(200, f"{notice.title} – {title}", body)

    def login_page(self, status: int, headers: Headers, message: str = "", to_setup: bool = False,
                   step: str = "") -> Reply:
        """Sign in, or the first claim while no password is set. With
        `to_setup`, signing in goes back to the wizard, at `step` if it is one of setup_steps."""
        if not self._password.is_set():
            return self.reset_page(status, headers, message)  # the first claim
        wording = self._wording()
        token, cookie = self._form_token(headers)
        error = (f'<p class="error" role="alert" id="login-error">{html.escape(message, quote=False)}</p>'
                 if message else "")
        described = ' aria-describedby="login-error" aria-invalid="true"' if message else ""
        then = '<input type="hidden" name="next" value="setup">' if to_setup else ""
        if to_setup and step in self._setup_steps:
            then += f'<input type="hidden" name="step" value="{html.escape(step)}">'
        body = (
            f"<h1>{wording.title}</h1>"
            f"{error}"
            f'<form method="post" action="{self._prefix}/login" class="signin">{then}'
            '<label for="password">Password</label>'
            '<input id="password" name="password" type="password" autocomplete="current-password" '
            f"required autofocus{described}>"
            '<button type="submit" class="primary">Sign in</button></form>'
            f'<form method="post" action="{self._prefix}/forgot" class="forgot">'
            f'<input type="hidden" name="form" value="{token}">'
            '<button type="submit">Forgot password?</button></form>'
            f"{wording.back}"
        )
        return self._html(status, "Sign in", body, cookie)

    def reset_page(self, status: int, headers: Headers, message: str = "", invalid: str = "",
                   asked: bool = False) -> Reply:
        """Ask for a code, then type it with a new password: for a forgotten
        password, and for the first claim when there is none. `invalid` names
        the field `message` is about."""
        wording = self._wording()
        claim = not self._password.is_set()
        token, cookie = self._form_token(headers)
        title = wording.claim_title if claim else "Set a new password"
        intro = wording.claim_intro if claim else wording.reset_intro
        parts = [f"<h1>{title}</h1>", f"<p>{intro}</p>"]
        if asked:
            parts.append(f'<p class="notice" role="status">{wording.asked}</p>')
        if message:
            parts.append(f'<p class="error" role="alert" id="reset-error">{html.escape(message, quote=False)}</p>')

        def flagged(name: str, hint: str = "") -> str:
            """A field's description, and the error when it is about this field."""
            wrong = bool(message) and invalid == name
            described = " ".join(part for part in (hint, "reset-error" if wrong else "") if part)
            attributes = f' aria-describedby="{described}"' if described else ""
            return attributes + (' aria-invalid="true"' if wrong else "")

        def focus(name: str) -> str:
            """The cursor goes to the field the error is about: an alert already
            on the page as it loads isn't always said, but a field's description is."""
            return " autofocus" if message and invalid == name else ""

        ask = wording.ask_again if asked else wording.ask
        parts.append(
            f'<form method="post" action="{self._prefix}/forgot" class="forgot">'
            f'<input type="hidden" name="form" value="{token}">'
            f'<button type="submit">{ask}</button></form>'
            f'<form method="post" action="{self._prefix}/reset" class="signin">'
            f'<input type="hidden" name="form" value="{token}">'
            f'<label for="code">{wording.code_label}</label>'
            '<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="7" '
            f'required{" autofocus" if asked or invalid == "code" else ""}{flagged("code")}>'
            '<label for="new-password">New password</label>'
            f'<p class="hint" id="new-password-hint">At least {MIN_PASSWORD} characters, and not a common one.</p>'
            '<input id="new-password" name="password" type="password" autocomplete="new-password" '
            f'minlength="{MIN_PASSWORD}" required{focus("password")}{flagged("password", "new-password-hint")}>'
            '<label for="again">New password again</label>'
            '<input id="again" name="again" type="password" autocomplete="new-password" '
            f'required{focus("again")}{flagged("again")}>'
            f'<button type="submit" class="primary">{"Set password" if claim else "Set new password"} and sign in'
            "</button></form>"
            f'<p class="hint after">{wording.no_code}</p>'
        )
        if claim:
            parts.append(wording.claim_hint)
        else:
            parts.append(f'<p class="after"><a href="{self._prefix}/login">Back to sign in</a></p>')
        parts.append(wording.back)
        return self._html(status, title, "".join(parts), cookie)

    # Forms

    def sign_in(self, fields: dict[str, str], headers: Headers, client: str) -> Reply:
        """A password typed on the sign-in page, rate limited by `client`'s
        address (an IPv6 one by its /64) and across every client."""
        if not self._password.is_set():
            return self.login_page(403, headers)
        to_setup = bool(self._resume_setup) and fields.get("next") == "setup"
        # Only a step on the list goes into the address signing in leads to.
        step = fields.get("step", "") if to_setup and fields.get("step") in self._setup_steps else ""
        result = self._checker.sign_in(client, fields.get("password", ""))
        if result is SignInResult.BUSY:
            return self.login_page(429, headers, "Busy, try again in a moment.", to_setup, step)
        if result is SignInResult.LOCKED:
            return self.login_page(429, headers, "Too many attempts. Try again in 15 minutes.", to_setup, step)
        if result is SignInResult.SLOWED:
            return self.login_page(429, headers, SLOWED, to_setup, step)
        if result is SignInResult.WRONG:
            return self.login_page(401, headers, "That password isn't right.", to_setup, step)
        return self._signed_in(to_setup, step)

    def ask_for_code(self, fields: dict[str, str], headers: Headers, client: str) -> Reply:
        """Issue a reset code, if this browser's form token is good. The reply
        is the same whether a code was issued, one still live stands, or it
        was limited or failed; but `client`'s address, shut out of the code
        that stands by its wrong tries, is told so and when it runs out."""
        if not self._form_token_ok(fields, headers):
            return self.reset_page(403, headers, self._wording().no_code_made)
        try:
            self._codes.issue(self._clock(), client=client)
        except ResetShutOut as shut:
            return self.reset_page(429, headers, SHUT_OUT.format(until=shut.until))
        except ResetLimited:
            pass  # the code already out stands; the page reads the same either way
        except OSError as error:
            self._log.warning("Can't issue a settings reset code: %s", type(error).__name__)
        return redirect(self._prefix + "/reset?asked")

    def reset(self, fields: dict[str, str], headers: Headers, client: str) -> Reply:
        """A new password with a reset code, its wrong tries counted by
        `client`'s address: every session ends, and this browser starts a new one."""
        if not self._form_token_ok(fields, headers):
            return self.reset_page(403, headers, "That form had run out, so nothing was changed. Try again.")
        new = fields.get("password", "")
        # Checked before the code, so a typo or a common password here doesn't use the code up.
        if len(new) < MIN_PASSWORD:
            return self.reset_page(422, headers, f"Use at least {MIN_PASSWORD} characters for the new password.",
                                   "password")
        if common_password(new):
            return self.reset_page(422, headers, TOO_COMMON, "password")
        if new != fields.get("again", ""):
            return self.reset_page(422, headers, "The two new passwords don't match.", "again")
        try:
            right = self._codes.verify(fields.get("code", ""), self._clock(), client)
        except OSError as error:
            self._log.warning("Can't check a settings reset code: %s", type(error).__name__)
            return self.reset_page(503, headers, self._wording().cant_check)
        if not right:
            return self.reset_page(401, headers, WRONG_CODE, "code")
        self._password.set(new)
        self._log.info(self._wording().reset_logged)
        self._sessions.end_all()
        self._lockout.succeeded(client)
        return self._signed_in()

    def sign_out(self, fields: dict[str, str], headers: Headers) -> Reply:
        """End the session if the form carries its token; clear the cookie either way."""
        token = self._token(headers)
        if csrf_matches(self._sessions.get(token), fields.get("csrf", "")):
            self._sessions.end(token)
        return redirect(self._prefix + "/login", {"Set-Cookie": self._sessions.expired_cookie()})

    def save(self, fields: dict[str, str], headers: Headers, page: SettingsPage | None = None,
             title: str | None = None, where: str | None = None) -> Reply:
        """A settings form (`page`, or the one given at the start), from a
        signed-in session with its token. Saved, it redirects to `where`
        (the page's address, or the prefix) with ?saved, whose page says
        what changed, so reloading it can't post the form again; refused,
        the form is drawn again under `title` (or the wording's) with what
        was typed and the problems."""
        session = self._sessions.get(self._token(headers))
        if session is None:
            return redirect(self._prefix + "/login")
        if not csrf_matches(session, fields.get("csrf", "")):
            return Reply(403, b"Forbidden: form expired, reload the page")
        fields.pop("csrf", None)
        page = page or self._page
        wording = self._wording()
        title = title or wording.title
        changed: list[str] = []
        errors = page.save(fields, changed)
        if errors:
            body = page.render(session.csrf, errors=errors, values=fields)
            return self._signed_in_html(422, f"{title}: fix the errors", body)
        self._notice(session.csrf, Notice(page, "Saved", saved_notice(changed, wording.saved)))
        return redirect((where or self._prefix) + "?saved")

    def key_form(self, fields: dict[str, str], headers: Headers, page: KeyForms, title: str, where: str) -> Reply:
        """One of the Keys page's forms (fields["key"]), from a signed-in
        session with its token, as save is. Its button (fields["do"]) saves
        its key, saves then tests it, or tests the saved key. Done, it
        redirects to `where` with ?saved (or ?tested, after a test alone),
        whose page says what changed and what the test found; refused, the
        page is drawn again under `title` with the problems, its key boxes empty."""
        session = self._sessions.get(self._token(headers))
        if session is None:
            return redirect(self._prefix + "/login")
        if not csrf_matches(session, fields.get("csrf", "")):
            return Reply(403, b"Forbidden: form expired, reload the page")
        fields.pop("csrf", None)
        do = fields.get("do") or "save"  # Enter in a box sends the first button, Save key
        if do not in ("save", "save_test", "test") or not page.knows(fields.get("key", "")):
            return Reply(400, b"Bad request: no such key or button")
        changed: list[str] = []
        if do != "test":
            errors = page.save(fields, changed)
            if errors:
                body = page.render(session.csrf, errors=errors, values=fields)
                return self._signed_in_html(422, f"{title}: fix the errors", body)
        tested = page.test_key(fields) if do != "save" else None
        if do != "save" and tested is None:
            return Reply(400, b"Bad request: no such key to test")
        if tested is not None and not changed:  # nothing saved: a test alone
            self._notice(session.csrf, Notice(page, "Key tested", "", tested))
            return redirect(where + "?tested")
        self._notice(session.csrf, Notice(page, "Saved", saved_notice(changed, self._wording().saved), tested))
        return redirect(where + "?saved")

    # The setup wizard

    def wizard_cross_site(self, headers: Headers) -> Reply | None:
        """The refusal for any request to the wizard's API from another
        site's page (Sec-Fetch-Site: cross-site), session or not, before
        anything else is looked at; None for any other request."""
        if (headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            return self._json(403, {"error": "Not from another site"})
        return None

    def wizard_unknown_step(self, name: str) -> Reply | None:
        """The refusal for a step name that isn't 1 to 20 plain lowercase
        letters, so no other name ever reaches the wizard's API; None for one
        that is. A host checks it before reading the body."""
        if _STEP_NAME.fullmatch(name):
            return None
        return self._json(404, {"error": "No such setup step"})

    def wizard_session(self, headers: Headers) -> Reply:
        """The session's form token, for the wizard's page to send back in X-CSRF-Token."""
        session = self._sessions.get(self._token(headers))
        if session is None:
            return self._json(401, {"error": "Sign in first"})
        return self._json(200, {"csrf": session.csrf})

    def wizard_refusal(self, headers: Headers) -> Reply | None:
        """Why a wizard step can't run, or None when it can: it needs the
        session, its form token in X-CSRF-Token, and JSON. The host checks
        the origin and the body's size itself."""
        session = self._sessions.get(self._token(headers))
        if session is None:
            return self._json(401, {"error": "You've been signed out. Reload the page and sign in again."})
        if not csrf_matches(session, headers.get("X-CSRF-Token") or ""):
            return self._json(403, {"error": self._wording().wrong_step_token})
        if headers.get_content_type() != "application/json":
            return self._json(415, {"error": "Send JSON"})
        return None

    def wizard_step(self, name: str, body: bytes) -> Reply:
        """Run step `name` with the request's JSON, once wizard_refusal has let it through."""
        unknown = self.wizard_unknown_step(name)
        if unknown is not None:
            return unknown
        try:
            payload = api.decode(body)
        except api.Invalid as error:
            return self._json(400, {"error": str(error)})
        if self._setup is None:  # no wizard here, so no steps
            return self._json(404, {"error": "No such setup step"})
        return self._json(*self._setup.handle(name, payload))

    # Helpers

    def _notice(self, csrf: str, notice: Notice) -> None:
        """Keep `notice` for the session's next page, in place of any before it."""
        with self._notices_lock:
            self._notices.pop(csrf, None)
            while len(self._notices) >= _MAX_NOTICES:
                del self._notices[next(iter(self._notices))]  # the oldest
            self._notices[csrf] = notice

    def _token(self, headers: Headers) -> str | None:
        return self._sessions.token_from(headers.get("Cookie"))

    def _html(self, status: int, title: str, body: str, extra: dict[str, str] | None = None) -> Reply:
        return Reply(status, self._shell(title, body).encode(), HTML, extra or {})

    def _signed_in_html(self, status: int, title: str, body: str) -> Reply:
        """A signed-in visitor's settings page."""
        return Reply(status, self._page_shell(title, body).encode(), HTML)

    def _form_token(self, headers: Headers) -> tuple[str, dict[str, str]]:
        """A pre-session form's hidden field, and the header that sets its cookie."""
        nonce = self._forms.nonce(headers.get("Cookie"))
        return html.escape(self._forms.form_value(nonce)), {"Set-Cookie": self._forms.cookie(nonce)}

    def _form_token_ok(self, fields: dict[str, str], headers: Headers) -> bool:
        return self._forms.check(headers.get("Cookie"), fields.get("form", ""))

    def _signed_in(self, to_setup: bool = False, step: str = "") -> Reply:
        token, _ = self._sessions.create()
        where = self._resume_setup if to_setup else self._home()
        if to_setup and step in self._setup_steps:
            where += f"?step={step}"
        return redirect(where, {"Set-Cookie": self._sessions.cookie(token)})
