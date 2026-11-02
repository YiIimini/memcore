# 竞品拆解 02：Zep / Graphiti 与 Cognee

> 目标：给 memcore 的 `links` 图 + 时间窗找可借鉴机制。
> 口径：以源码为准（2026-11-02 取样 `main`），论文只用来定位机制语义。二手文章不采信。
> 三个关注点：双时态 bi-temporal、边失效 edge invalidation、社区/聚类。

---

## Zep / Graphiti

### 定位 / 规模 / 许可证 / 时间线

- **Graphiti**：Apache-2.0（仓库 `LICENSE`），Python 框架。图库、LLM、embedder 全由使用者提供，它只负责"把 episode 变成时序知识图谱"。
- **Zep**：商业服务。Graphiti 是它的图构建内核，Konig 是存储服务（README 自述分工）。
- **论文**：arXiv:2501.13956 v1，2025-01-20 提交，12 页。摘要口径：DMR 94.8% vs MemGPT 93.4%；LongMemEval 最多 +18.5% 准确率、延迟 -90%。均为自建 benchmark，只作定位参考，不作论据。
- **规模**（取样分支）：`graphiti.py` 2251 行、`node_operations.py` 1295 行、`edge_operations.py` 1153 行、`nodes.py` 1166 行、`edges.py` 1051 行。是**框架**体量，不是库体量。

### 图模型

论文 §2.1 定义三层子图：episode → semantic entity → community。

**节点**

| 节点 | 关键字段 | 位置 |
|---|---|---|
| `EpisodicNode` | `source` / `source_description` / `content` / `valid_at`（原文创建时间）/ `entity_edges` / `episode_metadata` | `graphiti_core/nodes.py:318-331` |
| `EntityNode` | `name_embedding` / `summary`（"regional summary of surrounding edges"）/ `attributes` | `graphiti_core/nodes.py:499-503` |
| `CommunityNode` | `name_embedding` / `summary`（"region summary of member nodes"） | `graphiti_core/nodes.py:727-729` |
| `SagaNode` | `summary` / `first_episode_uuid` / `last_episode_uuid` / `last_summarized_at` | `graphiti_core/nodes.py:907-917` |

**边**

| 边 | 作用 | 位置 |
|---|---|---|
| `EpisodicEdge`（`MENTIONS`） | episode → entity | `graphiti_core/edges.py:143` |
| `EntityEdge` | 事实本体 | `graphiti_core/edges.py:263-289` |
| `CommunityEdge` | community → entity member | `graphiti_core/edges.py:580` |
| `HasEpisodeEdge` / `NextEpisodeEdge` | saga 链（episode 之间的先后） | `graphiti_core/edges.py:694` / `827` |

`EntityEdge` 的字段就是整套设计的核心：

```python
name              # 关系名
fact              # 自然语言事实（被 embedding，也是回忆时给 LLM 的文本）
fact_embedding
episodes: list[str]   # 产生这条事实的 episode —— 溯源反向索引
valid_at          # 事实开始为真的时间（事件时间）
invalid_at        # 事实不再为真的时间（事件时间）
expired_at        # 系统何时发现它不再为真（摄入时间）
reference_time    # 产生这条边的 episode 的参考时刻
```

一个结构性取舍先摆明：**Graphiti 把时间放在边上，Cognee 把时间做成节点**（下节）。

### 写入路径

`add_episode` → 内部 `add_episode_endpoint`（`graphiti_core/graphiti.py:1465-1520`）：

1. `extract_nodes` —— 带前 n=4 条 episode 做上下文，再加一轮 reflexion 式补抽（论文 §2.2.1）
2. `resolve_extracted_nodes` —— 实体解析：name 的 1024 维 embedding 做 cosine 取候选 + 对 name/summary 做全文检索取候选 → 交给 LLM 判重；余弦阈值 `NODE_DEDUP_COSINE_MIN_SCORE = 0.6`（`node_operations.py:68`），判为重复则重写 name 与 summary
3. `_extract_and_resolve_edges` —— 抽边 → 边去重 → 边冲突/失效，返回三元组 `(resolved_edges, invalidated_edges, new_edges)`
4. `extract_attributes_from_nodes` —— **只拿 `new_edges`** 生成实体 summary，避免把已存在的事实重复写进摘要
5. 存 episode + episodic edges（+ saga 链），落库 `invalidated_edges`
6. `update_communities` 默认 **False**（`graphiti.py:1345`）；社区重建是显式动作（`build_communities`，`graphiti.py:1867`）

三个关键约束：

- **边去重只在同一对实体之间做**（论文 §2.2.2）。候选边经 `_pair_search_filter` / `_same_pair_edges` 限定到同一 `(src, dst)`（`edge_operations.py:399-423`）。既避免"句子很像但主语不同"的误合并，也把比对从全库降到 pair 级。
- **时间抽取有两条路**（`_extract_edge_timestamps`，`edge_operations.py:786-833`）：抽取 prompt 直接给出 `valid_at`/`invalid_at`；缺失时，用 `reference_time = edge.reference_time or episode.valid_at` 单独跑一次小模型。**没有 reference_time 就跳过**——宁可无时间，不猜时间。
- 两条业务规则写在 docstring 里，直接可抄（`edges.py:274-285`）：① 未来的计划/承诺，`valid_at` 取 `reference_time`（说话时刻），不取未来发生日；② 软性截止日期**不算** `invalid_at`。

### 边失效（edge invalidation）

`_apply_temporal_invalidation`（`edge_operations.py:715-746`）+ `resolve_edge_contradictions`（`748-784`）：

```
候选 = 同一 (src, dst) 的既有边
A. 新边自带 invalid_at（原文说了"到某时为止"） → 新边 expired_at = now
B. 候选中存在 valid_at 晚于新边的边（事件时间倒序到达） → 新边自己作废
C. 候选 valid_at < 新边 valid_at → 候选.invalid_at = 新边.valid_at
                                 候选.expired_at = now
                                 不删除
重叠豁免：旧边 invalid_at <= 新边 valid_at，或 新边 invalid_at <= 旧边 valid_at
         → 两条各活各的（时间上不重叠，不构成矛盾）
```

四个可迁移的点：

1. **失效值是"新事实的 `valid_at`"，不是 `now()`**。语义是"这条事实从那一刻起不再是事实"。写 `now()` 就把"事实为假的时间"和"我知道的时间"压进同一列，事后无法重放。
2. **不删行**。失效只是关窗，任何时点的查询都还能取回它。
3. **两个时间戳分开存**：`invalid_at`（语义终点，∈ T）与 `expired_at`（系统发现时刻，∈ T′）。论文 §2.2.3 的四时间戳就是这两对。
4. **要处理倒序到达**（规则 B）。事件时间可以晚于摄入顺序，不能假设"后到的就是更新的"。

### 检索路径

- 四类对象各自可配：`EdgeSearchConfig` / `NodeSearchConfig` / `EpisodeSearchConfig` / `CommunitySearchConfig`，每类能叠多个 search method 加**一个** reranker（`search_config.py:32-112`）。
- 方法三种（论文 §3.1）：cosine、BM25、**BFS**。BFS 显式接受"种子节点"，常见用法是拿最近几条 episode 当种子捞邻域。
- reranker 五种（`search_config.py:53-77`）：`rrf` / `mmr` / `node_distance`（离指定质心节点的图距离）/ `episode_mentions`（对话中被提及的频次）/ `cross_encoder`（最贵）。
- 预置 17 条 recipe（`search_config_recipes.py`）。默认组合 `COMBINED_HYBRID_SEARCH_RRF`（`34-54`）：edge / node / community 各跑 bm25 + cosine，episode 只跑 bm25，四路 RRF 合并。
- 返回 **3-tuple**（edges, nodes, communities）。上下文模板里带时间区间：`FACT (Date range: from - to)`（论文 §3 上下文构造）。**区间直接进 prompt，不是让模型自己算。**

### 时间处理（双时态怎么存、怎么按"当时"查）

存：

- T 轴（事件）：`valid_at` / `invalid_at`，挂在 `EntityEdge` 上
- T′ 轴（摄入）：`created_at`（边和节点都有）、`expired_at`
- episode 自带 `valid_at`（原文创建时间），作为该批事实 `reference_time` 的兜底

查"某时刻"：

- **默认不过滤**。`search` 把 `valid_at` / `invalid_at` / `expired_at` 当普通字段返回（`search_utils.py:390-404`），不做时间裁剪。
- 结构化时点查询靠 `SearchFilters`（`search_filters.py:55-73`）：`valid_at` / `invalid_at` / `created_at` / `expired_at` 四个字段，类型都是 `list[list[DateFilter]]` —— **外层 OR、内层 AND** 的区间表达式，配 `ComparisonOperator` 的 gt/lt/gte/lte。
- 所以 Zep 的"当时查询"是**调用方拼一个时间滤镜 DSL**，不是一个 `as_of` 标量。要表达"该时刻有效"得自己写：

```
valid_at <= t  AND  (invalid_at IS NULL OR invalid_at > t)
```

这个恒等式是**语义**，框架没给快捷方式 —— 它是 memcore 可以直接用的判据。

### 关键机制 → 失败模式 → memcore 能否用

| # | 机制 | 解决的失败模式 | memcore 能否用 |
|---|---|---|---|
| 1 | **边级 validity window，失效不删数据** | 事实更新时只有"留着错的"或"删了丢历史"两个坏选项 | ✅ `memories` 有 `valid_from/valid_to`，`links` 没有（`memcore.py:370-371`）。"某时刻 A 和 B 什么关系"现在查不出来 → 加两列即可 |
| 2 | **失效锚定新事实的 valid_at；T 与 T′ 分两列** | 用布尔 `status='superseded'` 表达失效后，答不出"它何时不再是事实"，也分不清"事实变了"和"我刚知道" | ✅ `retire()` 写的是 `valid_to = at or now()`（`memcore.py:829-832`），用的是**摄入时间**，语义 lossy。改成从新事实的 `valid_from` 推导 |
| 3 | **同 pair 约束的冲突检测** | 全库语义比对又贵又容易误判 | ✅ **最适配的一条**：`links` 的 PK 就是 `(src,dst,rel)`（`memcore.py:368`），天然是 pair 命名空间，零改造 |
| 4 | **episode 无损层 + `episodes[]` 反向索引** | 实体合并、摘要重写后原始输入丢失，无法复算"这条结论哪来的" | ⚠️ 有 `body` + `journal.jsonl` + `source/source_ref`，但没有"一条记忆由哪几次摄入合成"的关系。可用 journal 补边 |
| 5 | **社区层：label propagation + 动态扩展 + 摘要** | 只能命中条目，拿不到"这一片在讲什么" | ⚠️ 无社区层。可用但要克制（见落点 8，LLM 成本与 memcore 当前形态冲突） |

社区细节（论文 §2.3，`community_operations.py:96-140` / `256-297`）值得单独记三条：

- 用 **label propagation 而非 Leiden**，理由不是质量而是**动态扩展简单**：新节点加入时取邻居多数社区，只跑一步递归。
- 代价被明说："communities gradually diverge from those that would be generated by a complete label propagation run"，所以**仍需周期性全量 refresh**。
- summary 是 map-reduce 式迭代；社区名（关键词+主题）**单独 embedding** 供 cosine 检索 —— 检索时命中的是"名字"，返回的是"摘要"。

### 不要抄的部分

- **Cypher / Neo4j 依赖**。连"自带图库"的抽象层也是 Cypher 语义。memcore 是单文件 sqlite，换库等于重写。
- **LLM-in-the-loop 的冲突判定**。每条新边一次 LLM 比对。memcore 的 `brief`/`preflight` 是毫秒级、离线、零依赖，引入后延迟不可接受。
- **完整三层 + Pydantic 类型系统**。`nodes.py` 1166 行 + `edges.py` 1051 行，对 2k 条量级的个人记忆库是过度设计。
- **17 条 search recipe 的组合面**。memcore 的加权线性打分（`memcore.py:1466-1497`）已经够用，先加维度，别加配方。
- **`group_id` 多租户 / `edge_types` 自定义 ontology / saga**。memcore 是单用户单库。

### 来源

- repo: <https://github.com/getzep/graphiti>（`main`，2026-11-02 取样）
- 论文: <https://arxiv.org/abs/2501.13956> —— §2.1 三层子图 / §2.2.1 实体解析 / §2.2.2 边去重 / §2.2.3 双时态与失效 / §2.3 社区 / §3.1 检索 / §3.2 rerank
- 代码：`graphiti_core/nodes.py`、`edges.py`、`graphiti.py`、`utils/maintenance/{edge_operations,node_operations,community_operations}.py`、`search/{search_config,search_config_recipes,search_filters,search_utils}.py`

---

## Cognee

### 定位 / 规模 / 许可证 / 时间线

- Apache-2.0（`LICENSE`），Python 3.10–3.14。可完全本地跑：无 LLM key 时用 GLiNER 抽取 + 本地 embedding（README "Run locally without an LLM"）。
- **API 表面已经换代**：现在是 `remember` / `recall` / `improve` / `forget`（`cognee/__init__.py`）。`cognify` 还在（`cognify.py:137`），`memify` 已标 Deprecated —— "Removed in the next release"（`memify.py:161-163`），原语并入 `improve`。所以任务里说的"cognify/memify 流水线"要按**当前代码的两段 cognify + improve（旧 memify）增强**来读。
- 论文：arXiv:2505.24478（*Optimizing the Interface Between Knowledge Graphs and LLMs for Complex Reasoning*, 2025）。
- 规模：`remember.py` 2315 行、`cognify.py` 803 行、`search.py` 823 行、`get_search_type_retriever_instance.py` 483 行。

### 图模型

- 一切是 `DataPoint`（`infrastructure/engine/models/DataPoint.py:28-96`）。与时间/版本相关的字段：`created_at` / `updated_at`（毫秒，**都是摄入时间**）、`version`、`feedback_weight`、`importance_weight`、`ontology_valid` / `ontology_uri`、`source_pipeline` / `source_task` / `source_user` / `source_content_hash`。
- **身份是派生的**：声明 `metadata.identity_fields` 后，id = `uuid5(identity_fields)`。docstring 说得很直白：没有 identity_fields 的节点拿随机 id，"never deduplicates/merges across runs or mentions"。`Entity` 声明 `identity_fields: ["name"]`（`modules/engine/models/Entity.py:5-23`）。
- **边是隐式的**：一个字段如果装的是另一个 `DataPoint`（或列表），写图时就变成以该字段命名的边（`DataPoint.py:38-41`）。边可带 `edge_text` 与权重。
- **边没有任何 validity 字段**。这是与 Graphiti 最大的结构差异。

### 写入路径（cognify / 两段流水线）

`cognify` 文档字符串自己写了顺序（`cognify.py:188-197`）：

1. `indexing_pipeline`：切 chunk + 向量索引，**零 LLM 调用** —— 数据先可搜，再慢慢抽取（`early_chunk_indexing` 默认开）
2. `cognify_pipeline`：`classify_documents` → `extract_graph_and_summarize` → 每个 chunk 的图经 `integrate_chunk_graphs` 合并 → `add_data_points`

合并语义（`integrate_chunk_graphs`，`tasks/graph/extract_graph_from_data.py:115-224`）：

- `_remove_duplicate_extracted_nodes_by_id`：同 id 只留第一个，注释诚实："This is a lossy strategy"（`extract.py:44-63`）
- `construct_data_points_and_edges` 按 `(src_identity, rel, dst_identity)` 归并 → `find_existing_edge_identities` 把**已存在的边直接跳过、不再挂一遍**（`extract.py:199-215`）
- 结论：**幂等 upsert，没有"新事实让旧事实失效"这一步**。两条互相矛盾的事实会同时留在图上。

时间：抽取前给 chunk 附 `document_temporal_hints`（`modules/engine/utils/temporal_hints.py:1-30`）。它的角色是**抽取辅助**，不是事实有效期：

- 把 "of 27 April"、"later that spring" 这类缺年份的表达，按"文档内此前最后一个有年份的日期"补全成绝对日期，作为 normalization hint 塞进 prompt；**chunk 原文不改**。
- 两个防误报细节值得记：月名大小写敏感 + 整词匹配（小写 "may" 是动词，前缀匹配 "dec" 会撞 "decade"）；只解析绝对时间（`REQUIRE_PARTS: ["year"]`），不让 "four weeks later" 相对今天解析。

### 时间模型：Timestamp 是节点，不是属性

`Timestamp`（`modules/engine/models/Timestamp.py:12-52`）：

- `timestamp_str`（规范串：`YYYY` / `YYYY-MM` / `YYYY-MM-DD` / `YYYY-MM-DD HH:MM:SS` / `start/end`）、`precision`（year/month/day/second/**span**）
- `time_at`（ms，区间下界；未声明的部分填最早值）、`time_until`（**排他**上界，默认 `time_at+1000`）
- `year/month/day/hour/minute/second` 展开成独立字段 —— 目的是**让图库按字段范围过滤**
- `identity_fields: ["timestamp_str"]` → 同一时刻跨文档合并成同一个节点
- 事实通过 `*_at` 边挂到 Timestamp 上

重叠判据写在 docstring 里，这就是整套时间模型的核心：

```
time_at < window_end  AND  time_until > window_start
```

注意 `time_until` 的 0 与 None 之争写得很细（"`0` is a real bound (1970-01-01…) and must survive as-is"）—— 这类边界在 memcore 里同样的坑存在（`valid_to=0` 表示"永不到期"，不能同时表示"1970"）。

### 检索路径

- `SearchType` 20 种（`modules/search/types/SearchType.py:5-27`），映射表在 `get_search_type_retriever_instance.py`。每个类型一个 retriever 类，统一三段契约：`get_retrieved_objects` → `get_context_from_objects` → `get_completion_from_context`（`modules/retrieval/README.md`）。
- 默认 `HYBRID_COMPLETION`：chunk lane + entity lane（+ 可选 global-context lane）。
- `GRAPH_COMPLETION`：brute-force triplet search + 邻域扩展（`triplet_distance_penalty=6.5`）。
- **`TEMPORAL` = `TemporalHybridRetriever`**（`get_search_type_retriever_instance.py:365`）。机制值得逐句读：
  1. 先跑一个**超额**的 hybrid 候选集（4× top_k），"so the temporal filter has something left after it drops off-window chunks"
  2. LLM 从问题里抽一个时间窗（`QUERY_INTERVAL_PROMPT`，`temporal_hybrid/matching.py:15` 起）—— 规则细到"start 含、end 排他""Before X 的 start 为 null""Until X 含 X"，并要求"No explicit time, relative time, or disjoint windows: both null"
  3. adapter 用 `get_temporal_anchors` 找落在窗内的 Timestamp 节点
  4. 把挂在这些 Timestamp 上的 chunk/entity **前置**（`HybridCandidates.prioritize`），再用与普通 hybrid 相同的 `finalize` 截断
  5. **一条都不丢**；问题里没有时间窗就退化成普通 hybrid
- 一句话对比：**时间在 cognee 是排序信号，在 Graphiti 是过滤 DSL。**

### 时间处理（双时态怎么存、怎么按"当时"查）＋ 与 Graphiti 逐条对照

| 维度 | Graphiti | Cognee |
|---|---|---|
| 事件时间 | 边属性 `valid_at` / `invalid_at` | `Timestamp` 节点 + `*_at` 边 |
| 摄入时间 | 边/节点 `created_at`、`expired_at` | `DataPoint.created_at` / `updated_at` |
| 事实失效 | 有（规则 A/B/C，不删行） | **无**（幂等 upsert，矛盾事实共存） |
| 变更历史 | 边不删，只关窗 | `version` 自增 + `updated_at` 覆盖 |
| 按"当时"查 | `SearchFilters` 区间 DSL | 时间窗软前置（LLM 抽窗） |
| 社区 / 全局 | `CommunityNode` + label propagation + 摘要 | `GlobalContextSummary`（root + 非 root 两级，向量检索，作 context prelude） |
| 抽取失败兜底 | 无 reference_time 就跳过时间抽取 | 无时间窗就退化普通 hybrid |

Cognee 的 `GlobalContextSummary`（`modules/retrieval/utils/global_context.py:6-33`）：独立向量集合 `GlobalContextSummary_text`，分 root 与子摘要，检索时取 top_k 作为"全局上下文前导"塞进 prompt。**不是** Graphiti 那种"实体聚类 + 每簇 LLM 摘要 + 社区名 embedding"，更像"数据集级摘要索引"——粒度粗，但实现轻。

### 关键机制 → 失败模式 → memcore 能否用

| # | 机制 | 解决的失败模式 | memcore 能否用 |
|---|---|---|---|
| 1 | **身份派生 id（`uuid5(identity_fields)`）** | 同一实体反复入库膨胀 | ⚠️ 只有内容级去重（hash 精确 + 0.965 fuzzy，`memcore.py:668-703`），**没有实体层**。`entities` 是 `(memory_id, name)` 附属表，要升格成主键实体才等价 |
| 2 | **两段流水线（先索引、后抽取）** | 大文档抽取慢，用户等不到任何可搜结果 | ⚠️ `add()` 是单事务（`memcore.py:651-735`），ingest 大批文件会阻塞 brief。可拆，但 memcore 没有队列模型（`autostart.py` 是调度器，不是队列） |
| 3 | **时间窗软前置，而非硬过滤** | 问题里带"上周""2024 年"时，向量检索把时间当噪声；硬过滤又会把边界情形清零 | ✅ **高价值**：memcore 的 `as_of` 是硬过滤（`1322-1325`、`1438-1442`），窗口外直接丢 → 落点 7 |
| 4 | **抽取前的日期补全 hints** | 相对日期（"两周后""那年春天"）抽不出绝对时间 | ⚠️ body 是自由文本，时间只有 `created_at`。可用**规则版**（不引 dateparser） |
| 5 | **`feedback_weight` / `importance_weight`** | "反复被确认的"与"一次性的"无法区分 | ❌ 低价值：已有 `salience` + `use_count` + `reinforce`（`1465-1472`），已等价 |
| 6 | **improve 的 Task 白名单组装**（`memify.py:49-104`：`extract_subgraph` / `extract_user_sessions` / `detect_entity_duplicates` / `merge_entity_duplicates` / `apply_feedback_weights` / `index_data_points`） | 增强步骤可插拔、可按 dataset 组合 | ⚠️ 可借"命名步骤清单"这一层，不必借 Task 对象（memcore 的维护命令是散装函数） |

### 不要抄的部分

- **20 种 SearchType × 20 个 retriever 类**。memcore 的输出是 900 token 的 brief，不是问答编排；多一层 retriever 抽象只会让 `recall()` 更难调。
- **Pydantic `DataPoint` + metadata 契约**。memcore 明确是标准库实现（自写 embed / simhash / fts5），引 pydantic 直接破坏"单文件 + 离线"这个前提。
- **每 chunk 一次 LLM 抽取 + dateparser 依赖**。
- **`pipeline_executor` + subscriber 进度模型**（`run_in_background` / `pipeline_run_id` / `PipelineRunInfo`），对 CLI 过重。
- **root/非 root 两级 `GlobalContextSummary`**。没有跨数据集的 embedding 聚类，摘要质量撑不住，反而污染检索。

### 来源

- repo: <https://github.com/topoteretes/cognee>（`main`，2026-11-02 取样）
- 论文: <https://arxiv.org/abs/2505.24478>
- 代码：`cognee/api/v1/{remember/remember,cognify/cognify,memify/memify}.py`、`cognee/tasks/graph/extract_graph_from_data.py`、`cognee/modules/engine/models/{DataPoint,Entity,Timestamp}.py`、`cognee/modules/engine/utils/temporal_hints.py`、`cognee/modules/retrieval/{README.md,temporal_hybrid/matching.py,temporal_hybrid_retriever.py,utils/global_context.py}`、`cognee/modules/search/{types/SearchType.py,methods/get_search_type_retriever_instance.py}`

---

## 对 memcore 的具体落点

> **行号口径**：以下行号为 2026-11-02T18:45 的工作区快照。`memcore.py` 在本轮拆解期间**正被并发修改**（`git status` 里 `M memcore.py` 不是我改的），行号会漂；以**符号名**为准，取行号用 `grep -n '<符号>' memcore.py`。

先钉住现状（读代码得到的事实，后面每条落点都贴这层）：

- `memories` 有 `valid_from` / `valid_to` / `status`（`memcore.py:358-359`）；`retire()` 关窗 + 写 `supersedes` 边（`826-854`，边写入在 `850-852`）；`search(as_of=)` 做时点过滤（`1322-1325`、`1438-1442`）；CLI `--as-of` 已通到 search / recall（`mem:97`、`mem:106`）
- `links(src,dst,rel,weight,created_at)`（`memcore.py:370-371`）**没有任何时间属性**；`link()` / `_auto_links()` 用 `INSERT OR REPLACE`（`917-919`、`908-910`），同 `(src,dst,rel)` 会**原地覆盖**
- 写入端**没有事件时间入口**：`add()` 的 `valid_from` 默认取 `ts or now()`（`715`），CLI `add` 无 `--valid-from` / `--event-at`（`mem:73-89`），MCP 也没暴露 → 当前"双时态"实际上退化成一个时点
- `as_of` 只作用于 `memories` 行，**边不参与**
- `graph()`（`1754-1773`，取边在 `1764-1766`）纯 BFS，不按时间裁边；`server.py:437` 直接透传 edges

### 【落点 1】给 `links` 加双时态两列

`memcore.py:370-371`（`links` DDL）+ `400-410`（逐列迁移块）→ 增 `valid_at INTEGER NOT NULL DEFAULT 0, invalid_at INTEGER NOT NULL DEFAULT 0`，迁移时把存量行 `valid_at` 回填为 `created_at`（`UPDATE links SET valid_at=created_at WHERE valid_at=0`）。**工作量 S** → **风险**：`as_of` 的既有语义把 0 当"无下界"（`1323`），回填方向或时序搞错会让旧边显得"从 0 时刻就有效"；这两列必须与落点 4 的查询条件**同一版上线**，否则 0 值边的可见性在前后两版不一致。

### 【落点 2】边失效：同 pair、锚定新事实的 `valid_at`

`memcore.py:914-920`（`link()`）+ `888-912`（`_auto_links()`）+ 在 `retire()`（`826-854`）旁新增 `invalidate_pair(src, dst, rel, at)` → 写新边前先查同 `(src,dst)` 的未失效边，若旧边 `valid_at < 新边.valid_at` 则 `invalid_at = 新边.valid_at`（**不是 `now()`**），不删行。**工作量 M** → **风险**：`related` 边是余弦自动生成的（阈值 `min_sim`：`config.json` 里 0.20，代码兜底 0.30，判定在 `904`），把它纳入"冲突"会让相似记忆互相打残。必须按 `rel` 白名单：只对 `supersedes` / `contradicts` / 人工 `link()` 生效，`related` 只降权、永不失效。

### 【落点 3】PK 决定能不能留"同一对实体的关系演变"

`memcore.py:370-371` 的 `PRIMARY KEY(src,dst,rel)` + `INSERT OR REPLACE` → 二选一：

- **(a)** PK 改成 `(src,dst,rel,valid_at)`，同一关系可存多版本，查询取 `invalid_at=0 OR invalid_at>t` 那条
- **(b)** 保持 PK，新事实来就先 `UPDATE` 旧行 `invalid_at` 再 `REPLACE`（只留"当前 + 一条历史"）

**工作量 M** → **风险**：(a) 会打破所有按 `(src,dst,rel)` 写入的 `INSERT OR REPLACE`（`850-852`、`908-910`、`917-919`）与 `history()` 的双向 `LIMIT 1`（`862`、`876`）；(b) 同一 pair 反复变更会不断丢中间版本，与"不删数据"的初衷相悖。建议 (a)，并把 `history()` 改成"取全部再按窗口选"。这条不先定，落点 1 和 2 只能落成 (b) 的半成品。

### 【落点 4】边参与 `as_of`：`graph()` 与候选集

`memcore.py:1754-1773`（`graph()`，取边在 `1764-1766`）+ `1309-1330`（`_select_candidates`）→ `graph(mid, depth, as_of=None)`，查 links 时追加 `AND (valid_at=0 OR valid_at<=?) AND (invalid_at=0 OR invalid_at>?)`；`_select_candidates` 的 `as_of` 分支同步对 links 生效（只影响 `supersedes` 链的可见性，不动召回打分）。**工作量 S** → **风险**：`server.py:437` 的 `/api/graph` 不传 `as_of`，默认行为会从"显示全部边"变成"只显示仍有效"，前端 SVG 布局会跳变。要么 server 侧显式传 `as_of=now()`，要么保留一个 `include_expired` 开关。

### 【落点 5】召回行渲染带上窗口，别只印 `created_at`

`memcore.py:1615-1627`（`_format_hit`，日期在 `1616`）→ 把 `[%s]` 的日期从 `created_at` 换成 `valid_from~valid_to`（`valid_to=0` 只印起点，已失效用 `→` 标终点）；`compact` 模式保持只印起点。**工作量 S** → **风险**：每行多 8–12 字符，900 token 的 brief 会少 1–2 条；必须顺带在 `recall()` 的预算循环（`1579-1613`）里实测条数，否则等于悄悄缩预算。

### 【落点 6】`history()` 支持"回到某时点的那一版"

`memcore.py:856-886`（`history()`）→ 加 `as_of` 参数：沿 `supersedes` 链取**全部**候选（现在是 `LIMIT 1`，`862`/`876`），用 `valid_from`/`valid_to` 窗口筛出覆盖该时点的那条。**工作量 S** → **风险**：现有链是单亲单向遍历，而 `retire(by=...)`（`849-852`）允许多条新记忆分别取代同一条旧记忆 —— 一旦分叉，`LIMIT 1` 已经在丢分支。加 `as_of` 会把这个既有问题暴露成"查不到"，需要同时定义分叉语义（取最新 or 全返回）。

### 【落点 7】软时间前置：`as_of` 不只会丢，还会加权（借 cognee TEMPORAL）

`memcore.py:1473-1500`（打分）→ 增加一项 `w.get("asof", 0.0) * in_window(r, as_of)`，窗口覆盖该时点给 +0.05。落点是**第三段附加项那一层**（`1489-1494`，与 `tag`/`project`/`intent_file` 加分同层，且在 `pit_hint` 早退 `1487-1488` 之后），不是第一段 `sim_part`：

- 乘性模式（`1477-1481`）会把 `sim_part` 整体乘掉，加在 `sim_part`（`1473-1476`）里等于被乘没
- 也必须早于 `s *= self.sleep_scale(state)`（`1533`）与 `min_score` 判断（`1534`）

**工作量 S** → **风险**：与沉睡阈值（`0.30` / `0.55`，`1529-1532`）交互 —— 被前置的"窗内但无关"记忆会把真相关的挤出 24 条候选集。建议只在显式传 `as_of` 时启用，`DEFAULT_CONFIG["weights"]`（`65-77`）里留可关的键（注意配置有 `version` 门禁，`420-427`：`stale` 时会丢掉旧文件里的 `weights`，改 weights 必须抬 `version`，否则用户本地配置会把新键吃掉）。

### 【落点 8】社区摘要：先做无 LLM 的标签传播，不做摘要质量

`memcore.py:1754` + 新增 `cluster_summaries()` → 用现成 `entities` 表 `(memory_id, name)` 建 memory-entity 二分图，跑纯 stdlib 的 label propagation（迭代取邻居多数，收敛上限 10 轮），每个簇产出**一条** `kind='insight'` 记忆，`tags` 带 `cluster:<代表实体>`；`search()` 里给这个 kind 单独调低权重。**工作量 L** → **风险**：拼接标题生成的"摘要"会被 `_fuzzy_duplicate`（阈值 `dedupe_fuzzy` 0.965，`1787-1788`）判成重复，或被词面命中顶到前排，直接污染 brief。必须：(a) 单独 kind 且默认**不参与** `_auto_links` 的聚类；(b) 在 `KIND_DEFAULTS` 里给短 halflife。**这条收益最不确定，排最后做。**

### 【落点 9】给摄入端开一个事件时间入口（双时态成立的前提）

`memcore.py:638-716`（`add()`，写入在 `707-716`）→ 增 `event_at: Optional[int]`，写 `valid_from = event_at or ts`；CLI `add` 加 `--event-at`（`mem:73-89`），MCP（`mem_mcp.py`）同步。**工作量 S** → **风险**：**不传时绝不能写 0**。`_select_candidates` 把 `valid_from=0` 解释为"无下界"（`1323`），一旦有记忆落成 0，它会在**任意** `as_of` 查询里出现，时点检索静默变宽。默认必须回落到 `ts`（= 现状行为），0 只留给确实不知道事件时间的老数据。

### 优先级

1. **落点 1 + 2 + 3**：边的时间轴，一套改动必须一起上，否则语义半截（`links` 有时间列但 PK 不允许历史 = 白改）。
2. **落点 4 + 5 + 9**：让时间可读、可写（查询能裁边、brief 能看见窗口、`add` 能写事件时间）。
3. **落点 6 / 7 / 8**：`history` 的分叉语义、软前置、聚类摘要 —— 都依赖前两批的数据质量，先有数据再做。

一句话总结：**Graphiti 教的是"把失效写成时间区间而不是布尔位"，Cognee 教的是"时间可以只是排序信号而不必是硬过滤"**。memcore 已经有 `valid_from/valid_to` 的骨架和 `as_of` 的入口，缺的是把同一套语义搬到 `links` 上，以及把"事件时间"和"摄入时间"拆成两条通道。
