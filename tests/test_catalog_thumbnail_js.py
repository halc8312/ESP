"""Run the real catalog asset with a deterministic Node DOM and clock."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_public_thumbnail_refresh_dom_behavior():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the catalog DOM behavior checks")
    harness = Path(__file__).with_name("catalog_thumbnails_dom.cjs")
    result = subprocess.run(
        [node, str(harness)], capture_output=True, text=True, timeout=30,
        cwd=harness.parent.parent,
    )
    assert result.returncode == 0, result.stdout + result.stderr
