# -*- coding: utf-8 -*-
"""
Harness 条目 CRUD（L1）

基于已有 store 层（MemoryRepository + entry_memories 表）的 4 类条目门面：
    kind: prompt | memory | skill | subagent
    scope: local | global（local=当前项目，global=跨项目）

设计（docs/1.md §4 决策）：复用 MemoryRepository 机制与 db_manager 连接，
不新建平行存储；本模块负责条目语义（title/slug/版本/过滤），落库走既有仓储。
"""

import re
from typing import Dict, List, Optional

from app.core.store.memory_repository import MemoryRepository

# 4 类条目类型（与 prime harness 对齐）
KIND_PROMPT = MemoryRepository.KIND_PROMPT
KIND_MEMORY = MemoryRepository.KIND_MEMORY
KIND_SKILL = MemoryRepository.KIND_SKILL
KIND_SUBAGENT = MemoryRepository.KIND_SUBAGENT
VALID_KINDS = MemoryRepository.VALID_KINDS

SCOPE_LOCAL = MemoryRepository.SCOPE_LOCAL
SCOPE_GLOBAL = MemoryRepository.SCOPE_GLOBAL
VALID_SCOPES = MemoryRepository.VALID_SCOPES


def slugify(title: str, fallback: str = "entry") -> str:
    """由标题生成稳定 slug（用作条目 id；同标题幂等更新）

    规则：小写 + 非字母数字转下划线 + 压缩连续下划线，截断 64 字符。
    """
    raw = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "_", (title or "").strip().lower())
    raw = re.sub(r"_+", "_", raw).strip("_")
    return (raw or fallback)[:64]


class HarnessEntries:
    """4 类条目的 CRUD 门面（prompt/memory/skill/subagent × local/global）"""

    def __init__(self, repo: MemoryRepository):
        """
        Args:
            repo: 已初始化的 MemoryRepository（与 db_manager 共享连接）
        """
        self._repo = repo

    @property
    def repo(self) -> MemoryRepository:
        """底层仓储（refine 流程需要访问原始行）"""
        return self._repo

    def _to_entry(self, row: Dict) -> Dict:
        """规范化条目输出（与 prime HarnessEntry 字段对齐）"""
        return {
            "id": row.get("id", ""),
            "kind": row.get("kind", KIND_MEMORY),
            "title": row.get("title", ""),
            "content": row.get("content", ""),
            "source": row.get("source", "agent"),
            "scope": row.get("scope", SCOPE_LOCAL),
            "version": row.get("version", 1),
            "created_at": row.get("created_at", ""),
            "updated_at": row.get("updated_at", ""),
        }

    def save(
        self,
        kind: str,
        title: str,
        content: str,
        scope: str = SCOPE_LOCAL,
        source: str = "refine",
        entry_id: str = "",
    ) -> Optional[str]:
        """创建或更新条目（同标题幂等：覆盖旧条目，version+1）

        Args:
            kind: prompt/memory/skill/subagent
            title: 条目标题（非空）
            content: 条目内容（非空）
            scope: local/global
            source: 来源标注（manual/refine/agent）
            entry_id: 显式指定 id（默认按标题生成 slug）

        Returns:
            条目 id（str）或 None（保存失败）
        """
        if kind not in VALID_KINDS:
            raise ValueError(f"unknown harness kind {kind!r}; expected one of {VALID_KINDS}")
        title = (title or "").strip()
        content = (content or "").strip()
        if not title:
            raise ValueError("harness entry title must be non-empty")
        if not content:
            raise ValueError("harness entry content must be non-empty")
        if scope not in VALID_SCOPES:
            raise ValueError(f"unknown harness scope {scope!r}; expected one of {VALID_SCOPES}")

        entry_id = entry_id or slugify(title, kind)
        existing = self.get(entry_id)
        if existing:
            # 已有 → 更新（version+1 由仓储自动处理）；保留原 kind/scope 除非显式覆盖
            ok = self._repo.update_entry(
                entry_id,
                content=content,
                title=title,
                kind=kind if kind != existing.get("kind") else None,
                scope=scope if scope != existing.get("scope") else None,
            )
            return entry_id if ok else None

        ok = self._repo.save(
            {
                "id": entry_id,
                "kind": kind,
                "title": title,
                "content": content,
                "scope": scope,
                "source": source,
                "enabled": True,
                "confidence": 0.9,
                "version": 1,
            }
        )
        return entry_id if ok else None

    def get(self, entry_id: str) -> Optional[Dict]:
        """按 id 读取条目"""
        row = self._repo.get(entry_id)
        if not row:
            return None
        entry = self._to_entry(row)
        return entry if entry.get("id") else None

    def update(
        self,
        entry_id: str,
        content: Optional[str] = None,
        title: Optional[str] = None,
        kind: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> bool:
        """按 id 更新指定字段（省略字段保持不变，version+1）"""
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError(f"unknown harness kind {kind!r}; expected one of {VALID_KINDS}")
        if scope is not None and scope not in VALID_SCOPES:
            raise ValueError(f"unknown harness scope {scope!r}; expected one of {VALID_SCOPES}")
        if title is not None and not (title or "").strip():
            raise ValueError("harness entry title must be non-empty")
        if content is not None and not (content or "").strip():
            raise ValueError("harness entry content must be non-empty")
        return self._repo.update_entry(
            entry_id,
            content=content,
            title=title,
            kind=kind,
            scope=scope,
        )

    def delete(self, entry_id: str) -> bool:
        """删除条目"""
        return self._repo.delete(entry_id)

    def list(
        self,
        kind: Optional[str] = None,
        scope: Optional[str] = None,
        limit: int = 9999,
    ) -> List[Dict]:
        """列出条目，支持 kind/scope 过滤（按更新时间新→旧）"""
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError(f"unknown harness kind {kind!r}; expected one of {VALID_KINDS}")
        if scope is not None and scope not in VALID_SCOPES:
            raise ValueError(f"unknown harness scope {scope!r}; expected one of {VALID_SCOPES}")
        rows = self._repo.search(limit=limit, kind=kind, scope=scope)
        return [self._to_entry(r) for r in rows]

    def search(
        self,
        query: str = "",
        kind: Optional[str] = None,
        scope: Optional[str] = None,
        limit: int = 9999,
    ) -> List[Dict]:
        """按内容关键词搜索条目（支持 kind/scope 过滤）"""
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError(f"unknown harness kind {kind!r}; expected one of {VALID_KINDS}")
        rows = self._repo.search(query, limit=limit, kind=kind, scope=scope)
        return [self._to_entry(r) for r in rows]

    def count(self, kind: Optional[str] = None, scope: Optional[str] = None) -> int:
        """统计条目数（支持过滤）"""
        rows = self._repo.search(limit=100000, kind=kind, scope=scope)
        return len(rows)

    def clear(self) -> bool:
        """清空全部条目"""
        return self._repo.clear_all()