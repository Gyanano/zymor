from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from zymor.assembly import memory_context
from zymor.config import Config, load_config
from zymor.errors import ValidationError
from zymor.rule_checks import check_rules, record_rule_check
from zymor.vault import Vault
from zymor.write_service import memory_log


class HarnessAdapter(Protocol):
    """宿主 Hook/Skill 适配器契约；DSH 插件与各宿主 Hook 实现同一接口。"""

    name: str

    def session_start(
        self,
        vault: Vault,
        config: Config,
        *,
        task: str | None = None,
        project: str | None = None,
        budget_tokens: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """入参：vault、配置与任务信息。出参：会话启动上下文。"""
        ...

    def capture(
        self,
        vault: Vault,
        *,
        session_id: str,
        kind: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """入参：vault、会话、事件与元数据。出参：L0 追加结果。"""
        ...

    def generation(
        self,
        vault: Vault,
        *,
        paths: list[str] | None = None,
        content: dict[str, str] | None = None,
        task_type: str | None = None,
        attempt: int = 1,
        max_attempts: int = 3,
        session_id: str | None = None,
        config: Config | None = None,
    ) -> dict[str, Any]:
        """入参：生成内容/路径与修复轮次。出参：规则检查报告。"""
        ...


class LocalHarness:
    """基于本地 vault 的默认实现，供宿主 Hook 直接调用。"""

    name = "local"

    def session_start(
        self,
        vault: Vault,
        config: Config,
        *,
        task: str | None = None,
        project: str | None = None,
        budget_tokens: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        from zymor.security import redact
        from zymor.write_service import read_dissonance

        global_path = config.global_vault_path
        global_vault = Vault.open(global_path) if global_path.is_dir() else None
        pending: list[dict[str, Any]] = []
        for target in (vault, global_vault):
            if target is None:
                continue
            try:
                entries = read_dissonance(target)
            except Exception:
                continue
            pending.extend(item for item in entries if item.get("state") == "open")
        result = memory_context(
            project_vault=vault,
            config=config,
            global_vault=global_vault,
            task=task,
            project=project,
            budget_tokens=budget_tokens,
            session_id=session_id,
            dissonance_provider=lambda _vault: pending,
        )
        payload = result.to_dict()
        payload["text"] = redact(payload["text"])
        payload["pending_questions"] = [
            {
                "qid": item.get("qid"),
                "type": item.get("type", "conflict"),
                "question": redact(str(item.get("question") or "")),
            }
            for item in pending[:3]
        ]
        return {"adapter": self.name, **payload}

    def capture(
        self,
        vault: Vault,
        *,
        session_id: str,
        kind: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return memory_log(
            vault,
            session_id=session_id,
            kind=kind,
            content=content,
            meta={"source": "harness", **(meta or {})},
        )

    def generation(
        self,
        vault: Vault,
        *,
        paths: list[str] | None = None,
        content: dict[str, str] | None = None,
        task_type: str | None = None,
        attempt: int = 1,
        max_attempts: int = 3,
        session_id: str | None = None,
        config: Config | None = None,
    ) -> dict[str, Any]:
        from zymor.invalidation import project_root

        root = project_root(vault)
        files: dict[str, str] = {}
        for raw in paths or []:
            path = (root / raw).resolve()
            if not path.is_relative_to(root):
                raise ValidationError(f"路径越出项目根目录: {raw}", field="paths")
            if not path.is_file():
                raise ValidationError(f"文件不存在: {raw}", field="paths")
            files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
        for raw, text in (content or {}).items():
            normalized = Path(raw.replace("\\", "/"))
            if not raw or normalized.is_absolute() or ".." in normalized.parts:
                raise ValidationError(f"content 路径非法: {raw}", field="content")
            files[normalized.as_posix()] = text
        if not files:
            raise ValidationError("paths 或 content 至少提供一个", field="paths")
        settings = config or load_config()
        scopes: list[tuple[str, Vault]] = [("project", vault)]
        global_path = settings.global_vault_path
        if global_path.is_dir():
            global_vault = Vault.open(global_path)
            if global_vault.root.resolve() != vault.root.resolve():
                scopes.append(("global", global_vault))
        report = check_rules(
            scopes,
            files,
            task_type=task_type,
            attempt=attempt,
            max_attempts=max_attempts,
        )
        record_rule_check(
            vault,
            report,
            session_id=session_id,
            task_type=task_type,
            paths=sorted(files),
        )
        return report.to_dict()
