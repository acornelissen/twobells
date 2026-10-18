import time

import pytest

from twobells.core.errors import MESSAGE_LIMIT, safe_message


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
