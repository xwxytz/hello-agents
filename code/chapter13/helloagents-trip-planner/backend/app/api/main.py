"""FastAPI主应用"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import get_settings, print_config, validate_config
from ..services.logging_setup import logger, setup_logging
from .guard import ApiGuardMiddleware, guard_status
from .middleware import TraceIdMiddleware
from .routes import map as map_routes
from .routes import poi, trip, trips

# 日志必须在任何其他模块输出之前初始化，否则早期日志会走 loguru 默认格式、没有 trace_id
setup_logging()

# 获取配置
settings = get_settings()

# 创建FastAPI应用
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="基于HelloAgents框架的智能旅行规划助手API",
    docs_url="/docs",
    redoc_url="/redoc"
)

# 中间件的添加顺序 = 由内到外（越晚添加的越靠外），最终栈是：
#   TraceId → CORS → ApiGuard → 路由
# 这个顺序是刻意安排的：
# - ApiGuard 要在 CORS **里面**：它直接写回的 401/429 响应需要由外层的 CORS
#   中间件补上跨域头，否则浏览器只能看到一个 CORS 错误，看不到真正的原因；
# - TraceId 要在最外面：被拒掉的请求也要有 trace_id 与 request.summary 日志，
#   否则"谁在刷接口"这件事在日志里查不到。
app.add_middleware(ApiGuardMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# trace_id 中间件放在最后添加 = 位于最外层，CORS 预检请求也能带上 X-Trace-Id
app.add_middleware(TraceIdMiddleware)

# 注册路由
app.include_router(trip.router, prefix="/api")
app.include_router(poi.router, prefix="/api")
app.include_router(map_routes.router, prefix="/api")
app.include_router(trips.router, prefix="/api")


@app.on_event("startup")
async def startup_event():
    """应用启动事件"""
    logger.info("=" * 60)
    logger.info(f"🚀 {settings.app_name} v{settings.app_version}")
    logger.info("=" * 60)

    # 打印配置信息
    print_config()

    # 验证配置
    try:
        validate_config()
        logger.info("✅ 配置验证通过")
    except ValueError as e:
        logger.error(f"❌ 配置验证失败:\n{e}\n请检查.env文件并确保所有必要的配置项都已设置")
        raise

    # 装 token 探针：必须在预热之前，预热过程中的 LLM 调用才会被统计到
    try:
        from ..services import usage_probe
        from ..services.llm_service import get_llm

        usage_probe.install(get_llm())
    except Exception as e:
        logger.warning(f"⚠️ token 探针安装失败（不影响功能，token 将记为 unavailable）: {e}")

    # 行程持久化：失败只关掉分享功能，不让服务起不来
    try:
        from ..services import trip_store

        trip_store.init_db()
    except Exception as e:
        logger.warning(f"⚠️ 行程持久化初始化失败（分享功能不可用，规划不受影响）: {e}")

    # 预热多智能体系统：MCP 工具首次展开需要启动 uvx 子进程，耗时较长，
    # 放在启动阶段完成，避免首个请求长时间无响应
    try:
        from ..agents.trip_planner_agent import get_trip_planner_agent
        get_trip_planner_agent()
        logger.info("✅ 多智能体系统预热完成")
    except Exception as e:
        logger.warning(f"⚠️ 多智能体系统预热失败（首个请求会较慢）: {str(e)}")

    logger.info("📚 API文档: http://localhost:8000/docs")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭事件"""
    logger.info("👋 应用正在关闭...")


@app.get("/")
async def root():
    """根路径"""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
        "redoc": "/redoc"
    }


@app.get("/health")
def health():
    """
    健康检查（轻量探测：只读已初始化的单例状态，不发起任何真实请求）

    - LLM：读取 HelloAgentsLLM 实例的 provider / model
    - MCP：读取工具发现结果（子工具数 > 0 视为连通）
    两者都未就绪时整体状态为 degraded，但端点仍返回 200，
    便于外部探针区分「进程活着」与「依赖就绪」。
    """
    result = {
        "status": "healthy",
        "service": settings.app_name,
        "version": settings.app_version,
        "security": guard_status(),
    }

    # LLM 探测
    try:
        from ..services.llm_service import get_llm
        llm = get_llm()
        result["llm"] = {
            "configured": llm is not None,
            "provider": getattr(llm, "provider", None),
            "model": getattr(llm, "model", None),
        }
        if llm is None:
            result["status"] = "degraded"
    except Exception as e:
        result["llm"] = {"configured": False, "error": str(e)}
        result["status"] = "degraded"

    # MCP 探测：用 peek 而非 get，避免健康检查触发数十秒的 uvx 初始化
    # 行程库状态（不暴露任何令牌）
    try:
        from ..services import trip_store

        result["trip_store"] = trip_store.stats()
    except Exception as e:  # pragma: no cover - 健康检查不该因为库不可用而 500
        result["trip_store"] = {"enabled": False, "error": str(e)}

    try:
        from ..services.mcp_factory import peek_amap_mcp_tool
        tool = peek_amap_mcp_tool()
        if tool is None:
            result["mcp"] = {
                "connected": False,
                "tools_count": 0,
                "initialized": False,
                "note": "MCP 尚未初始化（预热未完成或失败）",
            }
            result["status"] = "degraded"
        else:
            tools = getattr(tool, "_available_tools", None) or []
            result["mcp"] = {
                "connected": bool(tools),
                "tools_count": len(tools),
                "initialized": True,
                "note": None if tools else "MCP 已初始化但未发现任何子工具",
            }
            if not tools:
                result["status"] = "degraded"
    except Exception as e:
        result["mcp"] = {"connected": False, "initialized": False, "error": str(e)}
        result["status"] = "degraded"

    return result


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=True
    )

