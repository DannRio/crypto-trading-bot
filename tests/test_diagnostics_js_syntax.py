"""Validate that the self-contained diagnostics UI JavaScript parses cleanly.

Node is optional for local Python-only development, available on GitHub's
Ubuntu Actions image. No browser or network access needed.
"""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


def test_diagnostics_frontend_javascript_syntax(tmp_path):
    node=shutil.which("node")
    if node is None:
        pytest.skip("Node.js not available for JavaScript syntax check.")
    html=Path("src/trading_bot/web/diagnostics.html").read_text(
        encoding="utf-8"
    )
    match=re.search(r"<script>([\s\S]*?)</script>",html)
    assert match,"Expected embedded diagnostics script."
    script=tmp_path/"diagnostics.mjs"
    script.write_text(match.group(1),encoding="utf-8")
    result=subprocess.run(
        [node,"--check",str(script)],
        capture_output=True,text=True,check=False,timeout=20,
    )
    assert result.returncode==0,result.stderr
