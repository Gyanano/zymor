from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from zymor import __version__
from zymor.atlas import generate as generate_artifacts
from zymor.config import Config, load_config
from zymor.dream.engine import run_dream
from zymor.errors import NotFoundError, UsageError, ZymorError
from zymor.maintenance import (
    confirm_persona_draft,
    forget_node,
    review_nodes,
    write_stats,
)
from zymor.model.event import EVENT_KINDS, append_event
from zymor.model.focus import TASK_TYPES, FocusFile, new_focus
from zymor.model.node import REQUIRED_H2, MemoryNode
from zymor.retrieval import (
    RetrievalSettings,
    SearchFilters,
    memory_read,
    memory_search,
)
from zymor.store.audit import read_audit
from zymor.store.index import Index
from zymor.store.nodes import read_node, update_node, write_node
from zymor.util.fs import atomic_write_text
from zymor.util.time import now_iso
from zymor.vault import (
    CONFIG_TEMPLATE,
    Vault,
    project_root_for,
    project_vault_root,
    resolve_vault,
)
from zymor.write_service import (
    memory_write,
    read_dissonance,
    update_dissonance,
)


def _common_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--vault", metavar="PATH", default=argparse.SUPPRESS, help="覆盖 vault 路径"
    )
    parent.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="机器可读输出",
    )
    parent.add_argument(
        "--log-level",
        choices=("debug", "info", "warn", "error"),
        default=argparse.SUPPRESS,
        help="日志级别",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    common = _common_parent()
    parser = argparse.ArgumentParser(prog="zymor", description="Zymor 记忆系统", parents=[common])
    parser.add_argument("--version", action="version", version=f"zymor {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    init = sub.add_parser("init", parents=[common], help="初始化 vault")
    init.add_argument("--global", dest="global_vault", action="store_true", help="初始化用户库")
    init.add_argument("--name", help="vault 名称")

    write = sub.add_parser("write", parents=[common], help="写入节点（Markdown 输入，服务端校验）")
    source = write.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="节点 Markdown 文件")
    source.add_argument("--stdin", action="store_true", help="从标准输入读取")

    remember = sub.add_parser("remember", parents=[common], help="快速捕获一条记忆")
    remember.add_argument("text")
    remember.add_argument(
        "--category",
        choices=("episodic", "semantic", "procedural", "axioms"),
        default="semantic",
    )
    remember.add_argument("--layer", choices=("L2", "L3"), default="L3")

    export = sub.add_parser("export", parents=[common], help="导出可校验的节点 bundle")
    export.add_argument("output", type=Path)
    import_parser = sub.add_parser("import", parents=[common], help="导入节点 bundle")
    import_parser.add_argument("bundle", type=Path)
    import_parser.add_argument("--mode", choices=("skip", "overwrite"), default="skip")

    search = sub.add_parser("search", parents=[common], help="检索节点")
    search.add_argument("query")
    search.add_argument(
        "--category", action="append", choices=("episodic", "semantic", "procedural", "axioms")
    )
    search.add_argument("--layer", action="append", choices=("L2", "L3", "L4"))
    search.add_argument(
        "--status", action="append", choices=("hypothesis", "verified", "disputed", "deprecated")
    )
    search.add_argument("--top-k", type=int, default=None)
    search.add_argument("--include-stale", action="store_true", help="包含锚点失效的节点")
    search.add_argument("--semantic", action="store_true", help="叠加向量语义检索")

    read = sub.add_parser("read", parents=[common], help="读取节点")
    read.add_argument("id")
    read.add_argument("--max-tokens", type=int, default=None)

    log = sub.add_parser("log", parents=[common], help="追加一条 L0 事件")
    log.add_argument("--kind", required=True, choices=EVENT_KINDS)
    log.add_argument("--content", required=True)
    log.add_argument("--session", default="cli")

    capture = sub.add_parser("capture", parents=[common], help="轮询项目文件变化并写入 L0")
    capture.add_argument("--path", default=".", help="项目内待监听目录")
    capture.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒")
    capture.add_argument("--session", default=None, help="默认自动生成 capture-<时间>")
    capture.add_argument("--once", action="store_true", help="只轮询一次后退出")
    capture.add_argument("--ignore", action="append", default=[], help="额外忽略 glob（可重复）")
    capture.add_argument("--max-events", type=int, default=50, help="单轮最多写入事件数")

    index = sub.add_parser("index", parents=[common], help="索引维护")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    index_sub.add_parser("sync", parents=[common], help="增量同步索引")
    index_sub.add_parser("rebuild", parents=[common], help="全量重建索引")
    index_sub.add_parser("catalog", parents=[common], help="生成 ATLAS 与 catalog")
    index_sub.add_parser("embed", parents=[common], help="为节点构建向量索引（需 embedding 配置）")

    semantic = sub.add_parser("semantic", parents=[common], help="语义检索（需向量索引）")
    semantic.add_argument("query")
    semantic.add_argument("--top-k", type=int, default=None)

    rules = sub.add_parser("rules", parents=[common], help="读取 verified 项目规则")
    rules.add_argument("--task-type", default=None)
    rules.add_argument("--limit", type=int, default=5)
    rules.add_argument("--token-budget", type=int, default=400)

    rule_check = sub.add_parser("rule-check", parents=[common], help="用 verified 规则检查文件")
    rule_check.add_argument("--file", action="append", default=[], help="可重复")
    rule_check.add_argument("--stdin", action="store_true", help="从标准输入读取待检查内容")
    rule_check.add_argument(
        "--path", default="<stdin>", help="stdin 对应的项目内虚拟路径（如 config.py）"
    )
    rule_check.add_argument("--task-type", default=None)
    rule_check.add_argument("--attempt", type=int, default=1, help="当前修复轮次（从 1 开始）")
    rule_check.add_argument("--max-attempts", type=int, default=3, help="最大修复轮次")

    review = sub.add_parser("review", parents=[common], help="列出待人工确认的节点")
    review.add_argument("--status", action="append", choices=("hypothesis", "disputed"))

    promote = sub.add_parser("promote", parents=[common], help="跨项目晋升 L3 节点到用户库")
    promote.add_argument("id")
    promote.add_argument("--dry-run", action="store_true", help="只评估资格，不写库")
    promote.add_argument("--yes", action="store_true", help="确认晋升")
    promote.add_argument("--force", action="store_true", help="跳过项目数门槛")

    anchor = sub.add_parser("anchor", parents=[common], help="为节点绑定文件/依赖版本锚点")
    anchor.add_argument("id")
    anchor.add_argument("--file", action="append", default=None, help="文件路径，可重复")
    anchor.add_argument(
        "--package",
        action="append",
        default=None,
        help="依赖清单:包名，如 package.json:react（可重复）",
    )
    anchor.add_argument(
        "--api",
        action="append",
        default=None,
        help="版本清单:点分 key，如 api-versions.toml:openai.responses（可重复）",
    )

    stale = sub.add_parser("stale", parents=[common], help="检查/标记失效的锚定节点")
    stale.add_argument("--deprecate", action="store_true", help="把失效节点标记为 deprecated")

    adopt = sub.add_parser("adopt", parents=[common], help="记录当前项目采纳了某条用户库知识")
    adopt.add_argument("id")

    forget = sub.add_parser("forget", parents=[common], help="隐私删除节点（不可恢复）")
    forget.add_argument("id")
    forget.add_argument("--yes", action="store_true", help="确认删除")

    answer = sub.add_parser("answer", parents=[common], help="回答晨间追问")
    answer.add_argument("qid")
    answer.add_argument("--text", required=True)
    answer.add_argument("--decision", choices=("accept", "dismiss", "defer"), default=None)

    doctor = sub.add_parser("doctor", parents=[common], help="自检")
    doctor.set_defaults(command="doctor")

    stats = sub.add_parser("stats", parents=[common], help="统计")
    stats.set_defaults(command="stats")

    focus = sub.add_parser("focus", parents=[common], help="查看/更新焦点文件")
    focus.add_argument("--session", default="cli")
    focus.add_argument("--task")
    focus.add_argument("--task-type", choices=TASK_TYPES, dest="task_type")
    focus.add_argument("--status", choices=("active", "closed"))
    focus.add_argument("--create", action="store_true")

    dream = sub.add_parser("dream", parents=[common], help="做梦（NREM/仲裁/REM）")
    dream.add_argument("--phase", choices=("nrem", "arbitration", "rem", "all"), default="all")
    dream.add_argument("--dry-run", action="store_true", help="只输出计划，不写文件、不调 LLM")
    dream.add_argument("--budget-usd", type=float, default=None)
    dream.add_argument("--history", type=int, default=None, metavar="N", help="列出最近 N 次 run")
    dream.add_argument("--watch", action="store_true", help="循环运行 dream（Ctrl+C 停止）")
    dream.add_argument("--interval", type=float, default=300.0, help="--watch 的运行间隔秒")

    hook = sub.add_parser("hook", parents=[common], help="宿主 Hook/Skill 接入入口")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)
    hook_context = hook_sub.add_parser("context", parents=[common], help="输出会话启动上下文")
    hook_context.add_argument("--task", default=None)
    hook_context.add_argument("--project", default=None)
    hook_context.add_argument("--budget-tokens", type=int, default=None)
    hook_context.add_argument("--session-id", default=None)

    panel = sub.add_parser("panel", parents=[common], help="启动只读记忆面板")
    panel.add_argument("--host", default="127.0.0.1")
    panel.add_argument("--port", type=int, default=8765)

    mcp = sub.add_parser("mcp", parents=[common], help="启动 MCP Server")
    mcp.add_argument("--transport", choices=("stdio", "http"), default=None)
    mcp.add_argument("--port", type=int, default=None)
    mcp.add_argument("--read-only", action="store_true", help="只读模式：不下发写工具")

    return parser


def _flag(args: argparse.Namespace, name: str, default: Any = False) -> Any:
    return getattr(args, name, default)


def _emit(args: argparse.Namespace, payload: Any, human: str) -> None:
    if _flag(args, "json_output"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(human)


def _context(args: argparse.Namespace) -> tuple[Config, Vault]:
    config = load_config()
    vault = resolve_vault(config, _flag(args, "vault", None))
    return config, vault


def _scopes(config: Config, vault: Vault) -> list[tuple[str, Vault]]:
    label = "global" if vault.info.kind == "global" else "project"
    scopes: list[tuple[str, Vault]] = [(label, vault)]
    global_vault = _global_vault(config)
    if vault.info.kind == "project" and global_vault is not None:
        if global_vault.root != vault.root:
            scopes.append(("global", global_vault))
    return scopes


def _global_vault(config: Config) -> Vault | None:
    root = Path(str(config.get("vault.global_path"))).expanduser()
    if not (root / "vault.json").is_file():
        return None
    return Vault.open(root)


def cmd_init(args: argparse.Namespace) -> int:
    config = load_config()
    cwd = Path.cwd()
    cli_vault = _flag(args, "vault", None)
    global_flag = bool(_flag(args, "global_vault"))
    if cli_vault:
        root = Path(cli_vault)
        kind = "global" if global_flag else "project"
    elif global_flag:
        root = Path(str(config.get("vault.global_path"))).expanduser()
        kind = "global"
    else:
        root = project_vault_root(cwd)
        kind = "project"
    explicit_name = getattr(args, "name", None)
    if explicit_name:
        name = explicit_name
    elif kind == "global":
        name = "global"
    elif root.parent.name == ".zymor":
        name = root.parent.parent.name
    else:
        name = cwd.name
    existed = (root / "vault.json").is_file()
    chosen_project_root = cwd if cli_vault else (project_root_for(cwd) or cwd)
    vault = Vault.init(
        root,
        kind,
        name,
        project_root=chosen_project_root if kind == "project" else None,
    )
    if root.parent.name == ".zymor":
        config_path = root.parent / "config.toml"
        if not config_path.is_file():
            atomic_write_text(config_path, CONFIG_TEMPLATE)
    payload = {
        "path": str(vault.root),
        "kind": vault.info.kind,
        "name": vault.info.name,
        "vault_id": vault.info.vault_id,
        "existed": existed,
    }
    human = f"{'已存在' if existed else '已初始化'} {vault.info.kind} vault: {vault.root}"
    _emit(args, payload, human)
    return 0


def cmd_write(args: argparse.Namespace) -> int:
    if _flag(args, "stdin"):
        text = sys.stdin.read()
    else:
        file_path = Path(args.file)
        if not file_path.is_file():
            raise UsageError(f"文件不存在: {file_path}", hint="检查 --file 路径")
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise UsageError(
                f"文件不是合法 UTF-8: {file_path}", hint="另存为 UTF-8（无 BOM）后重试"
            ) from error
    node = MemoryNode.from_markdown(text)
    _, vault = _context(args)
    result = write_node(vault, node, "write")
    payload = result.to_dict(vault)
    state = "已写入" if result.created else "内容未变化"
    _emit(args, payload, f"{state}: {node.id} -> {payload['path']}")
    return 0


def _remember_body(category: str, text: str) -> str:
    names = REQUIRED_H2[category]
    parts: list[str] = []
    for position, name in enumerate(names):
        content = text if position == 0 else "未提供。"
        parts.append(f"## {name}\n{content}")
    return "\n\n".join(parts)


def cmd_remember(args: argparse.Namespace) -> int:
    text = str(args.text).strip()
    if not text:
        raise UsageError("记忆内容不能为空", hint="zymor remember '<一句话结论>'")
    title = text.splitlines()[0].strip()[:80]
    summary = " ".join(text.split())[:400]
    config, vault = _context(args)
    project = vault.info.name if args.layer == "L2" else None
    outcome = memory_write(
        vault,
        config,
        title=title,
        summary=summary,
        body=_remember_body(str(args.category), text),
        category=str(args.category),
        layer=str(args.layer),
        session_id="cli",
        project=project,
    )
    payload = outcome.to_dict()
    if outcome.action == "rejected-gate":
        human = "未记录：内容被写作门控丢弃"
    else:
        human = f"{outcome.action}: {outcome.node_id} -> {outcome.path}"
        for warning in outcome.warnings:
            human += f"\n提示: {warning}"
    _emit(args, payload, human)
    return 0


def cmd_rule_check(args: argparse.Namespace) -> int:
    from zymor.invalidation import project_root
    from zymor.rule_checks import check_rules, record_rule_check

    config, vault = _context(args)
    root = project_root(vault).resolve()
    files: dict[str, str] = {}
    for raw in args.file:
        path = Path(raw).resolve()
        if not path.is_file():
            raise UsageError(f"文件不存在: {raw}")
        if not path.is_relative_to(root):
            raise UsageError(f"文件不在项目内: {raw}")
        files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    if args.stdin:
        virtual = Path(args.path.replace("\\", "/"))
        if not args.path or virtual.is_absolute() or ".." in virtual.parts:
            raise UsageError(f"stdin 虚拟路径非法: {args.path}")
        files[virtual.as_posix()] = sys.stdin.read()
    if not files:
        raise UsageError("至少提供 --file 或 --stdin")
    report = check_rules(
        _scopes(config, vault),
        files,
        task_type=args.task_type,
        attempt=args.attempt,
        max_attempts=args.max_attempts,
    )
    record_rule_check(
        vault,
        report,
        session_id=None,
        task_type=args.task_type,
        paths=sorted(files),
    )
    payload = report.to_dict()
    if report.diagnostics:
        lines = [
            ("PASS" if item.passed else "FAIL")
            + f" {item.title} [{item.path}] {item.kind}={item.value}"
            + (f"：{item.message}" if item.message else "")
            + (f" → {item.repair}" if item.repair else "")
            for item in report.diagnostics
        ]
        lines.append(f"attempt {report.attempt}/{report.max_attempts} → {report.next_action}")
        if report.skipped_checks:
            lines.append(f"跳过 {report.skipped_checks} 个无匹配文件的检查")
        human = "\n".join(lines)
    elif report.skipped_checks:
        human = (
            f"没有可执行检查：{report.skipped_checks} 个检查没有匹配文件（如需检查请传入对应文件）"
        )
    else:
        human = "没有可执行检查的 verified 规则"
    _emit(args, payload, human)
    return 0 if report.passed else 1


def cmd_capture(args: argparse.Namespace) -> int:
    from zymor.capture import DEFAULT_IGNORE, run_capture
    from zymor.invalidation import project_root

    _config, vault = _context(args)
    if vault.info.kind != "project":
        raise UsageError("capture 只监听项目库", hint="在项目目录运行，或改用项目库 --vault")
    root = project_root(vault).resolve()
    watch = (root / args.path).resolve()
    if not watch.is_relative_to(root) or not watch.is_dir():
        raise UsageError(f"监听路径必须是项目内目录: {args.path}")
    if args.interval <= 0:
        raise UsageError("--interval 必须大于 0")
    session_id = args.session or f"capture-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    try:
        result = run_capture(
            vault,
            watch,
            session_id=session_id,
            interval_s=args.interval,
            ignore=tuple(DEFAULT_IGNORE) + tuple(args.ignore),
            max_events=args.max_events,
            once=args.once,
        )
    except KeyboardInterrupt:
        result = {"captured": 0, "polls": 0, "warnings": ["已停止"]}
    payload = {"session_id": session_id, "path": watch.relative_to(root).as_posix(), **result}
    lines = [
        f"capture: {result['captured']} events / {result['polls']} polls",
        f"pending: {result.get('pending', 0)}",
        f"session: {session_id}",
    ]
    lines.extend(f"提示: {warning}" for warning in result["warnings"])
    _emit(args, payload, "\n".join(lines))
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    from zymor.retrieval import memory_rules

    config, vault = _context(args)
    result = memory_rules(
        _scopes(config, vault),
        task_type=args.task_type,
        limit=args.limit,
        token_budget=args.token_budget,
    )
    lines = [f"[verified] {rule['title']}：{rule['summary']}" for rule in result["rules"]]
    _emit(args, result, "\n".join(lines) if lines else "没有 verified 规则")
    return 0


def cmd_semantic(args: argparse.Namespace) -> int:
    from zymor.embeddings import (
        EmbeddingSettings,
        OpenAIEmbeddingClient,
        semantic_search,
    )

    config, vault = _context(args)
    settings = EmbeddingSettings.from_config(config)
    client = OpenAIEmbeddingClient(settings)
    result = semantic_search(
        _scopes(config, vault),
        args.query,
        settings,
        client=client,
        top_k=args.top_k or 8,
    )
    hits = result["hits"]
    lines = [
        f"{position}. {hit['title']} [{hit['id']}] {hit['layer']}/{hit['category']} "
        f"score={hit['score']:.3f} {hit['status']}"
        for position, hit in enumerate(hits, start=1)
    ]
    for warning in result["warnings"]:
        lines.append(f"提示: {warning}")
    _emit(args, result, "\n".join(lines) if lines else "无结果")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from zymor.bundle import export_bundle

    _config, vault = _context(args)
    result = export_bundle(vault, args.output)
    human = f"已导出 {result['node_count']} 个节点 → {result['path']}"
    _emit(args, result, human)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from zymor.bundle import import_bundle

    _config, vault = _context(args)
    result = import_bundle(vault, args.bundle, mode=args.mode)
    human = f"已导入 {result['imported']} 个节点（跳过 {result['skipped']}）← {result['path']}"
    _emit(args, result, human)
    return 0


def _redact_value(value: Any) -> Any:
    from zymor.security import redact

    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


def cmd_search(args: argparse.Namespace) -> int:
    config, vault = _context(args)
    settings = RetrievalSettings.from_config(config)
    explicit_deprecated = bool(args.status and "deprecated" in args.status)
    filters = SearchFilters(
        categories=args.category,
        layers=args.layer,
        statuses=args.status,
        include_deprecated=explicit_deprecated,
        valid_only=not explicit_deprecated,
        include_stale=bool(args.include_stale),
    )
    top_k = args.top_k if args.top_k is not None else settings.default_top_k
    if args.semantic:
        from zymor.embeddings import (
            EmbeddingSettings,
            OpenAIEmbeddingClient,
            hybrid_search,
        )

        embedding_settings = EmbeddingSettings.from_config(config)
        hybrid_result = hybrid_search(
            _scopes(config, vault),
            args.query,
            config=config,
            client=OpenAIEmbeddingClient(embedding_settings),
            top_k=top_k,
        )
        hits = hybrid_result["hits"]
        lines = [
            f"{position}. {hit['title']} [{hit['id']}] {hit['layer']}/{hit['category']} "
            f"{hit['status']} score={hit['score']:.3f}"
            for position, hit in enumerate(hits, start=1)
        ]
        for warning in hybrid_result["warnings"]:
            lines.append(f"提示: {warning}")
        payload = _redact_value(hits)
        human = "\n".join(lines) if lines else "无结果"
        _emit(args, payload, _redact_value(human))
        return 0
    result = memory_search(
        _scopes(config, vault),
        args.query,
        settings=settings,
        filters=filters,
        top_k=top_k,
    )
    payload = _redact_value([asdict(hit) for hit in result.hits])
    lines = [
        f"{position}. {hit.title} [{hit.id}] "
        f"{hit.layer}/{hit.category} {hit.status} "
        f"score={hit.score:.3f} {hit.path}" + (" [stale]" if hit.stale else "")
        for position, hit in enumerate(result.hits, start=1)
    ]
    for warning in result.warnings:
        lines.append(f"提示: {warning}")
    human = "\n".join(lines) if lines else "无结果"
    _emit(args, payload, _redact_value(human))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    config, vault = _context(args)
    max_tokens = args.max_tokens if args.max_tokens is not None else 1500
    result = memory_read(_scopes(config, vault), args.id, max_tokens=max_tokens)
    node = result.node
    payload = {
        "id": node.id,
        "title": node.title,
        "summary": node.summary,
        "layer": node.layer,
        "category": node.category,
        "status": node.status,
        "confidence": node.confidence,
        "arousal": node.arousal,
        "hit_count": node.hit_count,
        "verify_count": node.verify_count,
        "tags": node.tags,
        "path": result.path,
        "scope": result.scope,
        "tokens_estimate": result.tokens_estimate,
        "body": node.body,
        "truncated": result.truncated,
        "warnings": result.warnings,
    }
    if result.truncated:
        human = (
            f"{node.title}\n[{node.id}] {node.layer}/{node.category} {node.status}\n\n"
            f"{node.body}\n…（已截断）"
        )
    else:
        human = node.to_markdown()
    _emit(args, _redact_value(payload), _redact_value(human))
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    _, vault = _context(args)
    with vault.writing("log"):
        entry = append_event(vault.l0_dir, args.session, args.kind, args.content)
    payload = {
        "session_id": entry.session_id,
        "seq": entry.seq,
        "kind": entry.kind,
        "ts": entry.ts,
    }
    _emit(args, payload, f"已记录 L0 事件 {entry.session_id}#{entry.seq} ({entry.kind})")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    config, vault = _context(args)
    if args.index_command == "catalog":
        paths = generate_artifacts(vault)
        _emit(
            args,
            {"generated": paths},
            "生成物已刷新: " + ", ".join(f"{key}={value}" for key, value in paths.items()),
        )
        return 0
    if args.index_command == "embed":
        from zymor.embeddings import build_embeddings

        count = build_embeddings(vault, config)
        _emit(args, {"embedded": count}, f"已写入 {count} 条向量")
        return 0
    index = Index(vault.root)
    if args.index_command == "sync":
        report = index.sync()
        verb = "同步"
    else:
        report = index.rebuild()
        verb = "重建"
    payload = report.to_dict()
    human = (
        f"索引{verb}完成：扫描 {report.scanned}，更新 {report.upserted}，"
        f"删除 {report.deleted}，无效 {len(report.invalid)}"
    )
    _emit(args, payload, human)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    issues: list[dict[str, str]] = []

    def add(severity: str, code: str, message: str, fix: str) -> None:
        issues.append({"severity": severity, "code": code, "message": message, "fix": fix})

    try:
        config, vault = _context(args)
    except ZymorError as error:
        add("error", "vault", error.message, error.hint or "运行 zymor init")
        payload = {"ok": False, "issues": issues}
        _emit(args, payload, f"[error] {error.message}\n  修复: {error.hint or 'zymor init'}")
        return 1
    if vault.is_locked():
        holder = vault.lock_holder() or {}
        add(
            "warning",
            "lock",
            f"存在写入锁（pid={holder.get('pid')} command={holder.get('command')}）",
            "确认没有其他 zymor 进程后删除 .lock",
        )
    index = Index(vault.root)
    try:
        needs_rebuild = index._needs_rebuild()
        index.ensure()
        if needs_rebuild:
            add("warning", "index", "索引缺失或版本不符，已重建", "检查索引统计是否符合预期")
    except ZymorError as error:
        add("error", "index", error.message, "运行 `zymor index rebuild`")
    dirty = index.dirty_reason()
    if dirty is not None:
        add("warning", "index-dirty", f"索引 dirty: {dirty}", "运行 `zymor index rebuild`")
    invalid_count = 0
    for path in sorted(vault.nodes_dir.glob("*/*.md")):
        if path.name.startswith("_"):
            continue
        try:
            MemoryNode.from_markdown(path.read_text(encoding="utf-8"))
        except ZymorError as error:
            invalid_count += 1
            if invalid_count <= 20:
                add(
                    "error",
                    "node",
                    f"{vault.relative(path)}: {error.message}",
                    "修正 frontmatter 或正文后重建索引",
                )
        except (OSError, UnicodeError) as error:
            add("error", "node", f"{vault.relative(path)}: 不可读: {error}", "检查文件权限与编码")
    try:
        with index.connect() as connection:
            known = {row["id"] for row in connection.execute("SELECT id FROM nodes")}
            broken = [
                row
                for row in connection.execute("SELECT src, dst FROM links")
                if row["dst"] not in known
            ]
            if broken:
                preview = ", ".join(f"{row['src']}->{row['dst']}" for row in broken[:5])
                add(
                    "error",
                    "links",
                    f"{len(broken)} 条断链: {preview}",
                    "修复正文 WikiLink 或补写目标节点",
                )
    except ZymorError as error:
        add("error", "index", error.message, "运行 `zymor index rebuild`")
    last_op: dict[str, str] = {}
    for record in read_audit(vault.writes_audit_path):
        node_id = record.get("node_id")
        if isinstance(node_id, str):
            last_op[node_id] = str(record.get("op", ""))
    pending = [node_id for node_id, op in last_op.items() if op == "intent"]
    if pending:
        add(
            "error",
            "uncommitted",
            f"{len(pending)} 个节点存在未提交的写入意图（可能崩溃中断）",
            "运行 `zymor index rebuild` 后核对节点文件",
        )
    errors = [issue for issue in issues if issue["severity"] == "error"]
    payload = {"ok": not errors, "issues": issues}
    if not issues:
        human = "自检通过：无问题"
    else:
        lines = [
            f"[{issue['severity']}] {issue['code']}: {issue['message']}\n  修复: {issue['fix']}"
            for issue in issues
        ]
        human = "\n".join(lines)
    _emit(args, payload, human)
    return 0 if not errors else 1


def cmd_stats(args: argparse.Namespace) -> int:
    _, vault = _context(args)
    counts = Index(vault.root).counts()
    extras = write_stats(vault)
    payload = {"vault": str(vault.root), "kind": vault.info.kind, **counts, **extras}
    lines = [f"vault: {vault.root} ({vault.info.kind})", f"节点总数: {counts['total']}"]
    for layer, count in counts["layers"].items():
        lines.append(f"  {layer}: {count}")
    for status, count in counts["statuses"].items():
        lines.append(f"  {status}: {count}")
    lines.append(f"链接: {counts['links']}")
    actions = ", ".join(f"{key}={value}" for key, value in extras["write_actions"].items())
    lines.append(f"写入动作: {actions or '无'}")
    lines.append(f"L0 事件: {extras['l0_events']}")
    lines.append(
        f"dream: runs={extras['dream_runs']} failures={extras['dream_failures']} "
        f"last={extras['last_dream_at'] or '无'}"
    )
    lines.append(
        f"规则检查: checks={extras['rule_checks']} failures={extras['rule_check_failures']} "
        f"last={extras['last_rule_check_at'] or '无'}"
    )
    lines.append(f"检索: records={extras['retrievals']} reads={extras['retrieval_reads']}")
    lines.append(f"stale 节点: {extras['stale_nodes']}")
    _emit(args, payload, "\n".join(lines))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    _, vault = _context(args)
    statuses = tuple(args.status) if args.status else ("hypothesis", "disputed")
    rows = review_nodes(vault, statuses=statuses)
    payload = {"count": len(rows), "nodes": rows}
    if rows:
        human = "\n".join(
            f"- {row['title']} [{row['id']}] {row['status']} {row['updated_at']}" for row in rows
        )
    else:
        human = "没有待人工确认的节点"
    _emit(args, payload, human)
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    config, vault = _context(args)
    from zymor.promotion import promote_to_global
    from zymor.vault import Vault

    global_path = config.global_vault_path
    if not global_path.is_dir():
        raise UsageError(
            "未找到用户库",
            hint="运行 `zymor init --global` 初始化用户库",
        )
    global_vault = Vault.open(global_path)
    result = promote_to_global(
        vault,
        global_vault,
        config,
        args.id,
        dry_run=bool(args.dry_run),
        confirm=bool(args.yes),
        force=bool(args.force),
    )
    if result.status == "promoted":
        human = f"已晋升: {result.node_id} → {result.global_id}"
    elif result.status == "dry-run":
        human = f"符合晋升条件: {result.title or result.node_id}（加 --yes 执行）"
    elif result.status == "duplicate":
        human = f"用户库已有同标题节点: {result.global_id}"
    else:
        human = "未晋升: " + "；".join(result.reasons)
    _emit(args, result.to_dict(), human)
    return 0


def cmd_anchor(args: argparse.Namespace) -> int:
    _, vault = _context(args)
    from zymor.invalidation import api_anchor, file_anchor, version_anchor

    if not args.file and not args.package and not args.api:
        raise UsageError("至少提供一个 --file / --package / --api")
    node, _ = read_node(vault, args.id)
    anchors = set(node.anchors)
    for path in args.file or []:
        anchors.add(file_anchor(vault, Path(path)))
    for spec in args.package or []:
        manifest, separator, package = str(spec).partition(":")
        if not separator or not manifest or not package:
            raise UsageError("--package 格式应为 <清单路径>:<包名>")
        anchors.add(version_anchor(vault, Path(manifest), package))
    for spec in args.api or []:
        manifest, separator, key = str(spec).partition(":")
        if not separator or not manifest or not key:
            raise UsageError("--api 格式应为 <版本清单路径>:<点分 key>")
        anchors.add(api_anchor(vault, Path(manifest), key))
    updated = update_node(vault, args.id, {"anchors": sorted(anchors)}, command="anchor")
    _emit(
        args,
        {"id": updated.id, "anchors": updated.anchors},
        f"已锚定 {updated.id}: {len(updated.anchors)} 个目标",
    )
    return 0


def cmd_stale(args: argparse.Namespace) -> int:
    _, vault = _context(args)
    from zymor.invalidation import deprecate_stale, scan_stale

    items = scan_stale(vault)
    changed: list[str] = []
    if args.deprecate and items:
        changed = deprecate_stale(vault, items)
    payload = {"count": len(items), "nodes": items, "deprecated": changed}
    if items:
        human = "\n".join(
            f"- {item['title']} [{item['id']}]: " + "；".join(item["reasons"]) for item in items
        )
    else:
        human = "没有失效的锚定节点"
    _emit(args, payload, human)
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    config, vault = _context(args)
    from zymor.promotion import adopt_global
    from zymor.vault import Vault

    global_path = config.global_vault_path
    if not global_path.is_dir():
        raise UsageError("未找到用户库", hint="运行 `zymor init --global` 初始化用户库")
    updated = adopt_global(Vault.open(global_path), vault.info.name, args.id)
    _emit(
        args,
        {"id": updated.id, "projects": updated.projects},
        f"已采纳 {updated.id}：projects={', '.join(updated.projects)}",
    )
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    if not args.yes:
        raise UsageError("forget 是不可恢复的隐私删除，需要 --yes 确认")
    _, vault = _context(args)
    result = forget_node(vault, args.id)
    _emit(args, result, f"已删除: {result['id']}（索引已重建）")
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    config, vault = _context(args)
    items = read_dissonance(vault)
    item = next((entry for entry in items if entry.get("qid") == args.qid), None)
    if item is None:
        raise NotFoundError(
            f"追问不存在: {args.qid}",
            hint="用 dream/dissonance.md 查看队列，或先运行 zymor dream",
        )
    text = str(args.text).strip()
    if not text:
        raise UsageError("回答内容不能为空")
    note = f"answer:{args.qid}: {text}"
    entry_type = str(item.get("type") or "conflict")
    decision = getattr(args, "decision", None)
    updated: list[str] = []
    warnings: list[str] = []
    if entry_type == "persona-proposal":
        decision = decision or "accept"
        if decision == "accept":
            global_vault = _global_vault(config)
            if global_vault is None:
                raise UsageError(
                    "人格规则 draft 在用户库，但未找到用户库",
                    hint="先运行 zymor init --global，或手工把 draft 移到用户库",
                )
            result = confirm_persona_draft(vault, global_vault, qid=str(args.qid), note=note)
            updated.append(result["draft_id"])
            entry = {"state": result["state"]}
        elif decision == "dismiss":
            entry = update_dissonance(vault, str(args.qid), state="dismissed")
        else:
            entry = update_dissonance(
                vault,
                str(args.qid),
                attempts=int(item.get("attempts") or 0) + 1,
            )
    elif decision == "dismiss":
        entry = update_dissonance(vault, str(args.qid), state="dismissed")
    else:
        for node_id in (item.get("new_id"), item.get("old_id")):
            if not node_id:
                continue
            try:
                node, _ = read_node(vault, str(node_id))
                update_node(
                    vault,
                    str(node_id),
                    {"evidence": [*node.evidence, note]},
                    command="answer",
                )
                updated.append(str(node_id))
            except ZymorError as error:
                warnings.append(f"{node_id}: {error.message}")
        entry = update_dissonance(vault, str(args.qid), state="answered")
    from zymor.dream.state import append_dream_log

    append_dream_log(
        vault,
        {
            "run_id": f"answer:{args.qid}",
            "ts": now_iso(),
            "phase": "answer",
            "action": str(entry.get("state") or "answered"),
            "details": {
                "qid": args.qid,
                "text": text,
                "type": entry_type,
                "decision": decision,
                "updated_nodes": updated,
            },
            "tokens": 0,
            "usd": 0.0,
        },
    )
    payload = {
        "qid": args.qid,
        "type": entry_type,
        "state": entry.get("state"),
        "decision": decision,
        "updated_nodes": updated,
        "warnings": warnings,
    }
    human = f"已记录回答 {args.qid}（状态 {entry.get('state')}，更新 {len(updated)} 个节点）"
    for warning in warnings:
        human += f"\n提示: {warning}"
    _emit(args, payload, human)
    return 0


def _dream_lines(report: Any) -> str:
    if report.dry_run:
        lines = [f"dry-run {report.run_id}（不写文件、不调 LLM）"]
        lines.extend(f"- {step}" for step in report.plan.steps)
        lines.append(f"预计 LLM 调用: {report.plan.estimated_llm_calls}")
        return "\n".join(lines)
    lines = [f"run {report.run_id} ok={report.ok}"]
    for phase in report.phases:
        lines.append(
            f"- {phase.phase}: events={phase.processed_events} "
            f"candidates={phase.candidates} created={phase.nodes_created} "
            f"updated={phase.nodes_updated} errors={len(phase.errors)}"
        )
    budget = report.budget
    lines.append(
        f"budget: llm={budget['llm_calls']}/{budget['max_llm_calls']} "
        f"usd={float(budget['usd']):.3f}/{float(budget['max_usd']):.2f} "
        f"nodes={budget['nodes_changed']}/{budget['max_nodes_changed']}"
    )
    for warning in report.warnings:
        lines.append(f"提示: {warning}")
    for error in report.errors:
        lines.append(f"错误: {error}")
    return "\n".join(lines)


def cmd_panel(args: argparse.Namespace) -> int:
    from zymor.panel import run_panel

    config, vault = _context(args)
    run_panel(vault, config, host=args.host, port=args.port)
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    from zymor.harness import LocalHarness

    config, vault = _context(args)
    if args.hook_command == "context":
        result = LocalHarness().session_start(
            vault,
            config,
            task=args.task,
            project=args.project,
            budget_tokens=args.budget_tokens,
            session_id=args.session_id,
        )
        _emit(args, result, result["text"])
        return 0
    raise UsageError(f"未知 hook 命令: {args.hook_command}")


def cmd_dream(args: argparse.Namespace) -> int:
    config, vault = _context(args)
    if args.history is not None:
        from zymor.dream.state import list_run_records

        records = list_run_records(vault, limit=args.history)
        payload = {"count": len(records), "runs": records}
        if records:
            human = "\n".join(
                f"- {item['run_id']} ok={item['ok']} status={item['status']} "
                f"finished={item['finished_at']} phases={','.join(item['phases'])}"
                for item in records
            )
        else:
            human = "没有 dream run 记录"
        _emit(args, payload, human)
        return 0
    phases: tuple[str, ...]
    if args.phase == "all":
        all_phases = ["nrem", "arbitration"]
        if bool(config.get("dream.rem.enabled", True)):
            all_phases.append("rem")
        phases = tuple(all_phases)
    else:
        phases = (str(args.phase),)
    if args.watch:
        if args.dry_run:
            raise UsageError("--watch 不能与 --dry-run 同时使用")
        try:
            while True:
                report = run_dream(
                    vault,
                    config,
                    phases=phases,
                    dry_run=False,
                    budget_usd=args.budget_usd,
                )
                _emit(args, report.to_dict(), _dream_lines(report))
                time.sleep(max(1.0, args.interval))
        except KeyboardInterrupt:
            return 0
    report = run_dream(
        vault,
        config,
        phases=phases,
        dry_run=bool(args.dry_run),
        budget_usd=args.budget_usd,
    )
    _emit(args, report.to_dict(), _dream_lines(report))
    return 0 if report.dry_run or report.ok else 1


def cmd_focus(args: argparse.Namespace) -> int:
    _, vault = _context(args)
    session = args.session
    path = vault.focus_path(session)
    updates: dict[str, Any] = {}
    if args.task:
        updates["task"] = args.task
    if args.task_type:
        updates["task_type"] = args.task_type
    if args.status:
        updates["status"] = args.status
    if path.is_file():
        focus = FocusFile.from_markdown(path.read_text(encoding="utf-8"))
        created = False
    else:
        if not updates and not getattr(args, "create", False):
            raise NotFoundError(
                f"焦点文件不存在: {session}",
                hint=f"创建：zymor focus --session {session} --create --task '<任务>'",
            )
        focus = new_focus(
            session,
            vault.info.name,
            str(updates.get("task", "未命名任务")),
            str(updates.get("task_type", "other")),
        )
        created = True
    changed = created
    if updates:
        payload = focus.to_frontmatter()
        payload.update(updates)
        payload["updated_at"] = now_iso()
        payload["body"] = focus.body
        focus = FocusFile.model_validate(payload)
        changed = True
    if changed:
        with vault.writing("focus"):
            atomic_write_text(path, focus.to_markdown())
    output = {
        "session_id": focus.session_id,
        "path": vault.relative(path),
        "task": focus.task,
        "task_type": focus.task_type,
        "status": focus.status,
        "created": created,
        "changed": changed,
    }
    human = f"{'已创建' if created else '焦点文件'}: {vault.relative(path)}"
    if changed:
        _emit(args, output, human)
    else:
        if _flag(args, "json_output"):
            _emit(args, {**output, "body": focus.body}, human)
        else:
            print(focus.to_markdown())
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    config, vault = _context(args)
    from zymor.mcp_server import build_server

    read_only = True if args.read_only else None
    transport = str(args.transport or config.get("mcp.transport") or "stdio")
    if transport == "stdio":
        server = build_server(vault, config, read_only=read_only)
        server.run("stdio")
        return 0
    token_env = str(config.get("mcp.http_token_env") or "")
    token = os.environ.get(token_env, "") if token_env else ""
    if not token:
        raise UsageError(
            "HTTP 传输必须配置 token",
            hint=f"export {token_env or 'ZYMOR_MCP_HTTP_TOKEN'}=<token>；或改用 stdio",
        )
    if args.port:
        config.data["mcp"]["port"] = int(args.port)
    host = str(config.get("mcp.host") or "127.0.0.1")
    port = int(config.get("mcp.port") or 8765)
    server = build_server(vault, config, read_only=read_only, http_token=token)
    print(f"zymor MCP（streamable-http）: http://{host}:{port}", file=sys.stderr)
    server.run("streamable-http", host=host, port=port)
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    command = args.command
    if command == "index":
        return cmd_index(args)
    handlers = {
        "init": cmd_init,
        "write": cmd_write,
        "remember": cmd_remember,
        "export": cmd_export,
        "import": cmd_import,
        "search": cmd_search,
        "semantic": cmd_semantic,
        "rules": cmd_rules,
        "rule-check": cmd_rule_check,
        "read": cmd_read,
        "log": cmd_log,
        "capture": cmd_capture,
        "doctor": cmd_doctor,
        "stats": cmd_stats,
        "focus": cmd_focus,
        "review": cmd_review,
        "promote": cmd_promote,
        "anchor": cmd_anchor,
        "stale": cmd_stale,
        "adopt": cmd_adopt,
        "forget": cmd_forget,
        "answer": cmd_answer,
        "dream": cmd_dream,
        "hook": cmd_hook,
        "panel": cmd_panel,
        "mcp": cmd_mcp,
    }
    handler = handlers.get(command)
    if handler is None:
        raise UsageError(f"未知命令: {command}")
    return handler(args)


def main(argv: list[str] | None = None) -> int:
    """入参：命令行参数（默认取 sys.argv）。出参：进程退出码。CLI 入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except ZymorError as error:
        if _flag(args, "json_output"):
            print(json.dumps(error.to_dict(), ensure_ascii=False))
        else:
            print(f"错误: {error.message}", file=sys.stderr)
            if error.hint:
                print(f"提示: {error.hint}", file=sys.stderr)
        return error.exit_code
    except BrokenPipeError:
        return 1
    except UnicodeError as error:
        if _flag(args, "json_output"):
            print(
                json.dumps(
                    {"error": "invalid_input", "message": str(error), "hint": "使用 UTF-8 文本"},
                    ensure_ascii=False,
                )
            )
        else:
            print(f"错误: {error}", file=sys.stderr)
        return 4
    except OSError as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"错误: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
