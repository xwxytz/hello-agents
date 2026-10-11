"""高德地图MCP服务封装"""
import json
import time
from typing import Any, Dict, List, Optional

from ..models.schemas import Location, POIInfo, RouteInfo, WeatherInfo
from .logging_setup import event as log_event
from .mcp_factory import get_amap_mcp_tool


class AmapService:
    """
    高德地图服务封装类

    缓存策略本身实现在 `api_cache` 模块，并在 `mcp_factory` 里挂到 MCPTool.run 上。
    这样做的原因：本服务只是 MCP 的两条调用路径之一，另一条是 Agent 编排层
    （SimpleAgent 直接调 MCPTool.run），把缓存只放在这里的话规划主链路会完全绕过缓存。
    """

    def __init__(self):
        """初始化服务"""
        self.mcp_tool = get_amap_mcp_tool()

    def _call_mcp(self, tool_name: str, arguments: dict) -> dict:
        """统一调用 MCP 并解析：剥前缀 → json.loads → 检查 error"""
        started = time.perf_counter()
        try:
            raw = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": tool_name,
                "arguments": arguments,
            })
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            log_event(
                "mcp.call", tool=tool_name, cache="miss", duration_ms=round(elapsed_ms, 1), success=False,
                arguments=arguments,
            ).error(f"❌ MCP 调用失败 {tool_name} {elapsed_ms:.0f}ms: {exc}")
            raise

        # 实测返回形如: "工具 'maps_weather' 执行结果:\n{...}"
        text = raw.split("执行结果:", 1)[-1].strip() if "执行结果:" in raw else raw.strip()
        elapsed_ms = (time.perf_counter() - started) * 1000

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log_event("mcp.call", tool=tool_name, cache="miss", duration_ms=round(elapsed_ms, 1), success=False).error(
                f"❌ MCP 返回非JSON {tool_name}: {text[:200]}"
            )
            raise RuntimeError(f"{tool_name} 返回非JSON: {text[:200]}")

        if isinstance(data, dict) and data.get("error"):
            log_event("mcp.call", tool=tool_name, cache="miss", duration_ms=round(elapsed_ms, 1), success=False,
                      error=str(data["error"])[:300]).error(f"❌ MCP 业务错误 {tool_name}: {data['error']}")
            raise RuntimeError(f"{tool_name} 调用失败: {data['error']}")

        return data


    def search_poi(self, keywords: str, city: str, citylimit: bool = True) -> List[POIInfo]:
        data = self._call_mcp("maps_text_search", {
            "keywords": keywords,
            "city": city,
            "citylimit": str(citylimit).lower()
        })
        pois = data.get("pois") or []
        if not pois:
            raise RuntimeError(f"未搜索到POI: {keywords} @ {city}")
        return [
            POIInfo(
                id=p.get("id", ""),
                name=p.get("name", ""),
                type=p.get("typecode", ""),   # 实测无可读类型名，只有 typecode
                address=p.get("address", ""),
                location=None,                # 实测不返回经纬度，需要时再调 geocode
            )
            for p in pois
        ]

    def get_weather(self, city: str) -> List[WeatherInfo]:
        data = self._call_mcp("maps_weather", {"city": city})
        forecasts = data.get("forecasts") or []
        if not forecasts:
            raise RuntimeError(f"未获取到天气: {city}")
        return [
            WeatherInfo(
                date=f.get("date", ""),
                day_weather=f.get("dayweather", ""),
                night_weather=f.get("nightweather", ""),
                day_temp=f.get("daytemp", 0),
                night_temp=f.get("nighttemp", 0),
                wind_direction=f.get("daywind", ""),
                wind_power=f.get("daypower", ""),
            )
            for f in forecasts
        ]


    def plan_route(self, origin_address, destination_address,
                   origin_city=None, destination_city=None,
                   route_type="walking") -> RouteInfo:
        tool_map = {
            "walking": "maps_direction_walking_by_address",
            "driving": "maps_direction_driving_by_address",
            "transit": "maps_direction_transit_integrated_by_address",
        }
        tool_name = tool_map.get(route_type, "maps_direction_walking_by_address")

        arguments = {"origin_address": origin_address,
                     "destination_address": destination_address}
        if origin_city:
            arguments["origin_city"] = origin_city
        if destination_city:
            arguments["destination_city"] = destination_city

        data = self._call_mcp(tool_name, arguments)
        paths = (data.get("route") or {}).get("paths") or []
        if not paths:
            raise RuntimeError(f"未规划出路线: {origin_address} → {destination_address}")

        path = paths[0]
        steps = path.get("steps") or []
        description = " → ".join(s.get("instruction", "") for s in steps[:5])
        return RouteInfo(
            distance=float(path.get("distance", 0)),   # 米
            duration=int(path.get("duration", 0)),     # 秒
            route_type=route_type,
            description=description,
        )


    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Location]:
        arguments = {"address": address}
        if city:
            arguments["city"] = city
        data = self._call_mcp("maps_geo", arguments)
        items = data.get("return") or []      # 注意：键名就是 "return"
        if not items:
            return None
        loc = items[0].get("location", "")
        if not loc or "," not in loc:
            return None
        lng, lat = loc.split(",")[:2]
        return Location(longitude=float(lng), latitude=float(lat))



    def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        """
        获取POI详情

        Args:
            poi_id: POI ID

        Returns:
            POI详情信息

        Raises:
            RuntimeError: 调用失败时抛出（不再静默返回空字典，
                          否则调用方无法区分「调用失败」与「确实没有详情」）
        """
        data = self._call_mcp("maps_search_detail", {"id": poi_id})

        # 详情接口部分返回体把内容包在 poi / data 里，统一摊平到顶层便于前端使用
        if not data:
            raise RuntimeError(f"未获取到POI详情: {poi_id}")
        return data


# 创建全局服务实例
_amap_service = None


def get_amap_service() -> AmapService:
    """获取高德地图服务实例(单例模式)"""
    global _amap_service
    
    if _amap_service is None:
        _amap_service = AmapService()
    
    return _amap_service

