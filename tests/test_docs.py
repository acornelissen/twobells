"""The docs' links: every relative link, picture and #anchor resolves, as GitHub renders them."""

import re
import tomllib
from collections import Counter
from functools import cache
from pathlib import Path

import pytest

from tests.paths import ROOT

DOCS = ROOT / "docs"
PAGES = sorted([*ROOT.glob("*.md"), *DOCS.glob("*.md")])
INDEX = DOCS / "user-guide.md"
_FENCE = re.compile(r"^(```|~~~).*?^\1", re.M | re.S)
_CODE = re.compile(r"`[^`\n]*`")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_HTML = re.compile(r"\b(?:src|srcset)=\"([^\"]+)\"")
_HEADING = re.compile(r"^#{1,6} +(.+?) *#*$", re.M)


def _prose(text: str) -> str:
    """The text with code blocks and code spans taken out, so a link in an example isn't followed."""
    return _CODE.sub("", _FENCE.sub("", text))


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: its words, lower case, punctuation dropped, spaces as hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link counts by its text
    text = text.replace("`", "").replace("*", "").lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


@cache
def anchors(page: Path) -> frozenset[str]:
    seen: Counter[str] = Counter()
    found = set()
    for heading in _HEADING.findall(_FENCE.sub("", page.read_text())):
        slug = _slug(heading)
        found.add(f"{slug}-{seen[slug]}" if seen[slug] else slug)  # a repeated heading gets -1, -2, ...
        seen[slug] += 1
    return frozenset(found)


def links(page: Path) -> list[str]:
    text = _prose(page.read_text())
    found = _LINK.findall(text)
    for value in _HTML.findall(text):
        found += [part.split()[0] for part in value.split(",")]  # srcset may list several
    return [link for link in found if not re.match(r"[a-z]+:", link)]  # https:, mailto: and the like


def test_the_slug_matches_githubs():
    assert _slug('The 11.3" bar screen') == "the-113-bar-screen"
    assert _slug("`GET /api/bus/<id>`") == "get-apibusid"
    assert _slug("Snapshot: `timetable_changes`") == "snapshot-timetable_changes"
    assert _slug("Wall tablet (kiosk)") == "wall-tablet-kiosk"
    assert _slug("If you can't sign in") == "if-you-cant-sign-in"


@pytest.mark.parametrize("page", PAGES, ids=lambda page: str(page.relative_to(ROOT)))
def test_every_link_resolves(page):
    broken = []
    for link in links(page):
        path, _, anchor = link.partition("#")
        target = (page.parent / path).resolve() if path else page
        missing = not target.exists()
        if missing or (anchor and (target.suffix != ".md" or anchor not in anchors(target))):
            broken.append(link)
    assert not broken, broken


def test_every_page_is_in_the_index():
    linked = {(DOCS / link.partition("#")[0]).resolve() for link in links(INDEX)}
    missing = [page.name for page in DOCS.glob("*.md") if page != INDEX and page.resolve() not in linked]
    assert not missing, missing


def test_the_wizards_key_guides_point_at_headings_that_exist():
    # The wizard links each key to its section of keys.md on GitHub, from installs already out there too.
    from twobells.setup.api import _KEY_ANCHORS, KEY_DOCS

    assert KEY_DOCS.endswith("/blob/main/docs/keys.md")
    for name, anchor in _KEY_ANCHORS.items():
        assert anchor.removeprefix("#") in anchors(DOCS / "keys.md"), name


def test_the_package_points_at_the_guide():
    urls = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["urls"]
    assert urls["Documentation"].endswith("/blob/main/docs/user-guide.md")


def test_every_doc_named_in_the_code_and_deploy_files_exists():
    named = {}
    for folder in ("src", "deploy", "examples", "tools", "tests"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix not in {".png", ".pyc", ".woff2", ".csv", ".xml", ".json"}:
                for doc in re.findall(r"\bdocs/[\w-]+\.md\b", path.read_text(errors="ignore")):
                    named.setdefault(doc, path.relative_to(ROOT))
    for name in ("Dockerfile", "docker-compose.yml", "docker-compose.override.yml.example"):
        for doc in re.findall(r"\bdocs/[\w-]+\.md\b", (ROOT / name).read_text()):
            named.setdefault(doc, name)
    missing = {doc: str(where) for doc, where in named.items() if not (ROOT / doc).exists()}
    assert not missing, missing
