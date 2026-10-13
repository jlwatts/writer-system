"""Run the Node page-boot script so pytest CI also covers JS startup."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for page boot tests")
def test_page_components_load_in_javascript():
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "test_page_boot.mjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
    assert result.returncode == 0
    assert "page boot components loaded" in result.stdout
