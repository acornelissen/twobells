import hashlib
import json
import threading
from zoneinfo import ZoneInfo

import pytest

from twobells.settings.resetcodes import (
    RESET_CODE_SECONDS,
    RESET_ISSUE_GAP_S,
    RESET_ISSUES_PER_DAY,
    RESET_MAX_TRIES,
    RESET_MAX_TRIES_ALL,
    PendingCode,
    ResetCodes,
    ResetLimited,
    ResetShutOut,
    client_key,
    show_in_log,
)

# --- Who a client counts as ---


def test_a_client_is_its_ipv4_address_or_its_ipv6_network():
    assert client_key("192.168.1.20") == "192.168.1.20"
    assert client_key("::ffff:192.168.1.20") == "192.168.1.20"  # IPv4 by way of an IPv6 socket
    # One host can take any address in its /64 at will, so the /64 is the client.
    assert client_key("fd12:3456:789a:1:aaaa:bbbb:cccc:dddd") == "fd12:3456:789a:1::/64"
    assert client_key("fd12:3456:789a:1::2") == "fd12:3456:789a:1::/64"
    assert client_key("fd12:3456:789a:2::2") == "fd12:3456:789a:2::/64"
    assert client_key("fe80::1%eth0") == "fe80::/64"
    assert client_key("::1") == "::/64"
    assert client_key("") == ""  # no address, as from the command line


# --- Reset codes ---

T0 = 1_800_000_000.0


def displayed(folder):
    return json.loads((folder / "reset-display.json").read_text())


def wrong(code):
    return f"{(int(code) + 1) % 10**6:06d}"


def test_a_code_is_six_digits_and_checks_once(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    assert len(code) == 6 and code.isdigit()
    assert codes.verify(code, T0 + 1)
    assert not codes.verify(code, T0 + 2)  # single use


def test_a_code_typed_as_shown_with_a_space_checks(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    assert codes.verify(f" {code[:3]} {code[3:]} ", T0 + 1)


def test_a_code_runs_out_after_ten_minutes(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    assert RESET_CODE_SECONDS == 600
    assert not codes.verify(code, T0 + RESET_CODE_SECONDS)
    later = T0 + RESET_CODE_SECONDS
    code = codes.issue(later)
    assert codes.verify(code, later + RESET_CODE_SECONDS - 1)


OWNER, GUESSER = "192.168.1.20", "192.168.1.66"


def test_wrong_tries_shut_out_that_address_but_leave_the_code_for_the_owner(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    assert RESET_MAX_TRIES == 5
    for _ in range(RESET_MAX_TRIES - 1):
        assert not codes.verify(wrong(code), T0 + 1, OWNER)
    assert codes.verify(code, T0 + 1, OWNER)  # four wrong, then right: still fine
    later = T0 + RESET_ISSUE_GAP_S
    code = codes.issue(later)
    for _ in range(RESET_MAX_TRIES):
        assert not codes.verify(wrong(code), later + 1, GUESSER)
    assert not codes.verify(code, later + 2, GUESSER)  # that address is out, even with the right code
    assert displayed(tmp_path)["code"] == code  # still on the board
    assert codes.verify(code, later + 3, OWNER)  # someone else can't void the owner's code


def test_wrong_tries_from_many_addresses_void_the_code(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    assert RESET_MAX_TRIES < RESET_MAX_TRIES_ALL == 10
    for i in range(RESET_MAX_TRIES_ALL):  # one guess from each: a guesser changing its address
        assert not codes.verify(wrong(code), T0 + 1, f"192.168.1.{100 + i}")
    assert not codes.verify(code, T0 + 2, OWNER)
    assert not (tmp_path / "reset-display.json").exists()



OWNER6, GUESSER6 = "fd12:3456:789a:1::20", "fd12:3456:789a:2::66"


def test_a_guesser_changing_its_ipv6_address_is_still_one_client(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    for i in range(RESET_MAX_TRIES):  # a new address in its /64 for every guess
        assert not codes.verify(wrong(code), T0 + 1, f"fd12:3456:789a:2::{i + 100:x}")
    assert not codes.verify(code, T0 + 2, GUESSER6)  # that network is out, even with the right code
    with pytest.raises(ResetShutOut):
        codes.issue(T0 + 3, client="fd12:3456:789a:2:ffff::1")
    assert codes.verify(code, T0 + 4, OWNER6)  # not void: the owner's network still has its tries


def test_an_ipv4_client_counts_the_same_mapped_or_not_at_the_code(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    for _ in range(RESET_MAX_TRIES):
        assert not codes.verify(wrong(code), T0 + 1, "::ffff:" + GUESSER)
    assert not codes.verify(code, T0 + 2, GUESSER)
    assert codes.verify(code, T0 + 3, OWNER)


# T0 + RESET_CODE_SECONDS is 08:10 UTC, 08:10 in London (winter) and 13:40 in Kolkata.
KOLKATA = ZoneInfo("Asia/Kolkata")


def test_asking_again_from_a_shut_out_address_says_so_and_when_the_code_runs_out(tmp_path):
    codes = ResetCodes(tmp_path, zone=lambda: KOLKATA)
    code = codes.issue(T0)
    for _ in range(RESET_MAX_TRIES):
        assert not codes.verify(wrong(code), T0 + 1, GUESSER)
    with pytest.raises(ResetShutOut) as shut:
        codes.issue(T0 + 2, client=GUESSER)
    assert shut.value.until == "13:40"  # in the board's time zone, not the process's
    assert displayed(tmp_path)["code"] == code  # no one can swap or void the owner's code
    assert codes.issue(T0 + 3, client=OWNER) is None  # another address: the code still stands
    assert codes.verify(code, T0 + 4, OWNER)


def test_an_address_with_tries_left_is_not_shut_out_by_asking_again(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    for _ in range(RESET_MAX_TRIES - 1):
        assert not codes.verify(wrong(code), T0 + 1, GUESSER)
    assert codes.issue(T0 + 2, client=GUESSER) is None
    assert codes.verify(code, T0 + 3, GUESSER)


def test_a_shut_out_address_can_ask_for_a_new_code_once_the_last_runs_out(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    for _ in range(RESET_MAX_TRIES):
        codes.verify(wrong(code), T0 + 1, GUESSER)
    code = codes.issue(T0 + RESET_CODE_SECONDS, client=GUESSER)
    assert code is not None and codes.verify(code, T0 + RESET_CODE_SECONDS + 1, GUESSER)


def test_the_command_line_is_never_shut_out(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    for _ in range(RESET_MAX_TRIES):
        codes.verify(wrong(code), T0 + 1)
    assert codes.verify(codes.issue(T0 + 2, limit=False), T0 + 3)


def test_a_code_in_the_log_runs_out_at_a_time_in_the_boards_zone(tmp_path, caplog):
    caplog.set_level("DEBUG")
    codes = ResetCodes(tmp_path, to_board=lambda: False, zone=lambda: KOLKATA)
    codes.issue(T0)
    assert any("13:40" in r.getMessage() for r in caplog.records)
    caplog.clear()
    assert codes.issue(T0 + 1) is None
    assert any("13:40" in r.getMessage() for r in caplog.records)


def test_show_in_log_says_the_time_in_the_zone_given(caplog):
    caplog.set_level("DEBUG")
    show_in_log("123456", T0 + RESET_CODE_SECONDS, KOLKATA)
    [message] = [r.getMessage() for r in caplog.records]
    assert "123 456" in message and "13:40" in message


def test_the_tries_counted_per_address_are_kept_on_disk(tmp_path):
    code = ResetCodes(tmp_path).issue(T0)
    for _ in range(RESET_MAX_TRIES):
        assert not ResetCodes(tmp_path).verify(wrong(code), T0 + 1, GUESSER)
    assert not ResetCodes(tmp_path).verify(code, T0 + 2, GUESSER)
    assert ResetCodes(tmp_path).verify(code, T0 + 2, OWNER)


@pytest.mark.parametrize("typed", ["", "12345", "abcdef", "1234567", "١٢٣٤٥٦", "x" * 10_000])
def test_odd_input_never_matches_and_counts_as_a_try(tmp_path, typed):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    for _ in range(RESET_MAX_TRIES):
        assert not codes.verify(typed, T0 + 1)
    assert not codes.verify(code, T0 + 1)


def test_without_a_code_nothing_matches(tmp_path):
    codes = ResetCodes(tmp_path)
    assert not codes.verify("000000", T0)
    (tmp_path / "reset.json").write_text("{nope")
    assert not codes.verify("000000", T0)
    (tmp_path / "reset.json").write_text('{"code": {"salt": "zz"}}')
    assert not codes.verify("000000", T0)


def test_a_new_code_replaces_the_old_once_it_has_run_out(tmp_path, monkeypatch):
    import twobells.settings.resetcodes as resetcodes

    draws = iter([111111, 222222])
    monkeypatch.setattr(resetcodes.secrets, "randbelow", lambda n: next(draws))
    codes = ResetCodes(tmp_path)
    assert codes.issue(T0) == "111111"
    later = T0 + RESET_CODE_SECONDS
    assert codes.issue(later) == "222222"
    assert displayed(tmp_path)["code"] == "222222"
    assert not codes.verify("111111", later + 1)
    assert codes.verify("222222", later + 1)


def test_codes_come_from_the_secrets_module(tmp_path, monkeypatch):
    import twobells.settings.resetcodes as resetcodes

    asked = []
    monkeypatch.setattr(resetcodes.secrets, "randbelow", lambda n: asked.append(n) or 42)
    assert ResetCodes(tmp_path).issue(T0) == "000042"
    assert asked == [10**6]


def test_issuing_waits_a_minute_between_codes(tmp_path):
    codes = ResetCodes(tmp_path)
    assert codes.verify(codes.issue(T0), T0 + 1)  # used at once
    with pytest.raises(ResetLimited):
        codes.issue(T0 + RESET_ISSUE_GAP_S - 1)
    codes.issue(T0 + RESET_ISSUE_GAP_S)


def test_no_more_than_ten_codes_a_day(tmp_path):
    codes = ResetCodes(tmp_path)
    for i in range(RESET_ISSUES_PER_DAY):
        codes.issue(T0 + i * RESET_CODE_SECONDS)
    later = T0 + RESET_ISSUES_PER_DAY * RESET_CODE_SECONDS
    with pytest.raises(ResetLimited):
        codes.issue(later)
    with pytest.raises(ResetLimited):
        ResetCodes(tmp_path).issue(later + 3600)  # kept on disk, so every process sees it
    codes.issue(T0 + 24 * 3600)  # the first has left the day


def test_the_command_line_can_skip_the_limits(tmp_path):
    codes = ResetCodes(tmp_path)
    codes.issue(T0)
    assert codes.verify(codes.issue(T0 + 1, limit=False), T0 + 2)


def test_asking_again_while_a_code_is_live_shows_the_same_code(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    for later in (T0 + 1, T0 + RESET_ISSUE_GAP_S, T0 + RESET_CODE_SECONDS - 1):
        assert codes.issue(later) is None  # no new code, and not limited: the board still shows this one
        assert displayed(tmp_path)["code"] == code
    assert codes.verify(code, T0 + RESET_CODE_SECONDS - 1)


def test_asking_again_while_a_code_is_live_spends_none_of_the_allowance(tmp_path):
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    for i in range(1, 3 * RESET_ISSUES_PER_DAY):  # someone pressing the button all day long
        assert codes.issue(T0 + i) is None
    assert codes.verify(code, T0 + RESET_ISSUE_GAP_S, OWNER)
    for i in range(1, RESET_ISSUES_PER_DAY):  # the rest of the day's codes are still there
        codes.issue(T0 + i * RESET_CODE_SECONDS)


def test_a_live_code_that_went_to_the_log_is_pointed_to_again_never_logged_twice(tmp_path, caplog):
    caplog.set_level("DEBUG")
    codes = ResetCodes(tmp_path, to_board=lambda: False, zone=lambda: KOLKATA)
    code = codes.issue(T0)
    caplog.clear()
    assert codes.issue(T0 + 1) is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("13:40" in m for m in messages)  # pointed to: when the code in the log runs out
    assert not any(code[:3] in m and code[3:] in m for m in messages)
    assert codes.verify(code, T0 + 2)


def test_the_files_are_private_and_only_the_handover_holds_the_code(tmp_path):
    folder = tmp_path / "state"
    codes = ResetCodes(folder)
    code = codes.issue(T0)
    for name in ("reset.json", "reset-display.json", "reset.lock"):
        assert (folder / name).stat().st_mode & 0o777 == 0o600, name
    assert sorted(p.name for p in folder.iterdir()) == ["reset-display.json", "reset.json", "reset.lock"]
    assert code not in (folder / "reset.json").read_text()
    record = json.loads((folder / "reset.json").read_text())
    salt = bytes.fromhex(record["code"]["salt"])
    assert len(salt) == 16
    assert record["code"]["hash"] == hashlib.sha256(salt + code.encode()).hexdigest() != code
    assert displayed(folder) == {"code": code, "expires": T0 + RESET_CODE_SECONDS}


def test_each_code_gets_a_fresh_salt(tmp_path, monkeypatch):
    import twobells.settings.resetcodes as resetcodes

    monkeypatch.setattr(resetcodes.secrets, "randbelow", lambda n: 123456)
    codes = ResetCodes(tmp_path)
    codes.issue(T0)
    first = json.loads((tmp_path / "reset.json").read_text())["code"]
    codes.issue(T0 + RESET_CODE_SECONDS)
    second = json.loads((tmp_path / "reset.json").read_text())["code"]
    assert first["salt"] != second["salt"] and first["hash"] != second["hash"]


def test_the_board_sees_the_code_until_it_is_used(tmp_path):
    codes = ResetCodes(tmp_path)
    assert codes.pending(T0) is None
    code = codes.issue(T0)
    assert codes.pending(T0 + 60) == PendingCode(code, T0 + RESET_CODE_SECONDS)
    codes.verify(code, T0 + 61)
    assert codes.pending(T0 + 62) is None
    assert not (tmp_path / "reset-display.json").exists()


def test_the_board_removes_an_expired_code(tmp_path):
    codes = ResetCodes(tmp_path)
    codes.issue(T0)
    assert codes.pending(T0 + RESET_CODE_SECONDS) is None
    assert not (tmp_path / "reset-display.json").exists()


@pytest.mark.parametrize("content", ["{nope", "[]", '{"code": "12345", "expires": 9e12}',
                                     '{"code": "<b>12</b>", "expires": 9e12}', '{"code": "123456"}',
                                     '{"code": "123456\\n", "expires": 9e12}'])
def test_the_board_removes_a_handover_it_cannot_show(tmp_path, content):
    (tmp_path / "reset-display.json").write_text(content)
    assert ResetCodes(tmp_path).pending(T0) is None
    assert not (tmp_path / "reset-display.json").exists()


def test_a_code_shown_elsewhere_never_reaches_the_board(tmp_path):
    shown = []
    codes = ResetCodes(tmp_path)
    codes.issue(T0)
    elsewhere = ResetCodes(tmp_path, show=lambda code, expires: shown.append((code, expires)))
    later = T0 + RESET_CODE_SECONDS
    code = elsewhere.issue(later)
    assert shown == [(code, later + RESET_CODE_SECONDS)]
    assert codes.pending(later) is None  # and the old code has left the board
    assert codes.verify(code, later + 1)


def test_only_the_log_hook_ever_logs_a_code(tmp_path, caplog):
    caplog.set_level("DEBUG")
    codes = ResetCodes(tmp_path)
    code = codes.issue(T0)
    for _ in range(RESET_MAX_TRIES):
        codes.verify(wrong(code), T0 + 1)
    codes.pending(T0 + 2)
    messages = [r.getMessage() for r in caplog.records]
    assert messages and not any(code in m or f"{code[:3]} {code[3:]}" in m for m in messages)
    caplog.clear()
    code = ResetCodes(tmp_path, show=show_in_log).issue(T0 + RESET_CODE_SECONDS)
    assert any(f"{code[:3]} {code[3:]}" in r.getMessage() for r in caplog.records)
    assert not (tmp_path / "reset-display.json").exists()


def test_each_code_goes_to_the_board_or_the_log_as_the_board_view_is_now(tmp_path, caplog):
    caplog.set_level("DEBUG")
    board = {"on": True}
    codes = ResetCodes(tmp_path, to_board=lambda: board["on"])
    code = codes.issue(T0)
    assert displayed(tmp_path)["code"] == code
    assert not any(code[:3] in r.getMessage() and code[3:] in r.getMessage() for r in caplog.records)
    board["on"] = False  # the board view turned off: the next code goes to the log
    code = codes.issue(T0 + RESET_ISSUE_GAP_S)
    assert not (tmp_path / "reset-display.json").exists()  # and the old one is off the board
    assert any(f"{code[:3]} {code[3:]}" in r.getMessage() for r in caplog.records)
    assert codes.verify(code, T0 + RESET_ISSUE_GAP_S + 1)


def test_only_one_of_many_simultaneous_checks_uses_a_code(tmp_path):
    code = ResetCodes(tmp_path).issue(T0)
    results = []
    threads = [threading.Thread(target=lambda: results.append(ResetCodes(tmp_path).verify(code, T0 + 1)))
               for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1


def test_simultaneous_wrong_guesses_cannot_beat_the_try_limit(tmp_path):
    code = ResetCodes(tmp_path).issue(T0)
    guess = wrong(code)
    threads = [threading.Thread(target=lambda: ResetCodes(tmp_path).verify(guess, T0 + 1)) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not ResetCodes(tmp_path).verify(code, T0 + 2)


def test_simultaneous_wrong_guesses_from_many_addresses_cannot_beat_the_cap(tmp_path):
    code = ResetCodes(tmp_path).issue(T0)
    guess = wrong(code)
    threads = [threading.Thread(target=lambda i=i: ResetCodes(tmp_path).verify(guess, T0 + 1, f"10.0.0.{i}"))
               for i in range(3 * RESET_MAX_TRIES_ALL)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not ResetCodes(tmp_path).verify(code, T0 + 2, OWNER)
    record = json.loads((tmp_path / "reset.json").read_text())
    assert record["code"] is None  # voided, and its tally of addresses with it
