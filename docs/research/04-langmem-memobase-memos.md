# 04 · LangMem / Memobase / MemOS 拆解

对象：给本机 memcore（`~/.dsh/memory-core`，Python 标准库 + SQLite 单文件、不接向量服务、不依赖 LLM 在线）找可借鉴机制。

读的是 README + 源码/文档原文，不是二手博客。结论里的 `文件:行` 指本机 memcore 位置；外部结论给 URL。没核实的一律标 **未核实**。

memcore 现状基线（后文引用）：

- `memcore.py:571 maintain()` = ingest_sessions → relink → backup → gc → export_markdown → brief → rotate → backup，同步一把梭，无任务队列、无中途状态。
- `memcore.py:984 activation()` = `recency*0.52 + salience*0.20 + reinforce*0.12 + wake*0.10`，阈值 0.45/0.26/0.12（`_dorm_thresholds`），pinned floor 0.80。
- `memcore.py:48 KIND_DEFAULTS` 每个 kind 带 `(salience, halflife_days)`；`memcore.py:63 DEFAULT_CONFIG.weights` 是检索权重（lexical 0.50 / semantic 0.22 / overlap 0.12 / title 0.16 / recency 0.10 / salience 0.10 / reinforce 0.08 / pinned 0.15）。
- `memcore.py:2142 consolidate()` 已有 dup/conflict 两档 + `llm=True` 时用 `_llm_judge()`（`memcore.py:2052`）仲裁，但 `auto_retire` 默认 False（注释记了 60 条误删 56 条的事故）。
- CLI 面：`add/search/recall/brief/preflight/learn/states/wake/sleep/unsleep/consolidate/relink/gc/maintain/ingest/export/reindex/doctor`。

---

## LangMem

### 定位/许可证/语言/时间线

- LangChain 官方 agent memory SDK。定位是"给 agent 用的记忆原语 + LangGraph 存储集成"，不卖服务。
- 许可证 **MIT**（`LICENSE`：`Copyright (c) 2025 LangChain`）。语言 **Python ≥3.10**（PyPI `requires_python`）。
- 时间线（PyPI release 记录）：首个公开版 `0.0.0rc0` 2024-02-21 → `0.0.1` 2025-01-23（因只兼容 v0 alpha 服务被 yank）→ 稳定线 `0.0.10` 起 2025-02 密集迭代 → 最新 `0.0.30` 2025-10-27。
- 依赖重：`langchain>=0.3.15, langchain-core>=0.3.46, langgraph>=0.6.0,<2, langgraph-checkpoint>=2.0.12, langsmith>=0.3.8, trustcall>=0.0.39` + provider 包（`langchain-anthropic`/`langchain-openai`）。`trustcall` 是它的结构化 patch 引擎，绕不开。

### 记忆分类：它把记忆分成哪几类，各自存什么、什么时候写

官方 conceptual guide 的三分法（`docs/docs/concepts/conceptual_guide.md`）：

| 类型 | 存什么 | 典型存储形态 | 什么时候写 |
|---|---|---|---|
| Semantic（事实/知识） | 用户偏好、知识三元组、profile 字段 | **Collection**（无界条目，运行时检索）或 **Profile**（单文档、schema 固定、按需整份更新） | 每轮对话后抽取；profile 走 update 不 insert |
| Episodic（经历） | 成功交互的四段式：`observation / thoughts / action / result`（"当时怎么想、做了什么、为什么有效"） | Collection（few-shot 用） | 交互成功之后（作为可复用样例） |
| Procedural（行为） | agent 的人格、回答风格、规则 | prompt rules 或 Collection | 由 `create_prompt_optimizer(kind="metaprompt", max_reflection_steps=N)` 从轨迹 + `user_score` 反馈里迭代 |

关键一句（原文）：collection 的复杂度在于"必须把新信息与旧信念对账：delete/invalidate 或 update/consolidate"，**过抽 → 精度掉，欠抽 → 召回掉**，所以它做"memory enrichment"来平衡。

### 写入路径：hot path vs background 各自做什么、怎么触发

**Hot path（对话中同步写）**

- 形态：`create_manage_memory_tool(namespace=...)` + `create_search_memory_tool(...)` 挂给 agent，**由 agent 自己在对话中决定写什么、什么时候搜**（README "the agent gets to decide what and when to store"）。
- 代价（原文直说）：增加可感知延迟，并且"多了一个妨碍 agent 完成用户任务的障碍"（一笔 tool choice 预算）。
- 适合：关键上下文必须立刻落地（用户改了口径、下了决定）。

**Background（异步整理）**

- `create_memory_store_manager(...)` / `MemoryManager` + `ReflectionExecutor(manager, store=...)`；`reflection.submit({"messages": ...}, after_seconds=N)`。
- `after_seconds` 明确用于 **debounce + 去重**："Adding a delay lets you debounce and deduplicate reflection work whenever the user is actively engaging with the agent."
- 触发点：一轮回复结束之后（或对话空闲一段之后）"对整段会话反思"，而不是每句都抽。
- 抽取执行体（`src/langmem/knowledge/extraction.py`，`MemoryManager`）：
  - `enable_inserts / enable_updates / enable_deletes` 三个开关（默认 insert+update 开、delete 关）；
  - 用 `trustcall.create_extractor(tools=[schema...])` 让 LLM 输出 **patch 动作**（insert/update/RemoveDoc），`existing` 里带已有记忆一起喂；
  - `max_steps>1` 时进入多轮收敛：第一轮之后追加 `Done` 工具，把上一轮动作以 `tool` 消息回灌，"直到模型说 Done 或不再发 tool_calls"；
  - 内置 `_MEMORY_INSTRUCTIONS` 的策略词很值得抄：caveat 不确定信息并给 `p(x)`、consolidate/compress 冗余、"strengthen based on reliability and recency"、"prioritize retention of surprising (pattern deviation) and persistent (frequently reinforced) information"、"prefer dense, complete memories over overlapping ones"、"maximize SNR by avoiding idle words"。

### 检索路径

- 存储层是 LangGraph `BaseStore`：`get()` 直取、`search(query=...)` 语义检索、`filter={...}` 元数据过滤，三路可叠。
- namespace 支持模板变量（`("acme_corp", "{user_id}", "code_assistant")`），运行时从 `configurable` 注入。
- 相关性口径（原文，最重要的一句）：**"memory relevance is more than just semantic similarity. Recall should combine similarity with 'importance' of the memory, as well as the memory's 'strength', which is a function of how recently/frequently it was used."**
- 可选 `query_model`：用更小更快的模型先把会话编成检索 query，再拿它去 store 搜（`query_limit` 默认 5）；`default` / `default_factory` 支持"空库时先放一份模板 profile，之后在它上面演化"。

### 关键机制 3~5 条（机制 → 解决什么失败模式 → memcore 能否用）

1. **insert/update/delete 三动作 patch，而不是只 append**
   - 失败模式：记忆只增不改 → 同一事实多个版本、旧结论永久残留、检索时互相打架。
   - memcore：`consolidate()`（`memcore.py:2142`）已经能做 dup 合并 + conflict 报告 + `retire` 关闭 `valid_to`，`history()`（`memcore.py:853`）能看 supersedes 链 —— 机制在，但**触发是手动/批量余弦**，不是"写入时对账"。能用，见落点 2。

2. **多步 `max_steps` + `Done` 收敛**
   - 失败模式：一次抽取贪多必漏；提示词要求"一次并行多 tool call 全做完"反而降低质量。
   - memcore：`consolidate()` 单趟；可以做成"多趟直到无新增合并候选"。能用（S）。

3. **`ReflectionExecutor(after_seconds=N)` 的 debounce/去重**
   - 失败模式：用户连续追问时每轮都反思一次，LLM 成本 ×N 且互相覆盖。
   - memcore：`maintain()` 是手动一次性；可以加"同 project 在 N 分钟内不重复跑"的时间闸。能用（S）。

4. **相关性 = similarity × importance × strength(recency/frequency)**
   - 失败模式：纯余弦检索把"很久没用但词面像"的条目顶上来，或者把"刚被反复用到但措辞不同"的条目漏掉。
   - memcore：`activation()`（`memcore.py:984`）就是 importance×recency×reinforce 的同构物，但检索侧是**加项**（weights 里 recency/salience/reinforce 各占 0.10/0.10/0.08）而不是乘子。可以用，见落点 5。

5. **profile vs collection 的二选一决策**
   - 失败模式：把"当前状态"当流水账记（用户改了偏好，历史值还在检索里），或把流水账塞进 schema（丢信息）。
   - memcore：`identity/preference` 本质是 profile 语义，但表结构是 append-only + `valid_to`。可用（M），见落点 2。

### 不要抄的部分

- 依赖链：`langgraph` + `trustcall` + `langsmith` + provider 包，任何一条都跟"Python 标准库单文件"冲突。
- **每个记忆操作都要 LLM 在线**：`MemoryManager.invoke()` 的本质就是"prompt LLM 决定如何改记忆"，无网络即无记忆写入。memcore 的 hot path 必须保持纯本地。
- `InMemoryStore` 默认非持久（README 自己提示"process memory—lost on restart"）；真持久要 Postgres，等于引入服务。
- `enable_deletes=True` 的自动删除：作者默认关着，说明"让 LLM 删记忆"风险高。memcore 侧对应事故已经踩过（`consolidate` 的 `auto_retire=False` 注释）。
- namespace 三级模板：单 agent 本地库用 `project` 一维就够，多一层是纯抽象成本。

### 来源链接

- README：https://github.com/langchain-ai/langmem
- 概念（三分法 / hot vs background / relevance 口径）：https://github.com/langchain-ai/langmem/blob/main/docs/docs/concepts/conceptual_guide.md
- 抽取实现（MemoryManager / ReflectionExecutor / `_MEMORY_INSTRUCTIONS`）：https://github.com/langchain-ai/langmem/blob/main/src/langmem/knowledge/extraction.py
- LICENSE（MIT）：https://github.com/langchain-ai/langmem/blob/main/LICENSE
- 版本与依赖：https://pypi.org/pypi/langmem/json

---

## Memobase

### 定位/许可证/语言/时间线

- **面向"用户画像"的记忆服务**，原文立场："**Memory for User, not Agent**"——存的是用户是谁、发生过什么，不是 agent 的工作草稿。
- 许可证 **Apache-2.0**（README License 节）。语言 Python（FastAPI 服务端）+ 三套 client（Python / TS / Go）+ MCP。服务端生产形态 = **FastAPI + Postgres(+pgvector) + Redis**，全 docker 化。
- 版本线（README "Recent Updates"，只记影响机制的点）：
  - `0.0.36`：`context` api 检索 500~1000ms（取决于 embedding API），支持传 prompt 模板直接打包记忆；
  - `0.0.37`：加 **fine-grained event gist**，可对用户 timeline 做细粒度检索；
  - `0.0.40`：单次 run 的 LLM 调用从约 3~10 次压到**固定 3 次**，token 成本降约 40~50%。
- 明确不在 RAG 赛道，但 LOCOMO 上仍报 SOTA（其自述，**未独立核实**）。

### 记忆分类：它把记忆分成哪几类，各自存什么、什么时候写

数据模型（`src/server/api/memobase_server/models/database.py`）：

| 表 | 存什么 | 关键列 |
|---|---|---|
| `user_profiles` | 用户画像"槽位" | `content TEXT` + **`attributes JSONB`**（承载 `topic` / `sub_topic`） |
| `user_events` | 发生过的**事件**（时间性问题的原料） | `event_data JSONB` + `embedding Vector` |
| `user_events_gists` | 事件的细粒度摘要（0.0.37 加） | `gist_data JSONB` + `embedding` + `event_id` |
| `general_blobs` | 原始输入块（`blob_type` + `blob_data`），**默认处理完即删** | `blob_type`, `blob_data` |
| `buffer_zones` | 待处理队列（每 user 一条积压） | `token_size`, `status`(idle/processing/done/failed), `blob_id` |

profile 的对外形状是 `topic / sub_topic / content` 三元组 + 元信息（`id`/`created_at`），README 给的样例：`basic_info.name = "오*영"`、`interest.youtube_channels = "Kurzgesagt"`、`education.notes = "..."`。也就是说 **profile 是"有 schema 的槽位表"，不是自由文本**，槽位由 project 级配置（`projects.profile_config`）定义，可自定义。

### 写入路径：hot path vs background 各自做什么、怎么触发

它**明确拒绝 hot path 记忆化**（README 原文："In Memobase, we don't memoize users in hot path. We use buffer zones for the recent inserted blobs."）。

完整链路（`controllers/buffer.py` + `controllers/buffer_background.py` + `controllers/modal/chat/__init__.py`）：

1. `insert(ChatBlob)` → 写 `general_blobs` + `buffer_zones(status=idle, token_size=ptoken 估算)`。
2. **flush 触发三选一**：buffer 累计 token 超 `max_chat_blob_buffer_token_size`（README 举例 1024 tokens / 空闲 1 小时）、手动 `flush(sync=True)`（应用侧"会话关闭"时最合理）、后台排队消费。
3. `flush_buffer_by_ids()`：把 buffer 置 `processing` → join 出 blobs → 调 `BLOBS_PROCESS[blob_type]` → 成功置 `done`（并按配置删原始 blob）/ 失败置 `failed`（可重放）。并发保护是 **per-user Redis 锁（带 Lua 校验释放）+ Redis 队列 + 锁续期**，`max_iterations=200` / 单次 15 分钟 / 连续 5 次错误即停。
4. `process_blobs(chat)` 内部流水：
   - `truncate_chat_blobs()` 按 token 预算裁尾；
   - `entry_chat_summary()` → 得到 `user_memo_str`（这一步把"聊天原文"压成"关于用户的事实陈述"）；
   - **并行两路**：`process_profile_res()`（`extract_topics` → `merge_or_valid_new_memos`（merge_yolo）→ `organize_profiles` → `re_summary`）与 `process_event_res()`（`tag_event`）；
   - `append_user_event()` 写一条 `user_events`，`event_data` 里带 `event_tip` / `event_tags` / **`profile_delta`**（这次事件改动了哪些 profile 槽位——事件与画像互相可追溯）；
   - `add_update_delete_user_profiles()` 落库，返回 `add/update/delete` 三组 id。
5. `re_summary`：某个 profile 槽位膨胀就重新摘要压缩（槽位级的"防爆仓"）。

### 检索路径

两条完全不同的路，成本差一个数量级：

- **profile 直读（<100ms）**：几发 SQL 就能拿到画像 + 事件时间线，"不需要任何预处理"——因为它把抽取成本已经前置到 flush 了。
- **`context` 打包（500~1000ms）**：`u.context(max_token_size=500, prefer_topics=["basic_info"])`，支持传 prompt 模板；输出形如 `## User Background:` + `## Latest Events:`，且自带一句行为约束"Unless the user has relevant queries, do not actively mention those memories"。
- event/gist 的 embedding 语义检索是第三条路（gist 存在就是为了让 timeline 可细查）。

### 关键机制 3~5 条（机制 → 解决什么失败模式 → memcore 能否用）

1. **buffer zone + 状态机 + per-user 锁**
   - 失败模式：边聊边抽取 → 延迟高、成本高、同一段对话被重复处理（幂等性缺失）。
   - memcore：`maintain()` 无队列、无中间状态、无锁；两进程同跑只能靠 SQLite 事务兜底。可以借"任务表 + 状态 + 重试"的最小版，见落点 1。

2. **"槽位"语义：同 (topic, sub_topic) 只保留最新值（add/update/delete 三路）**
   - 失败模式：画像类事实越写越多版本（"用户喜欢深色模式" / "用户说深色刺眼"并存），检索时随机胜出。
   - memcore：现在靠 `dedupe_exact` + `consolidate` 余弦 0.94 兜底，属于"事后按相似度猜"，不是"按语义槽位覆盖"。可用（M），见落点 2。

3. **`re_summary` 槽位压缩**
   - 失败模式：单个字段越写越长（把整段对话塞进 `education.notes`），最后 profile 拖垮 prompt 预算。
   - memcore：`body` 无上限，`consolidate` 只合并相似条目、不压单体。可用（S），见落点 3（写成 maintain 的"超长条目压缩"任务）。

4. **event 与 profile 分离 + `profile_delta` 反向引用**
   - 失败模式：只存"是什么"不存"什么时候变成的"，时间性问题（"上次说的那个方案"）答不了；合并/回滚时不知道谁改了谁。
   - memcore：`valid_from/valid_to` + `history()` 已有时间窗，但没有"事件"这一层；`timeline()`（`memcore.py:1760`）是按 `updated_at` 拉最近条目，语义上更像 activity log 而非 event。部分可用（M），见落点 7。

5. **`context(max_token_size, prefer_topics, prompt_template)` 打包器**
   - 失败模式：召回一堆条目塞不进预算；或者塞进去了但关键段（身份/偏好）被挤掉。
   - memcore：`brief()`（`memcore.py:1670`）已经在做预算分配 + 分段 + cap，缺 `prefer_topics` 这种"显式优先段"入参和"最新事件"独立段。可用（S），见落点 3。

### 不要抄的部分

- **必须有 LLM + embedding 服务**：抽取、tag、merge、re_summary 全是 LLM 调用（固定 3 次/run 也只是"少"，不是"无"）。memcore 的降级路径只能是"没有 LLM 时这些任务整个跳过，只保留词法/向量相似度那套"。
- Postgres + pgvector + Redis 的运维面：为了 buffer 队列与锁引入 Redis，对单机单 agent 是纯成本（用 SQLite 一张表 + 事务即可）。
- 多租户结构：`(id, project_id)` 复合主键 + project 级 `profile_config`，本地单用户用不上。
- "默认处理完即删原始 blob"：可配置，但默认值意味着**原始语料不留底**，对"以后想重新抽取"是死路；memcore 的 `ingest_sessions` 保留原始归档更稳。
- 让 LLM 决定 `delete` 哪些 profile：与 LangMem 默认关 delete 同理，误删不可逆。

### 来源链接

- README（定位、buffer 阈值、context api、0.0.36~0.0.40 changelog）：https://github.com/memodb-io/memobase/blob/main/readme.md
- 数据模型：https://github.com/memodb-io/memobase/blob/main/src/server/api/memobase_server/models/database.py
- buffer 状态机/幂等/失败回滚：https://github.com/memodb-io/memobase/blob/main/src/server/api/memobase_server/controllers/buffer.py
- 后台队列 + per-user 锁：https://github.com/memodb-io/memobase/blob/main/src/server/api/memobase_server/controllers/buffer_background.py
- 抽取流水（summary → topics → merge → organize → re_summary → event）：https://github.com/memodb-io/memobase/blob/main/src/server/api/memobase_server/controllers/modal/chat/__init__.py
- blob 类型分派：https://github.com/memodb-io/memobase/blob/main/src/server/api/memobase_server/controllers/modal/__init__.py

---

## MemOS

### 定位/许可证/语言/时间线

- **"Memory OS"** 定位：把记忆当**系统资源**来管（表示、调度、演化），论文 `arXiv:2507.03724` "MemOS: A Memory OS for AI System"（v1 2025-07-04，v4 2025-12-03；36 页）。前身短版 `arXiv:2505.22101`。
- 许可证 **Apache-2.0**。语言 Python；当前仓库 README 是 **MemOS 2.0 "Stardust"**，同时提供 Cloud API / 自托管（Neo4j + Qdrant）/ Cloud plugin / **local plugin（本地 SQLite + FTS5 + vector）**，并且有 DeepSeek Harness 的接入说明。
- 注意版本漂移：论文讲的是 MemCube + 三类记忆 + 调度/生命周期；2.0 README 的主推面是"unified memory API / multi-cube KB / MemScheduler / feedback&correction"，**论文概念在代码里仍在**（见下），但 README 叙事重心已转工程化。读 README 不看论文会漏掉生命周期语义。

### 记忆分类：它把记忆分成哪几类，各自存什么、什么时候写

论文摘要的三类（"unifies the representation, scheduling, and evolution of **plaintext, activation-based, and parameter-level** memories"），在代码里能一一对上（`src/memos/memories/factory.py`）：

| 论文类别 | 代码 backend | 存什么 | 写入时机 |
|---|---|---|---|
| **Plaintext（明文）** | `naive_text` / `general_text` / `tree_text` / `simple_tree_text` / `pref_text` / `simple_pref_text` | 文本记忆条目；`tree_text` 是树/图层（图库）、`pref_text` 是偏好专用 | 交互后由 reader 抽取写入 |
| **Activation（激活）** | `kv_cache` / `vllm_kv_cache`（`KVCacheMemory`/`VLLMKVCacheMemory`） | KV-cache 片段（把常驻上下文压成可复用缓存） | 高频/近期使用的明文记忆被"提升"成激活记忆，由 `ActivationMemoryManager` 周期性 dump |
| **Parametric（参数化）** | `lora`（`LoRAMemory`） | LoRA 权重（把知识写进参数） | 长期稳定的知识沉淀后训练/加载 |

外加 `pref_mem`（偏好）在 `GeneralMemCube` 里是**第四个子记忆槽**，backend 仍是 text 家族。

### MemCube 定义

- 论文原话（摘要）："**As the basic unit, a MemCube encapsulates both memory content and metadata such as provenance and versioning. MemCubes can be composed, migrated, and fused over time, enabling flexible transitions between memory types** and bridging retrieval with parameter-based learning."
  - 三个动作：**compose（组合）/ migrate（迁移）/ fuse（融合）**；四类元数据重点：**provenance + versioning**。
- 代码（`src/memos/mem_cube/general.py`）：`GeneralMemCube` docstring 直说 "MemCube is a box for loading and dumping three types of memories"，持有 `_text_mem / _act_mem / _para_mem / _pref_mem`，任一可为 `uninitialized`；`load(dir, memory_types=[...])` / `dump(dir, memory_types=[...])` 支持**按类选择性搬运**，`dump` 要求目标目录为空（避免覆盖），`init_from_dir` 会校验 `config.json` 的 `model_schema` 版本，还能 `init_from_remote_repo`（HuggingFace）。
  - 工程含义：**cube 是一个可拷贝、可版本校验、可按记忆类裁剪的整体工件**——不是"一个数据库连接"。

### 调度与生命周期

- `SchedulerFactory` 两种后端：`general_scheduler` / `optimized_scheduler`（`src/memos/mem_scheduler/scheduler_factory.py`）。
- `BaseScheduler`（`src/memos/mem_scheduler/base_scheduler.py`）的组成说明它的调度观：
  - **队列**：`ScheduleTaskQueue`（可 Redis 或进程内）、`AutoDroppingQueue`（满了丢最旧，防雪崩）、`max_internal_message_queue_size`；
  - **分发**：`SchedulerDispatcher`（线程池 `thread_pool_max_workers`、`enable_parallel_dispatch`）+ `SchedulerOrchestrator`（按 handler 编排）+ `disabled_handlers`（可关某类任务）；
  - **状态**：`TaskStatusTracker`（任务级进度，Redis 时可用）+ `SchedulerGeneralMonitor` / `DispatcherMonitor` / `TaskScheduleMonitor`（三个 monitor 各盯一层）；
  - **跨类生命周期动作**：`update_activation_memory()` 与 `update_activation_memory_periodically(interval_seconds=...)` —— **把明文记忆定时"提升"为激活记忆并 dump 到磁盘**，这是论文里 "flexible transitions between memory types" 的落地代码；
  - **搜索**：`SchedulerSearchService` / `SchedulerRetriever` / `Searcher`（`tree_text_memory` 的 searcher）+ 可选 reranker（`HTTPBGEReranker`）。
- 生命周期链条：**明文（可检索、可编辑）→ 激活（KV，省 prompt）→ 参数化（LoRA，省检索）**；反向由 feedback（`mem_feedback/`）+ 修正接口驱动。`src/memos/dream/` 目录存在，**其内容未核实**（GitHub API 限流，未能取到文件）。

### 关键机制 3~5 条（机制 → 解决什么失败模式 → memcore 能否用）

1. **cube 作为"可 dump/load + 版本校验"的工件**
   - 失败模式：记忆库和它的配置/格式漂移（升级后读不回、迁移时半残）；想"只搬明文不搬参数"做不到。
   - memcore：`memcore.py:532 backup()`/`556 _rotate()` 已经在做快照轮转，`export_markdown/jsonl` 能导出，`SCHEMA_VERSION` + `_meta_init()` 在管版本 —— 机制齐了大半，缺的是"按类选择性导出 + 导入校验"。部分可用（S），见落点 7。

2. **分级记忆 + 迁移（明文 → 激活 → 参数化）**
   - 失败模式：所有记忆一个待遇 —— 高频核心事实每次都要检索、都要占 token；低频噪音也走同一条路。
   - memcore：四档生命周期（清醒/模糊/沉睡/深度沉睡）+ `pinned` 是"降级"方向；**缺"升级"方向**（把反复命中/反复唤醒的条目固化成 short-term 常驻，或提升 salience）。可用（M），见落点 6。真做 KV/LoRA 不值得（无训练回路），只借"分级 + 升级"的形。

3. **调度器把"写"和"整理"解耦成队列任务 + 任务状态**
   - 失败模式：`maintain()` 一次跑 8 个步骤，任一步崩了就"不知道做到哪"，重跑要从头；跑的时候不能并发、不能观测。
   - memcore：`maintain()` 无状态机。可用（S~M），见落点 1（这张表就是 MemScheduler 的 20 行 SQLite 版）。

4. **provenance + versioning 进记忆本体**
   - 失败模式：合并/退役之后查不到"这条是从哪来的、谁改的、上一版是什么"，误合并不可回滚。
   - memcore：`valid_from/valid_to` + `history()` + `_journal()`（`memcore.py:1817`）已覆盖"版本"，缺"来源"（`source/source_ref` 在 `add()` 里已有字段但未用于链路）。可用（S），见落点 7。

5. **reranker 二次排序**
   - 失败模式：词法/向量初排把"字面像但语义无关"的排前面；长 query 尤其明显。
   - memcore：`_select_candidates()`（`memcore.py:1306`）是 FTS + 向量 + overlap + title 的加权和，`search()` 有 MMR（`mmr_lambda=0.72`），**没有 cross-encoder 级重排**。可用（S），见落点 8（只留钩子，不引模型）。

### 不要抄的部分

- **全家桶基础设施**：Neo4j（图）+ Qdrant（向量）+ Redis/RabbitMQ（队列）+ 三个 monitor 线程。单机单 agent 上这些每一个都是运维负债，收益接近 0。
- **参数化记忆（LoRA）/ KV-cache 记忆**：需要训练/推理回路与 GPU，本地库没有落点；"把稳定知识写进权重"在 agent 侧还带来不可解释与不可回滚。
- **多 cube 组合/迁移/融合**：`multi_mem_cube/` 与 `readable_cube_ids/writable_cube_ids` 是为多 agent / 多租户设计的；memcore 一个 SQLite 文件就是"一个 cube"。
- **Cloud API 依赖**：`mpg-` key + 远端 recall/save 的插件形态，与"不依赖 LLM/服务在线"的定位冲突（其 README 也强调 cloud plugin 是 fail-open，正因为远端会挂）。

### 来源链接

- README（2.0 Stardust、四入口、plugin、license）：https://github.com/MemTensor/MemOS
- 论文摘要（MemCube 定义 + 三类记忆 + compose/migrate/fuse）：https://arxiv.org/abs/2507.03724
- 记忆 backend 分派（textual/activation/parametric）：https://github.com/MemTensor/MemOS/blob/main/src/memos/memories/factory.py
- MemCube 实现（load/dump/选择性搬运/版本校验）：https://github.com/MemTensor/MemOS/blob/main/src/memos/mem_cube/general.py
- 调度器（队列/分发/状态/monitor/激活记忆周期提升）：https://github.com/MemTensor/MemOS/blob/main/src/memos/mem_scheduler/base_scheduler.py
- 调度器后端：https://github.com/MemTensor/MemOS/blob/main/src/memos/mem_scheduler/scheduler_factory.py

---

## 对 memcore 的具体落点

排序原则：先做"低成本、可回滚、不引依赖"的；凡是需要 LLM 在线的一律标代价并给降级路径。

**【落点】`memcore.py:571 maintain()` → 拆成"任务表 + 状态机"两步，而不是一串直线调用 → M → 风险：schema 迁移要兼容老库；并发写要靠事务，别重演 `_next_id()` 注释里 4×40 并发只落 110 行那类读-改-写冲突。**
借 Memobase `buffer_zones` 与 MemOS `ScheduleTaskQueue`/`TaskStatusTracker`：新增 `maintenance_tasks(id, name, status[idle|running|done|failed], attempts, updated_at, note)`，`maintain()` 改成"认领一条 idle → running → 执行 → done/failed"；`--resume` 只跑 failed+idle。好处是崩了知道断在哪、可以只重跑 gc，不用整条链重来。

**【落点】`memcore.py:2142 consolidate()` + `memcore.py:48 KIND_DEFAULTS` → 给 `preference/identity/fact` 引入"槽位键"（subject+attr），同键新值覆盖旧值（旧行 `retire` 到 `valid_to`），不再靠余弦 0.94 猜 → M → 风险：槽位抽取若靠 LLM 则离线不可用，若靠正则则漏抽；误并代价高于漏并（代码注释已有"60 条误删 56 条"的教训），必须先 dry-run 三周。**
借 Memobase 的 `topic/sub_topic` 槽位语义。降级路径：无 LLM 时槽位键由 `kind + project + tags + title` 的规范化串生成（`_path_keys` 那套已有现成实现可复用），只覆盖"标题即属性"的窄集（偏好、身份），其余仍走余弦。

**【落点】`memcore.py:1670 brief()` → 增加 `prefer=("identity","preference")` 显式优先段 + "最近 7 天新增且 use_count=0"独立段，把新记忆顶到简报里 → S → 风险：预算被挤压，`used + est_tokens > budget` 的早退会让后面的段整体消失，需要按段预留配额而不是顺序吃。**
借 Memobase `context(max_token_size, prefer_topics)`：现在 `brief()` 六段是固定顺序 + `cap`，没有权重入参；新记忆段是"召回盲区"的补丁（刚写进去的记忆在 45 天决策段/21 天项目段里可能都进不去）。

**【落点】`memcore.py:635 add()` → 保持纯本地零网络（现状已如此），但把"要不要 LLM"的判定从 `add()` 彻底移到 `maintain()`，并在 `add()` 里对超长 body 只做截断提示不做摘要 → S → 风险：几乎没有；注意 `_fuzzy_duplicate`（`memcore.py:1772`）已经用了本地向量，别顺手在那里加 LLM 调用。**
这是 LangMem hot/background 分工在 memcore 上的正确投影：**写入时轻**（`add` 只做 norm/dedupe/embed/autolink）、**维护时重**（`maintain` 里跑 `consolidate(llm=...)`）。代价明说：`consolidate(llm=True)` 需要 LLM 在线，离线时必须整体跳过并保留报告（现在是 `if llm and not dry_run` 才调 `_llm_judge`，默认路径已经安全）。

**【落点】`memcore.py:1414 search()` 与 `memcore.py:984 activation()` → 把 activation 从"加项之一"改成"乘子"（`score *= 0.6 + 0.4*activation`），避免沉睡条目靠词法分冲上来 → M → 风险：全库排序口径变化，必须用 `bench.py` / `tests_eval.py` 回归；权重 `DEFAULT_CONFIG.weights` 注释写着"validated on the real store (12/12 paraphrase queries)"，改之前先把这 12 条查询跑成基线。**
依据是 LangMem 原文的 relevance 三因子（similarity × importance × strength），memcore 目前把 recency/salience/reinforce 塞进同一个线性加权和里，语义上等于"用近期性替代相关性"，而不是"用相关性再乘一个活性系数"。建议先加一个 `cfg["rank"]["activation_multiplier"]=false` 开关默认关，A/B 后再开。

**【落点】`memcore.py:1784 gc()` → 冷条目先降级不直删：`cold` 分支改为 `update(salience=salience*0.5)`，连续 N 次仍冷才 `forget`；`kind='episode'` 的 120 天硬阈值保留为最后一道闸 → S → 风险：库里条目变多（深睡区膨胀），`states`/`sleepers`（`memcore.py:1170`）要能扛住；`mem` 的 `--json` 输出字段别改，UI（`ui/`、`server.py`）在吃。**
借 MemOS 的"分级 + 迁移"与 Memobase 的 tier 思路，但只做"降级"这一半（升级那一半见下条）。

**【落点】`memcore.py:1113 wake()` / `memcore.py:984 activation()` → 加"升级"方向：`use_count>=N` 或 `wake_count>=M` 的 active 条目自动提 `salience` 并在 brief 里进"核心事实"段；同时给 `add()` 补 `provenance`（`source/source_ref/derived_from` 写进 `meta`，`consolidate` 合并后写 `derived_from=旧id`）→ M → 风险：salience 单调上升会让"曾经重要"永久重要，需要一个随时间回落的机制（现成的 `_life_days` 半衰期可复用）；`meta` 是 JSON，`server.py`/UI 要不要透出得先对一遍字段。**
这是 MemOS "明文 → 激活 → 参数化"在本地库的**唯一可落地投影**：不引 KV/LoRA，只把"经常被用到的明文"提升到常驻/高权重层，把"很久没用的"留在沉睡层 —— 即"迁移"，不是"训练"。

**【落点】`memcore.py:1760 timeline()` → 从"最近 updated_at 列表"升级成"事件流"：`consolidate` 的 merge/retire、`wake/sleep`、`state` 跨越阈值都写一条 `events(kind, actor, target, delta, ts)`；`timeline()` 读事件表，`history()` 作为单条视角 → M → 风险：事件表会涨（需保留窗口 + `maintain` 里裁剪）；现成的 `_journal()`（`memcore.py:1817`）结构可复用大半，先看它落库在哪。**
借 Memobase 的 `user_events.event_data.profile_delta`：事件里带"这次改动了哪些记忆"，比"哪条记忆被改过"信息量高一个量级（这正是"上次说的那个方案"这类时间性问题需要的原料）。

**【落点】`memcore.py:1414 search()` 尾部留 `rerank` 钩子（默认恒等函数，`cfg["rank"]["rerank"]="none"`）→ S → 风险：为不存在的需求做抽象；只留签名不实现，等真需要再加 cross-encoder。**
借 MemOS 的 `HTTPBGEReranker` 位置感：排序管线里"初排 → 重排"是两个阶段，现在 memcore 把它们压成一次加权和。留钩子的价值是将来换排序器不用改 `search()` 的调用方。

**【落点】`memcore.py:532 backup()` / `556 _rotate()` / `1836 export_markdown()` → 增加"按 kind 选择性导出"（`--kinds fact,decision`），导出文件头写 `SCHEMA_VERSION` + 条数 + 源库 hash，导入前校验 → S → 风险：`export_markdown` 是给人和 Obsidian 看的镜像，别把机器校验字段塞进正文段落，放 front-matter。**
借 `GeneralMemCube.dump(memory_types=[...])` 与 `model_schema` 校验：cube 的核心不是"四类记忆"，而是"**可搬运 + 版本可验**"。

**【落点】`tools/mem-guard.sh` → 测试失败时除了打雷区，顺便 `mem states --json` 快照一次（哪条记忆此刻是模糊/沉睡态），把状态作为失败上下文一起留档 → S → 风险：guard 在 CI/钩子里跑，多一次 SQLite 读要确认不阻塞；输出要能关（环境变量）。**
这一条不是抄谁，是三个项目共同的隐含前提：**记忆系统的失败往往不是"没记住"，而是"记住了但当时处于错误状态"**。留证据比加功能便宜。

### 三条代价必须写在前面

1. **任何"必须调 LLM 才能用"的机制在 memcore 上只能是可选增强**：`consolidate(llm=True)`、槽位抽取、re_summary 都属于此类。降级路径统一为"离线时跳过 + 出报告 + 不落破坏性变更"（现状 `consolidate` 的 `dry_run=True` 默认与 `auto_retire=False` 已经是对的姿势，别改）。
2. **不要引入向量服务**：三个项目里 Memobase/MemOS 的检索质量很大一部分来自 embedding 服务（`embedding_dim` 与库 schema 绑死，见 `database.py` 的 `check_legal_embedding_dim`）。memcore 的 512 维本地 hash 向量在"同义改写"上必然更弱，靠 `preflight` 的 tag 精确命中 + `learn` 的结构化字段补偿，而不是靠换 embedding。
3. **控制面（队列/锁/多租户/cube 调度）在单机单 agent 上是净成本**：只抄"状态 + 可重放"这一层（一张表），不抄 Redis 队列、RabbitMQ、三件套 monitor。
