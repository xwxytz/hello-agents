"""旅行规划API路由"""

import asyncio
import contextvars
import json
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ...agents.trip_planner_agent import (
    BudgetExceeded,
    StructuredOutputError,
    get_trip_planner_agent,
)
from ...models.schemas import (
    LLMStatus,
    MCPStatus,
    SubAgentStatus,
    TripPlan,
    TripPlannerHealthResponse,
    TripPlanResponse,
    TripRequest,
    TripShareInfo,
)
from ...services import trip_store
from ...services.logging_setup import event as log_event
from ...services.logging_setup import get_trace_id, logger

router = APIRouter(prefix="/trip", tags=["旅行规划"])

# SSE 心跳间隔（秒）：MCP 工具调用可能长时间无输出，需定期保活
SSE_HEARTBEAT_INTERVAL = 15


@router.post(
    "/plan",
    response_model=TripPlanResponse,
    summary="生成旅行计划",
    description="根据用户输入的旅行需求,生成详细的旅行计划"
)
async def plan_trip(request: TripRequest):
    """
    生成旅行计划

    Args:
        request: 旅行请求参数

    Returns:
        旅行计划响应

    这里是 async def：agent.plan_trip() 内部把三个检索 Agent 用
    asyncio.to_thread 并发，行程生成同样丢进线程，所以本协程全程不阻塞
    事件循环。（早期版本用 def 是为了避免同步阻塞卡住事件循环，
    现在阻塞调用都已在线程里，改回 async 才能拿到并发收益。）

    失败时返回 502 而不是 500：这里的失败来自上游 LLM / 高德 MCP，
    不是本服务的代码缺陷。且不再返回占位行程——宁可报错，也不给一份
    看起来完整、实际全是编造的行程。
    """
    # 请求入参在这里记：中间件不读 body（会消费 receive 通道），
    # 而这里已经拿到校验后的模型对象，记出来的字段更干净
    log_event(
        "trip.request",
        city=request.city,
        start_date=str(request.start_date),
        end_date=str(request.end_date),
        travel_days=request.travel_days,
        transportation=request.transportation,
        accommodation=request.accommodation,
        preferences=request.preferences,
        free_text_len=len(request.free_text_input or ""),
    ).info(f"📥 旅行规划请求: {request.city} {request.travel_days}天")

    try:
        agent = get_trip_planner_agent()
        trip_plan = await agent.plan_trip(request)

        log_event(
            "trip.result",
            city=trip_plan.city,
            days=len(trip_plan.days),
            attractions=sum(len(d.attractions) for d in trip_plan.days),
        ).info("✅ 旅行计划生成成功，准备返回响应")

        # 顺手落库并生成分享链接。保存失败不影响本次结果：行程已经在手上了，
        # 因为存不进去而让用户重跑一次十秒钟的规划是不合理的。
        # 两层 getattr：FakeAgent（测试替身）连 llm 属性都没有，
        # 取模型名这种"锦上添花"的信息不该让整个请求失败
        model_name = getattr(getattr(agent, "llm", None), "model", None)
        share = await asyncio.to_thread(
            trip_store.save_plan,
            request,
            trip_plan,
            model_name,
            get_trace_id(),
        )

        return TripPlanResponse(
            success=True,
            message="旅行计划生成成功",
            data=trip_plan,
            share=TripShareInfo(**share) if share else None,
        )

    except BudgetExceeded as e:
        # 超时止损 = 上游太慢（504）；token 预算止损 = 本次请求消耗被主动掐断（429）
        status_code = 504 if e.kind == "timeout" else 429
        error_code = "BUDGET_TIMEOUT_EXCEEDED" if e.kind == "timeout" else "BUDGET_TOKEN_EXCEEDED"
        log_event("trip.budget_exceeded", error_code=error_code, reason=str(e)).error(str(e))
        raise HTTPException(
            status_code=status_code,
            detail={"success": False, "error_code": error_code, "message": str(e)},
        )
    except StructuredOutputError as e:
        # 结构化输出失败单独一个错误码，便于和上游故障区分开
        log_event("trip.failed", error_code="STRUCTURED_OUTPUT_FAILED", reason=str(e)).error(
            f"❌ 行程结构化输出失败: {str(e)}"
        )
        raise HTTPException(
            status_code=502,
            detail={
                "success": False,
                "error_code": "STRUCTURED_OUTPUT_FAILED",
                "message": str(e),
            },
        )
    except Exception as e:
        log_event("trip.failed", error_code=type(e).__name__, reason=str(e)).exception(
            f"❌ 生成旅行计划失败: {str(e)}"
        )
        raise HTTPException(
            status_code=502,
            detail={
                "success": False,
                "error_code": type(e).__name__,
                "message": f"生成旅行计划失败: {str(e)}",
            },
        )


@router.post(
    "/plan/stream",
    summary="流式生成旅行计划",
    description="以 SSE 方式逐步推送各 Agent 的执行阶段与最终结果"
)
async def plan_trip_stream(request: TripRequest):
    """
    流式生成旅行计划（Server-Sent Events）

    事件类型：
    - stage: 阶段开始/结束（index/total 可用于计算真实进度）
    - result: 最终 TripPlan
    - error: 生成失败，包含错误码与失败阶段

    实现说明：
    MultiAgentTripPlanner 的 plan_trip_stream() 是同步生成器，
    直接在 async 路由里迭代会阻塞整个事件循环，因此放到工作线程执行，
    通过 asyncio.Queue 把事件回投到事件循环。
    """
    log_event(
        "trip.request",
        stream=True,
        city=request.city,
        start_date=str(request.start_date),
        end_date=str(request.end_date),
        travel_days=request.travel_days,
    ).info(f"📥 流式旅行规划请求: {request.city} {request.travel_days}天")

    agent = get_trip_planner_agent()
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def worker():
        """在工作线程中驱动同步生成器，把事件回投到事件循环"""
        try:
            for event in agent.plan_trip_stream(request):
                # result 事件里顺带带上分享信息，前端不用为了拿链接再发一次请求。
                # 这里已经在 worker 线程里，直接同步保存即可。
                if event.get("type") == "result":
                    try:
                        plan = TripPlan(**(event.get("data") or {}))
                        model_name = getattr(getattr(agent, "llm", None), "model", None)
                        record = trip_store.save_plan(request, plan, model_name, get_trace_id())
                        if record:
                            event["share"] = record
                    except Exception as exc:  # 保存失败不打断流式返回
                        logger.warning(f"⚠️ 流式规划的行程保存失败（结果不受影响）: {exc}")
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # 生成器内部异常兜底，确保一定投放结束哨兵
            loop.call_soon_threadsafe(queue.put_nowait, {
                "type": "error",
                "code": "STREAM_FAILED",
                "message": str(exc),
                "stage": None,
            })
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    # threading.Thread 不会继承 contextvars（asyncio.to_thread 会），
    # 不显式传 context 的话，线程内所有日志的 trace_id 都会是默认值 "-"，
    # 这条链路就串不起来了。这里复制当前 context 并在其中运行 worker。
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(worker,), daemon=True).start()

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                # 注释行保活，避免代理层因长时间无数据断开连接
                yield ": ping\n\n"
                continue

            if event is None:  # 结束哨兵
                return

            yield (
                f"event: {event.get('type', 'message')}\n"
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get(
    "/health",
    response_model=TripPlannerHealthResponse,
    summary="健康检查",
    description=(
        "检查多智能体系统的真实状态：各子Agent名称与挂载工具数、MCP工具发现结果、LLM配置。"
        "只读已初始化实例的状态，不发起任何真实调用。"
    )
)
def health_check():
    """
    健康检查

    原实现访问 `agent.agent.name`，但 get_trip_planner_agent() 返回的是
    MultiAgentTripPlanner（没有 .agent 属性），必然抛 AttributeError → 500。
    现改为反映 4 个子Agent + MCP + LLM 的真实状态。

    注意：get_trip_planner_agent() 在未预热时会同步阻塞（启动 uvx 子进程），
    因此本路由用 def 而非 async def，交由 FastAPI 线程池执行。
    """
    try:
        agent = get_trip_planner_agent()
        status = agent.get_health_status()

        mcp_ok = bool(status["mcp"]["connected"])
        llm_ok = bool(status["llm"]["configured"])

        return TripPlannerHealthResponse(
            status="healthy" if (mcp_ok and llm_ok) else "degraded",
            service="trip-planner",
            agents_count=status["agents_count"],
            agents=[SubAgentStatus(**a) for a in status["agents"]],
            mcp=MCPStatus(**status["mcp"]),
            llm=LLMStatus(**status["llm"]),
            cache_groups=status.get("cache_groups"),
            structured_output=status.get("structured_output"),
            message=None if (mcp_ok and llm_ok) else (
                status["mcp"]["note"] if not mcp_ok else "LLM 未配置"
            ),
        )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"服务不可用: {str(e)}"
        )

