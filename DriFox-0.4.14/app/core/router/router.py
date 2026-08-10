# -*- coding: utf-8 -*-
"""
DriFox 任务路由评分器（L2，源：reverse-skill master-route.ps1）

规则单一事实源：app/core/router/routing.json（勿在本模块硬编码路由表）。

评分语义（与 reverse-skill 一致）：
1. 每条路由的每个 keyword 规则独立评判：
   - must 正则命中 → hit=True
   - mustAll（列表）需全部命中才保持 hit
   - exclude 命中 → 本条规则不触发（防误伤）
2. 命中则计入候选集；同一路 id 多条规则命中 → 加分（score+1/条）
3. 按 priority 数组顺序取『命中分数最高』者为 PRIMARY；
   分数并列时 priority 靠前胜出（遍历时仅 > 才替换）。
4. 未命中任何规则 → 回退 meta.fallbackId。
5. confidence：候选唯一 high / 多候选 medium / 回退 low。

本模块为纯逻辑，不依赖 Qt；可注入 availability provider 检测目标可用性。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

# 内置路由配置（单一事实源）：与 router.py 同目录
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "routing.json"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class RoutingResult:
    """一次路由的结果"""

    primary: str = ""             # PRIMARY 路由 id（如 R1）
    label: str = ""               # PRIMARY label
    target_type: str = ""         # skill | tool | command | agent | none
    target_name: str = ""         # 目标名（如 "explore" / "refine"）
    score: int = 0                # PRIMARY 命中分数
    confidence: str = "low"       # high | medium | low
    hits: List[str] = field(default_factory=list)   # 命中说明（依据，含 note/正则）
    candidates: Dict[str, int] = field(default_factory=dict)  # 全部候选 id → 分数
    fallback: bool = False        # 是否回退到 fallbackId
    available: bool = True        # PRIMARY 目标是否可用
    available_note: str = ""      # 不可用原因（可用时为空）

    @property
    def target(self) -> Dict[str, str]:
        return {"type": self.target_type, "name": self.target_name}


# ---------------------------------------------------------------------------
# 配置加载与校验
# ---------------------------------------------------------------------------


def load_routing_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """加载路由配置（单一事实源）

    Args:
        path: 路由配置路径，默认 DEFAULT_CONFIG_PATH

    Returns:
        解析后的 dict（含 routes / priority / meta）

    Raises:
        FileNotFoundError: 配置文件缺失
        ValueError: JSON 非法（带具体错误信息）
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"路由配置缺失: {cfg_path}")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"路由配置不是合法 JSON: {cfg_path} ({e})") from e
    if not isinstance(cfg, dict) or "routes" not in cfg or "priority" not in cfg:
        raise ValueError(f"路由配置缺少 routes/priority: {cfg_path}")
    return cfg


def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """校验路由配置，返回问题清单（空 = 合法）

    - routes 与 priority 必须一一对应（防新增路由漏加优先级 / 幽灵 id）
    - meta.fallbackId 必须存在于 routes
    - 每条路由必须有 label 和 keywords
    - 每个 keyword 必须有 must
    """
    problems: List[str] = []
    routes = cfg.get("routes", {})
    priority = cfg.get("priority", [])
    meta = cfg.get("meta", {})

    route_ids = set(routes.keys())
    priority_set = set(priority)
    missing = sorted(route_ids - priority_set)
    extra = sorted(priority_set - route_ids)
    if missing:
        problems.append(f"routes 不在 priority 中: {', '.join(missing)}")
    if extra:
        problems.append(f"priority 不在 routes 中: {', '.join(extra)}")

    fallback_id = meta.get("fallbackId", "")
    if fallback_id and fallback_id not in routes:
        problems.append(f"meta.fallbackId {fallback_id!r} 不在 routes 中")

    for rid, route in routes.items():
        if not route.get("label"):
            problems.append(f"route {rid}: 缺少 label")
        keywords = route.get("keywords")
        if not isinstance(keywords, list) or not keywords:
            problems.append(f"route {rid}: 缺少 keywords")
            continue
        for i, kw in enumerate(keywords):
            if not isinstance(kw, dict) or not kw.get("must"):
                problems.append(f"route {rid}.keywords[{i}]: 缺少 must 正则")
            if "mustAll" in kw and not isinstance(kw["mustAll"], list):
                problems.append(f"route {rid}.keywords[{i}]: mustAll 必须是列表")
            if "exclude" in kw and not isinstance(kw["exclude"], str):
                problems.append(f"route {rid}.keywords[{i}]: exclude 必须是字符串")
    return problems


# ---------------------------------------------------------------------------
# 可用性检测
# ---------------------------------------------------------------------------


def _default_availability_provider() -> Callable[[Dict[str, str]], str]:
    """构造默认可用性检测器（懒加载，避免测试/无 Qt 环境硬依赖）

    返回函数：target dict → "" 可用 / 非空原因 不可用
    """
    def check(target: Dict[str, str]) -> str:
        ttype = target.get("type", "")
        name = (target.get("name") or "").strip()
        reason = ""
        try:
            if ttype == "none":
                return ""  # 普通对话恒可用
            if ttype == "agent":
                reason = _check_agent(name)
            elif ttype == "skill":
                reason = _check_skill(name)
            elif ttype == "command":
                reason = _check_command(name)
            elif ttype == "tool":
                reason = _check_tool(name)
            else:
                return f"未知 target.type: {ttype}"
        except Exception as e:  # 检测器异常不阻断路由，标记不可用
            logger.warning(f"[Router] availability check failed: {e}")
            return f"检测异常: {e}"
        return reason

    return check


def _check_agent(name: str) -> str:
    """agent 可用性：AgentManager 是否加载了该 agent（未加载时回退文件存在性）"""
    if not name:
        return "agent 名为空"
    try:
        from app.core.agent import AgentManager

        mgr = AgentManager.get_instance() if hasattr(AgentManager, "get_instance") else None
        loaded = bool(mgr is not None and getattr(mgr, "_agents", None))
        if loaded and name in mgr._agents:
            return ""
        # 未加载/空实例（如无 Qt 环境或插件未初始化）→ 文件存在性兜底
        from app.core.plugin_manager import PluginManager

        pm = PluginManager.get_instance() if hasattr(PluginManager, "get_instance") else None
        agents_dir = None
        if pm is not None and pm.is_initialized():
            for plugin in pm.get_enabled_plugins():
                d = plugin.path / "agents"
                if d.exists():
                    agents_dir = d
                    break
        if agents_dir is None:
            agents_dir = Path(__file__).resolve().parents[3] / "plugins" / "system" / "agents"
        md = agents_dir / f"{name}.md"
        if md.exists():
            return "" if loaded else ""  # 文件存在即可用（未加载时也可路由）
        return f"agent {name!r} 不存在"
    except Exception as e:
        return f"agent 检测失败: {e}"


def _check_skill(name: str) -> str:
    """skill 可用性：技能目录是否存在（目录为最终依据，get_local_skills 仅辅助）"""
    if not name:
        return "skill 名为空"
    # 兜底主检：目录存在性（不依赖 PluginManager/Qt 环境）
    base = Path(__file__).resolve().parents[3] / "plugins" / "system" / "skills"
    if (base / name).exists():
        return ""
    try:
        from app.utils.utils import get_local_skills

        skills = get_local_skills()
        if skills:
            for s in skills:
                if s.get("name", "") == name:
                    return ""
    except Exception:
        pass
    # user-custom 插件技能目录
    try:
        from app.core.plugin_manager import PluginManager

        pm = PluginManager.get_instance() if hasattr(PluginManager, "get_instance") else None
        if pm is not None and pm.is_initialized():
            for plugin in pm.get_enabled_plugins():
                if (plugin.path / "skills" / name).exists():
                    return ""
    except Exception:
        pass
    return f"skill {name!r} 未安装"


def _check_command(name: str) -> str:
    """command 可用性：CommandManager 是否已注册（未注册时回退命令文件存在性）"""
    if not name:
        return "command 名为空"
    try:
        from app.core.command_manager import CommandManager

        mgr = CommandManager.get_instance()
        if mgr.has_command(name):
            return ""
    except Exception as e:
        logger.debug(f"[Router] CommandManager 查询异常: {e}")
    # 回退：system commands 目录文件存在性
    base = Path(__file__).resolve().parents[3] / "plugins" / "system" / "commands"
    if (base / f"{name}.md").exists():
        return ""
    return f"命令 /{name} 未注册"


def _check_tool(name: str) -> str:
    """tool 可用性：内置工具 schema 中是否存在"""
    if not name:
        return "tool 名为空"
    try:
        from app.tools import TOOL_SCHEMAS

        known = {t.get("function", {}).get("name", "") for t in TOOL_SCHEMAS}
        return "" if name in known else f"工具 {name!r} 不存在"
    except Exception as e:
        return f"tool 检测失败: {e}"


# ---------------------------------------------------------------------------
# 评分器
# ---------------------------------------------------------------------------


class Router:
    """任务路由评分器

    用法：
        router = Router()                       # 加载默认配置
        result = router.route("帮我看看这个代码")  # → RoutingResult
        router.reload()                          # 热重载配置

    线程安全：读取配置时加锁，reload 并发安全。
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        availability: Optional[Callable[[Dict[str, str]], str]] = None,
    ):
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._availability = availability or _default_availability_provider()
        self._lock = threading.Lock()
        self._cfg: Dict[str, Any] = {}
        self.reload()

    # ---- 配置 ----

    def reload(self) -> Dict[str, Any]:
        """（重新）加载路由配置；校验失败时记录 warning 但仍保留可匹配的配置"""
        cfg = load_routing_config(self._config_path)
        problems = validate_config(cfg)
        for p in problems:
            logger.warning(f"[Router] 路由配置问题: {p}")
        with self._lock:
            self._cfg = cfg
        return cfg

    @property
    def config(self) -> Dict[str, Any]:
        return self._cfg

    def list_rules(self) -> List[Dict[str, Any]]:
        """列出全部路由规则（id/label/target），按 priority 顺序"""
        priority = self._cfg.get("priority", [])
        routes = self._cfg.get("routes", {})
        result = []
        for rid in priority:
            route = routes.get(rid)
            if not route:
                continue
            result.append({
                "id": rid,
                "label": route.get("label", ""),
                "target_type": route.get("target", {}).get("type", ""),
                "target_name": route.get("target", {}).get("name", ""),
                "keyword_count": len(route.get("keywords", [])),
            })
        return result

    # ---- 评分 ----

    def route(self, text: str) -> RoutingResult:
        """对任务文本评分，返回 PRIMARY 路由结果

        Args:
            text: 用户任务描述（中文/英文均可，内部小写化拉丁 token）

        Returns:
            RoutingResult：primary/label/target/score/confidence/hits/
            candidates/fallback/available
        """
        with self._lock:
            cfg = self._cfg
        routes = cfg.get("routes", {})
        priority = cfg.get("priority", [])
        fallback_id = cfg.get("meta", {}).get("fallbackId", "")
        if not fallback_id:
            fallback_id = priority[0] if priority else ""

        t = (text or "").lower()

        # 1. 逐规则匹配，命中计入候选集并加分
        candidates: Dict[str, int] = {}
        hits_by_route: Dict[str, List[str]] = {}
        for rid, route in routes.items():
            score = 0
            hits: List[str] = []
            for kw in route.get("keywords", []):
                must = kw.get("must", "")
                if not must:
                    continue
                try:
                    hit = re.search(must, t) is not None
                except re.error as e:
                    logger.warning(f"[Router] 正则错误 route={rid}: {must!r} ({e})")
                    continue
                if hit and kw.get("mustAll"):
                    for m in kw["mustAll"]:
                        try:
                            if re.search(m, t) is None:
                                hit = False
                                break
                        except re.error:
                            hit = False
                            break
                if hit and kw.get("exclude"):
                    try:
                        if re.search(kw["exclude"], t) is not None:
                            hit = False
                    except re.error:
                        pass
                if hit:
                    score += 1
                    note = kw.get("note") or kw.get("must", "")
                    hits.append(note)
            if score > 0:
                candidates[rid] = score
                hits_by_route[rid] = hits

        # 2. priority 决胜：分数最高者胜出；并列时 priority 靠前（仅 > 替换）
        primary = ""
        max_score = -1
        for rid in priority:
            if rid in candidates and candidates[rid] > max_score:
                max_score = candidates[rid]
                primary = rid

        # 3. 未命中 → fallback
        is_fallback = False
        if primary == "" or primary not in routes:
            primary = fallback_id if fallback_id in routes else (priority[0] if priority else "")
            is_fallback = primary != ""
            max_score = 0
            candidates = {}
            hits_by_route = {}

        # confidence：候选唯一 high / 多候选 medium / 回退 low
        if is_fallback:
            confidence = "low"
        elif len(candidates) == 1:
            confidence = "high"
        else:
            confidence = "medium"

        route = routes.get(primary, {})
        target = route.get("target", {})
        result = RoutingResult(
            primary=primary,
            label=route.get("label", ""),
            target_type=target.get("type", ""),
            target_name=target.get("name", ""),
            score=max_score,
            confidence=confidence,
            hits=hits_by_route.get(primary, []) if primary else [],
            candidates=candidates,
            fallback=is_fallback,
        )

        # 4. 工具可用性检测（不阻断；none 类型跳过）
        if not is_fallback and primary and target.get("type") != "none":
            try:
                reason = self._availability(target)
            except Exception as e:
                logger.warning(f"[Router] availability checker 异常: {e}")
                reason = f"检测异常: {e}"
            result.available = not reason
            result.available_note = reason
        return result

    # ---- 便捷 ----

    def primary_for(self, text: str) -> str:
        """只取 PRIMARY id（测试/脚本用）"""
        return self.route(text).primary


# 全局默认评分器（懒加载，供命令/hook 复用）
_default_router: Optional[Router] = None
_router_lock = threading.Lock()


def get_router() -> Router:
    """获取全局默认 Router 实例（懒加载）"""
    global _default_router
    if _default_router is None:
        with _router_lock:
            if _default_router is None:
                _default_router = Router()
    return _default_router


def reset_router_instance():
    """重置全局默认 Router（测试用）"""
    global _default_router
    with _router_lock:
        _default_router = None


def format_route_result(result: RoutingResult) -> List[str]:
    """格式化路由结果为可读行（供 /route InfoBar / hook 注入复用）

    Returns:
        多行文本列表（不要以 \n 结尾的行间空格）
    """
    lines = [f"路由: {result.primary} {result.label}"]
    if result.target_type and result.target_type != "none":
        lines.append(f"目标: [{result.target_type}] {result.target_name}")
    lines.append(f"置信度: {result.confidence} | 命中分数: {result.score}")
    if result.hits:
        lines.append("依据: " + "; ".join(result.hits[:3]))
    if not result.available:
        lines.append(f"⚠ 目标不可用: {result.available_note}")
    if result.fallback:
        lines.append("(未命中强规则，回退普通对话)")
    return lines