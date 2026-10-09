"""XML parsing that refuses any document type declaration.

BODS never sends one, and without a DOCTYPE a document can declare no
entities, so nothing can expand or point outside it. The parser itself
refuses the declaration as it reaches it, whatever the encoding and however
much comes before it.
"""

from __future__ import annotations

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
