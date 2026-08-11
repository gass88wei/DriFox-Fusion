# -*- coding: utf-8 -*-
"""
L5 /mission 冒烟脚本：特工团队 + 对抗回环（进程级 E2E）

两段：
1. Runner 级冒烟（无 LLM）：fake executor 验证 hypothesis→solver→test-generator 回环→summarizer
2. 真实 LLM 10 样题（需环境变量）：
     DRIFOX_TEST_API_KEY   必选（用户提供，不入代码）
     DRIFOX_TEST_API_BASE  默认 https://opencode.ai/zen/v1
     DRIFOX_TEST_MODEL     默认 deepseek-v4-flash-free
   每个样题：hypothesis(LLM) → solver(LLM 生成 solution.py 落盘) → test-generator(LLM 生成
   对抗测试并真实运行) → 失败带错误回环 solver（最多 N 轮）→ summarizer。
   验收：10 题过 8。

用法:
  py -X utf8 scripts/smoke_mission_l5.py            # 仅 runner 级冒烟
  py -X utf8 scripts/smoke_mission_l5.py --real     # 完整（runner 冒烟 + 10 样题真实 LLM）
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
AGENTS_DIR = ROOT / "plugins" / "system" / "agents"
WORK_DIR = ROOT / ".drifox" / "cache" / "mission-sample"


def read_agent_prompt(name: str) -> str:
    """读取 agent md 正文（frontmatter 之后的部分）作为 system prompt"""
    md = (AGENTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    parts = md.split("---", 2)
    return parts[2].strip() if len(parts) >= 3 else md


# ========== 第一段：Runner 级冒烟（fake，无 LLM） ==========

def smoke_runner() -> bool:
    from app.core.mission import MissionRunner, NodeOutcome

    class Fake:
        def __init__(self):
            self.calls = []

        def __call__(self, agent, description, context):
            self.calls.append(agent)
            if agent == "hypothesis":
                return NodeOutcome(agent, description, "## 假设\n迭代实现")
            if agent == "solver":
                rnd = context.count("上一轮测试失败")
                return NodeOutcome(agent, description, f"## 实现\nround{len(self.calls)}")
            if agent == "test-generator":
                # 第 2 次调用起 PASS（模拟回环第 2 轮通过）
                if sum(1 for c in self.calls if c == "test-generator") >= 2:
                    return NodeOutcome(agent, description, "TEST_RESULT: PASS\nall ok")
                return NodeOutcome(agent, description, "TEST_RESULT: FAIL\nfib(50) 超时")
            if agent == "summarizer":
                return NodeOutcome(agent, description, "## 最终结论\nPASS")
            return NodeOutcome(agent, description, "")

    fake = Fake()
    result = MissionRunner(node_executor=fake).run("写斐波那契脚本")
    assert result.test_passed, f"runner 冒烟失败: 对抗未通过 {result}"
    assert result.loop_rounds_used == 2, f"runner 冒烟失败: 回环轮数不对 {result.loop_rounds_used}"
    assert result.summary is not None
    assert fake.calls[-1] == "summarizer"
    print(f"[runner 冒烟] hypothesis→solver→(FAIL→solver→PASS)→summarizer 全链路 OK，"
          f"回环 {result.loop_rounds_used} 轮，tokens {result.total_tokens}")
    return True


# ========== 第二段：真实 LLM 10 样题 ==========

SAMPLE_QUESTIONS = [
    "写一个 Python 函数 is_palindrome(s)，判断字符串是否为回文（要求：忽略大小写与空白，空串视为回文）",
    "写一个 Python 函数 two_sum(nums, target)，返回列表中两数之和等于 target 的下标对（要求：无解返回 None）",
    "写一个 Python 函数 reverse_str(s)，反转字符串（要求：正确处理 Unicode 字符与空串）",
    "写一个 Python 函数 fibonacci(n)，返回第 n 个斐波那契数（要求：n 从 0 开始，迭代实现，n<0 抛 ValueError）",
    "写一个 Python 函数 dedupe(lst)，列表去重且保持原顺序",
    "写一个 Python 函数 to_binary(n)，十进制整数转二进制字符串（要求：负数返回 '-' 前缀，0 返回 '0'）",
    "写一个 Python 函数 gcd(a, b)，欧几里得算法求最大公约数（要求：支持 0 与负数输入）",
    "写一个 Python 函数 is_balanced(s)，检查括号字符串是否匹配（支持 ()[]{} 混合，要求：空串 True）",
    "写一个 Python 函数 sort_by_value(d)，字典按值升序排序返回键列表（要求：值相同时按键排序）",
    "写一个 Python 函数 capitalize_words(s)，将句子中每个单词首字母大写（要求：保留原有非字母字符）",
]


def _extract_python(text: str) -> str:
    """提取 python 代码：优先完整代码块；支持截断代码块（无闭合标记）；回退 def/import 行。"""
    if not text:
        return ""
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if blocks:
        return max(blocks, key=len).strip()
    # 截断的代码块：取 ```python 之后的全部文本，去掉最后一行（可能不完整）
    m = re.search(r"```(?:python|py)?\s*\n(.*)$", text, re.S)
    if m:
        raw = m.group(1).strip()
        lines = raw.split("\n")
        if len(lines) > 1 and lines[-1].strip() and not lines[-1].strip().endswith((":", ")", "]", "}", '"', "'")):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    candidates = re.findall(r"(?ms)^(?:def |import |from |class ).*?(?=\n\S|$)", text)
    if candidates:
        return "\n\n".join(c.strip() for c in candidates if c.strip()).strip()
    return text.strip()


def build_llm_node(api_key: str, api_base: str, model: str, sample_dir: Path, timeout_ms: int):
    """构造真实 LLM 节点执行器（openai 直连 + 本地执行，模拟 SubAgentExecutor 节点）

    - solver: LLM 生成 solution.py → 落盘
    - test-generator: LLM 生成 test_solution.py → subprocess 真实运行 → TEST_RESULT 输出
    - hypothesis/summarizer: 纯 LLM 输出
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=api_base)

    def llm(system_prompt: str, user_prompt: str) -> str:
        time.sleep(3)  # 全局节流：free 端点对连续请求敏感，避免触发空转限流
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=8192,
            timeout=120,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            # 空转关键信息：finish_reason 区分「服务端限流」与「max_tokens 截断」
            print(
                f"    [llm-空转] finish={resp.choices[0].finish_reason} "
                f"usage={resp.usage} reasoning={bool(getattr(resp.choices[0].message, 'reasoning_content', None))}",
                flush=True,
            )
        return content

    def run_test(test_code: str, solution_code: str) -> tuple[bool, str]:
        """本地真实运行测试代码 → (passed, 输出)"""
        (sample_dir / "solution.py").write_text(solution_code, encoding="utf-8")
        (sample_dir / "test_solution.py").write_text(test_code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "test_solution.py", "-q", "--tb=short"],
                cwd=str(sample_dir),
                capture_output=True,
                text=True,
                timeout=timeout_ms / 1000,
                env={**os.environ, "PYTHONPATH": str(sample_dir)},
            )
        except subprocess.TimeoutExpired:
            return False, f"测试运行超时（>{timeout_ms}ms）"
        except Exception as e:
            return False, f"测试运行失败: {e}"
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode == 0:
            return True, output[-2000:]
        return False, output[-2000:]

    def node(agent: str, description: str, context: str) -> "NodeOutcome":
        from app.core.mission import NodeOutcome

        system_prompt = read_agent_prompt(agent)
        # thinking 模型偶发空转（content 为空/无代码）：无效输出自动重试，最多 3 次尝试
        for attempt in range(1, 4):
            if agent == "solver":
                out = llm(system_prompt, context + "\n\n请把实现代码写入 solution.py。输出格式：**开头第一行就是完整 ```python 代码块**（只含代码，不要任何解释文字），代码块之后可以跟简短说明。")
                code = _extract_python(out)
                if code:
                    (sample_dir / "solution.py").write_text(code, encoding="utf-8")
                    return NodeOutcome(agent, description, result=out, tokens=0)
            elif agent == "test-generator":
                solution_code = (sample_dir / "solution.py").read_text(encoding="utf-8", errors="replace")
                # 精简 context：test-generator 只需 任务+实现，去掉假设段（降低请求复杂度）
                ctx_short = re.sub(r"## 假设\n.*?(?=## 实现|$)", "", context, flags=re.S)
                out = llm(
                    system_prompt,
                    ctx_short
                    + f"\n\n当前 solution.py 内容:\n```python\n{solution_code}\n```\n"
                    + "请生成对抗测试代码。输出格式：**开头第一行就是完整 ```python 代码块**（只含 pytest 测试代码，import 用 from solution import ...），不要输出 TEST_RESULT 标记行。",
                )
                test_code = _extract_python(out)
                if test_code and "def test" in test_code:
                    passed, output = run_test(test_code, solution_code)
                    # 以真实运行结果为准构造标记（free 模型输出受限，LLM 声明不可靠）
                    marker = "TEST_RESULT: PASS" if passed else "TEST_RESULT: FAIL"
                    detail = "" if passed else f"\n## 失败详情\n{output}"
                    return NodeOutcome(agent, description, result=f"{marker}\n{detail}", tokens=0)
            else:
                out = llm(system_prompt, context or description)
                return NodeOutcome(agent, description, result=out, tokens=0)
            if attempt < 3:
                print(f"    [重试 {attempt}/3] {agent} 输出无效（len={len(out)}），15s 冷却后重试", flush=True)
                time.sleep(15)
        # 3 次尝试均无效
        if agent == "solver":
            return NodeOutcome(agent, description, error="solver 未能生成代码（3 次尝试输出均无效）")
        return NodeOutcome(agent, description, error="test-generator 未能生成测试代码（3 次尝试输出均无效）")

    return node


def smoke_real(api_key: str, api_base: str, model: str, max_rounds: int = 3) -> bool:
    from app.core.mission import MissionRunner

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    passed_count = 0
    details = []
    t_start = time.time()
    for i, question in enumerate(SAMPLE_QUESTIONS, 1):
        sample_dir = WORK_DIR / f"{i:02d}"
        sample_dir.mkdir(parents=True, exist_ok=True)
        node = build_llm_node(api_key, api_base, model, sample_dir, timeout_ms=60000)
        runner = MissionRunner(node_executor=node, max_rounds=max_rounds)
        result = runner.run(question)
        ok = result.test_passed
        passed_count += 1 if ok else 0
        details.append(
            f"  样题{i:02d} {'PASS' if ok else 'FAIL'} | 轮数 {result.loop_rounds_used} | "
            f"状态 {result.status} | 耗时 {result.elapsed_seconds:.0f}s"
        )
        print(details[-1], flush=True)
        # 单题预算保护：每题最长 10 分钟
        if time.time() - t_start > 600 * i:
            print(f"  样题{i} 超时保护终止", flush=True)
    print(f"\n[真实 LLM] 10 样题结果: {passed_count}/10 通过（耗时 {(time.time() - t_start) / 60:.1f} 分钟）")
    for d in details:
        print(d)
    return passed_count >= 8


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="跑真实 LLM 10 样题")
    parser.add_argument("--rounds", type=int, default=3, help="对抗回环最大轮数（默认 3）")
    args = parser.parse_args()

    print("===== L5 /mission 冒烟 =====")
    if not smoke_runner():
        print("FAIL: runner 级冒烟失败")
        return 1
    print()

    if args.real:
        api_key = os.environ.get("DRIFOX_TEST_API_KEY", "")
        api_base = os.environ.get("DRIFOX_TEST_API_BASE", "https://opencode.ai/zen/v1")
        model = os.environ.get("DRIFOX_TEST_MODEL", "deepseek-v4-flash-free")
        if not api_key:
            print("FAIL: 需要环境变量 DRIFOX_TEST_API_KEY（--real 模式）")
            return 2
        print(f"[真实 LLM] base={api_base} model={model} rounds={args.rounds}")
        ok = smoke_real(api_key, api_base, model, max_rounds=args.rounds)
        if not ok:
            print("FAIL: 10 样题未过 8")
            return 1
    print("\nSMOKE /mission L5 全链路 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())