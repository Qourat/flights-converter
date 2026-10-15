"""Keep repository-relative links in public documentation publishable offline."""

import re
from pathlib import Path

import pytest


@pytest.mark.parametrize("name", ["README.md", "docs/method.md", "docs/review-2026-10.md"])
def test_public_documentation_local_links_exist(name):
    document = Path(__file__).resolve().parents[1] / name
    for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
        if "://" not in target and not target.startswith("#"):
            assert (document.parent / target.split("#", 1)[0]).is_file(), target
