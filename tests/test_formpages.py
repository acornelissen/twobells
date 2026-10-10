"""The pieces both settings pages share, tested on their own."""

import pytest

from departureboard.formpages import WIZARD_TYPES, document, parse_form


def test_a_form_keeps_the_last_value_of_each_field_and_blank_ones():
    assert parse_form(b"a=1&b=&a=2&c=x+y%21") == {"a": "2", "b": "", "c": "x y!"}


def test_bytes_that_are_not_utf_8_cannot_break_a_form():
    assert parse_form(b"name=%ff%fe&other=\xff") == {"name": "��", "other": "�"}


def test_a_form_with_too_many_fields_is_refused():
    assert len(parse_form(b"&".join(b"f%d=1" % n for n in range(300)))) == 300
    with pytest.raises(ValueError):
        parse_form(b"&".join(b"f%d=1" % n for n in range(301)))


def test_the_document_escapes_its_title_and_names_its_stylesheet():
    page = document("<Board> & co", "<p>body</p>", "/settings/style.css")
    assert "<title>&lt;Board&gt; &amp; co</title>" in page
    assert '<link rel="stylesheet" href="/settings/style.css">' in page
    assert page.startswith("<!doctype html>") and "<main><p>body</p></main>" in page
    assert '<html lang="en-GB">' in page


def test_the_wizard_serves_only_pages_scripts_and_styles():
    assert set(WIZARD_TYPES) == {".html", ".js", ".css"}
    assert all(value.endswith("; charset=utf-8") for value in WIZARD_TYPES.values())
