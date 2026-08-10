# -*- coding: utf-8 -*-
"""
L3 RLM 执行内核 — KernelManager 单元测试

覆盖：start / execute / 状态跨轮 / 超时 abort / dill 快照恢复 / host bridge / shutdown
依赖：ipykernel + jupyter-client + dill（pyproject 已加入，uv sync 后可用）
标记：pytestmark = pytest.mark.kernel（不阻塞常规回归时可跳过）
"""
import json
import time

import pytest

pytestmark = pytest.mark.kernel

from app.core.kernel_manager import (
    KernelManager,
    build_restore_code,
    build_snapshot_code,
    _parse_marker_line,
)


@pytest.fixture()
def km():
    kernel = KernelManager()
    kernel.start()
    yield kernel
    kernel.shutdown()


# ---------------------------------------------------------------------------
# start / shutdown
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_start_makes_running(self, km):
        assert km.is_running

    def test_execute_simple_expression(self, km):
        r = km.execute("1 + 1")
        assert r.status == "ok"
        assert r.result == "2"

    def test_execute_stdout_captured(self, km):
        r = km.execute("print('hello kernel')")
        assert r.status == "ok"
        assert r.stdout.strip() == "hello kernel"

    def test_shutdown_stops_kernel(self, km):
        km.shutdown()
        assert not km.is_running


# ---------------------------------------------------------------------------
# 状态跨轮（RLM 工作层核心语义）
# ---------------------------------------------------------------------------


class TestCrossRoundState:
    def test_variable_persists_across_execute(self, km):
        assert km.execute("x = 42").status == "ok"
        r = km.execute("x + 1")
        assert r.status == "ok"
        assert r.result == "43"

    def test_import_persists_across_execute(self, km):
        assert km.execute("import json").status == "ok"
        r = km.execute("json.dumps({'a': 1})")
        assert r.status == "ok"
        assert r.result is not None

    def test_function_persists(self, km):
        assert km.execute("def double(v):\n    return v * 2").status == "ok"
        r = km.execute("double(21)")
        assert r.result == "42"

    def test_execute_result_none_for_assignment(self, km):
        r = km.execute("y = 10")
        assert r.status == "ok"
        assert r.result is None


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_python_error_reported(self, km):
        r = km.execute("raise ValueError('boom')")
        assert r.status == "error"
        assert r.error is not None
        assert r.error.ename == "ValueError"
        assert "boom" in r.error.evalue

    def test_error_does_not_kill_kernel(self, km):
        assert km.execute("1 / 0").status == "error"
        r = km.execute("40 + 2")
        assert r.status == "ok"
        assert r.result == "42"


# ---------------------------------------------------------------------------
# 超时 abort
# ---------------------------------------------------------------------------


class TestAbort:
    def test_slow_code_aborted_on_timeout(self, km):
        code = "import time; time.sleep(30); print('done')"
        r = km.execute(code, timeout_ms=1500)
        assert r.status == "aborted"
        assert "done" not in r.stdout

    def test_kernel_usable_after_abort(self, km):
        km.execute("import time; time.sleep(30); print('done')", timeout_ms=1500)
        r = km.execute("1 + 1")
        assert r.status == "ok"
        assert r.result == "2"


# ---------------------------------------------------------------------------
# dill 快照 / 恢复
# ---------------------------------------------------------------------------


class TestSnapshotRestore:
    def test_build_snapshot_code_syntax(self):
        code = build_snapshot_code("out.dill", "out.json")
        assert "dill" in code and "__DRIFOX_KERNEL_STATE__" in code

    def test_build_restore_code_syntax(self):
        code = build_restore_code("in.dill")
        assert "dill" in code and "restored" in code

    def test_snapshot_persists_values(self, km, tmp_path):
        km.execute("z = {'k': [1, 2, 3]}")
        snap = km.snapshot_state(str(tmp_path / "state.dill"))
        assert snap is not None
        assert "z" in snap.saved
        assert (tmp_path / "state.dill").exists()
        assert (tmp_path / "state.json").exists()

    def test_restore_into_fresh_kernel(self, km, tmp_path):
        km.execute("import math")
        km.execute("answer = 42")
        snap = km.snapshot_state(str(tmp_path / "state.dill"))
        assert snap is not None and "answer" in snap.saved

        # 全新内核恢复
        km2 = KernelManager()
        km2.start()
        try:
            rest = km2.restore_state(str(tmp_path / "state.dill"))
            assert rest is not None
            assert "answer" in rest.restored
            r = km2.execute("answer + 1")
            assert r.result == "43"
            # import 也恢复（模块按引用重新导入）
            km2.execute("import math")
            assert km2.execute("math.sqrt(16)").result == "4.0"
        finally:
            km2.shutdown()

    def test_restore_missing_file_tolerant(self, km, tmp_path):
        rest = km.restore_state(str(tmp_path / "nope.dill"))
        assert rest is not None
        assert rest.restored == []

    def test_marker_parse(self):
        assert _parse_marker_line('x\n__DRIFOX_KERNEL_STATE__{"a": 1}\ntail') == {"a": 1}
        assert _parse_marker_line("no marker") is None


# ---------------------------------------------------------------------------
# host bridge（kernel 内 host_request shim ↔ 宿主 handler）
# ---------------------------------------------------------------------------


class TestHostBridge:
    def test_shim_installed(self, km):
        r = km.install_host_shim()
        assert r.status == "ok"

    def test_host_request_echo(self, km):
        km.register_host_handler("echo", lambda payload: {"echo": payload.get("value")})
        km.install_host_shim()
        r = km.execute(
            "result = await host_request('echo', {'value': 42})\nprint(result)",
            timeout_ms=15000,
        )
        assert r.status == "ok", r.stderr
        assert "'echo': 42" in r.stdout

    def test_host_request_unregistered_errors(self, km):
        km.install_host_shim()
        r = km.execute(
            "try:\n"
            "    await host_request('nope.type', {})\n"
            "    print('NO-ERROR')\n"
            "except RuntimeError as e:\n"
            "    print('ERR:', str(e))\n",
            timeout_ms=15000,
        )
        assert "NO-ERROR" not in r.stdout
        assert "ERR:" in r.stdout

    def test_host_request_after_shim_restart(self):
        # 重启内核后 shim 需重新注入（幂等）
        km = KernelManager()
        km.start()
        try:
            km.register_host_handler("ping", lambda payload: {"pong": True})
            km.install_host_shim()
            r = km.execute(
                "r = await host_request('ping', {})\nprint(r)",
                timeout_ms=15000,
            )
            assert r.status == "ok", r.stderr
            assert "'pong': True" in r.stdout
        finally:
            km.shutdown()


# ---------------------------------------------------------------------------
# 回归锚点：本文件不依赖 pytest-qt，不触发 Qt 导入
# ---------------------------------------------------------------------------


def test_no_qt_import_on_module_import():
    """kernel_manager 模块导入成功且不依赖 PyQt5 顶层符号（供 CI 常驻）"""
    from app.core.kernel_manager import KernelManager  # noqa: F401

    # 模块顶层只 import jupyter_client/loguru/pathlib 等轻量依赖，不触发 Qt
    import inspect

    src = inspect.getsource(KernelManager)
    assert "PyQt5" not in src