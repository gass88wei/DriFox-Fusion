# -*- coding: utf-8 -*-
"""
Harness 注入格式化（L1）

将 4 类条目（prompt/memory/skill/subagent）格式化为 LLM 上下文注入文本。
供 format_memory_context hook 使用，与「条目记忆」段并存、不冲突。
"""

from typing import Dict, List

from app.core.harness.entries import (
    KIND_MEMORY,
    KIND_PROMPT,
    KIND_SKILL,
    KIND_SUBAGENT,
    VALID_KINDS,
)

_KIND_LABELS = {
    KIND_PROMPT: "提示词",
    KIND_MEMORY: "记忆",
    KIND_SKILL: "技能",
    KIND_SUBAGENT: "子智能体",
}

_MAX_CONTENT_PREVIEW = 160


def _format_entry_line(entry: Dict, idx: int) -> str:
    """格式化单条条目为一行（含 scope 标注）"""
    kind = entry.get("kind", KIND_MEMORY)
    title = entry.get("title", "")
    content = (entry.get("content", "") or "").strip().replace("\n", " ")
    if len(content) > _MAX_CONTENT_PREVIEW:
        content = content[:_MAX_CONTENT_PREVIEW] + "…"
    scope_mark = " 🌐" if entry.get("scope") == "global" else ""
    version = entry.get("version", 1)
    return f"{idx}. [{kind}{scope_mark}] **{title}** (v{version}): {content}"


def format_entries_for_prompt(entries: List[Dict]) -> str:
    """将条目列表格式化为注入文本（空列表返回空串，不产生占位噪音）"""
    if not entries:
        return ""

    # 按 kind 分组（保持稳定顺序，与 VALID_KINDS 一致）
    grouped: Dict[str, List[Dict]] = {k: [] for k in VALID_KINDS}
    for entry in entries:
        kind = entry.get("kind", KIND_MEMORY)
        if kind in grouped:
            grouped[kind].append(entry)

    lines = ["### Harness 记忆"]
    total = 0
    for kind in VALID_KINDS:
        items = grouped[kind]
        if not items:
            continue
        lines.append("")
        lines.append(f"#### {_KIND_LABELS[kind]}（{len(items)}）")
        for idx, entry in enumerate(items, 1):
            total += 1
            lines.append(_format_entry_line(entry, idx))
    return "\n".join(lines)


def format_entry_for_summary(entry: Dict) -> str:
    """格式化单条条目（用于 /refine 的结果展示/日志）"""
    kind = entry.get("kind", KIND_MEMORY)
    title = entry.get("title", "")
    content = (entry.get("content", "") or "").strip().replace("\n", " ")
    if len(content) > _MAX_CONTENT_PREVIEW:
        content = content[:_MAX_CONTENT_PREVIEW] + "…"
    scope = entry.get("scope", "local")
    return f"[{kind}/{scope}] {title}: {content}"