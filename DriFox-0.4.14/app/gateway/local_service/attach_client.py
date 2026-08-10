# -*- coding: utf-8 -*-
"""
RLM 常驻服务 attach 客户端（L4）

UI 窗口 / 测试脚本通过本客户端连接 --serve 常驻服务：
- status/task 查询、任务提交
- attach(task_id)：SSE 回放历史事件 + 实时推送（后台线程，事件回调）
- detach()：断开连接，**不影响服务端任务继续**（关闭窗口仅 detach）

无 Qt 依赖，可用在 UI 进程或独立脚本。事件格式：
    {"ts": "...", "type": "task_submitted|task_started|task_stream|task_result|task_finished|task_error|task_aborted|task_snapshot|kernel_restored", "data": {...}}
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Optional

import requests
from loguru import logger


class KernelAttachError(RuntimeError):
    """服务连接/协议错误"""


class KernelAttachClient:
    """RLM 常驻服务 HTTP/SSE 客户端

    用法：
        c = KernelAttachClient(port=8765)
        c.attach(task_id, on_event=lambda ev: print(ev))
        ...
        c.detach()   # 仅断开，服务端任务继续
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        timeout: float = 10.0,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.on_event = on_event
        self.on_error = on_error
        self._base = f"http://{host}:{port}"
        self._attached = False
        self._attach_task_id: Optional[str] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._session = requests.Session()

    # ========== 查询/提交 ==========

    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        try:
            r = self._session.request(method, f"{self._base}{path}", timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise KernelAttachError(f"连接服务失败（{self.host}:{self.port}）: {e}") from e
        if r.status_code == 503:
            raise KernelAttachError("服务未启动或未挂载 RLM 内核（需 --serve 模式）")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text[:200]
            raise KernelAttachError(f"HTTP {r.status_code}: {detail}")
        return r.json()

    def status(self) -> Dict[str, Any]:
        """服务/内核/任务统计状态"""
        return self._request("GET", "/kernel/status")["status"]

    def list_tasks(self, session: Optional[str] = None) -> list:
        return self._request("GET", "/kernel/tasks", params={"session": session} if session else None)["tasks"]

    def task_detail(self, task_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/kernel/tasks/{task_id}")["task"]

    def submit(self, code: str, timeout_ms: Optional[int] = None, session: str = "default") -> Dict[str, Any]:
        """提交内核任务（返回任务摘要）"""
        return self._request(
            "POST", "/kernel/tasks", json={"code": code, "timeout_ms": timeout_ms, "session": session}
        )["task"]

    def trigger_snapshot(self) -> str:
        return self._request("POST", "/kernel/snapshot")["snapshot_path"]

    def restore(self) -> bool:
        return self._request("POST", "/kernel/restore")["success"]

    def restart(self) -> Dict[str, Any]:
        return self._request("POST", "/kernel/restart")

    def download_snapshot(self, dest: Optional[str] = None) -> str:
        """下载内核快照 .dill 到本地（默认当前目录 kernel-state.dill）"""
        import os

        dest = dest or "kernel-state.dill"
        r = self._session.get(f"{self._base}/kernel/snapshot", timeout=self.timeout)
        if r.status_code == 404:
            raise KernelAttachError("尚无快照（先提交任务或 POST /kernel/snapshot）")
        if r.status_code >= 400:
            raise KernelAttachError(f"HTTP {r.status_code}: {r.text[:200]}")
        with open(dest, "wb") as fh:
            fh.write(r.content)
        return os.path.abspath(dest)

    # ========== attach / detach ==========

    @property
    def attached(self) -> bool:
        return self._attached

    @property
    def attach_task_id(self) -> Optional[str]:
        return self._attach_task_id

    def attach(
        self,
        task_id: str,
        after: int = 0,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        """attach 到任务事件流（回放 after 之后历史 + 实时）

        另起后台线程读取 SSE；detach() 断开连接。连接/读取失败回调 on_error。
        """
        if self._attached:
            raise KernelAttachError(f"已 attach 到任务 {self._attach_task_id}，先 detach")
        cb = on_event or self.on_event
        eb = on_error or self.on_error
        self._stop_event.clear()
        self._attached = True
        self._attach_task_id = task_id
        self._thread = threading.Thread(
            target=self._read_sse, args=(task_id, after, cb, eb), name="kernel-attach", daemon=True
        )
        self._thread.start()

    def detach(self) -> None:
        """断开（仅 detach 不杀服务；服务端任务继续执行）"""
        self._stop_event.set()
        self._attached = False
        self._attach_task_id = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _read_sse(
        self,
        task_id: str,
        after: int,
        cb: Optional[Callable[[Dict[str, Any]], None]],
        eb: Optional[Callable[[str], None]],
    ) -> None:
        try:
            with self._session.get(
                f"{self._base}/kernel/tasks/{task_id}/events",
                params={"after": after},
                stream=True,
                timeout=(self.timeout, 30.0),
            ) as r:
                if r.status_code >= 400:
                    raise KernelAttachError(f"attach 失败 HTTP {r.status_code}: {r.text[:200]}")
                for raw in r.iter_lines(decode_unicode=True):
                    if self._stop_event.is_set():
                        break
                    if not raw or not raw.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(raw[6:])
                    except Exception as e:
                        logger.warning(f"[KernelAttach] 事件解析失败: {e}")
                        continue
                    if cb:
                        try:
                            cb(ev)
                        except Exception as e:
                            logger.warning(f"[KernelAttach] on_event 回调异常: {e}")
        except requests.RequestException as e:
            if not self._stop_event.is_set():
                _err = f"SSE 连接中断: {e}"
                logger.warning(f"[KernelAttach] {_err}")
                if eb:
                    eb(_err)
        except Exception as e:
            _err = f"attach 异常: {e}"
            logger.warning(f"[KernelAttach] {_err}")
            if eb:
                eb(_err)
        finally:
            self._attached = False
            self._attach_task_id = None

    def wait_event(
        self,
        task_id: str,
        wanted: str,
        timeout: float = 60.0,
        after: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """阻塞等待指定类型事件（冒烟/测试便捷方法）"""
        box: Dict[str, Any] = {}
        stop = threading.Event()

        def _cb(ev):
            if ev.get("type") == wanted:
                box["ev"] = ev
                stop.set()

        self.attach(task_id, after=after, on_event=_cb)
        try:
            if not stop.wait(timeout):
                return None
            return box.get("ev")
        finally:
            self.detach()


def wait_for_server(host: str = "127.0.0.1", port: int = 8765, timeout: float = 20.0) -> bool:
    """等待服务端口就绪（--serve 启动探测）"""
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False