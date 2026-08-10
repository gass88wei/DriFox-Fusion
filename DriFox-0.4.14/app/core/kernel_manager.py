# -*- coding: utf-8 -*-
"""
RLM 执行内核管理器 — 持久 ipykernel + jupyter_client + dill 快照（L3）

借鉴 prime-agent `packages/coding-agent/src/core/kernel/index.ts` 的设计：
- 启动 ipykernel 子进程，通过 jupyter_client 连接 shell/iopub 通道
- execute(code)：串行执行，收集 stdout/stderr/result，超时可中断（control 通道）
- 快照/恢复：dill 逐变量序列化到 .dill 文件 + .json manifest（单变量失败跳过不中断）
- host bridge：kernel 内 Python 代码通过 Jupyter Comm(target_name="host.request")
  向宿主进程发类型化请求，宿主注册 handler 后异步回复

与 prime 的差异（适配 DriFox）：
- 用 jupyter_client 而非手写 ZMQ 协议
- 快照仅在本会话目录内（不跨会话自动持久化，由上层调用方决定何时 snapshot/restore）
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

try:
    from jupyter_client import KernelManager as JupyterKernelManager
    from jupyter_client import BlockingKernelClient
except Exception as e:  # pragma: no cover - 依赖未安装时仅记录
    JupyterKernelManager = None  # type: ignore[assignment]
    BlockingKernelClient = None  # type: ignore[assignment]
    logger.warning(f"[KernelManager] jupyter_client 不可用: {e}")

# Comm target：kernel 侧 host_request shim 打开的通道名（与 prime 一致）
HOST_COMM_TARGET = "host.request"
# 快照/恢复结果 marker 行前缀
RESULT_MARKER = "__DRIFOX_KERNEL_STATE__"
# 默认 stdout/stderr/result 字符上限
DEFAULT_MAX_OUTPUT_CHARS = 65536
# 内核就绪等待上限
READY_TIMEOUT_MS = 15000
# 中断后等待强制放弃的宽限（毫秒）
KERNEL_ABORT_GRACE_MS = 2000


@dataclass
class ExecuteError:
    ename: str
    evalue: str
    traceback: List[str]


@dataclass
class ExecuteResult:
    stdout: str = ""
    stderr: str = ""
    result: Optional[str] = None
    status: str = "ok"  # ok | error | aborted
    error: Optional[ExecuteError] = None
    duration_ms: int = 0

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


@dataclass
class SnapshotResult:
    saved: List[str] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    bytes: int = 0
    path: str = ""


@dataclass
class RestoreResult:
    restored: List[str] = field(default_factory=list)
    failed: List[Dict[str, str]] = field(default_factory=list)
    path: str = ""


# host request handler 类型：payload(dict) -> dict
HostRequestHandler = Callable[[Dict[str, Any]], Any]


def _py_str(value: str) -> str:
    """把 Python 字符串渲染为 Python 字符串字面量（JSON 转义是合法子集）"""
    return json.dumps(value)


def build_snapshot_code(out_path: str, manifest_path: str, max_bytes: int = 256 * 1024 * 1024) -> str:
    """将内核用户命名空间逐变量 dill 序列化到 out_path（原子写）+ manifest，打印 marker 行"""
    return f"""
def _drifox_snapshot_state():
    import builtins as _b, json, os, sys, datetime
    try:
        import dill
    except _b.Exception as _err:
        _b.print({_py_str(RESULT_MARKER)} + json.dumps({{"error": "dill unavailable: " + _b.str(_err)}}))
        return
    dill.settings["recurse"] = True
    ip = None
    try:
        ip = get_ipython()  # noqa: F821 (injected by IPython)
    except _b.Exception:
        ip = None
    ns = ip.user_ns if ip is not None else _b.globals()
    hidden = _b.set(_b.getattr(ip, "user_ns_hidden", {{}}) or {{}}) if ip is not None else _b.set()
    always_skip = {{"In", "Out", "get_ipython", "exit", "quit", "open", "_ih", "_oh"}}
    payload = {{}}
    skipped = []
    total = 0
    for name in _b.list(ns.keys()):
        if name.startswith("_") or name in hidden or name in always_skip:
            continue
        value = ns[name]
        try:
            blob = dill.dumps(value)
        except _b.Exception as _err:
            skipped.append({{"name": name, "reason": _b.type(_err).__name__ + ": " + _b.str(_err)[:200]}})
            continue
        if _b.len(blob) > {max_bytes} or total + _b.len(blob) > {max_bytes}:
            skipped.append({{"name": name, "reason": "exceeds snapshot size cap"}})
            continue
        payload[name] = blob
        total += _b.len(blob)
    os.makedirs(os.path.dirname({_py_str(out_path)}), exist_ok=True)
    tmp = {_py_str(out_path)} + ".tmp"
    try:
        with _b.open(tmp, "wb") as fh:
            dill.dump(payload, fh)
        os.replace(tmp, {_py_str(out_path)})
    except _b.Exception as _err:
        try:
            os.remove(tmp)
        except _b.Exception:
            pass
        _b.print({_py_str(RESULT_MARKER)} + json.dumps({{"error": "write failed: " + _b.str(_err)}}))
        return
    bytes_written = os.path.getsize({_py_str(out_path)})
    saved = _b.sorted(payload.keys())
    manifest = {{
        "version": 1,
        "savedNames": saved,
        "skipped": skipped,
        "bytes": bytes_written,
        "pythonVersion": sys.version.split()[0],
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }}
    try:
        with _b.open({_py_str(manifest_path)}, "w") as fh:
            json.dump(manifest, fh)
    except _b.Exception:
        pass
    _b.print({_py_str(RESULT_MARKER)} + json.dumps({{"saved": saved, "skipped": skipped, "bytes": bytes_written}}))


try:
    _drifox_snapshot_state()
finally:
    del _drifox_snapshot_state
""".strip()


def build_restore_code(in_path: str) -> str:
    """从 .dill 快照恢复命名空间到内核（逐变量，容忍缺失/损坏），打印 marker 行"""
    return f"""
def _drifox_restore_state():
    import builtins as _b, json, os
    if not os.path.exists({_py_str(in_path)}):
        _b.print({_py_str(RESULT_MARKER)} + json.dumps({{"restored": [], "failed": []}}))
        return
    try:
        import dill
    except _b.Exception as _err:
        _b.print({_py_str(RESULT_MARKER)} + json.dumps({{"restored": [], "failed": [], "error": "dill unavailable: " + _b.str(_err)}}))
        return
    try:
        with _b.open({_py_str(in_path)}, "rb") as fh:
            payload = dill.load(fh)
    except _b.Exception as _err:
        _b.print({_py_str(RESULT_MARKER)} + json.dumps({{"restored": [], "failed": [], "error": "load failed: " + _b.str(_err)}}))
        return
    if not _b.isinstance(payload, _b.dict):
        _b.print({_py_str(RESULT_MARKER)} + json.dumps({{"restored": [], "failed": [], "error": "corrupt snapshot: not a dict"}}))
        return
    ip = None
    try:
        ip = get_ipython()  # noqa: F821
    except _b.Exception:
        ip = None
    ns = ip.user_ns if ip is not None else _b.globals()
    restored = []
    failed = []
    for name, blob in payload.items():
        try:
            ns[name] = dill.loads(blob)
            restored.append(name)
        except _b.Exception as _err:
            failed.append({{"name": name, "reason": _b.type(_err).__name__ + ": " + _b.str(_err)[:200]}})
    _b.print({_py_str(RESULT_MARKER)} + json.dumps({{"restored": _b.sorted(restored), "failed": failed}}))


try:
    _drifox_restore_state()
finally:
    del _drifox_restore_state
""".strip()


def _parse_marker_line(stdout: str) -> Optional[Dict[str, Any]]:
    """从 cell stdout 提取 marker 行并解析 JSON，失败返回 None"""
    idx = stdout.rfind(RESULT_MARKER)
    if idx == -1:
        return None
    rest = stdout[idx + len(RESULT_MARKER):]
    line = rest.split("\n", 1)[0].strip() if rest else ""
    if not line:
        return None
    try:
        data = json.loads(line)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str)]
    return []


def _as_reason_list(value: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                out.append({"name": entry["name"], "reason": str(entry.get("reason") or "")})
    return out


class KernelManager:
    """RLM 执行内核管理器（持久 ipykernel，状态跨轮）

    用法：
        km = KernelManager()
        km.start()
        r1 = km.execute("x = 42")        # ok
        r2 = km.execute("x + 1")         # result = "43"（跨轮状态）
        km.snapshot_state(path)          # dill 快照
        km.shutdown()
    """

    def __init__(
        self,
        python: Optional[str] = None,
        cwd: Optional[str] = None,
        session_dir: Optional[str] = None,
        host_handlers: Optional[Dict[str, HostRequestHandler]] = None,
        kernel_name: str = "python3",
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ):
        if JupyterKernelManager is None:
            raise RuntimeError("jupyter_client 未安装（L3 依赖缺失）")
        self._python = python or sys.executable
        self._cwd = cwd
        self._session_dir = Path(session_dir) if session_dir else Path(tempfile.mkdtemp(prefix="drifox-kernel-"))
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._host_handlers: Dict[str, HostRequestHandler] = dict(host_handlers or {})
        self._kernel_name = kernel_name
        self._max_output_chars = max_output_chars

        self._manager: Optional[JupyterKernelManager] = None
        self._client: Optional[BlockingKernelClient] = None
        self._started = False
        self._lock = threading.Lock()  # execute 串行化
        self._comm_targets: Dict[str, str] = {}
        self._handled_comm_ids: set = set()
        self._connection_file: Optional[str] = None
        self._kernel_proc: Optional[subprocess.Popen] = None
        # Windows 上 signal 中断后，紧随的下一个 execute 会被内核静默丢弃；
        # abort 后置位，下次 execute 先发 sentinel 排空幽灵请求
        self._drain_needed = False

    # ========== 生命周期 ==========

    @property
    def is_running(self) -> bool:
        return self._started and self._client is not None and self._client.is_alive()

    def start(self, timeout_ms: int = READY_TIMEOUT_MS) -> None:
        """启动 ipykernel 子进程并建立 jupyter_client 连接"""
        if self._started:
            return
        if JupyterKernelManager is None:
            raise RuntimeError("jupyter_client 未安装（L3 依赖缺失）")

        # 用 jupyter_client 的 KernelManager 管理进程 + 连接文件
        self._manager = JupyterKernelManager(kernel_name=self._kernel_name)
        self._manager.start_kernel(cwd=self._cwd)
        self._started = True

        self._client = self._manager.client()  # type: ignore[attr-defined]
        self._client.start_channels()
        try:
            self._client.wait_for_ready(timeout=timeout_ms / 1000.0)
        except Exception:
            self.shutdown()
            raise
        logger.info(f"[KernelManager] 内核就绪: {self._kernel_name}")

    def shutdown(self) -> None:
        """终止内核并清理通道"""
        if self._client is not None:
            try:
                self._client.stop_channels()
            except Exception:
                pass
            self._client = None
        if self._manager is not None:
            try:
                self._manager.shutdown_kernel(now=True)
            except Exception:
                try:
                    self._manager.kill_kernel()
                except Exception:
                    pass
            self._manager = None
        self._started = False

    # ========== 执行 ==========

    def execute(
        self,
        code: str,
        timeout_ms: Optional[int] = None,
        max_output_chars: Optional[int] = None,
        on_stream: Optional[Callable[[str, str], None]] = None,
    ) -> ExecuteResult:
        """串行执行代码，收集 stdout/stderr/result

        timeout_ms 超时后发 interrupt_request（control 通道），
        宽限 KERNEL_ABORT_GRACE_MS 后仍 busy 则强制标记 aborted。

        注：Windows 上 signal 中断生效瞬间会静默吞掉排队中的下一个
        execute_request（busy→idle 但无 execute_input）。因此：
        1. abort 后置 _drain_needed，下次 execute 先发 sentinel 排空；
        2. 若本次请求本身被吞（idle 前无 execute_input），自动重发一次。
        """
        if not self._started or self._client is None:
            raise RuntimeError("Kernel 未启动，先调用 start()")
        cap = max_output_chars or self._max_output_chars
        started_at = time.monotonic()

        with self._lock:
            client = self._client
            # abort 后先排空幽灵请求（Windows signal 中断吞掉下一个 execute）
            if self._drain_needed:
                self._drain_after_abort()
            for attempt in range(2):
                result, swallowed = self._execute_once(
                    client, code, cap, timeout_ms, on_stream
                )
                if swallowed:
                    # 被内核静默吞掉（中断副作用）：自动重发一次
                    logger.warning("[KernelManager] execute 被内核吞掉，自动重发一次")
                    continue
                result.duration_ms = int((time.monotonic() - started_at) * 1000)
                return result
            # 理论不可达：最多重发一次
            result.duration_ms = int((time.monotonic() - started_at) * 1000)
            return result

    def _execute_once(
        self,
        client: BlockingKernelClient,
        code: str,
        cap: int,
        timeout_ms: Optional[int],
        on_stream: Optional[Callable[[str, str], None]],
    ) -> tuple[ExecuteResult, bool]:
        """单次 execute_request：发送并收集消息直到该请求 idle

        Returns:
            (result, swallowed)：swallowed=True 表示收到本请求 idle 但从未见
            execute_input（被内核静默丢弃），调用方应重发。
        """
        msg_id = client.execute(code, silent=False, store_history=True)
        result = ExecuteResult(duration_ms=0)
        stdout_parts: List[str] = []
        stderr_parts: List[str] = []
        stdout_truncated = False
        stderr_truncated = False
        aborted = False
        saw_input = False
        deadline = time.monotonic() + (timeout_ms / 1000.0) if timeout_ms else None
        interrupt_sent = False

        while True:
            # 超时：发中断（仅一次），再宽限
            if deadline is not None and time.monotonic() > deadline:
                if not interrupt_sent:
                    try:
                        if self._manager is not None:
                            self._manager.interrupt_kernel()
                        else:
                            # 无 manager（仅 client）时手工发 interrupt_request（control 通道）
                            msg = self._client.session.msg("interrupt_request", content={})
                            self._client.control_channel.send(msg)
                    except Exception as e:
                        logger.warning(f"[KernelManager] interrupt_kernel 失败: {e}")
                    interrupt_sent = True
                    deadline = time.monotonic() + KERNEL_ABORT_GRACE_MS / 1000.0
                    continue
                aborted = True
                result.status = "aborted"
                self._drain_needed = True
                break

            try:
                msg = client.get_iopub_msg(timeout=0.5)
            except queue.Empty:
                continue
            except Exception:
                # 通道中断（内核死亡等）
                result.stderr = "kernel channel closed unexpectedly: kernel may have died"
                if result.status == "ok":
                    result.status = "error"
                break

            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                # 其他请求的消息（如 host comm 回复无关 execute），但仍需处理 comm
                if msg.get("msg_type") in ("comm_open", "comm_msg", "comm_close"):
                    self._handle_comm_message(msg)
                continue

            msg_type = msg.get("msg_type")
            content = msg.get("content", {})
            if msg_type == "execute_input":
                saw_input = True
            elif msg_type == "stream":
                text = str(content.get("text") or "")
                name = content.get("name", "stdout")
                if name == "stderr":
                    if len("".join(stderr_parts)) < cap:
                        stderr_parts.append(text)
                        if len("".join(stderr_parts)) > cap:
                            stderr_parts = ["".join(stderr_parts)[:cap]]
                            stderr_truncated = True
                else:
                    if len("".join(stdout_parts)) < cap:
                        stdout_parts.append(text)
                        if len("".join(stdout_parts)) > cap:
                            stdout_parts = ["".join(stdout_parts)[:cap]]
                            stdout_truncated = True
                if on_stream:
                    on_stream(text, name)
            elif msg_type == "execute_result":
                data = content.get("data", {})
                if isinstance(data, dict) and data.get("text/plain"):
                    result.result = str(data["text/plain"])
            elif msg_type == "error":
                result.status = "error"
                result.error = ExecuteError(
                    ename=str(content.get("ename") or ""),
                    evalue=str(content.get("evalue") or ""),
                    traceback=[str(t) for t in (content.get("traceback") or [])],
                )
            elif msg_type == "status":
                if content.get("execution_state") == "idle":
                    break
            elif msg_type in ("comm_open", "comm_msg", "comm_close"):
                self._handle_comm_message(msg)

        result.stdout = "".join(stdout_parts)
        result.stderr = "".join(stderr_parts)
        if stdout_truncated:
            result.stdout += f"\n[... output truncated at {cap} chars ...]"
        if stderr_truncated:
            result.stderr += f"\n[... output truncated at {cap} chars ...]"
        if result.result is not None and len(result.result) > cap:
            result.result = f"{result.result[:cap]}\n[... output truncated at {cap} chars ...]"
        # 被吞判定：正常收到本请求 idle 但从未见 execute_input（中断副作用吞请求）
        swallowed = (
            result.status == "ok"
            and not saw_input
            and "kernel channel closed" not in result.stderr
        )
        return result, swallowed

    def _drain_after_abort(self, timeout_ms: int = 5000) -> None:
        """abort 后排空"幽灵请求"：发一个无副作用的 sentinel cell 并消费到 idle

        Windows 上 signal 中断生效后，内核会静默丢弃紧随其后的第一个
        execute_request（表现为 busy→idle 无 execute_input/result）。
        用 `pass` 作为 sentinel 消化该效应，保证后续真实 execute 不被吞。
        """
        if self._client is None:
            self._drain_needed = False
            return
        try:
            sentinel_id = self._client.execute("pass", silent=False, store_history=True)
        except Exception:
            self._drain_needed = False
            return
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            try:
                msg = self._client.get_iopub_msg(timeout=0.5)
            except queue.Empty:
                continue
            except Exception:
                break
            if msg.get("parent_header", {}).get("msg_id") != sentinel_id:
                continue
            if msg.get("msg_type") == "status" and msg.get("content", {}).get("execution_state") == "idle":
                break
        self._drain_needed = False

    # ========== 快照 / 恢复 ==========

    def snapshot_state(self, path: Optional[str] = None, max_bytes: int = 256 * 1024 * 1024) -> Optional[SnapshotResult]:
        """把当前命名空间 dill 快照到磁盘（best-effort，逐变量）"""
        if not self.is_running:
            return None
        out_path = str(Path(path) if path else self._session_dir / "kernel-state.dill")
        manifest_path = str(Path(out_path).with_suffix(".json"))
        r = self.execute(build_snapshot_code(out_path, manifest_path, max_bytes), timeout_ms=60000)
        if r.status != "ok":
            logger.warning(f"[KernelManager] 快照失败: {r.error.evalue if r.error else r.stderr}")
            return None
        data = _parse_marker_line(r.stdout)
        if not data or data.get("error"):
            logger.warning(f"[KernelManager] 快照结果解析失败: {r.stdout[-300:]}")
            return None
        return SnapshotResult(
            saved=_as_str_list(data.get("saved")),
            skipped=_as_reason_list(data.get("skipped")),
            bytes=int(data.get("bytes") or 0),
            path=out_path,
        )

    def restore_state(self, path: Optional[str] = None) -> Optional[RestoreResult]:
        """从快照恢复命名空间到内核（容忍缺失/损坏，不抛错）"""
        if not self.is_running:
            return None
        in_path = str(Path(path) if path else self._session_dir / "kernel-state.dill")
        r = self.execute(build_restore_code(in_path), timeout_ms=60000)
        if r.status != "ok":
            logger.warning(f"[KernelManager] 恢复失败: {r.error.evalue if r.error else r.stderr}")
            return None
        data = _parse_marker_line(r.stdout)
        if not data or data.get("error"):
            logger.warning(f"[KernelManager] 恢复结果解析失败: {r.stdout[-300:]}")
            return None
        return RestoreResult(
            restored=_as_str_list(data.get("restored")),
            failed=_as_reason_list(data.get("failed")),
            path=in_path,
        )

    # ========== host bridge ==========

    HOST_SHIM_CODE = r'''
"""kernel 内 host_request shim — 通过 Jupyter Comm 向宿主进程发起类型化请求（L3）

用法（在内核 cell 中）：
    await host_request("echo", {"value": 42})
"""
import asyncio

try:
    from ipykernel.comm import Comm
except Exception:  # pragma: no cover
    Comm = None

try:
    from IPython import get_ipython
except Exception:  # pragma: no cover
    get_ipython = None

__HOST_COMM_TARGET = "host.request"


def _install_control_comm_handlers():
    """让 comm 回复能在 execute_request 执行期间走 control 通道到达"""
    if get_ipython is None:
        return
    shell = get_ipython()
    kernel = getattr(shell, "kernel", None)
    comm_manager = getattr(kernel, "comm_manager", None)
    control_handlers = getattr(kernel, "control_handlers", None)
    if comm_manager is None or not isinstance(control_handlers, dict):
        return
    control_handlers.setdefault("comm_msg", comm_manager.comm_msg)
    control_handlers.setdefault("comm_close", comm_manager.comm_close)


async def host_request(request_type, payload=None):
    """向宿主进程发送类型化请求并等待回复

    Args:
        request_type: 请求类型（宿主侧通过 register_host_handler 注册）
        payload: 附加数据 dict
    Returns:
        宿主回复内容（不含 status 字段）
    Raises:
        RuntimeError: comm 不可用 / 宿主报错 / 未注册类型
        TypeError: 参数非法
    """
    if not isinstance(request_type, str) or not request_type:
        raise TypeError("request_type must be a non-empty str")
    if payload is not None and not isinstance(payload, dict):
        raise TypeError(f"payload must be a dict or None, got {type(payload).__name__}")
    if Comm is None:
        raise RuntimeError("Jupyter comm support is unavailable in this kernel")
    _install_control_comm_handlers()

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    comm = Comm(target_name=__HOST_COMM_TARGET, primary=False)

    def _on_msg(msg):
        content = msg.get("content", {})
        reply = content.get("data", {}) if isinstance(content, dict) else {}
        if not isinstance(reply, dict):
            return
        status = reply.get("status")

        def _resolve_result():
            if not future.done():
                future.set_result({k: v for k, v in reply.items() if k != "status"})
                comm.close()

        def _resolve_error(message):
            if not future.done():
                future.set_exception(RuntimeError(str(message)))
                comm.close()

        if status == "ok":
            loop.call_soon_threadsafe(_resolve_result)
        elif status == "error":
            loop.call_soon_threadsafe(lambda: _resolve_error(reply.get("error") or f"host request {request_type} failed"))
        else:
            loop.call_soon_threadsafe(lambda: _resolve_error(f"host request {request_type} returned unexpected status: {status!r}"))

    comm.on_msg(_on_msg)
    # request_type 放最后：payload 里的 "type" 键不能重定向请求
    comm.open(data={**(payload or {}), "type": request_type})
    return await future
'''

    def install_host_shim(self) -> ExecuteResult:
        """把 host_request shim 注入内核命名空间（幂等，重复注入直接覆盖同名定义）

        Returns:
            shim 注入 cell 的执行结果
        """
        r = self.execute(self.HOST_SHIM_CODE, timeout_ms=15000)
        if r.status != "ok":
            logger.warning(f"[KernelManager] host_request shim 注入失败: {r.stderr}")
        return r

    def register_host_handler(self, request_type: str, handler: HostRequestHandler) -> None:
        """注册 host request 类型处理器（kernel 内 host_request("<type>", {...}) 的宿主侧）"""
        self._host_handlers[request_type] = handler

    def _handle_comm_message(self, msg: Dict[str, Any]) -> None:
        """处理 ipykernel 发来的 comm_open/comm_msg（host.request 通道）"""
        msg_type = msg.get("msg_type")
        content = msg.get("content", {}) or {}
        comm_id = content.get("comm_id")
        if not isinstance(comm_id, str):
            return

        if msg_type == "comm_close":
            self._comm_targets.pop(comm_id, None)
            self._handled_comm_ids.discard(comm_id)
            return

        if msg_type == "comm_open":
            target = content.get("target_name")
            if not isinstance(target, str):
                return
            self._comm_targets[comm_id] = target
            if target == HOST_COMM_TARGET:
                self._start_host_request(comm_id, content.get("data"))
            return

        if msg_type == "comm_msg" and self._comm_targets.get(comm_id) == HOST_COMM_TARGET:
            self._start_host_request(comm_id, content.get("data"))

    def _start_host_request(self, comm_id: str, data: Any) -> None:
        """异步处理一次 host request：找 handler → 处理 → 回复 comm_msg"""
        if comm_id in self._handled_comm_ids:
            return
        self._handled_comm_ids.add(comm_id)

        def _worker() -> None:
            try:
                reply_payload = self._handle_host_request(data)
                self._send_comm_reply(comm_id, {"status": "ok", **(reply_payload or {})})
            except Exception as e:
                logger.warning(f"[KernelManager] host request 失败 (comm {comm_id}): {e}")
                self._send_comm_reply(comm_id, {"status": "error", "error": str(e)})

        threading.Thread(target=_worker, daemon=True).start()

    def _handle_host_request(self, data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise RuntimeError("host request payload must be an object")
        request_type = data.get("type")
        if not isinstance(request_type, str) or not request_type:
            raise RuntimeError("host request payload must have a string type")
        handler = self._host_handlers.get(request_type)
        if handler is None:
            raise RuntimeError(f'host request type "{request_type}" is not registered in this session')
        return handler(data)

    def _send_comm_reply(self, comm_id: str, payload: Dict[str, Any]) -> None:
        """向内核发送 comm_msg 回复（优先 control 通道——shell 通道被 execute_request 占用）

        与 prime 一致：回复先走 control；control 未连接时回退 shell。
        """
        if self._client is None:
            return
        try:
            msg = self._client.session.msg("comm_msg", content={"comm_id": comm_id, "data": payload})
            channel = self._client.control_channel if self._client.control_channel else self._client.shell_channel
            channel.send(msg)
        except Exception as e:
            logger.warning(f"[KernelManager] 发送 comm 回复失败: {e}")

    # ========== 辅助 ==========

    def get_session_dir(self) -> Path:
        return self._session_dir

    def __enter__(self) -> "KernelManager":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.shutdown()