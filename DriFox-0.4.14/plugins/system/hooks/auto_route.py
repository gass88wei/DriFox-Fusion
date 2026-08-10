# -*- coding: utf-8 -*-
"""
PreUserMessage Hook 函数 — 可选自动路由（默认关闭，L2）

当开关开启时，对每条即将发送的用户消息做路由评分，并把 PRIMARY 路径
注入上下文（供 LLM 参考走对应 agent/技能/工具路径）。

开关控制：
- /router --auto on|off 切换（持久化到 Settings "routerAutoRoute"）
- 默认关闭：hook 返回空字符串，不注入任何内容，零侵入

本模块不依赖 Qt 导入路径（hook 函数仅做字符串处理；Settings 读取懒加载）。
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger

_ENABLED: Optional[bool] = None  # None=未初始化（首次访问时从 Settings 读取）

_SETTINGS_KEY = "routerAutoRoute"


def _load_persisted() -> bool:
    """从 Settings 读取持久化开关（懒加载，失败回退 False=关）"""
    try:
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        if cfg is not None and hasattr(cfg, "get"):
            return bool(cfg.get(_SETTINGS_KEY, False))
    except Exception as e:
        logger.debug(f"[auto_route] Settings 读取失败，按默认关处理: {e}")
    return False


def is_auto_route_enabled() -> bool:
    """当前自动路由开关状态（默认关）"""
    global _ENABLED
    if _ENABLED is None:
        _ENABLED = _load_persisted()
    return _ENABLED


def set_auto_route_enabled(enabled: bool) -> bool:
    """设置自动路由开关并持久化

    Returns:
        True: 设置成功（含持久化）
        False: 仅内存生效，持久化失败
    """
    global _ENABLED
    _ENABLED = bool(enabled)
    try:
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        if cfg is not None and hasattr(cfg, "set") and hasattr(cfg, "save"):
            cfg.set(_SETTINGS_KEY, _ENABLED)
            cfg.save()
            return True
    except Exception as e:
        logger.warning(f"[auto_route] 持久化失败（开关仅在本次会话生效）: {e}")
    return False


def hook(event: str, context: dict) -> str:
    """PreUserMessage 自动路由 hook

    Args:
        event: 事件名（PreUserMessage）
        context: backend 预取上下文，含：
            - message: str 当前用户消息内容
            - project_root: str 工作目录

    Returns:
        开关关闭 / 消息为空 → ""（不注入）
        开关开启且有命中 → 路由建议文本（`### 路由建议` 段）
    """
    if not is_auto_route_enabled():
        return ""

    message = str(context.get("message") or "").strip()
    if not message:
        return ""
    # 命令自身（/route /router）不重复路由
    if message.startswith(("/route", "/router")):
        return ""

    try:
        from app.core.router import get_router
        from app.core.router.router import format_route_result

        result = get_router().route(message)
        lines = ["### 路由建议", ""]
        lines.extend(format_route_result(result))
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"[auto_route] 路由失败，跳过注入: {e}")
        return ""


def reset_for_tests():
    """测试用：重置内存开关为未初始化"""
    global _ENABLED
    _ENABLED = None