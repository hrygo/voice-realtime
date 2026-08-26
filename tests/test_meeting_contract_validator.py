"""会议助手契约门禁命令测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_contract_validator_cli_passes_from_repository_root() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/validate-meeting-contract.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "meeting contract validated" in result.stdout
