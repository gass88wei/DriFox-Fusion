# -*- coding: utf-8 -*-
"""L5 特工团队 /mission 测试。

覆盖范围：
1. MissionRunner 纯逻辑（fake node_executor，不触发 LLM/网络）：
   - 成功路径（无回环）、对抗回环（FAIL→下一轮修复→PASS）、N 轮上限
   - 输出无法解析视为 FAIL（保守回环）
   - 预算复用 AutoLoopConfig：max_tokens / max_duration_minutes / max_iterations 超限即停
   - 节点异常 → status=error
2. plugins/system/agents/ 四个内置角色 md 定义完整性
3. mission.md 命令定义 + main_widget 接入点（AST 静态校验，风格参照 test_subagents_command）
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from app.core.engines.auto_loop.config import AutoLoopConfig  # noqa: E402
from app.core.mission import (  # noqa: E402
    DEFAULT_MAX_ROUNDS,
    MissionRunner,
    NodeOutcome,
    parse_test_result,
)


# ========== 工具 ==========

class FakeExecutor:
    """脚本化节点执行器：按 agent 名返回预置输出，记录调用历史"""

    def __init__(self, outputs: dict, tokens: int = 0):
        # outputs: {agent_name: [输出序列]}；序列耗尽后用最后一个
        self._outputs = {k: list(v) for k, v in outputs.items()}
        self._tokens = tokens
        self.calls: list[dict] = []  # {"agent", "description", "context"}

    def __call__(self, agent: str, description: str, context: str) -> NodeOutcome:
        self.calls.append({"agent": agent, "description": description, "context": context})
        seq = self._outputs.get(agent) or [""]
        text = seq[0] if len(seq) == 1 else seq.pop(0)
        return NodeOutcome(agent=agent, description=description, result=text, tokens=self._tokens)


def _runner(fake: FakeExecutor, **cfg_kwargs) -> MissionRunner:
    return MissionRunner(
        node_executor=fake,
        config=AutoLoopConfig(**cfg_kwargs),
        max_rounds=DEFAULT_MAX_ROUNDS,
    )


HYP = "## 假设\n方案：实现一个计算斐波那契的脚本"
SOLVED = "## 实现摘要\n已写入 fib.py"
TEST_PASS = "TEST_RESULT: PASS\n## 用例清单\ncase1 ok"
TEST_FAIL = "TEST_RESULT: FAIL\n## 失败详情\n输入 fib(50) 超时"


# ========== parse_test_result ==========

class TestParseTestResult:
    def test_pass_marker(self):
        passed, error = parse_test_result("TEST_RESULT: PASS\nall good")
        assert passed is True
        assert error == ""

    def test_fail_marker_with_detail(self):
        passed, error = parse_test_result("TEST_RESULT: FAIL\nIndexError: list index out of range")
        assert passed is False
        assert "IndexError" in error

    def test_no_marker_means_fail(self):
        passed, error = parse_test_result("用例执行完毕，未发现标记")
        assert passed is False
        assert error == ""

    def test_empty_output_fail(self):
        assert parse_test_result("") == (False, "")
        assert parse_test_result(None) == (False, "")

    def test_pass_precedes_fail_when_both(self):
        """PASS 行优先：输出含 PASS 行即视为通过（首个匹配行）"""
        passed, _ = parse_test_result("intro\nTEST_RESULT: PASS\nTEST_RESULT: FAIL")
        assert passed is True

    def test_fail_only_detail_excludes_marker_line(self):
        passed, error = parse_test_result("前导说明\nTEST_RESULT: FAIL\nline1\nline2")
        assert passed is False
        assert error == "line1\nline2"
        assert "TEST_RESULT" not in error

    def test_fail_detail_truncated(self):
        long_fail = "TEST_RESULT: FAIL\n" + "x" * 5000
        passed, error = parse_test_result(long_fail)
        assert passed is False
        assert len(error) <= 3000 + 20  # 截断 + 后缀


# ========== MissionRunner 流程 ==========

class TestMissionRunnerFlow:
    def test_success_path_no_loop(self):
        """hypothesis → solver → test-generator PASS → summarizer，无回环"""
        fake = FakeExecutor({
            "hypothesis": [HYP],
            "solver": [SOLVED],
            "test-generator": [TEST_PASS],
            "summarizer": ["## 最终结论\nPASS"],
        })
        result = _runner(fake).run("任务A")

        assert result.status == "completed"
        assert result.test_passed is True
        assert result.loop_rounds_used == 1
        assert result.summary is not None
        agents = [c["agent"] for c in fake.calls]
        assert agents == ["hypothesis", "solver", "test-generator", "summarizer"]

    def test_adversarial_loop_fail_then_pass(self):
        """第 1 轮 FAIL → 第 2 轮 PASS，且第 2 轮 solver 收到失败错误"""
        fake = FakeExecutor({
            "hypothesis": [HYP],
            "solver": [SOLVED, SOLVED],
            "test-generator": [TEST_FAIL, TEST_PASS],
            "summarizer": ["## 最终结论\nPASS(第2轮)"],
        })
        result = _runner(fake).run("任务B")

        assert result.test_passed is True
        assert result.loop_rounds_used == 2
        assert result.rounds[0].passed is False
        assert result.rounds[1].passed is True
        # 第 2 轮 solver 的 context 必须带回失败错误
        solver_calls = [c for c in fake.calls if c["agent"] == "solver"]
        assert len(solver_calls) == 2
        assert "上一轮测试失败" in solver_calls[1]["context"]
        assert "输入 fib(50) 超时" in solver_calls[1]["context"]

    def test_loop_stops_at_max_rounds(self):
        """持续 FAIL → 恰好 N 轮停止（N=3），test_passed=False"""
        fake = FakeExecutor({
            "hypothesis": [HYP],
            "solver": [SOLVED] * 3,
            "test-generator": [TEST_FAIL] * 3,
            "summarizer": ["## 最终结论\nFAIL"],
        })
        result = _runner(fake).run("任务C")

        assert result.test_passed is False
        assert result.loop_rounds_used == DEFAULT_MAX_ROUNDS == 3
        assert all(not r.passed for r in result.rounds)

    def test_unparseable_output_is_fail_and_loops(self):
        """test-generator 输出无 TEST_RESULT 标记 → 视为 FAIL，回环第 2 轮"""
        fake = FakeExecutor({
            "hypothesis": [HYP],
            "solver": [SOLVED, SOLVED],
            "test-generator": ["没有标记的输出", TEST_PASS],
            "summarizer": ["## 最终结论\nPASS"],
        })
        result = _runner(fake).run("任务D")

        assert result.loop_rounds_used == 2
        assert result.rounds[0].passed is False
        assert result.rounds[1].passed is True

    def test_node_error_marks_status_error(self):
        """节点执行器抛异常 → status=error 并终止"""
        def boom(agent, description, context):
            raise RuntimeError("executor boom")

        result = MissionRunner(node_executor=boom).run("任务E")
        assert result.status == "error"
        assert "executor boom" in result.reason

    def test_max_rounds_one(self):
        """max_rounds=1：单轮，FAIL 后不再回环"""
        fake = FakeExecutor({
            "hypothesis": [HYP],
            "solver": [SOLVED],
            "test-generator": [TEST_FAIL],
            "summarizer": ["## 最终结论\nFAIL"],
        })
        runner = MissionRunner(node_executor=fake, max_rounds=1)
        result = runner.run("任务F")
        assert result.loop_rounds_used == 1
        assert result.test_passed is False


# ========== 预算复用 AutoLoopConfig ==========

class TestMissionBudget:
    def test_token_budget_exceeded_stops(self):
        """每节点消耗 60 token，max_tokens=100：
        hypothesis 后累计 60；第 1 轮 solver 后 120 → 第 2 轮开始前检查超限停止"""
        fake = FakeExecutor({
            "hypothesis": [HYP],
            "solver": [SOLVED, SOLVED],
            "test-generator": [TEST_FAIL, TEST_PASS],
            "summarizer": ["## 最终结论"],
        }, tokens=60)
        result = _runner(fake, max_tokens=100).run("预算任务A")

        assert result.status == "budget_exceeded"
        assert "Token 超限" in result.reason
        assert result.loop_rounds_used == 1  # 第 1 轮跑完，第 2 轮开始前停止
        assert result.summary is None  # 超限后不再执行 summarizer

    def test_duration_budget_exceeded_stops(self):
        """注入时钟：max_duration_minutes=1 分钟，序列时钟在 hypothesis 后即超时"""
        clock = iter([0.0, 61.0, 61.0, 61.0])  # start, hypothesis后, solver后, ...
        fake = FakeExecutor({
            "hypothesis": [HYP],
            "solver": [SOLVED],
            "test-generator": [TEST_FAIL],
            "summarizer": ["## 最终结论"],
        })
        runner = MissionRunner(
            node_executor=fake,
            config=AutoLoopConfig(max_duration_minutes=1),
            max_rounds=3,
            now=lambda: next(clock),
        )
        result = runner.run("预算任务B")

        assert result.status == "timeout"
        assert "超时" in result.reason

    def test_max_iterations_budget_stops(self):
        """max_iterations=3（节点上限）：第 1 轮消耗 2 节点 OK，
        第 2 轮开始前 2*2=4 > 3 → max_nodes_exceeded 停止"""
        fake = FakeExecutor({
            "hypothesis": [HYP],
            "solver": [SOLVED, SOLVED],
            "test-generator": [TEST_FAIL, TEST_PASS],
            "summarizer": ["## 最终结论"],
        })
        result = _runner(fake, max_iterations=3).run("预算任务C")

        assert result.status == "max_nodes_exceeded"
        assert "节点数超限" in result.reason
        assert result.loop_rounds_used == 1


# ========== 内置 agent 与命令定义 ==========

class TestAgentDefinitions:
    def test_four_mission_agents_exist_and_parsable(self):
        agents_dir = PROJECT_ROOT / "plugins" / "system" / "agents"
        for name in ("hypothesis", "solver", "test-generator", "summarizer"):
            md_path = agents_dir / f"{name}.md"
            assert md_path.exists(), f"缺少内置 agent 定义: {name}.md"
            md = md_path.read_text(encoding="utf-8")
            # frontmatter 必须完整（AgentManager 依赖）
            assert re.search(r"^---\s*$", md, re.M), f"{name}.md 缺 frontmatter"
            assert re.search(r"^name:\s*" + re.escape(name) + r"\s*$", md, re.M)
            assert re.search(r"^mode:\s*subagent\s*$", md, re.M)
            assert re.search(r"^description:", md, re.M)

    def test_test_generator_declares_result_marker(self):
        md = (PROJECT_ROOT / "plugins" / "system" / "agents" / "test-generator.md").read_text(encoding="utf-8")
        assert "TEST_RESULT: PASS" in md
        assert "TEST_RESULT: FAIL" in md

    def test_summarizer_and_hypothesis_are_read_only(self):
        for name in ("hypothesis", "summarizer"):
            md = (PROJECT_ROOT / "plugins" / "system" / "agents" / f"{name}.md").read_text(encoding="utf-8")
            assert re.search(r"^  edit:\s*deny\s*$", md, re.M), f"{name} 应 deny edit"
            assert re.search(r"^  write:\s*deny\s*$", md, re.M), f"{name} 应 deny write"

    def test_mission_command_md_exists(self):
        md_path = PROJECT_ROOT / "plugins" / "system" / "commands" / "mission.md"
        assert md_path.exists(), "缺少 mission.md 命令定义"
        md = md_path.read_text(encoding="utf-8")
        assert "usage:" in md
        assert "--rounds" in md


class TestMainWidgetIntegration:
    """AST 静态校验 main_widget 的 /mission 接入点（风格同 test_subagents_command）"""

    @pytest.fixture(scope="class")
    def mw_src(self):
        src_path = PROJECT_ROOT / "app" / "main_widget.py"
        return src_path.read_text(encoding="utf-8")

    def _find_method(self, tree: ast.Module, name: str):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def test_execute_command_has_mission_branch(self, mw_src):
        tree = ast.parse(mw_src)
        method = self._find_method(tree, "_execute_command")
        assert method is not None
        src = textwrap.dedent(ast.unparse(method))
        assert re.search(r"""command_name == ["']mission["']""", src), "_execute_command 必须分发 /mission"
        assert "_handle_mission_command" in src

    def test_has_command_handler_lists_mission(self, mw_src):
        tree = ast.parse(mw_src)
        method = self._find_method(tree, "_has_command_handler")
        assert method is not None
        src = textwrap.dedent(ast.unparse(method))
        assert "mission" in src, "_has_command_handler 硬编码列表必须含 mission"

    def test_mission_handler_and_worker_exist(self, mw_src):
        tree = ast.parse(mw_src)
        assert self._find_method(tree, "_handle_mission_command") is not None
        assert self._find_method(tree, "_on_mission_finished") is not None
        worker = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "_MissionWorker"]
        assert len(worker) == 1, "必须定义 _MissionWorker(QThread)"