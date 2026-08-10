# -*- coding: utf-8 -*-
"""
Harness 条目 CRUD 测试（L1）

覆盖 store 层 harness 扩展 + entries 门面：
- entry_memories 表新列（kind/title/scope/version）与老表迁移
- 4 类条目（prompt/memory/skill/subagent）保存/读取/更新/删除
- kind/scope 过滤、幂等 upsert（version+1）、slugify
- refine_history 快照表 CRUD

使用独立内存 SQLite（FakeDb），不触碰全局 DatabaseManager 单例。
"""

import sqlite3

import pytest

from app.core.harness.entries import (
    KIND_MEMORY,
    KIND_PROMPT,
    KIND_SKILL,
    KIND_SUBAGENT,
    SCOPE_GLOBAL,
    SCOPE_LOCAL,
    HarnessEntries,
    slugify,
)
from app.core.store.memory_repository import MemoryRepository
from app.core.store.refine_history_repository import RefineHistoryRepository


class FakeDb:
    """最小 db_manager 替身：execute_sql + is_connected + _conn（内存库）"""

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
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield FakeDb(conn)
    conn.close()


@pytest.fixture
def repo(db):
    return MemoryRepository(db)


@pytest.fixture
def entries(repo):
    return HarnessEntries(repo)


@pytest.fixture
def history(db):
    return RefineHistoryRepository(db)


class TestStoreHarnessColumns:
    """store 层 harness 扩展字段"""

    def test_new_table_has_harness_columns(self, repo):
        """新建表自带 kind/title/scope/version 列"""
        repo.save({"content": "记忆"})
        row = repo.search()[0]
        assert row["kind"] == "memory"
        assert row["scope"] == "local"
        assert row["version"] == 1
        assert row["title"] == ""

    def test_old_table_alter_add_columns(self):
        """老式 entry_memories 表（无 harness 列）→ ALTER 增量迁移"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE entry_memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, "
            "enabled INTEGER DEFAULT 1, confidence REAL DEFAULT 0.8, source TEXT DEFAULT 'manual', "
            "created_at TEXT, updated_at TEXT, last_accessed TEXT)"
        )
        cur.execute("INSERT INTO entry_memories (id, content) VALUES ('e1', '老条目')")
        conn.commit()
        repo = MemoryRepository(FakeDb(conn))
        row = repo.search()[0]
        assert row["kind"] == "memory"
        assert row["scope"] == "local"
        assert row["version"] == 1
        conn.close()

    def test_old_memories_table_migrates(self):
        """旧 memories 表（含 category）→ 迁移到 entry_memories 并补默认列"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE memories (memory_id TEXT PRIMARY KEY, content TEXT, enabled INTEGER, "
            "confidence REAL, source TEXT, created_at TEXT, updated_at TEXT, last_accessed TEXT, category TEXT)"
        )
        cur.execute(
            "INSERT INTO memories VALUES ('m1', '旧记忆', 1, 0.9, 'manual', "
            "'2026-01-01', '2026-01-01', '', 'cat')"
        )
        conn.commit()
        repo = MemoryRepository(FakeDb(conn))
        rows = repo.search()
        assert len(rows) == 1
        assert rows[0]["id"] == "m1"
        assert rows[0]["kind"] == "memory"
        assert rows[0]["scope"] == "local"
        conn.close()

    def test_invalid_kind_normalized_to_memory(self, repo):
        """非法 kind 落库时回退 memory"""
        repo.save({"content": "x", "kind": "bogus"})
        assert repo.search()[0]["kind"] == "memory"

    def test_invalid_scope_normalized_to_local(self, repo):
        """非法 scope 落库时回退 local"""
        repo.save({"content": "x", "scope": "bogus"})
        assert repo.search()[0]["scope"] == "local"


class TestHarnessEntriesCRUD:
    """entries 门面 CRUD"""

    def test_save_and_get(self, entries):
        eid = entries.save(KIND_MEMORY, "规则", "提交走 conventional commits")
        entry = entries.get(eid)
        assert entry["id"] == eid
        assert entry["kind"] == "memory"
        assert entry["title"] == "规则"
        assert entry["content"] == "提交走 conventional commits"
        assert entry["scope"] == "local"
        assert entry["version"] == 1
        assert entry["created_at"] and entry["updated_at"]

    def test_save_requires_title(self, entries):
        with pytest.raises(ValueError):
            entries.save(KIND_MEMORY, "", "内容")

    def test_save_requires_content(self, entries):
        with pytest.raises(ValueError):
            entries.save(KIND_MEMORY, "标题", "  ")

    def test_save_invalid_kind(self, entries):
        with pytest.raises(ValueError):
            entries.save("bogus", "标题", "内容")

    def test_save_invalid_scope(self, entries):
        with pytest.raises(ValueError):
            entries.save(KIND_MEMORY, "标题", "内容", scope="bogus")

    def test_upsert_same_title_increments_version(self, entries):
        eid = entries.save(KIND_MEMORY, "规则", "v1 内容")
        entries.save(KIND_MEMORY, "规则", "v2 内容")
        entry = entries.get(eid)
        assert entry["version"] == 2
        assert entry["content"] == "v2 内容"
        # 仅一条（幂等 upsert，不重复）
        assert entries.count() == 1

    def test_upsert_keeps_kind_when_unchanged(self, entries):
        """同类覆盖不重置 kind 字段"""
        entries.save(KIND_SKILL, "git", "用 git commit")
        entries.save(KIND_SKILL, "git", "用 git commit -m")
        assert entries.get("git")["kind"] == "skill"

    def test_update_fields(self, entries):
        eid = entries.save(KIND_MEMORY, "标题A", "内容A")
        assert entries.update(eid, content="内容B", title="标题B", scope=SCOPE_GLOBAL)
        entry = entries.get(eid)
        assert entry["content"] == "内容B"
        assert entry["title"] == "标题B"
        assert entry["scope"] == "global"
        assert entry["version"] == 2

    def test_update_requires_existing(self, entries):
        assert entries.update("不存在", content="x") is False

    def test_delete(self, entries):
        eid = entries.save(KIND_MEMORY, "临时", "内容")
        assert entries.delete(eid) is True
        assert entries.get(eid) is None
        assert entries.delete(eid) is False  # 二次删除返回 False

    def test_list_kind_filter(self, entries):
        entries.save(KIND_MEMORY, "m1", "记忆1")
        entries.save(KIND_SKILL, "s1", "技能1")
        entries.save(KIND_PROMPT, "p1", "提示1")
        entries.save(KIND_SUBAGENT, "a1", "子代理1")
        assert len(entries.list(kind=KIND_SKILL)) == 1
        assert entries.list(kind=KIND_SKILL)[0]["title"] == "s1"
        assert len(entries.list()) == 4

    def test_list_scope_filter(self, entries):
        entries.save(KIND_MEMORY, "local1", "本地", scope=SCOPE_LOCAL)
        entries.save(KIND_MEMORY, "global1", "全局", scope=SCOPE_GLOBAL)
        assert len(entries.list(scope=SCOPE_GLOBAL)) == 1
        assert len(entries.list(scope=SCOPE_LOCAL)) == 1

    def test_list_invalid_kind(self, entries):
        with pytest.raises(ValueError):
            entries.list(kind="bogus")

    def test_search_by_query(self, entries):
        entries.save(KIND_MEMORY, "提交流程", "使用 git commit -m 提交")
        entries.save(KIND_MEMORY, "审查", "检查 import 顺序")
        result = entries.search("git")
        assert len(result) == 1
        assert result[0]["title"] == "提交流程"

    def test_search_kind_and_scope(self, entries):
        entries.save(KIND_SKILL, "s", "git 技能", scope=SCOPE_GLOBAL)
        entries.save(KIND_MEMORY, "m", "git 记忆", scope=SCOPE_LOCAL)
        result = entries.search("git", kind=KIND_SKILL)
        assert len(result) == 1 and result[0]["kind"] == "skill"
        result = entries.search("git", scope=SCOPE_GLOBAL)
        assert len(result) == 1 and result[0]["scope"] == "global"

    def test_count_with_filter(self, entries):
        entries.save(KIND_MEMORY, "a", "1")
        entries.save(KIND_SKILL, "b", "2")
        assert entries.count() == 2
        assert entries.count(kind=KIND_MEMORY) == 1


class TestSlugify:
    """slug 生成"""

    def test_basic(self):
        assert slugify("Hello World") == "hello_world"

    def test_chinese_preserved(self):
        assert slugify("项目规则") == "项目规则"

    def test_special_chars(self):
        assert slugify("A!B@C#") == "a_b_c"

    def test_empty_fallback(self):
        assert slugify("") == "entry"
        assert slugify("   ") == "entry"

    def test_truncate(self):
        assert len(slugify("x" * 100)) <= 64

    def test_used_as_entry_id(self, entries):
        eid = entries.save(KIND_MEMORY, "Git 提交流程", "内容")
        assert eid == "git_提交流程"


class TestRefineHistoryRepository:
    """refine 快照表 CRUD"""

    def test_save_and_get(self, history):
        changes = [
            {"entry_id": "e1", "kind": "memory", "before": None, "after": {"content": "新"}},
            {"entry_id": "e2", "kind": "skill", "before": {"content": "旧"}, "after": None},
        ]
        rid = history.save_refinement(changes, trigger="指令", scope="local")
        assert rid and rid.startswith("refine_")
        snap = history.get(rid)
        assert snap["trigger"] == "指令"
        assert snap["scope"] == "local"
        assert len(snap["changes"]) == 2
        assert snap["changes"][0]["after"]["content"] == "新"

    def test_save_with_explicit_id(self, history):
        rid = history.save_refinement([], refine_id="refine_abc")
        assert rid == "refine_abc"
        assert history.get("refine_abc") is not None

    def test_save_explicit_id_overwrites(self, history):
        history.save_refinement([{"entry_id": "x", "kind": "memory", "before": None, "after": None}], refine_id="refine_fixed")
        history.save_refinement([], trigger="再提炼", refine_id="refine_fixed")
        snap = history.get("refine_fixed")
        assert snap["trigger"] == "再提炼"  # INSERT OR REPLACE 覆盖

    def test_get_missing(self, history):
        assert history.get("refine_none") is None

    def test_list_newest_first(self, history):
        history.save_refinement([], trigger="t1")
        history.save_refinement([], trigger="t2")
        snaps = history.list(limit=10)
        assert len(snaps) == 2
        assert snaps[0]["trigger"] == "t2"  # 新→旧

    def test_list_scope_filter(self, history):
        history.save_refinement([], trigger="本地", scope="local")
        history.save_refinement([], trigger="全局", scope="global")
        assert len(history.list(scope="global")) == 1
        assert history.list(scope="global")[0]["trigger"] == "全局"

    def test_count(self, history):
        assert history.count() == 0
        history.save_refinement([], trigger="c1")
        assert history.count() == 1

    def test_delete_snapshot(self, history):
        rid = history.save_refinement([], trigger="d1")
        assert history.delete(rid) is True
        assert history.get(rid) is None

    def test_changes_json_roundtrip(self, history):
        """before/after 复杂结构 JSON 序列化往返无损"""
        changes = [
            {
                "entry_id": "e1",
                "kind": "memory",
                "before": {"kind": "memory", "title": "T", "content": "旧\n多行", "scope": "local", "version": 1},
                "after": None,
            }
        ]
        rid = history.save_refinement(changes)
        snap = history.get(rid)
        assert snap["changes"][0]["before"]["content"] == "旧\n多行"