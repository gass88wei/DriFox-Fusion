# -*- coding: utf-8 -*-
"""
路由层测试（L2，源：reverse-skill test-routing 基准矩阵）

覆盖：
- 配置加载/校验：缺失/坏 JSON/缺字段/routes-priority 一致性/fallback 存在
- 评分器：must / mustAll / exclude / priority 决胜 / fallback / 多规则加分
  / 大小写不敏感 / 空文本 / 坏正则防御
- 内置规则基准：R1-R30 每条至少 1 个真实命中（中文+英文示例）+ fallback
- 工具可用性检测：agent/skill/command/tool/none/未知类型
- 自动路由 hook：默认关 / 开启注入 / 命令自身跳过 / 空消息
- 全量一致性：routing.json 所有路由在 priority 中（防漏加）
"""

import json
import re
import sqlite3

import pytest

from app.core.router import Router, get_router, reset_router_instance
from app.core.router.router import (
    DEFAULT_CONFIG_PATH,
    RoutingResult,
    format_route_result,
    load_routing_config,
    validate_config,
)


# ---------------------------------------------------------------------------
# 配置加载与校验
# ---------------------------------------------------------------------------


class TestConfigLoad:
    def test_default_config_exists(self):
        assert DEFAULT_CONFIG_PATH.exists(), "内置 routing.json 必须存在"

    def test_default_config_valid_json(self):
        cfg = load_routing_config()
        assert isinstance(cfg, dict)

    def test_has_routes_and_priority(self):
        cfg = load_routing_config()
        assert "routes" in cfg and "priority" in cfg

    def test_has_meta_fallback(self):
        cfg = load_routing_config()
        assert "fallbackId" in cfg.get("meta", {})

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_routing_config(tmp_path / "nope.json")

    def test_bad_json_raises_valueerror(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError):
            load_routing_config(p)

    def test_missing_routes_key_raises(self, tmp_path):
        p = tmp_path / "no_routes.json"
        p.write_text('{"priority": []}', encoding="utf-8")
        with pytest.raises(ValueError):
            load_routing_config(p)

    def test_empty_routes_allowed_shape(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text('{"routes": {}, "priority": []}', encoding="utf-8")
        cfg = load_routing_config(p)
        assert cfg["routes"] == {}

    def test_reload_changes_visible(self, tmp_path):
        p = tmp_path / "cfg.json"
        p.write_text('{"routes": {}, "priority": [], "meta": {}}', encoding="utf-8")
        r = Router(config_path=p)
        assert r.list_rules() == []
        p.write_text(
            json.dumps({
                "routes": {"X1": {"label": "L", "target": {"type": "none", "name": ""},
                                  "keywords": [{"must": "abc"}]}},
                "priority": ["X1"],
                "meta": {"fallbackId": "X1"},
            }),
            encoding="utf-8",
        )
        r.reload()
        assert r.route("abc").primary == "X1"


class TestValidateConfig:
    def test_valid_config_no_problems(self):
        cfg = load_routing_config()
        assert validate_config(cfg) == []

    def test_route_missing_in_priority_detected(self):
        cfg = {"routes": {"A": {"label": "x", "target": {}, "keywords": [{"must": "a"}]}},
               "priority": [], "meta": {}}
        problems = validate_config(cfg)
        assert any("不在 priority" in p for p in problems)

    def test_priority_extra_id_detected(self):
        cfg = {"routes": {"A": {"label": "x", "target": {}, "keywords": [{"must": "a"}]}},
               "priority": ["A", "GHOST"], "meta": {}}
        problems = validate_config(cfg)
        assert any("GHOST" in p for p in problems)

    def test_fallback_not_in_routes_detected(self):
        cfg = {"routes": {"A": {"label": "x", "target": {}, "keywords": [{"must": "a"}]}},
               "priority": ["A"], "meta": {"fallbackId": "Z"}}
        problems = validate_config(cfg)
        assert any("fallbackId" in p for p in problems)

    def test_missing_label_detected(self):
        cfg = {"routes": {"A": {"target": {}, "keywords": [{"must": "a"}]}},
               "priority": ["A"], "meta": {}}
        problems = validate_config(cfg)
        assert any("缺少 label" in p for p in problems)

    def test_missing_keywords_detected(self):
        cfg = {"routes": {"A": {"label": "x", "target": {}}},
               "priority": ["A"], "meta": {}}
        problems = validate_config(cfg)
        assert any("缺少 keywords" in p for p in problems)

    def test_keyword_without_must_detected(self):
        cfg = {"routes": {"A": {"label": "x", "target": {}, "keywords": [{"exclude": "zz"}]}},
               "priority": ["A"], "meta": {}}
        problems = validate_config(cfg)
        assert any("缺少 must" in p for p in problems)

    def test_bad_mustall_type_detected(self):
        cfg = {"routes": {"A": {"label": "x", "target": {}, "keywords": [{"must": "a", "mustAll": "str"}]}},
               "priority": ["A"], "meta": {}}
        problems = validate_config(cfg)
        assert any("mustAll" in p for p in problems)


# ---------------------------------------------------------------------------
# 评分器核心逻辑（用最小自制配置，隔离内置规则变化）
# ---------------------------------------------------------------------------


def _mk_cfg(routes, priority, fallback="FB"):
    return {
        "routes": routes,
        "priority": priority,
        "meta": {"fallbackId": fallback},
    }


def _mini_config():
    """两条路由 + fallback：A 优先于 B"""
    return {
        "routes": {
            "A": {"label": "路由A", "target": {"type": "tool", "name": "read"},
                  "keywords": [{"must": "apple|苹果"}]},
            "B": {"label": "路由B", "target": {"type": "tool", "name": "bash"},
                  "keywords": [
                      {"must": "banana", "mustAll": ["yellow|黄"]},
                      {"must": "水果", "exclude": "苹果"},
                  ]},
            "FB": {"label": "兜底", "target": {"type": "none", "name": ""}, "keywords": []},
        },
        "priority": ["A", "B", "FB"],
        "meta": {"fallbackId": "FB"},
    }


@pytest.fixture
def mini(tmp_path):
    p = tmp_path / "mini.json"
    p.write_text(json.dumps(_mini_config(), ensure_ascii=False), encoding="utf-8")
    return Router(config_path=p)


class TestScoringCore:
    # ---- must ----
    def test_must_hit(self, mini):
        assert mini.route("apple pie").primary == "A"

    def test_must_hit_chinese(self, mini):
        assert mini.route("吃苹果").primary == "A"

    def test_must_miss(self, mini):
        assert mini.route("nothing here").primary == "FB"

    # ---- 大小写 ----
    def test_case_insensitive_latin(self, mini):
        assert mini.route("APPLE pie").primary == "A"

    def test_chinese_not_affected_by_case(self, mini):
        assert mini.route("苹果").primary == "A"

    # ---- mustAll ----
    def test_mustall_all_hit(self, mini):
        assert mini.route("banana is yellow").primary == "B"

    def test_mustall_one_missing(self, mini):
        assert mini.route("banana is green").primary == "FB"

    def test_mustall_requires_each(self, mini):
        assert mini.route("yellow banana").primary == "B"

    # ---- exclude ----
    def test_exclude_blocks(self, mini):
        assert mini.route("水果苹果").primary == "A"  # 苹果 → A；水果被 exclude 挡住

    def test_exclude_only_applies_for_that_keyword(self, mini):
        # banana 关键字没有 exclude；水果关键字有 exclude 苹果
        assert mini.route("水果 banana yellow").primary == "B"

    # ---- 多规则加分 ----
    def test_multiple_keywords_boost_score(self, tmp_path):
        p = tmp_path / "boost.json"
        p.write_text(json.dumps({
            "routes": {
                "A": {"label": "A", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "x"}, {"must": "y"}]},
                "B": {"label": "B", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "x"}]},
            },
            "priority": ["A", "B"],
            "meta": {"fallbackId": "B"},
        }), encoding="utf-8")
        r = Router(config_path=p)
        assert r.route("x and y").primary == "A"  # A 命中 2 词 > B 1 词

    # ---- priority 决胜 ----
    def test_priority_tiebreak_first_wins(self, tmp_path):
        p = tmp_path / "tie.json"
        p.write_text(json.dumps({
            "routes": {
                "A": {"label": "A", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "共同词"}]},
                "B": {"label": "B", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "共同词"}]},
            },
            "priority": ["A", "B"],
            "meta": {"fallbackId": "B"},
        }), encoding="utf-8")
        r = Router(config_path=p)
        assert r.route("共同词").primary == "A"   # 同分，priority 靠前 A 胜出

    def test_priority_reorder_changes_winner(self, tmp_path):
        p = tmp_path / "reorder.json"
        p.write_text(json.dumps({
            "routes": {
                "A": {"label": "A", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "共同词"}]},
                "B": {"label": "B", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "共同词"}]},
            },
            "priority": ["B", "A"],
            "meta": {"fallbackId": "A"},
        }), encoding="utf-8")
        r = Router(config_path=p)
        assert r.route("共同词").primary == "B"

    def test_higher_score_beats_priority_order(self, tmp_path):
        p = tmp_path / "score_vs_priority.json"
        p.write_text(json.dumps({
            "routes": {
                "A": {"label": "A", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "x"}]},
                "B": {"label": "B", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "x"}, {"must": "y"}]},
            },
            "priority": ["A", "B"],
            "meta": {"fallbackId": "A"},
        }), encoding="utf-8")
        r = Router(config_path=p)
        # B 命中 2 条 > A 1 条 → B 胜出（即使 A 在 priority 更前）
        assert r.route("x y").primary == "B"

    # ---- fallback ----
    def test_fallback_on_no_hit(self, mini):
        res = mini.route("zzz no keywords")
        assert res.primary == "FB"
        assert res.fallback is True

    def test_fallback_confidence_low(self, mini):
        assert mini.route("zzz").confidence == "low"

    def test_fallback_empty_text(self, mini):
        assert mini.route("").primary == "FB"

    def test_fallback_none_text(self, mini):
        assert mini.route(None).primary == "FB"

    def test_fallback_whitespace_only(self, mini):
        assert mini.route("   ").primary == "FB"

    # ---- confidence ----
    def test_confidence_high_single_candidate(self, mini):
        assert mini.route("apple").confidence == "high"

    def test_confidence_medium_multi_candidate(self, tmp_path):
        p = tmp_path / "multi.json"
        p.write_text(json.dumps({
            "routes": {
                "A": {"label": "A", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "共同"}]},
                "B": {"label": "B", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "共同"}]},
            },
            "priority": ["A", "B"],
            "meta": {"fallbackId": "B"},
        }), encoding="utf-8")
        r = Router(config_path=p)
        assert r.route("共同").confidence == "medium"

    # ---- 返回结构 ----
    def test_result_fields(self, mini):
        res = mini.route("apple")
        assert isinstance(res, RoutingResult)
        assert res.primary == "A"
        assert res.label == "路由A"
        assert res.target_type == "tool"
        assert res.target_name == "read"
        assert res.score >= 1
        assert res.hits  # 依据非空
        assert "A" in res.candidates

    def test_candidates_contains_all_hits(self, mini):
        res = mini.route("banana yellow 苹果")
        assert "A" in res.candidates and "B" in res.candidates

    def test_target_property(self, mini):
        res = mini.route("apple")
        assert res.target == {"type": "tool", "name": "read"}

    # ---- 坏正则防御 ----
    def test_bad_regex_not_raising(self, tmp_path):
        p = tmp_path / "badregex.json"
        p.write_text(json.dumps({
            "routes": {
                "A": {"label": "A", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "("}]},  # 非法正则
            },
            "priority": ["A"],
            "meta": {"fallbackId": "A"},
        }), encoding="utf-8")
        r = Router(config_path=p)
        assert r.route("anything").primary == "A"  # 不崩，回退

    def test_bad_regex_in_exclude_ok(self, tmp_path):
        p = tmp_path / "badex.json"
        p.write_text(json.dumps({
            "routes": {
                "A": {"label": "A", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "x", "exclude": "("}]},
            },
            "priority": ["A"],
            "meta": {"fallbackId": "A"},
        }), encoding="utf-8")
        r = Router(config_path=p)
        assert r.route("x").primary == "A"

    def test_bad_regex_in_mustall_ok(self, tmp_path):
        p = tmp_path / "badma.json"
        p.write_text(json.dumps({
            "routes": {
                "A": {"label": "A", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "x", "mustAll": ["("]}]},
            },
            "priority": ["A"],
            "meta": {"fallbackId": "A"},
        }), encoding="utf-8")
        r = Router(config_path=p)
        # 不崩即可：mustAll 坏正则视为不满足 → x 命中 A
        res = r.route("x")
        assert res.primary in ("A", "FB")


# ---------------------------------------------------------------------------
# 内置规则基准（routing.json 20-30 条真实命中）
# ---------------------------------------------------------------------------

ROUTING_BENCHMARK = [
    # (任务文本, 期望 PRIMARY)
    ("帮我看看这个项目的代码结构", "R1"),
    ("理解一下这段代码在做什么", "R1"),
    ("分析这个代码库的架构", "R1"),
    ("找一下 read 函数的实现", "R1"),
    ("explore this codebase", "R1"),
    ("追踪这个调用的调用链", "R1"),
    ("实现一个读取 CSV 的函数", "R2"),
    ("帮我写一个爬虫脚本", "R2"),
    ("修复这个 bug", "R2"),
    ("给这个功能加个新按钮", "R2"),
    ("implement a sorting function", "R2"),
    ("build the project", "R2"),
    ("规划一下这个功能的实施路径", "R3"),
    ("拆解这个任务", "R3"),
    ("plan the implementation", "R3"),
    ("先做个 roadmap", "R3"),
    ("把这个代码 review 一遍", "R4"),
    ("审查一下最近的改动", "R4"),
    ("code review these changes", "R4"),
    ("团队分工一下并行做", "R5"),
    ("派活给队友", "R5"),
    ("team collaboration needed", "R5"),
    ("执行这个 task", "R6"),
    ("task-executor 跑一下", "R6"),
    ("总结一下这个会话", "R7"),
    ("汇总今天的成果", "R7"),
    ("summarize the result", "R7"),
    ("把这次的经验提炼成记忆", "R8"),
    ("refine 一下对话", "R8"),
    ("帮我记住这个配置", "R9"),
    ("记得以后用这个方案", "R9"),
    ("列一下待办", "R10"),
    ("接管这个话题做 todos", "R10"),
    ("建个分支来改", "R11"),
    ("切换 worktree", "R11"),
    ("开个新会话", "R12"),
    ("用 tdd 先写测试", "R13"),
    ("测试驱动开发这个模块", "R13"),
    ("头脑风暴一下方案", "R14"),
    ("brainstorm new ideas", "R14"),
    ("写个实施计划文档", "R15"),
    ("writing-plans 出一份 spec", "R15"),
    ("并行分派子代理处理", "R16"),
    ("subagent_dag 编排任务", "R16"),
    ("把会话压缩后继续", "R17"),
    ("session-summary 续上下文", "R17"),
    ("诊断这个报错", "R18"),
    ("排查为什么崩溃", "R18"),
    ("triage 一下这些 issue", "R19"),
    ("分诊处理优先级", "R19"),
    ("找个处理 git 的技能", "R20"),
    ("find-skills 看看有什么技能", "R20"),
    ("创建一个提取摘要的技能", "R21"),
    ("skill-creator 写个新技能", "R21"),
    ("抓取这个网页", "R22"),
    ("webfetch 读一下在线文档", "R22"),
    ("搜索一下最新的资料", "R23"),
    ("websearch 查这个库的用法", "R23"),
    ("读一下这个文件", "R24"),
    ("写一个配置文件", "R24"),
    ("搜索代码里的 TODO", "R25"),
    ("grep 一下这个符号", "R25"),
    ("在终端跑一下测试", "R26"),
    ("git commit 提交", "R26"),
    ("查一下这个文件的语法错误", "R27"),
    ("跑 pyright 类型检查", "R27"),
    ("截个屏看看界面", "R28"),
    ("screenshot 桌面状态", "R28"),
    ("看看 MCP 服务器连没连", "R29"),
    ("mcp_list_servers 查状态", "R29"),
    ("给队友发个进度消息", "R30"),
    ("team_send_message 汇报", "R30"),
    ("你好", "R0"),
    ("随便聊聊", "R0"),
    ("帮我做个和代码无关的数学题", "R0"),
]


class TestRoutingBenchmark:
    @pytest.fixture(scope="module")
    def router(self):
        return Router()

    @pytest.mark.parametrize("text,expect", ROUTING_BENCHMARK,
                             ids=[f"{e}:{t[:12]}" for t, e in ROUTING_BENCHMARK])
    def test_benchmark_case(self, router, text, expect):
        res = router.route(text)
        assert res.primary == expect, f"hint={text!r} expect={expect} got={res.primary} cands={res.candidates}"


class TestBuiltinConsistency:
    """内置配置一致性（防新增路由漏加优先级）"""

    def test_all_routes_in_priority(self):
        cfg = load_routing_config()
        assert validate_config(cfg) == []

    def test_rule_count_between_20_40(self):
        cfg = load_routing_config()
        n = len(cfg["routes"])
        assert 20 <= n <= 40, f"内置规则初集期望 20-30+ 条，当前 {n}"

    def test_targets_text(self):
        router = Router()
        cfg = load_routing_config()
        for rid, route in cfg["routes"].items():
            t = route.get("target", {})
            assert t.get("type") in ("agent", "skill", "tool", "command", "none"), f"{rid} 目标类型非法"
            if t.get("type") != "none":
                assert t.get("name"), f"{rid} 目标名不能为空"


# ---------------------------------------------------------------------------
# 可用性检测（注入假检测器 + 真值抽查）
# ---------------------------------------------------------------------------


class TestAvailability:
    def _router_with(self, checker, tmp_path):
        p = tmp_path / "av.json"
        p.write_text(json.dumps({
            "routes": {
                "A": {"label": "A", "target": {"type": "tool", "name": "read"},
                      "keywords": [{"must": "x"}]},
                "N": {"label": "N", "target": {"type": "none", "name": ""},
                      "keywords": [{"must": "y"}]},
            },
            "priority": ["A", "N"],
            "meta": {"fallbackId": "N"},
        }), encoding="utf-8")
        return Router(config_path=p, availability=checker)

    def test_available_true(self, tmp_path):
        r = self._router_with(lambda t: "", tmp_path)
        res = r.route("x")
        assert res.available is True
        assert res.available_note == ""

    def test_unavailable_sets_note(self, tmp_path):
        r = self._router_with(lambda t: "工具不存在", tmp_path)
        res = r.route("x")
        assert res.available is False
        assert res.available_note == "工具不存在"

    def test_none_target_always_available(self, tmp_path):
        r = self._router_with(lambda t: "永远不可用", tmp_path)
        res = r.route("y")
        assert res.available is True  # none 类型跳过检测

    def test_fallback_available_untouched(self, tmp_path):
        checker = lambda t: "nope"  # noqa: E731
        r = self._router_with(checker, tmp_path)
        res = r.route("zzz")
        assert res.available is True  # fallback 不检测

    def test_checker_exception_treated_unavailable(self, tmp_path):
        def boom(t):
            raise RuntimeError("boom")
        r = self._router_with(boom, tmp_path)
        res = r.route("x")
        assert res.available is False
        assert "boom" in res.available_note or "检测" in res.available_note

    # ---- 真实默认检测器 ----
    def test_real_tool_read_available(self):
        from app.tools import TOOL_SCHEMAS
        names = {t["function"]["name"] for t in TOOL_SCHEMAS}
        assert "read" in names  # 前提：read 工具存在
        r = Router()
        res = r.route("读一下这个文件")
        assert res.primary == "R24"
        assert res.available is True  # write 工具存在

    def test_real_unknown_tool_unavailable(self, tmp_path):
        p = tmp_path / "unk.json"
        p.write_text(json.dumps({
            "routes": {
                "A": {"label": "A", "target": {"type": "tool", "name": "不存在的工具xyz"},
                      "keywords": [{"must": "x"}]},
            },
            "priority": ["A"],
            "meta": {"fallbackId": "A"},
        }), encoding="utf-8")
        r = Router(config_path=p)
        res = r.route("x")
        assert res.available is False
        assert "不存在" in res.available_note or "失败" in res.available_note


# ---------------------------------------------------------------------------
# 全局 Router 单例 / list_rules / format
# ---------------------------------------------------------------------------


class TestRouterUtils:
    def test_get_router_singleton(self):
        reset_router_instance()
        a = get_router()
        b = get_router()
        assert a is b

    def test_reset_router_instance(self):
        reset_router_instance()
        a = get_router()
        reset_router_instance()
        b = get_router()
        assert a is not b

    def test_list_rules_has_all_priority(self):
        cfg = load_routing_config()
        r = Router()
        rules = r.list_rules()
        assert [x["id"] for x in rules] == cfg["priority"]

    def test_list_rules_fields(self):
        r = Router()
        for rule in r.list_rules():
            assert "id" in rule and "label" in rule
            assert "target_type" in rule and "target_name" in rule
            assert "keyword_count" in rule

    def test_format_route_result_lines(self):
        r = Router()
        res = r.route("实现一个函数")
        lines = format_route_result(res)
        assert lines and lines[0].startswith("路由:")
        assert any(("目标:" in l and "[agent]" in l) for l in lines)

    def test_format_includes_hits(self):
        r = Router()
        res = r.route("实现一个函数")
        text = "\n".join(format_route_result(res))
        assert "依据" in text

    def test_format_unavailable_note(self, tmp_path):
        p = tmp_path / "unav.json"
        p.write_text(json.dumps({
            "routes": {
                "A": {"label": "A", "target": {"type": "agent", "name": "nope"},
                      "keywords": [{"must": "x"}]},
            },
            "priority": ["A"],
            "meta": {"fallbackId": "A"},
        }), encoding="utf-8")
        r = Router(config_path=p, availability=lambda t: "agent nope 不存在")
        text = "\n".join(format_route_result(r.route("x")))
        assert "不可用" in text

    def test_primary_for(self, tmp_path):
        p = tmp_path / "pf.json"
        p.write_text(json.dumps(_mini_config(), ensure_ascii=False), encoding="utf-8")
        r = Router(config_path=p)
        assert r.primary_for("apple") == "A"


# ---------------------------------------------------------------------------
# 自动路由 hook（默认关）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_hook_state():
    from plugins.system.hooks import auto_route as ar
    ar.reset_for_tests()
    yield
    ar.reset_for_tests()


class TestAutoRouteHook:
    def test_default_disabled_returns_empty(self):
        from plugins.system.hooks.auto_route import hook
        assert hook("PreUserMessage", {"message": "实现一个函数"}) == ""

    def test_enabled_returns_suggestion(self):
        from plugins.system.hooks.auto_route import hook, set_auto_route_enabled
        set_auto_route_enabled(True)
        out = hook("PreUserMessage", {"message": "实现一个函数"})
        assert "### 路由建议" in out
        assert "R2" in out

    def test_enabled_empty_message_returns_empty(self):
        from plugins.system.hooks.auto_route import hook, set_auto_route_enabled
        set_auto_route_enabled(True)
        assert hook("PreUserMessage", {"message": ""}) == ""
        assert hook("PreUserMessage", {"message": "   "}) == ""

    def test_enabled_skips_route_command(self):
        from plugins.system.hooks.auto_route import hook, set_auto_route_enabled
        set_auto_route_enabled(True)
        assert hook("PreUserMessage", {"message": "/route 实现"}) == ""
        assert hook("PreUserMessage", {"message": "/router --list"}) == ""

    def test_enabled_normal_conversation(self):
        from plugins.system.hooks.auto_route import hook, set_auto_route_enabled
        set_auto_route_enabled(True)
        out = hook("PreUserMessage", {"message": "你好"})
        assert "R0" in out

    def test_toggle_off_stops_output(self):
        from plugins.system.hooks.auto_route import hook, set_auto_route_enabled
        set_auto_route_enabled(True)
        assert hook("PreUserMessage", {"message": "实现"}) != ""
        set_auto_route_enabled(False)
        assert hook("PreUserMessage", {"message": "实现"}) == ""

    def test_is_auto_route_enabled_default_false(self):
        from plugins.system.hooks.auto_route import is_auto_route_enabled
        assert is_auto_route_enabled() is False

    def test_set_enabled_persisted_true(self):
        from plugins.system.hooks.auto_route import is_auto_route_enabled, set_auto_route_enabled
        set_auto_route_enabled(True)
        assert is_auto_route_enabled() is True

    def test_hook_missing_context_key(self):
        from plugins.system.hooks.auto_route import hook, set_auto_route_enabled
        set_auto_route_enabled(True)
        assert hook("PreUserMessage", {}) == ""


# ---------------------------------------------------------------------------
# 内置命令文件存在性（/route /router 注册依赖）
# ---------------------------------------------------------------------------


class TestCommandFiles:
    def test_route_md_exists(self):
        p = __import__("pathlib").Path("plugins/system/commands/route.md")
        assert p.exists()

    def test_router_md_exists(self):
        p = __import__("pathlib").Path("plugins/system/commands/router.md")
        assert p.exists()

    def test_route_md_is_function_type(self):
        import yaml
        text = __import__("pathlib").Path("plugins/system/commands/route.md").read_text(encoding="utf-8")
        assert text.startswith("---")
        meta = yaml.safe_load(text.split("---", 2)[1])
        assert meta["type"] == "function"

    def test_router_md_is_function_type(self):
        import yaml
        text = __import__("pathlib").Path("plugins/system/commands/router.md").read_text(encoding="utf-8")
        assert text.startswith("---")
        meta = yaml.safe_load(text.split("---", 2)[1])
        assert meta["type"] == "function"

    def test_auto_route_hook_file_exists(self):
        p = __import__("pathlib").Path("plugins/system/hooks/auto_route.py")
        assert p.exists()

    def test_hooks_json_registers_auto_route(self):
        # hooks.json 是本地配置（可能不入库），存在时校验已注册
        p = __import__("pathlib").Path("plugins/system/hooks/hooks.json")
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            pre = data["hooks"].get("PreUserMessage", [])
            funcs = []
            for rule in pre:
                for h in rule.get("hooks", []):
                    funcs.append(h.get("function", ""))
            assert any(".auto_route:hook" in f for f in funcs)