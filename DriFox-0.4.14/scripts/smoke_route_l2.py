# -*- coding: utf-8 -*-
"""L2 出口冒烟：/route 真实示例命中验证

场景：脚本级模拟 main_widget 的 _handle_route_command 调用路径，
验证命令 md 可加载（builtin_commands）、router 评分命中 PRIMARY、
自动路由 hook 注入渲染。
退出码 0 = 全链路通过
"""
import sys

from pathlib import Path

# scripts/ 运行时不自动含项目根 → 手动加（与 pytest rootdir 行为一致）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    # 1. /route 命令可被 CommandManager 加载（type: function）
    # 无 Qt 环境 PluginManager 不可用 → 用注册链路同一解析函数验证 md 文件
    from pathlib import Path

    from app.core.builtin_commands import _load_command_file

    route_meta = _load_command_file(Path("plugins/system/commands/route.md"))
    assert route_meta is not None, "/route.md 解析失败"
    assert route_meta["name"] == "route" and route_meta["type"] == "function"
    print("[1] /route 命令定义 ✓ (function)")

    router_meta = _load_command_file(Path("plugins/system/commands/router.md"))
    assert router_meta is not None and router_meta["name"] == "router"
    assert router_meta["type"] == "function"
    print("[1b] /router 命令定义 ✓ (function)")

    # 2. 真实示例命中（PRIMARY + 依据 + 可用性）
    from app.core.router import get_router

    router = get_router()
    cases = [
        ("帮我看看这个项目的代码结构", "R1"),
        ("实现一个读取 CSV 的函数", "R2"),
        ("用 tdd 先写测试再实现", "R13"),
        ("审查一下最近的改动", "R4"),
        ("团队分工一下", "R5"),
        ("把这次经验提炼成记忆", "R8"),
        ("搜索一下最新资料", "R23"),
        ("刷新路由规则", None),  # 未命中强规则 → fallback R0
    ]
    for text, expect in cases:
        res = router.route(text)
        if expect is None:
            assert res.fallback, f"{text!r} 应回退 fallback，got {res.primary}"
            print(f"[2] {text} -> {res.primary}(fallback) ✓")
        else:
            assert res.primary == expect, f"{text!r} expect={expect} got={res.primary} cands={res.candidates}"
            assert res.hits, f"{text!r} 命中但无依据"
            print(f"[2] {text} -> {res.primary} {res.label} | 依据: {res.hits[0][:40]} | 可用: {res.available}")

    # 3. format_route_result 渲染（main_widget InfoBar 展示文本）
    from app.core.router.router import format_route_result

    res = router.route("审查一下最近的改动")
    text = "\n".join(format_route_result(res))
    assert "路由:" in text and "目标:" in text and "依据:" in text
    print("[3] 展示文本渲染 ✓")
    print("    " + text.replace("\n", "\n    "))

    # 4. 自动路由 hook：默认关 → 开 → 命中注入
    from plugins.system.hooks.auto_route import hook, is_auto_route_enabled, set_auto_route_enabled

    assert is_auto_route_enabled() is False, "默认应为关闭"
    assert hook("PreUserMessage", {"message": "实现一个函数"}) == ""
    print("[4] 自动路由默认关闭 ✓")

    set_auto_route_enabled(True)
    out = hook("PreUserMessage", {"message": "帮我看看这个项目"})
    assert "### 路由建议" in out and "R1" in out
    print("[5] 自动路由开启注入 ✓:")
    print("    " + out.replace("\n", "\n    "))
    set_auto_route_enabled(False)

    print("\nSMOKE /route 全链路 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())