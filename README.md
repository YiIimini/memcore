# memory-core · 我自己的长期记忆系统

[![tests](https://github.com/YiIimini/memcore/actions/workflows/test.yml/badge.svg)](https://github.com/YiIimini/memcore/actions/workflows/test.yml)
`private repo · git@github.com:YiIimini/memcore.git`

单机、零依赖、可解释。给智能体用的「记忆存储 + 调用」整套工具：
写入去重、混合检索、图谱关联、预算打包、历史会话灌入、MCP 工具暴露。

```
~/.dsh/memory-core/          代码
├── mem                      CLI 入口（Python 3.9+，仅标准库）
├── memcore.py               引擎：存储 / 索引 / 排序 / 打包 / 维护
├── mem_mcp.py               MCP stdio 服务：memory_search|recall|brief|add|get|stats
├── bench.py                 规模与延迟基准
├── tests_eval.py            真实语料上的中文改写检索评测
├── tests/test_memcore.py    18 个单元测试
├── SKILL.md                 使用纪律（何时查、怎么写）
└── tools/sessions_dump.js   解压 dsh 会话归档

~/.dsh/memory/               数据
├── memory.db                SQLite(WAL)：memories/tags/entities/links/vectors/FTS5
├── journal.jsonl            追加写日志（审计 / 恢复）
├── MEMORY.md                人类可读镜像（每次写入后重建）
├── BRIEF.md                 会话开场简报
└── config.json              权重与阈值（可调，不需改代码）
```

## 1. 数据模型

| kind | 用途 | 默认显著度 / 半衰期 |
|---|---|---|
| identity | 我是谁、运行环境 | 1.00 / 10 年 |
| preference | 用户长期口味与约束（建议 pin） | 0.90 / 2 年 |
| fact | 可验证事实、路径、端口 | 0.70 / 18 月 |
| decision | 决策 + 理由 + 被放弃的替代 | 0.80 / 8 月 |
| project | 项目状态、里程碑 | 0.85 / 4 月 |
| artifact | 产物文件与要点 | 0.60 / 6 月 |
| insight | 复用型经验 | 0.70 / 13 月 |
| todo | 未闭环 | 0.75 / 30 天 |
| episode | 会话片段（原话 + 回应） | 0.45 / 45 天 |
| note | 其它 | 0.50 / 6 月 |

每条记忆：`id, kind, title, body, project, tags, source, source_ref, created_at,
updated_at, last_used_at, use_count, salience, confidence, pinned, ttl_days,
hash, meta, sim`。`hash` 保证精确去重，`sim`（64 位 SimHash）做语义预筛。

## 2. 检索：五路信号 → 融合 → 去冗余 → 打包

```
query
 ├─ 词法：FTS5(trigram) + 中文 3-gram 滑窗 OR + BM25        权重 .38
 ├─ 语义：特征哈希向量(512d, 字/词 bi-gram+tri-gram) 余弦   权重 .16
 ├─ 覆盖：查询 n-gram 在正文的覆盖率（非对称，短查询不吃亏） 权重 .12
 ├─ 标题：查询 n-gram 在标题的覆盖率                        权重 .16
 └─ 先验：时近(按 kind 半衰期) + 显著度 + 复用次数 + pin     权重 .10/.08/.06/.10
        ↓  证据门（无任何词法/语义/覆盖证据的条目不参与，pin 也不例外）
        ↓  MMR 去冗余（SimHash 汉明距离 ≈ 余弦，快 50×）
        ↓  按 token 预算贪心打包 → 带 ID 的紧凑上下文块
```

- 中文不依赖分词器：3-gram 命中做召回，2-gram 覆盖率做精排。
- 短查询不因文档长而被稀释（覆盖率按查询侧归一）。
- `pinned` 只加权重，不越过证据门 —— 偏好条目不会污染无关检索。
- 结果带 `M00042` ID，可 `mem get` 取原文、`mem link` 建边。

## 3. 实测（本机，Python 3.9，无 GPU）

| 场景 | 结果 |
|---|---|
| 5000 条合成记忆：写入 | 2.7 s（≈0.54 ms/条，含向量+SimHash+FTS） |
| 5000 条：检索 p50 / p95 / p99 | 23 / 33 / 45 ms |
| 5000 条：唯一 ID 召回 Recall@3 | 100% |
| 真实库 12 条中文改写查询（语料扩充后） | Recall@1 11/12、**Recall@3 12/12**，平均 5 ms |
| 单元测试 | 24/24 通过 |
| LoCoMo 全量 1540 题（检索层） | Recall@10 **58.9%** · Recall@20 **66.2%** |
| 后台维护 | launchd `com.memcore.maintain`，每 30 分钟 ingestion+建边+gc，实测连续两次运行成功 |

体积：5000 条 ≈ 27 MB（向量 2 KB/条 + FTS 倒排）；48 条 ≈ 0.9 MB。

## 4. 用法

```bash
M=~/.dsh/memory-core/mem      # 已软链到 ~/.local/bin/mem

# 写
$M add -k decision -t "选 FTS5 做检索" -b "理由：零依赖/单文件/trigram 支持中文；放弃 ES（部署重）。"
$M add -k preference -t "短视频竖屏 1080x1920" -b "默认竖屏，横屏需明确要求。" --pin
$M link M00010 M00012 --rel produces
$M update M00010 --salience 0.95 --tags arch,search

# 读
$M recall "上次直播平台做到哪一步" --budget 900     # 预算打包的上下文
$M brief --budget 700                              # 开场胶囊：身份/偏好/项目/未闭环/决策
$M search "粒子特效" -n 8 --kind decision
$M get M00022
$M graph M00010 --depth 2
$M timeline --days 7

# 维护
$M retire M00010 --by M00021 --reason "结论被推翻"   # 关闭有效期（旧值仍可按时点查回）
$M history M00021                                   # 版本链：谁取代了谁
$M consolidate --llm        # 查重复/矛盾并合并（dry-run；--apply 落库）
$M relink                   # 重建 related 语义边
$M ingest          # 增量灌入 ~/.dsh/sessions（幂等、过滤系统注入噪声）
$M gc --dry-run    # 过期/冷数据处理
$M reindex         # 重建 FTS + 向量 + SimHash
$M doctor          # 自检
$M export          # 重建 MEMORY.md
$M stats --json
```

## 5. 三种调用形态

1. **CLI**（永远可用）：`mem recall ...`，就是普通 bash 调用。
2. **MCP 工具**（原生 tool calling）：
   ```json
   {"mcpServers": {"memory": {"command": "python3",
     "args": ["/Users/x/.dsh/memory-core/mem_mcp.py"]}}}
   ```
   暴露 `memory_search / memory_recall / memory_brief / memory_add / memory_get / memory_stats`。
3. **MCP 原生工具**（已装进 desktop profile）：`mcp__memory__memory_search / _recall / _brief / _add / _get / _stats`，
   bundle 包在 `~/.dsh/memory-core/bundle-mcp`，由 `@deepseek-ai/dsh-mcp-client` 以 stdio 拉起 `mem_mcp.py`，进程常驻。
4. **常驻协议**：`~/.dsh/prompt-inject.md` 末尾已注入 MEMORY PROTOCOL，
   让"遇到历史问题先查记忆、完成工作立刻沉淀"成为默认行为，不依赖我临时想起。

## 6. 自动化

- `mem ingest` 解析 `session.v4.jsonl.zstd`：抽用户意图（过滤 runtime-context /
  system-reminder / job 通知等注入噪声）、生成 episode、每会话一份"意图序列"回顾、
  仅从写类工具（write/edit/apply_patch…）提取产物路径；按 mtime 幂等，重复跑不产生副本。
- 写入即重建 `MEMORY.md`；`journal.jsonl` 保留全部变更，可审计。
- `gc`：TTL 过期 + 120 天未被复用的 episode 自动清理。
- 自身不入库：`~/.dsh/memory-core`、`~/.dsh/memory` 下的文件不会生成产物记忆，
  避免工具文档里的示例文本污染检索。

## 7. 设计取舍

- **为什么不是向量数据库**：512 维特征哈希 + SimHash 预筛在本机 5k 条下 22 ms，
  零依赖、可离线、可解释（每分项都打印）；需要更强语义时换 `embed()` 即可，
  接口不变。
- **为什么本地**：OpenViking（`mem*` MCP）在 1933 端口未起时不可用；本系统不依赖网络，
  服务恢复后可用 `mem export --jsonl` 推送同步。
- **为什么一定要证据门**：没有它，pin 的身份/偏好条目会占据每次检索的预算，
  看起来"相关"实则噪声；实测该门把无关命中从 9 条降到 3 条。
- **为什么 MMR 用 SimHash**：全量余弦做多样性在 3k 条时单次检索 >300 ms；
  64 位汉明近似后同规模 15 ms，排序质量无可感知差异。

## 8. 已知边界

- 语义向量是统计近似，不做同义改写（"离职/粒子"这类错字靠词法与覆盖率兜底，实测有效）。
- 单写入者模型：并发写由 SQLite WAL 串行化，未做多进程合并冲突处理。
- 未内建加密；`~/.dsh/memory` 与 dsh 会话同权限级别。
- 时间衰减按 `updated_at`，不区分"被引用"带来的时效刷新（复用会提升显著度）。


## 9. 时间与冲突（本轮新增）

- **有效期区间**：每条记忆有 `valid_from / valid_to / status`。
  `retire(A, by=B)` 关闭 A 的有效期并写入 `B --supersedes--> A`。
  `mem history B` 输出完整版本链；检索时 `as_of=<epoch>` 可回到任意时点
  （例：`mem search "机房位置" --as-of 2026-09-01`），默认只看当前有效。
- **冲突消解**：`consolidate()` 用 SimHash 预筛 + 余弦在同类记忆里找候选：
  - `cos ≥ 0.93` → 判为重复，保留较新/被 pin 的一条，旧的 `supersedes` 关窗；
  - `0.74 ≤ cos < 0.93` → 交给人或 LLM 裁判，只报告不动手；
  - `--llm` 时调用任意 OpenAI 兼容端点（`MEM_LLM_BASE/MEM_LLM_KEY/MEM_LLM_MODEL`），
    返回 `duplicate|contradicts|refines|unrelated` + 可选合并句；无 key 时静默降级为纯启发式。
- 写入时自动建边：SimHash 预筛 40 条 → 余弦 ≥0.20 取前 4 条，写 `related`（无向、去重、权重=相似度）。

## 10. 可视化（Obsidian 风格关系图 + 设置面板）

**常驻入口（不需要终端）**

- 双击桌面 **“记忆面板”**，或浏览器打开 **http://127.0.0.1:8790/**
- 由 launchd `com.memcore.ui` 常驻（`RunAtLoad` + `KeepAlive`），开机自起；
  端口被占会自动顺延到 8791–8799 并在 `~/.dsh/memory/ui-server.log` 记一行
- 应用副本在 `~/Applications/记忆面板.app`（可拖进 Dock）

```bash
mem ui                      # 生成 ~/.dsh/memory/graph.html（单文件，双击即开，数据内嵌）
mem ui --out ~/Desktop/mem.html --limit 1500
mem serve --port 8787 --open # 实时版：读活库 + JSON API
```

- 力导向布局（同步预热 240 tick，首帧即可读）、按类型着色、按度数/显著度定半径，pin 有光环；
- 左侧：搜索过滤（高亮命中并自动适配视野）、类型开关、项目下拉、重排/适配/标签/含失效；
- 右侧：点击节点看正文、标签、显著度、有效期状态与全部关联（可跳转）；
- `GET /api/graph|/api/memory/<id>|/api/search|/api/stats` 可直接被别的界面复用。
- 实现：单文件 HTML + Canvas 2D，无任何外部依赖/CDN，`file://` 也能跑。
- **右上角 ⚙ 设置面板**（`mem serve` 打开时可用，`file://` 为只读）：
  - 大模型裁判：Base URL / Model / API Key（密钥写 `secrets.json` 0600，不回显，只显示"已配置"）
  - 自动维护：开关 + 间隔（15/30/60/180 分钟），落盘为 launchd `com.memcore.maintain`
  - 隐私排除：项目名 / 路径前缀 / 关键词，命中即不灌入、不检索
  - 一键动作：立即维护、重建关系边、重建索引、导出 MEMORY.md
  - 状态行：记忆数、关系数、上次维护时间、后台任务是否运行

默认每天后台维护一次不需要终端：

```bash
mem serve --open     # 打开后点 ⚙ 即可全部配置
```

### 后台任务（命名规则：一眼能认出谁是谁）

| 名称（LaunchAgent Label） | 作用 | 位置 |
|---|---|---|
| **MemoryPanel** | 常驻面板服务（图表 + 设置），端口 8790，占用时自动顺延 | `~/Library/LaunchAgents/MemoryPanel.plist` |
| **MemoryMaintain** | 每 30 分钟入库/建边/GC/重建镜像，并 checkpoint WAL | `~/Library/LaunchAgents/MemoryMaintain.plist` |

日志：`~/.dsh/memory/MemoryPanel.log`、`~/.dsh/memory/MemoryMaintain.log`。

约定：**新增任何后台脚本/守护项，必须给可读名称（Label + 文件名同名），并在本表登记；不用的立刻删除**，
不留 `com.xxx.yyy` 这类看不出用途的条目。

## 11. LoCoMo 基准（检索层）

`bench_locomo.py`：每个 conversation 独立建库，逐轮对话入库（按 session 日期打时间戳），
问题检索 top-k，看 gold `dia_id` 是否命中。全量 1540 题（排除 446 道对抗题）：

| 类别 | 题数 | Recall@10 | Recall@20 | 答案出现在上下文 |
|---|---|---|---|---|
| single-hop | 841 | 65.4% | 71.9% | 40.4% |
| multi-hop | 282 | 41.5% | 53.5% | 11.3% |
| temporal | 321 | 66.4% | 71.3% | 5.0% |
| open-domain | 96 | 28.1% | 36.5% | 9.4% |
| **合计** | **1540** | **58.9%** | **66.2%** | **25.8%** |

单次检索 17–21 ms（含 10 个独立库的灌入，总耗时 73–80 s）。
注意口径：Mem0 等在 LoCoMo 上报告的是**端到端 QA 分**（LLM 生成后判分），
这里是**纯检索召回**，不可直接比较；本表用于自体回归，不是排行榜。
