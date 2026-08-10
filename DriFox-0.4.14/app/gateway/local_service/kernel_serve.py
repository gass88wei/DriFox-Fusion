# -*- coding: utf-8 -*-
"""
RLM 常驻执行服务 - 任务管理 + SSE 事件 + 快照持久化（L4）

借鉴 prime-agent daemon 设计 + 融合计划 §3 L4：
- 服务进程内挂 L3 KernelManager（ipykernel 常驻），串行执行提交的任务
- 任务记录（含事件列表）持久化到 .drifox/cache/kernel-serve/tasks.json
- 每个任务完成后自动打 dill 快照；服务重启时重载快照 + 未完成任务继续
- 事件广播：多个 SSE 客户端（UI 窗口）可 attach/detach，断开不影响任务执行

仅做服务接口层（不推翻 backend/UI 设计）；本模块无 Qt 依赖，可在
--serve 模式（无 UI 进程）或 UI 进程内平级使用。
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from app.core.kernel_manager import KernelManager


# 任务事件类型（SSE 载荷 `event` 字段）
EV_SUBMITTED = "task_submitted"
EV_STARTED = "task_started"
EV_STREAM = "task_stream"
EV_RESULT = "task_result"
EV_FINISHED = "task_finished"
EV_ERROR = "task_error"
EV_ABORTED = "task_aborted"
EV_SNAPSHOT = "task_snapshot"
EV_RESTORED = "kernel_restored"

# 单个任务保留的最大事件数（回放防膨胀）
MAX_EVENTS_PER_TASK = 500
# 任务状态
ST_QUEUED = "queued"
ST_RUNNING = "running"
ST_DONE = "done"
ST_ERROR = "error"
ST_ABORTED = "aborted"
_ST_ACTIVE = {ST_QUEUED, ST_RUNNING}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _state_dir() -> Path:
    """服务状态目录（.drifox/cache/kernel-serve）

    与 auto_route 同约定：状态类小文件统一放 cache 目录
    （`.drifox/app.config` 是文件而非目录，不能在其下 mkdir）。
    """
    try:
        from app.utils.utils import get_app_data_dir

        d = get_app_data_dir() / "cache" / "kernel-serve"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        p = Path("kernel-serve")
        p.mkdir(parents=True, exist_ok=True)
        return p


@dataclass
class TaskRecord:
    """一次内核任务（内存 + tasks.json 双态）"""

    id: str
    code: str
    session: str = "default"
    status: str = ST_QUEUED
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    timeout_ms: Optional[int] = None
    result: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    snapshot_path: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "session": self.session,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timeout_ms": self.timeout_ms,
            "result": self.result,
            "events": self.events[-MAX_EVENTS_PER_TASK:],
            "snapshot_path": self.snapshot_path,
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "TaskRecord":
        return cls(
            id=str(data.get("id", "")),
            code=str(data.get("code", "")),
            session=str(data.get("session", "default")),
            status=str(data.get("status", ST_QUEUED)),
            created_at=str(data.get("created_at", "")),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
            timeout_ms=data.get("timeout_ms"),
            result=data.get("result") or {},
            events=list(data.get("events") or []),
            snapshot_path=str(data.get("snapshot_path", "")),
        )


class KernelServeHandler:
    """RLM 常驻执行服务（单例式使用，无 UI 依赖）

    用法：
        handler = KernelServeHandler()
        handler.start()                      # 启动内核 + 恢复快照/未完成任务
        task = handler.submit_task("x = 42")
        handler.stop()                       # 关停（不丢任务记录）
    """

    _instance: Optional["KernelServeHandler"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, **kwargs) -> "KernelServeHandler":
        """全局单例（同一进程内多个 uvicorn/UI attach 共享同一内核）"""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(**kwargs)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """测试专用：重置单例（调用方负责先 stop）"""
        with cls._instance_lock:
            cls._instance = None

    def __init__(
        self,
        state_dir: Optional[str] = None,
        kernel: Optional[KernelManager] = None,
        auto_snapshot: bool = True,
    ):
        self._state_dir = Path(state_dir) if state_dir else _state_dir()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        # 直接注入 kernel 便于测试（不注入则懒启动真实内核）
        self._kernel = kernel
        self._auto_snapshot = auto_snapshot

        self._tasks: Dict[str, TaskRecord] = {}
        self._tasks_lock = threading.Lock()
        self._queue: "queue.Queue[TaskRecord]" = queue.Queue()
        # 事件订阅：task_id -> set(Queue)，unsubscribe 由 SSE 生成器 finally 调用
        self._subscribers: Dict[str, Set["queue.Queue"]] = {}
        self._sub_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._started = False
        self._snapshot_path = str(self._state_dir / "kernel-state.dill")

    # ========== 生命周期 ==========

    @property
    def is_running(self) -> bool:
        return self._started and self._worker is not None and self._worker.is_alive()

    @property
    def kernel(self) -> Optional[KernelManager]:
        return self._kernel

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def start(self) -> None:
        """启动服务：载入持久化任务 + 快照恢复 + 未完成任务继续"""
        if self._started:
            return
        self._load_tasks()
        restored = self.restore_kernel_state()
        self._started = True
        self._worker = threading.Thread(target=self._run_worker, name="kernel-serve", daemon=True)
        self._worker.start()
        logger.info(
            f"[KernelServe] 服务已启动（state_dir={self._state_dir}，"
            f"恢复任务={len(self._requeue_unfinished())}，快照恢复={restored}）"
        )

    def stop(self, keep_tasks: bool = True) -> None:
        """停止服务：持久化任务记录（keep_tasks=False 清空）"""
        self._started = False
        # 唤醒 worker 退出
        self._queue.put(None)  # type: ignore[arg-type]
        if self._worker is not None:
            self._worker.join(timeout=3.0)
            self._worker = None
        if keep_tasks:
            self._persist()
        else:
            try:
                for f in ("tasks.json", "kernel-state.dill", "kernel-state.json"):
                    p = self._state_dir / f
                    if p.exists():
                        p.unlink()
            except Exception as e:
                logger.warning(f"[KernelServe] 清理状态失败: {e}")
            with self._tasks_lock:
                self._tasks.clear()
        logger.info("[KernelServe] 服务已停止")

    # ========== 任务管理 ==========

    def submit_task(self, code: str, timeout_ms: Optional[int] = None, session: str = "default") -> Dict[str, Any]:
        """提交任务（入队，worker 串行执行），返回任务摘要"""
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code 不能为空")
        task = TaskRecord(
            id=f"t_{uuid.uuid4().hex[:12]}",
            code=code,
            session=session or "default",
            timeout_ms=timeout_ms,
            created_at=_now(),
        )
        self._append_event(task, EV_SUBMITTED, {"session": task.session})
        with self._tasks_lock:
            self._tasks[task.id] = task
        self._queue.put(task)
        self._persist()
        logger.info(f"[KernelServe] 任务已提交: {task.id}")
        return self._task_summary(task)

    def task_list(self, session: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._tasks_lock:
            tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [self._task_summary(t) for t in tasks if session is None or t.session == session]

    def task_detail(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return self._task_summary(task, full=True)

    def task_events(self, task_id: str, after: int = 0) -> List[Dict[str, Any]]:
        """任务事件列表（SSE 回放：after=已消费条数）

        防御：FastAPI query 参数可能以 str 到达，slice 前强制 int。
        """
        try:
            after = int(after or 0)
        except (TypeError, ValueError):
            after = 0
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if task is None:
                return []
            return list(task.events[after:])

    def subscribe(self, task_id: str) -> "queue.Queue":
        """订阅实时事件（返回队列；结束必须 unsubscribe）"""
        q: "queue.Queue" = queue.Queue(maxsize=256)
        with self._sub_lock:
            self._subscribers.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id: str, q: "queue.Queue") -> None:
        with self._sub_lock:
            subs = self._subscribers.get(task_id)
            if subs:
                subs.discard(q)
                if not subs:
                    self._subscribers.pop(task_id, None)

    def snapshot(self) -> Optional[str]:
        """手动/自动快照当前内核状态到 state_dir/kernel-state.dill"""
        km = self._ensure_kernel()
        if km is None:
            return None
        result = km.snapshot_state(self._snapshot_path)
        if result is None:
            logger.warning("[KernelServe] 快照失败（内核未运行或解析失败）")
            return None
        logger.info(f"[KernelServe] 快照已保存: {result.path} ({result.bytes} bytes, {len(result.saved)} 变量)")
        return result.path

    def restore_kernel_state(self) -> bool:
        """从 state_dir 快照恢复内核命名空间（文件缺失/损坏容忍）"""
        km = self._ensure_kernel()
        if km is None:
            return False
        p = Path(self._snapshot_path)
        if not p.exists():
            return False
        result = km.restore_state(str(p))
        if result is None:
            return False
        self._append_global_event(EV_RESTORED, {"restored": result.restored, "path": str(p)})
        logger.info(f"[KernelServe] 快照恢复: {len(result.restored)} 变量 -> {p.name}")
        return True

    def restart_kernel(self) -> Dict[str, Any]:
        """重启内核（清空命名空间；持久化任务记录不受影响）"""
        km = self._ensure_kernel()
        if km is None:
            return {"success": False, "message": "内核未初始化"}
        km.shutdown()
        km.start()
        logger.info("[KernelServe] 内核已重启")
        return {"success": True, "message": "内核已重启（命名空间已清空）"}

    def status(self) -> Dict[str, Any]:
        km = self._kernel
        kernel_state = None
        if km is not None:
            try:
                kernel_state = {
                    "running": km.is_running,
                    "session_dir": str(km.get_session_dir()),
                }
            except Exception:
                kernel_state = {"running": False}
        with self._tasks_lock:
            by_status: Dict[str, int] = {}
            for t in self._tasks.values():
                by_status[t.status] = by_status.get(t.status, 0) + 1
        return {
            "service": "kernel-serve",
            "running": self.is_running or self._started,
            "kernel": kernel_state,
            "tasks_total": len(self._tasks),
            "tasks_by_status": by_status,
            "snapshot": self._snapshot_path if Path(self._snapshot_path).exists() else None,
        }

    # ========== 内部 ==========

    def _ensure_kernel(self) -> Optional[KernelManager]:
        """懒启动真实内核（未注入 kernel 时）"""
        if self._kernel is None:
            self._kernel = KernelManager(session_dir=str(self._state_dir))
        if not self._kernel.is_running:
            try:
                self._kernel.start()
            except Exception as e:
                logger.error(f"[KernelServe] 内核启动失败: {e}")
                return None
        return self._kernel

    def _run_worker(self) -> None:
        """串行执行队列任务"""
        while self._started:
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if task is None:
                break
            try:
                self._execute_task(task)
            except Exception as e:
                logger.exception(f"[KernelServe] 任务执行异常: {task.id}: {e}")
                self._append_event(task, EV_ERROR, {"error": str(e)})
                task.status = ST_ERROR
                task.finished_at = _now()
                self._persist()

    def _execute_task(self, task: TaskRecord) -> None:
        km = self._ensure_kernel()
        if km is None:
            task.status = ST_ERROR
            task.finished_at = _now()
            self._append_event(task, EV_ERROR, {"error": "内核不可用"})
            self._persist()
            return

        task.status = ST_RUNNING
        task.started_at = _now()
        self._append_event(task, EV_STARTED, {})

        def _on_stream(name: str, text: str) -> None:
            # 实时流事件（SSE 推送；事件列表仅保留摘要防膨胀）
            self._push_event(task.id, EV_STREAM, {"name": name, "piece": text[:2000]})

        r = km.execute(
            task.code,
            timeout_ms=task.timeout_ms,
            on_stream=_on_stream,
        )
        result = {
            "stdout": r.stdout,
            "stderr": r.stderr,
            "result": r.result,
            "status": r.status,
            "duration_ms": r.duration_ms,
            "error": (
                {"ename": r.error.ename, "evalue": r.error.evalue}
                if r.error is not None
                else None
            ),
        }
        task.result = result
        task.finished_at = _now()
        if r.status == "ok":
            task.status = ST_DONE
            self._append_event(task, EV_RESULT, {"result": r.result})
            self._append_event(task, EV_FINISHED, {"duration_ms": r.duration_ms})
        elif r.status == "aborted":
            task.status = ST_ABORTED
            self._append_event(task, EV_ABORTED, {"duration_ms": r.duration_ms})
        else:
            task.status = ST_ERROR
            self._append_event(
                task,
                EV_ERROR,
                {
                    "ename": r.error.ename if r.error else "",
                    "evalue": r.error.evalue if r.error else r.stderr[:500],
                },
            )

        # 任务完成自动快照（供快照下载/重启恢复）
        if self._auto_snapshot:
            snap = self.snapshot()
            if snap:
                task.snapshot_path = snap
                self._append_event(task, EV_SNAPSHOT, {"path": snap})
        self._persist()

    def _push_event(self, task_id: str, ev_type: str, data: Dict[str, Any]) -> None:
        """广播实时事件给该任务的所有订阅者（不落任务事件列表）"""
        with self._sub_lock:
            subs = list(self._subscribers.get(task_id, ()))
        if not subs:
            return
        payload = {"type": ev_type, "data": data}
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # 订阅者消费过慢：淘汰最旧一条（SSE 端保证最终一致）
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass

    def _append_event(self, task: TaskRecord, ev_type: str, data: Dict[str, Any]) -> None:
        """任务事件落列表（回放）+ 实时广播"""
        event = {"ts": _now(), "type": ev_type, "data": data}
        task.events.append(event)
        if len(task.events) > MAX_EVENTS_PER_TASK:
            task.events = task.events[-MAX_EVENTS_PER_TASK:]
        self._push_event(task.id, ev_type, data)

    def _append_global_event(self, ev_type: str, data: Dict[str, Any]) -> None:
        # 全局事件（如快照恢复）广播给当前所有任务订阅者
        with self._sub_lock:
            all_subs = set()
            for subs in self._subscribers.values():
                all_subs.update(subs)
        payload = {"type": ev_type, "data": data}
        for q in all_subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass

    def _task_summary(self, task: TaskRecord, full: bool = False) -> Dict[str, Any]:
        summary = {
            "id": task.id,
            "session": task.session,
            "status": task.status,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "timeout_ms": task.timeout_ms,
        }
        if full:
            summary["code"] = task.code
            summary["result"] = task.result
            summary["events"] = list(task.events)
            summary["snapshot_path"] = task.snapshot_path
        return summary

    # ========== 持久化 ==========

    def _tasks_file(self) -> Path:
        return self._state_dir / "tasks.json"

    def _persist(self) -> None:
        """原子写 tasks.json（tmp + os.replace）"""
        try:
            with self._tasks_lock:
                payload = [t.to_json() for t in self._tasks.values()]
            tmp = self._tasks_file().with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            tmp.replace(self._tasks_file())
        except Exception as e:
            logger.warning(f"[KernelServe] 任务持久化失败: {e}")

    def _load_tasks(self) -> None:
        """载入历史任务记录；未完成任务状态重置为 queued（重启继续）"""
        f = self._tasks_file()
        if not f.exists():
            return
        try:
            with open(f, encoding="utf-8") as fh:
                raw = json.load(fh)
            with self._tasks_lock:
                for data in raw:
                    task = TaskRecord.from_json(data)
                    if task.status in _ST_ACTIVE:
                        task.status = ST_QUEUED
                    self._tasks[task.id] = task
            logger.info(f"[KernelServe] 已载入 {len(raw)} 条历史任务")
        except Exception as e:
            logger.warning(f"[KernelServe] 任务载入失败: {e}")

    def _requeue_unfinished(self) -> List[str]:
        """把未完成任务重新入队（重启恢复），返回任务 id 列表"""
        ids: List[str] = []
        with self._tasks_lock:
            pending = [t for t in self._tasks.values() if t.status == ST_QUEUED]
        for t in pending:
            self._queue.put(t)
            ids.append(t.id)
        return ids