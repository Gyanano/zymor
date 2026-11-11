"""进程入口。`hook recall` 在每条用户消息上运行，所以先走一条不加载完整 CLI 的快速路径。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _fast_hook_recall() -> tuple[int | None, dict[str, Any]]:
    """入参：无。出参：（退出码，已读入的 Hook 输入）。

    配置了服务器时在这里处理完毕；没配置服务器时退出码为 None，交给完整 CLI 走本地检索。
    """
    payload: dict[str, Any] = {}
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        loaded = json.loads(raw) if raw.strip() else {}
        payload = loaded if isinstance(loaded, dict) else {}
        from zymor.config import load_config
        from zymor.remote import RemoteSettings, recall_for_prompt

        cwd = payload.get("cwd")
        start = Path(cwd) if isinstance(cwd, str) and Path(cwd).is_dir() else None
        settings = RemoteSettings.from_config(load_config(start))
        if settings is None:
            return None, payload
        text = recall_for_prompt(settings, payload)
    except Exception as error:
        message = getattr(error, "message", None) or str(error)
        print(f"zymor hook recall 已跳过: {message}", file=sys.stderr)
        return 0, payload
    if text:
        print(text)
    return 0, payload


def main(argv: list[str] | None = None) -> int:
    """入参：命令行参数（默认取 sys.argv）。出参：进程退出码。"""
    args = sys.argv[1:] if argv is None else argv
    if args == ["hook", "recall"]:
        code, payload = _fast_hook_recall()
        if code is not None:
            return code
        from zymor.cli import main as cli_main

        return cli_main(args, hook_payload=payload)
    from zymor.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
