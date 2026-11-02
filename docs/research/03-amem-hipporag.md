# 竞品记忆系统拆解 03 · A-MEM / HippoRAG（v1 & v2）

> 目标：给本机 memcore（`~/.dsh/memory-core`，Python 标准库 + SQLite 单文件）找**能落在现有 `memories` / `links` / `vectors` / `mem_fts` 结构上**的机制。
> 调研时间：2026-11-02。
>
> **取证口径**：论文摘要、仓库 README、以及 raw 源码原文三样都读了；二手解读只用来定位。上游引用按「路径 › 符号（行号为本地 raw 快照的实际行号）」给出，仓库抓到的是 `main` 分支快照；memcore 引用按「`memcore.py:行号`」给出。
> memcore 行号取自 **2026-11-02 18:34 的快照（2359 行）**——该文件当时正在被并发编辑，行号会漂移，所以每条都同时给了函数名，以符号为准。
> 标 `未核实` 的条目表示：读过相关材料但没在源码里找到对应实现点。

---

## memcore 基线（落点要用到的现状）

| 位置 | 事实 |
|---|---|
| `memcore.py:638 add()` | 写入时算 `embed()`（692 附近）+ `simhash`（702 附近），落 `memories` / `mem_fts` / `vectors` / `tags`，最后调 `_auto_links`（725） |
| `memcore.py:714` | `memories.meta` 是一个自由 JSON 列，`meta or {}` 直接序列化；`update()` 里 meta 走 **merge 而不是覆盖**（731 起，注释明确写了理由） |
| `memcore.py:370-371` | `links(src,dst,rel,weight,created_at)`，`PRIMARY KEY(src,dst,rel)`——**一条边只能带 1 个标量权重，没有属性** |
| `memcore.py:888 _auto_links()` | 先用 simhash 汉明距离线性扫 `max_scan=4000` 取前 40，再余弦精排，`min_sim` 默认 0.20（`DEFAULT_CONFIG` 78 行附近），`max_links=4` |
| `memcore.py:1309 _select_candidates()` | 五路候选：FTS5 trigram(bm25) → LIKE 兜底 → tags/entities → 标题 bigram 扫 → simhash+余弦；最后 pinned/近 14 天兜底 |
| `memcore.py:1417 search()` | 加权分（`DEFAULT_CONFIG.weights`：lexical .50 / semantic .22 / overlap .12 / title .16 / recency .10 / salience .10 / reinforce .08 / pinned .15）+ 沉睡门槛（1508/1519/1533）+ 末尾 MMR（1557-1574，`mmr_lambda=0.72`，用 simhash 汉明当相似度） |
| `memcore.py:368 entities` 表 | **建表、建索引（369）、查询（1379）、加分（1490/1505/1519）全都在，但整个仓库没有一条 `INSERT INTO entities`**——纯死信号 |
| `memcore.py:2219 relink()` | `rebuild=True` 时 `DELETE FROM links WHERE rel='related'` 后全量重建 |
| `memcore.py:2067 _llm_judge()` | 私有 LLM 调用，urllib + 强制 JSON + 「never raises」，是目前唯一可复用的 LLM 通道 |
| `memcore.py:1817 reindex()` | 全量重算向量；但**没有记录 embedding 算法/维度版本**，改了 `_features` 或 `VECTOR_DIM` 后旧向量会静默错配 |

---

## A-MEM

### 定位 / 许可证 / 语言 / 时间线

- 论文 arXiv:2502.12110「A-MEM: Agentic Memory for LLM Agents」，Xu et al.，**NeurIPS 2025**；摘要页显示 v1 = 2025-02-17，最新 **v11 = 2025-10-08**（迭代了 11 版，是这份拆解里改得最勤的一篇）。
- 两个仓库，**别混**：
  - `agiresearch/A-mem`：论文摘要里指的"system"实现，本文读的就是它（`main` 分支，`agentic_memory/memory_system.py` 727 行 + `retrievers.py` + `llm_controller.py` 97 行）。
  - `WujiangXu/AgenticMemory`：论文实验代码（摘要里写 "source code for evaluating performance"）。**未核实**（未读）。
- 许可证 **MIT**（`LICENSE`：`Copyright (c) 2025 AGI Research`）。语言 **Python**，`pyproject.toml` + `pip install .`。
- 依赖重：`chromadb`、`sentence-transformers`、`rank_bm25`、`litellm`、`nltk`、`transformers`、`scikit-learn`。→ **和 memcore「标准库 + SQLite」的定位正面冲突**，所以下面只抄机制、不抄依赖。

### 数据模型：一条记忆里到底存了什么

`agentic_memory/memory_system.py › MemoryNote.__init__`（36-81）字段全集：

| 字段 | 默认 | 说明 |
|---|---|---|
| `content` | 必填 | 原始文本 |
| `id` | `uuid4()` | 字符串 UUID |
| `keywords` | `[]` | LLM 抽的关键词，按重要性排序 |
| `links` | `[]` | 相关记忆 id 列表（**扁平 list，不是边表**） |
| `context` | `"General"` | **一句话的"这条记忆在讲什么"**，A-MEM 的核心增量 |
| `category` | `"Uncategorized"` | 粗分类 |
| `tags` | `[]` | 分类标签 |
| `timestamp` / `last_accessed` | `YYYYMMDDHHMM` 字符串 | 分精度到分钟 |
| `retrieval_count` | `0` | 被检索次数 |
| `evolution_history` | `[]` | 演化历史（**实际是死字段，见下**） |

关键结构事实：

1. **note 本身没有 embedding 字段**。向量在 ChromaDB，`ChromaRetriever.add_document()`（`retrievers.py`）把 metadata 全部压成 `str`：`list`/`dict` → `json.dumps`，其它 → `str()`；读回来靠 `ast.literal_eval`（`_convert_metadata_dict`）反序列化。**这是脆的**，中文引号/`None`/嵌套都会打回原形。
2. **没有持久化的 note store**。`self.memories = {}` 是内存 dict（`__init__` 里），进程退出即丢。唯一"落盘"是 Chroma 里那份被打扁的 metadata 副本。`PersistentChromaRetriever` 只解决向量+metadata 的持久化，不解决 note 对象。
3. 向量化只对 `content`（`add_document(note.content, metadata, note.id)`）——**`keywords`/`context`/`tags` 不参与 embedding**，只做展示和喂给后续 LLM 决策。

### 写入路径：新建笔记时发生了什么

`add_note()`（233-265）+ `process_memory()`（590-727）：

1. `MemoryNote(content, **kwargs)` 构造——kwargs 里可以带 `keywords`/`context`/`tags`/`category`/`timestamp`。
   **注意**：`analyze_content()`（159-232，那个负责"生成 keywords/context/tags"的 LLM 调用）在仓库里**只有定义、没有任何调用点**（全仓 grep 只命中 159 行的 `def`）。→ 当前 main 分支的"结构化笔记生成"是**调用方手工喂**，不是自动的。README 里 "Generates comprehensive notes with structured attributes" 与源码有落差。
2. `process_memory(note)` → `(should_evolve, note)`（下面「动态演化」细讲）。
3. `self.memories[note.id] = note`。
4. 组 metadata dict → `retriever.add_document(note.content, metadata, note.id)`。
5. 若 `should_evolve=True` 则 `evo_cnt += 1`，满 `evo_threshold`（默认 **100**）触发 `consolidate_memories()`——**reset 整个 Chroma collection 再全量重嵌**。

代价：**每写一条 = 1 次向量检索 + 1 次 LLM 往返**（`process_memory` 无条件跑）。memcore 的 `add()` 是纯 CPU、毫秒级，这是最需要警惕的落差。

### 检索路径：先粗筛再精排？链式扩散？

`search_agentic()`（509-588）：

1. **一次** ChromaDB `query(query_text, n_results=k)`，没有二阶段精排，也没有 BM25 混合（`rank_bm25` 虽然装了但 `search_agentic` 不用）。
2. 逐条取 metadata 组 dict，`score` 直接塞 `distances`（**Chroma 的 distance，越小越相似**，但代码没做任何方向统一，也没排序）。
3. **一跳链式扩展**：遍历已命中记忆的 `links`，把邻居 append 进来并标 `is_neighbor=True`，每个种子最多带 k 个邻居。
4. `return memories[:k]`——**但前面已经收满了 k 个种子**，所以邻居几乎必然被这个切片砍掉。→ 对 `k<=5` 的常规调用，"链式扩散"实际是**惰性的**。

真正的多跳上下文在另一条路径：`find_related_memories_raw()`（315-345）对每个种子展开 `j < k` 个邻居，拼成大字符串喂给 QA 模型——**这是给 LLM 读的，不是给排序用的**。

返回结构：`{id, content, context, keywords, tags, timestamp, category, is_neighbor, score?}`——**`context` 和 `keywords` 是随结果一起返回的**，这点比 memcore 只回 title/body 更有信息量。

### 「动态演化」具体是怎么实现的（最值得看的部分）

`process_memory()`（590-727）是全文核心，逐步拆：

**第 0 步：首条记忆直接返回**，不演化（`if not self.memories: return False, note`）。

**第 1 步：取邻居**
`find_related_memories(note.content, k=5)`（288-315）→ 返回 `(neighbors_text, indices)`。
`neighbors_text` 是 5 行文本，每行格式：
`memory index:{i}\ttalk start time:{ts}\tmemory content:…\tmemory context:…\tmemory keywords:…\tmemory tags:…`
`indices` 是 **`indices.append(i)` 得到的 0..k-1 位次**，不是记忆 id。← 这里埋了 bug（见第 4 步）。

**第 2 步：让 LLM 决策**
用 `_evolution_system_prompt`（126-157），入参是：新记忆的 `context`/`content`/`keywords` + 上面 5 行邻居文本 + `neighbor_number`。要求返回**严格 JSON schema**（`strict: True`）：

```json
{
  "should_evolve": true,
  "actions": ["strengthen", "update_neighbor"],
  "suggested_connections": ["neighbor_memory_ids"],
  "tags_to_update": ["tag_1", "tag_n"],
  "new_context_neighborhood": ["new context", "..."],
  "new_tags_neighborhood": [["tag_1"], ["tag_n"]]
}
```

prompt 里写明两条硬约束：`new_tags_neighborhood` 与 `new_context_neighborhood` 的长度必须等于邻居数；两个动作的语义分别是"给这条新记忆连边/改标签"和"反过来改旧邻居的 context 与 tags"。

**第 3 步：执行 `strengthen`（改新记忆自己）**
```python
note.links.extend(response_json["suggested_connections"])
note.tags = response_json["tags_to_update"]
```
- `suggested_connections` 是 LLM **凭上下文生成**的 id 字符串，**没有任何校验**（不查 `self.memories`，不查 Chroma）。→ 幻觉 id 直接进 `links`，后续扩展时 `self.memories.get(link_id)` 取不到就静默跳过。
- `note.tags = ...` 是**整体覆盖**而不是合并。旧标签全丢。

**第 4 步：执行 `update_neighbor`（反向改旧记忆）—— 这是 A-MEM 的灵魂，也是它最大的坑**
```python
noteslist = list(self.memories.values())     # 按插入顺序
notes_id  = list(self.memories.keys())
for i in range(min(len(indices), len(new_tags_neighborhood))):
    ...
    memorytmp_idx = indices[i]               # == i
    notetmp = noteslist[memorytmp_idx]       # ← 拿的是"第 i 条插入的记忆"
    notetmp.tags = tag
    notetmp.context = context
    self.memories[notes_id[memorytmp_idx]] = notetmp
```
因为 `indices[i] == i`，这段代码实际改的是 **`memories` 字典里第 i 个插入的记忆**，而不是**第 i 个最相似的邻居**。→ 新记忆会随机改写若干条**毫不相干**的旧记忆的 `context`/`tags`。这是语义污染，且不可审计（没有历史记录）。
**这就是「动态演化」在开源实现里的真实状态：意图对，索引错。**

**第 5 步：反向更新没有落到向量库**
`update_neighbor` 只改了 `self.memories` 里的对象，**没有调用 `retriever.add_document` 重新写回 Chroma**（对比 `update()` 357-397 是 `delete_document` + `add_document` 的）。→ 被改过的 `context`/`tags` 在向量库里依然是旧值，**要等到第 100 次演化触发 `consolidate_memories()` 全量重建才同步**。中间这段窗口里，note 对象和检索结果互相矛盾。

**第 6 步：`evolution_history` 是死字段**
全仓 grep：只有 `__init__` 里 `self.evolution_history = evolution_history or []`（81）和三处 metadata 拷贝（254/282/387）。**没有任何一处 append**。演化发生过什么，事后无法回溯。

**补充：`consolidate_memories()`（266-287）** 是全量重建 — `ChromaRetriever(collection_name="memories")` 新建（构造里 `client.reset()`），再把 `self.memories` 全量 `add_document`。**O(N) 重嵌，且中途失败就半截**。

### 关键机制（机制 → 解决什么失败模式 → memcore 能否用）

| # | 机制 | 解决的失败模式 | memcore 能否用 |
|---|---|---|---|
| 1 | **`context` 字段：每条记忆带一句"它在讲什么"** | 向量相似但语义不相干的假命中；召回结果只有正文，人（和 agent）看不出"为什么相关" | ✅ 直接能——`memories.meta` 里加一个键即可，零 schema 变更 |
| 2 | **写入时反向编辑旧记忆**（新记忆改善旧记忆的表述） | 老记忆的标签/描述是"写入那一刻"的快照，后来同主题的记忆进来了，老条目永远是孤岛 | ✅ 能，但**只能 append 不能覆盖**（A-MEM 覆盖 tags 是事故源头） |
| 3 | **LLM 决策 `should_evolve` 布尔开关** | 无条件演化 = 每次写入都烧 token；大多数新记忆其实不改变任何东西 | ✅ 能，`_llm_judge` 已有现成通道 |
| 4 | **把邻居连同 `context`/`keywords` 一起格式化成文本喂 LLM** | 只给 embedding 距离，LLM 无法判断"是否同一话题"；给结构化文本可以 | ✅ 能，纯字符串拼接 |
| 5 | **演化动作被拆成"改自己"和"改邻居"两类** | 单一"更新"语义混淆了"我的描述变了"和"别人的描述该变了" | ✅ 概念可用，实现要重写 |

### 不要抄的部分

1. **`update_neighbor` 的 `indices` 混用**（第 4 步）——位次当 id 用，直接改错记忆。任何"按 rank 回写"的实现都在这个坑里。
2. **`note.tags = ...` 整体覆盖**——不可逆、无历史。
3. **`evolution_history` 建了不用**——要么实现，要么别留这个字段（memcore 有 `_journal`，别重复犯）。
4. **`suggested_connections` 不校验**——LLM 编的 id 直接进图。
5. **反向更新不回写向量库**（第 5 步）——内存与索引长期不一致。
6. **`consolidate_memories` 全量 reset 重建**——O(N) 重嵌，中断即半截。memcore 的 `relink(rebuild=True)` 是同一类问题，别再加一个。
7. **`search_agentic` 用 distance 当 score 且不排序**——语义方向反了。
8. **`add_note` 不调 `analyze_content`**——"agentic" 的名不副实；抄它就要连这个落差一起抄。
9. **依赖 ChromaDB + sentence-transformers**——与 memcore 定位冲突，机制可搬、栈不可搬。

### 来源

- 论文摘要：https://arxiv.org/abs/2502.12110
- 仓库 README：https://github.com/agiresearch/A-mem（raw：`https://raw.githubusercontent.com/agiresearch/A-mem/main/README.md`）
- 源码：`agentic_memory/memory_system.py`（`MemoryNote` 36-81、`analyze_content` 159-232、`add_note` 233-265、`consolidate_memories` 266-287、`find_related_memories` 288-315、`find_related_memories_raw` 315-345、`update` 357-397、`search_agentic` 509-588、`process_memory` 590-727、`_evolution_system_prompt` 126-157）
  https://raw.githubusercontent.com/agiresearch/A-mem/main/agentic_memory/memory_system.py
- 源码：`agentic_memory/retrievers.py`（`ChromaRetriever.add_document` 的 metadata 压平与 `_convert_metadata_dict` 的 `ast.literal_eval` 还原）
  https://raw.githubusercontent.com/agiresearch/A-mem/main/agentic_memory/retrievers.py
- 许可证：https://raw.githubusercontent.com/agiresearch/A-mem/main/LICENSE（MIT, 2025 AGI Research）

---

## HippoRAG（v1 & v2）

### 定位 / 许可证 / 语言 / 时间线

- v1 论文 arXiv:2405.14831「HippoRAG: Neurobiologically Inspired Long-Term Memory for LLMs」，**NeurIPS 2024**（v1 = 2024-05-23，v3 = 2025-01-14）。
- v2 论文 arXiv:2502.14802「From RAG to Memory: Non-Parametric Continual Learning for LLMs」，**ICML 2025**（v1 = 2025-02-20，v2 = 2025-06-19）。
- 仓库 `OSU-NLP-Group/HippoRAG`：**MIT**（`LICENSE`：`Copyright (c) 2025 OSU Natural Language Processing`）。Python **3.10**（conda/uv）。v1 源码留在 **`legacy` 分支**（README 顶部链接 `tree/legacy`）。
- 依赖：`igraph`（PPR 用它的 `personalized_pagerank`，`implementation='prpack'` 是 C 实现）、`numpy`、`torch`/`sentence-transformers`、`litellm`/`openai`、`dspy` 的 prompt 资产。→ 同样是"重栈"，机制可搬、依赖不可搬。
- v2 比起 v1 的实质变化，按摘要 + 源码：**passage node 深度纳入 PPR 种子**（不再只靠实体）+ **识别记忆**（LLM 只做 fact 筛选，不生成）+ **增量索引**（新实体才做 synonymy KNN）。

> ⚠️ **一处需要更正的前提**：流传的说法是「v2 不依赖 OpenIE 的增量索引」。**源码里 v2 仍然用 OpenIE**（`information_extraction/openie_openai.py`、`openie_vllm_offline.py`、`openie_transformers_offline.py` 三个实现都在，`index()` 仍然抽 triples）。v2 真正的改动是**让检索不再单点依赖 OpenIE 的质量**：passage 的 dense 分数直接注入 PPR 种子（`passage_node_weight`），图通道挂了还能退化成 dense。这是"鲁棒性"上的解耦，不是"去掉 OpenIE"。

### 数据模型：图里到底有哪几种节点/边

`HippoRAG.py`，**三层节点、三类边，全部存在一个 `igraph.Graph` 里**，没有关系型 schema：

**节点**（`name` 是 md5 派生的稳定 key，`compute_mdhash_id(content=..., prefix=...)`）：

| 节点 | key 前缀 | 来源 |
|---|---|---|
| passage node | `chunk-` | 原始文本块，`passage_node_keys` / `passage_node_idxs` 维护下标映射 |
| entity（phrase）node | `entity-` | OpenIE 抽出的 subject/object 短语，小写化后哈希 |
| （v1 里另有 fact 的表达，v2 收敛为"实体对之间的边"） | | |

**边**（权重全在 `self.node_to_node_stats: Dict[Tuple, float]`，最后一次性灌进 igraph 的 `weight` 属性）：

| 边 | 建立函数 | 权重 |
|---|---|---|
| entity — entity（=fact） | `add_fact_edges`（1179-1229） | **共现次数累加**（`+1.0` 每次），并记 `_fact_edge_source_counts[edge][chunk_key]` |
| passage — entity | `add_passage_edges`（1230-1277） | 固定 `1.0` |
| entity — entity（同义） | `add_synonymy_edges`（1278-1353） | 余弦相似度，`max(existing, score)` 合并 |

**关键细节（对 memcore 有直接启发）**：
- 图是**无向**的（`add_fact_edges` 里 `if self.graph.is_directed(): ... else: (tuple(sorted((k1,k2))),)`），PPR 也按 `directed=False` 跑。
- 同义边只在**新增实体**与全库之间做 KNN（增量），不是每轮全量。这正是 v2 "显著低于 GraphRAG/RAPTOR 索引成本"的来源。
- **同义边对中文是死的**：`add_synonymy_edges` 里有一行门槛 `if len(re.sub('[^A-Za-z0-9]', '', entity)) > 2:` —— 纯中文实体剥掉非 ASCII 后长度 0，**永远拿不到同义边**。对中文记忆库直接失效（这是个可以明确避开的坑）。
- 落盘三件套：`igraph` 图（`save_igraph`）、`embedding_store` 的 parquet（passage/entity/fact 三套向量）、`index_manifest.json`（绑定 embedding 模型、LLM、normalization、openie token 上限等身份；不匹配**拒绝复用**旧索引，而不是静默混用）。

### 写入路径：`index()`（499-594）

1. `_preprocess_docs` → `Chunk` 列表（切块）。
2. OpenIE：NER + triple 抽取（online 走 chat LLM；offline 走 vLLM/Transformers 批推理）。结果可缓存复用（`load_existing_openie` 1354）。
3. `add_fact_edges`（建实体对边 + 记 `ent_node_to_chunk_ids` 反查表）。
4. `add_passage_edges`（passage 连到它里面出现的所有实体，权重 1.0）。
5. `add_synonymy_edges`（只对新实体做 KNN）。
6. `augment_graph()` → `add_new_nodes()`（1521）+ `add_new_edges()`（1551）落到 igraph，边权在 `compose_metadata`/`merge_metadata`（1566/1598）里做合并。
7. `save_igraph()`；`delete()`（595-683）走反向链路清理（`_remove_fact_sources_from_graph` 684）。

**没有"每条记忆一份 note 对象"的概念**——只有图 + 三套向量。这和 A-MEM 是两种世界观：A-MEM 是**文档库 + 显式 links**，HippoRAG 是**图即索引**。

### 检索路径：`retrieve()`（704-795）

四步，顺序很关键：

1. **fact 打分**：`get_fact_scores(query)`（1892-1928）= 查询向量与**全部 fact 向量**的点积，再 `min_max_normalize`。→ **O(#facts) 全量**，没有 ANN 预筛。
2. **识别记忆（recognition memory）**：`rerank_facts()`（2124-2176）把 top facts 的 JSON 丢给 LLM（`rerank.py › DSPyFilter`），要求返回**筛选后的 fact 列表**——**只筛不生成**，这是防幻觉的设计。映射回候选靠 `difflib.get_close_matches(..., cutoff=0.0)`，`cutoff=0.0` 意味着**永远返回最像的那一个**，LLM 输出乱码也会硬塞一个回来（鲁棒性打折）。
3. **退化路径**：`if len(top_k_facts) == 0: 直接 dense_passage_retrieval()`——**显式写着 fallback**，不是异常。
4. **PPR 重排**：`graph_search_with_fact_entities()`（2008-2123），见下。

**种子权重怎么算（这段是精华）**：

```python
# 1) 短语权重：只取 fact 的 subject / object
phrase_key = compute_mdhash_id(phrase, prefix="entity-")
if len(self.ent_node_to_chunk_ids.get(phrase_key, set())) > 0:
    weighted_fact_score = fact_score / len(self.ent_node_to_chunk_ids[phrase_key])   # ← hub 抑制
phrase_weights[phrase_id] += weighted_fact_score
number_of_occurs[phrase_id] += 1
...
phrase_weights = np.divide(phrase_weights, number_of_occurs, ...)                    # ← 出现次数平均
```
- **`fact_score / df`**：某个实体在越多 passage 里出现（df 越大），它作为种子的权重越低。这是 IDF 式的 hub 抑制——专治"到处都是的实体（本项目名、产品名、'用户'）把 PPR 质量冲淡"。
- 再对同一实体的多次出现取**均值**（不是求和），防止一个高频 fact 独占某实体。
- `get_top_k_weights(link_top_k)`（1970-2007）把**非 top-k 的短语种子权重全部清零**——种子上限，防止发散。

```python
# 2) passage 权重：dense 分数归一化 × 0.05
dpr_sorted_doc_ids, dpr_sorted_doc_scores = self.dense_passage_retrieval(query)
normalized = min_max_normalize(dpr_sorted_doc_scores)
passage_weights[passage_node_id] = normalized[i] * passage_node_weight   # 默认 0.05
node_weights = phrase_weights + passage_weights
if sum(node_weights) <= 0: raise StateConsistencyError(...)
```

**PPR 本体**（`run_ppr` 2177-2218）：

```python
reset_prob = np.where(np.isnan(reset_prob) | (reset_prob < 0), 0, reset_prob)   # 脏种子清零
pagerank_scores = self.graph.personalized_pagerank(
    vertices=range(len(self.node_name_to_vertex_idx)),
    damping=damping,            # 默认 0.5
    directed=False,             # 无向投影
    weights='weight',           # 边权进转移概率
    reset=reset_prob,           # personalized：种子即 teleport 分布
    implementation='prpack')
doc_scores = [pagerank_scores[idx] for idx in self.passage_node_idxs]
sorted_doc_ids = np.argsort(doc_scores)[::-1]
```

→ **PPR 输出的不是"路径"，是"每个 passage 节点被随机游走访问到的概率"**。damping=0.5 意味着游走半径约 2 跳，这正是它"多跳但发散可控"的来源。多跳不需要真的 BFS，一次矩阵迭代就够，token 成本为 0（这也是摘要里"比 IRCoT 便宜 10-30 倍、快 6-13 倍"的机械原因）。

### 「动态演化」——它其实没有演化，只有增量索引

诚实说：**HippoRAG 没有 A-MEM 那种"新记忆改写旧记忆"的机制**。它的"动态"体现在三处：

1. **增量建图**：`augment_graph()`（1508）→ `add_new_nodes`（1521）+ `add_new_edges`（1551），只为新 chunk 加节点/边，不重建全图。`add_new_edges` 里用 `logical_key(source_key, target_key)`（1563）保证无向边只有一种 key，`merge_metadata`（1598）负责把新观测的 source 计数并进旧边。
2. **增量同义边**：只对新实体做 KNN（1353 附近注释写明动机："only between newly inserted phrase nodes and all phrase nodes ... to reduce cost for incremental graph updates"）。
3. **索引身份绑定**：`index_manifest.json` + `_validate_or_create_index_manifest`（317）/ `_validate_openie_provenance`（390）。换 embedding 模型、换 OpenIE 参数（连 `openie_ner_max_tokens` 都算身份）→ **拒绝加载**，要求重建。这是"演化可复现"的工程化答案：**宁可不复用，也不混用**。

代价对照 A-MEM：A-MEM 每次写 = 1 次 LLM；HippoRAG 每次写 = OpenIE 一次（贵得多），但**检索时零 LLM**（除了一次可选的 fact 筛选）。

### 关键机制（机制 → 解决什么失败模式 → memcore 能否用）

| # | 机制 | 解决的失败模式 | memcore 能否用 |
|---|---|---|---|
| 1 | **hub 抑制：种子权重 = fact_score / 实体覆盖的记忆数** | 高频实体（项目名、"用户"）在所有记忆里都出现，纯 cosine 检索被它主导，长尾实体永远排不上 | ✅ **最值得抄的一条**。`entities` 表已有 `(memory_id, name)` + `name` 索引，`count(*)` 就是 df |
| 2 | **双通道种子 + 显式 fallback**（图通道 + dense×0.05；fact 为空就退 dense） | 图/实体通道建错或为空时整体检索崩掉 | ✅ 能，`search()` 末尾加一个 `if not out:` 分支即可 |
| 3 | **PPR 当排序器**（一次随机游走 = 多跳，token 成本 0） | 多跳推理靠"反复 retrieve + 让 LLM 拼"，贵且发散 | ✅ 能，`links` 表就是邻接表；用 12 次幂迭代代替 igraph |
| 4 | **识别记忆：LLM 只筛候选，不生成** | 让 LLM "重写记忆"会产幻觉；只在**已有候选集**里做二分判断则风险可控 | ✅ 能，`_llm_judge` 已有同款结构 |
| 5 | **增量索引 + 身份绑定**（只处理新节点；manifest 不匹配即拒绝） | 换 embedding 后旧向量静默错配，检索结果悄悄变差 | ✅ 半个——memcore 有 `vectors` 表但**没有版本记录** |
| 6 | 边权用 `max()` 合并而不是覆盖 | 增量更新时新观测把旧的高权重边压掉 | ✅ 能，`INSERT OR REPLACE` 改成 `max()` 语义 |

### 不要抄的部分

1. **`igraph` + `prpack`**——C 扩展依赖，memcore 要 Python 标准库。PPR 自己写幂迭代（1000 节点 × 12 轮纯 Python 也够快）。
2. **`get_fact_scores` 全量点积**（`np.dot(全部 fact 向量, query)`）——O(#facts) 每次查询。memcore 只有几千条还撑得住，但**架构上必须先预筛再打分**，别把这个线性扫描当范式。
3. **`difflib.get_close_matches(..., cutoff=0.0)`**——永远返回一个匹配，LLM 输出垃圾也硬塞。要么设阈值，要么丢弃。
4. **同义边的纯 ASCII 过滤**（`re.sub('[^A-Za-z0-9]','',entity)` 长度 >2）——中文实体直接失效。
5. **OpenIE 全量抽取**——每条记忆一次 LLM，比 A-MEM 还贵；而且抽取质量决定图质量，是个不可控前置依赖。
6. **无向化一切边**——HippoRAG 可以让 `supersedes`/`contradicts` 这种有方向的语义边失去方向；memcore 的 `links.rel` 是有类型的，别学它压平。
7. **`index_manifest` 的严格拒绝策略** 对 memcore 过重（用户会莫名其妙被挡住）；但"版本不匹配要**显式降级**而不是静默"这个原则要留。

### 来源

- v1 摘要：https://arxiv.org/abs/2405.14831
- v2 摘要：https://arxiv.org/abs/2502.14802
- 仓库 README（含 legacy 分支链接、MCP server、vector store 后端、升级策略）：https://github.com/OSU-NLP-Group/HippoRAG
  raw：`https://raw.githubusercontent.com/OSU-NLP-Group/HippoRAG/main/README.md`
- 源码 `src/hipporag/HippoRAG.py`：`retrieve` 704-795、`add_fact_edges` 1179-1229、`add_passage_edges` 1230-1277、`add_synonymy_edges` 1278-1353、`augment_graph` 1508、`add_new_nodes` 1521、`add_new_edges` 1551、`get_fact_scores` 1892-1928、`get_top_k_weights` 1970-2007、`graph_search_with_fact_entities` 2008-2123、`rerank_facts` 2124-2176、`run_ppr` 2177-2218、`index` 499-594、`_validate_or_create_index_manifest` 317
  https://raw.githubusercontent.com/OSU-NLP-Group/HippoRAG/main/src/hipporag/HippoRAG.py
- 源码 `src/hipporag/rerank.py`（`DSPyFilter.rerank` 的筛选式识别记忆 + difflib 回填）
  https://raw.githubusercontent.com/OSU-NLP-Group/HippoRAG/main/src/hipporag/rerank.py
- 许可证：https://raw.githubusercontent.com/OSU-NLP-Group/HippoRAG/main/LICENSE（MIT, 2025 OSU NLP）
- v1 的代码级细节（legacy 分支实现）**未核实**——本文 v1 论述仅依据论文摘要与 README 表述。

---

## 对 memcore 的具体落点

> 全部落在现有 `memories` / `links` / `vectors` / `mem_fts` / `entities` 上。不引入向量服务、不引入图数据库、不要求 LLM 在线（LLM 项一律 best-effort + 超时 + 静默失败）。
> 行号为 2026-11-02 18:34 快照；文件在并发编辑中，**以函数名为准**。

### 【落点 1】启用 `entities` 死表（HippoRAG 的地基，也是唯一的前置条件）

**`memcore.py › add()`（638）/ `_select_candidates()`（1309）→ 给 `entities` 表加一条写入路径，让 1379 行那个 `SELECT memory_id FROM entities WHERE name=?` 真正有数据。**
具体：在 `add()` 的 `_auto_links` 调用（725）之前插入 `_index_entities(mid, title, body, tags)`：只把 **tags + 标题里的 ASCII/数字标识符**（路径、函数名、`memcore.py`、`FTS5`、`0.72` 这类）写进 `entities(memory_id, name, etype='term')`，`etype='term'` 先只装这一类，不碰自由短语。
→ **工作量 S** → **风险**：标准库没有中文分词器，自由短语必然碎，所以第一版**只做确定性标识符**；`_select_candidates` 里 `ent` 命中会给 `+0.05`（1490），一旦误召回就是全局加分，先用小样本对 `search()` 的 top-10 做过 A/B 再开。

### 【落点 2】hub 抑制：把 `ent` 从 0/1 换成 `1/log(1+df)`

**`memcore.py › _select_candidates()`（1379 行那 3 行）→ `"ent": 1.0` 改成 `"ent": 1.0 / math.log(1 + df)`，`df` 来自 `SELECT count(*) FROM entities WHERE name=?`。**
这是 HippoRAG `fact_score / len(ent_node_to_chunk_ids[phrase])` 的直译，专治"到处都出现的词吃满候选"。
→ **工作量 S** → **风险**：`entities(name)` 索引已有（369），count 是一次索引查找，可接受；但**同一次查询里同一个 name 会被 count 多次**，要加个 per-query 小缓存；另外 df 依赖落点 1 的写入覆盖率，写入稀疏期 df 恒为 1，等于没抑制——先观察一个月再固化为默认值。

### 【落点 3】`search()` 里的 PPR 重排（HippoRAG 核心机制）

**`memcore.py › search()`（1417）→ 在 `out.sort(...)`（1557）之后、MMR（1559-1574）之前插一个 `_ppr_rerank(seed_ids, alpha, iters)`：把 top-30 命中当种子，从 `links` 表读邻接表（`src,dst,weight`），幂迭代 `x ← (1-α)e + α·Wᵀx`（α≈0.5、12 轮、行归一化），结果按 `score ← 0.85·score + 0.15·ppr` 并入。**
→ **工作量 M** → **风险**：(a) `links` 全表读入内存——几千条无压力，10 万条要分块 + 缓存失效策略；(b) 必须处理 `rel` 差异：`supersedes` 边的语义是"新的取代旧的"，PPR 会把它当普通连通性，**旧记忆会被顶上来**，所以 PPR 邻接表里 `rel='supersedes'` 权重乘 0.2 或直接剔掉；(c) PPR 天然奖励"连接多的记忆"，与 MMR 的多样性目标相反，α 别调大、只 re-rank 前 30 条、别让它覆盖 MMR 之后的顺序。

### 【落点 4】A-MEM 的 `context` → `memories.meta['ctx']`

**`memcore.py › add()`（638，714 行 `json.dumps(meta or {})`）→ 加一个可选 `ctx: str = ""` 参数，写进 `meta["ctx"]`；展示侧改 `_format_hit()`（1615）在标题后追加 ` — {ctx}`（截断 60 字）。**
`update()` 的 meta 是 merge 语义（731-758），后续改写不会覆盖。
→ **工作量 S** → **风险**：**绝对不要把 `ctx` 塞进 `mem_fts` 的 text（717 行 `" ".join([title, body, tags, project])`）**——`ctx` 是"泛化描述"，每条都长得像，会污染 trigram 的 bm25；它只该出现在向量里（`embed(title+"\n"+body)` 692 附近可以改成 `+ "\n" + ctx`）和展示里。改向量输入意味着**所有旧向量与新向量不同分布**，要么全量 `reindex()`（1817，O(N)），要么接受一段混合期——这是这个落点真正的代价。

### 【落点 5】A-MEM 的"反向更新旧记忆" → `meta['ctx_nb']` 只 append

**`memcore.py › add()`（725，`_auto_links` 返回后）→ 取 `_auto_links` 已算出的 top-1~2 邻居，把新记忆的 `title` append 进邻居的 `meta["ctx_nb"]` 列表（FIFO，上限 8 条），并 `_journal("evolve", 邻居id, {...})`（1832）留痕。**
这是 A-MEM「新记忆改善旧记忆」的**安全版**：append 而不是覆盖，有上限，有审计。
→ **工作量 S** → **风险**：(a) `add()` 会多出 1-2 次 UPDATE，`_ingest_one()`（1947）批量灌库时**必须绕开**（走 `autolink=False` 或 `touch=False` 的路径），否则灌 1000 条会话会多 2000 次写；(b) 不要让 `ctx_nb` 参与打分——它是给人看和给 `brief()` 用的"这条记忆后来被谁提过"，一旦进权重就变成隐式的 recency 干扰；(c) `meta` 变大，`export_markdown`/`MEMORY.md` 要确认不会把 `ctx_nb` 打印出来（噪音）。

### 【落点 6】把 `_llm_judge` 抽成通用 `_llm_json()`，承载 `ctx` 生成与 `should_evolve` 开关

**`memcore.py › _llm_judge()`（2067）→ 把 urllib + 强制 JSON + 「never raises」那套提成 `_llm_json(prompt, timeout=3)`，供落点 4/5 复用；A-MEM 的 `should_evolve` 布尔直接当"要不要写 `ctx_nb`"的闸门。**
→ **工作量 S** → **风险**：本地 gateway 在 `_llm_judge` 里是 `timeout=25`，而 `add()` 是同步路径——**必须压到 3s 并彻底静默失败**（A-MEM 的 `process_memory` 是全 try/except 吞掉、note 照常落库，这点学它）；另外 `MEM_LLM_KEY` 缺失时 `_llm_key()` 会回落到 gateway 文件（448-476），在离线机器上要能快速判定"没 key 就跳过"，别每次 add 都试一次文件读。

### 【落点 7】显式 fallback：`search()` 现在"没找到"就是空

**`memcore.py › search()`（1417）→ 在返回前加 `if not out and q:` 分支：忽略 `evidence` 门（1508），只按 `pinned` + `salience` + `recency` 取 `limit` 条，hit 上标 `"fallback": "recency"`。**
HippoRAG 的 `if len(top_k_facts)==0: dense_passage_retrieval()` 就是这个思想：**退化路径要写在主流程里，不是靠异常**。
→ **工作量 S** → **风险**：**只给 `recall()` / `brief()` 用，不给 `preflight()` 用**（`preflight` 走 1223 的独立路径，"没坑"必须能返回"没坑"，否则 `mem preflight` 每次都吐一堆无关记忆，直接毁掉这个功能的价值）；`fallback` 标记必须在 `_format_hit` 里可见，否则用户分不清"相关"和"兜底"。

### 【落点 8】向量版本号：现在换 embedding 算法是静默的

**`memcore.py › _meta_init()`（605 附近）/ `Memory.__init__` → 往 `meta` 表写 `k='vector_version'`，值 = `sha1(源码里 _features/embed 的实现 + str(VECTOR_DIM))`；启动时不一致就把语义项降级（不参与打分）并记一次 warning，而不是静默用旧向量。**
这是 HippoRAG `index_manifest.json` 的最小可用版（**不要**学它"直接拒绝加载"，那对本地工具太重）。
→ **工作量 S** → **风险**：降级期间检索质量肉眼可见地掉，用户会以为"记忆库坏了"——所以 warning 必须出现在 `mem doctor` 和 `brief()` 的头部，且给出 `mem reindex` 的明确提示；`reindex()` 是全量 O(N)，大库要前台卡住，得考虑分批。

### 【落点 9】CJK 的两个反面教训 → 调 `_auto_links` 的门槛

**`memcore.py › _auto_links()`（888）→ `min_sim`（0.20）从标量改成按 `len(body)` 分档，并追加 `dice(_grams(...))`（302 已有）做二次门槛；同时确认同义/近邻判定里**没有任何"必须含 ASCII"的过滤**（HippoRAG 的 `re.sub('[^A-Za-z0-9]','',entity)` 就是中文全灭的反例）。**
→ **工作量 S** → **风险**：调阈值会让 `relink()`（2219）一跑全图重排，边数、`graph_export` 的连通片、UI 的力导向布局全变——**先 dry-run 统计边数变化**（`relink(rebuild=False)` 只加不删），再决定是否 `rebuild`。

---

## 一句话总结（给 Lead 的三个最优发现）

1. **A-MEM 的"动态演化"在开源实现里是坏的**：`update_neighbor` 把"第 i 个最相似邻居"当成"第 i 条插入的记忆"来改（`memory_system.py:684-720`，病灶在 `708` 行 `memorytmp_idx = indices[i]`），新记忆会随机污染无关旧记忆的 `context`/`tags`；且改完不回写向量库、`evolution_history` 全程没 append。**抄它的意图（写时反向更新旧记忆），别抄它的实现。**
2. **HippoRAG 最省事的可移植点是"hub 抑制"**：种子权重 `fact_score / 该实体覆盖的 chunk 数`，一行除法治好了"高频实体吃掉检索质量"。而 memcore 的 `entities` 表**已经建好、已经查询、已经加分，就是没人写入**——补一条 `INSERT` 路径就能吃到这个机制。
3. **两个项目都在同一件事上失败**：图/实体通道一旦为空或建错，检索整条崩掉。HippoRAG 的答案是**把 fallback 写进主流程**（`retrieve()` 里 `top_k_facts==0 → dense`），A-MEM 的答案是**没有 fallback**（`search_agentic` 邻居被 `[:k]` 截断后静默少返回）。memcore 现在属于后者：`evidence` 门（1508）不满足就 `continue`，可能返回空列表且没有任何标记。
