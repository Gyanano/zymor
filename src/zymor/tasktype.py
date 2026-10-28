from __future__ import annotations

KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("debug", "trace", "报错", "异常", "复现"), "debugging"),
    (("设计", "架构", "方案", "取舍"), "architecture"),
    (("调研", "对比", "选型"), "research"),
    (("重构", "迁移"), "refactor"),
    (("文档", "写作", "readme"), "writing"),
)


def task_type_from_text(text: str) -> str:
    """入参：任务描述或用户消息。出参：task_type。按关键词表顺序取第一个命中，无命中为 other。"""
    lowered = text.lower()
    for keywords, label in KEYWORDS:
        for keyword in keywords:
            if keyword.lower() in lowered:
                return label
    return "other"
