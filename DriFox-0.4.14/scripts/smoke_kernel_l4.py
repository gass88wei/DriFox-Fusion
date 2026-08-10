# -*- coding: utf-8 -*-
"""
L4 冒烟：RLM 常驻服务全链路（进程级 E2E）

真实 --serve 子进程（main.py --serve --port N --state-dir D）：
  [1] 服务端口就绪
  [2] 内核状态 running（attach_client.status）
  [3] 提交任务 x=42 → done
  [4] SSE attach 实时收到 task_finished
  [5] 跨任务状态 x+1=43
  [6] 快照下载 kernel-state.dill 落盘
  [7] UI 模拟：attach → started → detach（关窗口）→ 服务继续 → 重 attach 回放 finished
  [8] 杀 serve 进程（模拟应用重启）
  [9] 重启 serve（同 state-dir）→ 快照重载 → x**2=1764（恢复成功）
  [10] 收尾：杀进程，清理 state_dir
  SMOKE /kernel L4 全链路 PASS
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.gateway.local_service.attach_client import (  # noqa: E402
    KernelAttachClient,
    wait_for_server,
)


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_serve(port, state_dir):
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "from app.gateway.local_service.api_server import serve_main; "
        "serve_main(host='127.0.0.1', port=%d, state_dir=r'%s')"
        % (ROOT, port, state_dir)
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def _kill(proc):
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def main() -> None:
    state_dir = tempfile.mkdtemp(prefix="ks-l4-smoke-")
    port = _free_port()
    proc = _spawn_serve(port, state_dir)
    results = []
    try:
        # [1] 服务就绪
        ok = wait_for_server("127.0.0.1", port, timeout=25)
        results.append(("[1] --serve 端口就绪", ok))

        c = KernelAttachClient(port=port)

        # [2] 内核状态
        try:
            st = c.status()
            kernel_ok = bool(st["kernel"] and st["kernel"]["running"])
        except Exception:
            kernel_ok = False
        results.append(("[2] 内核 running", kernel_ok))

        # [3] 提交 x=42
        t1 = c.submit("x = 42\nx")
        ev = c.wait_event(t1["id"], "task_finished", timeout=60)
        d1 = c.task_detail(t1["id"])
        results.append(("[3] 任务 x=42 done", ev is not None and d1["status"] == "done"))

        # [4] 实时收到 finished（上一个 wait_event 已证明）

        # [5] 跨任务状态
        t2 = c.submit("x + 1")
        c.wait_event(t2["id"], "task_finished", timeout=60)
        d2 = c.task_detail(t2["id"])
        results.append(("[5] 跨任务 x+1=43", d2["status"] == "done" and d2["result"]["result"] == "43"))

        # [6] 快照下载
        snap = c.download_snapshot(os.path.join(state_dir, "downloaded-state.dill"))
        results.append(("[6] 快照下载", os.path.exists(snap)))

        # [7] UI 模拟：attach → detach → 服务继续 → 重 attach 回放
        t3 = c.submit("import time; time.sleep(2); ks_live = 99\nks_live")
        ev_started = c.wait_event(t3["id"], "task_started", timeout=30, after=0)
        c.detach()
        detach_ok = not c.attached
        deadline = time.time() + 60
        d3 = None
        while time.time() < deadline:
            d3 = c.task_detail(t3["id"])
            if d3["status"] in ("done", "error", "aborted"):
                break
            time.sleep(0.3)
        continue_ok = d3 and d3["status"] == "done" and d3["result"]["result"] == "99"
        c2 = KernelAttachClient(port=port)
        ev2 = c2.wait_event(t3["id"], "task_finished", timeout=30, after=0)
        replay_ok = ev2 is not None and ev2["type"] == "task_finished"
        results.append(
            ("[7] UI detach→服务继续→重attach 回放",
             ev_started is not None and detach_ok and continue_ok and replay_ok)
        )

        # [8] 杀 serve（应用重启）
        _kill(proc)
        proc = None
        time.sleep(1.0)

        # [9] 重启 serve 同 state-dir → 快照重载 → x**2=1764
        proc2 = _spawn_serve(port, state_dir)
        ok2 = wait_for_server("127.0.0.1", port, timeout=25)
        c3 = KernelAttachClient(port=port)
        t4 = c3.submit("x ** 2")
        ev4 = c3.wait_event(t4["id"], "task_finished", timeout=60)
        d4 = c3.task_detail(t4["id"])
        restore_ok = (
            ok2 and ev4 is not None and d4["status"] == "done" and d4["result"]["result"] == "1764"
        )
        results.append(("[9] 重启恢复 x**2=1764", restore_ok))

        # 历史任务保留
        tasks = c3.list_tasks()
        results.append(("[9b] 历史任务保留", len(tasks) >= 4))
        _kill(proc2)
        proc2 = None

    finally:
        if proc is not None:
            _kill(proc)
        shutil.rmtree(state_dir, ignore_errors=True)

    failed = [name for name, ok in results if not ok]
    for name, ok in results:
        print(f"{name}: {'PASS' if ok else 'FAIL'}")
    if failed:
        print(f"SMOKE /kernel L4 失败: {failed}")
        sys.exit(1)
    print("SMOKE /kernel L4 全链路 PASS")


if __name__ == "__main__":
    main()