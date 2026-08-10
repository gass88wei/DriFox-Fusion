# -*- coding: utf-8 -*-
"""
/refine 提炼闭环（L1，源：prime refinement 工作流）

流程：对话轨迹 + 用户指令 → 补丁生成（LLM 可注入）→ 校验 → 落库 → 快照
回滚：--rollback <refine_id> → 从 refine_history 快照恢复 before 状态

补丁格式（JSON 列表，由 llm provider 返回）：
    {"op": "create", "kind": "memory", "title": "...", "content": "..."}
    {"op": "update", "id": "...", "content": "..."}
    {"op": "delete", "id": "..."}

无 LLM 时使用内置启发式（HeuristicPatcher）：把指令提炼为一条 memory 条目，
保证 /refine 在无外部服务时也能完成冒烟闭环。
"""

import json
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from app.core.harness.entries import (
    KIND_MEMORY,
    VALID_KINDS,
    VALID_SCOPES,
    HarnessEntries,
)
from app.core.store.refine_history_repository import RefineHistoryRepository

# 补丁操作类型
OP_CREATE = "create"
OP_UPDATE = "update"
OP_DELETE = "delete"
VALID_OPS = (OP_CREATE, OP_UPDATE, OP_DELETE)


class RefineValidationError(ValueError):
    """补丁校验失败（不落库、不写快照）"""


def validate_patch(patch: Dict) -> Dict:
    """校验单条补丁，返回规范化补丁（非法则抛 RefineValidationError）"""
    if not isinstance(patch, dict):
        raise RefineValidationError(f"patch 必须是 dict，got {type(patch).__name__}")

    op = patch.get("op")
    if op not in VALID_OPS:
        raise RefineValidationError(f"未知操作 {op!r}，期望 {VALID_OPS}")

    if op == OP_CREATE:
        kind = patch.get("kind")
        if kind not in VALID_KINDS:
            raise RefineValidationError(f"create: 非法 kind {kind!r}，期望 {VALID_KINDS}")
        title = (patch.get("title") or "").strip()
        content = (patch.get("content") or "").strip()
        if not title:
            raise RefineValidationError("create: title 不能为空")
        if not content:
            raise RefineValidationError("create: content 不能为空")
        scope = patch.get("scope") or ""
        if scope and scope not in VALID_SCOPES:
            raise RefineValidationError(f"create: 非法 scope {scope!r}，期望 {VALID_SCOPES}")
        return {
            "op": OP_CREATE,
            "kind": kind,
            "title": title,
            "content": content,
            "scope": scope,  # 空=未指定，由 refine 层继承调用级 scope
            "source": patch.get("source", "refine"),
            "id": patch.get("id") or "",
        }

    if op == OP_UPDATE:
        entry_id = (patch.get("id") or "").strip()
        if not entry_id:
            raise RefineValidationError("update: id 不能为空")
        fields = {}
        if patch.get("content") is not None:
            fields["content"] = (patch.get("content") or "").strip()
        if patch.get("title") is not None:
            fields["title"] = (patch.get("title") or "").strip()
        if patch.get("kind") is not None:
            if patch["kind"] not in VALID_KINDS:
                raise RefineValidationError(f"update: 非法 kind {patch['kind']!r}")
            fields["kind"] = patch["kind"]
        if patch.get("scope") is not None:
            if patch["scope"] not in VALID_SCOPES:
                raise RefineValidationError(f"update: 非法 scope {patch['scope']!r}")
            fields["scope"] = patch["scope"]
        if not fields:
            raise RefineValidationError("update: 至少提供一个待更新字段")
        return {"op": OP_UPDATE, "id": entry_id, **fields}

    # delete
    entry_id = (patch.get("id") or "").strip()
    if not entry_id:
        raise RefineValidationError("delete: id 不能为空")
    return {"op": OP_DELETE, "id": entry_id}


class HeuristicPatcher:
    """内置启发式补丁器：无 LLM 时把指令提炼为 memory 条目

    规则：指令首行作为标题（≤40 字符），全文作为内容；若存在同标题条目则 update。
    """

    def __call__(self, trajectory: List[Dict], instructions: str) -> List[Dict]:
        text = (instructions or "").strip()
        if not text:
            raise RefineValidationError("无 LLM 且指令为空，无法提炼条目")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = lines[0][:40]
        content = "\n".join(lines)
        return [{"op": OP_CREATE, "kind": KIND_MEMORY, "title": title, "content": content}]


class RefineEngine:
    """refine 提炼引擎：轨迹→补丁→校验→落库→快照→回滚"""

    def __init__(
        self,
        entries: HarnessEntries,
        history: RefineHistoryRepository,
        llm: Optional[Callable[[List[Dict], str], List[Dict]]] = None,
    ):
        """
        Args:
            entries: 条目 CRUD 门面
            history: refine 快照仓储
            llm: 可注入补丁生成器 callable(trajectory, instructions) -> List[patch]；
                为 None 时使用 HeuristicPatcher
        """
        self._entries = entries
        self._history = history
        self._llm = llm or HeuristicPatcher()

    # ==================== 提炼主流程 ====================

    def refine(
        self,
        trajectory: List[Dict],
        instructions: str = "",
        scope: str = "local",
        refine_id: str = "",
    ) -> Dict:
        """执行一次提炼：轨迹 + 指令 → 补丁 → 校验 → 落库 → 快照

        Args:
            trajectory: 对话轨迹 [{"role": "user|assistant", "content": "..."}]
            instructions: 用户提炼指令
            scope: 条目作用域（local/global）
            refine_id: 显式指定 refine_id（幂等重试复用）；空则自动生成

        Returns:
            {
                "refine_id": str,
                "patches": [...],      # 规范化后的补丁
                "changes": [...],      # 快照变更 [{entry_id, kind, before, after}]
                "created": [...], "updated": [...], "deleted": [...],  # 条目 id
            }

        Raises:
            RefineValidationError: 补丁校验失败（未落库）
        """
        if scope not in VALID_SCOPES:
            raise RefineValidationError(f"非法 scope {scope!r}，期望 {VALID_SCOPES}")

        # 1. 生成补丁
        raw_patches = self._llm(trajectory, instructions or "")
        if isinstance(raw_patches, str):
            try:
                raw_patches = json.loads(raw_patches)
            except ValueError as e:
                raise RefineValidationError(f"LLM 返回非法 JSON: {e}") from e
        if not isinstance(raw_patches, list):
            raise RefineValidationError("补丁必须是 JSON 列表")

        # 2. 校验（全量通过才允许落库）
        patches = [validate_patch(p) for p in raw_patches]
        if not patches:
            raise RefineValidationError("补丁列表为空，未执行任何变更")

        # 2.5 create 补丁未显式指定 scope 时，继承 refine 调用级 scope
        #     （/refine --global 应作用于本次全部新建条目）
        for patch in patches:
            if patch["op"] == OP_CREATE and not patch.get("scope"):
                patch["scope"] = scope

        # 3. 逐条应用并收集 before/after 快照
        changes: List[Dict] = []
        created, updated, deleted = [], [], []
        for patch in patches:
            entry_id = patch.get("id") or ""
            if patch["op"] == OP_CREATE:
                before = None  # 新增：应用前不存在
                if not entry_id:
                    entry_id = self._entries.save(
                        patch["kind"], patch["title"], patch["content"],
                        scope=patch["scope"], source=patch.get("source", "refine"),
                    )
                else:
                    entry_id = self._entries.save(
                        patch["kind"], patch["title"], patch["content"],
                        scope=patch["scope"], source=patch.get("source", "refine"),
                        entry_id=entry_id,
                    )
                after = self._entries.get(entry_id) if entry_id else None
                created.append(entry_id)
            elif patch["op"] == OP_UPDATE:
                before = self._entries.get(entry_id)
                if not before:
                    raise RefineValidationError(f"update: 条目 {entry_id!r} 不存在")
                ok = self._entries.update(
                    entry_id,
                    content=patch.get("content"),
                    title=patch.get("title"),
                    kind=patch.get("kind"),
                    scope=patch.get("scope"),
                )
                if not ok:
                    raise RefineValidationError(f"update: 条目 {entry_id!r} 落库失败")
                after = self._entries.get(entry_id)
                updated.append(entry_id)
            else:  # delete
                before = self._entries.get(entry_id)
                if not before:
                    raise RefineValidationError(f"delete: 条目 {entry_id!r} 不存在")
                if not self._entries.delete(entry_id):
                    raise RefineValidationError(f"delete: 条目 {entry_id!r} 删除失败")
                after = None
                deleted.append(entry_id)

            changes.append({
                "entry_id": entry_id,
                "kind": patch.get("kind") or (before or {}).get("kind", "memory"),
                "before": before,
                "after": after,
            })

        # 4. 写快照（changes 为 JSON 可序列化 dict）
        rid = self._history.save_refinement(
            changes,
            trigger=instructions,
            scope=scope,
            refine_id=refine_id,
        )
        if not rid:
            raise RefineValidationError("refine 快照写入失败（已应用变更但未落快照）")

        logger.info(f"[RefineEngine] refine 完成 {rid}: +{len(created)} ~{len(updated)} -{len(deleted)}")
        return {
            "refine_id": rid,
            "patches": patches,
            "changes": changes,
            "created": created,
            "updated": updated,
            "deleted": deleted,
        }

    # ==================== 回滚 ====================

    def rollback(self, refine_id: str) -> Dict:
        """回滚一次 refine：按快照恢复 before 状态

        before 存在 → 恢复条目（不存在则重建；存在则更新回原值）
        before 为空（原本不存在）→ 删除本次新增的条目

        Returns:
            {"refine_id": str, "restored": [...], "removed": [...]}

        Raises:
            RefineValidationError: 快照不存在或恢复失败
        """
        snap = self._history.get(refine_id)
        if not snap:
            raise RefineValidationError(f"refine 快照 {refine_id!r} 不存在")

        restored, removed = [], []
        for change in snap.get("changes", []):
            entry_id = change.get("entry_id", "")
            before = change.get("before")
            if before:
                # 恢复条目到 before 状态
                existing = self._entries.get(entry_id)
                if not existing:
                    # 原条目已被删 → 重建（保留原 id/kind/scope/version）
                    self._entries.repo.save({
                        "id": entry_id,
                        "kind": before.get("kind", KIND_MEMORY),
                        "title": before.get("title", ""),
                        "content": before.get("content", ""),
                        "scope": before.get("scope", "local"),
                        "source": before.get("source", "manual"),
                        "enabled": True,
                        "confidence": 0.9,
                        "version": before.get("version", 1),
                    })
                else:
                    self._entries.update(
                        entry_id,
                        content=before.get("content"),
                        title=before.get("title"),
                        kind=before.get("kind"),
                        scope=before.get("scope"),
                    )
                restored.append(entry_id)
            elif before is None and change.get("after"):
                # 原本不存在（本次新增）→ 删除
                if self._entries.delete(entry_id):
                    removed.append(entry_id)

        logger.info(f"[RefineEngine] 回滚完成 {refine_id}: restore={len(restored)} remove={len(removed)}")
        return {"refine_id": refine_id, "restored": restored, "removed": removed}

    # ==================== 历史查看 ====================

    def list_history(self, limit: int = 20, scope: str = "") -> List[Dict]:
        """列出最近 refine 快照（含简要变更数）"""
        snaps = self._history.list(limit=limit, scope=scope or None)
        result = []
        for s in snaps:
            result.append({
                "refine_id": s.get("id", ""),
                "trigger": s.get("trigger", ""),
                "scope": s.get("scope", "local"),
                "created_at": s.get("created_at", ""),
                "change_count": len(s.get("changes", [])),
            })
        return result


def build_engine(
    repo: Any = None,
    llm: Optional[Callable[[List[Dict], str], List[Dict]]] = None,
    scope: str = "local",
) -> RefineEngine:
    """便捷构造：默认复用全局 MemoryManager 的仓储实例

    无 db 可用时抛出 RuntimeError（调用方需保证存储已初始化）。
    """
    from app.core.memory_manager import MemoryManagerCore

    mm = MemoryManagerCore.get_instance()
    mem_repo = getattr(mm, "entry_memories", None)
    db = getattr(mm, "_db_manager", None)
    if mem_repo is None or db is None:
        raise RuntimeError("MemoryManager 存储未初始化，无法构建 RefineEngine")

    entries = HarnessEntries(mem_repo)
    history = RefineHistoryRepository(db)
    return RefineEngine(entries, history, llm=llm)