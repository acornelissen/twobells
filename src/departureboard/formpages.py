"""What the two settings pages share: the Pi's (web.py, at /) and the
dashboard's (dashsettings.py, under /settings).

FormPages draws the sign-in and reset pages and answers their forms: sign
in, ask for a reset code, reset the password with it (which is also the
first claim, when no password is set), sign out, and save the settings
form. It also checks a setup wizard step (the session, its token in
X-CSRF-Token, JSON) and runs it. Each host routes its own requests, refuses
outsiders, checks the origin, reads bodies (with read_body, under its own
limits), serves the wizard's files and sends its own security headers; it
hands the parsed form (or the step's body) here and sends back the Reply.

What differs between the hosts is passed in: the path prefix ("" or
"/settings"), the sessions and form tokens (each with its own cookie name
and path), the reset codes (shown on the board, or put in the log), the
wording, the settings form, the wizard's API and its JSON, and where a new
session lands.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Protocol
from urllib.parse import parse_qs

from . import setupapi
from .auth import (
    MIN_PASSWORD,
    FormTokens,
    Lockout,
    PasswordCheck,
    PasswordStore,
    ResetCodes,
    ResetLimited,
    Sessions,
    SignInResult,
    csrf_matches,
)

TEXT = "text/plain; charset=utf-8"
HTML = "text/html; charset=utf-8"
# The largest sign-in or settings form either page takes.
MAX_FORM_BODY = 64 * 1024
# The only kinds of file the setup wizard serves, each with its type said outright.
WIZARD_TYPES = {
    ".html": HTML,
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
# The same words whether the code was wrong, used, run out, voided or never
# issued, so a guesser learns nothing about whether there is one.
WRONG_CODE = "That code isn't right, or it has run out. Ask for a new code and try again."
_LENGTH = re.compile(r"[0-9]{1,15}")


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes = b""
    content_type: str = TEXT
    headers: dict[str, str] = field(default_factory=dict)  # Location, Set-Cookie


class SettingsPage(Protocol):
    """The everyday form (settingsform.SettingsForm, dashform.DashboardForm):
    drawn for a signed-in visitor, and saved from their fields."""

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None) -> str: ...

    def save(self, fields: dict[str, str]) -> dict[str, str]: ...  # field -> error; empty when saved


class SetupSteps(Protocol):
    """setupapi.SetupApi: a step's (status, JSON-ready reply) for its decoded body."""

    def handle(self, name: str, payload: object) -> tuple[int, dict]: ...


class Headers(Protocol):
    """A request's headers, as http.server gives them."""

    def get(self, name: str, default: str | None = None) -> str | None: ...

    def get_all(self, name: str, failobj: list | None = None) -> list[str] | None: ...

    def get_content_type(self) -> str: ...


@dataclass(frozen=True)
class Wording:
    """What a host calls itself, and how it tells the owner where reset codes
    go. Each is trusted text, HTML where it says so."""

    title: str  # "Board settings": the settings page's title, and the sign-in page's heading
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
    saved: str  # the message after a save
    reset_logged: str  # the log line after a reset
    wrong_step_token: str  # the error when a wizard step's X-CSRF-Token is wrong


def document(title: str, body: str, stylesheet: str) -> str:
    """The HTML document around a page's content, with `stylesheet` its CSS's path."""
    return (
        '<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f'<link rel="stylesheet" href="{stylesheet}"></head>'
        f"<body><main>{body}</main></body></html>"
    )


def parse_form(body: bytes) -> dict[str, str]:
    """An urlencoded form's fields, the last value of each. Bytes that aren't
    UTF-8 become U+FFFD; past 300 fields it is refused (ValueError)."""
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=300)
    return {key: values[-1] for key, values in parsed.items()}


def read_body(headers: Headers, stream: BinaryIO, limit: int) -> bytes | int:
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


def compact_json(status: int, value: dict) -> Reply:
    """Compact ASCII JSON: the dashboard's way. The Pi's page sends its own."""
    return Reply(status, json.dumps(value, separators=(",", ":"), allow_nan=False).encode("ascii"),
                 "application/json")


class FormPages:
    """The sign-in, reset and settings pages and forms, under `prefix`, and
    the setup wizard's session and step checks.

    `home()` is where a new session lands. `resume_setup`, if set, is the
    wizard's page: a sign-in started there (`?next=setup`) goes back to it.
    `shell(title, body)` wraps a page in the HTML document. Log lines go to
    `log`, so each host's keep their own name. `setup` runs the wizard's
    steps, answered with `json_reply`."""

    def __init__(self, page: SettingsPage, password: PasswordStore, sessions: Sessions, lockout: Lockout,
                 codes: ResetCodes, forms: FormTokens, wording: Wording, *, prefix: str,
                 shell: Callable[[str, str], str], home: Callable[[], str], log: logging.Logger,
                 resume_setup: str = "", setup: SetupSteps | None = None,
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
        self._home = home
        self._log = log
        self._resume_setup = resume_setup
        self._setup = setup
        self._json = json_reply
        self._clock = clock

    # Pages

    def settings_page(self, headers: Headers) -> Reply:
        """The settings form for a signed-in visitor; sign-in for anyone else."""
        session = self._sessions.get(self._token(headers))
        if session is None:
            return redirect(self._prefix + "/login")
        return self._html(200, self._wording.title, self._page.render(session.csrf))

    def login_page(self, status: int, headers: Headers, message: str = "", to_setup: bool = False) -> Reply:
        """Sign in, or the first claim while no password is set."""
        if not self._password.is_set():
            return self.reset_page(status, headers, message)  # the first claim
        token, cookie = self._form_token(headers)
        error = (f'<p class="error" role="alert" id="login-error">{html.escape(message, quote=False)}</p>'
                 if message else "")
        described = ' aria-describedby="login-error" aria-invalid="true"' if message else ""
        then = '<input type="hidden" name="next" value="setup">' if to_setup else ""
        body = (
            f"<h1>{self._wording.title}</h1>"
            f"{error}"
            f'<form method="post" action="{self._prefix}/login" class="signin">{then}'
            '<label for="password">Password</label>'
            '<input id="password" name="password" type="password" autocomplete="current-password" '
            f"required autofocus{described}>"
            '<button type="submit">Sign in</button></form>'
            f'<form method="post" action="{self._prefix}/forgot" class="forgot">'
            f'<input type="hidden" name="form" value="{token}">'
            '<button type="submit" class="quiet">Forgot password?</button></form>'
            f"{self._wording.back}"
        )
        return self._html(status, "Sign in", body, cookie)

    def reset_page(self, status: int, headers: Headers, message: str = "", invalid: str = "",
                   asked: bool = False) -> Reply:
        """Ask for a code, then type it with a new password: for a forgotten
        password, and for the first claim when there is none. `invalid` names
        the field `message` is about."""
        wording = self._wording
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

        ask = wording.ask_again if asked else wording.ask
        parts.append(
            f'<form method="post" action="{self._prefix}/forgot" class="forgot">'
            f'<input type="hidden" name="form" value="{token}">'
            f'<button type="submit" class="quiet">{ask}</button></form>'
            f'<form method="post" action="{self._prefix}/reset" class="signin">'
            f'<input type="hidden" name="form" value="{token}">'
            f'<label for="code">{wording.code_label}</label>'
            '<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="7" '
            f'required{" autofocus" if asked or invalid == "code" else ""}{flagged("code")}>'
            '<label for="new-password">New password</label>'
            f'<p class="hint" id="new-password-hint">At least {MIN_PASSWORD} characters.</p>'
            '<input id="new-password" name="password" type="password" autocomplete="new-password" '
            f'minlength="{MIN_PASSWORD}" required{flagged("password", "new-password-hint")}>'
            '<label for="again">New password again</label>'
            '<input id="again" name="again" type="password" autocomplete="new-password" '
            f'required{flagged("again")}>'
            f'<button type="submit">{"Set password" if claim else "Set new password"} and sign in</button></form>'
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
        """A password typed on the sign-in page, rate limited by `client`'s address."""
        if not self._password.is_set():
            return self.login_page(403, headers)
        to_setup = bool(self._resume_setup) and fields.get("next") == "setup"
        result = self._checker.sign_in(client, fields.get("password", ""))
        if result is SignInResult.BUSY:
            return self.login_page(429, headers, "Busy, try again in a moment.", to_setup)
        if result is SignInResult.LOCKED:
            return self.login_page(429, headers, "Too many attempts. Try again in 15 minutes.", to_setup)
        if result is SignInResult.WRONG:
            return self.login_page(401, headers, "That password isn't right.", to_setup)
        return self._signed_in(to_setup)

    def ask_for_code(self, fields: dict[str, str], headers: Headers) -> Reply:
        """Issue a reset code, if this browser's form token is good. The reply
        is the same whether a code was issued, limited or failed."""
        if not self._form_token_ok(fields, headers):
            return self.reset_page(403, headers, self._wording.no_code_made)
        try:
            self._codes.issue(self._clock())
        except ResetLimited:
            pass  # the code already out stands; the page reads the same either way
        except OSError as error:
            self._log.warning("Can't issue a settings reset code: %s", type(error).__name__)
        return redirect(self._prefix + "/reset?asked")

    def reset(self, fields: dict[str, str], headers: Headers, client: str) -> Reply:
        """A new password with a reset code: every session ends, and this
        browser starts a new one."""
        if not self._form_token_ok(fields, headers):
            return self.reset_page(403, headers, "That form had run out, so nothing was changed. Try again.")
        new = fields.get("password", "")
        # Checked before the code, so a typo here doesn't use the code up.
        if len(new) < MIN_PASSWORD:
            return self.reset_page(422, headers, f"Use at least {MIN_PASSWORD} characters for the new password.",
                                   "password")
        if new != fields.get("again", ""):
            return self.reset_page(422, headers, "The two new passwords don't match.", "again")
        try:
            right = self._codes.verify(fields.get("code", ""), self._clock())
        except OSError as error:
            self._log.warning("Can't check a settings reset code: %s", type(error).__name__)
            return self.reset_page(503, headers, self._wording.cant_check)
        if not right:
            return self.reset_page(401, headers, WRONG_CODE, "code")
        self._password.set(new)
        self._log.info(self._wording.reset_logged)
        self._sessions.end_all()
        self._lockout.succeeded(client)
        return self._signed_in()

    def sign_out(self, fields: dict[str, str], headers: Headers) -> Reply:
        """End the session if the form carries its token; clear the cookie either way."""
        token = self._token(headers)
        if csrf_matches(self._sessions.get(token), fields.get("csrf", "")):
            self._sessions.end(token)
        return redirect(self._prefix + "/login", {"Set-Cookie": self._sessions.expired_cookie()})

    def save(self, fields: dict[str, str], headers: Headers) -> Reply:
        """The settings form, from a signed-in session with its token."""
        session = self._sessions.get(self._token(headers))
        if session is None:
            return redirect(self._prefix + "/login")
        if not csrf_matches(session, fields.get("csrf", "")):
            return Reply(403, b"Forbidden: form expired, reload the page")
        fields.pop("csrf", None)
        errors = self._page.save(fields)
        title = self._wording.title
        if errors:
            body = self._page.render(session.csrf, errors=errors, values=fields)
            return self._html(422, f"{title}: fix the errors", body)
        return self._html(200, f"Saved – {title}", self._page.render(session.csrf, message=self._wording.saved))

    # The setup wizard

    def wizard_cross_site(self, headers: Headers) -> Reply | None:
        """The refusal for any request to the wizard's API from another
        site's page (Sec-Fetch-Site: cross-site), session or not, before
        anything else is looked at; None for any other request."""
        if (headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            return self._json(403, {"error": "Not from another site"})
        return None

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
            return self._json(403, {"error": self._wording.wrong_step_token})
        if headers.get_content_type() != "application/json":
            return self._json(415, {"error": "Send JSON"})
        return None

    def wizard_step(self, name: str, body: bytes) -> Reply:
        """Run step `name` with the request's JSON, once wizard_refusal has let it through."""
        try:
            payload = setupapi.decode(body)
        except setupapi.Invalid as error:
            return self._json(400, {"error": str(error)})
        return self._json(*self._setup.handle(name, payload))

    # Helpers

    def _token(self, headers: Headers) -> str | None:
        return self._sessions.token_from(headers.get("Cookie"))

    def _html(self, status: int, title: str, body: str, extra: dict[str, str] | None = None) -> Reply:
        return Reply(status, self._shell(title, body).encode(), HTML, extra or {})

    def _form_token(self, headers: Headers) -> tuple[str, dict[str, str]]:
        """A pre-session form's hidden field, and the header that sets its cookie."""
        nonce = self._forms.nonce(headers.get("Cookie"))
        return html.escape(self._forms.form_value(nonce)), {"Set-Cookie": self._forms.cookie(nonce)}

    def _form_token_ok(self, fields: dict[str, str], headers: Headers) -> bool:
        return self._forms.check(headers.get("Cookie"), fields.get("form", ""))

    def _signed_in(self, to_setup: bool = False) -> Reply:
        token, _ = self._sessions.create()
        where = self._resume_setup if to_setup else self._home()
        return redirect(where, {"Set-Cookie": self._sessions.cookie(token)})
