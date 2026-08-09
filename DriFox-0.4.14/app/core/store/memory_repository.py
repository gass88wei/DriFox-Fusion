# -*- coding: utf-8 -*-
"""
条目记忆仓储模块 - 专门负责长期记忆的持久化

重构为无分类的简单条目记忆结构。
"""

import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


class MemoryRepository:
    """条目记忆数据仓储，处理记忆的 CRUD 操作"""

    TABLE_NAME = "entry_memories"  # 重命名表
    DB_FILENAME = "sessions.db"

    # ========== Harness 扩展（L1）：4 类条目（prompt/memory/skill/subagent） ==========
    # 在原有 content/source 基础上扩展 kind/title/scope/version 字段，
    # 旧数据迁移时自动补默认值（kind='memory'、scope='local'、version=1）。
    KIND_PROMPT = "prompt"
    KIND_MEMORY = "memory"
    KIND_SKILL = "skill"
    KIND_SUBAGENT = "subagent"
    VALID_KINDS = (KIND_PROMPT, KIND_MEMORY, KIND_SKILL, KIND_SUBAGENT)
    SCOPE_LOCAL = "local"
    SCOPE_GLOBAL = "global"
    VALID_SCOPES = (SCOPE_LOCAL, SCOPE_GLOBAL)
    # 迁移时通过 ALTER TABLE 补充的列（SQLite 支持带 DEFAULT 的 ADD COLUMN）
    _HARNESS_COLUMNS = (
        ("kind", "TEXT DEFAULT 'memory'"),
        ("title", "TEXT DEFAULT ''"),
        ("scope", "TEXT DEFAULT 'local'"),
        ("version", "INTEGER DEFAULT 1"),
    )

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

    def _column_names(self) -> set:
        """读取当前表的列名集合（用于增量迁移）

        注意：db_manager 的 row_factory=sqlite3.Row，PRAGMA 返回 Row 对象，
        不能用 isinstance(col, (list, tuple)) 判断（Row 不是 list/tuple）。
        """
        success, cols = self._execute(f"PRAGMA table_info({self.TABLE_NAME})")
        names = set()
        if success and cols:
            for col in cols:
                try:
                    names.add(col[1])  # Row / tuple 均支持索引访问
                except (IndexError, TypeError):
                    continue
        return names

    def _ensure_harness_columns(self) -> bool:
        """增量迁移：为已存在的表补充 harness 扩展列（kind/title/scope/version）"""
        existing = self._column_names()
        if not existing:
            return False
        ok = True
        for name, ddl in self._HARNESS_COLUMNS:
            if name not in existing:
                success, _ = self._execute(
                    f"ALTER TABLE {self.TABLE_NAME} ADD COLUMN {name} {ddl}"
                )
                ok = ok and success
        return ok

    def _ensure_table(self) -> bool:
        """确保表存在（迁移旧表或创建新表）+ 增量补充 harness 扩展列"""
        if not self.is_initialized:
            return False
        try:
            # 检查旧表是否存在
            success, rows = self._execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
            )
            has_old_table = success and rows and len(rows) > 0

            if has_old_table:
                # 检查旧表是否有 category 列
                success, cols = self._execute("PRAGMA table_info(memories)")
                has_category = False
                if success and cols:
                    # cols 是 Row 对象列表（row_factory=sqlite3.Row），索引访问列名
                    for col in cols:
                        try:
                            if col[1] == 'category':
                                has_category = True
                                break
                        except (IndexError, TypeError):
                            continue

                if has_category:
                    # 创建新表并迁移数据
                    self._execute(f'''
                        CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                            id TEXT PRIMARY KEY,
                            content TEXT NOT NULL,
                            enabled INTEGER DEFAULT 1,
                            confidence REAL DEFAULT 0.8,
                            source TEXT DEFAULT 'manual',
                            created_at TEXT,
                            updated_at TEXT,
                            last_accessed TEXT,
                            kind TEXT DEFAULT 'memory',
                            title TEXT DEFAULT '',
                            scope TEXT DEFAULT 'local',
                            version INTEGER DEFAULT 1
                        )
                    ''')
                    # 迁移数据（忽略 category）
                    self._execute(f'''
                        INSERT INTO {self.TABLE_NAME} (id, content, enabled, confidence, source, created_at, updated_at, last_accessed, kind, title, scope, version)
                        SELECT memory_id, content, enabled, confidence, source, created_at, updated_at, last_accessed, 'memory', '', 'local', 1 FROM memories
                    ''')
                    logger.info("[MemoryRepository] 已迁移旧记忆数据到新表")
                    return True

            # 直接创建新表（若已存在则跳过；再补 harness 扩展列）
            success, _ = self._execute(f'''
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    confidence REAL DEFAULT 0.8,
                    source TEXT DEFAULT 'manual',
                    created_at TEXT,
                    updated_at TEXT,
                    last_accessed TEXT,
                    kind TEXT DEFAULT 'memory',
                    title TEXT DEFAULT '',
                    scope TEXT DEFAULT 'local',
                    version INTEGER DEFAULT 1
                )
            ''')
            # 已有表（老 schema）→ 增量补充 harness 扩展列
            self._ensure_harness_columns()
            return success

        except Exception as e:
            logger.error(f"[MemoryRepository] _ensure_table 异常: {e}")
            return False

    def _row_to_memory(self, row) -> Dict:
        """将数据库行转换为记忆字典"""
        if not row:
            return {}

        if hasattr(row, 'keys'):
            d = {k: row[k] for k in row.keys()}
        elif isinstance(row, dict):
            d = dict(row)
        else:
            return {}

        return {
            "id": d.get("id", "") or d.get("memory_id", ""),
            "content": d.get("content", ""),
            "enabled": bool(d.get("enabled", 1)),
            "confidence": float(d.get("confidence", 0.8)),
            "source": d.get("source", "manual"),
            "created_at": d.get("created_at", ""),
            "updated_at": d.get("updated_at", ""),
            "last_accessed": d.get("last_accessed", ""),
            # Harness 扩展字段
            "kind": d.get("kind", self.KIND_MEMORY),
            "title": d.get("title", ""),
            "scope": d.get("scope", self.SCOPE_LOCAL),
            "version": int(d.get("version", 1) or 1),
        }

    def _normalize_kind(self, kind) -> str:
        """校验并规范化 kind（非法值回退 memory）"""
        if kind in self.VALID_KINDS:
            return kind
        return self.KIND_MEMORY

    def _normalize_scope(self, scope) -> str:
        """校验并规范化 scope（非法值回退 local）"""
        if scope in self.VALID_SCOPES:
            return scope
        return self.SCOPE_LOCAL

    def save(self, memory: Dict) -> bool:
        """
        保存单条记忆

        Args:
            memory: 记忆数据，包含 content, confidence, source 等

        Returns:
            bool: 保存是否成功
        """
        if not self._ensure_table():
            return False

        # 生成唯一的 id
        existing_id = memory.get("id") or memory.get("memory_id")
        if existing_id:
            memory_id = str(existing_id)
        else:
            content_hash = hashlib.md5(str(memory.get("content", "")).encode()).hexdigest()[:8]
            memory_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{content_hash}"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        success, _ = self._execute(f'''
            INSERT OR REPLACE INTO {self.TABLE_NAME}
            (id, content, enabled, confidence, source, created_at, updated_at, last_accessed, kind, title, scope, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            memory_id,
            memory.get("content", ""),
            1 if memory.get("enabled", True) else 0,
            memory.get("confidence", 0.8),
            memory.get("source", "manual"),
            memory.get("created_at") or now,
            now,
            now,
            self._normalize_kind(memory.get("kind", self.KIND_MEMORY)),
            memory.get("title", ""),
            self._normalize_scope(memory.get("scope", self.SCOPE_LOCAL)),
            int(memory.get("version", 1) or 1),
        ))

        return success

    def save_all(self, memories: List[Dict]) -> bool:
        """
        批量保存记忆（增量更新，仅 upsert 新增/变更项 + 清理已删除项）

        替代原来的 DELETE + 全量 INSERT 模式，在记忆量大时显著减少 I/O。

        Args:
            memories: 记忆列表

        Returns:
            bool: 是否成功
        """
        if not self._ensure_table() or not self._db:
            return False

        try:
            conn = self._db._conn
            cursor = conn.cursor()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 获取现有记忆 ID 集合（用于后续清理已删除项）
            cursor.execute(f"SELECT id FROM {self.TABLE_NAME}")
            existing_ids = {row[0] for row in cursor.fetchall()}

            # 逐条 upsert（INSERT OR REPLACE），仅新增/变更项写入
            new_ids = set()
            for memory in memories:
                existing_id = memory.get("id") or memory.get("memory_id")
                if existing_id:
                    memory_id = str(existing_id)
                else:
                    content_hash = hashlib.md5(str(memory.get("content", "")).encode()).hexdigest()[:8]
                    memory_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{content_hash}"
                new_ids.add(memory_id)

                cursor.execute(f'''
                    INSERT OR REPLACE INTO {self.TABLE_NAME}
                    (id, content, enabled, confidence, source, created_at, updated_at, last_accessed, kind, title, scope, version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    memory_id,
                    memory.get("content", ""),
                    1 if memory.get("enabled", True) else 0,
                    memory.get("confidence", 0.8),
                    memory.get("source", "manual"),
                    memory.get("created_at") or now,
                    now,
                    now,
                    self._normalize_kind(memory.get("kind", self.KIND_MEMORY)),
                    memory.get("title", ""),
                    self._normalize_scope(memory.get("scope", self.SCOPE_LOCAL)),
                    int(memory.get("version", 1) or 1),
                ))

            # 清理不再存在的记忆（避免全量 DELETE 再 INSERT 导致碎片+WAL 膨胀）
            removed_ids = existing_ids - new_ids
            if removed_ids:
                placeholders = ",".join("?" for _ in removed_ids)
                cursor.execute(
                    f"DELETE FROM {self.TABLE_NAME} WHERE id IN ({placeholders})",
                    list(removed_ids),
                )

            conn.commit()
            logger.debug(
                f"[MemoryRepository] 已保存 {len(memories)} 条记忆 "
                f"(upserted={len(new_ids - existing_ids)}, kept={len(new_ids & existing_ids)}, "
                f"removed={len(removed_ids)})"
            )
            return True

        except Exception as e:
            logger.error(f"[MemoryRepository] save_all 异常: {e}")
            if self._db and self._db._conn:
                self._db._conn.rollback()
            return False

    def load_all(self, limit: int = 200, include_disabled: bool = False) -> List[Dict]:
        """
        加载所有记忆

        Args:
            limit: 最大返回数量
            include_disabled: 是否包含已禁用的记忆

        Returns:
            List[Dict]: 记忆列表
        """
        if not self._ensure_table():
            return []

        try:
            if include_disabled:
                sql = f'SELECT * FROM {self.TABLE_NAME} ORDER BY confidence DESC, updated_at DESC LIMIT ?'
                params = (limit,)
            else:
                sql = f'SELECT * FROM {self.TABLE_NAME} WHERE enabled = 1 ORDER BY confidence DESC, updated_at DESC LIMIT ?'
                params = (limit,)

            success, rows = self._execute(sql, params)
            if success:
                return [self._row_to_memory(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[MemoryRepository] load_all 异常: {e}")
            return []

    def get(self, memory_id: str) -> Optional[Dict]:
        """
        获取单条记忆

        Args:
            memory_id: 记忆 ID

        Returns:
            Optional[Dict]: 记忆数据
        """
        if not self._ensure_table():
            return None

        try:
            success, rows = self._execute(
                f'SELECT * FROM {self.TABLE_NAME} WHERE id = ?',
                (memory_id,)
            )
            if success and rows and len(rows) > 0:
                return self._row_to_memory(rows[0])
            return None
        except Exception as e:
            logger.error(f"[MemoryRepository] get 异常: {e}")
            return None

    def update(self, memory_id: str, content: str) -> bool:
        """
        更新记忆内容

        Args:
            memory_id: 记忆 ID
            content: 新内容

        Returns:
            bool: 更新是否成功
        """
        if not self._ensure_table():
            return False

        try:
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET content = ?, updated_at = ? WHERE id = ?',
                (content, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), memory_id)
            )
            return success
        except Exception as e:
            logger.error(f"[MemoryRepository] update 异常: {e}")
            return False

    def update_entry(
        self,
        memory_id: str,
        content: str = None,
        title: str = None,
        kind: str = None,
        scope: str = None,
        version: int = None,
    ) -> bool:
        """
        Harness 扩展更新：按需更新任意字段（省略的字段保持不变），version 默认 +1

        Args:
            memory_id: 记忆 ID
            content: 新内容（None=不变）
            title: 新标题（None=不变）
            kind: 新类型（None=不变）
            scope: 新作用域（None=不变）
            version: 新版本号（None=自动 +1）

        Returns:
            bool: 更新是否成功
        """
        if not self._ensure_table():
            return False

        try:
            sets = ["updated_at = ?"]
            params: list = [datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
            if content is not None:
                sets.append("content = ?")
                params.append(content)
            if title is not None:
                sets.append("title = ?")
                params.append(title)
            if kind is not None:
                sets.append("kind = ?")
                params.append(self._normalize_kind(kind))
            if scope is not None:
                sets.append("scope = ?")
                params.append(self._normalize_scope(scope))
            if version is not None:
                sets.append("version = ?")
                params.append(int(version))
            else:
                # 默认 version+1（保留原值加 1）
                sets.append("version = COALESCE(version, 1) + 1")
            params.append(memory_id)
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET {", ".join(sets)} WHERE id = ?',
                tuple(params)
            )
            return success
        except Exception as e:
            logger.error(f"[MemoryRepository] update_entry 异常: {e}")
            return False

    def delete(self, memory_id: str) -> bool:
        """
        删除指定记忆

        Args:
            memory_id: 记忆 ID

        Returns:
            bool: 删除是否成功
        """
        if not self._ensure_table():
            return False

        try:
            success, _ = self._execute(
                f'DELETE FROM {self.TABLE_NAME} WHERE id = ?',
                (memory_id,)
            )
            return success
        except Exception as e:
            logger.error(f"[MemoryRepository] delete 异常: {e}")
            return False

    def clear_all(self) -> bool:
        """
        清空所有记忆

        Returns:
            bool: 是否成功
        """
        if not self._ensure_table():
            return False

        try:
            success, _ = self._execute(f'DELETE FROM {self.TABLE_NAME}')
            return success
        except Exception as e:
            logger.error(f"[MemoryRepository] clear_all 异常: {e}")
            return False

    def update_enabled(self, memory_id: str, enabled: bool) -> bool:
        """
        更新记忆的启用状态

        Args:
            memory_id: 记忆 ID
            enabled: 是否启用

        Returns:
            bool: 更新是否成功
        """
        if not self._ensure_table():
            return False

        try:
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET enabled = ?, updated_at = ? WHERE id = ?',
                (1 if enabled else 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), memory_id)
            )
            return success
        except Exception as e:
            logger.error(f"[MemoryRepository] update_enabled 异常: {e}")
            return False

    def update_last_accessed(self, memory_id: str) -> bool:
        """
        更新记忆的最后访问时间

        Args:
            memory_id: 记忆 ID

        Returns:
            bool: 更新是否成功
        """
        if not self._ensure_table():
            return False

        try:
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET last_accessed = ? WHERE id = ?',
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), memory_id)
            )
            return success
        except Exception as e:
            logger.error(f"[MemoryRepository] update_last_accessed 异常: {e}")
            return False

    def search(self, query: str = "", limit: int = 9999, kind: str = None, scope: str = None) -> List[Dict]:
        """
        搜索记忆

        Args:
            query: 搜索关键词
            limit: 最大返回数量
            kind: 可选，按条目类型过滤（prompt/memory/skill/subagent，None=全部）
            scope: 可选，按作用域过滤（local/global，None=全部）

        Returns:
            List[Dict]: 匹配的记忆列表
        """
        if not self._ensure_table():
            return []

        try:
            conditions = []
            params_list: list = []
            if query:
                conditions.append("content LIKE ?")
                params_list.append(f"%{query}%")
            if kind:
                conditions.append("kind = ?")
                params_list.append(self._normalize_kind(kind))
            if scope:
                conditions.append("scope = ?")
                params_list.append(self._normalize_scope(scope))

            where = ""
            if conditions:
                where = "WHERE " + " AND ".join(conditions)
            params_list.append(limit)

            success, rows = self._execute(
                f'SELECT * FROM {self.TABLE_NAME} {where} ORDER BY confidence DESC, updated_at DESC LIMIT ?',
                tuple(params_list)
            )

            if success and rows:
                return [self._row_to_memory(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[MemoryRepository] search 异常: {e}")
            return []

    def count(self) -> int:
        """
        获取记忆总数

        Returns:
            int: 记忆数量
        """
        if not self._ensure_table():
            return 0

        try:
            success, result = self._execute(f'SELECT COUNT(*) FROM {self.TABLE_NAME}')
            if success and result:
                return result[0][0] if isinstance(result[0], tuple) else result[0]
            return 0
        except Exception as e:
            logger.error(f"[MemoryRepository] count 异常: {e}")
            return 0
