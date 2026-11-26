from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zymor.errors import NotFoundError, UsageError, UserError, ZymorError
from zymor.knowledge import KnowledgeStore
from zymor.knowledge_import import distill_shared_statement, import_materials
from zymor.knowledge_semantic import propose_semantic
from zymor.knowledge_view import export_markdown, propose_article_edit


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", metavar="PATH", type=Path, required=True, help="独立试点数据库")
    sub = parser.add_subparsers(dest="knowledge_action", required=True)
    sub.add_parser("init", help="初始化独立试点库")

    capture = sub.add_parser("capture", help="归档一份 UTF-8 对话原文")
    capture.add_argument("file", type=Path)
    capture.add_argument("--key", required=True, help="对话稳定标识")

    batch = sub.add_parser("import", help="批量归档 ChatGPT 导出与 Markdown")
    batch.add_argument("paths", nargs="+", type=Path)

    search = sub.add_parser("search-sources", help="搜索批量导入的原文")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)

    source = sub.add_parser("source", help="按照来源编号读取归档原文")
    source.add_argument("source_id", type=int)

    distill = sub.add_parser("distill", help="把多份原文中的共同结论提交审核")
    distill.add_argument("item_id")
    distill.add_argument("query", help="目标结论中的一段具体原文")
    distill.add_argument("--title", help="创建条目时指定标题")
    distill.add_argument("--base-revision", type=int, default=0)

    semantic = sub.add_parser("distill-semantic", help="试点语义归并和冲突识别")
    semantic.add_argument("item_prefix")
    semantic.add_argument("query", help="用于搜集同一主题材料的原文关键词")

    propose = sub.add_parser("propose", help="从原文提交待审知识")
    propose.add_argument("item_id")
    propose.add_argument("--title", help="创建条目时指定标题")
    propose.add_argument("--statement", required=True)
    propose.add_argument("--source", type=int, required=True)
    propose.add_argument("--evidence", required=True, help="来源中的精确摘录")
    propose.add_argument("--base-revision", type=int, default=0)

    article = sub.add_parser("propose-article", help="从文章编辑稿提交待审更正")
    article.add_argument("file", type=Path)
    article.add_argument("--statement", required=True, help="从编辑稿正文选中的新结论")

    review = sub.add_parser("review", help="查看提案和来源")
    review.add_argument("proposal_id", type=int)

    approve = sub.add_parser("approve", help="由所有者核准一个提案")
    approve.add_argument("proposal_id", type=int)
    approve.add_argument("--markdown-dir", type=Path, help="核准后更新目录中的 Markdown")

    show = sub.add_parser("show", help="查看当前核准版本")
    show.add_argument("item_id")
    show.add_argument("--evidence", action="store_true", help="列出该版本引用的来源")

    export = sub.add_parser("export", help="重新生成当前核准版本的 Markdown")
    export.add_argument("item_id")
    export.add_argument("directory", type=Path)

    sub.add_parser("mcp", help="启动本地只读 MCP")


def run_knowledge(args: argparse.Namespace) -> int:
    store = KnowledgeStore(args.db)
    try:
        result = _run(store, args)
    except sqlite3.Error as error:
        raise UserError(f"知识试点数据库操作失败：{error}") from error
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _run(store: KnowledgeStore, args: argparse.Namespace) -> dict[str, Any] | None:
    match args.knowledge_action:
        case "init":
            store.initialize()
            return {"database": str(store.database)}
        case "capture":
            source = store.capture("dialogue", args.key, args.file.read_text(encoding="utf-8-sig"))
            return {"source_id": source}
        case "import":
            return import_materials(store, args.paths)
        case "search-sources":
            return {"hits": store.search_sources(args.query, limit=args.limit)}
        case "source":
            return store.read_source(args.source_id)
        case "distill":
            proposal = distill_shared_statement(
                store, args.item_id, args.query, title=args.title, base_revision=args.base_revision
            )
            return {"proposal_id": proposal, "status": "pending"}
        case "propose":
            proposal = store.propose(
                args.item_id,
                title=args.title,
                statement=args.statement,
                source_id=args.source,
                evidence=args.evidence,
                base_revision=args.base_revision,
            )
            return {"proposal_id": proposal, "status": "pending"}
        case "distill-semantic":
            return {"proposals": propose_semantic(store, args.item_prefix, args.query)}
        case "propose-article":
            return {
                "proposal_id": propose_article_edit(store, args.file, args.statement),
                "status": "pending",
            }
        case "review":
            return store.review(args.proposal_id)
        case "approve":
            approved = store.approve(args.proposal_id)
            result = asdict(approved)
            if args.markdown_dir is not None:
                try:
                    publication = export_markdown(store, approved.item_id, args.markdown_dir)
                except (OSError, UnicodeError, sqlite3.Error, ZymorError) as error:
                    raise UserError(
                        f"已核准版本 {approved.revision}，Markdown 更新失败：{error}",
                        hint="排除文件问题后用 knowledge export 重试",
                    ) from error
                result["markdown"] = str(publication.path)
                result["markdown_revision"] = publication.revision
            return result
        case "show":
            revision = store.read(args.item_id)
            if revision is None:
                raise NotFoundError(f"找不到已核准条目 {args.item_id}")
            payload = asdict(revision)
            if args.evidence:
                payload["evidence_refs"] = store.revision_evidence(
                    revision.item_id, revision.revision
                )
            return payload
        case "export":
            publication = export_markdown(store, args.item_id, args.directory)
            return {"markdown": str(publication.path), "markdown_revision": publication.revision}
        case "mcp":
            _serve_mcp(store)
            return None
        case _:
            raise UsageError(f"未知知识试点命令：{args.knowledge_action}")


def _serve_mcp(store: KnowledgeStore) -> None:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    server = MCPServer(name="zymor-knowledge", description="单人知识试点的当前核准版本")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def knowledge_get(item_id: str) -> dict[str, Any]:
        """Look up the current approved knowledge item by its stable item_id."""
        try:
            revision = store.read(item_id)
        except ZymorError as error:
            raise ToolError(error.message) from None
        except sqlite3.Error:
            raise ToolError("当前核准版本暂不可用") from None
        if revision is None:
            return {"found": False, "item_id": item_id}
        return {"found": True, **asdict(revision)}

    server.run("stdio")
