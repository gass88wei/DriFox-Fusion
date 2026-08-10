# -*- coding: utf-8 -*-
"""
/refine 提炼闭环测试（L1）

覆盖 refine 全链路：
- 补丁校验（合法/非法/空/JSON 解析）
- 启发式补丁（无 LLM）
- LLM 补丁注入（create/update/delete/scope 继承）
- 落库 + 快照（before/after JSON）
- 回滚（restore 恢复/removed 删除新增）
- 历史查看
"""

import json
import sqlite3

import pytest

from app.core.harness.entries import (
    KIND_MEMORY,
    KIND_SKILL,
    SCOPE_GLOBAL,
    SCOPE_LOCAL,
    HarnessEntries,
)
from app.core.harness.refine import (
    OP_CREATE,
    OP_DELETE,
    OP_UPDATE,
    HeuristicPatcher,
    RefineEngine,
    RefineValidationError,
    validate_patch,
)
from app.core.store.memory_repository import MemoryRepository
from app.core.store.refine_history_repository import RefineHistoryRepository


class FakeDb:
    def __init__(self, conn):
        self._conn = conn
        self.is_connected = True

    def execute_sql(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(sql, params)
        if sql.strip().upper().startswith(("SELECT", "PRAGMA")):
            return True, cur.fetchall()
        self._conn.commit()
        return True, cur.rowcount


@pytest.fixture
def engine():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = FakeDb(conn)
    entries = HarnessEntries(MemoryRepository(db))
    history = RefineHistoryRepository(db)
    yield RefineEngine(entries, history)
    conn.close()


class TestValidatePatch:
    """补丁校验"""

    def test_valid_create(self):
        p = validate_patch({"op": "create", "kind": "memory", "title": "T", "content": "C"})
        assert p["op"] == "create"
        assert p["kind"] == "memory"
        assert p["title"] == "T"

    def test_valid_update(self):
        p = validate_patch({"op": "update", "id": "e1", "content": "新"})
        assert p["op"] == "update" and p["id"] == "e1" and p["content"] == "新"

    def test_valid_delete(self):
        p = validate_patch({"op": "delete", "id": "e1"})
        assert p["op"] == "delete" and p["id"] == "e1"

    def test_invalid_op(self):
        with pytest.raises(RefineValidationError):
            validate_patch({"op": "fly", "kind": "memory", "title": "T", "content": "C"})

    def test_create_missing_title(self):
        with pytest.raises(RefineValidationError):
            validate_patch({"op": "create", "kind": "memory", "title": "", "content": "C"})

    def test_create_missing_content(self):
        with pytest.raises(RefineValidationError):
            validate_patch({"op": "create", "kind": "memory", "title": "T", "content": ""})

    def test_create_invalid_kind(self):
        with pytest.raises(RefineValidationError):
            validate_patch({"op": "create", "kind": "bogus", "title": "T", "content": "C"})

    def test_create_invalid_scope(self):
        with pytest.raises(RefineValidationError):
            validate_patch({"op": "create", "kind": "memory", "title": "T", "content": "C", "scope": "bogus"})

    def test_update_missing_id(self):
        with pytest.raises(RefineValidationError):
            validate_patch({"op": "update", "content": "x"})

    def test_update_no_fields(self):
        with pytest.raises(RefineValidationError):
            validate_patch({"op": "update", "id": "e1"})

    def test_delete_missing_id(self):
        with pytest.raises(RefineValidationError):
            validate_patch({"op": "delete"})

    def test_not_dict(self):
        with pytest.raises(RefineValidationError):
            validate_patch("create")

    def test_create_scope_empty_allowed(self):
        """scope 未指定（空串）→ 通过校验（由 refine 层继承调用级 scope）"""
        p = validate_patch({"op": "create", "kind": "skill", "title": "T", "content": "C"})
        assert p["scope"] == ""


class TestHeuristicPatcher:
    """无 LLM 时的内置启发式"""

    def test_basic(self):
        patches = HeuristicPatcher()([], "用 git commit -m 提交代码")
        assert len(patches) == 1
        assert patches[0]["op"] == "create"
        assert patches[0]["kind"] == "memory"
        assert "git" in patches[0]["content"]

    def test_title_from_first_line(self):
        patches = HeuristicPatcher()([], "第一行标题\n第二行内容")
        assert patches[0]["title"] == "第一行标题"
        assert patches[0]["content"] == "第一行标题\n第二行内容"

    def test_empty_instructions_rejected(self):
        with pytest.raises(RefineValidationError):
            HeuristicPatcher()([], "")


class TestRefineChain:
    """提炼主流程"""

    def test_heuristic_refine_creates_entry(self, engine):
        trajectory = [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "答案"}]
        result = engine.refine(trajectory, "新知识：X", scope="local")
        assert result["refine_id"].startswith("refine_")
        assert len(result["created"]) == 1
        eid = result["created"][0]
        entry = engine._entries.get(eid)
        assert entry["content"] == "新知识：X"
        assert entry["scope"] == "local"
        assert entry["source"] == "refine"

    def test_refine_records_snapshot(self, engine):
        result = engine.refine([], "知识A")
        snap = engine._history.get(result["refine_id"])
        assert snap["trigger"] == "知识A"
        assert len(snap["changes"]) == 1
        change = snap["changes"][0]
        assert change["before"] is None  # 新增
        assert change["after"]["content"] == "知识A"

    def test_llm_create_inherits_call_scope(self, engine):
        """LLM 未指定 scope → 继承 refine 调用级 scope（--global 语义）"""
        engine._llm = lambda traj, ins: [{"op": "create", "kind": "memory", "title": "G", "content": "全局"}]
        result = engine.refine([], "全局条目", scope="global")
        eid = result["created"][0]
        assert engine._entries.get(eid)["scope"] == "global"

    def test_llm_create_explicit_scope_wins(self, engine):
        """LLM 显式指定 scope → 优先于调用级"""
        engine._llm = lambda traj, ins: [{"op": "create", "kind": "skill", "title": "S", "content": "技能", "scope": "global"}]
        result = engine.refine([], "指令", scope="local")
        assert engine._entries.get(result["created"][0])["scope"] == "global"

    def test_llm_update_existing(self, engine):
        eid = engine._entries.save(KIND_MEMORY, "目标", "原内容")
        engine._llm = lambda traj, ins: [{"op": "update", "id": eid, "content": "新内容"}]
        result = engine.refine([], "更新")
        assert eid in result["updated"]
        assert engine._entries.get(eid)["content"] == "新内容"
        assert engine._entries.get(eid)["version"] == 2

    def test_llm_update_missing_entry(self, engine):
        engine._llm = lambda traj, ins: [{"op": "update", "id": "不存在", "content": "x"}]
        with pytest.raises(RefineValidationError):
            engine.refine([], "更新失败")

    def test_llm_delete(self, engine):
        eid = engine._entries.save(KIND_MEMORY, "待删", "内容")
        engine._llm = lambda traj, ins: [{"op": "delete", "id": eid}]
        result = engine.refine([], "删除")
        assert eid in result["deleted"]
        assert engine._entries.get(eid) is None

    def test_llm_delete_missing_entry(self, engine):
        engine._llm = lambda traj, ins: [{"op": "delete", "id": "不存在"}]
        with pytest.raises(RefineValidationError):
            engine.refine([], "删除失败")

    def test_mixed_patches_applied(self, engine):
        keep_id = engine._entries.save(KIND_MEMORY, "保留", "原")
        engine._llm = lambda traj, ins: [
            {"op": "create", "kind": "skill", "title": "新技能", "content": "技能内容"},
            {"op": "update", "id": keep_id, "content": "更新"},
            {"op": "delete", "id": "删除"},
        ]
        with pytest.raises(RefineValidationError):
            engine.refine([], "混合")  # delete 不存在 → 整体拒绝（校验先行）
        # 校验失败不落库：无新增、无更新
        assert engine._entries.search("新技能") == []
        assert engine._entries.get(keep_id)["content"] == "原"

    def test_llm_json_string_patch(self, engine):
        """LLM 返回 JSON 字符串 → 解析后应用"""
        payload = json.dumps([{"op": "create", "kind": "memory", "title": "J", "content": "JSON条目"}])
        engine._llm = lambda traj, ins: payload
        result = engine.refine([], "json")
        assert len(result["created"]) == 1

    def test_llm_invalid_json_rejected(self, engine):
        engine._llm = lambda traj, ins: "{not json"
        with pytest.raises(RefineValidationError):
            engine.refine([], "坏json")

    def test_empty_patches_rejected(self, engine):
        engine._llm = lambda traj, ins: []
        with pytest.raises(RefineValidationError):
            engine.refine([], "空补丁")

    def test_invalid_scope_rejected(self, engine):
        with pytest.raises(RefineValidationError):
            engine.refine([], "越界", scope="bogus")

    def test_explicit_refine_id(self, engine):
        result = engine.refine([], "固定id", refine_id="refine_fixed")
        assert result["refine_id"] == "refine_fixed"
        assert engine._history.get("refine_fixed") is not None


class TestRollback:
    """快照回滚"""

    def test_rollback_create_removes_new_entry(self, engine):
        result = engine.refine([], "临时条目")
        eid = result["created"][0]
        assert engine._entries.get(eid) is not None
        rb = engine.rollback(result["refine_id"])
        assert eid in rb["removed"]
        assert engine._entries.get(eid) is None

    def test_rollback_update_restores_before(self, engine):
        eid = engine._entries.save(KIND_MEMORY, "目标", "原始内容")
        engine._llm = lambda traj, ins: [{"op": "update", "id": eid, "content": "被改坏"}]
        result = engine.refine([], "更新")
        assert engine._entries.get(eid)["content"] == "被改坏"
        rb = engine.rollback(result["refine_id"])
        assert eid in rb["restored"]
        entry = engine._entries.get(eid)
        assert entry["content"] == "原始内容"

    def test_rollback_delete_recreates_entry(self, engine):
        eid = engine._entries.save(KIND_MEMORY, "珍贵", "重要内容")
        engine._llm = lambda traj, ins: [{"op": "delete", "id": eid}]
        result = engine.refine([], "误删")
        assert engine._entries.get(eid) is None
        rb = engine.rollback(result["refine_id"])
        assert eid in rb["restored"]
        entry = engine._entries.get(eid)
        assert entry["content"] == "重要内容"
        assert entry["title"] == "珍贵"

    def test_mixed_rollback(self, engine):
        """混合变更回滚：新增删除 + 更新恢复"""
        keep = engine._entries.save(KIND_MEMORY, "保留", "旧值")
        victim = engine._entries.save(KIND_MEMORY, "待删", "将被删")
        engine._llm = lambda traj, ins: [
            {"op": "create", "kind": "skill", "title": "新技能", "content": "技能"},
            {"op": "update", "id": keep, "content": "新值"},
            {"op": "delete", "id": victim},
        ]
        result = engine.refine([], "混合1")
        assert engine._entries.get(victim) is None
        rb = engine.rollback(result["refine_id"])
        # 新增条目被删
        assert engine._entries.search("新技能") == []
        # 更新恢复旧值 / 删除的条目重建
        assert engine._entries.get(keep)["content"] == "旧值"
        assert engine._entries.get(victim)["content"] == "将被删"

    def test_rollback_missing_refine_id(self, engine):
        with pytest.raises(RefineValidationError):
            engine.rollback("refine_不存在")

    def test_rollback_keeps_snapshot_for_audit(self, engine):
        result = engine.refine([], "审计条目")
        rid = result["refine_id"]
        engine.rollback(rid)
        # 快照保留（可追溯）
        assert engine._history.get(rid) is not None

    def test_refine_again_after_rollback(self, engine):
        """回滚后同指令可再次提炼（不残留状态）"""
        result = engine.refine([], "可重复")
        eid = result["created"][0]
        engine.rollback(result["refine_id"])
        result2 = engine.refine([], "可重复")
        assert len(result2["created"]) == 1


class TestHistory:
    """历史查看"""

    def test_list_history(self, engine):
        engine.refine([], "第一条")
        engine.refine([], "第二条", scope="global")
        records = engine.list_history(limit=10)
        assert len(records) == 2
        assert records[0]["change_count"] == 1
        assert any(r["scope"] == "global" for r in records)

    def test_list_history_empty(self, engine):
        assert engine.list_history() == []