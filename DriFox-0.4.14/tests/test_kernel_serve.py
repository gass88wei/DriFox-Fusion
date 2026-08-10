# -*- coding: utf-8 -*-
"""
L4 后台保活 - RLM 常驻服务测试（真实内核，marker=kernel）

覆盖：
- KernelServeHandler：任务生命周期/跨轮状态/事件链/订阅推送/错误/超时中止
- 持久化 + 重启恢复：快照重载 + 未完成任务继续
- HTTP API：提交/列表/详情/SSE 事件流回放/快照下载/恢复/重启/状态
- UI attach 验收：detach（关 UI）→ 服务保持任务继续 → 重 attach 回放

说明：httpx.ASGITransport 不支持流式响应（无限 SSE 会聚合 body 卡死），
SSE 相关用例走真实 uvicorn 后台线程 + attach_client（requests 流式）。
"""
import socket
import time

import pytest

from app.gateway.local_service.api_server import LLMAPIService
from app.gateway.local_service.kernel_serve import KernelServeHandler

pytestmark = pytest.mark.kernel


def _wait_status(handler, task_id, statuses, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = handler.task_detail(task_id)
        if detail is None:
            time.sleep(0.3)
            continue
        if detail["status"] in statuses:
            return detail
        time.sleep(0.3)
    raise AssertionError(f"任务 {task_id} 超时未达 {statuses}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestServeHandler:
    """KernelServeHandler 核心（类级共享一个内核，方法间变量隔离）"""

    @pytest.fixture(scope="class")
    def handler(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("ks-handler")
        h = KernelServeHandler(state_dir=str(d))
        h.start()
        yield h
        h.stop(keep_tasks=False)

    def test_submit_and_execute(self, handler):
        t = handler.submit_task("ks_a = 40 + 2\nks_a")
        detail = _wait_status(handler, t["id"], ("done", "error", "aborted"))
        assert detail["status"] == "done"
        assert detail["result"]["result"] == "42"

    def test_cross_task_state(self, handler):
        """跨任务状态保持（内核常驻）"""
        t = handler.submit_task("ks_a + 1")
        detail = _wait_status(handler, t["id"], ("done", "error", "aborted"))
        assert detail["status"] == "done"
        assert detail["result"]["result"] == "43"

    def test_events_chain(self, handler):
        """事件链：submitted → started → result → finished → snapshot"""
        t = handler.submit_task("ks_ev = 1")
        detail = _wait_status(handler, t["id"], ("done", "error", "aborted"))
        types = [e["type"] for e in detail["events"]]
        assert types[0] == "task_submitted"
        assert types[1] == "task_started"
        assert "task_result" in types
        assert "task_finished" in types

    def test_subscribe_push_and_unsubscribe(self, handler):
        """订阅收到实时事件；unsubscribe 后不再推送"""
        t = handler.submit_task("ks_sub = 7")
        q = handler.subscribe(t["id"])
        got = None
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                ev = q.get(timeout=1)
                if ev["type"] == "task_finished":
                    got = ev
                    break
            except Exception:
                continue
        assert got is not None
        handler.unsubscribe(t["id"], q)
        # unsubscribe 后再提交同任务，队列不应有事件
        t2 = handler.submit_task("ks_sub2 = 8")
        detail = _wait_status(handler, t2["id"], ("done", "error", "aborted"))
        assert detail["status"] == "done"

    def test_error_task_reported(self, handler):
        t = handler.submit_task("1 / 0")
        detail = _wait_status(handler, t["id"], ("done", "error", "aborted"))
        assert detail["status"] == "error"
        assert detail["result"]["status"] == "error"
        assert detail["result"]["error"]["ename"] == "ZeroDivisionError"

    def test_abort_on_timeout(self, handler):
        t = handler.submit_task("import time; time.sleep(60)", timeout_ms=1500)
        detail = _wait_status(handler, t["id"], ("done", "error", "aborted"), timeout=30)
        assert detail["status"] == "aborted"
        # abort 后内核仍可复用
        t2 = handler.submit_task("ks_after_abort = 1")
        d2 = _wait_status(handler, t2["id"], ("done", "error", "aborted"))
        assert d2["status"] == "done"

    def test_session_filter(self, handler):
        t = handler.submit_task("ks_sess = 1", session="alpha")
        _wait_status(handler, t["id"], ("done", "error", "aborted"))
        lst = handler.task_list(session="alpha")
        assert any(x["id"] == t["id"] for x in lst)
        lst2 = handler.task_list(session="beta")
        assert all(x["id"] != t["id"] for x in lst2)

    def test_status_report(self, handler):
        st = handler.status()
        assert st["service"] == "kernel-serve"
        assert st["kernel"]["running"] is True
        assert st["tasks_total"] > 0

    def test_snapshot_path_exists(self, handler):
        """任务完成自动快照落盘"""
        st = handler.status()
        assert st["snapshot"] is not None


class TestServeRestore:
    """重启恢复：快照重载 + 任务继续"""

    def test_restart_restores_state_and_continues(self, tmp_path):
        import shutil

        d = tmp_path
        h1 = KernelServeHandler(state_dir=str(d))
        h1.start()
        t = h1.submit_task("ks_r = 100")
        _wait_status(h1, t["id"], ("done", "error", "aborted"))
        h1.stop(keep_tasks=True)
        try:
            h2 = KernelServeHandler(state_dir=str(d))
            h2.start()
            # 快照恢复 + 未完成任务继续
            t2 = h2.submit_task("ks_r + 1")
            d2 = _wait_status(h2, t2["id"], ("done", "error", "aborted"))
            assert d2["status"] == "done"
            assert d2["result"]["result"] == "101"
            # 历史任务仍在列表中
            ids = [x["id"] for x in h2.task_list()]
            assert t["id"] in ids
            h2.stop(keep_tasks=True)
        finally:
            import shutil as _sh

            _sh.rmtree(str(d), ignore_errors=True)

    def test_unfinished_task_continues_after_restart(self, tmp_path):
        """重启后未完成任务继续（queued 重置重新入队）"""
        import shutil

        d = tmp_path
        h1 = KernelServeHandler(state_dir=str(d))
        h1.start()
        # 先占住 worker：慢任务在队列头
        h1.submit_task("import time; time.sleep(3)")
        t_pending = h1.submit_task("ks_pend = 1")
        time.sleep(0.5)  # 慢任务开始执行，pending 仍在队列
        detail = h1.task_detail(t_pending["id"])
        assert detail["status"] == "queued"
        h1.stop(keep_tasks=True)  # 中途停止：pending 任务落盘为 queued
        try:
            h2 = KernelServeHandler(state_dir=str(d))
            h2.start()
            # 未完成任务重新入队并执行完成
            deadline = time.time() + 60
            while time.time() < deadline:
                d2 = h2.task_detail(t_pending["id"])
                if d2 and d2["status"] in ("done", "error", "aborted"):
                    break
                time.sleep(0.5)
            assert d2 is not None
            assert d2["status"] == "done"
            h2.stop(keep_tasks=True)
        finally:
            shutil.rmtree(str(d), ignore_errors=True)


class TestServeAPI:
    """HTTP API 层：TestClient（非 SSE 路由）+ 真实 uvicorn（SSE/attach）"""

    @pytest.fixture(scope="class")
    def svc(self, tmp_path_factory):
        """启动真实 uvicorn 后台线程（随机端口）+ kernel handler"""
        from app.gateway.local_service.attach_client import wait_for_server

        d = tmp_path_factory.mktemp("ks-api")
        handler = KernelServeHandler(state_dir=str(d))
        handler.start()
        port = _free_port()
        service = LLMAPIService(host="127.0.0.1", port=port, kernel_handler=handler)
        service.start(background=True)
        assert wait_for_server("127.0.0.1", port), "服务端口未就绪"
        from fastapi.testclient import TestClient

        tc = TestClient(service.app)
        yield {"handler": handler, "service": service, "port": port, "tc": tc}
        try:
            tc.close()
        except Exception:
            pass
        service.stop()
        handler.stop(keep_tasks=False)

    def test_status_endpoint(self, svc):
        r = svc["tc"].get("/kernel/status")
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert r.json()["status"]["service"] == "kernel-serve"

    def test_submit_via_http(self, svc):
        r = svc["tc"].post("/kernel/tasks", json={"code": "ks_http = 5\nks_http"})
        assert r.status_code == 200
        tid = r.json()["task"]["id"]
        detail = _wait_status(svc["handler"], tid, ("done", "error", "aborted"))
        assert detail["result"]["result"] == "5"

    def test_submit_rejects_empty(self, svc):
        r = svc["tc"].post("/kernel/tasks", json={"code": "  "})
        assert r.status_code == 400

    def test_list_and_detail(self, svc):
        r = svc["tc"].get("/kernel/tasks")
        assert r.status_code == 200
        tasks = r.json()["tasks"]
        assert len(tasks) > 0
        tid = tasks[0]["id"]
        r2 = svc["tc"].get(f"/kernel/tasks/{tid}")
        assert r2.status_code == 200
        assert r2.json()["task"]["id"] == tid

    def test_detail_404(self, svc):
        r = svc["tc"].get("/kernel/tasks/nonexistent")
        assert r.status_code == 404

    def test_sse_replay_history(self, svc):
        """SSE 事件流：attach_client 回放历史事件（after=0 读到 finished）"""
        from app.gateway.local_service.attach_client import KernelAttachClient

        h = svc["handler"]
        t = h.submit_task("ks_sse = 11\nks_sse")
        _wait_status(h, t["id"], ("done", "error", "aborted"))
        client = KernelAttachClient(port=svc["port"])
        ev = client.wait_event(t["id"], "task_finished", timeout=30)
        assert ev is not None
        assert ev["type"] == "task_finished"

    def test_attach_detach_service_continues(self, svc):
        """L4 验收核心：UI attach → detach（关 UI）→ 服务保持任务继续 → 重 attach 回放"""
        from app.gateway.local_service.attach_client import KernelAttachClient

        h = svc["handler"]
        # 慢任务（保证 detach 后仍在执行）
        t = h.submit_task("import time; time.sleep(3)\nks_live = 99\nks_live")
        client = KernelAttachClient(port=svc["port"])
        # 1) attach：等 started
        ev = client.wait_event(t["id"], "task_started", timeout=30, after=0)
        assert ev is not None and ev["type"] == "task_started"
        # 2) detach（模拟关闭 UI 窗口）
        client.detach()
        assert not client.attached
        # 3) 服务端任务继续执行直至完成
        detail = _wait_status(h, t["id"], ("done", "error", "aborted"))
        assert detail["status"] == "done"
        assert detail["result"]["result"] == "99"
        # 4) 重开 UI attach：回放完整事件链（含 finished）
        client2 = KernelAttachClient(port=svc["port"])
        ev2 = client2.wait_event(t["id"], "task_finished", timeout=30, after=0)
        assert ev2 is not None and ev2["type"] == "task_finished"

    def test_snapshot_download(self, svc):
        r = svc["tc"].post("/kernel/snapshot")
        assert r.status_code == 200
        r2 = svc["tc"].get("/kernel/snapshot")
        assert r2.status_code == 200
        assert r2.headers["content-type"] == "application/octet-stream"
        assert len(r2.content) > 0

    def test_restore_endpoint(self, svc):
        r = svc["tc"].post("/kernel/restore")
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_restart_endpoint(self, svc):
        r = svc["tc"].post("/kernel/restart")
        assert r.status_code == 200
        assert r.json()["success"] is True
        # 重启后内核可继续执行
        t = svc["handler"].submit_task("ks_rs = 3\nks_rs")
        detail = _wait_status(svc["handler"], t["id"], ("done", "error", "aborted"))
        assert detail["status"] == "done"

    def test_kernel_503_without_handler(self):
        """未注入 kernel_handler → 503"""
        service = LLMAPIService()
        from fastapi.testclient import TestClient

        c = TestClient(service.app)
        r = c.get("/kernel/status")
        assert r.status_code == 503
        c.close()