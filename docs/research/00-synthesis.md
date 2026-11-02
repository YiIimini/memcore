# 记忆系统横向调研 · 2026-11

> 目的：给 memcore 找**可落地的机制**，不是列竞品清单。
> 方法：9 个项目，每个都读**源码/官方文档/论文原文**（不看二手博客做结论），
> 再对照 memcore 现状写「落点：文件·函数 → 改法 → 工作量 → 风险」。
> 分报告在 `01~04`，本文只做汇总、排序和取舍。

| 分报告 | 覆盖 |
|---|---|
| [01-mem0-letta.md](01-mem0-letta.md) | Mem0、Letta（原 MemGPT） |
| [02-zep-graphiti.md](02-zep-graphiti.md) | Zep/Graphiti、Cognee |
| [03-amem-hipporag.md](03-amem-hipporag.md) | A-MEM、HippoRAG v1/v2 |
| [04-langmem-memobase-memos.md](04-langmem-memobase-memos.md) | LangMem、Memobase、MemOS |

---

## 0. 一句话结论

**能抄的机制不在"更聪明的抽取"，而在四处工程细节：实体通道、时间轴、写入契约、后台整理的状态机。**
反过来，业界最响的那套"LLM 判断 ADD/UPDATE/DELETE"已经被 Mem0 自己砍掉了 —— 追它是追一个已经过时的方向。

---

## 1. 横向对照

| 项目 | 依赖 | 记忆单元 | 写入时的"对账" | 时间处理 | 检索 | 最大代价 |
|---|---|---|---|---|---|---|
| **Mem0** | LLM+向量库 | 抽取后的事实句 | 2024 版四算子；**2026 版退成 ADD-only** | 时间当排序信号 | semantic+BM25+entity 三路 sigmoid 融合 | 必须 LLM 在线 |
| **Letta** | LLM+Postgres/SQLite | memory block（可编辑文本块）+ archival + recall | 后台 agent 独占写权限 | `last_processed_message_id` 水位线 | 块常驻上下文，其余按需检索 | 双 agent 架构重 |
| **Zep/Graphiti** | Neo4j 等 + LLM | episode → entity → fact 边 | **双时态边失效**（invalid_at 锚新事实） | `valid_at`/`invalid_at`/`created_at` 三轴 | 语义+BM25+图遍历混合 | 图数据库重；LLM 抽图 |
| **Cognee** | 图数据库 + LLM | DataPoint 三元组 | 幂等 upsert，矛盾事实**共存** | 时间只做排序前置（不硬筛） | 图+向量；TEMPORAL 检索 | 无 validity，靠版本号 |
| **A-MEM** | 向量库 + LLM | Zettelkasten 原子笔记（content/keywords/tags/context/links） | 建链时**反向更新邻居的 context** | 时间戳只做元数据 | 向量粗筛 + 链式扩散 | 开源实现有真 bug（见 §4） |
| **HippoRAG** | 向量库 + LLM(OpenIE) | KG 三元组 + passage 节点 | 实体去重 + synonymy 边 | 无 | **PPR 多跳**（实体→passage） | LLM 建图贵；中文同义边会失效 |
| **LangMem** | LangGraph 全家桶 | semantic/episodic/procedural 三类 | LLM 决定改哪条（`MemoryManager`） | 强度因子 | `sim × importance × strength` **乘子** | hot path 强依赖 LLM |
| **Memobase** | FastAPI+PG+Redis | profile 槽位 + 事件 + gist | **槽位覆盖**（topic/sub_topic） | 事件时间是一等公民 | 向量 + 时间窗 | 服务化重 |
| **MemOS** | 自研调度 | MemCube（明文/激活/参数化） | 生命周期调度 | 有 | 分层调度 | 论文概念多，可落地少 |

---

## 2. 值得抄的六条（按"证据强度 × 收益/成本"排序）

### ① 实体是独立检索通道，不是 0.05 的加分项 —— **P0**
**来源**：Mem0 2026 算法（entity linking 与 semantic/BM25 **并排打分再融合**）、A-MEM（keywords/tags 写时生成）、HippoRAG（实体做 PPR 种子，并用 `1/df` 抑制 hub）。

**memcore 现状（硬事实）**：`entities` 表**建了、查了、删了，全仓没有一条 INSERT**（生产库 `count(*)=0`）；
而 `search()` 里 `s += 0.05 * (tag + ent)` 一直在给这个空信号留权重。

**我做的原型验证**：给 493 条记忆灌了 4699 个真实别名实体（标题 2~4 元 + 标签 + 拉丁词），
R@1/R@3 仍是 **10/12、11/12，MRR 0.881 → 0.881**；把注入强度拉到 4.0 也只到 0.896。
原因不是数据不够，是**系数被硬编码 0.05**（`memcore.py:1490`）—— 任何信号经它加成都会被稀释成噪声。

**结论**：要抄就得抄全 —— 写入端补实体抽取 + 检索端给独立通道权重（可配置）+ 融合时按 `1/log(1+df)` 抑制高频实体。
半抄（只补写入、仍用 0.05）等于白干，这点我实测过。

### ② 时间要分两根轴：事实何时成立 / 系统何时知道 —— **P1**
**来源**：Graphiti（`valid_at`/`invalid_at` + `expired_at`，旧边失效锚定**新事实的 valid_at**，不删行）；Cognee 反证（没有 validity 也能用，但只能把时间当排序信号）。

**memcore 现状**：`memories` 有 `valid_from/valid_to`，但 `valid_from` 默认取 `ts or now()`，CLI/MCP 都没有事件时间入口 → **双时态实为单时态**；`links` 完全没有时间列；`retire()` 把"语义终点"和"系统发现时刻"压成一列。

**落地（有依赖顺序）**：`add(event_at=)`（不传时回落 `ts`，**绝不能写 0**，否则 `as_of` 无下界语义会把该条放进任何时点查询）
→ `links` 加 `valid_at/invalid_at`（回填 `created_at`）→ 同 pair 失效（`rel` 白名单，`related` 边只降权不失效）
→ `graph()/as_of` 支持裁边。**`links` 的 `PRIMARY KEY(src,dst,rel)` 决定了"同一对实体的关系演变"目前存不下**，这条不先定，前两步只能做成半成品。

### ③ 写入契约：改记忆要"定点且唯一"，不是整字段覆盖 —— **P0**
**来源**：Letta `memory_replace` 要求 `old_string` 逐字且在块内**唯一**，0 次或 >1 次都抛错并列出歧义行号。

**memcore 现状**：`update(mid, body=...)` 是整字段覆盖，正是这条契约要防的写法（幻觉原文、改错位置都会静默成功）。
**落地**：`update()` 增加 `old/new` 模式（`replace_in_body(mid, old, new)`），找不到或不唯一就报错并返回候选行；`learn()` 生成的坑条目天然适合用它定点补"修法"。

### ④ 后台整理要有状态机和进度水位 —— **P1**
**来源**：Memobase buffer zones（idle→processing→done/failed，可重放，per-user 锁）；Letta sleep-time（轮次取模 + `last_processed_message_id`，且**前台 agent 一个写记忆的工具都没有**）。

**memcore 现状**：`maintain()` 一次跑 8 个动作，崩了不知道断在哪、只能整段重跑；`consolidate()` 每次 `updated_at DESC LIMIT 400` 重扫同一批旧条目。
**落地**：SQLite 一张 `maintenance_tasks(state, attempts, last_error)` + `meta.consolidate_at` 水位线。不需要 Redis，只抄状态层。

### ⑤ 检索应有显式 fallback，但只在"该有答案"的通道上加 —— **P1**
**来源**：HippoRAG（`top_k_facts==0 → dense` 兜底写进主流程）；A-MEM 反例（没有 fallback，链式扩展被 `[:k]` 切掉）。

**memcore 现状**：`evidence` 门不满足直接 `continue`，可能静默返回空列表。
**落地**：`recall/brief` 加 `fallback:"recency"`（按 pinned+salience+recency 取几条并标注）；
**`preflight` 绝不能加** —— "没坑就是没坑"是它的语义，兜底会毁掉。

### ⑥ 归一化用有界函数，别用 min-max —— **P0（小改动，收益确定）**
**来源**：Mem0 `scoring.py` 用 sigmoid（按 query 词数选 `(m,k)`）；memcore 用 min-max，**候选集只有 1 条时 `lexn` 恒等于 1.0**（自己的代码注释里踩过这个坑却只是绕过）。
**落地**：`sigmoid` 归一化 + `evidence` 判据同步改成 `lexn > 0.5`（不同步改会让所有候选都算"有词法证据"）。风险是排序分布整体变化，必须复跑 `tests_eval.py`。

---

## 3. 明确不要抄的

| 不抄 | 理由 |
|---|---|
| LLM 决策的 ADD/UPDATE/DELETE/NOOP | Mem0 自己在 2026 版退成 ADD-only；memcore 不能要求 LLM 在线 |
| 自动删除/自动合并（`enable_deletes`、`auto_retire`） | memcore 已经踩过：60 条"重复"误删 56 条；两边作者都把它默认关掉 |
| 图数据库 / 向量数据库 / Redis 队列 | 单机零依赖是 memcore 的立身之本；机制抄过来，栈不抄 |
| HippoRAG 的 `re.sub('[^A-Za-z0-9]','')` 同义边门槛 | **纯中文实体永远拿不到同义边**，中文库照抄直接失效 |
| A-MEM 的动态演化**代码** | 实现是坏的（见 §4），只抄意图 |
| MemOS 的 MemCube 分层 | 论文级概念，落到本地库只剩"升级方向"，投入产出比低 |
| 社区摘要（Graphiti label propagation） | 收益最不确定，且拼接标题会被自己的 `_fuzzy_dedupe` 判重、污染 brief |

---

## 4. 三个"反直觉"发现（都影响我们的方向判断）

1. **Mem0 的论文卖点已被自己作废。** 论文主打四算子，现行 `main` 是 single-pass ADD-only，`DEFAULT_UPDATE_MEMORY_PROMPT` 成了死代码。
   → 对 memcore 的结论是**反的**：`consolidate()` 不该做大做强，该降级成"只读体检报告"。
2. **A-MEM 的动态演化在开源实现里是坏的。** `update_neighbor` 把"第 i 个最相似邻居"当成"第 i 条插入的记忆"写回（`enumerate` 位次 vs 列表下标），会改写毫不相干的旧记忆；且不回写向量库，`evolution_history` 全仓无 append。
   → 抄意图（写时反向更新邻居的 `context`），不抄代码。
3. **明星项目的图通道也经常是空的。** HippoRAG 把 fallback 写进主流程，A-MEM 没有，memcore 站在坏的那边（`evidence` 门直接 `continue`）。

---

## 5. 我这轮做的实测（含失败，失败也算数据）

| 实验 | 做法 | 结果 | 结论 |
|---|---|---|---|
| **实体通道** | 493 条记忆灌 4699 个真实别名；查询侧加 CJK 2-gram 命中 | R@1/R@3 不变；把信号抬到 4 倍，MRR 0.881→0.896 | 0.05 的硬编码系数把信号吃掉了；**要么做通道，要么别做** |
| **乘子式排序** | 加 40 条"刚写入但无关"的干扰项，对比 additive vs multiplicative | MRR 0.881 vs 0.880 | 12 题集已饱和、干扰项太弱，**无法判别**；保留 `weights.mode` 开关，默认 additive，等更大评测集 |
| **雷区隔离** | 普通召回剔除 `pitfall` 条目，仅当查询含"坑/雷区/教训"时带出 | 12 题分数不变，但"直播宠物方案"那条的 top1 不再被坑条目占据 | 元记忆不该跟业务记忆抢席位，**已默认生效** |

> 方法论教训：`tests_eval.py` 的 12 条已经**饱和到没有区分度**（10/12、11/12 卡住）。
> 任何排序改动都测不出差异。**建覆盖更广的评测集应该排在任何排序优化之前**（建议：真实库 + 人工标注 40~60 条，
> 含时间错位、近义改写、跨语言别名三类硬样本）。

---

## 6. 路线图（建议顺序）

| 批次 | 内容 | 为什么这个顺序 |
|---|---|---|
| **P0** | ① 实体通道（写入+通道权重+hub 抑制）、⑥ sigmoid 归一化、③ 定点写入 `replace_in_body` | 三件都小、都可独立验证；① 是后面 PPR 的地基 |
| **P1** | ② 时间轴三件套（`event_at` → `links` 两列 → 同 pair 失效）、④ 整理状态机与水位线、⑤ recall fallback | ②有内部依赖，必须一起上；④⑤ 都是"防止静默失败" |
| **P2** | PPR 重排（借 ①）、A-MEM 的 `ctx`/`ctx_nb`、`history()` 支持时点、社区聚类 | 依赖前两批的数据质量；收益不确定，最后做 |

---

## 7. 来源

各分报告末尾都有 3~8 条带 URL 的原始来源（raw 源码路径 + 论文 + 官方 docs）。
汇总入口：
- Mem0：https://github.com/mem0ai/mem0 （arXiv:2504.19413）
- Letta：https://github.com/letta-ai/letta （arXiv:2310.08560）
- Graphiti / Zep：https://github.com/getzep/graphiti （arXiv:2501.13956）
- Cognee：https://github.com/topoteretes/cognee
- A-MEM：https://github.com/agiresearch/A-mem （arXiv:2502.12110）
- HippoRAG：https://github.com/OSU-NLP-Group/HippoRAG （arXiv:2405.14831 / 2502.14802）
- LangMem：https://github.com/langchain-ai/langmem
- Memobase：https://github.com/memodb-io/memobase
- MemOS：https://github.com/MemTensor/MemOS （arXiv:2507.03724）
