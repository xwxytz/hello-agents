# 基于多智能体协作与工具增强的旅行规划系统

[![CI](https://github.com/xwxytz/hello-agents/actions/workflows/trip-planner-ci.yml/badge.svg)](https://github.com/xwxytz/hello-agents/actions/workflows/trip-planner-ci.yml)

> 用 MCP 协议接入高德地图真实 POI / 天气 / 路径数据，把大模型的开放生成约束为
> **可校验、可追溯的结构化行程**。

## 要解决的问题

通用大模型直接生成旅行计划时会出现三类失效：

| 失效类型 | 具体表现 |
|---|---|
| 虚构实体 | 编造不存在的景点、错误的地址 |
| 编造数值 | 票价、开放时间、游览时长凭空生成 |
| 不可执行 | 路线与景点地理位置不自洽，一天排不下 |

本项目不试图"让模型更聪明"，而是**改变信息的来源与约束方式**：

1. **真实数据 grounding** —— 通过 MCP（Model Context Protocol）接入 `amap-mcp-server`，
   让 Agent 真正调用高德的 POI 搜索 / 天气 / 路径规划接口，而不是让模型回忆。
2. **职责拆分** —— 用四个专职 Agent（景点、天气、酒店、行程编排）替代一个全能 Agent，
   每步的提示词只约束一件事，失败可定位到具体阶段。
3. **结构化契约** —— 最终行程必须通过 Pydantic 模型校验才返回，解析失败**明确报错**，
   不返回占位数据静默降级。

## 我的设计决策

技术上没有银弹，以下是本项目做出的取舍及其理由。

### 决策 1：拆四个专职 Agent，而不是一个全能 Agent

**放弃了**：单次调用完成全部规划（省 3 次 LLM 往返）。

**选择理由**：

- 全能 Agent 需要同时持有工具描述 + 天气 + 酒店 + 已选景点，上下文越长，
  工具选择与参数质量越差；
- 任一步失败会污染整份行程，且无法定位是哪一步错了；
- 拆分后每步的 prompt 可以**独立调优与替换**（例如把天气查询换成纯 API 调用，不影响其他三步）。

**代价**：3 次额外 LLM 往返；上游结果需显式拼进下游 prompt，存在上下文传递开销。

### 决策 2：工具调用使用文本协议 `[TOOL_CALL:...]`，而非 function calling

**原因**：所用框架版本（hello_agents 0.2.9）的 `SimpleAgent` 通过提示词中的文本协议解析工具调用。
该框架的 `stream_run()` 接口**不解析工具调用**，因此三个检索 Agent 不能改用流式接口
——否则 MCP 工具全部失效，模型会退回凭空编景点，正好摧毁本项目最核心的 grounding 能力。

**取舍**：换来的是工具可插拔（MCP 是标准协议，换数据源不需要改 Agent 编排）；
代价是依赖模型遵守输出格式，需要在 prompt 里做硬性约束（例如"温度字段必须是纯数字"）。

**迁移路径**：若框架升级到支持原生 function calling，只需替换工具描述的注入方式
与工具调用的解析入口，Agent 编排结构、prompt 内容、路由层代码均不需要改动。

### 决策 3：确定性查询不经过 Agent

POI / 天气 / 路线这类端点（`/api/map/*`）由服务层**直接调用 MCP 工具**，不让 LLM 转一圈。
参数已在 URL 中确定，再让模型决策只会：多烧 token、增加延迟、且可能擅自改写参数。

对应系统里两条不同的调用链：

```
不确定编排：  Agent → prompt 中的 [TOOL_CALL] → MCPTool → amap-mcp-server
确定性查询：  API   → AmapService            → MCPTool → amap-mcp-server
```

### 决策 4：流式执行用「工作线程 + asyncio.Queue」桥接

`SimpleAgent.run()` 是**同步阻塞**调用。若在 `async def` 路由里直接迭代，
阻塞的是整个事件循环——不是"这个请求慢"，而是 uvicorn 全进程不响应，连 `/health` 都不通。
因此在工作线程中驱动同步生成器，通过 `asyncio.Queue` 把事件回投到事件循环，再以 SSE 推送。

前端进度条由后端 `stage` 事件驱动，**不使用定时器模拟**——景点搜索阶段进度条会真实停止等待。

### 决策 5：全局唯一的 MCP 工具实例

系统最初存在两套独立的 MCP 客户端（Agent 侧与服务侧各一个），各自维护 `server_command`
与环境变量，一旦改一处忘一处，就会出现"行程生成正常、地图端点全挂"这类极难排查的问题。
现抽取统一工厂，配置单一来源，工具发现只在启动时执行一次。

### 决策 6：结构化输出靠「源头约束 + 严格校验 + 自纠重试」，并且**敢让失败可见**

**放弃了**：解析失败时返回一份占位行程。早期实现会拼出"北京景点1 / 北京景点2"、
"当地特色早餐"，坐标用 `116.4 + i*0.01` 硬编出来。

**为什么必须删掉它**：占位行程让系统永远"看起来能跑"，但那是最坏的失败模式——
返回 HTTP 200 + 成功，没有报错、没有日志，用户拿到的是一份看起来完整、
内容却全是编造的行程，还可能照着它出门。`_create_fallback_plan()` 已整个删除，
不留备用路径。

**现在四层防线**：

1. **源头约束** —— planner 调用带 `response_format={"type":"json_object"}`、
   `temperature=0`、`max_tokens=8192`。DeepSeek 官方对 JSON Output 要求三点配套
   （设参数、prompt 里给格式示例、给足长度防截断），后两点 `PLANNER_AGENT_PROMPT` 已满足。
2. **严格校验** —— 输出直接进 Pydantic，字段类型与必填项全查，不过就抛异常，不做"尽力解析"。
3. **自纠重试** —— 把失败原文 + Pydantic 的错误信息一起回灌给模型，让它对着自己的错误改，最多 3 次。
4. **明确失败** —— 仍不过则抛 `StructuredOutputError`，同步端点返回 **502 + `STRUCTURED_OUTPUT_FAILED`**。

**两个实现细节**：

- **重试不走 `SimpleAgent.run()`。** 它在每轮 `run()` 后把 user/assistant 消息追加进 `_history`，
  而 `_build_messages()` 每次都把历史拼进 prompt——直接重试等于把上一轮的坏 JSON 又喂一遍，
  既烧 token 又容易把模型往同一个错法上带。planner 不带工具，这里自己维护 messages 更可控。
- **JSON 模式只对 planner 开。** 三个检索 Agent 挂载了工具，走的是 Function Calling 分支；
  JSON 模式与工具调用能否共存，在 DeepSeek 官方文档里没查到明确结论——没核到就不赌。
  planner 不带工具，是安全的。

**边界（这条比上面四层更重要）**：JSON 模式只保证"输出是合法 JSON"，
Pydantic 只保证"字段符合 schema"，**两者都不校验内容是否真实**。
本次改的是**格式可靠性**，不是内容可靠性。见「已知限制」第 1 条。

### 决策 7：三路检索并发化，失败分级降级

**放弃了**：严格串行四步。景点、天气、酒店三者之间**没有数据依赖**，
串行等于把三路耗时相加；行程规划 Agent 才有数据依赖，必须排在最后。

**三个不能省的点**：

- **`asyncio.to_thread` 不能省** —— `SimpleAgent.run()` 是同步阻塞的
  （LLM 请求 + MCP 子进程调用），直接 `gather` 三个同步调用等于串行。
- **锁必须用 `asyncio.Lock`** —— async 路径里用 `threading.Lock` 会在 `await` 期间
  堵死整个事件循环，并发白做。锁只防**跨请求**的 Agent 历史污染；
  同一请求内三个 Agent 是不同实例、各有独立 history，可以在锁内安全并发。
- **流式的 `end` 事件按完成先后重新编号为 1/2/3** —— 前端按 `index/total` 算百分比，
  若按固定 index 发，酒店先完成会跳到 75%、景点后完成又退回 25%，进度条会倒退。
  改成按完成顺序编号后**前端零改动**。

**并发安全性**：查过 `MCPTool.run()` 源码——每次调用都新建独立的 MCP 客户端与事件循环，
无共享状态，所以三路并发是安全的；代价是同时拉起 3 个 `uvx` 子进程。

**降级是分级的**：天气、酒店失败只降级为空字符串、不阻断整体（行程照常生成，
只是缺这部分信息）；**景点失败必须整体失败**——检索不到景点还继续跑，
planner 就只能凭空编，那正是"火星行程"的形态，比报错更糟。

### 决策 8：结构化日志 + trace_id，让一次请求能在日志里被完整还原

**放弃了**：全项目 `print()`。它只有一条消息文本，没有级别、没有字段、没有请求维度，
并发时多条请求的输出会彻底混在一起——出问题只能靠猜。现在项目内 **print 数量为 0**。

**trace_id 用 contextvars 而不是全局变量**：全局锁/全局变量在「异步 + 多线程」混合场景下
必然串号；contextvars 由解释器保证同一逻辑调用链的隔离。loguru 的 `patcher` 在每条记录
生成时把当前 context 的 trace_id 注入 `extra`，业务代码不用手动 `bind`。

**这里踩到一个真实的坑，值得单独写**：`contextvars` 并不是「设了就到处都有」——

| 启动方式 | 是否继承 contextvars |
|---|---|
| `asyncio.to_thread` | ✅ 会（内部 `copy_context().run()`） |
| `threading.Thread` | ❌ 不会 |
| `ThreadPoolExecutor` | ❌ 不会 |

本项目三条路径全占：同步规划走 `asyncio.to_thread`、流式走 `threading.Thread`、
流式内部的三路并发走 `ThreadPoolExecutor`。**后两个必须显式
`contextvars.copy_context()` 传进去**，否则线程内日志的 trace_id 会退回默认值 `-`。
这个问题在实测中真的出现过一次（流式链路里三路检索的日志全部丢了 trace_id），
已修复。

**中间件不用 `BaseHTTPMiddleware`**：它会对响应体做缓冲处理，与本项目的 SSE 流式端点
配合时会出现响应不能及时下发的问题。改为直接写 ASGI 中间件，只透传 message，不碰 body。
同理，**中间件也不读请求体**（读 body 会消费 receive 通道，对流式请求是个隐患），
请求入参改由路由层记录——那里已经拿到校验后的模型对象，记出来的字段更干净。

**token 统计：拿不到就是拿不到，不填 0。**
已核实 HelloAgents 0.2.9 的 `HelloAgentsLLM.invoke()` 只读
`response.choices[0].message.content` 就返回，**把 `response.usage` 直接丢弃了**，
框架层面没有任何途径拿到 token。所以在更底层包了一层：包装 OpenAI 兼容客户端的
`chat.completions.create`，从响应对象上读 `usage`——这是**精确值**，不是估算。
三条边界写死在代码里：

- 拿到 `usage` → 记为精确值，`usage_source="api_usage"`
- `usage` 为 None（流式响应或端点未返回）→ 记 `None`，并累计 `unknown_usage_calls`，**绝不当 0**
- 探针安装失败（依赖的私有属性 `_client` 在框架升级后可能消失）→ 快照返回 `available=false`，不提供任何数字

### 决策 9：缓存装在哪一层，是这次最关键的判断

**要点 1 的描述与实际情况有出入**：系统原本**不是**"无任何缓存"，而是有一个
`dict + 统一 300 秒 TTL` 的简陋版本。它有三个问题：所有工具共用一个 TTL；
**没有条目上限**（内存只增不减）；没有命中统计。所以这次不是"加缓存"，是重做。

**TTL 按数据新鲜度分档**——统一 TTL 只能二选一，要么天气失真，要么 POI 白打上游：

| 数据类型 | TTL | 理由 |
|---|---|---|
| 天气 | 3 小时 | 一天内多次更新 |
| POI / 地理编码 / POI 详情 | 24 小时 | 几乎不变 |
| 路线规划 | 1 小时 | 受实时路况影响，但仍以静态路径为主 |
| 其他 | 5 分钟 | 保守默认 |

**用 `TTLCache` 而不是普通 dict**：普通 dict 没有 maxsize，任意关键词组合都会永久占位。
每组上限 512 条。

**缓存装在 `MCPTool.run` 上，而不是 `AmapService` 里——这是本次最重要的一个决定。**
系统里有两条调用 MCP 的路径：

```
确定性查询层：  /api/map/*  → AmapService._call_mcp → MCPTool.run
Agent 编排层：  SimpleAgent → MCPTool.run            （绕过 AmapService）
```

我最初把缓存写在了 `AmapService` 里，跑完一次完整规划后去看计数——**miss 纹丝不动**。
也就是说真正的调用大头（一次规划 7 次 LLM 调用、多次 MCP 调用）完全没享受到缓存。
实测数据也印证了这一点：把缓存下沉到 `MCPTool.run` 之后，同参数规划才从 12.3s 降到 8.6s。

能安全包一层的前提：`MCPTool.run(parameters: dict) -> str` 是纯粹的"参数进、字符串出"接口，
且每次调用内部都新建独立的 MCPClient 与事件循环，没有跨调用状态，重复返回同一份结果不会破坏任何东西。

**不缓存错误响应**（这条是实测补上的）。原本只判断"响应里有没有成功标记"，结果发现
查询不存在的城市时，MCP 依然返回一个"执行成功"的结果串，载荷是 `{"error": "No forecast data available"}`。
按原判据它会被当成成功结果写进缓存——**POI 的 TTL 是 24 小时，等于把一次故障放大一整天**。
现在会解析载荷，命中 `error` / `status="0"` / `errmsg` 等失败标记就不写缓存。

**要点 2 关于并发安全性的结论**：不存在"有状态长连接被并发破坏"的问题——因为
`MCPTool.run()` 每次调用都会新建独立的客户端与事件循环，压根没有长连接。
真正的并发风险是**缓存字典本身**（`TTLCache` 内部依赖 `OrderedDict` 顺序操作，非线程安全），
所以这里用锁只包住字典读写，**不把 MCP 调用放进锁**——那会把并发请求串成一路，
比偶尔的缓存穿透糟糕得多。

**要点 4 关于"无限重试"的事实修正**：查了框架源码，`SimpleAgent.run()` **自带
`max_tool_iterations=3` 上限**，planner 的自纠重试也已经限死 3 次。所以严格意义上的
"无限重试烧钱"并不存在。真正的风险在别处，**缺的是全局预算**：

| 防线 | 现状 | 本次补充 |
|---|---|---|
| 工具调用循环 | 框架已限 3 次 | — |
| planner 自纠重试 | 已限 3 次 | 每次重试前查预算，超预算就不再重试 |
| 单次规划总耗时 | 无 | 上限 180s → 超时返回 **504** |
| 单次规划 token | 无 | 上限 200,000 → 超出返回 **429** |
| 输入长度 | 无限制 | 城市/交通/住宿 ≤50 字（超限 422）；自由文本截断到 2000 字 |

**必须说清的边界**：这个"预算"是**协作式**的。Python 没有办法安全地强杀一个正阻塞在
IO 里的线程，所以只能在阶段之间检查是否已经超预算，超了就停止往下走，
而做不到"精确地在第 N 秒掐断"。它能挡住的是真正烧钱的场景——重试循环、
超长 prompt 导致的一轮轮大模型调用。

### 决策 10：鉴权用 API Key，限流自己写中间件（不上 JWT、不上 slowapi）

问题是真实的：接口完全裸奔，一次规划要打 7 次 LLM + 多次 MCP，谁拿到地址谁就能刷。

**为什么不用 JWT**：本项目没有用户系统。JWT 签给谁、给谁刷新、吊销谁的都是问题，
在没有用户的前提下它和 API Key 是等价的——只是看起来更"正规"。
等真的有账号体系了再换 JWT 不迟，那时候换的成本也就是一个中间件。

**为什么不用 slowapi**：它基于 `BaseHTTPMiddleware`，会对响应体做缓冲，
与本项目的 SSE 流式端点冲突（这也是决策 8 里 trace_id 中间件坚持写纯 ASGI 的原因）。
而且限流要按端点分档、要把命中情况记进结构化日志，这层逻辑本身只有一个数据结构加一个判断，
引入一个依赖不值得。现在是 `app/api/guard.py` 里的滑动窗口计数器。

三个刻意的设计：

| 选择 | 理由 |
|---|---|
| 鉴权与限流放在**同一个**中间件里 | 顺序是这件事的全部：必须先定身份再按身份限流。拆成两个中间件，顺序就依赖调用方记得怎么 add |
| 规划接口与普通查询**分档** | 一次规划的代价是普通查询的几十倍，用同一个额度要么把查询卡死，要么根本挡不住刷规划 |
| 未配置任何 Key 时**关闭鉴权但明确告警** | 本地开发不该被拦；但"悄悄放行"会让人误以为接口是受保护的，所以启动日志直接警告 |

`TRUST_PROXY` 默认关闭：只有在反向代理后面才该信 `X-Forwarded-For`，
直接暴露公网时伪造这个头就能一键绕过限流。

### 决策 11：行程持久化只做"分享链接"，不做用户系统

行程原本只存在前端 `sessionStorage` 里：刷新即丢、换标签页就没、更没法发给别人——
一份花了十几秒和两万 token 生成的结果，关掉页面就消失了。

做了什么：`SQLite`（SQLAlchemy ORM，换 PostgreSQL 只改 `TRIP_DB_URL` 一行）
存行程 + 分享令牌 + 管理令牌，`/api/trips` 三个端点。

**为什么砍掉用户系统**：注册/登录/密码哈希/会话管理是一整套独立工程，
对"多智能体规划"这个卖点零加成，而它带来的复杂度（会话、找回密码、权限模型）
会让主线失焦。分享令牌 + 管理令牌已经覆盖真实需求：发给别人看、自己能删。

三条硬规则：

1. **持久化失败绝不让规划失败。** 行程已经在内存里生成好了，库写不进去顶多是
   "这次不能分享"，返回 `share: null` 并告警，绝不因为存不进去让一次成功的规划报错。
2. **令牌不可猜测。** 用 `secrets.token_urlsafe` 而不是自增 ID——自增 ID 会让
   分享链接变成"一路 +1 翻出所有人行程"的公开遍历接口。
3. **分享令牌不能删行程。** 删除要单独的管理令牌，且它只在创建响应里出现一次，
   不会出现在任何读取结果里。

### 决策 12：评测集宁可少，也不能有假数字

Agent 类项目最怕"我也不知道效果好不好"。所以建了 `backend/eval/`：
20 城 × 3 类偏好 = 60 条用例，四个指标（结构化成功率 / 景点真实存在率 / 坐标准确率 / 行程合理性）。

方法论上守住三条，这三条比多跑几十条用例重要得多：

1. **核验失败 ≠ 不存在。** 上游报错、超时的条目记为 `exists=None`，
   单独统计，不进存在率的分母。把它算成"不存在"会同时污染两个指标。
2. **没样本 ≠ 0%。** 所有比率在分母为 0 时返回 `None`，报告里显示 `—`。
3. **参考坐标本身有误差。** 高德地理编码对"景区大门 vs 景区中心"可能差几公里，
   所以同时给 1km（严格）和 5km（宽松）两档，并把原始距离写进 JSON 便于换阈值重算。

**行程合理性这一项必须由人打分**，模板在 `eval/results/manual_scoring_*.csv`。
自动化指标只能证明"格式对、景点存在、坐标不离谱"，证明不了"这个安排好不好"——
自己给自己打合理性的分，是自说自话。

## 系统架构与数据流

一次行程生成的完整链路：

1. **前端** `POST /api/trip/plan/stream`，以 SSE 接收事件
2. **编排层** `MultiAgentTripPlanner.plan_trip_stream()` 驱动两个阶段：
   - 阶段 1（并发）：景点搜索 ∥ 天气查询 ∥ 酒店推荐 —— 三者无数据依赖
   - 阶段 2：行程编排 —— 有数据依赖，等三路结果齐了才跑
3. **工具层** 三个检索 Agent 通过 MCP 调用高德真实接口；
   三路结果拼进编排 Agent 的 prompt
4. **校验层** 输出经 Pydantic 模型校验后作为 `result` 事件返回；
   校验失败先自纠重试（最多 3 次），仍失败则发送 `error` 事件，不返回占位行程

同步端点 `/api/trip/plan` 走同一套编排逻辑（`asyncio.gather` 版本），
区别只在返回方式：成功返回完整 JSON，失败返回 502。

SSE 事件协议（三路检索的 `end` 事件按实际完成先后推送，`index` 递增）：

| 事件 | 载荷 | 用途 |
|---|---|---|
| `stage` | `stage`, `status`(start/end), `label`, `index`, `total` | 真实进度与阶段文案 |
| `result` | 完整 TripPlan | 渲染结果页 |
| `error` | `code`, `message`, `stage` | 明确失败，不静默降级 |

## 实测数据

> 以下全部是**本机单环境**下的测量结果，每组样本仅 3 次，且高度依赖网络与 LLM 响应速度。
> **不要把它当作性能承诺**——记录它们的目的只是让优化有对照，而不是证明有多快。

### 端到端延迟（并发改造前后）

同一输入（成都 · 2 天 · 偏好"美食"），用 `curl -w %{time_total}` 测端到端，各连跑 3 次：

| | 第 1 次 | 第 2 次 | 第 3 次 | 平均 |
|---|---|---|---|---|
| 改造前（串行四步） | 21.32s | 17.49s | 16.85s | **18.55s** |
| 改造后（三路并发） | 11.13s | 10.70s | 10.71s | **10.85s** |

下降约 **41%**。

并发版各阶段自身耗时均值：`attraction 3.09s / weather 4.52s / hotel 5.50s`。
三路合计约 13.1s——**串行版的墙钟时间大致就是这个量级**（18.55s 中还有行程规划与 HTTP 开销）。
并发后检索阶段收敛到最慢那一路（约 5.5s），余下约 5.3s 由行程规划占用
（该值为 `10.85 − 5.5` 的推算，**未单独埋点**）。
也就是说延迟确实由"三者之和"变成了"最慢那一路"，理论预期对上了。

这些耗时是进程内累计值，可通过 `GET /api/trip/health` 的
`structured_output.stage_latency_avg_s` 实时读取。

### 结构化输出

- DeepSeek `deepseek-chat` **确实支持** `response_format={"type":"json_object"}`：
  实测从未触发降级分支，首次输出即通过 Pydantic 校验，**自纠重试触发 0 次**。
- `/api/trip/health` 的 `structured_output` 会返回 `success_rate` 与 `retry_per_plan`。
  **样本数为 0 时返回 `null` 而不是 0**——"没跑过"和"成功率 0%"是两回事，
  把它们混在一起等于凭空造出一个数字。

### 评测集结果（60 条用例，真实跑出来的）

`backend/eval/`：20 城 × 3 类偏好（历史文化 / 美食探店 / 自然风光）= 60 条，
3 天、日期与交通住宿全部固定，逐条真实调用后核验。报告在
`eval/results/REPORT.md`，逐条原始结果在 `eval/results/eval_full-60-v2.json`。

| 指标 | 结果 | 样本 | 口径 |
|---|---|---|---|
| 结构化成功率 | **100%** | 60/60 | 60 次规划全部产出通过 schema 校验的行程；60 次里只触发 1 次自纠重试 |
| 景点真实存在率 | **98.1%** | 462/471 | 高德 POI 回查命中率（1 条回查失败，不计入分母） |
| 坐标准确率 ≤1 km | 59.7% | 457 | 与高德地理编码参考值的球面距离 |
| 坐标准确率 ≤5 km | **82.9%** | 457 | 同上 |
| 坐标偏差中位数 / p90 / 最大 | 0.72 / 10.6 / 150.0 km | 457 | 同上 |
| 行程合理性（人工） | **未打分** | 60 | 模板已生成，待人工填写 |

结构完整性（60 份行程）：天数对齐、日期连续、每天三餐、每天推荐酒店、给出预算
**全部 100%**；「每天 2–3 个景点」**91.7%**（有 5 份行程某天只排了 1 个或排了 4 个）。
跨天重复景点比例 0.4%。同日景点最大跨度中位数 8.0 km、p90 33.4 km、最大 99.1 km
（跨度越大越不可执行——这是客观量，不是评分）。

开销：端到端 p50 11.23s / p95 14.68s / 最大 17.71s；
token 合计 **4,380,641**（平均每条 73,011，来自 API 响应的 `usage`，是精确值）。
**也就是说全量跑一次大约半小时和四百多万 token**——所以评测集只作为周期性验证，
不做成 CI 里的常规环节。

**这些数字要连着下面三条一起读，否则会被误用：**

1. **98.1% 是下限，不是"2% 的景点是编的"。** 那 9 条判为"不存在"的里，
   多数是别称与泛称问题——「八市（第八市场）」vs 高德的「第八市场(八市)」、
   「镇海楼（广州博物馆）」vs「广州博物馆(镇海楼展区)」，名字都对得上，
   只是字符相似度没过 0.6 的阈值。它们说明匹配器还能更聪明，不能说明模型在编。
2. **坐标准确率的分母里已经剔除了 5 条不可信的参考坐标。** 首轮跑完发现
   「莫高窟 @ 敦煌」被地理编码到了距市中心 2548 km 的地方——**错的是参考值，不是行程**。
   没有这道闸门，核验器的错会被记成模型的错。现在以城市中心为锚，
   参考坐标离市中心超过 200 km 就判不可信、单独计数、不进统计。
   这是本次评测里最有价值的一个发现：**评测器本身也会被评测发现是错的。**
3. **"行程好不好"这一栏是空的，我不会替它填。** 自动化指标只能证明
   "格式对、景点存在、坐标不离谱"，证明不了"这个安排合不合理"。
   自己给自己打合理性的分，是自说自话。

### 一次请求的日志链路（真实抓取）

苏州 · 2 天 · 流式请求，`grep "$TRACE_ID"` 筛出来的完整链路（已裁剪宽度）：

```
16:36:09.478 | INFO  | 1aeb999f00f7 | middleware             | ➡️ POST /api/trip/plan/stream
16:36:09.482 | INFO  | 1aeb999f00f7 | routes.trip            | 📥 流式旅行规划请求: 苏州 2天
16:36:09.483 | INFO  | 1aeb999f00f7 | trip_planner_agent     | 🚀 开始流式规划: 苏州 2天
16:36:09.484 | INFO  | 1aeb999f00f7 | trip_planner_agent     | ▶️ attraction 检索开始
16:36:09.489 | INFO  | 1aeb999f00f7 | trip_planner_agent     | ▶️ weather 检索开始
16:36:09.490 | INFO  | 1aeb999f00f7 | trip_planner_agent     | ▶️ hotel 检索开始
16:36:14.474 | INFO  | 1aeb999f00f7 | trip_planner_agent     | ✅ weather 检索完成 4.98s
16:36:15.419 | INFO  | 1aeb999f00f7 | trip_planner_agent     | ✅ attraction 检索完成 5.94s
16:36:15.511 | INFO  | 1aeb999f00f7 | trip_planner_agent     | ✅ hotel 检索完成 6.02s
16:36:15.512 | INFO  | 1aeb999f00f7 | trip_planner_agent     | 📋 生成行程计划
16:36:21.230 | INFO  | 1aeb999f00f7 | trip_planner_agent     | ✅ 行程 JSON 校验通过（第 1 次尝试）
16:36:21.231 | INFO  | 1aeb999f00f7 | trip_planner_agent     | ✅ 流式旅行计划生成完成，端到端 11.75s
16:36:21.232 | INFO  | 1aeb999f00f7 | middleware             | ⬅️ POST /api/trip/plan/stream 200 11753ms tokens=19396 calls=7
```

两处信息是 `print` 时代拿不到的：

1. **并发是真的**——三路检索在 09.484 ~ 09.490 之间几乎同时启动，最慢一路 6.02s 结束，
   而不是三者的耗时相加。
2. **token 有了精确数字**——本次 7 次 LLM 调用共 19,396 tokens。
   同步端点同输入的另一次实测为 7 次调用 / 19,706 tokens。

`trace_id` 同时会写进响应头 `X-Trace-Id`，前端或上游可以直接拿它来对齐。
若上游请求带了 `X-Trace-Id` 请求头，系统会沿用而不是重新生成，便于跨服务串联。

### 缓存效果

> 同样是**本机单进程**、样本极小的测量。绝对值受网络与 LLM 响应影响，
> 这里记的是量级与对照关系，不是性能承诺。

**确定性查询层**（`GET /api/map/weather?city=北京`，同一 key 连续两次）：

| | 第 1 次 | 第 2 次 |
|---|---|---|
| 耗时 | 2092ms | **3.4ms** |

命中的时候省掉的是一次 `uvx` 子进程启动 + 一次外部 API 请求。

**规划主链路**（苏州 · 2 天，完全相同的入参连跑 3 次）：

| | 第 1 次 | 第 2 次 | 第 3 次 |
|---|---|---|---|
| 端到端耗时 | 12.29s | 8.62s | 9.53s |

三次下来累计 **4 次命中 / 3 次未命中，命中率 57%**。

为什么没有像确定性查询那样降到毫秒：**LLM 调用不受缓存影响**（一次规划 7 次 LLM 调用
这部分开销原封不动），而且三个检索 Agent 的工具参数是由模型生成的，措辞的细微差异
会导致缓存键不同——所以永远不会是 100% 命中。这是这套架构的固有特性，不是缺陷。

这些数据都可以在运行时读到：`GET /api/trip/health` 的 `structured_output.cache`
返回 `hits / misses / saved_calls / hit_rate / per_tool`，`cache_groups` 返回各分组的
条目数、上限与 TTL。

## 技术栈

### 后端

- **Agent 框架**：HelloAgents（SimpleAgent + MCPTool）
- **API**：FastAPI（含 SSE 流式端点）
- **工具协议**：MCP，服务端 `amap-mcp-server`（高德地图，展开为 16 个子工具）
- **数据契约**：Pydantic v2
- **LLM**：DeepSeek / OpenAI 兼容接口

### 前端

- **框架**：Vue 3 + TypeScript
- **构建工具**：Vite
- **UI 组件库**：Ant Design Vue
- **地图**：高德地图 JavaScript API（`amap-jsapi-loader`）
- **导出**：html2canvas + jsPDF

## 项目结构

```
helloagents-trip-planner/
├── backend/
│   ├── run.py                 启动脚本
│   ├── requirements.txt
│   ├── .env.example           配置模板（真实密钥填在 .env，不入库）
│   └── app/
│       ├── config.py          配置加载与启动校验
│       ├── Prompt/prompt.py   四个 Agent 的提示词
│       ├── agents/
│       │   └── trip_planner_agent.py   多智能体编排（含流式生成器）
│       ├── services/
│       │   ├── mcp_factory.py     MCP 工具统一工厂（全局单例）
│       │   ├── amap_service.py    确定性地图查询（POI/天气/路线/地理编码）
│       │   ├── api_cache.py      外部API缓存：TTL 分档 + 条目上限（挂在 MCPTool.run 上）
│       │   ├── logging_setup.py   loguru 配置 + trace_id contextvar
│       │   ├── usage_probe.py     LLM token 探针（从底层客户端读 usage）
│       │   ├── llm_service.py
│       │   ├── struct_metrics.py  结构化输出成功率 / 分阶段耗时统计
│       │   ├── rate_limit.py      滑动窗口限流（按身份分桶、按端点分档）
│       │   ├── trip_store.py      行程持久化：保存 / 读取 / 删除（SQLAlchemy）
│       │   └── unsplash_service.py
│       ├── api/
│       │   ├── main.py        FastAPI 实例、CORS、启动预热
│       │   ├── middleware.py  trace_id 注入 + 请求摘要（纯 ASGI 中间件）
│       │   ├── guard.py       API Key 鉴权 + 限流（纯 ASGI，与 SSE 兼容）
│       │   └── routes/  trip.py  poi.py  map.py  trips.py
│       ├── models/
│       │   ├── schemas.py     全部数据契约
│       │   └── db.py          行程表 ORM 模型
│   ├── eval/                  评测集（20 城 × 3 偏好 = 60 条）
│   │   ├── cases.py           用例集定义
│   │   ├── metrics.py         结构完整性与地理指标（纯计算，可单测）
│   │   ├── verify.py          景点真实性与坐标核验（三态：存在/不存在/核验失败）
│   │   ├── run_eval.py        运行器，产出 JSON + 报告 + 人工打分表
│   │   └── results/           评测产物（报告、逐条原始结果、打分模板）
└── frontend/
    └── src/
        ├── main.ts  App.vue
        ├── types/index.ts      与后端 schema 对应的 TS 类型
        ├── services/api.ts     axios + SSE 流式读取 + 分享链接读取
        ├── utils/planEdit.ts   行程编辑规则（从 Result.vue 抽出以便单测）
        └── views/  Home.vue（表单）  Result.vue（行程/地图/编辑/导出/分享）
```

## 快速开始

### 方式一：Docker 一键启动（推荐给第一次接触本项目的人）

```bash
cp .env.example .env      # 填入自己的密钥（必填 LLM_API_KEY 与 AMAP_API_KEY）
./scripts/start.sh        # Windows: powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

启动脚本会先检查 docker 是否可用、`.env` 是否存在、密钥是否还是占位符，然后执行
`docker compose up --build`。起来后：

| 地址 | 说明 |
|---|---|
| http://localhost:8080 | 前端（nginx 托管，同时反代 `/api` 到后端） |
| http://localhost:8000/docs | 后端 API 文档（也直接暴露，方便单独调试） |

编排了三个服务：`backend` / `frontend` / `redis`。

> **关于 Redis**：它目前**没有被任何代码使用**。P1-9 落地的是进程内 `TTLCache`，
> 单副本够用；把 Redis 编排进来是为下一步把缓存换成分布式做准备——多副本部署时
> 进程内缓存每个副本各存一份，命中率会随副本数下降。在真正接入之前删掉它不影响任何功能。

两个 Dockerfile 的关键取舍：

- **后端多阶段 + 非 root**：依赖装进 `/opt/venv` 后整体拷进运行镜像，不带 pip 缓存。
  `uvx` 必须留在运行镜像里——MCP 工具是**运行时**用 `uvx amap-mcp-server` 拉起子进程的，
  只在构建期装 uv 的话服务能启动、健康检查也通过，但一调用地图工具就失败。
- **前端构建后由 nginx 托管**：`VITE_API_BASE_URL` 传空串，前端用相对路径 `/api` 发请求，
  由 nginx 反代到 backend 容器。这样浏览器只跟一个端口通信，彻底绕开 CORS；
  写死 `http://localhost:8000` 的话，访问容器化前端的浏览器会去连**自己机器**的 8000 端口，必然失败。
  nginx 对 `/api/` 关掉了 `proxy_buffering`，否则 SSE 事件会被攒着一起发，前端进度条不动。

### 方式二：本地启动

### 前提条件

- Python 3.10+
- Node.js 16+
- 高德地图 API 密钥 **两个**：
  - **「Web服务」类型** —— 后端 MCP 调用使用（REST 接口）
  - **「Web端(JS API)」类型** —— 前端地图渲染使用
  - ⚠️ 两者**不能混用**。把 JS 类型的 Key 填进后端，所有地图调用会返回 `USERKEY_PLAT_NOMATCH`
- LLM API 密钥（DeepSeek / OpenAI 等）

### 后端

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # 编辑 .env，填入你自己的密钥
python -u run.py
```

> 两条容易踩的坑：
>
> 1. **必须在 `backend/` 目录下启动。**&#8203; 配置加载按当前工作目录查找 `.env`，
>    在仓库根目录启动会导致所有配置项为空并拒绝启动。
> 2. **首次启动有约 40 秒的 MCP 预热**（需要拉起 `uvx` 子进程完成工具发现），
>    期间端口不监听。用 `python -u run.py` 关闭输出缓冲，
>    看到 `✅ 多智能体系统预热完成` 后再发起请求。

### 日志配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | 日志级别。想看每次 MCP 调用与 LLM 调用耗时设为 `DEBUG` |
| `LOG_JSON` | 关闭 | 设为 `1/true` 时每行输出一个 JSON 对象，便于日志采集端解析 |

默认输出人类可读格式：`时间 | 级别 | trace_id | logger | 消息`。
排查时按 `trace_id` 过滤即可还原整条链路：

```bash
# 从响应头拿到 X-Trace-Id 后
grep "1aeb999f00f7" backend.log
```

标准库 logging（uvicorn 等）已被接管，access log 同样带 trace_id。

> ⚠️ **注意：`.env` 会覆盖系统环境变量。**&#8203;
> 本项目在 `config.py` 里用的是 `load_dotenv(override=True)`，所以
> `export LOG_LEVEL=DEBUG` 不会生效——`.env` 里的 `LOG_LEVEL=INFO` 优先级更高。
> 要改配置请直接编辑 `.env`。

### 缓存与预算配置

全部在 `.env` 中设置（`app_version` 之外的值均可通过环境变量注入，但见上面关于
`.env` 覆盖的提醒）：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `CACHE_WEATHER_TTL` | `10800` | 天气缓存秒数（3 小时） |
| `CACHE_POI_TTL` | `86400` | POI / 地理编码 / POI 详情缓存秒数（24 小时） |
| `CACHE_ROUTE_TTL` | `3600` | 路线缓存秒数（1 小时） |
| `CACHE_DEFAULT_TTL` | `300` | 未列出的工具走这个默认值 |
| `CACHE_MAX_ENTRIES` | `512` | 单组缓存条目上限 |
| `PLAN_TIMEOUT_S` | `180` | 单次规划总耗时上限，超出返回 504 |
| `PLAN_TOKEN_BUDGET` | `200000` | 单次规划 token 上限，超出返回 429 |
| `MAX_TEXT_INPUT_LEN` | `2000` | 自由文本输入截断阈值 |

### 前端

```bash
cd frontend
npm install
cp .env.example .env            # 填入高德 Web端(JS API) Key
npm run dev
```

打开 `http://localhost:5173`。

### 验证安装

```bash
curl http://localhost:8000/api/trip/health
```

正常时会返回四个子 Agent 的名称与工具数、MCP 工具数量、LLM 的 provider/model，
以及 `structured_output`（结构化输出成功率、平均重试次数、分阶段平均耗时）。

若 `mcp.connected` 为 `false`，说明 MCP 工具未展开，此时行程中的景点将是模型幻觉。
`structured_output.success_rate` 在尚未处理任何请求时为 `null`（不是 0）。

## API 端点

启动后访问 `http://localhost:8000/docs` 查看完整交互式文档。

| 方法 | 端点 | 说明 |
|---|---|---|
| POST | `/api/trip/plan` | 生成旅行计划（同步）。结构化输出失败 **502**；超预算：超时 **504** / token 超限 **429** |
| POST | `/api/trip/plan/stream` | 生成旅行计划（SSE 流式）。失败推送 `error` 事件 |
| GET | `/api/trip/health` | 多智能体系统状态（各子 Agent、MCP、LLM、结构化输出统计） |
| GET | `/api/map/poi` | POI 搜索 |
| GET | `/api/map/weather` | 城市天气预报 |
| POST | `/api/map/route` | 路线规划（步行 / 驾车 / 公交） |
| GET | `/api/map/health` | 地图服务状态 |
| GET | `/api/poi/search` | POI 搜索（简化版） |
| GET | `/api/poi/detail/{id}` | POI 详情 |
| GET | `/api/poi/photo` | 景点配图（Unsplash） |
| POST | `/api/trips` | 保存行程并生成分享链接（返回 `share_token` 与只出现一次的 `manage_token`） |
| GET | `/api/trips/{share_token}` | 按分享令牌读取行程（公开只读，浏览量 +1） |
| DELETE | `/api/trips/{share_token}` | 删除行程，需请求头 `X-Manage-Token` |
| GET | `/health` | 应用健康检查（含 LLM / MCP 探测、鉴权限流状态、行程库状态） |

鉴权开启后，除 `/health`、`/api/trip/health` 与分享读取（公开只读）外，
`/api/*` 都要带 `X-API-Key`（或 `Authorization: Bearer <key>`）。
被限流时返回 **429** 并带 `Retry-After` 与 `X-RateLimit-*` 响应头；
放行时也会回 `X-RateLimit-Remaining`，调用方不用猜自己还剩多少额度。

流式端点示例：

```bash
curl -N -X POST http://localhost:8000/api/trip/plan/stream \
  -H "Content-Type: application/json" \
  -d '{"city":"北京","start_date":"2026-10-15","end_date":"2026-10-17","travel_days":3,"transportation":"地铁","accommodation":"经济型","preferences":["历史文化"],"free_text_input":""}'
```

## 测试

覆盖率不高，但每一条都冲着**具体会出问题的地方**去，不是凑数。

### 后端（pytest，80 个用例）

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

| 文件 | 覆盖什么 |
|---|---|
| `tests/test_parse_strict.py` | 行程 JSON 解析：裸 JSON / 带 ```json 围栏 / 前后有废话 / 完全不是 JSON / 字段类型错误 / 缺必填字段 |
| `tests/test_schemas.py` | `WeatherInfo.parse_temperature`（`"28°C"` → `28`）、`travel_days` 的 1–30 边界、P1-9 加的输入长度约束 |
| `tests/test_api_smoke.py` | `/health`、`/api/trip/health`、`X-Trace-Id` 响应头，以及用假 Agent 打 `/api/trip/plan` 验证错误码映射（502 / 504 / 429 / 422） |
| `tests/test_guard.py` | 鉴权与限流：缺 Key / 错 Key 返回 401、额度按身份隔离、规划与查询分档、未配 Key 时放行 |
| `tests/test_trip_store.py` | 行程存取闭环、分享令牌不可猜测、分享令牌不能删行程、库不可用时返回 `None` 而不是抛异常 |
| `tests/test_trips_api.py` | `/api/trips` 三个端点的接线：保存→读回、未知令牌 404、无管理令牌 401、库不可用 503 |
| `tests/test_eval_metrics.py` | **评测器本身的算术**：距离量级、结构检查各项判定、核验汇总里"失败不进分母""无样本不填 0" |

三条原则：

1. **不连任何外部服务、不需要真实密钥。** 所有会走到 LLM / 高德的地方用假 Agent 顶替。
   冒烟测试验证的是路由、校验、错误映射这一层，不是模型能不能生成行程。
2. **用 `TestClient(app)` 而不是 `with TestClient(app)`。** 后者会触发 startup 事件，
   里面有 MCP 预热（约 40 秒）和 `validate_config`（缺密钥直接抛错），冒烟测试不该依赖这些。
3. **失败路径比成功路径更重要。** `test_plan_结构化输出失败返回502` 和
   `test_不返回占位行程` 是回归测试，守住 P1-6 的结论：解析失败必须是明确错误，不能退回占位行程。

### 前端（vitest，17 个用例）

```bash
cd frontend
npm ci
npm run test
```

| 文件 | 覆盖什么 |
|---|---|
| `tests/planEdit.spec.ts` | 行程编辑规则：每天至少保留一个景点、上移/下移的边界、餐饮类型转中文标签 |
| `tests/api.spec.ts` | SSE 流解析：事件顺序、**事件被切在两个 chunk 中间能否拼回来**、心跳注释行不产生事件、坏 JSON 不中断后续事件；以及分享链接读取的成功/404/空内容三条路径 |

为了能测「行程编辑逻辑」，把 `canDeleteAttraction` / `moveAttractionInPlace` / `getMealLabel`
从 `Result.vue` 抽到了 `src/utils/planEdit.ts`。原因很实际：挂载这个组件要准备
ant-design-vue、vue-router、高德地图 loader 和 sessionStorage，成本远高于被测逻辑本身。

### CI

`.github/workflows/ci.yml` 跑四条：

| 环节 | 命令 |
|---|---|
| 后端静态检查 | `ruff check .` |
| 后端测试 | `pytest -q` |
| 前端类型检查 | `npx vue-tsc --noEmit` |
| 前端测试 + 构建 | `npm run test`、`npm run build` |
| 镜像可构建 | `docker compose build` |

> ⚠️ 一个必须说明的问题：**GitHub Actions 只扫描仓库根目录下的 `.github/workflows/`**。
> 本项目目前是 `hello-agents` 仓库的一个子目录，所以放在项目内的这份 yml **不会被触发**。
> 两种让它生效的方式：(1) 把本项目拆成独立仓库，文件位置不用动；
> (2) 复制到仓库根的 `.github/workflows/`（建议改名 `trip-planner-ci.yml`），
> 同时把 yml 里的 `PROJECT_PATH` 与 `paths` 改成相对仓库根的路径。

顺带一提：`vue-tsc` 在接入 CI 时是**红的**（10 个既有错误）。修的过程里发现三件真问题：
缺少 Vite 的 `env.d.ts` 声明（导致 `import.meta.env` 与 CSS import 报错）、
`Home.vue` 里 `TripFormData & { start_date: Dayjs | null }` 的交叉类型让日期字段变成了
`string & Dayjs`（既不能赋 null 也不能赋 Dayjs）、以及 `Result.vue` 里两个从未被引用的
函数 `captureMapImage` / `restoreMap`（已删除，git 历史里还在）。
这说明一件事：**CI 的价值不在于"加了 CI"，而在于它真的跑起来之后暴露了什么。**

## 已知限制与后续计划

主动列出当前实现中尚未解决的问题：

1. **行程内容未做真实性校验 —— 这是当前最大的缺口。**
   实测 `city="火星"` 仍然返回 HTTP 200 与一份 schema 完全合法的"火星行程"
   （火星先驱者酒店、水手号峡谷、交通方式"火星漫游车"、坐标 `{0.0, 0.0}`）。
   原因很直接：**JSON 模式只保证"输出是合法 JSON"，Pydantic 只保证"字段符合 schema"，
   两者都不校验内容是否真实**。上游查不到数据时，planner 照样能编出一份完全合规的行程，
   于是畅通无阻。手工拼装的占位行程（"北京景点1"）已经彻底堵死，
   但**模型自己编的行程目前拦不住**。
   *候选加固（成本从低到高）：拒绝 `location == (0,0)`；校验 `len(days) == travel_days`；
   要求景点名出现在检索 Agent 的返回中（需改 prompt 让检索 Agent 输出结构化列表，工作量最大）。*

   **补充（P2-1 之后）**：这个问题现在有了量化的边界——60 条正常用例下
   景点真实存在率 98.1%（462/471），说明**常规城市 + 常规偏好下模型基本不会编景点**；
   但"火星"这类上游完全查不到数据的输入仍然畅通无阻，因为检测器依赖的是
   "高德查得到"，而查不到时模型照编。评测集量化的是常态，堵不住这个极端。

2. **景点经纬度由模型生成，不是高德返回值。**&#8203;
   `maps_text_search` 经当前 MCP server 仅返回 `id / name / address / typecode`，
   不含经纬度。行程中的坐标来自模型输出，缺少坐标的景点不会在地图上打点（前端已做跳过）。
   *计划：批量调用 `maps_geo` 为景点补齐真实坐标。*

3. **门票价格、开放时间为模型估算值，未接入实时票务接口。**&#8203;

4. **每次 MCP 缓存未命中的调用都会新建一个客户端并重启 `uvx` 子进程（约 2–4 秒），
   三路并发时会同时拉起 3 个。**&#8203;
   已用 TTL 缓存大幅降低命中场景的开销（见「实测数据」），但未命中的那次仍然要付这份代价。
   彻底解决需要维护持久化 MCP 会话，当前框架的 `MCPTool.run()` 每次都新建客户端，不支持。

5. **同一时刻只允许一个规划流程（串行锁）。**&#8203;
   锁的目的是避免多请求并发时 Agent 对话历史互相污染。三路检索已经在**单个请求内**并发，
   但**跨请求仍然排队**。真正的跨请求并发需要每次请求新建 Agent 实例并共享 MCP 工具。

6. **行程持久化是单机 SQLite，且没有用户系统。**&#8203;
   已支持保存与分享链接（见决策 11），但库是单文件、单副本的：
   多副本部署时各副本各写各的，分享链接只在写入那个副本上有效。
   也没有"我的行程列表"——没有用户系统，做列表等于给用户看一堆别人的行程。

7. **限流计数器在进程内存里。**&#8203;
   多副本部署时实际额度 = 单副本额度 × 副本数。要精确限流必须换成 Redis，
   `docker-compose.yml` 里的 redis 服务就是留给这件事的（目前仍是占位，未接入）。

8. **评测集的"行程合理性"一栏还是空的。**&#8203;
   结构化成功率、景点真实存在率、坐标准确率已由 60 条用例跑出实数（见「实测数据」），
   但合理性必须由人打分，模板已生成、尚未填写。
   **在这一栏填完之前，"行程好不好"没有数值声明**——自动化指标证明不了它。

9. **日志只在进程内，没有持久化与采集。**&#8203;
   trace_id、token 计数、结构化输出成功率全部是内存态，进程重启即清零，
   也没有接入任何日志系统（ELK / Loki / 云日志）。当前只解决"单机可排查"，
   不解决"跨实例聚合"。

10. **token 探针依赖框架私有属性 `HelloAgentsLLM._client`。**&#8203;
    框架本身丢弃了 `response.usage`，只能从底层客户端拿。框架升级后若该属性消失，
    探针会安装失败并降级为 `available=false`（**不填 0**），此时日志里 token 显示为
    unavailable。这是已知的技术债，替代方案是改用支持 usage 透传的框架或自研 LLM 适配层。

11. **缓存是纯进程内的，且只有 TTL 没有主动失效。**&#8203;
    进程重启即全部丢失；多实例部署时各实例的缓存互不相通，命中率会随实例数下降。
    也没有 Redis 这类共享层。要让缓存真正生效需要考虑请求亲和（sticky session）
    或换成分布式缓存——当前是单机方案。

12. **缓存键的一致性依赖模型输出。**&#8203;
    Agent 侧的缓存键是模型生成的工具参数（`{"keywords": "苏州园林", "city": "苏州"}`）。
    同一个意图，模型这次写成"苏州园林"、下次写成"苏州 园林"，就是两个不同的键、两次未命中。
    这是为什么实测规划主链路命中率只有 57%，且在确定性查询层（参数由 URL 给定）
    能接近 100%。要改善需要在缓存键之前做参数归一化。

13. **Docker 镜像没有在本机构建验证过。**&#8203;
    开发环境没有装 Docker，两个 Dockerfile 与 `docker-compose.yml` 只做了
    YAML/语法层面的检查，以及 CI 里加了一个 `docker compose build` 任务来做构建验证。
    首次在真实环境跑之前，建议先单独 `docker compose build backend` 确认
    `uvx` 在运行镜像里可用（这是唯一一个"构建成功但运行期才暴露"的依赖项）。

14. **测试只覆盖了解析、校验、路由与前端纯逻辑。**&#8203;
    Agent 编排、MCP 调用、LLM 输出质量这些真正复杂的部分没有被自动化测试覆盖——
    它们要么依赖外部服务，要么每次调用都要真金白银。目前靠 `/api/trip/health`
    与结构化日志做运行时观测，不是断言。
    （P2-1 的评测集是对这一条的**部分**补救：它量化了端到端结果的质量，
    但不是断言式的回归测试，跑一次要几十分钟和真实配额。）

15. **前端没有引入 Pinia，`Result.vue` 也没有拆分。**&#8203;
    当前只补了加载骨架屏、错误边界与分享入口（这是用户能直接感知的部分）。
    状态仍然散落在组件里，`Result.vue` 仍有上千行。
    这是**刻意砍掉的**：组件拆分与引入状态库是纯粹的"可维护性改善"，
    在面试与使用中都不产生可验证的收益，优先级低于"能证明效果"的评测集。
    要做的话顺序是：先按地图/行程卡/预算/天气拆子组件，再考虑 Pinia——
    只有一个视图时引入 Pinia 只是把局部状态搬到全局，反而更难追踪。

16. **分享链接没有过期机制，也没有访问控制。**&#8203;
    拿到链接的人可以无限次查看（浏览量会累加），删除只能靠创建时拿到的管理令牌。
    真实产品需要有效期、访问次数上限与回收入口。

## 说明

本项目参考 Hello-Agents 教程第 13 章的示例实现，并在此基础上做了以下改造：

- 流式执行链路（SSE + 工作线程桥接，前端进度由真实阶段事件驱动）
- 统一 MCP 实例（消除两套客户端导致的配置漂移）
- 确定性查询层补全（POI / 天气 / 路线不经 LLM）
- 健康检查与状态暴露（子 Agent、MCP、LLM、结构化输出统计）
- **结构化输出可靠性**：JSON 模式 + Pydantic 严格校验 + 错误回灌自纠重试 + 明确失败；
  删除原有的占位行程兜底逻辑
- **三路检索并发化** + 失败分级降级，端到端延迟下降约 41%（见「实测数据」）
- **结构化日志 + trace_id**：全项目 `print` 清零，改用 loguru 结构化事件，
  一次请求可在日志中完整还原（含各阶段耗时与 token 消耗）
- **缓存与成本控制**：外部 API 响应按数据新鲜度分档缓存（下沉到 MCP 层，覆盖规划主链路），
  配套输入长度约束与单次规划的耗时 / token 预算上限
- **鉴权与限流**：API Key 鉴权 + 滑动窗口限流（规划接口与普通查询分档），
  纯 ASGI 中间件实现以兼容 SSE；未配置 Key 时放行但启动告警
- **行程持久化与分享**：SQLite 存行程，生成不可猜测的分享令牌与管理令牌；
  保存失败不影响规划结果返回
- **量化评测集**：20 城 × 3 类偏好共 60 条用例，量化结构化成功率、景点真实存在率、
  坐标准确率与结构完整性；合理性一栏留人工打分模板
- **前端可用性**：结果页补加载骨架屏与错误边界，支持分享链接直达

Agent 编排结构、提示词工程与上述改造部分为独立设计与实现。

## 致谢

- [Hello-Agents](https://github.com/datawhalechina/Hello-Agents) - 智能体教程
- [HelloAgents 框架](https://github.com/jjyaoao/HelloAgents) - 智能体框架
- [高德地图开放平台](https://lbs.amap.com/) - 地图服务
- [amap-mcp-server](https://github.com/sugarforever/amap-mcp-server) - 高德地图 MCP 服务器

## 开源协议

CC BY-NC-SA 4.0
