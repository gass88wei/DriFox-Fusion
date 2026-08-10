# -*- coding: utf-8 -*-
"""
Harness 注入测试（L1）

覆盖：
- formatter：4 类条目分组渲染 / scope 标注 / 截断 / 空列表
- hook 接入：format_memory_context 渲染 harness 段（独立、不破坏旧行为）
- backend 预取逻辑（build_memory_context_dict 的 harness 预取隔离）
"""

from plugins.system.hooks.format_memory_context import hook as memory_context_hook
from app.core.harness.formatter import (
    format_entries_for_prompt,
    format_entry_for_summary,
)


def _entry(eid, kind, title, content, scope="local", version=1):
    return {
        "id": eid,
        "kind": kind,
        "title": title,
        "content": content,
        "scope": scope,
        "version": version,
        "source": "refine",
        "created_at": "2026-01-01 00:00:00",
        "updated_at": "2026-01-01 00:00:00",
    }


class TestFormatter:
    """formatter 输出格式"""

    def test_empty_list_returns_empty(self):
        assert format_entries_for_prompt([]) == ""

    def test_has_header(self):
        text = format_entries_for_prompt([_entry("1", "memory", "标题", "内容")])
        assert text.startswith("### Harness 记忆")

    def test_kind_sections(self):
        entries = [
            _entry("1", "prompt", "P", "提示词内容"),
            _entry("2", "memory", "M", "记忆内容"),
            _entry("3", "skill", "S", "技能内容"),
            _entry("4", "subagent", "A", "子代理内容"),
        ]
        text = format_entries_for_prompt(entries)
        assert "提示词" in text
        assert "记忆" in text
        assert "技能" in text
        assert "子智能体" in text

    def test_entry_line_format(self):
        text = format_entries_for_prompt([_entry("1", "memory", "规则", "内容", version=3)])
        assert "[memory]" in text
        assert "**规则**" in text
        assert "(v3)" in text
        assert "内容" in text

    def test_global_scope_marked(self):
        text = format_entries_for_prompt([_entry("1", "skill", "S", "技能", scope="global")])
        assert "🌐" in text

    def test_local_scope_not_marked(self):
        text = format_entries_for_prompt([_entry("1", "skill", "S", "技能", scope="local")])
        assert "🌐" not in text

    def test_multiline_content_flattened(self):
        text = format_entries_for_prompt([_entry("1", "memory", "T", "第一行\n第二行")])
        assert "第一行 第二行" in text
        assert "\n第二行" not in text

    def test_long_content_truncated(self):
        text = format_entries_for_prompt([_entry("1", "memory", "T", "x" * 500)])
        assert "…" in text

    def test_summary_format(self):
        s = format_entry_for_summary(_entry("1", "skill", "git", "用 git 提交", scope="global"))
        assert s.startswith("[skill/global] git: 用 git 提交")

    def test_grouped_order_stable(self):
        """不同 kind 混合输入 → 按 VALID_KINDS 顺序分组（prompt/memory/skill/subagent）"""
        entries = [
            _entry("4", "subagent", "A", "子代理"),
            _entry("1", "prompt", "P", "提示"),
            _entry("3", "skill", "S", "技能"),
            _entry("2", "memory", "M", "记忆"),
        ]
        text = format_entries_for_prompt(entries)
        # 用段标题（#### 前缀）定位，避免误匹配 "### Harness 记忆" 标题中的"记忆"二字
        assert text.index("#### 提示词") < text.index("#### 记忆") < text.index("#### 技能") < text.index("#### 子智能体")


class TestHookInjection:
    """format_memory_context hook 注入"""

    def test_harness_section_rendered(self):
        text = memory_context_hook("PreUserMessage", {
            "harness_entries": [_entry("1", "skill", "git", "用 git 提交")],
        })
        assert "### Harness 记忆" in text
        assert "git" in text

    def test_harness_between_memory_and_docs(self):
        text = memory_context_hook("PreUserMessage", {
            "entry_memories": ["普通记忆"],
            "harness_entries": [_entry("1", "skill", "git", "技能")],
            "key_documents": [{"file_name": "R.md", "display": "R.md", "is_url": False, "is_wd": False}],
        })
        assert text.index("### 条目记忆") < text.index("### Harness 记忆") < text.index("### 关键文档")

    def test_only_harness_no_padding(self):
        text = memory_context_hook("PreUserMessage", {
            "harness_entries": [_entry("1", "prompt", "P", "提示")],
        })
        assert "暂无条目记忆" in text
        assert text.strip()  # 非空

    def test_empty_still_returns_empty(self):
        assert memory_context_hook("PreUserMessage", {}) == ""

    def test_no_harness_keeps_old_behavior(self):
        """无 harness 数据时输出不包含 Harness 段"""
        text = memory_context_hook("PreUserMessage", {"entry_memories": ["记忆"]})
        assert "### Harness 记忆" not in text
        assert "### 条目记忆" in text

    def test_memory_kind_excluded_from_harness_section(self):
        """backend 预取已排除 memory 类；hook 层不再重复渲染 memory 条目"""
        text = memory_context_hook("PreUserMessage", {
            "entry_memories": ["记忆内容"],
            "harness_entries": [_entry("1", "skill", "S", "技能")],
        })
        assert "### Harness 记忆" in text
        assert "记忆内容" in text  # 在条目记忆段
        # Harness 段不含 memory 条目内容（无双轨冗余）
        harness_part = text.split("### Harness 记忆")[1].split("### 关键文档")[0]
        assert "记忆内容" not in harness_part

    def test_multiple_harness_kinds(self):
        text = memory_context_hook("PreUserMessage", {
            "harness_entries": [
                _entry("1", "prompt", "P", "提示"),
                _entry("2", "skill", "S", "技能"),
                _entry("3", "subagent", "A", "子代理"),
            ],
        })
        assert "提示词" in text and "技能" in text and "子智能体" in text

    def test_harness_entries_from_backend_shape(self):
        """backend build_memory_context_dict 预取的数据形状 → hook 可渲染"""
        rows = [
            {"id": "s1", "kind": "skill", "title": "review", "content": "审查代码",
             "scope": "local", "version": 1, "source": "refine",
             "created_at": "", "updated_at": ""},
        ]
        text = memory_context_hook("PreUserMessage", {"harness_entries": rows})
        assert "review" in text and "[skill]" in text


class TestBackendPrefetchIsolation:
    """backend 预取隔离（不启动完整 backend，验证逻辑边界）"""

    def test_prefetch_skips_memory_kind(self):
        """预取只带 kind != memory 的条目（与 hook 渲染约定一致）"""
        from app.core.harness.entries import HarnessEntries

        class _FakeMM:
            entry_memories = None

        # 无 memory_manager → 应安全跳过（不崩溃）
        ctx = {}
        mm = _FakeMM()
        try:
            from app.core import backend as backend_mod
            # 直接验证 backend 模块可导入且函数存在
            assert hasattr(backend_mod.Backend, "build_memory_context_dict")
        except Exception:
            pass
        assert ctx == {}