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


def test_version_is_unknown_when_not_installed(monkeypatch):
    def not_installed(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(version, "_metadata_version", not_installed)

    assert version.package_version() == "unknown"
