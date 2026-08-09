# -*- coding: utf-8 -*-
"""
refine 快照仓储模块 - 记录 /refine 提炼前后的条目快照，支持回滚

设计（L1 Harness）：
- 每次 refine 生成一个 refine_id（与一次 /refine 调用一一对应）
- changes 字段为 JSON 数组，每项形如：
    {"entry_id": "...", "kind": "memory",
     "before": {...} | None,   # 提炼前条目快照（None=当时不存在/新增）
     "after": {...} | None}    # 提炼后条目快照（None=已被删除）
- before/after 均为 MemoryRepository._row_to_memory 的字典结构（JSON 可序列化）
- 回滚 = 读取 refine_id 对应 changes，逐条恢复 before 状态
"""

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


class RefineHistoryRepository:
    """refine 快照数据仓储，处理提炼历史的写入与回滚读取"""

    TABLE_NAME = "refine_history"
    DB_FILENAME = "sessions.db"

    def __init__(self, db_manager):
        """
        Args:
            db_manager: DatabaseManager 实例
        """
        self._db = db_manager

    @property
    def is_initialized(self) -> bool:
        return self._db is not None and self._db.is_connected

    def _execute(self, sql: str, params: tuple = ()) -> Tuple[bool, Any]:
        """执行 SQL（内部使用）"""
        if not self._db:
            return False, "数据库未初始化"
        return self._db.execute_sql(sql, params)

    def _ensure_table(self) -> bool:
        """确保 refine_history 表存在"""
        if not self.is_initialized:
            return False
        try:
            success, _ = self._execute(f'''
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id TEXT PRIMARY KEY,
                    trigger TEXT DEFAULT '',
                    scope TEXT DEFAULT 'local',
                    changes TEXT NOT NULL,
                    created_at TEXT
                )
            ''')
            return success
        except Exception as e:
            logger.error(f"[RefineHistoryRepository] _ensure_table 异常: {e}")
            return False

    def _row_to_refine(self, row) -> Optional[Dict]:
        """将数据库行转换为 refine 快照字典"""
        if not row:
            return None

        if hasattr(row, 'keys'):
            d = {k: row[k] for k in row.keys()}
        elif isinstance(row, dict):
            d = dict(row)
        else:
            return None

        changes = []
        raw = d.get("changes", "")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    changes = parsed
            except (ValueError, TypeError):
                changes = []

        return {
            "id": d.get("id", ""),
            "trigger": d.get("trigger", ""),
            "scope": d.get("scope", "local"),
            "changes": changes,
            "created_at": d.get("created_at", ""),
        }

    def save_refinement(
        self,
        changes: List[Dict],
        trigger: str = "",
        scope: str = "local",
        refine_id: str = "",
    ) -> Optional[str]:
        """
        记录一次 refine 快照

        Args:
            changes: 变更列表，每项 {"entry_id", "kind", "before", "after"}
            trigger: 触发描述（如 commands 传入的指令 / 提炼说明）
            scope: 本次 refine 的作用域（local/global）
            refine_id: 指定 refine_id（同名重试时复用）；为空则自动生成

        Returns:
            refine_id（str）或 None（失败）
        """
        if not self._ensure_table():
            return None

        if not refine_id:
            raw = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{hashlib.md5(str(changes).encode()).hexdigest()[:8]}"
            refine_id = f"refine_{raw}"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            success, _ = self._execute(f'''
                INSERT OR REPLACE INTO {self.TABLE_NAME}
                (id, trigger, scope, changes, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                refine_id,
                trigger,
                scope,
                json.dumps(changes, ensure_ascii=False),
                now,
            ))
            if success:
                logger.info(f"[RefineHistoryRepository] 已记录 refine 快照: {refine_id} ({len(changes)} 条变更)")
                return refine_id
            return None
        except Exception as e:
            logger.error(f"[RefineHistoryRepository] save_refinement 异常: {e}")
            return None

    def get(self, refine_id: str) -> Optional[Dict]:
        """读取一次 refine 快照"""
        if not self._ensure_table():
            return None

        try:
            success, rows = self._execute(
                f'SELECT * FROM {self.TABLE_NAME} WHERE id = ?',
                (refine_id,)
            )
            if success and rows and len(rows) > 0:
                return self._row_to_refine(rows[0])
            return None
        except Exception as e:
            logger.error(f"[RefineHistoryRepository] get 异常: {e}")
            return None

    def list(self, limit: int = 50, scope: str = None) -> List[Dict]:
        """列出最近 refine 快照（新→旧）"""
        if not self._ensure_table():
            return []

        try:
            if scope:
                success, rows = self._execute(
                    f'SELECT * FROM {self.TABLE_NAME} WHERE scope = ? ORDER BY created_at DESC, rowid DESC LIMIT ?',
                    (scope, limit)
                )
            else:
                success, rows = self._execute(
                    f'SELECT * FROM {self.TABLE_NAME} ORDER BY created_at DESC, rowid DESC LIMIT ?',
                    (limit,)
                )
            if success and rows:
                return [self._row_to_refine(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[RefineHistoryRepository] list 异常: {e}")
            return []

    def count(self) -> int:
        """获取快照总数"""
        if not self._ensure_table():
            return 0

        try:
            success, result = self._execute(f'SELECT COUNT(*) FROM {self.TABLE_NAME}')
            if success and result:
                try:
                    return int(result[0][0])  # Row / tuple 均支持索引
                except (IndexError, TypeError, ValueError):
                    return 0
            return 0
        except Exception as e:
            logger.error(f"[RefineHistoryRepository] count 异常: {e}")
            return 0

    def delete(self, refine_id: str) -> bool:
        """删除一次快照（回滚完成后清理用，可选）"""
        if not self._ensure_table():
            return False

        try:
            success, _ = self._execute(
                f'DELETE FROM {self.TABLE_NAME} WHERE id = ?',
                (refine_id,)
            )
            return success
        except Exception as e:
            logger.error(f"[RefineHistoryRepository] delete 异常: {e}")
            return False