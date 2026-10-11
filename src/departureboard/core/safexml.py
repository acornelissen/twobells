"""XML parsing that refuses any document type declaration.

BODS never sends one, and without a DOCTYPE a document can declare no
entities, so nothing can expand or point outside it. The parser itself
refuses the declaration as it reaches it, whatever the encoding and however
much comes before it.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator
from xml.etree import ElementTree


class DoctypeError(ValueError):
    pass


class _Builder(ElementTree.TreeBuilder):
    def doctype(self, name, pubid, system):
        # Called at the start of <!DOCTYPE, before any entity declarations in it.
        raise DoctypeError("XML has a DOCTYPE; refusing to parse it")


def fromstring(data: bytes) -> ElementTree.Element:
    parser = ElementTree.XMLParser(target=_Builder())  # noqa: S314 - the target refuses a DOCTYPE
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
    parser = ElementTree.XMLParser(target=collector)  # noqa: S314 - the target refuses a DOCTYPE
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


class _Grandchildren:
    """Builds only the elements one level inside the root's children whose
    tags are in `parents`, for iter_grandchildren; the rest of the document
    is read past and never built."""

    def __init__(self, parents: Collection[str]) -> None:
        self._parents = frozenset(parents)
        self._depth = 0
        self._parent = ""  # the tag of the root's child we are in, while it is one of `parents`
        self._builder: ElementTree.TreeBuilder | None = None  # while inside an element being built
        self.done: list[tuple[str, ElementTree.Element]] = []

    def doctype(self, name, pubid, system):
        # Called at the start of <!DOCTYPE, before any entity declarations in it.
        raise DoctypeError("XML has a DOCTYPE; refusing to parse it")

    def start(self, tag, attrib):
        if self._depth == 0:
            self.done.append(("", ElementTree.Element(tag, attrib)))
        elif self._depth == 1:
            self._parent = tag if tag in self._parents else ""
        elif self._depth == 2 and self._parent:
            self._builder = ElementTree.TreeBuilder()
        if self._builder is not None:
            self._builder.start(tag, attrib)
        self._depth += 1

    def data(self, text):
        if self._builder is not None:
            self._builder.data(text)

    def end(self, tag):
        self._depth -= 1
        if self._builder is not None:
            self._builder.end(tag)
            if self._depth == 2:
                self.done.append((self._parent, self._builder.close()))
                self._builder = None

    def close(self):
        return None


def iter_grandchildren(chunks: Iterable[bytes], parents: Collection[str]) -> Iterator[tuple[str, ElementTree.Element]]:
    """(parent's tag, element) for each element inside one of the root's
    children whose tag (namespace in braces) is in `parents`, as soon as it
    is complete, from a document arriving in chunks: what
    root.iterfind("Parent/*") gives, in document order. Only those elements
    are built, so memory follows the largest of them, not the document. The
    root comes first, as ("", root) with its attributes and no children.
    Refuses a DOCTYPE like fromstring; a document that isn't well formed
    raises ElementTree.ParseError."""
    target = _Grandchildren(parents)
    parser = ElementTree.XMLParser(target=target)  # noqa: S314 - the target refuses a DOCTYPE
    for chunk in chunks:
        parser.feed(chunk)
        yield from _take(target)
    parser.close()
    yield from _take(target)


def _take(target: _Grandchildren) -> Iterator[tuple[str, ElementTree.Element]]:
    while target.done:
        yield target.done.pop(0)
