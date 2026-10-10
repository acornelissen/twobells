import pytest

from departureboard.board.render import render
from departureboard.board.sign import render_sign
from departureboard.board.themes import THEMES, theme_named
from departureboard.board.view import PLAIN, SIGN


def test_plain_theme():
    theme = theme_named("plain")
    assert (theme.wording, theme.render, theme.accent) == (PLAIN, render, "red")


def test_sign_theme_uses_yellow_as_amber():
    theme = theme_named("sign")
    assert (theme.wording, theme.render, theme.accent) == (SIGN, render_sign, "yellow")


def test_unknown_theme_lists_the_choices():
    with pytest.raises(ValueError, match=r"plain.*sign|sign.*plain"):
        theme_named("neon")


def test_registry_names_match():
    assert all(theme.name == name for name, theme in THEMES.items())
