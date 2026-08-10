# -*- coding: utf-8 -*-
"""
Python 执行工具集 — 持久 RLM 内核（L3）

提供 make_python：在跨轮持久的 ipykernel 中执行 Python 代码，
变量/函数/import 在多次调用间保留（工作层上下文模型）。

与 bash/terminal 工具并行（L3 已确认：不接管现有工具）。
内核实例懒启动，挂在 BuiltinTools 上随会话共享；失败时自动重建一次。
"""
import time
from pathlib import Path
from typing import Dict, Optional

from loguru import logger

from app.core.kernel_manager import KernelManager, ExecuteResult
from app.tools.result import ToolResult


class KernelTools:
    def __init__(self, owner):
        self._owner = owner
        self._kernel: Optional[KernelManager] = None
        self._started = False
        self._session_dir: Optional[Path] = None

    # ========== 内核生命周期 ==========

    def _ensure_kernel(self) -> KernelManager:
        """懒启动内核（首次调用 / 内核死亡后重建一次）"""
        if self._kernel is not None and self._kernel.is_running:
            return self._kernel
        if self._started:
            # 上次已启动过但死了：重建（仅一次）
            logger.warning("[KernelTools] 内核已退出，重建")
            self._started = False
        km = KernelManager()
        km.start()
        self._kernel = km
        self._started = True
        logger.info("[KernelTools] RLM 内核已启动")
        return km

    def kernel_status(self) -> dict:
        """查询内核状态（供 UI/调试）"""
        running = self._kernel is not None and self._kernel.is_running
        return {
            "running": running,
            "started": self._started,
            "session_dir": str(self._kernel.get_session_dir()) if self._kernel else None,
        }

    def kernel_restart(self) -> ToolResult:
        """重启内核（清空工作层状态）"""
        if self._kernel is not None:
            self._kernel.shutdown()
        self._kernel = None
        self._started = False
        km = self._ensure_kernel()
        return ToolResult(True, content=f"内核已重启（session: {km.get_session_dir()}）")

    def kernel_snapshot(self, path: Optional[str] = None) -> ToolResult:
        """快照当前工作层状态到会话目录（可指定路径）"""
        km = self._ensure_kernel()
        snap = km.snapshot_state(path)
        if snap is None:
            return ToolResult(False, error="快照失败（内核未运行或序列化错误）")
        saved = ", ".join(snap.saved[:20]) if snap.saved else "(无)"
        skipped = len(snap.skipped)
        return ToolResult(
            True,
            content=f"快照已保存: {snap.path}\n变量 {len(snap.saved)} 个: {saved}\n跳过 {skipped} 个不可序列化",
        )

    def kernel_restore(self, path: Optional[str] = None) -> ToolResult:
        """从快照恢复工作层状态（默认本会话目录，可指定外部快照路径）"""
        km = self._ensure_kernel()
        rest = km.restore_state(path)
        if rest is None:
            return ToolResult(False, error="恢复失败（快照不存在或损坏）")
        restored = ", ".join(rest.restored[:20]) if rest.restored else "(无)"
        failed = len(rest.failed)
        return ToolResult(
            True,
            content=f"恢复完成: {rest.path}\n恢复变量 {len(rest.restored)} 个: {restored}\n失败 {failed} 个",
        )

    # ========== 工具入口 ==========

    def make_python(self, code: str, timeout_ms: Optional[int] = None) -> ToolResult:
        """在持久 Python 内核中执行代码，变量/函数/import 跨轮保留。

        Args:
            code: Python 代码
            timeout_ms: 超时毫秒（默认 None=不超时；建议长任务显式设置）
        Returns:
            ToolResult: content 为 stdout + 最后表达式结果；error 时含异常信息
        """
        if not code or not code.strip():
            return ToolResult(False, error="No code provided")
        km = self._ensure_kernel()
        try:
            result: ExecuteResult = km.execute(code, timeout_ms=timeout_ms)
        except Exception as e:
            logger.exception("[KernelTools] 内核执行异常")
            self._started = False
            return ToolResult(False, error=f"内核执行异常: {e}")

        if result.status == "ok":
            parts = []
            if result.stdout:
                parts.append(result.stdout)
            if result.result is not None:
                text = result.result
                if len(text) > 20000:
                    text = text[:20000] + "\n[... 结果截断 ...]"
                parts.append(text)
            content = "\n".join(parts).strip()
            return ToolResult(True, content=content or "(无输出)")
        if result.status == "aborted":
            return ToolResult(False, error=f"执行超时已中止（{result.duration_ms}ms）")
        # error
        err = result.error
        detail = f"{err.ename}: {err.evalue}" if err else (result.stderr or "unknown error")
        return ToolResult(False, error=detail)

    def cleanup(self):
        """释放内核（会话结束调用）"""
        if self._kernel is not None:
            try:
                self._kernel.shutdown()
            except Exception as e:
                logger.warning(f"[KernelTools] 内核清理异常: {e}")
            self._kernel = None
        self._started = False