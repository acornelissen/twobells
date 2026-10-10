"""Where the tests find the repo and their recorded and made-up data."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
