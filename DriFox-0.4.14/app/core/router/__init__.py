# -*- coding: utf-8 -*-
"""
DriFox 路由层（L2，源：reverse-skill routing.json / MASTER-ROUTING）

- routing.json：路由规则单一事实源（内置规则初集）
- router.py：评分器（must/mustAll/exclude 正则 + priority 决胜 + fallback）
  + 工具可用性检测
"""
from app.core.router.router import (
    DEFAULT_CONFIG_PATH,
    Router,
    RoutingResult,
    get_router,
    reset_router_instance,
)

__all__ = [
    "RoutingResult",
    "Router",
    "get_router",
    "reset_router_instance",
    "DEFAULT_CONFIG_PATH",
]