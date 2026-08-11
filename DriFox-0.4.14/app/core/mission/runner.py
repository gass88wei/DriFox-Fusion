# -*- coding: utf-8 -*-
"""
MissionRunner - 特工团队编排器（L5）

流水线：hypothesis → [solver → test-generator]×N（对抗回环）→ summarizer

关键设计：
- 纯逻辑、无 Qt 依赖：节点执行通过注入的 node_executor 回调（同步返回 NodeOutcome），
  便于单测（fake executor）与真实接入（SubAgentManager.execute_task + 事件等待）解耦。
- 对抗回环：test-generator 输出解析 TEST_RESULT 标记行；FAIL 时把该轮测试输出
  （含错误）带回下一轮 solver 的上下文，最多 N 轮（默认 3，用户已确认）。
- 预算复用 AutoLoopConfig：max_tokens（默认 500 万）累计节点消耗、max_duration_minutes
  （默认 120 分钟）总时长、max_iterations（默认 50）节点总数上限；任一超限即停止。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from loguru import logger

from app.core.engines.auto_loop.config import AutoLoopConfig

# 默认对抗回环最大轮数（用户确认 N=3）
DEFAULT_MAX_ROUNDS = 3

# test-generator 输出结果标记行（约定格式，agent 提示词中强制声明）
RESULT_MARK_PASS = "TEST_RESULT: PASS"
RESULT_MARK_FAIL = "TEST_RESULT: FAIL"

# 失败错误摘录最大字符数（带回下一轮 solver 的上下文）
_ERROR_EXCERPT_CHARS = 3000

# 结束状态
STATUS_COMPLETED = "completed"
STATUS_BUDGET_EXCEEDED = "budget_exceeded"
STATUS_TIMEOUT = "timeout"
STATUS_MAX_NODES = "max_nodes_exceeded"
STATUS_ERROR = "error"


@dataclass
class NodeOutcome:
    """单个 agent 节点的执行结果"""

    agent: str
    description: str
    result: str = ""  # 节点最终输出文本
    error: str = ""  # 执行层错误（提交失败/异常/取消）
    tokens: int = 0  # 节点消耗 token（预算核算）

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class MissionRound:
    """一轮对抗（solver 实现 + test-generator 验证）"""

    round_no: int
    solver: Optional[NodeOutcome] = None
    test: Optional[NodeOutcome] = None
    passed: bool = False
    error_text: str = ""  # 用例失败错误摘录（带回下一轮 solver）


@dataclass
class MissionResult:
    """Mission 最终结果"""

    task_description: str
    status: str = STATUS_COMPLETED
    reason: str = ""  # 结束原因（预算超限/超时等）
    hypothesis: Optional[NodeOutcome] = None
    rounds: List[MissionRound] = field(default_factory=list)
    summary: Optional[NodeOutcome] = None
    total_tokens: int = 0
    elapsed_seconds: float = 0.0

    @property
    def test_passed(self) -> bool:
        """是否存在通过的对抗轮次"""
        return any(r.passed for r in self.rounds)

    @property
    def loop_rounds_used(self) -> int:
        return len(self.rounds)

    def to_dict(self) -> dict:
        return {
            "task_description": self.task_description,
            "status": self.status,
            "reason": self.reason,
            "test_passed": self.test_passed,
            "loop_rounds_used": self.loop_rounds_used,
            "total_tokens": self.total_tokens,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "hypothesis_agent": self.hypothesis.agent if self.hypothesis else "",
            "rounds": [
                {
                    "round_no": r.round_no,
                    "passed": r.passed,
                    "solver_result_head": (r.solver.result or "")[:120] if r.solver else "",
                    "test_result_head": (r.test.result or "")[:120] if r.test else "",
                    "error_text_head": r.error_text[:120],
                }
                for r in self.rounds
            ],
            "summary_agent": self.summary.agent if self.summary else "",
        }


def parse_test_result(output: str) -> tuple[bool, str]:
    """
    解析 test-generator 输出中的 TEST_RESULT 标记行。

    约定（test-generator 提示词强制声明输出格式）：
         TEST_RESULT: PASS    → 用例全部通过
         TEST_RESULT: FAIL    → 存在失败用例

    返回 (passed, error_text)：
    - 命中 PASS → (True, "")
    - 命中 FAIL → (False, 错误摘录：FAIL 标记行之后的输出，截断 _ERROR_EXCERPT_CHARS)
    - 两个标记都未命中 → (False, "")（无法确认通过即视为失败，触发回环——保守语义）
    """
    if not output:
        return False, ""

    lines = output.splitlines()
    fail_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(RESULT_MARK_PASS):
            return True, ""
        if stripped.startswith(RESULT_MARK_FAIL):
            fail_idx = i
    if fail_idx >= 0:
        # FAIL 标记之后的输出视为错误详情（去掉标记行本身）
        error_text = "\n".join(lines[fail_idx + 1 :]).strip()
        if len(error_text) > _ERROR_EXCERPT_CHARS:
            error_text = error_text[:_ERROR_EXCERPT_CHARS] + "\n…(截断)"
        return False, error_text
    return False, ""


class MissionRunner:
    """
    特工团队编排器：hypothesis → [solver → test-generator]×N → summarizer。

    Args:
        node_executor: Callable[[agent, description, parent_context], NodeOutcome]
            节点执行回调（真实接入时由调用方包装 SubAgentManager）。
        config: AutoLoopConfig（预算复用，默认全默认值：max_tokens=500万/120min/50节点）
        max_rounds: 对抗回环最大轮数（默认 DEFAULT_MAX_ROUNDS=3）
        now: 时钟注入（测试用）
    """

    def __init__(
        self,
        node_executor: Callable[[str, str, str], NodeOutcome],
        config: Optional[AutoLoopConfig] = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        now: Callable[[], float] = time.time,
    ):
        self._node_executor = node_executor
        self._config = config or AutoLoopConfig()
        self._max_rounds = max(1, int(max_rounds))
        self._now = now

    # ---------- 预算 ----------

    def _check_budget(self, tokens: int, elapsed: float) -> Optional[str]:
        """预算检查：返回超限原因（None=未超限）。语义与 AutoLoopEngine.check_budget 对齐"""
        cfg = self._config
        if cfg.max_duration_minutes > 0 and elapsed >= cfg.max_duration_minutes * 60:
            return f"超时: 已运行 {elapsed / 60:.1f} 分钟，上限 {cfg.max_duration_minutes} 分钟"
        if cfg.max_tokens > 0 and tokens >= cfg.max_tokens:
            return f"Token 超限: 已用 {tokens}，上限 {cfg.max_tokens}"
        return None

    # ---------- 主流程 ----------

    def run(self, task_description: str, extra_context: str = "") -> MissionResult:
        """
        执行 Mission 全流程。

        Args:
            task_description: 任务描述（传给所有角色）
            extra_context: 额外上下文（追加到 solver 与 summarizer，如项目根/约束）

        Returns:
            MissionResult（含 hypothesis、rounds、summary 与状态/预算信息）
        """
        start = self._now()
        result = MissionResult(task_description=task_description)
        total_tokens = 0

        def _track(outcome: NodeOutcome) -> None:
            nonlocal total_tokens
            if outcome and outcome.tokens:
                total_tokens += outcome.tokens

        def _budget_stop(status: str, reason: str) -> MissionResult:
            result.status = status
            result.reason = reason
            result.total_tokens = total_tokens
            result.elapsed_seconds = self._now() - start
            return result

        # ---- 1. hypothesis（假设）----
        try:
            result.hypothesis = self._node_executor(
                "hypothesis", task_description, extra_context or ""
            )
        except Exception as e:
            logger.error(f"[Mission] hypothesis 节点执行异常: {e}")
            result.status = STATUS_ERROR
            result.reason = f"hypothesis 节点异常: {e}"
            result.total_tokens = total_tokens
            result.elapsed_seconds = self._now() - start
            return result
        _track(result.hypothesis)

        hypothesis_text = (result.hypothesis.result or "").strip()
        if not hypothesis_text and not result.hypothesis.error:
            hypothesis_text = "(无输出)"

        # ---- 2. 对抗回环：solver → test-generator，最多 max_rounds 轮 ----
        pending_error = ""  # 上一轮测试失败错误摘录（带回 solver）
        for round_no in range(1, self._max_rounds + 1):
            # 预算检查（每轮开始）
            elapsed = self._now() - start
            reason = self._check_budget(total_tokens, elapsed)
            if reason:
                return _budget_stop(STATUS_BUDGET_EXCEEDED if "Token" in reason else STATUS_TIMEOUT, reason)
            if self._config.max_iterations > 0 and (2 * round_no) > self._config.max_iterations:
                # 每轮消耗 2 个节点（solver+test-generator），含 hypothesis/summarizer 共 2N+2
                return _budget_stop(STATUS_MAX_NODES, f"节点数超限: 上限 {self._config.max_iterations}")

            round_item = MissionRound(round_no=round_no)

            # 2a. solver（实现；第 2+ 轮带上一轮测试错误）
            solver_ctx_parts = [f"## 任务\n{task_description}", f"## 假设\n{hypothesis_text}"]
            if pending_error:
                solver_ctx_parts.append(
                    f"## 上一轮测试失败（第 {round_no - 1} 轮）\n{pending_error}\n请修复问题后重新实现。"
                )
            if extra_context:
                solver_ctx_parts.append(f"## 附加上下文\n{extra_context}")
            try:
                round_item.solver = self._node_executor(
                    "solver", task_description, "\n\n".join(solver_ctx_parts)
                )
            except Exception as e:
                logger.error(f"[Mission] solver 节点执行异常 (round {round_no}): {e}")
                result.status = STATUS_ERROR
                result.reason = f"solver 节点异常 (round {round_no}): {e}"
                result.rounds.append(round_item)
                result.total_tokens = total_tokens
                result.elapsed_seconds = self._now() - start
                return result
            _track(round_item.solver)

            # 2b. test-generator（生成对抗用例并执行）
            test_ctx_parts = [
                f"## 任务\n{task_description}",
                f"## 假设\n{hypothesis_text}",
                f"## 实现（第 {round_no} 轮）\n{(round_item.solver.result or '').strip() or '(无输出)'}",
            ]
            try:
                round_item.test = self._node_executor(
                    "test-generator", task_description, "\n\n".join(test_ctx_parts)
                )
            except Exception as e:
                logger.error(f"[Mission] test-generator 节点执行异常 (round {round_no}): {e}")
                result.status = STATUS_ERROR
                result.reason = f"test-generator 节点异常 (round {round_no}): {e}"
                result.rounds.append(round_item)
                result.total_tokens = total_tokens
                result.elapsed_seconds = self._now() - start
                return result
            _track(round_item.test)

            # 2c. 解析测试结果 → 回环判定
            test_output = (round_item.test.result or "") if round_item.test.ok else ""
            if not round_item.test.ok:
                # 执行层错误（如任务取消）→ 视为失败，错误带回
                round_item.passed = False
                round_item.error_text = round_item.test.error
            else:
                passed, error_text = parse_test_result(test_output)
                round_item.passed = passed
                round_item.error_text = error_text
            result.rounds.append(round_item)

            if round_item.passed:
                break
            # FAIL → 错误带回下一轮 solver；已到最大轮数则不再继续
            pending_error = round_item.error_text or (test_output[:_ERROR_EXCERPT_CHARS] or "用例执行失败（无错误详情）")
            logger.info(
                f"[Mission] 第 {round_no} 轮对抗未通过，回环下一轮（已用 {round_no}/{self._max_rounds}）"
            )

        # ---- 3. summarizer（压缩提炼最终结论）----
        summary_ctx_parts = [
            f"## 任务\n{task_description}",
            f"## 假设\n{hypothesis_text}",
            f"## 对抗轮数\n共执行 {len(result.rounds)} 轮，"
            f"{'通过' if result.test_passed else '未通过（达到最大回环轮数 ' + str(self._max_rounds) + '）'}",
        ]
        for r in result.rounds:
            test_head = (r.test.result or "")[:800] if r.test else ""
            summary_ctx_parts.append(
                f"### 第 {r.round_no} 轮 {'PASS' if r.passed else 'FAIL'}\n测试输出摘录:\n{test_head}"
            )
        if extra_context:
            summary_ctx_parts.append(f"## 附加上下文\n{extra_context}")
        try:
            result.summary = self._node_executor(
                "summarizer", task_description, "\n\n".join(summary_ctx_parts)
            )
        except Exception as e:
            logger.error(f"[Mission] summarizer 节点执行异常: {e}")
            result.status = STATUS_ERROR
            result.reason = f"summarizer 节点异常: {e}"
            result.total_tokens = total_tokens
            result.elapsed_seconds = self._now() - start
            return result
        _track(result.summary)

        result.total_tokens = total_tokens
        result.elapsed_seconds = self._now() - start
        return result
