"""数据模型定义"""

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator

from ..services.logging_setup import logger

# 这段代码的核心作用是定义整个系统的数据结构“契约”。
#
# 可以把它们理解为系统内外部沟通的 “标准用语” 和 “校验规则”，主要服务于三大功能：
#
# 1. 定义 API 的请求和响应格式（接口契约）
# 前后端交互时，数据必须按约定格式传输。这些模型就是“合同”：
#
# TripRequest：约定了前端发起旅行规划请求时，必须发送哪些字段（如城市、日期、偏好），以及字段的类型和示例。
#
# TripPlanResponse：约定了后端处理后，必须返回什么样的成功/失败结构和数据。
#
# 其他如 POISearchResponse、RouteResponse 等，也为具体的 POI 搜索、路线规划等接口定义了统一的输出格式。
#
# 2. 自动校验和转换数据（安全与容错）
# 它们基于 Pydantic，能自动守护数据质量：
#
# 校验类型：如果前端误传了 "travel_days": "三天"（应为整数），系统会立刻报错，不会让错误数据进入业务逻辑。
#
# 数据清洗：如 WeatherInfo 中的 parse_temperature 方法，会自动将 "28°C" 这样的字符串清洗为整数 28，方便后续计算。
#
# 设置默认值与限制：如 travel_days 的 ge=1, le=30 限定了天数范围。
#
# 3. 生成 API 文档
# 你浏览器里看到的清晰文档（http://localhost:8000/docs）正是由这些模型自动生成的。Field(..., description=..., example=...) 里的信息，直接变成了文档里的字段说明和示例。

# ============ 请求模型 ============

class TripRequest(BaseModel):
    """旅行规划请求"""
    city: str = Field(..., description="目的地城市", example="北京")
    start_date: str = Field(..., description="开始日期 YYYY-MM-DD", example="2025-06-01")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD", example="2025-06-03")
    travel_days: int = Field(..., description="旅行天数", ge=1, le=30, example=3)
    transportation: str = Field(..., description="交通方式", example="公共交通")
    accommodation: str = Field(..., description="住宿偏好", example="经济型酒店")
    preferences: List[str] = Field(default=[], description="旅行偏好标签", example=["历史文化", "美食"])
    free_text_input: Optional[str] = Field(default="", description="额外要求", example="希望多安排一些博物馆")

    @field_validator("city", "transportation", "accommodation")
    @classmethod
    def _reject_overlong_short_text(cls, v: str) -> str:
        """
        城市/交通/住宿这类短文本超长，基本只有两种来源：误用或构造输入。
        它们会被拼进 prompt，不限长度等于把 token 消耗权交给调用方，因此直接拒绝而不是截断。
        """
        v = (v or "").strip()
        if len(v) > 50:
            raise ValueError(f"长度不能超过 50 个字符（当前 {len(v)}）")
        return v

    @field_validator("preferences")
    @classmethod
    def _cap_preferences(cls, v: List[str]) -> List[str]:
        """偏好项会被逐条拼进 prompt，限制条数与单项长度"""
        if len(v) > 10:
            raise ValueError(f"偏好标签最多 10 个（当前 {len(v)}）")
        for item in v:
            if len(item) > 20:
                raise ValueError(f"单个偏好标签不能超过 20 个字符：{item[:20]}...")
        return v

    @field_validator("free_text_input")
    @classmethod
    def _truncate_free_text(cls, v: Optional[str]) -> str:
        """
        自由文本超长是正常用户行为（不是攻击），直接报错体验太差，所以截断。

        截断会在文本末尾留下可见标记：这部分内容同样会进入 prompt，
        模型能知道自己拿到的是被截断的内容，而不是"用户就写了这么多"。
        """
        from ..config import get_settings

        limit = get_settings().max_text_input_len
        v = v or ""
        if len(v) > limit:
            logger.warning(
                f"✂️ free_text_input 超长被截断：{len(v)} → {limit} 字符"
            )
            return v[:limit] + f"…[输入已截断至{limit}字]"
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "city": "北京",
                "start_date": "2025-06-01",
                "end_date": "2025-06-03",
                "travel_days": 3,
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "preferences": ["历史文化", "美食"],
                "free_text_input": "希望多安排一些博物馆"
            }
        }


class POISearchRequest(BaseModel):
    """POI搜索请求"""
    keywords: str = Field(..., description="搜索关键词", example="故宫")
    city: str = Field(..., description="城市", example="北京")
    citylimit: bool = Field(default=True, description="是否限制在城市范围内")


class RouteRequest(BaseModel):
    """路线规划请求"""
    origin_address: str = Field(..., description="起点地址", example="北京市朝阳区阜通东大街6号")
    destination_address: str = Field(..., description="终点地址", example="北京市海淀区上地十街10号")
    origin_city: Optional[str] = Field(default=None, description="起点城市")
    destination_city: Optional[str] = Field(default=None, description="终点城市")
    route_type: str = Field(default="walking", description="路线类型: walking/driving/transit")


# ============ 响应模型 ============

class Location(BaseModel):
    """地理位置"""
    longitude: float = Field(..., description="经度")
    latitude: float = Field(..., description="纬度")


class Attraction(BaseModel):
    """景点信息"""
    name: str = Field(..., description="景点名称")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    visit_duration: int = Field(..., description="建议游览时间(分钟)")
    description: str = Field(..., description="景点描述")
    category: Optional[str] = Field(default="景点", description="景点类别")
    rating: Optional[float] = Field(default=None, description="评分")
    photos: Optional[List[str]] = Field(default_factory=list, description="景点图片URL列表")
    poi_id: Optional[str] = Field(default="", description="POI ID")
    image_url: Optional[str] = Field(default=None, description="图片URL")
    ticket_price: int = Field(default=0, description="门票价格(元)")


class Meal(BaseModel):
    """餐饮信息"""
    type: str = Field(..., description="餐饮类型: breakfast/lunch/dinner/snack")
    name: str = Field(..., description="餐饮名称")
    address: Optional[str] = Field(default=None, description="地址")
    location: Optional[Location] = Field(default=None, description="经纬度坐标")
    description: Optional[str] = Field(default=None, description="描述")
    estimated_cost: int = Field(default=0, description="预估费用(元)")


class Hotel(BaseModel):
    """酒店信息"""
    name: str = Field(..., description="酒店名称")
    address: str = Field(default="", description="酒店地址")
    location: Optional[Location] = Field(default=None, description="酒店位置")
    price_range: str = Field(default="", description="价格范围")
    rating: str = Field(default="", description="评分")
    distance: str = Field(default="", description="距离景点距离")
    type: str = Field(default="", description="酒店类型")
    estimated_cost: int = Field(default=0, description="预估费用(元/晚)")


class DayPlan(BaseModel):
    """单日行程"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_index: int = Field(..., description="第几天(从0开始)")
    description: str = Field(..., description="当日行程描述")
    transportation: str = Field(..., description="交通方式")
    accommodation: str = Field(..., description="住宿")
    hotel: Optional[Hotel] = Field(default=None, description="推荐酒店")
    attractions: List[Attraction] = Field(default=[], description="景点列表")
    meals: List[Meal] = Field(default=[], description="餐饮列表")


class WeatherInfo(BaseModel):
    """天气信息"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_weather: str = Field(default="", description="白天天气")
    night_weather: str = Field(default="", description="夜间天气")
    day_temp: Union[int, str] = Field(default=0, description="白天温度")
    night_temp: Union[int, str] = Field(default=0, description="夜间温度")
    wind_direction: str = Field(default="", description="风向")
    wind_power: str = Field(default="", description="风力")

    @field_validator('day_temp', 'night_temp', mode='before')
    @classmethod
    def parse_temperature(cls, v):
        """解析温度,移除°C等单位"""
        if isinstance(v, str):
            # 移除°C, ℃等单位符号
            v = v.replace('°C', '').replace('℃', '').replace('°', '').strip()
            try:
                return int(v)
            except ValueError:
                return 0
        return v


class Budget(BaseModel):
    """预算信息"""
    total_attractions: int = Field(default=0, description="景点门票总费用")
    total_hotels: int = Field(default=0, description="酒店总费用")
    total_meals: int = Field(default=0, description="餐饮总费用")
    total_transportation: int = Field(default=0, description="交通总费用")
    total: int = Field(default=0, description="总费用")


class TripPlan(BaseModel):
    """旅行计划"""
    city: str = Field(..., description="目的地城市")
    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    days: List[DayPlan] = Field(..., description="每日行程")
    weather_info: List[WeatherInfo] = Field(default=[], description="天气信息")
    overall_suggestions: str = Field(..., description="总体建议")
    budget: Optional[Budget] = Field(default=None, description="预算信息")


# ============ 行程存档（P2-3：持久化与分享）============

class TripShareInfo(BaseModel):
    """行程保存后返回的分享信息"""
    id: str = Field(..., description="记录ID")
    share_token: str = Field(..., description="分享令牌（出现在分享链接里）")
    share_path: str = Field(..., description="前端分享路径，如 /result?share=xxx")
    city: str = Field(default="", description="城市")
    created_at: Optional[str] = Field(default=None, description="保存时间")
    manage_token: Optional[str] = Field(
        default=None,
        description="管理令牌（仅创建时返回一次，用于删除；不会出现在分享链接里）",
    )


class TripSaveRequest(BaseModel):
    """保存一份已生成的行程"""
    request: TripRequest = Field(..., description="生成该行程时的原始请求")
    plan: TripPlan = Field(..., description="行程内容")


class TripRecordResponse(BaseModel):
    """行程存档读取响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[Dict[str, Any]] = Field(default=None, description="存档内容（含 request 与 plan）")


class TripPlanResponse(BaseModel):
    """旅行计划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[TripPlan] = Field(default=None, description="旅行计划数据")
    share: Optional[TripShareInfo] = Field(
        default=None,
        description="本次行程的分享信息；持久化未启用或保存失败时为 null（行程本身照常返回）",
    )


class POIInfo(BaseModel):
    """
    POI信息

    字段类型不能只按"理想情况"定义：实测高德 text_search 对部分 POI 返回的
    `address` 是**空列表**而不是字符串（例如「八达岭长城」），直接按 str 校验会
    抛 ValidationError，进而让整次检索失败。这里统一做一次宽容转换：
    列表按非空元素拼接，其他非字符串转字符串，缺失则为空串。
    """
    id: str = Field(..., description="POI ID")
    name: str = Field(..., description="名称")
    type: str = Field(..., description="类型")
    address: str = Field(..., description="地址")
    location: Optional[Location] = Field(default=None, description="经纬度坐标")
    tel: Optional[str] = Field(default=None, description="电话")

    @field_validator("id", "name", "type", "address", "tel", mode="before")
    @classmethod
    def _coerce_str(cls, v):
        """把上游偶尔返回的非字符串（列表 / None）收敛成字符串"""
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            return " ".join(str(x) for x in v if x not in (None, "", [], {}))
        return str(v)


class POISearchResponse(BaseModel):
    """POI搜索响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[POIInfo] = Field(default=[], description="POI列表")


class RouteInfo(BaseModel):
    """路线信息"""
    distance: float = Field(..., description="距离(米)")
    duration: int = Field(..., description="时间(秒)")
    route_type: str = Field(..., description="路线类型")
    description: str = Field(..., description="路线描述")


class RouteResponse(BaseModel):
    """路线规划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[RouteInfo] = Field(default=None, description="路线信息")


class WeatherResponse(BaseModel):
    """天气查询响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[WeatherInfo] = Field(default=[], description="天气信息")


# ============ 健康检查 ============

class SubAgentStatus(BaseModel):
    """子Agent状态"""
    role: str = Field(..., description="角色标识(attraction/weather/hotel/planner)")
    name: str = Field(default="", description="Agent名称")
    tools_count: int = Field(default=0, description="该Agent挂载的工具数量")


class MCPStatus(BaseModel):
    """MCP连接状态（通过工具发现结果判断，不发起真实调用）"""
    connected: bool = Field(..., description="是否已成功发现子工具")
    tools_count: int = Field(default=0, description="已展开的子工具数量")
    transport: Optional[str] = Field(default=None, description="MCP服务器启动命令")
    note: Optional[str] = Field(default=None, description="补充说明(如未展开时的后果)")


class LLMStatus(BaseModel):
    """LLM配置状态（只读实例信息，不发起请求）"""
    configured: bool = Field(..., description="LLM实例是否可用")
    provider: Optional[str] = Field(default=None, description="提供商")
    model: Optional[str] = Field(default=None, description="模型名")


class TripPlannerHealthResponse(BaseModel):
    """旅行规划服务健康检查响应"""
    status: str = Field(..., description="healthy / degraded")
    service: str = Field(default="trip-planner", description="服务标识")
    agents_count: int = Field(default=0, description="子Agent数量")
    agents: List[SubAgentStatus] = Field(default=[], description="各子Agent状态")
    mcp: MCPStatus = Field(..., description="MCP连接状态")
    llm: LLMStatus = Field(..., description="LLM配置状态")
    cache_groups: Optional[Dict[str, Any]] = Field(
        default=None,
        description="外部API缓存分组：每组的服务内条目数/上限/TTL（秒）。尚未初始化时为 null",
    )
    structured_output: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "结构化输出统计（进程内计数，重启清零）：plan_total/plan_ok/plan_failed/parse_retry，"
            "以及 success_rate 与 retry_per_plan——无样本时两者均为 null，不做估计；"
            "cache 子对象为外部API缓存命中情况（hits/misses/saved_calls/hit_rate）"
        ),
    )
    message: Optional[str] = Field(default=None, description="异常说明")


# ============ 错误响应 ============

class ErrorResponse(BaseModel):
    """错误响应"""
    success: bool = Field(default=False, description="是否成功")
    message: str = Field(..., description="错误消息")
    error_code: Optional[str] = Field(default=None, description="错误代码")

