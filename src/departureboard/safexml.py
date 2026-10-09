"""XML parsing that refuses any document type declaration.

BODS never sends one, and without a DOCTYPE a document can declare no
entities, so nothing can expand or point outside it. The parser itself
refuses the declaration as it reaches it, whatever the encoding and however
much comes before it.
"""

from __future__ import annotations

from typing import Iterable, Iterator
from xml.etree import ElementTree


class DoctypeError(ValueError):
    pass


class _Builder(ElementTree.TreeBuilder):
    def doctype(self, name, pubid, system):
        # Called at the start of <!DOCTYPE, before any entity declarations in it.
        raise DoctypeError("XML has a DOCTYPE; refusing to parse it")


def fromstring(data: bytes) -> ElementTree.Element:
    parser = ElementTree.XMLParser(target=_Builder())
    parser.feed(data)
    return parser.close()


class _Collector(_Builder):
    """Keeps each finished element whose local name is `name`, for iter_elements."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.done: list[ElementTree.Element] = []

    def end(self, tag):
        element = super().end(tag)
        if tag.rpartition("}")[2] == self._name:
            self.done.append(element)
        return element


def iter_elements(chunks: Iterable[bytes], name: str) -> Iterator[ElementTree.Element]:
    """Each element named `name` (in any namespace) as soon as it is complete,
    from a document arriving in chunks. Each is emptied once the caller moves
    on, so a large document never sits in memory whole. Refuses a DOCTYPE like
    fromstring; a document that isn't well formed raises ElementTree.ParseError."""
    collector = _Collector(name)
    parser = ElementTree.XMLParser(target=collector)
    for chunk in chunks:
        parser.feed(chunk)
        yield from _drain(collector)
    parser.close()
    yield from _drain(collector)


def _drain(collector: _Collector) -> Iterator[ElementTree.Element]:
    while collector.done:
        element = collector.done.pop(0)
        yield element
        element.clear()
