# -*- coding: utf-8 -*-
"""
Harness 记忆包（L1）

在既有 store 层（entry_memories 表 + refine_history 快照表）之上提供：
- entries.py   ：4 类条目（prompt/memory/skill/subagent）CRUD 门面
- refine.py    ：/refine 提炼闭环（轨迹→补丁→校验→落库→快照→回滚）
- formatter.py ：上下文注入格式化

设计约束（docs/1.md §4）：复用 MemoryRepository / RefineHistoryRepository，
不新建平行存储、不引入独立 DB 连接。
"""

from app.core.harness.entries import (
    KIND_MEMORY,
    KIND_PROMPT,
    KIND_SKILL,
    KIND_SUBAGENT,
    SCOPE_GLOBAL,
    SCOPE_LOCAL,
    VALID_KINDS,
    VALID_SCOPES,
    HarnessEntries,
    slugify,
)
from app.core.harness.formatter import (
    format_entries_for_prompt,
    format_entry_for_summary,
)
from app.core.harness.refine import (
    OP_CREATE,
    OP_DELETE,
    OP_UPDATE,
    RefineEngine,
    RefineValidationError,
    build_engine,
)

__all__ = [
    # entries
    "HarnessEntries",
    "slugify",
    "KIND_PROMPT",
    "KIND_MEMORY",
    "KIND_SKILL",
    "KIND_SUBAGENT",
    "VALID_KINDS",
    "SCOPE_LOCAL",
    "SCOPE_GLOBAL",
    "VALID_SCOPES",
    # refine
    "RefineEngine",
    "RefineValidationError",
    "build_engine",
    "OP_CREATE",
    "OP_UPDATE",
    "OP_DELETE",
    # formatter
    "format_entries_for_prompt",
    "format_entry_for_summary",
]