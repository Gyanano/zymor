from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "vault": {"path": None, "global_path": "~/.zymor/vault"},
    "llm": {
        "provider": "openai-compatible",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "timeout_s": 60,
        "max_retries": 3,
        "models": {
            "extract": None,
            "dream": None,
            "arbitration": None,
            "judge": "gpt-4o",
            "answer": "gpt-4o-mini",
        },
        "pricing": {},
    },
    "embedding": {
        "enabled": False,
        "base_url": None,
        "api_key_env": "OPENAI_API_KEY",
        "model": "text-embedding-3-small",
        "timeout_s": 30,
        "min_score": 0.2,
        "batch_size": 64,
    },
    "retrieval": {
        "weights": {
            "bm25": 0.55,
            "confidence": 0.15,
            "recency": 0.15,
            "arousal": 0.10,
            "hits": 0.05,
        },
        "half_life_days": 30,
        "default_top_k": 8,
        "token_budget": 1200,
        "links_token_budget": 800,
        "links_max_lines": 20,
    },
    "context": {
        "l4_tokens": 200,
        "l1_tokens": 500,
        "l2_tokens": 400,
        "atlas_tokens": 120,
        "structure_tokens": 80,
        "pending_questions_tokens": 200,
        "catalog_line_tokens": 60,
        "total_tokens": 1500,
        "max_facets": 3,
        "max_capsules": 8,
    },
    "write": {
        "salience_review": False,
        "llm_calls_max": 1,
        "dedup_window_hours": 720,
        "dedup_topk": 8,
        "max_input_tokens": 2000,
    },
    "lifecycle": {"focus_idle_close_after_hours": 72, "capsule_downweight_after_days": 30},
    "promotion": {
        "min_confidence": 0.85,
        "min_verify_count": 3,
        "min_hit_count": 3,
        "min_projects": 1,
        "auto_enabled": True,
        "auto_scan_limit": 300,
    },
    "dream": {
        "allow_tool_trigger": True,
        "auto_after_close": False,
        "batch_events": 200,
        "max_events_per_run": 2000,
        "l0_retention_days": 90,
        "updates": {
            "alpha": 0.40,
            "arousal_lambda": 0.7,
            "arousal_min": 0.1,
            "confidence_cap": 0.99,
        },
        "eager_singletons": False,
        "max_candidates": 300,
        "max_arbitrations": 20,
        "max_arbitration_attempts": 3,
        "rem": {"enabled": True, "max_pairs": 3},
        "budget": {"max_llm_calls": 100, "max_usd": 2, "max_nodes_changed": 200},
    },
    "mcp": {
        "transport": "stdio",
        "mode": "rw",
        "host": "127.0.0.1",
        "port": 8765,
        "http_token_env": "ZYMOR_MCP_HTTP_TOKEN",
        "instructions_max_chars": 1800,
        "resources": {"enabled": True},
        "prompts": {"enabled": True},
        "benchmark_profile": False,
    },
    "eval": {
        "budget_usd": 5,
        "regression_budget_usd": 40,
        "release_budget_usd": 300,
        "evidence_budget_tokens": 16000,
        "datasets_dir": "~/.zymor/eval/datasets",
        "results_dir": "~/.zymor/eval/results",
        "judge_model": "gpt-4o",
        "answer_models": ["gpt-4o-mini", "gpt-4o"],
    },
    "logging": {"level": "info", "audit": True, "otel": False},
}

ENV_ALIASES = {
    "ZYMOR_VAULT_PATH": "vault.path",
    "ZYMOR_GLOBAL_VAULT_PATH": "vault.global_path",
    "ZYMOR_LOG_LEVEL": "logging.level",
}

MISSING = object()


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: _deep_copy(DEFAULTS))
    sources: list[str] = field(default_factory=list)

    def get(self, dotted: str, default: Any = MISSING) -> Any:
        """入参：点分路径。出参：配置值；缺失时返回 default 或抛 KeyError。"""
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is MISSING:
                    raise KeyError(f"缺少配置项: {dotted}")
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        """入参：配置组名。出参：该组的浅拷贝。"""
        value = self.data.get(name, {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def vault_path(self) -> Path | None:
        """入参：无。出参：配置中的项目库路径（未配置为 None）。"""
        raw = self.get("vault.path", None)
        return Path(str(raw)).expanduser() if raw else None

    @property
    def global_vault_path(self) -> Path:
        """入参：无。出参：用户库路径。"""
        return Path(str(self.get("vault.global_path"))).expanduser()

    def to_dict(self) -> dict[str, Any]:
        """入参：无。出参：完整配置字典（可 JSON 序列化）。"""
        copied: dict[str, Any] = _deep_copy(self.data)
        return copied


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _parse_env_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _set_dotted(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def find_project_root(start: Path) -> Path | None:
    """入参：起始目录。出参：最近的含 `.zymor/` 的祖先目录；找不到返回 None。"""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".zymor").is_dir():
            return candidate
    return None


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        from zymor.errors import UsageError

        raise UsageError(f"配置文件解析失败: {path}: {error}") from error
    return data if isinstance(data, dict) else {}


def load_config(
    start: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """入参：起始目录、点分覆盖项。出参：合并后的配置。优先级 CLI > 环境 > 项目 > 用户 > 默认。"""
    cwd = (start or Path.cwd()).resolve()
    config = Config()
    user_config = Path("~/.zymor/config.toml").expanduser()
    if user_config.is_file():
        _merge(config.data, _load_toml(user_config))
        config.sources.append(str(user_config))
    project_root = find_project_root(cwd)
    if project_root is not None:
        project_config = project_root / ".zymor" / "config.toml"
        if project_config.is_file():
            _merge(config.data, _load_toml(project_config))
            config.sources.append(str(project_config))
    for env_name, value in os.environ.items():
        if not env_name.startswith("ZYMOR_"):
            continue
        dotted = ENV_ALIASES.get(env_name)
        if dotted is None:
            parts = env_name[len("ZYMOR_") :].lower().split("__")
            if len(parts) < 2:
                continue
            dotted = ".".join(parts)
        _set_dotted(config.data, dotted, _parse_env_value(value))
        config.sources.append(f"env:{env_name}")
    for dotted, value in (overrides or {}).items():
        _set_dotted(config.data, dotted, value)
        config.sources.append(f"cli:{dotted}")
    return config
