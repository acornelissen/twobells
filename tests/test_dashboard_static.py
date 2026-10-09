"""Static checks on the dashboard's browser files and its contract fixtures.

The page runs under `script-src 'self'; style-src 'self'`, sets feed text only
as text, and ships a vendored Leaflet pinned by hash. These tests keep it that
way without a browser.
"""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "departureboard" / "dashboard_static"
VENDOR = STATIC / "vendor"


# --- Vendored Leaflet ---


def test_vendor_hashes_match():
    leaflet = VENDOR / "leaflet-1.9.4"
    listed = {}
    for line in (leaflet / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        listed[name.lstrip("*")] = digest
    assert set(listed) == {"LICENSE", "leaflet-src.esm.js", "leaflet.css"}
    for name, digest in listed.items():
        assert hashlib.sha256((leaflet / name).read_bytes()).hexdigest() == digest, name
    shipped = {p.name for p in leaflet.iterdir()}
    assert shipped == set(listed) | {"SHA256SUMS"}, "a vendored file is not pinned"
