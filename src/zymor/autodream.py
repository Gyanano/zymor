"""Server-side trigger: run a dream after enough new conversation has been ingested."""

from __future__ import annotations

import sys
import threading
import time
from collections import deque

from zymor.config import Config
from zymor.vault import Vault

DAY_SECONDS = 24 * 3600


class AutoDreamer:
    """入参：vault 与配置。累计新事件数达到阈值时，在后台线程运行一次 NREM 与仲裁。

    同一时刻至多一个 run；两次 run 之间有最小间隔；每天有次数上限。阈值为 0 时不触发。
    """

    def __init__(self, vault: Vault, config: Config) -> None:
        self.vault = vault
        self.config = config
        self.threshold = int(config.get("dream.auto_after_ingest_events", 0) or 0)
        self.min_interval_s = float(config.get("dream.auto_min_interval_minutes", 30)) * 60
        self.max_per_day = int(config.get("dream.auto_max_runs_per_day", 12))
        self._lock = threading.Lock()
        self._pending = 0
        self._running = False
        self._started: deque[float] = deque()
        self.last_error: str | None = None

    def notify(self, captured: int) -> bool:
        """入参：本次新写入的事件数。出参：是否启动了一次后台做梦。"""
        if self.threshold <= 0 or captured <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            self._pending += captured
            while self._started and now - self._started[0] > DAY_SECONDS:
                self._started.popleft()
            too_soon = bool(self._started) and now - self._started[-1] < self.min_interval_s
            if (
                self._running
                or self._pending < self.threshold
                or too_soon
                or len(self._started) >= self.max_per_day
            ):
                return False
            self._running = True
            self._pending = 0
            self._started.append(now)
        threading.Thread(target=self._run, name="zymor-auto-dream", daemon=True).start()
        return True

    def _run(self) -> None:
        from zymor.dream.engine import run_dream

        try:
            report = run_dream(self.vault, self.config, phases=("nrem", "arbitration"))
            self.last_error = None if report.ok else "; ".join(report.errors)[:500]
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {error}"
        finally:
            if self.last_error:
                print(f"zymor 自动做梦失败: {self.last_error}", file=sys.stderr)
            with self._lock:
                self._running = False
