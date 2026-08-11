# -*- coding: utf-8 -*-
"""
特工团队 Mission 编排（L5）：hypothesis → solver → test-generator（对抗回环）→ summarizer

借鉴 GrandCode 四角色协作思想，复用 DriFox 既有 SubAgentManager / AutoLoopConfig 基础设施：
- 角色 = plugins/system/agents/ 下的内置 agent md（hypothesis / solver / test-generator / summarizer）
- 编排 = 串行流水线 + 对抗回环（test-generator 用例失败带错误重跑 solver，最多 N 轮）
- 预算 = 复用 AutoLoopConfig（max_tokens / max_duration_minutes / max_iterations）
"""
from app.core.mission.runner import (
    DEFAULT_MAX_ROUNDS,
    MissionResult,
    MissionRound,
    MissionRunner,
    NodeOutcome,
    parse_test_result,
)

__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "MissionResult",
    "MissionRound",
    "MissionRunner",
    "NodeOutcome",
    "parse_test_result",
]