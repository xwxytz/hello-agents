"""MCP 工具统一工厂：全系统只创建一个 amap MCPTool 实例"""

import time

from hello_agents.tools import MCPTool

from ..config import get_settings
from . import api_cache
from .logging_setup import event as log_event

_amap_mcp_tool = None


def peek_amap_mcp_tool():
    """
    返回已初始化的 MCPTool 单例；尚未初始化则返回 None。

    与 get_amap_mcp_tool() 的区别：本函数**不会触发初始化**（初始化需要拉起
    uvx 子进程，约数十秒且是同步阻塞），因此适合健康检查这类轻量探测场景。
    """
    return _amap_mcp_tool


def _install_run_cache(tool: MCPTool) -> None:
    """
    给 MCPTool.run 包一层 TTL 缓存。

    为什么要在这里做而不是只在 AmapService 里：三个检索 Agent 是**直接**调用
    MCPTool.run 的，根本不经过 AmapService。实测跑完一次完整规划后 miss 计数
    纹丝不动，证实了规划主链路完全没享受到缓存——而它才是调用量最大的一侧
    （一次规划 7 次 LLM 调用、多次 MCP 调用）。

    为什么可以安全地包一层：MCPTool.run(parameters: dict) -> str 是一个纯粹的
    「参数进、字符串出」接口，且每次调用内部都新建独立的 MCPClient 与事件循环，
    没有跨调用的状态依赖，重复返回同一份结果不会破坏任何东西。

    不缓存的东西：
    - action != call_tool（例如 list_tools）：这些是元数据发现操作，不该被缓存
    - 不含 "执行结果:" 的响应：那是错误返回，缓存它会把一次上游故障放大到整个 TTL
    """

    original_run = tool.run

    def cached_run(parameters, *args, **kwargs):
        if not isinstance(parameters, dict):
            return original_run(parameters, *args, **kwargs)

        action = (parameters.get("action") or "").lower()
        if not action and "tool_name" in parameters:
            action = "call_tool"          # 与 MCPTool.run 内部同样的推断逻辑
        if action != "call_tool":
            return original_run(parameters, *args, **kwargs)

        tool_name = parameters.get("tool_name") or ""
        cache = api_cache.get_cache(tool_name)
        key = api_cache.build_key(parameters.get("arguments") or {})

        with api_cache.cache_lock():
            hit = cache.get(key)
        if hit is not None:
            api_cache.record_hit(tool_name)
            log_event("mcp.call", tool=tool_name, cache="hit", layer="mcp", duration_ms=0.0).debug(
                f"🎯 MCP 层缓存命中 {tool_name}"
            )
            return hit

        api_cache.record_miss(tool_name)
        started = time.perf_counter()
        result = original_run(parameters, *args, **kwargs)
        elapsed_ms = (time.perf_counter() - started) * 1000

        if api_cache.should_cache(result):
            with api_cache.cache_lock():
                cache[key] = result
            log_event("mcp.call", tool=tool_name, cache="miss", layer="mcp",
                      duration_ms=round(elapsed_ms, 1), success=True).debug(
                f"🔧 MCP 调用完成 {tool_name} {elapsed_ms:.0f}ms"
            )
        else:
            log_event("mcp.call", tool=tool_name, cache="miss", layer="mcp",
                      duration_ms=round(elapsed_ms, 1), success=False, cached=False).warning(
                f"⚠️ MCP 返回疑似错误，不写入缓存 {tool_name}: {str(result)[:120]}"
            )
        return result

    tool.run = cached_run


def get_amap_mcp_tool() -> MCPTool:
    """获取全局唯一的 amap MCPTool 实例（懒加载单例）"""
    global _amap_mcp_tool
    if _amap_mcp_tool is not None:
        return _amap_mcp_tool

    settings = get_settings()
    if not settings.amap_api_key:
        raise ValueError("高德地图API Key未配置,请在.env中设置AMAP_API_KEY")

    started = time.perf_counter()
    tool = MCPTool(
        name="amap",
        description="高德地图服务,支持POI搜索、路线规划、天气查询等功能",
        # uvx 默认解析的 pydantic 与 amap-mcp-server 依赖的旧版 mcp 包不兼容
        # （ImportError: eval_type_backport），会导致服务器启动即退出、工具不展开
        server_command=["uvx", "--with", "pydantic==2.9.2", "amap-mcp-server"],
        env={"AMAP_MAPS_API_KEY": settings.amap_api_key},
        auto_expand=True,
    )
    tool.expandable = True
    elapsed_ms = (time.perf_counter() - started) * 1000

    # 缓存装在 MCPTool.run 上，这样 Agent 编排层 与 /api/map/* 确定性查询层
    # 两条路径都能命中（只装在 AmapService 里的话，规划主链路会完全绕过）
    _install_run_cache(tool)

    discovered = getattr(tool, "_available_tools", None) or []
    if not discovered:
        log_event("mcp.expand", tools_count=0, duration_ms=round(elapsed_ms, 1), success=False).error(
            "❌ MCP工具未展开：未发现任何 amap 子工具。"
            "常见原因：uvx 依赖版本冲突 / 网络不通 / Key 不是「Web服务」类型。"
            "此时工具调用必然失败，Agent 会退化为纯 LLM 幻觉输出"
        )
    else:
        log_event("mcp.expand", tools_count=len(discovered), duration_ms=round(elapsed_ms, 1), success=True).info(
            f"✅ MCP工具已展开为 {len(discovered)} 个子工具（{elapsed_ms / 1000:.1f}s）"
        )

    _amap_mcp_tool = tool
    return tool
