---
name: mission
description: 特工团队编排（L5）——hypothesis 假设 → solver 实现 → test-generator 对抗用例（失败带错误回环 solver，最多 N 轮）→ summarizer 提炼
usage: /mission <任务描述> [--rounds N] [--no-loop]
tags: [mission, team, agent, l5, loop]
---
# /mission — 特工团队编排（GrandCode 四角色 + 对抗回环）

把任务交给四个内置特工角色协作完成，test-generator 的对抗用例失败时带错误回环 solver 重试（最多 N 轮）。

## 流水线

\`\`\`
hypothesis（假设） → [solver → test-generator]×N（对抗回环） → summarizer（提炼）
\`\`\`

- **hypothesis**: 只读分析任务，产出可验证的实现假设
- **solver**: 按假设落地实现，收到上一轮测试失败详情时先修再写
- **test-generator**: 生成对抗用例并实际执行，首行输出 \`TEST_RESULT: PASS|FAIL\`（PASS/FAIL 由编排器解析）
- **summarizer**: 压缩全过程为最终结论（方案要点/失败修复轨迹/遗留风险）

## 用法

| 命令 | 说明 |
|---|---|
| \`/mission 写一个计算斐波那契的脚本\` | 默认对抗回环 3 轮 |
| \`/mission <任务> --rounds 5\` | 指定最大回环轮数 |
| \`/mission <任务> --no-loop\` | 单轮（solver 只跑一次） |

## 预算（复用 AutoLoopConfig）

- 总 token 上限：\`max_tokens\`（默认 5,000,000），节点累计消耗超限即停止
- 总时长上限：\`max_duration_minutes\`（默认 120 分钟）
- 节点总数上限：\`max_iterations\`（默认 50）

## 说明

- 回环判定基于 test-generator 输出的 \`TEST_RESULT:\` 标记行；无法解析视为 FAIL（保守回环）
- 运行中任务在后台线程执行，完成/失败以 InfoBar 提示
- 四角色内置 agent 定义位于 plugins/system/agents/（hypothesis/solver/test-generator/summarizer），可被 /subagents 体系复用
