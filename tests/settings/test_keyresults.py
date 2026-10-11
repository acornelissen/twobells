"""The last test of each key, kept in the store as its state and time only."""

import json

import pytest

from twobells.settings import keyresults
from twobells.settings.keyresults import LastTest

NOW = 1_791_000_000  # October 2026


def test_a_result_is_kept_as_its_state_and_time_in_a_private_file(tmp_path):
    keyresults.record(tmp_path, "RDM_API_KEY", "works", NOW + 0.7)
    keyresults.record(tmp_path, "BODS_API_KEY", "refused", NOW)
    path = tmp_path / "keytests.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {"results": {"BODS_API_KEY": {"state": "refused", "time": NOW},
                                                         "RDM_API_KEY": {"state": "works", "time": NOW}}}
    assert keyresults.load(tmp_path) == {"RDM_API_KEY": LastTest("works", NOW),
                                         "BODS_API_KEY": LastTest("refused", NOW)}


def test_a_newer_result_replaces_the_last(tmp_path):
    keyresults.record(tmp_path, "RDM_API_KEY", "works", NOW)
    keyresults.record(tmp_path, "RDM_API_KEY", "refused", NOW + 60)
    assert keyresults.load(tmp_path) == {"RDM_API_KEY": LastTest("refused", NOW + 60)}


def test_transportapis_pair_has_one_result(tmp_path):
    keyresults.record(tmp_path, "TAPI_APP_ID", "works", NOW)
    assert keyresults.load(tmp_path) == {"TAPI_APP_KEY": LastTest("works", NOW)}
    keyresults.forget(tmp_path, ["TAPI_APP_ID"])
    assert keyresults.load(tmp_path) == {}


def test_forgetting_a_key_keeps_the_others(tmp_path):
    keyresults.record(tmp_path, "RDM_API_KEY", "works", NOW)
    keyresults.record(tmp_path, "BODS_API_KEY", "works", NOW)
    keyresults.forget(tmp_path, ["RDM_API_KEY", "RDM_KB_API_KEY"])
    assert keyresults.load(tmp_path) == {"BODS_API_KEY": LastTest("works", NOW)}


def test_forgetting_with_nothing_kept_writes_nothing(tmp_path):
    keyresults.forget(tmp_path, ["RDM_API_KEY"])
    assert not (tmp_path / "keytests.json").exists()


@pytest.mark.parametrize("name, state", [("PATH", "works"), ("RDM_API_KEY", "wrong_key"), ("RDM_API_KEY", "")])
def test_only_a_key_and_a_result_that_says_either_way_are_kept(tmp_path, name, state):
    with pytest.raises(ValueError):
        keyresults.record(tmp_path, name, state, NOW)
    assert not (tmp_path / "keytests.json").exists()


def test_each_test_state_says_what_is_kept():
    assert keyresults.FOUND == {"works": "works", "wrong_key": "refused", "not_subscribed": "refused"}


@pytest.mark.parametrize("content", [
    b"not json",
    b"[]",
    b'{"results": []}',
    b'{"results": {}, "key": "x"}',
    b'{"results": {"PATH": {"state": "works", "time": 1791000000}}}',
    b'{"results": {"RDM_API_KEY": {"state": "great", "time": 1791000000}}}',
    b'{"results": {"RDM_API_KEY": {"state": "works", "time": true}}}',
    b'{"results": {"RDM_API_KEY": {"state": "works", "time": 1791000000.5}}}',
    b'{"results": {"RDM_API_KEY": {"state": "works", "time": -1}}}',
    b'{"results": {"RDM_API_KEY": {"state": "works", "time": 99999999999999}}}',
    b'{"results": {"RDM_API_KEY": {"state": "works", "time": 1791000000, "key": "rail-key-1234567"}}}',
    b'{"results": {"RDM_API_KEY": "works"}}',
    b'{"results": {"RDM_API_KEY": {"state": "works", "time": 1791000000}}, "pad": "' + b"x" * 5000 + b'"}',
    b"[" * 4000,  # deeper than json reads, and small enough to try
])
def test_a_file_not_as_written_means_no_results(tmp_path, caplog, content):
    (tmp_path / "keytests.json").write_bytes(content)
    assert keyresults.load(tmp_path) == {}
    assert "isn't as written" in caplog.text and "rail-key" not in caplog.text


def test_a_results_file_that_is_a_link_is_not_followed(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    keyresults.record(elsewhere, "RDM_API_KEY", "works", NOW)
    (tmp_path / "keytests.json").symlink_to(elsewhere / "keytests.json")
    assert keyresults.load(tmp_path) == {}


def test_a_file_that_cant_be_written_leaves_no_old_results(tmp_path, monkeypatch, caplog):
    keyresults.record(tmp_path, "RDM_API_KEY", "works", NOW)

    def broken(*args):
        raise PermissionError("read-only")

    monkeypatch.setattr(keyresults.files, "write_atomic", broken)
    keyresults.forget(tmp_path, ["RDM_API_KEY"])
    assert not (tmp_path / "keytests.json").exists()
    assert "Can't save the keys' test results (PermissionError)" in caplog.text
