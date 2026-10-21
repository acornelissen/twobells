"""The docs' links: every relative link, picture and #anchor resolves, as GitHub renders them."""

import re
import shutil
import subprocess
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
# A row of docs/architecture.md's table: a folder (or a top-level file) and what it holds.
_ARCHITECTURE_ROW = re.compile(r"^\| `([\w/.]+)` \| (.+) \|$", re.M)


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


def test_the_performance_page_s_key_guide_points_at_a_heading_that_exists():
    from twobells.setup.api import KEY_DOCS

    script = (ROOT / "src" / "twobells" / "dashboard_static" / "performance.js").read_text()
    [anchor] = re.findall(re.escape(KEY_DOCS) + r"#([a-z0-9-]+)", script)
    assert anchor in anchors(DOCS / "keys.md")


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


def test_every_file_the_architecture_table_names_is_in_its_folder():
    # A name with a slash is from the package (or the repo's) top; the wizard's
    # row names the dashboard's files it uses by relative path.
    package = ROOT / "src" / "twobells"
    text = (DOCS / "architecture.md").read_text(encoding="utf-8")
    rows = _ARCHITECTURE_ROW.findall(text)
    assert len(rows) > 10
    missing = []
    for folder, role in rows:
        for name in re.findall(r"`([\w./-]+\.(?:py|js|html|css|txt))`", role):
            places = [package / name, ROOT / name] if "/" in name else [package / folder / name]
            if folder == "wizard_static/":
                places.append(package / "dashboard_static" / name)
            if not any(place.is_file() for place in places):
                missing.append(f"{folder} {name}")
    assert missing == []


def unnamed_modules(table: str, modules: list[str]) -> list[str]:
    """Those of `modules` (paths inside the package, like "core/net.py") that
    the architecture table doesn't name. A module is named in its folder's
    row, by file name or by module name (the cli row says `run`), or by its
    path in any row; a top-level one has a row of its own. A folder's
    __init__.py needs only the folder's row."""
    rows = dict(_ARCHITECTURE_ROW.findall(table))
    paths = {name for role in rows.values() for name in re.findall(r"`([\w/]+/\w+\.py)`", role)}
    missing = []
    for module in modules:
        folder, _, name = module.rpartition("/")
        if not folder:
            named = module == "__init__.py" or module in rows
        elif name == "__init__.py":
            named = f"{folder}/" in rows
        else:
            said = re.findall(r"`([^`]+)`", rows.get(f"{folder}/", ""))
            named = name in said or name.removesuffix(".py") in said or module in paths
        if not named:
            missing.append(module)
    return missing


def _tracked_modules() -> list[str]:
    """Every Python module in the package that git tracks, as a path inside it."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("needs git to list the package's files")
    listed = subprocess.run([git, "ls-files", "-z", "--", "src/twobells"], cwd=ROOT, capture_output=True, check=False)
    if listed.returncode != 0:
        pytest.skip("needs a git checkout")
    paths = listed.stdout.decode("utf-8").split("\0")
    return sorted(path.removeprefix("src/twobells/") for path in paths if path.endswith(".py"))


def test_the_architecture_table_names_every_module():
    modules = _tracked_modules()
    assert len(modules) > 50
    assert unnamed_modules((DOCS / "architecture.md").read_text(encoding="utf-8"), modules) == []


def test_a_module_left_out_of_the_architecture_table_is_found():
    table = """| Folder | Role |
|---|---|
| `cli/` | The commands, a module each (`run`, `serve`), helpers in `common.py` and the parser in `cli/main.py` |
| `core/` | The model (`model.py`) |
| `version.py` | The installed version |
"""
    modules = ["__init__.py", "__main__.py", "version.py", "cli/__init__.py", "cli/common.py", "cli/dev.py",
               "cli/main.py", "cli/run.py", "cli/serve.py", "core/__init__.py", "core/model.py", "core/net.py",
               "web/__init__.py", "web/server.py"]
    assert unnamed_modules(table, modules) == ["__main__.py", "cli/dev.py", "core/net.py", "web/__init__.py",
                                               "web/server.py"]


def test_every_test_named_in_the_code_and_docs_exists():
    named = {}
    files = [path for folder in ("src", "tools", "docs", "deploy") for path in (ROOT / folder).rglob("*")]
    for path in [*files, *PAGES]:
        if path.is_file() and path.suffix in {".py", ".js", ".html", ".css", ".md", ".sh", ".toml"}:
            for test in re.findall(r"\btests/[\w/]+\.py\b", path.read_text(encoding="utf-8")):
                named.setdefault(test, str(path.relative_to(ROOT)))
    assert len(named) > 3
    assert {test: where for test, where in named.items() if not (ROOT / test).is_file()} == {}
