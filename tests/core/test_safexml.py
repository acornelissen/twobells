from xml.etree import ElementTree

import pytest

from twobells.core.safexml import DoctypeError, TooManyElements, fromstring, iter_elements, iter_grandchildren

NS = "{urn:x}"
DOC = (b'<?xml version="1.0"?><Root xmlns="urn:x" Rev="3"><A><Item id="1"><Inner>one</Inner></Item>'
       b'<Item id="2"/></A><B><Item id="3"/><Other/></B><C><Item id="4"/></C>'
       b'<A><Deep><Item id="5"/></Deep></A></Root>')


def _read(data, parents, size=7, max_elements=1_000):
    chunks = (data[start : start + size] for start in range(0, len(data), size))
    return list(iter_grandchildren(chunks, parents, max_elements=max_elements))


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


# How many elements a document may build. An empty element is 4 bytes sent
# and about 100 held, so a byte cap alone lets a document fill a Pi's memory.


def _counted(chunks, read):
    for chunk in chunks:
        read.append(chunk)
        yield chunk


def _chunks(data, size=64):
    return (data[start : start + size] for start in range(0, len(data), size))


def test_a_document_of_more_elements_than_allowed_is_refused():
    doc = b"<Root><x>" + b"<a/>" * 97 + b"</x></Root>"  # with Root and x, 99 elements
    assert fromstring(doc, max_elements=99).tag == "Root"
    with pytest.raises(TooManyElements):
        fromstring(doc, max_elements=98)
    assert len(list(iter_elements([doc], "x", max_elements=99))) == 1
    with pytest.raises(TooManyElements):
        list(iter_elements([doc], "x", max_elements=98))


def test_elements_are_counted_as_they_are_read():
    doc = b"<Root><x>" + b"<a/>" * 10_000 + b"</x></Root>"
    read = []
    with pytest.raises(TooManyElements):
        list(iter_elements(_counted(_chunks(doc), read), "a", max_elements=100))
    assert len(read) < 20  # of over 600 chunks


def test_an_element_built_holding_more_elements_than_allowed_is_refused():
    doc = b'<Root xmlns="urn:x"><A><Item>' + b"<a/>" * 97 + b"</Item><Item/></A></Root>"
    found = _read(doc, {NS + "A"}, size=64, max_elements=98)  # the first Item, with its 97
    assert [len(node) for _, node in found[1:]] == [97, 0]
    with pytest.raises(TooManyElements):
        _read(doc, {NS + "A"}, size=64, max_elements=97)


def test_only_the_elements_built_are_counted():
    # The root's other children are read past, so they can hold any number.
    doc = b'<Root xmlns="urn:x"><B>' + b"<a/>" * 10_000 + b"</B><A><Item/></A></Root>"
    assert [node.tag for _, node in _read(doc, {NS + "A"}, size=64, max_elements=1)] == [NS + "Root", NS + "Item"]


def test_an_element_too_big_is_refused_as_it_is_read():
    doc = b'<Root xmlns="urn:x"><A><Item>' + b"<a/>" * 10_000 + b"</Item></A></Root>"
    read = []
    with pytest.raises(TooManyElements):
        list(iter_grandchildren(_counted(_chunks(doc), read), {NS + "A"}, max_elements=100))
    assert len(read) < 20
