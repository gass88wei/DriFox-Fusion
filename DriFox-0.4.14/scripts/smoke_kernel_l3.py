# -*- coding: utf-8 -*-
"""
L3 RLM 执行内核冒烟（脚本级 E2E，Gate3 出口验收用）

验证链路：BuiltinTools → kernel_tools.make_python →
  1. 首次调用自动启动内核
  2. x = 42 → 下一轮 x + 1 = 43（跨轮状态）
  3. 函数/import 跨轮保留
  4. 报错不杀内核
  5. 超时 abort 后内核可复用
  6. kernel_status / kernel_restart / 快照
用法：uv run python scripts/smoke_kernel_l3.py
"""
import sys
import time


def main() -> int:
    from app.tools import BuiltinTools

    bt = BuiltinTools()
    kt = bt.kernel_tools
    print("[1] BuiltinTools kernel_tools 就绪:", kt is not None)
    assert kt is not None

    # 1. 首轮执行（自动启动内核）
    r = kt.make_python("x = 42")
    print("[2] 首轮 x=42:", r.success, str(r.content)[:60])
    assert r.success, r.error

    # 2. 跨轮状态：x + 1 = 43
    r = kt.make_python("x + 1")
    print("[3] 跨轮 x+1:", r.success, r.content)
    assert r.success and r.content.strip() == "43", r

    # 3. 函数 + import 跨轮
    r = kt.make_python("def double(v):\n    return v * 2")
    assert r.success, r.error
    r = kt.make_python("import math")
    assert r.success, r.error
    r = kt.make_python("double(math.floor(math.sqrt(81)))")
    print("[4] 函数+import 跨轮:", r.success, r.content)
    assert r.success and r.content.strip() == "18", r

    # 4. 报错不杀内核
    r = kt.make_python("1 / 0")
    print("[5] 报错返回:", r.success, str(r.error)[:60])
    assert not r.success and "ZeroDivisionError" in str(r.error)
    r = kt.make_python("40 + 2")
    assert r.success and r.content.strip() == "42"

    # 5. 超时 abort 后内核可复用
    r = kt.make_python("import time; time.sleep(30); print('done')", timeout_ms=1200)
    print("[6] 超时中止:", r.success, str(r.error)[:60])
    assert not r.success and "超时" in str(r.error)
    r = kt.make_python("1 + 1")
    assert r.success and r.content.strip() == "2"
    print("[7] abort 后内核可复用: OK")

    # 6. 状态查询 / 内核重启（重启清空 x）
    status = kt.kernel_status()
    print("[8] kernel_status:", status)
    assert status["running"]
    r = kt.kernel_restart()
    print("[9] 重启:", r.success, str(r.content)[:60])
    assert r.success
    r = kt.make_python("x + 1")
    print("[10] 重启后 x 已清空（应报错）:", r.success, str(r.error)[:50])
    assert not r.success  # x 未定义

    # 7. 快照/恢复（跨实例：恢复需显式传快照路径，因每个 KernelManager 各自 mkdtemp）
    bt2 = BuiltinTools()
    kt2 = bt2.kernel_tools
    r = kt2.make_python("answer = 42")
    assert r.success
    snap = kt2.kernel_snapshot()
    print("[11] 快照:", snap.success, str(snap.content)[:80])
    assert snap.success
    snap_path = snap.content.split("快照已保存: ")[1].splitlines()[0].strip()
    kt3 = BuiltinTools().kernel_tools
    rest = kt3.kernel_restore(path=snap_path)
    print("[12] 恢复:", rest.success, str(rest.content)[:80])
    assert rest.success and "answer" in (rest.content or "")
    r = kt3.make_python("answer + 1")
    print("[13] 恢复后 answer+1:", r.success, r.content)
    assert r.success and r.content.strip() == "43"

    print()
    print("SMOKE /kernel L3 全链路 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())