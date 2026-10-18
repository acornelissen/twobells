"""XML parsing that refuses any document type declaration, and more
elements than its caller allows.

BODS never sends one, and without a DOCTYPE a document can declare no
entities, so nothing can expand or point outside it. The parser itself
refuses the declaration as it reaches it, whatever the encoding and however
much comes before it.

An empty element is 4 bytes to send and about 100 to hold, so a cap on a
document's bytes doesn't cap the memory it takes: each caller also gives the
most elements it may build, counted as they are read.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator
from xml.etree import ElementTree


class DoctypeError(ValueError):
    pass


class TooManyElements(ValueError):
    pass


def _too_many(max_elements: int) -> TooManyElements:
    return TooManyElements(f"XML has too many elements, more than {max_elements:,}; refusing to parse it")


class _Builder(ElementTree.TreeBuilder):
    """Builds the whole document, up to `max_elements` elements."""

    def __init__(self, max_elements: int) -> None:
        super().__init__()
        self._max_elements = max_elements
        self._elements = 0

    def doctype(self, name, pubid, system):
        # Called at the start of <!DOCTYPE, before any entity declarations in it.
        raise DoctypeError("XML has a DOCTYPE; refusing to parse it")

    def start(self, tag, attrs):
        self._elements += 1
        if self._elements > self._max_elements:
            raise _too_many(self._max_elements)
        return super().start(tag, attrs)


def fromstring(data: bytes, *, max_elements: int) -> ElementTree.Element:
    """The document's root. TooManyElements if it has more than `max_elements`."""
    parser = ElementTree.XMLParser(target=_Builder(max_elements))  # noqa: S314 - the target refuses a DOCTYPE
    parser.feed(data)
    return parser.close()


class _Collector(_Builder):
    """Keeps each finished element whose local name is `name`, for iter_elements."""

    def __init__(self, name: str, max_elements: int) -> None:
        super().__init__(max_elements)
        self._name = name
        self.done: list[ElementTree.Element] = []

    def end(self, tag):
        element = super().end(tag)
        if tag.rpartition("}")[2] == self._name:
            self.done.append(element)
        return element


def iter_elements(chunks: Iterable[bytes], name: str, *, max_elements: int) -> Iterator[ElementTree.Element]:
    """Each element named `name` (in any namespace) as soon as it is complete,
    from a document arriving in chunks. Each is emptied once the caller moves
    on, so a large document never sits in memory whole. Refuses a DOCTYPE like
    fromstring, and a document of more than `max_elements` elements (each is
    built, though those emptied are let go); a document that isn't well formed
    raises ElementTree.ParseError."""
    collector = _Collector(name, max_elements)
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
    tags are in `parents`, for iter_grandchildren, each of up to
    `max_elements` elements; the rest of the document is read past and never
    built."""

    def __init__(self, parents: Collection[str], max_elements: int) -> None:
        self._parents = frozenset(parents)
        self._max_elements = max_elements
        self._elements = 0  # in the element being built
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
            self._elements = 0
        if self._builder is not None:
            self._elements += 1
            if self._elements > self._max_elements:
                raise _too_many(self._max_elements)
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


def iter_grandchildren(
    chunks: Iterable[bytes], parents: Collection[str], *, max_elements: int,
) -> Iterator[tuple[str, ElementTree.Element]]:
    """(parent's tag, element) for each element inside one of the root's
    children whose tag (namespace in braces) is in `parents`, as soon as it
    is complete, from a document arriving in chunks: what
    root.iterfind("Parent/*") gives, in document order. Only those elements
    are built, so memory follows the largest of them, not the document, and
    one holding more than `max_elements` elements, itself among them, is
    refused (TooManyElements). The root comes first, as ("", root) with its
    attributes and no children. Refuses a DOCTYPE like fromstring; a document
    that isn't well formed raises ElementTree.ParseError."""
    target = _Grandchildren(parents, max_elements)
    parser = ElementTree.XMLParser(target=target)  # noqa: S314 - the target refuses a DOCTYPE
    for chunk in chunks:
        parser.feed(chunk)
        yield from _take(target)
    parser.close()
    yield from _take(target)


def _take(target: _Grandchildren) -> Iterator[tuple[str, ElementTree.Element]]:
    while target.done:
        yield target.done.pop(0)
