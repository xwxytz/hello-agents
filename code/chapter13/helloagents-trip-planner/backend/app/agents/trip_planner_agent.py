"""多智能体旅行规划系统"""
import asyncio
import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterator, List, Optional, Tuple

from hello_agents import SimpleAgent

from ..config import get_settings
from ..models.schemas import TripPlan, TripRequest

# ============ Agent提示词 ============
from ..Prompt.prompt import ATTRACTION_AGENT_PROMPT, HOTEL_AGENT_PROMPT, PLANNER_AGENT_PROMPT, WEATHER_AGENT_PROMPT
from ..services import usage_probe

# from hello_agents.tools import MCPTool
from ..services.llm_service import get_llm
from ..services.logging_setup import event as log_event
from ..services.logging_setup import logger
from ..services.mcp_factory import get_amap_mcp_tool
from ..services.struct_metrics import (
    record as record_metric,
)
from ..services.struct_metrics import (
    record_timing,
)
from ..services.struct_metrics import (
    snapshot as metrics_snapshot,
)


class BudgetExceeded(RuntimeError):
    """
    单次规划超出预算（耗时或 token）。

    单独定义的原因：这是「主动止损」而不是「上游故障」，路由层需要把它和
    MCP 失败、结构化输出失败区分开，返回不同的状态码。

    Attributes:
        kind: "timeout"（超时止损 → 504）或 "tokens"（token 预算止损 → 429）
    """

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


class StructuredOutputError(RuntimeError):
    """
    行程结构化输出失败（JSON 解析失败或 Pydantic 校验失败，且重试后仍未通过）。

    单独定义异常类型的原因：路由层需要把它与「MCP 调用失败」「LLM 不可用」区分开，
    返回不同的 HTTP 状态码与错误码，便于前端与日志定位是哪一环出了问题。
    """


def _looks_like_json_mode_error(exc: Exception) -> bool:
    """
    判断异常是否由「端点不支持 response_format=json_object」引起。

    仅在异常文本命中特征词时才判定为不支持，避免把鉴权失败、限流、
    网络错误等真实故障误判成能力不支持而被静默吞掉。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(token in text for token in ("response_format", "json_object", "json mode", "json_mode"))


class MultiAgentTripPlanner:
    """多智能体旅行规划系统"""

    def __init__(self):
        """初始化多智能体系统"""
        logger.info("🔄 开始初始化多智能体旅行规划系统...")

        try:
            settings = get_settings()
            self.llm = get_llm()

            # 共享 MCP 工具实例（与 amap_service 共用同一份，避免两套配置漂移）
            logger.debug("  - 获取共享MCP工具")
            self.amap_tool = get_amap_mcp_tool()

            # 创建景点搜索Agent
            logger.debug("  - 创建景点搜索Agent")
            self.attraction_agent = SimpleAgent(
                name="景点搜索专家",
                llm=self.llm,
                system_prompt=ATTRACTION_AGENT_PROMPT
            )
            self.attraction_agent.add_tool(self.amap_tool)

            # 创建天气查询Agent
            logger.debug("  - 创建天气查询Agent")
            self.weather_agent = SimpleAgent(
                name="天气查询专家",
                llm=self.llm,
                system_prompt=WEATHER_AGENT_PROMPT
            )
            self.weather_agent.add_tool(self.amap_tool)

            # 创建酒店推荐Agent
            logger.debug("  - 创建酒店推荐Agent")
            self.hotel_agent = SimpleAgent(
                name="酒店推荐专家",
                llm=self.llm,
                system_prompt=HOTEL_AGENT_PROMPT
            )
            self.hotel_agent.add_tool(self.amap_tool)

            # 创建行程规划Agent(不需要工具)
            logger.debug("  - 创建行程规划Agent")
            self.planner_agent = SimpleAgent(
                name="行程规划专家",
                llm=self.llm,
                system_prompt=PLANNER_AGENT_PROMPT
            )

            logger.bind(
                attraction_tools=len(self.attraction_agent.list_tools()),
                weather_tools=len(self.weather_agent.list_tools()),
                hotel_tools=len(self.hotel_agent.list_tools()),
            ).info("✅ 多智能体系统初始化成功")

            # 规划流程串行锁：SimpleAgent 会在每次 run() 后写入 _history，
            # 多请求并发时会互相污染上下文，此处用锁保证同一时刻只有一个规划流程在跑。
            # 注意：锁只管「跨请求」——同一个请求内的三路检索用的是三个不同 Agent
            # 实例，各自独立 history，所以可以在锁内并发。
            # 同步路径用 threading.Lock，异步路径必须用 asyncio.Lock，
            # 否则 await 期间会把整个事件循环堵死，并发就白做了。
            self._plan_lock = threading.Lock()
            self._async_plan_lock = asyncio.Lock()

            # 行程 JSON 很长（N 天 × 每天 2-3 景点 + 三餐），max_tokens 给小了会被截断，
            # 截出来的半截 JSON 必然解析失败。DeepSeek 官方 JSON Output 文档明确要求
            # 「合理设置 max_tokens，防止 JSON 字符串被中途截断」。
            self._planner_max_tokens = 8192

            # 单次规划的预算上限。注意这是「协作式检查」：Python 没有办法安全地强杀
            # 一个正阻塞在 IO 里的线程，所以只能在阶段之间检查是否已经超预算，
            # 超了就停止往下走（而不是精确地在第 N 秒掐断）。
            self._plan_timeout_s = settings.plan_timeout_s
            self._plan_token_budget = settings.plan_token_budget
            # 端点是否支持 response_format=json_object。首次调用失败且命中特征词时置 False，
            # 之后不再重试该能力，避免每个请求都白试一遍。
            self._json_mode_supported = True

        except Exception as e:
            logger.exception(f"❌ 多智能体系统初始化失败: {str(e)}")
            raise
    
    def _check_budget(self, started: float, tokens_before: Optional[Dict[str, Any]], stage: str) -> None:
        """
        检查是否已超出单次规划的开销预算，超出则抛 BudgetExceeded 止损。

        这是**协作式**检查：只在阶段之间检查，无法中断正在进行中的那次阻塞调用。
        但它能挡住真正烧钱的情况——重试循环、超长 prompt 导致的一轮轮大模型调用。

        Args:
            started: 本次规划开始的时间点（perf_counter）
            tokens_before: 规划开始时的 token 快照；探针不可用时为 None
            stage: 当前所处阶段，用于错误信息定位
        """
        elapsed = time.perf_counter() - started
        if elapsed > self._plan_timeout_s:
            raise BudgetExceeded(
                f"规划超时止损：{stage} 阶段累计 {elapsed:.1f}s，"
                f"超过上限 {self._plan_timeout_s}s",
                kind="timeout",
            )

        if tokens_before:
            spent = usage_probe.diff(tokens_before, usage_probe.snapshot())
            total = spent.get("total_tokens")
            if spent.get("available") and total is not None and total > self._plan_token_budget:
                raise BudgetExceeded(
                    f"token 预算止损：{stage} 阶段累计消耗 {total} tokens，"
                    f"超过上限 {self._plan_token_budget}",
                    kind="tokens",
                )

    def _retrieval_tasks(self, request: TripRequest) -> List[Tuple[str, Any, str, str]]:
        """
        三路检索任务定义：(键, Agent 实例, 查询文本, 展示文案)

        景点 / 天气 / 酒店三者之间没有数据依赖，可以并发；行程规划必须等三者齐了再跑。
        """
        return [
            ("attraction", self.attraction_agent, self._build_attraction_query(request), "景点检索"),
            ("weather", self.weather_agent, f"请查询{request.city}的天气信息", "天气查询"),
            ("hotel", self.hotel_agent, f"请搜索{request.city}的{request.accommodation}酒店", "酒店推荐"),
        ]

    def _run_retrieval_agent(self, key: str, agent, query: str) -> str:
        """
        执行单个检索 Agent 并记录耗时。

        异常直接向上抛，由 _collect_retrieval() 统一决定降级还是整体失败。
        """
        log_event("agent.start", agent=key).info(f"▶️ {key} 检索开始")
        started = time.perf_counter()
        try:
            result = agent.run(query)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            record_timing(key, elapsed)
            log_event("agent.end", agent=key, duration_ms=round(elapsed * 1000, 1), success=False).error(
                f"❌ {key} 检索失败 {elapsed:.2f}s: {exc}"
            )
            raise
        elapsed = time.perf_counter() - started
        record_timing(key, elapsed)
        log_event("agent.end", agent=key, duration_ms=round(elapsed * 1000, 1), success=True).info(
            f"✅ {key} 检索完成 {elapsed:.2f}s"
        )
        return result

    def _collect_retrieval(self, key: str, outcome: Any) -> str:
        """
        把一路检索的结果（或异常）收敛成字符串。

        降级策略：
        - 景点是行程的核心，检索失败就整体失败。检索不到却继续，planner 只能凭空
          编景点，那是比报错更糟的结果（实测 city=火星 就是这种形态）。
        - 天气 / 酒店失败只降级为空字符串，行程照常生成，只是缺这部分信息。
        """
        if not isinstance(outcome, BaseException):
            return outcome

        if key == "attraction":
            raise RuntimeError(f"景点检索失败: {outcome}") from outcome

        record_metric(f"{key}_degraded")
        log_event("retrieval.degraded", agent=key, reason=str(outcome)).warning(
            f"⚠️ {key} 检索失败，降级为空（不阻断整体行程）: {outcome}"
        )
        return ""

    async def _gather_retrieval(self, request: TripRequest) -> Dict[str, str]:
        """
        并发执行三路检索。

        SimpleAgent.run() 是同步阻塞的（内含 LLM 请求 + MCP 子进程调用），
        必须用 asyncio.to_thread 包一层，否则会堵住事件循环，gather 形同串行。
        MCPTool.run() 每次调用都新建独立的 MCPClient 与事件循环，无共享状态，
        所以三路并发是安全的。
        """
        tasks = self._retrieval_tasks(request)
        keys = [key for key, _, _, _ in tasks]
        log_event("retrieval.start", agents=keys).info(f"🔀 并发启动三路检索: {', '.join(keys)}")

        outcomes = await asyncio.gather(
            *[
                asyncio.to_thread(self._run_retrieval_agent, key, agent, query)
                for key, agent, query, _ in tasks
            ],
            return_exceptions=True,
        )

        return {
            key: self._collect_retrieval(key, outcome)
            for (key, _, _, _), outcome in zip(tasks, outcomes)
        }

    async def plan_trip(self, request: TripRequest) -> TripPlan:
        """
        使用多智能体协作生成旅行计划（三路检索并发版）

        失败行为：任何一步失败都向上抛出异常，由路由层转成明确的错误响应。
        早期版本在异常分支返回一个占位行程（"XX景点1"），那等于把失败伪装成
        成功——用户拿到的是一份看起来完整、内容却全是编造的行程，比报错危险得多。

        Args:
            request: 旅行请求

        Returns:
            旅行计划

        Raises:
            StructuredOutputError: 行程 JSON 解析/校验失败，且自纠重试后仍未通过
            RuntimeError: 景点检索失败，或 MCP 工具调用等上游错误
        """
        record_metric("plan_total")
        started = time.perf_counter()
        tokens_before = usage_probe.snapshot()

        async with self._async_plan_lock:
            try:
                log_event(
                    "plan.start",
                    city=request.city,
                    start_date=str(request.start_date),
                    end_date=str(request.end_date),
                    travel_days=request.travel_days,
                    preferences=request.preferences,
                ).info(f"🚀 开始规划: {request.city} {request.travel_days}天")

                collected = await self._gather_retrieval(request)
                for key in ("attraction", "weather", "hotel"):
                    logger.debug(f"{key} 检索结果: {collected[key][:200]}")

                # 第一个检查点：三路检索是整个流程中外部调用最密集的一段
                self._check_budget(started, tokens_before, "retrieval")

                # 行程规划必须等三路结果齐了再跑
                logger.info("📋 生成行程计划（JSON 模式 + 自纠重试）")
                planner_query = self._build_planner_query(
                    request,
                    collected["attraction"],
                    collected["weather"],
                    collected["hotel"],
                )
                trip_plan = await asyncio.to_thread(
                    self._run_planner_with_retry, planner_query, 3, started, tokens_before
                )

                elapsed = time.perf_counter() - started
                record_timing("end_to_end", elapsed)
                log_event("plan.end", duration_ms=round(elapsed * 1000, 1), success=True).info(
                    f"✅ 旅行计划生成完成，端到端 {elapsed:.2f}s"
                )

                record_metric("plan_ok")
                return trip_plan

            except Exception as e:
                record_metric("plan_failed")
                logger.exception(f"❌ 生成旅行计划失败: {str(e)}")
                raise
    
    def plan_trip_stream(self, request: TripRequest) -> Iterator[Dict[str, Any]]:
        """
        流式版多智能体旅行规划

        逐步 yield 事件字典，供 SSE 端点消费。与 plan_trip() 的关键区别：
        解析或校验失败时 yield error 事件，而不是静默返回
        _create_fallback_plan() 生成的占位行程——流式场景下把假数据当成
        成功结果返回，比明确报错危险得多。

        三路检索同样并发执行（用线程池而非 asyncio，因为这里是同步生成器，
        由 SSE 端点放在工作线程里驱动），end 事件按完成先后推送。

        Yields:
            事件字典，type 取值为 stage / result / error
        """
        # 4 个进度格：三路检索各占一格 + 行程规划一格
        total = 4
        collected: Dict[str, str] = {}
        current_stage = None

        record_metric("plan_total")
        started = time.perf_counter()
        tokens_before = usage_probe.snapshot()

        log_event(
            "plan.start",
            stream=True,
            city=request.city,
            travel_days=request.travel_days,
        ).info(f"🚀 开始流式规划: {request.city} {request.travel_days}天")

        # 串行锁：避免多个请求的 Agent 历史互相污染
        with self._plan_lock:
            try:
                # 阶段 1-3：三路检索并发执行，按「完成先后」推送 end 事件。
                # end 的 index 按完成顺序重新编号为 1/2/3，这样前端进度条仍然
                # 单调递增（前端按 index/total 算百分比），前端无需改动。
                tasks = self._retrieval_tasks(request)
                current_stage = "retrieval"
                yield {
                    "type": "stage",
                    "stage": "retrieval",
                    "status": "start",
                    "label": "正在并行检索景点、天气、酒店",
                    "index": 1,
                    "total": total,
                }

                done = 0
                with ThreadPoolExecutor(max_workers=3) as pool:
                    # ThreadPoolExecutor 不会传播 contextvars（asyncio.to_thread 会），
                    # 不显式把 context 带进提交的任务，线程内日志的 trace_id 就会是
                    # 默认值 "-"，这条链路在日志里就断了。每个任务各复制一份 context。
                    futures = {}
                    for key, agent, query, label in tasks:
                        ctx = contextvars.copy_context()
                        futures[pool.submit(ctx.run, self._run_retrieval_agent, key, agent, query)] = (key, label)
                    for future in as_completed(futures):
                        key, label = futures[future]
                        done += 1
                        try:
                            outcome = future.result()
                        except Exception as exc:
                            outcome = exc
                        collected[key] = self._collect_retrieval(key, outcome)
                        yield {
                            "type": "stage",
                            "stage": key,
                            "status": "end",
                            "label": f"{label}完成",
                            "index": done,
                            "total": total,
                        }

                # 第一个检查点：三路检索是外部调用最密集的一段
                self._check_budget(started, tokens_before, "retrieval")

                # 阶段 4：行程规划，必须等三路结果齐了再跑
                current_stage = "planner"
                yield {
                    "type": "stage",
                    "stage": "planner",
                    "status": "start",
                    "label": "正在生成行程",
                    "index": total,
                    "total": total,
                }
                logger.info("📋 生成行程计划")
                planner_query = self._build_planner_query(
                    request,
                    collected.get("attraction", ""),
                    collected.get("weather", ""),
                    collected.get("hotel", ""),
                )
                trip_plan = self._run_planner_with_retry(planner_query, 3, started, tokens_before)
                record_metric("plan_ok")
                yield {"type": "result", "data": trip_plan.model_dump(mode="json")}
                yield {
                    "type": "stage",
                    "stage": "planner",
                    "status": "end",
                    "label": "行程生成完成",
                    "index": total,
                    "total": total,
                }

                elapsed = time.perf_counter() - started
                record_timing("end_to_end", elapsed)
                log_event("plan.end", stream=True, duration_ms=round(elapsed * 1000, 1), success=True).info(
                    f"✅ 流式旅行计划生成完成，端到端 {elapsed:.2f}s"
                )

            except Exception as e:
                record_metric("plan_failed")
                logger.exception(f"❌ 流式生成旅行计划失败: {str(e)}")
                yield {
                    "type": "error",
                    "code": type(e).__name__,
                    "message": str(e),
                    "stage": current_stage,
                }

    def _invoke_planner(self, messages: List[Dict[str, str]]):
        """
        调用行程规划使用的 LLM。

        优先带 response_format={"type": "json_object"}（DeepSeek 官方 JSON Output）。
        官方对 JSON Output 提了三点要求：设置该参数、提示词中指导 JSON 格式、
        合理设置 max_tokens 防止截断。前两点 PLANNER_AGENT_PROMPT 已满足
        （"请严格按照以下JSON格式返回旅行计划" + 完整 schema 示例），
        max_tokens 在这里显式给足。

        注意：JSON 模式只保证「输出是合法 JSON」，不保证字段符合我们的 schema，
        所以 _parse_strict() 里的 Pydantic 校验这一层不能省。

        只对 planner 开启是因为：三个检索 Agent 挂载了工具，走的是 SimpleAgent 的
        Function Calling 分支（invoke_with_tools），JSON 模式与工具调用能否共存
        在官方文档里没查到明确结论，不去赌；planner 不带工具，是安全的。

        若端点明确不支持该参数（异常文本命中特征词），则永久降级为纯 prompt 约束，
        避免每个请求都白试一遍。
        """
        call_kwargs = {"temperature": 0, "max_tokens": self._planner_max_tokens}

        if self._json_mode_supported:
            try:
                return self.llm.invoke(messages, response_format={"type": "json_object"}, **call_kwargs)
            except Exception as exc:
                if not _looks_like_json_mode_error(exc):
                    raise
                self._json_mode_supported = False
                logger.warning(f"⚠️ 当前端点不支持 response_format=json_object，降级为纯 prompt 约束: {exc}")

        return self.llm.invoke(messages, **call_kwargs)

    def _run_planner_with_retry(
        self,
        planner_query: str,
        max_attempts: int = 3,
        started: Optional[float] = None,
        tokens_before: Optional[Dict[str, Any]] = None,
    ) -> TripPlan:
        """
        生成行程并校验，失败时把错误回灌给模型自纠，最多尝试 max_attempts 次。

        重试是有成本的：每一次重试都要把上一轮的失败输出连同错误说明一起发回去，
        prompt 只增不减。所以每次重试前先查预算，超了就停止重试并抛 BudgetExceeded
        ——宁可失败，也不要让一次异常输入滚出一串越来越贵的调用。

        这里绕过 SimpleAgent.run() 直接调 LLM，原因：
        SimpleAgent.run() 在结束时无条件把本轮 user/assistant 追加进 _history，
        而 _build_messages() 每次都会把 _history 拼进 messages——直接重试等于把
        上一轮那个解析失败的 JSON 当作历史一起发回去，既浪费 token，还会把模型
        往同一个错法上带。planner 本身不带工具，自己维护 messages 更可控，
        代价是放弃 SimpleAgent 的 trace/history 能力。

        Args:
            planner_query: 行程规划查询文本
            max_attempts: 最大尝试次数（含首次）

        Returns:
            通过 Pydantic 校验的旅行计划

        Raises:
            StructuredOutputError: 连续 max_attempts 次都没产出合法结构
        """
        messages = [
            {"role": "system", "content": PLANNER_AGENT_PROMPT},
            {"role": "user", "content": planner_query},
        ]
        last_error = None

        for attempt in range(1, max_attempts + 1):
            # 重试是这个流程里唯一会把开销放大的地方，每次尝试前都要查预算
            if attempt > 1 and started is not None:
                self._check_budget(started, tokens_before, f"planner(第{attempt}次重试前)")

            response = self._invoke_planner(messages)
            raw = response.content if hasattr(response, "content") else str(response)

            try:
                plan = self._parse_strict(raw)
                log_event("plan.parsed", attempt=attempt, days=len(plan.days)).info(
                    f"✅ 行程 JSON 校验通过（第 {attempt} 次尝试）"
                )
                return plan
            except Exception as exc:
                last_error = exc
                record_metric("parse_retry")
                log_event(
                    "plan.retry",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                ).warning(f"⚠️ 行程结构化输出第 {attempt}/{max_attempts} 次校验失败: {type(exc).__name__}")

                # 把失败原文与错误一起回灌，让模型对着自己的错误改
                messages.append({"role": "assistant", "content": raw[:4000]})
                messages.append({"role": "user", "content": (
                    "你上一次的输出没有被系统接受，错误如下：\n"
                    f"{type(exc).__name__}: {exc}\n"
                    "请只输出符合系统提示词中 JSON schema 的 json，不要输出任何解释文字、"
                    "Markdown 代码围栏或额外说明。若上次输出疑似被长度限制截断，"
                    "请缩短 description 等长文本字段后重新输出完整 JSON。"
                )})

        raise StructuredOutputError(
            f"行程结构化输出连续 {max_attempts} 次未通过校验，"
            f"最后一次错误：{type(last_error).__name__}: {last_error}"
        )

    def _parse_strict(self, response: str) -> TripPlan:
        """
        严格解析 Agent 响应为 TripPlan

        与 _parse_response() 的区别：解析或 Pydantic 校验失败时直接抛出异常，
        不调用 _create_fallback_plan() 生成占位行程。

        Args:
            response: Agent 响应文本

        Returns:
            旅行计划

        Raises:
            ValueError: 响应中未找到 JSON 数据
            json.JSONDecodeError / ValidationError: 解析或校验失败
        """
        # 开启 JSON 模式后模型应直接返回裸 JSON，优先整段解析；
        # 解析不通再走下面的围栏/大括号提取（降级为纯 prompt 约束时的形态）
        try:
            return TripPlan(**json.loads((response or "").strip()))
        except Exception:
            pass

        if "```json" in response:
            json_start = response.find("```json") + 7
            json_end = response.find("```", json_start)
            json_str = response[json_start:json_end].strip()
        elif "```" in response:
            json_start = response.find("```") + 3
            json_end = response.find("```", json_start)
            json_str = response[json_start:json_end].strip()
        elif "{" in response and "}" in response:
            json_start = response.find("{")
            json_end = response.rfind("}") + 1
            json_str = response[json_start:json_end]
        else:
            raise ValueError("响应中未找到JSON数据")

        data = json.loads(json_str)
        return TripPlan(**data)

    def get_health_status(self) -> Dict[str, Any]:
        """
        返回多智能体系统的真实运行状态（供健康检查端点使用）

        只读取已初始化的实例状态，不发起任何真实调用：
        - MCP 是否连通，以「工具发现成功(子工具数 > 0)」为判据
        - LLM 是否可用，以实例是否创建成功 + provider/model 为判据
        """
        agents = []
        for role, agent in (
            ("attraction", self.attraction_agent),
            ("weather", self.weather_agent),
            ("hotel", self.hotel_agent),
            ("planner", self.planner_agent),
        ):
            try:
                tools_count = len(agent.list_tools())
            except Exception:
                tools_count = 0
            agents.append({
                "role": role,
                "name": getattr(agent, "name", ""),
                "tools_count": tools_count,
            })

        mcp_tools = getattr(self.amap_tool, "_available_tools", None) or []
        server_command = getattr(self.amap_tool, "server_command", None) or []

        # 缓存分组状态（挂在 MCPTool.run 上的那层缓存）
        try:
            from ..services.api_cache import all_groups

            cache_groups = all_groups() or None
        except Exception:
            cache_groups = None

        return {
            "agents_count": len(agents),
            "agents": agents,
            "cache_groups": cache_groups,
            "mcp": {
                "connected": bool(mcp_tools),
                "tools_count": len(mcp_tools),
                "transport": " ".join(str(c) for c in server_command) or None,
                "note": None if mcp_tools else (
                    "MCP工具未展开，Agent 的 [TOOL_CALL:amap_*] 将全部命中失败，"
                    "景点/天气/酒店会退化为纯 LLM 幻觉输出"
                ),
            },
            "llm": {
                "configured": self.llm is not None,
                "provider": getattr(self.llm, "provider", None),
                "model": getattr(self.llm, "model", None),
            },
            "structured_output": metrics_snapshot(),
        }

    def _build_attraction_query(self, request: TripRequest) -> str:
        """构建景点搜索查询 - 直接包含工具调用"""
        keywords = []
        if request.preferences:
            # 只取第一个偏好作为关键词
            keywords = request.preferences[0]
        else:
            keywords = "景点"

        # 直接返回工具调用格式
        query = f"请使用amap_maps_text_search工具搜索{request.city}的{keywords}相关景点。\n[TOOL_CALL:amap_maps_text_search:keywords={keywords},city={request.city}]"
        return query

    def _build_planner_query(self, request: TripRequest, attractions: str, weather: str, hotels: str = "") -> str:
        """构建行程规划查询"""
        query = f"""请根据以下信息生成{request.city}的{request.travel_days}天旅行计划:

**基本信息:**
- 城市: {request.city}
- 日期: {request.start_date} 至 {request.end_date}
- 天数: {request.travel_days}天
- 交通方式: {request.transportation}
- 住宿: {request.accommodation}
- 偏好: {', '.join(request.preferences) if request.preferences else '无'}

**景点信息:**
{attractions}

**天气信息:**
{weather}

**酒店信息:**
{hotels}

**要求:**
1. 每天安排2-3个景点
2. 每天必须包含早中晚三餐
3. 每天推荐一个具体的酒店(从酒店信息中选择)
3. 考虑景点之间的距离和交通方式
4. 返回完整的JSON格式数据
5. 景点的经纬度坐标要真实准确
"""
        if request.free_text_input:
            query += f"\n**额外要求:** {request.free_text_input}"

        return query
    
    # 说明：_parse_response() 与 _create_fallback_plan() 已删除。
    # 原实现在解析失败时返回一份占位行程（"XX景点1" / "当地特色早餐"），
    # 把 LLM 或 MCP 的失败伪装成一次成功的规划——用户拿到的是一份看起来完整、
    # 内容却全是编造的行程。现在统一由 _parse_strict() 抛异常，
    # _run_planner_with_retry() 做自纠重试，仍失败则向上抛 StructuredOutputError，
    # 由路由层返回明确的错误响应。


# 全局多智能体系统实例
_multi_agent_planner = None


def get_trip_planner_agent() -> MultiAgentTripPlanner:
    """获取多智能体旅行规划系统实例(单例模式)"""
    global _multi_agent_planner

    if _multi_agent_planner is None:
        _multi_agent_planner = MultiAgentTripPlanner()

    return _multi_agent_planner

