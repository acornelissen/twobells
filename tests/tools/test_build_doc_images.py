"""tools/build_doc_images.py redraws docs/images/: it takes no arguments, and
draws nothing when given one, --help among them."""

import importlib.util

import pytest

from tests.paths import ROOT

_SCRIPT = ROOT / "tools" / "build_doc_images.py"


@pytest.fixture
def build(monkeypatch):
    spec = importlib.util.spec_from_file_location("build_doc_images", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runs = []
    monkeypatch.setattr(module, "run", lambda: runs.append(True))
    module.runs = runs
    return module


@pytest.mark.parametrize("args, code", [(["--help"], 0), (["--out", "elsewhere"], 2), (["pictures"], 2)])
def test_it_draws_nothing_when_asked_for_help_or_given_an_argument(build, args, code):
    with pytest.raises(SystemExit) as stopped:
        build.main(args)
    assert stopped.value.code == code
    assert build.runs == []


def test_with_no_arguments_it_draws_the_pictures(build):
    build.main([])
    assert build.runs == [True]
