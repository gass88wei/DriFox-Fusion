# -*- coding: utf-8 -*-
"""Gate1 冒烟：/refine 全链路（提炼 → 快照 → 历史 → 回滚）脚本级 E2E

复用测试的 FakeDb（内存 SQLite），走真实 RefineEngine 闭环：
1. 启发式提炼（无 LLM）→ 新增 memory 条目
2. refine_history 快照落库（before/after JSON）
3. list_history 可查
4. rollback 精确撤销（新增删除）
退出码 0 = 全链路通过
"""
import sqlite3
import sys

from app.core.harness.refine import RefineEngine
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


def main() -> int:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = FakeDb(conn)
    entries = HarnessEntries2(db)
    history = RefineHistoryRepository(db)
    engine = RefineEngine(entries, history)

    trajectory = [
        {"role": "user", "content": "帮我写一个读取 CSV 的 Python 函数"},
        {"role": "assistant", "content": "使用 csv.DictReader 逐行读取，处理 BOM 头"},
        {"role": "user", "content": "记得处理编码问题"},
    ]

    # 1. 提炼
    result = engine.refine(trajectory, "用户偏好：处理 CSV 时注意编码与 BOM", scope="local")
    rid = result["refine_id"]
    created = result["created"]
    assert rid, "refine_id 为空"
    assert created, "应产出新增条目"
    created_id = created[0] if isinstance(created[0], str) else created[0]["id"]
    print(f"[1] 提炼完成: {rid} 新增 {len(created)} 条")
    row = entries.get(created_id)
    assert row, "新增条目未落库"
    assert row.get("title") and row.get("content"), "条目字段缺失"
    print(f"    entry: id={created_id} kind={row.get('kind')} title={row.get('title')!r}")

    # 2. 快照可查
    records = engine.list_history(limit=5)
    assert any(r["refine_id"] == rid for r in records), "refine_history 无快照"
    print(f"[2] 快照落库可查: {len(records)} 条历史")

    # 3. 回滚（新增删除）
    rb = engine.rollback(rid)
    assert len(rb.get("removed", [])) == 1, "回滚应删除 1 条新增"
    assert entries.get(created_id) is None, "回滚后条目应不存在"
    print(f"[3] 回滚成功: 删除新增 {len(rb['removed'])} 条，条目已消失")

    # 4. 回滚后快照仍保留（审计可追溯）
    records2 = engine.list_history(limit=5)
    assert any(r["refine_id"] == rid for r in records2), "回滚后快照应保留"
    print("[4] 快照审计保留 ✓")

    print("\nSMOKE /refine 全链路 PASS")
    return 0


def HarnessEntries2(db):
    from app.core.harness.entries import HarnessEntries
    return HarnessEntries(MemoryRepository(db))


if __name__ == "__main__":
    sys.exit(main())