from xml.etree import ElementTree

import pytest

from departureboard.core.safexml import DoctypeError, iter_grandchildren

NS = "{urn:x}"
DOC = (b'<?xml version="1.0"?><Root xmlns="urn:x" Rev="3"><A><Item id="1"><Inner>one</Inner></Item>'
       b'<Item id="2"/></A><B><Item id="3"/><Other/></B><C><Item id="4"/></C>'
       b'<A><Deep><Item id="5"/></Deep></A></Root>')


def _read(data, parents, size=7):
    chunks = (data[start : start + size] for start in range(0, len(data), size))
    return list(iter_grandchildren(chunks, parents))


def test_the_root_comes_first_with_its_attributes_and_no_children():
    section, root = _read(DOC, {NS + "A"})[0]
    assert (section, root.tag, root.attrib, len(root)) == ("", NS + "Root", {"Rev": "3"}, 0)


def test_only_the_children_of_the_sections_asked_for_in_document_order():
    found = _read(DOC, {NS + "A", NS + "B"})[1:]
    assert [(section, node.tag, node.get("id")) for section, node in found] == [
        (NS + "A", NS + "Item", "1"), (NS + "A", NS + "Item", "2"),
        (NS + "B", NS + "Item", "3"), (NS + "B", NS + "Other", None),
        (NS + "A", NS + "Deep", None),  # a child of A again; the Item inside it is not one
    ]


def test_each_element_is_built_whole():
    item = _read(DOC, {NS + "A"})[1][1]
    assert item.findtext(NS + "Inner") == "one"
    assert ElementTree.tostring(item) == ElementTree.tostring(ElementTree.fromstring(DOC)[0][0])


def test_whatever_size_the_chunks():
    whole = _read(DOC, {NS + "A", NS + "C"}, size=len(DOC))
    for size in (1, 3, 64):
        found = _read(DOC, {NS + "A", NS + "C"}, size=size)
        assert [(s, ElementTree.tostring(n)) for s, n in found] == [(s, ElementTree.tostring(n)) for s, n in whole]


def test_a_tag_is_matched_with_its_namespace():
    for parents in ({"A"}, {"{urn:y}A"}):
        assert [section for section, _ in _read(DOC, parents)] == [""]  # the root alone


@pytest.mark.parametrize("evil", [
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x><A><b>&a;</b></A></x>',
    b"<!--" + b" " * 5000 + b'--><!DOCTYPE x [<!ENTITY a "aaaa">]><x/>',
    '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x/>'.encode("utf-16"),
])
def test_a_doctype_is_refused(evil):
    with pytest.raises(DoctypeError):
        _read(evil, {"A"})


def test_a_document_that_is_not_well_formed_is_an_error():
    with pytest.raises(ElementTree.ParseError):
        _read(b'<Root xmlns="urn:x"><A><Item></A></Root>', {NS + "A"})
