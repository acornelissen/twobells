import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from departureboard import version
from departureboard.cli import main

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def pyproject_version():
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def test_version_comes_from_pyproject():
    # The tests run against an installed (editable) package, as `mise run setup` makes.
    assert version.package_version() == pyproject_version()


def test_cli_prints_its_version_and_exits(capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["--version"])

    assert exit_.value.code == 0
    assert capsys.readouterr().out == f"departureboard {pyproject_version()}\n"


def not_installed(name):
    raise PackageNotFoundError(name)


def test_a_source_tree_never_installed_reads_its_pyproject(monkeypatch):
    # As the Docker image runs: src/ and pyproject.toml copied, nothing installed.
    monkeypatch.setattr(version, "_metadata_version", not_installed)

    assert version.SOURCE_PYPROJECT == PYPROJECT
    assert version.package_version() == pyproject_version()


def test_version_is_unknown_when_not_installed_and_no_pyproject(monkeypatch, tmp_path):
    monkeypatch.setattr(version, "_metadata_version", not_installed)
    monkeypatch.setattr(version, "SOURCE_PYPROJECT", tmp_path / "pyproject.toml")

    assert version.package_version() == "unknown"


@pytest.mark.parametrize("text", [
    '[project]\nname = "something-else"\nversion = "2.0.0"\n',  # another project's
    '[project]\nname = "departureboard"\n',  # no version
    '[project]\nname = "departureboard"\nversion = 3\n',  # not a string
    '[project]\nname = "departureboard"\nversion = "<b>1</b>"\n',  # not a version
    '[project]\nname = "departureboard"\nversion = ""\n',
    '[project\nname = ',  # not TOML
    'project = "departureboard"\n',
])
def test_a_pyproject_that_isnt_ours_or_is_broken_gives_unknown(tmp_path, text):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(text, encoding="utf-8")

    assert version.source_version(pyproject) == "unknown"


def test_a_pyproject_that_isnt_utf8_gives_unknown(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_bytes(b'[project]\nname = "departureboard"\nversion = "\xff"\n')

    assert version.source_version(pyproject) == "unknown"


def test_a_pyproject_version_is_read_as_written(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "departureboard"\nversion = "1.2.3rc1+local.7"\n', encoding="utf-8")

    assert version.source_version(pyproject) == "1.2.3rc1+local.7"
