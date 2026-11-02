# 竞品记忆系统拆解 01 · Mem0 / Letta（MemGPT）

> 目标：给本机 memcore（`~/.dsh/memory-core`）找**能落在现有 SQLite + FTS5 + 哈希向量 + links 图结构上**的机制。
> 调研时间：2026-11-02。
>
> **取证口径**：结论全部来自 raw 源码 / 官方 docs / 论文摘要，二手文章只用于定位。
> 上游引用按「路径 › 符号」给出（raw 抓取不带行号，不臆造 `#L`）；memcore 引用按「`memcore.py:行号`」给出，行号取自本轮实际读取的版本。
> 标注 `未核实` 的条目表示：读过相关文档但没在源码里找到实现点。

---

## Mem0

### 定位 / 规模 / 许可证 / 时间线

- 仓库 `mem0ai/mem0`，**68,273 star / 8,102 fork / 898 open issues**，主语言 Python，**Apache-2.0**，创建于 2023-06-20，最后一次 push 2026-10-30（GitHub API `repos/mem0ai/mem0`，本轮读取）。
- 形态是三层：`pip install mem0ai` 的 library / `server/` 自托管（docker compose + 自带 auth）/ 托管平台。README 明确写了「Scores reflect Mem0's managed platform … open-source users should expect directionally similar gains but not identical numbers」——**OSS 与平台不是同一套实现**。
- **时间线断代很关键**：README 顶部有「New Memory Algorithm (April 2026)」小节，列出 LoCoMo 71.4 → **92.5**、LongMemEval 67.8 → **94.4**，并写明改动是 *「Single-pass ADD-only extraction — one LLM call, no UPDATE/DELETE」*。而论文 arXiv:2504.19413（2025-04）描述的是 v2：LLM 在四个算子 ADD / UPDATE / DELETE / NOOP 之间决策。
  → **论文的核心卖点在新版里被砍掉了**。这份拆解以**现行源码为准**，论文只作为"他们试过什么、为什么退回来"的证据。

### 核心数据结构

存储是「向量库 + 一张 SQLite 历史表」的双写，没有关系型主体表：

- **向量库**：每条记忆 = `{id: uuid4, vector: embedding, payload: {...}}`。`payload` 里是全部真相：
  `data`（正文）、`hash`（`md5(data)`）、`created_at` / `updated_at`（ISO8601 UTC）、`text_lemmatized`（喂 BM25 用的词形还原文本）、`attributed_to`（user / assistant）、`expiration_date`（`YYYY-MM-DD`，可选）、以及作用域键 `user_id` / `agent_id` / `run_id` / `actor_id`、`role`。
  见 `mem0/memory/main.py › _create_memory` / `_add_to_vector_store` Phase 4。
- **历史表**（`SQLiteManager`，`mem0/memory/storage.py`）：`add_history(memory_id, old_memory, new_memory, event, created_at, updated_at, actor_id, role, is_deleted)`，`event ∈ {ADD, UPDATE, DELETE}`。这是审计链，不是检索源。
- **实体库**：第二套 collection，命名 `<collection>_entities`（`_entity_collection_name`）。payload = `{data: 实体名, entity_type, linked_memory_ids: [...], user_id/agent_id/run_id}`。
- **作用域是硬约束**：`_build_filters_and_metadata` 要求 `user_id` / `agent_id` / `run_id` **至少给一个**，否则抛 `VALIDATION_001`；并且 `_strip_identity_keys` 会把调用方 metadata 里的 identity 键剥离（issue #4490 / #6277 / #6655 的补丁）——**作用域只能由 entity 参数决定，不能由 metadata 顺手塞**。id 还要过 `_validate_and_trim_entity_id`：空串、纯空白、含内部空格一律拒。

### 写入路径

`add()` → `_add_to_vector_store()`，是一条编号流水线（源码注释里就写着 `# Phase 0` … `# Phase 7`）：

1. **Phase 0 上下文**：`_build_session_scope(filters)` 生成确定性 scope 串（`agent_id=..&run_id=..&user_id=..`，值做 `%25/%26/%3D` 转义），据此取 `db.get_last_messages(scope, limit=10)`——最近的原始消息，用来解代词。
2. **Phase 1 取候选**：用**本批消息本身**去向量库 top_k=10 取"已存在的相关记忆"，然后 **UUID → 整数重映射**（`uuid_mapping[str(idx)] = mem.id`，注释写的是 anti-hallucination）。
3. **Phase 2 单次 LLM 抽取**：`ADDITIVE_EXTRACTION_PROMPT` + `generate_additive_extraction_prompt(...)`，一次调用，`response_format={"type":"json_object"}`。**失败不再静默返回 `[]`**——源码注释直说旧版把"LLM 挂了"和"没抽到东西"混成同一个空列表，现在抛 `LLMError`。
4. **Phase 3 批量 embed**：`embed_batch(mem_texts)`，失败逐条降级。
5. **Phase 4 逐条 CPU 加工**：为每条算 `text_lemmatized`、`hash`、`created_at/updated_at`，组装 payload。
6. **Phase 5 去重**：**只有 `md5(text)` 一层**。`existing_hashes`（来自 Phase 1 的候选）+ 批内 `seen_hashes`，命中就 `continue`。**没有语义阈值合并，没有冲突判定**。
7. **Phase 6 批量落库**：整批 `vector_store.insert(...)`，失败逐条重试；一条都没进去则抛 `VectorStoreError`。注释说明：只有真正落库的记录才进 history / entity / 返回值——「被存储拒绝的记录绝不能回报成成功的 ADD」。
8. **Phase 7 实体链接**：`extract_entities_batch` → 归一化键（`" ".join(text.strip().lower().split())`）做**跨记忆全局去重** → 批量 embed → 先按归一化文本精确查，再语义查（`score >= 0.95` 才算同一个实体）→ 命中就并 `linked_memory_ids`（排序后写回），否则批量新建。

**抽取 prompt 的几个硬约定**（`mem0/configs/prompts.py › ADDITIVE_EXTRACTION_PROMPT`）：

- 唯一算子是 ADD；输出 `{id, text, attributed_to, linked_memory_ids}`。
- 每条要求"自包含、15–80 词、带观察日期"，且给了 4 类明确的反例：**No Echo Extraction**（助手复述用户的话不得二次抽取）、**No Within-Response Duplication**、**No Meta-Extraction**（存文档内容而不是"用户分享了文档"）、**No Detail Contamination**（新消息没提的细节不许从旧记忆里搬进来）。
- `Existing Memories` 章节只用于去重与链接，明确禁止从里面抽新记忆。
- `Observation Date` 是**唯一**的时间锚点，`Current Date` 另给——例 7 专门演示 "recently" 要按观察日（2022-01）解析，而不是按今天（2026-02）。
- 还有一条反"首话题支配"的 checklist：「10+ 条消息的对话通常该抽 5–15 条；少于 3 条就回去重读」。

**v2 的算子循环还在，但已不在主路径**：`DEFAULT_UPDATE_MEMORY_PROMPT`（ADD/UPDATE/DELETE/NONE，规则写得很细：语义相同取信息量更大的、矛盾才 DELETE）仍留在 `prompts.py`，但 `main.py` 的 import 列表里只有 `ADDITIVE_EXTRACTION_PROMPT` / `AGENT_CONTEXT_SUFFIX` / `PROCEDURAL_MEMORY_SYSTEM_PROMPT` / `generate_additive_extraction_prompt`。**写时冲突消解在 OSS 主路径上已经是死代码**；`update()` / `delete()` 退化为人工调用的 API（作用域不可改，见 `_strip_identity_keys`）。

### 检索路径

`search()` → `_search_vector_store()`，9 步（源码 `# Step 1..9`）：

1. 预处理：`lemmatize_for_bm25(query)` + `extract_entities(query)`。
2. **过取**：`internal_limit = max(top_k * 4, 60)`，同时打向量 `search` 和 `keyword_search`（BM25）。
3. **BM25 归一化不是 min-max，是 sigmoid**（`mem0/utils/scoring.py`）：
   `normalize_bm25(raw, midpoint, steepness) = 1/(1+exp(-steepness*(raw-midpoint)))`，
   `get_bm25_params` 按 query 词数选参数：≤3 词 `(5.0, 0.7)`、≤6 `(7.0, 0.6)`、≤9 `(9.0, 0.5)`、≤15 `(10.0, 0.5)`、更多 `(12.0, 0.5)`。理由写在 docstring：长 query 原始 BM25 天然更高。
4. **实体加成**：query 实体最大 8 个去重 → 实体库 top_k=500 → 只认 `score >= 0.5` → 命中实体的 `linked_memory_ids` 全部加权：
   `boost = similarity * ENTITY_BOOST_WEIGHT(0.5) * 1/(1 + 0.001*(n_linked-1)^2)`——**关联记忆越多的实体，单条加成越被稀释**（防"热门实体垄断榜单"）。多实体命中同一记忆取 `max`。
5. **融合是加性的，且分母动态**：`raw = semantic + bm25 + entity_boost`，`combined = min(raw / max_possible, 1.0)`，`max_possible = 1.0 + (有 bm25 ? 1.0 : 0) + (有 entity ? 0.5 : 0)`。**threshold 在融合之前卡 semantic**——「candidates 低于阈值即排除，即便 BM25/entity 能把它拉上来」。
6. 可选的 cross-encoder rerank（`rerank=True` 且配了 reranker，失败只 warn 并保留原序）。
7. 过期过滤在**读路径**：`_payload_is_expired(payload)` 比 `expiration_date` 与 UTC 今天，`show_expired=False` 时直接跳过。
8. `top_k` 截断（默认 20，README 的 benchmark 用 `top_200` 预算）。
9. 返回时把 `user_id/agent_id/run_id/actor_id/role/attributed_to/expiration_date` 提到顶层，其余 payload 塞进 `metadata`；`explain=True` 会带 `score_details`（各分项 + max_possible + threshold）。

注意：**"graph memory" 不在 OSS 检索主路径里**。论文的 Mem0^g（Neo4j 实体关系图）是另一条支线；现行 OSS 的 embedding 检索只用了实体加成这一层轻量图。

### 遗忘与时间

- **TTL 有，但只到"日期"粒度**：`expiration_date` 是 payload 字段，`_normalize_expiration_date` 只接受 `YYYY-MM-DD`（`datetime`/`date`/`str` 三种入参，其余抛错），**过期只在检索时过滤，不删除**（`_payload_is_expired` 是唯一消费点）。`update(expiration_date=None)` 可清除。
- **衰减（decay）在 OSS 里是关着的**：`project.update(decay=True)` 直接 `raise ValueError(get_decay_feature_error_message(...))`，`_OSSProject` 是个 stub。→ 时间衰减是**平台专有**。`detect_decay_usage_from_*` 那堆 notice 只是"你用了平台功能"的提示。
- 没有 `confidence` 衰减、没有 `last_used` 权重、没有"用进废退"。时间维度全靠 `created_at` + 抽取时写进正文的日期短语（temporal reasoning 靠 prompt，不靠模型）。

### 关键机制（5 条）

**M1. 单趟 ADD-only 抽取 + 写时只做精确哈希去重**
- 机制：一次 LLM 调用抽事实；写入只按 `md5(text)` 去重；不做 UPDATE/DELETE，历史靠追加累积。
- 解决什么：v2 的"LLM 决定 UPDATE 还是 DELETE"是**最贵的失败点**——它要一次额外 LLM 调用、要 LLM 精确回指 id、写错就是不可逆的数据丢失。改成 append-only 后：写入不可破坏（幂等、可重放），冲突下沉到读时多信号融合。
- memcore 能不能用：**已经在精神上一致**（`add()` 只做 exact/path/fuzzy 去重，`memcore.py:638`），但 memcore 还多背了一个 `consolidate()` 的 v2 式冲突合并（`memcore.py:2157`，默认 `dry_run=True`、`auto_retire=False`，注释记着"审计实测 60 条误删 56 条"）。→ 方向应当是**把 consolidate 降级成"体检报告"**，而不是让它变得更能自动合并。

**M2. BM25 用 sigmoid 归一化，参数随 query 长度自适应**
- 机制：`1/(1+exp(-k*(raw-m)))`，`(m,k)` 由 query 词数档位决定（5.0/0.7 … 12.0/0.5）。
- 解决什么：原始 BM25 无上界、随语料漂移。min-max 归一化更糟——它**依赖候选集**：只召回一条时，那条的 lexn 恒等于 1.0，于是"一句话把整库叫醒"。memcore 自己的注释就踩过这个坑（`memcore.py:1520`："批量归一化过的 lexn 会让'只有一条结果的短查询'显得极端匹配"），但它只是**绕过**（trigger 只看绝对量），归一化本身没修。
- memcore 能不能用：**能，改动很小**（`memcore.py:1453` 一行）。代价是需要把 `evidence` 闸门里 `lexn > 0.0` 改成绝对阈值——sigmoid 永远不会返回 0。

**M3. 抽取端产出 `linked_memory_ids`，写入端照单建链**
- 机制：抽事实的同一次 LLM 调用里就把"这条和哪条已有记忆相关"判了（同实体 / 偏好演进 / 后续事件 / 矛盾），输出已有记忆的 id；写入端直接落成边。prompt 里还专门警告："已有 '用户有条狗叫 Max' 不代表 Max 的一切都记过了"。
- 解决什么：语义链接从"事后按向量余弦 ≥ 阈值连边"变成"抽取时按语义判定连边"。余弦连边会把"模板相同、载荷不同"的记忆连成一片（这正是 memcore `consolidate` 误删的那类）。
- memcore 能不能用：**能**。`_auto_links()`（`memcore.py:888`）现在是 `min_sim=0.20 / max_links=4` 的余弦近邻，噪声很大；`add()` 加一个 `links=[...]` 参数直接 `link(rel='related')`，两条路并存即可，零迁移成本。

**M4. 实体作为一等公民，且加成按"实体热度"稀释**
- 机制：实体单独一套 collection，写入时归一化去重（精确优先、0.95 语义兜底），检索时按 query 实体回查 → `1/(1+0.001*(n-1)^2)` 稀释。
- 解决什么：① 实体名的大小写/空白差异会让同一实体分裂成多条；② 不做稀释时，一个被 500 条记忆引用的大实体（比如项目名）会给半库打分，等于没有信号。
- memcore 能不能用：**能，而且是修一个真 bug**——`entities` 表在 SCHEMA 里建了（`memcore.py:368`）、`_select_candidates` 里读了（`memcore.py:1379`）、`forget()` 里删了（`memcore.py:812`），但**全项目没有任何一处 INSERT**。即 `ent` 信号恒为空，是死代码。

**M5. 语义阈值是硬门，且在融合之前**
- 机制：`if semantic_score < threshold: continue`，然后才加 BM25/entity。
- 解决什么：防止"字面命中但语义无关"的条目靠 BM25 堆分冲进 top-k（典型：日志里的 `error`、代码里的 `class`）。
- memcore 能不能用：**能，但要小心**。memcore 用的是 512 维哈希 n-gram 向量，语义分辨力本来就弱，"措辞完全不同但相关"的召回率靠的是 `overlap`/`title` 兜底。硬门会一刀切掉这部分召回 → 必须配 `mem bench_locomo.py` / `tests_eval.py` 回归才知道值不值。

### 不要抄的部分

- **LLM 是硬依赖**：`Memory()` 构造即建 LLM client，`add(infer=True)` 必须调模型。memcore 的卖点是"标准库、零依赖、不走网络"。
- **向量库抽象层**：20+ 后端工厂 + `keyword_search` 能力探测 + 能力缺失时 warn。memcore 只有 SQLite，这层是纯负担。
- **双写一致性**（SQL 先写、Turbopuffer 后再写、失败按 `strict_mode` 决定是否抛、删除两边都要删）。memcore 单库单事务，引入这层只会引入一致性问题。
- **多租户 identity 剥离逻辑**（`_strip_identity_keys` / `_RUNTIME_FIELDS` / `_SENSITIVE_FIELDS_EXACT` / telemetry 打码）。单人单机不需要；但**"作用域只能由参数决定，不能由 metadata 塞"这条原则**值得抄（见 M3 的链接权限）。
- **平台专有功能**：`decay`、temporal `reference_date`、`expiration_date` 的调度清理，OSS 里全是 raise + notice。
- **benchmark 数字**：README 的 92.5 是平台栈（top_200 + 闭源优化）。拿它当基线对比 memcore 没有意义。

### 来源

- 仓库：https://github.com/mem0ai/mem0
- README（含 April 2026 新算法与 benchmark 声明）：https://raw.githubusercontent.com/mem0ai/mem0/main/README.md
- 写入主流程：https://raw.githubusercontent.com/mem0ai/mem0/main/mem0/memory/main.py （`_build_filters_and_metadata` / `_add_to_vector_store` / `_search_vector_store` / `_compute_entity_boosts`）
- 融合打分：https://raw.githubusercontent.com/mem0ai/mem0/main/mem0/utils/scoring.py （`get_bm25_params` / `normalize_bm25` / `score_and_rank`）
- 抽取与更新 prompt：https://raw.githubusercontent.com/mem0ai/mem0/main/mem0/configs/prompts.py （`ADDITIVE_EXTRACTION_PROMPT` / `DEFAULT_UPDATE_MEMORY_PROMPT`）
- 论文：https://arxiv.org/abs/2504.19413 （v2 的 ADD/UPDATE/DELETE/NOOP 与 26% 相对提升、91% p95 延迟下降）

---

## Letta（原 MemGPT）

### 定位 / 规模 / 许可证 / 时间线

- `letta-ai/letta`：**25,320 star / 2,665 fork / Apache-2.0**（GitHub 仓库页内嵌的 `stargazerCount`，本轮读取），`main` 分支最后提交 2026-10-18。
- **仓库结构在 2026 年发生过迁移，必须先说清**：`letta-ai/letta` 的 `main` README 现在只有一句话——「current source lives in `letta-ai/letta-code`」，而**带 memory blocks / archival memory / sleep-time agent 的 Python V1 API server 已经挪到 `archive` 分支**（"the retired Letta V1 API server"）。
  → 本轮的机制结论分两处取证：**概念与算法**读 `archive` 分支源码（那才是 MemGPT 血统的实现），**现行产品形态**读 `letta-code`（TypeScript harness，3,759 star）与 docs.letta.com。
- 论文 arXiv:2310.08560（2023-10，v2 2024-02）：把上下文窗口管理类比成操作系统的虚拟内存——"virtual context management"，靠 **memory tiers**（快/慢两级）+ **interrupts**（自中断控制流）实现"看起来很大的内存"。

### 核心数据结构

两套完全不同的记忆，靠"是否进上下文"区分：

**A. `Block`（core memory / memory block）** — 进系统提示，逐轮可见

```
Block{ id, label, value: str, limit: int = CORE_MEMORY_BLOCK_CHAR_LIMIT, description,
       read_only: bool, metadata, tags, ... }
```
- 见 `letta/schemas/block.py`。默认 label 有 `human`（`CORE_MEMORY_HUMAN_CHAR_LIMIT=20000`）、`persona`（`CORE_MEMORY_PERSONA_CHAR_LIMIT=20000`），通用上限 `CORE_MEMORY_BLOCK_CHAR_LIMIT=100000`（`letta/constants.py`）。
- 访问靠 **label 精确匹配**：`Memory.get_block(label)` 找不到就 `KeyError`，并且错误信息里**列出所有可用 label**（`letta/schemas/memory.py › get_block`）。
- `limit` 的强制点不在 `update_block_value()`（该函数只校验"必须是 str"和"label 必须存在"）——`未核实`具体校验层（应在工具执行层，本轮未读到）。
- **有版本链**：`block_manager.py` 里有 `BlockHistory` / `current_history_entry_id` / 按 sequence 的 undo/redo（`PROMPT_AFFECTING_BLOCK_FIELDS = {"description","label","limit","read_only","value"}`——改这些字段会重建系统提示）。

**B. `ArchivalPassage` / `SourcePassage`（archival memory）** — 不进上下文，靠工具检索

```
Passage{ id, text, embedding, embedding_config, tags: List[str], metadata_, created_at,
         is_deleted, archive_id(agent 记忆) | source_id + file_id + file_name(文件记忆) }
```
- 见 `letta/services/passage_manager.py`。**archival 与 source 是两种表**，互斥（`create_agent_passage_async` 里 `if pydantic_passage.source_id: raise`）。
- `tags` **双写**：JSON 列 + `PassageTag` 关联表，注释写的是"junction table enables efficient DISTINCT queries and tag-based filtering"；`get_tag_counts_for_archive_async` 就是靠它做 `GROUP BY tag`。
- **不切块**：`insert_passage` 里 `text_chunks = [text]`，只有一行 `# TODO: check to make sure token count is okay`。一条 insert = 一条 passage。
- 每个 archive 可以各自选向量后端（pgvector / Turbopuffer / Pinecone），TPUF 要双写 SQL 与向量库。

**C. 现行形态（letta-code / MemFS）：记忆被投影成 git 仓库里的 Markdown 文件**
- 路径即寻址：`label=persona` → `persona.md`（YAML frontmatter: `name` / `description`）。
- **根目录文件每轮都进系统提示**；有自己 `MEMORY.md` 索引的**子目录默认不进上下文**，靠索引里的目录名/文件名当路标，按需再读。
- 每根目录下有 `MEMORY.md` 索引、`persona.md`、`human.md`、`reference/`、`skills/`。
- **默认没有向量索引**：关键词/语义检索要装 `memfs-search` mod（语义模式还要 QMD）。→ **检索从"相似度"退回到"结构 + 文件名"**。

### 写入路径

Letta 的写入不是"抽取事实"，而是**agent 直接编辑自己的记忆**，靠工具契约把关：

- **`memory_replace(label, old_string, new_string)`** — 定点替换，三条硬校验（`letta/functions/function_sets/base.py`）：
  1. `old_string` 必须**逐字出现**（先 `expandtabs()` 归一制表符）；0 次出现 → `ValueError("... did not appear verbatim ...")`；**>1 次出现也报错**，并把命中的行号列出来（"Please ensure it is unique"）。
  2. 参数里不许带显示用的行号前缀（`\nLine \d+: ` 或 `N→`），带了直接拒；`new_string` 同样检查。
  3. docstring 里明确"不要拿它替换整块内容"——那是 `memory_rethink` 的活。
- **`memory_insert(label, new_string, insert_line=-1)`** — 按行插入，`insert_line` 必须落在 `[0, n_lines]` 或 `-1`，越界报错。同样禁行号前缀。
- **`memory_rethink(label, new_memory)` / `rethink_memory`** — 整块重写，用于"压缩/重组"；label 不存在则新建 Block。配套 `memory_finish_edits()` 作为"我改完了"的显式结束标记。
- **`memory_apply_patch(label, patch)`** — codex 风格的统一 diff，支持多块：`*** Add Block: <label>` / `*** Delete Block:` / `*** Update Block:` / `*** Move to:`，`+/-/ ` 行 + `@@` 分隔。**一次调用原子地改多块**。
- 旧口径 `core_memory_append` / `core_memory_replace`（MemGPT 原版）：`replace` 里 `if old_content not in current_value: raise ValueError`——同样要求逐字存在，但**不要求唯一**。
- `read_only=True` 的块禁写，错误串 `READ_ONLY_BLOCK_EDIT_ERROR`。

**archival 写入**由 agent 主动调 `archival_memory_insert(content, tags)`，docstring 直接给了写作规范："存自包含的事实或摘要，不要存对话碎片"。

### 检索路径

- **core memory**：不用检索。它就在系统提示里，每轮全量可见。代价是每轮都付 token，所以有 `limit`。
- **archival memory**：`archival_memory_search(query, tags, tag_match_mode="any"|"all", top_k=10, start_datetime, end_datetime)` —— 语义相似度 + **标签过滤（任意/全部）** + **时间区间过滤**，`RETRIEVAL_QUERY_DEFAULT_PAGE_SIZE = 5`。
  → 一个工具调用里同时给了三条正交过滤轴：语义、类别、时间。
- **conversation_search(query, roles, limit, start_date, end_date)**：原始消息的混合检索（text + semantic），**按角色过滤**。
- **MemFS 现行形态**：`MEMORY.md` 路标 + 普通文件 grep/read；语义是可选的 mod。官方 doc 明说"Agents find memory in its Markdown files with normal file-search and read tools"。

### 遗忘与时间

Letta 的"遗忘"不是 TTL，是**上下文驱逐 + 主动抢救**：

- **驱逐触发**：`SUMMARIZATION_TRIGGER_MULTIPLIER = 0.9`（到上下文窗口 90% 触发，注释："using instead of 1.0 to avoid 'too many tokens in prompt' fallbacks"）。
- **驱逐前先告警**：`MESSAGE_SUMMARY_WARNING_STR` —— 「对话即将达到最大长度并被裁剪 …… 如果有任何重要信息想保存，**现在立刻**调用 core_memory_append / core_memory_replace / archival_memory_insert」。这是**把"抢救"责任交给 agent 自己**，而不是系统替它压缩。
- **驱逐策略两种**（`letta/services/summarizer/summarizer.py`）：
  - `STATIC_MESSAGE_BUFFER`：留最后 `message_buffer_min` 条，前面的全删，异步交给 summarizer agent 归纳。
  - `PARTIAL_EVICT_MESSAGE_BUFFER`：按 `partial_evict_summarizer_percentage=0.30` 砍掉约 30%，把归纳结果作为**一条 user 消息插在 index 1**（index 0 是 system），形成递归摘要。
- **裁剪算法**（`middle_truncate_text`）：按 UTF-8 **字节**预算，保留头 30% + 尾 30%，**丢掉中间**，并插入显式标记 `[TRUNCATED: dropped N middle chars due to context budget]`，标记本身也计入预算。
- **没有 TTL / 没有 activation 衰减 / 没有"沉睡"**。`archival_memory_search` 是永久的（docstring：「Information stored here persists indefinitely」），只靠标签和时间区间收窄。
- **记忆仓库有容量上限**：云上 Free 5 MiB / Pro 50 MiB per memory repository（按快照算，不含 git 历史）。

### 关键机制（5 条）

**L1. 定点编辑必须"逐字且唯一"，否则报错而不是猜**
- 机制：`memory_replace` 对 `old_string` 做 0 次/多次双重拒绝，报错时列出歧义行号；参数里的显示行号前缀一律拒绝。
- 解决什么：LLM 改记忆最典型的两种坏写——**幻觉出一段不存在的原文**（0 次匹配）和**改到了不该改的那一处**（多次匹配）。这两种在"整体覆盖"式写入里都会静默成功，把一块好记忆改烂。逐字+唯一把失败变成**可见的报错**，模型下一次能自己纠正。
- memcore 能不能用：**能，而且这是最该抄的一条**。`update()`（`memcore.py:731`）目前是"给什么字段就整体覆盖什么字段"，没有定点编辑。加 `old/new` 模式即可（详见落点表）。

**L2. 三种写法按"改动规模"分工，而不是一种写入打天下**
- 机制：`memory_replace`（改一句）/ `memory_insert`（加几行）/ `memory_rethink`（整块重写）/ `memory_apply_patch`（多块原子）。
- 解决什么：写操作粒度错配。用小改动的工具去做大重构（replace 替整块）会丢信息；用大重构的工具做小改动（rethink 整块）会顺手改坏没让它改的部分。docstring 里互相点名"这不是你该用的工具"。
- memcore 能不能用：**部分能**。memcore 的 `add` / `update` / `consolidate` 已经大致对应"追加统计 / 字段覆盖 / 事后合并"，缺的是**定点替换**和**原子多改**。

**L3. 后台整理 agent 独占写权，前台 agent 只有读权**
- 机制：sleep-time 开启时，前台工具的集合是 `BASE_SLEEPTIME_CHAT_TOOLS = [send_message, conversation_search, archival_memory_search]`——**一个写记忆的工具都没有**；后台 agent 才有 `BASE_SLEEPTIME_TOOLS = [memory_replace, memory_insert, memory_rethink, memory_finish_edits]`。
- 解决什么：并发写冲突 + 前台为了"记住"而打断对话流。回答问题的进程和整理记忆的进程**职责分离**，写只发生在一个串行通道里。
- memcore 能不能用：**能，但要换形态**。memcore 没有并发 agent；可借的是"写入口收敛"——`learn` / `consolidate` 是仅有的两条自动写路径，其余靠人。真正的落点是**给后台整理加水位线**（见 L4）。

**L4. 后台整理靠"轮次取模 + 消息水位线"增量推进**
- 机制（`letta/groups/sleeptime_multi_agent_v2.py`）：每次前台 step 后 `turns_counter += 1`，`turns_counter % sleeptime_agent_frequency == 0` 才跑后台；后台任务先 `get_last_processed_message_id_and_update` 拿到上次处理到哪，再用 `list_messages(after=watermark, before=first_new)` 拼出**恰好没处理过的那段** transcript；每次跑都是一个 `Job`（running / completed / failed，失败带 error 落库）。
- 解决什么：全量重扫（越跑越慢、越跑越贵）和重复处理（同一段对话被归纳两遍，记忆里出现两份互相矛盾的摘要）。
- memcore 能不能用：**能**，`consolidate()`（`memcore.py:2157`）现在每次都按 `updated_at DESC LIMIT 400` 重扫，天然会反复咀嚼同一批旧条目。

**L5. 上下文预算用"路标"而不是"相似度"来分层**
- 机制（MemFS）：根目录文件常驻系统提示；**子目录只要有自己的 `MEMORY.md` 就不进上下文**，agent 看到的只是索引里的一行目录名，需要时再按路径打开。文档原话："directory and file names act as signposts the agent follows to read the right file. This keeps the active context lean while preserving deeper reference material."
- 解决什么：把"检索"问题降级成"导航"问题。相似度检索会漏（措辞不同就召回不到），而结构索引不会漏——你只要知道它在哪个目录。代价是**组织记忆的人要维护目录结构**。
- memcore 能不能用：**能**，而且 `brief()`（`memcore.py:1685`）现在正好是反例：预算耗尽就 `break`，被截掉的区块**连一行提示都不留**，读者不知道"还有东西没显示"。

### 不要抄的部分

- **git-backed MemFS**：把记忆做成 git 仓库（commit / push / worktree / 冲突解决 / `memory_repo:lock` 60s TTL）。版本能力很强，但要引入 git 运行时、冲突语义、云端同步——和 memcore"单文件 SQLite + append-only journal"的设计正面冲突。
- **多 agent / group / actor / organization 多租户**：`ArchivesAgents` 关联表、`BlocksAgents`、`org_id` 贯穿每张表。单人单机是纯粹的复杂度税。
- **LLM summarizer 作为上下文管理核心**：memcore 零依赖、无模型，这条路走不通（也不该走——它的"雾化"是本地确定性的替代方案）。
- **每块 100k 字符上限**这种"给了上限但不强制"的设计：`Block.limit` 是声明字段，`update_block_value` 并不校验，真正超限行为在别处（未核实）。抄一个不落地的上限没有意义。
- **per-archive 向量后端选择 + 双写**：pgvector/Turbopuffer/Pinecone 的可插拔，是给多租户云准备的。

### 来源

- 仓库（现为壳，指向 letta-code）：https://github.com/letta-ai/letta
- 现行 harness：https://github.com/letta-ai/letta-code ；README：https://raw.githubusercontent.com/letta-ai/letta-code/main/README.md （`/sleeptime` = dreaming、`/palace`、`/doctor`、MemFS、subagents）
- 记忆工具实现（V1）：https://raw.githubusercontent.com/letta-ai/letta/archive/letta/functions/function_sets/base.py （`memory_replace` / `memory_insert` / `memory_rethink` / `memory_apply_patch` / `archival_memory_insert|search`）
- 常量与预算：https://raw.githubusercontent.com/letta-ai/letta/archive/letta/constants.py （`CORE_MEMORY_BLOCK_CHAR_LIMIT` / `SUMMARIZATION_TRIGGER_MULTIPLIER` / `MESSAGE_SUMMARY_WARNING_STR` / `BASE_SLEEPTIME_TOOLS`）
- Block / Memory 结构：https://raw.githubusercontent.com/letta-ai/letta/archive/letta/schemas/block.py 、https://raw.githubusercontent.com/letta-ai/letta/archive/letta/schemas/memory.py
- archival 存储：https://raw.githubusercontent.com/letta-ai/letta/archive/letta/services/passage_manager.py
- 上下文驱逐：https://raw.githubusercontent.com/letta-ai/letta/archive/letta/services/summarizer/summarizer.py （`middle_truncate_text` / `_partial_evict_buffer_summarization`）
- sleep-time 调度：https://raw.githubusercontent.com/letta-ai/letta/archive/letta/groups/sleeptime_multi_agent_v2.py
- 概念文档：https://docs.letta.com/concepts/memfs 、https://docs.letta.com/configuration/memory
- 论文（MemGPT）：https://arxiv.org/abs/2310.08560

---

## 跨项目对照表

| 维度 | Mem0（现行 OSS） | Letta（V1 archive / 现行 MemFS） | memcore 现状 |
|---|---|---|---|
| 记忆单元 | 一条事实（自包含句，15–80 词） | Block（整块文本）+ Passage（整段） | 一条 memory（title+body，10 种 kind） |
| 作用域 | `user_id`/`agent_id`/`run_id`(+`actor_id`) 硬约束，metadata 不许塞 | `archive_id`（agent）/ `source_id`（文件）/ 多租户 `org_id` | `project` + `scope` + `kind` + `tags` |
| 写入算子 | **只有 ADD**（v2 的 ADD/UPDATE/DELETE/NOOP 已成死代码） | `replace` / `insert` / `rethink` / `apply_patch`（agent 自己编辑） | `add` / `update`（整字段覆盖）/ `retire` / `consolidate`（离线） |
| 去重 | 写时 `md5(text)` + 实体归一化精确/0.95 语义 | 无自动去重（agent 自己判断；`/doctor` 事后审计） | exact hash + artifact path + fuzzy 0.965 余弦 |
| 冲突 | 不做（冲突变成并存，读时融合排序） | 不做（靠 rethink 重写整块） | `consolidate()` 报 `merge`/`retire`，默认只报不动（`auto_retire=False`） |
| 检索 | 向量 + BM25 + 实体加成，加性融合，`threshold` 先卡语义 | archival：语义 + tags(any/all) + 时间区间；core：不检索，常驻；MemFS：结构导航 | FTS5 trigram(bm25) + 哈希向量 + simhash + tag/entity + 标题 bigram，加权和 + MMR |
| 归一化 | BM25 → sigmoid（按 query 长度选 m/k），有界 | —（无 BM25） | BM25 → **min-max**（依赖候选集，见 `memcore.py:1453`） |
| 时间 | `expiration_date` 读路径过滤；衰减是平台专有 | 驱逐触发 + 驱逐前告警；无 TTL | `ttl_days` + `gc()`；`valid_from/valid_to` + `as_of` 时间旅行；activation 半衰期 |
| "忘记"的机制 | 过期不返回（数据还在） | 上下文驱逐 + 递归摘要（数据还在 archival） | activation 四档（清醒/模糊/沉睡/深度沉睡）+ 雾化 + `wake()` |
| 上下文预算 | `top_k`（bench 用 top_200）；动态 `max_possible` 分母 | `Block.limit`；summarizer 头尾各 30% + 截断标记；路标分层 | `est_tokens()` + `recall(budget_tokens)` + `brief(budget_tokens)`，**截断时静默** |
| 版本/审计 | `history` 表（ADD/UPDATE/DELETE 事件链） | `BlockHistory` + undo/redo；git commit | `journal.jsonl`（append-only）+ `supersedes` 链 + `history()`；**无回滚命令** |
| 依赖 | LLM + embedding + 向量库 | Python/TypeScript 栈 + DB + git（MemFS） | **Python 标准库，单文件 SQLite** |
| 可移植性 | 中（融合打分与 prompt 可搬，运行时不可搬） | 中（编辑契约可搬，存储模型不可搬） | — |

---

## 对 memcore 的具体落点

前提：下面每条都**只动现有结构**（SQLite 表、`_select_candidates` 的五路信号、`activation` 四档、journal、links 图），不引入向量数据库、不引入 LLM 依赖、不改数据格式的语义。

### 高优先（低成本、修的是真问题）

| # | 落点 | 改法（一句话） | 工作量 | 风险 |
|---|---|---|---|---|
| 1 | `memcore.py:1453`（`search()` 里的 `lexn`） | 把 BM25 的 min-max 归一化换成 Mem0 的 sigmoid（`1/(1+exp(-k*(raw-m)))`，`(m,k)` 按 query 词数取 5.0/0.7 … 12.0/0.5），同时把 `evidence` 里的 `lexn > 0.0` 改成 `lexn > 0.5` | **S** | 有界归一化会改变排序分布 → 必须复跑 `tests_eval.py`（12/12 基线）与 `bench_locomo.py`；`evidence` 判据不同步改会让**全部**候选都算"有词法证据" |
| 2 | `memcore.py:638` `add()` + `memcore.py:1379` 读取点 | 让 `entities` 表真正有写入：`add(entities=[...])` 落库；至少给 `learn()` 自动抽"文件名/路径词"，让现在恒空的 `ent` 信号活起来 | **M**（抽取规则）+ S（落库） | 抽取噪声会被 `+0.05*(tag+ent)` 直接放大 → 必须同时做 #3 的稀释，否则等于给噪声加分 |
| 3 | `memcore.py:1490`（`tag/entity bonus`） | 实体加成按"该实体已连记忆数"稀释：`0.05 * 1/(1+0.001*(n-1)^2)`，取代固定 `+0.05` | **S** | 需要一次 `SELECT COUNT(*) FROM entities WHERE name=?`（有 `idx_entities_name`，可接受）；n 很小时几乎无影响 |
| 4 | `memcore.py:731` `update()` | 加定点编辑模式：`update(mid, old="...", new="...")`，对 `norm_text` 后的 body 做逐字匹配，**0 次或 >1 次都抛错并列出歧义行**（Letta `memory_replace` 契约） | **S** | 库里正文已过 `norm_text`（压空白/全角半角），逐字匹配必须对归一化后文本做，否则日常失配；报错语义要给 CLI 友好文案，别让 `mem patch` 脚本半途炸 |
| 5 | `memcore.py:670`（`add()` 的 exact dedupe 段） | 加 `links=[mid,...]` 参数，写入时直接 `link(...,'related')`，让"抽取端判定的相关性"能落地，和 `_auto_links()` 并存 | **S** | 外来 id 可能不存在或跨 project → 复用 `link()` 的存在性校验（`memcore.py:914`），并拒绝跨 project 建边（避免 preflight 串味） |
| 6 | `memcore.py:1685` `brief()` 的 `sect()` | 预算耗尽时不再静默 `break`：对未展示的区块输出一行路标（`## 未展示 · 决策 12 条 → mem recall "..."`），学 MemFS 的 signpost | **S** | 简报会多几行；要保证路标本身不吃掉预算（先预留 N token 给尾部） |

### 中优先（要改流程，收益明确但要配回归）

| # | 落点 | 改法（一句话） | 工作量 | 风险 |
|---|---|---|---|---|
| 7 | `memcore.py:2157` `consolidate()` | 加水位线：`meta` 表记 `consolidate_at`，只对 `updated_at > watermark` 的条目发起"新 × 旧"比对，跑完推进水位 | **S** | 只扫新条目会漏"两条旧条目互相冲突"→ 保留一个低频全量模式（例如 `--full`），水位线只用于日常增量 |
| 8 | `memcore.py:1101` `mask_body()` / `memcore.py:1579` `recall()` | 雾化/截断时带上"丢了多少"的显式标记（Letta `middle_truncate_text` 的 `[TRUNCATED: dropped N ...]`），只在 recall 输出行尾加，**不改库里正文** | **S** | 现在 `mask_text` 用 `░`/`…` 表达"记不清"，加数字标记可能让沉睡记忆读起来像 bug；建议只对 `awake/fuzzy` 加 |
| 9 | `memcore.py:638` `add()` 入口 | 写入前的"现在不存就没了"提示对齐 Letta `MESSAGE_SUMMARY_WARNING_STR`：`brief()` / `recall()` 若因预算丢了条目，末尾追加一行"本次有 N 条未展示（ID: ...），要用请 mem get" | **S** | 与 #6 同源，二者应合并成一处实现，别写两遍 |
| 10 | 新增 `mem doctor`（`mem` CLI + 复用 `memcore.stats()`/`graph_export()`） | 记忆体检：重复簇（复用 `consolidate(dry_run=True)`）、错位 kind、`entities` 空表告警、`MEMORY.md` 与库不一致、token 占用 Top-N（对齐 Letta `/doctor`） | **M** | 容易做成"又一套 consolidate" → 边界要定死：**doctor 只读不写**，所有修复动作仍走现有命令 |
| 11 | `memcore.py:731` `update()` + `memcore.py:1832` `_journal()` | 版本回滚：`update()` 前把旧 `title/body/tags` 快照写进 journal（现在 `_journal("update", ...)` 只记字段名、**不记旧值**），新增 `mem undo <ID>` 按最后一条 update 还原 | **M** | journal 体积增长（body 可能很长）→ 需设上限或只存 diff；与 `retire()` 释放 hash 的逻辑有交互（回滚一条已 retire 的记忆要重建 hash 唯一性） |

### 低优先 / 只记录不实施

| # | 落点 | 改法 | 工作量 | 风险 |
|---|---|---|---|---|
| 12 | `memcore.py:888` `_auto_links()` | 收紧余弦连边（`min_sim` 0.20 → 0.35）并用 `weight` 记录相似度，供 `graph_export` 过滤 | **S** | 边变少会让 `relink()` 的图变稀疏，而 `gc()` 靠"有边就不清"保护旧 episode（`memcore.py:1808`）→ 可能间接提高误删率，需一起调 |
| 13 | `memcore.py:1947` `_ingest_one()` | 会话灌入时记 `meta.attributed_to`（用户 / 助手自述），检索侧对"助手自述"降权 | **S** | 历史 episode 没有该字段 → 需默认值或回填；`body` 现在是"用户：…\n回应：…"拼串，拆分要改格式（老数据不一致） |
| 14 | 定点替换之外的"原子多改" | `mem patch` 子命令吃统一 diff，一次事务内改多条（Letta `memory_apply_patch`） | **L** | 收益取决于是否真有批量改需求；memcore 的自动化只有 `learn` 一条路，先做 #4 就够 |

### 明确不做

- **换向量库 / 引入真 embedding**：memcore 的零依赖是硬约束。Mem0 的融合逻辑可以搬，embedding 不能搬。
- **写时 LLM 冲突裁决**：Mem0 自己都从 v2 退回了 ADD-only；memcore 的 `_llm_judge`（`memcore.py:2067`）保持"可选、`dry_run` 默认"的现状即可。
- **git-backed 记忆仓库**：`journal.jsonl` + `MEMORY.md` 已经提供了可审计性，够用。

### 一句话总结三个项目各自的最优借鉴点

- **Mem0**：`sigmoid` 归一化（M2）+ 抽取期产出 `linked_memory_ids`（M3）——都小到可以一天落地。
- **Letta**：`memory_replace` 的"逐字且唯一否则报错"（L1）+ 后台整理的**水位线**（L4）——前者防写坏，后者防重复咀嚼。
- **memcore 自身最该修的是**：`entities` 表建了却从不写入（`memcore.py:368` 声明 / `:1379` 读取 / `:812` 删除，**零 INSERT**），即 `ent` 信号一直是空的。这是本轮唯一发现的**确定性 bug**，优先级高于以上所有借鉴项。
