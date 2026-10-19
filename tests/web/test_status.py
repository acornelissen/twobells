from datetime import datetime

import pytest

from twobells.core.errors import safe_message
from twobells.web.status import time_ago


@pytest.mark.parametrize("text, secret", [
    ("GET https://transportapi.com/v3/uk/bus/stop/x.json?app_id=myid&app_key=s3cr3t failed", "s3cr3t"),
    ("GET https://transportapi.com/v3/uk/bus/stop/x.json?app_id=myid1234 failed", "myid1234"),
    ("<urlopen error data.bus-data.dft.gov.uk/api/v1/datafeed/?api_key=abc123def>", "abc123def"),
    ("refused key=hunter2hunter2 at the gate", "hunter2hunter2"),
    ("refused token=t0k3nvalue", "t0k3nvalue"),
    ("headers {'x-apikey': 'darwinsecret99'}", "darwinsecret99"),
    ("x-apikey: darwinsecret99 rejected", "darwinsecret99"),
    ("X-ApiKey=darwinsecret99", "darwinsecret99"),
])
def test_credentials_are_redacted(text, secret):
    message = safe_message(RuntimeError(text))
    assert secret not in message
    assert "REDACTED" in message or "?…" in message


def test_urls_lose_their_query_strings():
    message = safe_message(RuntimeError("Failed: https://example.com/a/b?boundingBox=1,2,3,4&api_key=zzz9"))
    assert message == "Failed: https://example.com/a/b?…"


def test_messages_are_one_short_line():
    message = safe_message(OSError("first line\nsecond\tline\x00 " + "x" * 400))
    assert "\n" not in message and "\t" not in message and "\x00" not in message
    assert len(message) <= 160
    assert message.startswith("first line second line")
    assert message.endswith("…")


def test_a_redacted_secret_cannot_survive_by_being_cut_short():
    # The cut comes after the redaction, so no part of the key is left behind.
    message = safe_message(RuntimeError("x" * 150 + " key=abcdefghijklmnop"))
    assert "abcdef" not in message


def test_an_error_without_words_is_named_by_its_type():
    assert safe_message(TimeoutError()) == "TimeoutError"


def test_ordinary_messages_pass_through():
    assert safe_message(OSError("HTTP Error 503: Service Unavailable")) == "HTTP Error 503: Service Unavailable"


def test_times_say_how_long_ago_naming_the_day_and_year_when_not_today():
    now = datetime(2026, 10, 8, 8, 1)
    assert time_ago(datetime(2026, 10, 8, 7, 58), now) == "07:58, 3 min ago"
    assert time_ago(datetime(2026, 10, 8, 8, 0, 30), now) == "08:00, just now"
    assert time_ago(datetime(2026, 10, 8, 7, 58), datetime(2026, 10, 10, 9, 0)) == "Thu 8 Oct 07:58, 2 days ago"
    assert time_ago(datetime(2026, 10, 8, 7, 58), datetime(2027, 1, 2, 9, 0)) == "Thu 8 Oct 2026 07:58, 86 days ago"
    assert time_ago(datetime(2026, 10, 8, 9, 0), now) == "09:00"  # the clock went back since
