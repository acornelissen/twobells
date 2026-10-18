import time

import pytest

from twobells.core.errors import MESSAGE_LIMIT, redact_keys, safe_message, scrub_secrets


# A server chooses what an error says: an HTTP reason phrase can be 64 KB.
@pytest.mark.parametrize("filler", ["a-", "http://", "a "])
def test_a_long_message_takes_no_longer_than_a_short_one(filler):
    text = "HTTP Error 500: " + filler * (64 * 1024 // len(filler))
    start = time.monotonic()
    message = safe_message(text)
    assert time.monotonic() - start < 1.0  # it took 30 s, reading every byte before the cut
    assert len(message) <= MESSAGE_LIMIT


def test_a_key_is_redacted_however_long_the_message_after_it():
    message = safe_message("Refused api_key=s3cr3tvalue " + "a-" * 32 * 1024)
    assert "s3cr3tvalue" not in message
    assert message.startswith("Refused api_key=REDACTED")


def test_a_key_past_the_part_read_is_never_shown():
    message = safe_message("http://example.com/x?" + "q" * 5000 + " key=s3cr3tvalue")
    assert "s3cr3tvalue" not in message


# The one key redactor, for anything that looks like a key by its name.
@pytest.mark.parametrize("text, redacted", [
    ("https://x/?boundingBox=1&api_key=abc123&y=2", "https://x/?boundingBox=1&api_key=REDACTED&y=2"),
    ("https://x.test/route.json?app_id=abc&app_key=s3cret&live=true",
     "https://x.test/route.json?app_id=REDACTED&app_key=REDACTED&live=true"),
    ("refused key=hunter2hunter2 at the gate", "refused key=REDACTED at the gate"),
    ("refused token=t0k3nvalue", "refused token=REDACTED"),
    ("headers {'x-apikey': 'darwinsecret99'}", "headers {'x-apikey': 'REDACTED'}"),
    ("x-apikey: darwinsecret99 rejected", "x-apikey: REDACTED rejected"),
    ("X-ApiKey=darwinsecret99", "X-ApiKey=REDACTED"),
    ('AUTH_TOKEN="abc"', 'AUTH_TOKEN="REDACTED"'),
    ("Password: hunter2", "Password: REDACTED"),
    ("client_secret = 'v4lue'", "client_secret = 'REDACTED'"),
    ("x-app_id=1234", "x-app_id=REDACTED"),
    ("foo.api_key=abc", "foo.api_key=REDACTED"),
    ("--key=a.b/c+d~e=f", "--key=REDACTED"),  # every character a saved key may hold
    ("keyword=x spoken=yes record: 5 app_idx=1", "keyword=x spoken=yes record: 5 app_idx=1"),
])
def test_redact_keys_replaces_a_key_by_its_name(text, redacted):
    assert redact_keys(text) == redacted


def test_redact_keys_reaches_every_string_and_copies():
    payload = {
        "departures": {
            "all": [{"id": "https://x.test/route.json?app_id=abc&app_key=s3cret&live=true", "line": "237"}]
        },
        "request_time": "2026-10-08T23:08:00+01:00",
        "token=t0k3nvalue": 3,
    }
    redacted = redact_keys(payload)
    url = redacted["departures"]["all"][0]["id"]
    assert "abc" not in url and "s3cret" not in url
    assert "app_id=REDACTED" in url and "app_key=REDACTED" in url and "live=true" in url
    assert redacted["departures"]["all"][0]["line"] == "237"
    assert redacted["request_time"] == "2026-10-08T23:08:00+01:00"
    assert redacted["token=REDACTED"] == 3
    assert payload["departures"]["all"][0]["id"].endswith("app_key=s3cret&live=true")


# It runs on whole server answers too, with nothing cut first.
@pytest.mark.parametrize("filler", ["a-", "http://", "a ", "key ", "key = ", "app_id-"])
def test_redact_keys_takes_no_longer_than_reading_once(filler):
    text = "HTTP Error 500: " + filler * (64 * 1024 // len(filler)) + " api_key=s3cr3tvalue"
    start = time.monotonic()
    redacted = redact_keys(text)
    assert time.monotonic() - start < 1.0  # the pattern safe_message used took 30 s on "a-"
    assert redacted.endswith(" api_key=REDACTED")


# The one value scrubber, for the keys known.
def test_scrub_secrets_replaces_every_secret_everywhere():
    value = {"error": "bad abcdef123", "abcdef123": ["xyz789xyz and abcdef123", 4], "n": None}
    assert scrub_secrets(value, ["abcdef123", "xyz789xyz"]) == {"error": "bad …", "…": ["… and …", 4], "n": None}
    assert value["error"] == "bad abcdef123"


def test_scrub_secrets_takes_the_longest_first_and_passes_over_empty_ones():
    assert scrub_secrets("key abcdef-long here", ["", "abcdef", "abcdef-long"]) == "key … here"
    assert scrub_secrets("nothing to hide", [""]) == "nothing to hide"
    assert scrub_secrets("nothing to hide", []) == "nothing to hide"
    assert scrub_secrets("bad 1234", {"A": "1234"}.values()) == "bad …"
