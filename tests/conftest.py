from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """测试不读写真实的 ~/.zymor 与 ~/.claude，也不受本机 ZYMOR_* 环境变量影响。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in list(os.environ):
        if name.startswith("ZYMOR_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ZYMOR_CLIENT_DIR", str(home / ".zymor" / "client"))
    return home
