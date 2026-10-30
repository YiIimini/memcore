---
name: memory-core
description: >
  自己的长期记忆系统（memcore）。当需要回忆过去的会话/决策/偏好/产物、开工前拉取上下文、
  或把刚完成的工作沉淀成可复用记忆时使用。CLI: ~/.dsh/memory-core/mem
---

# memory-core · 自己的记忆

存储：`~/.dsh/memory/`（SQLite WAL + `journal.jsonl` + `MEMORY.md` 镜像）
代码：`~/.dsh/memory-core/`（`mem` CLI、`memcore.py` 引擎、`mem_mcp.py` MCP 服务）

## 何时用（不要凭印象回答历史问题）

| 触发 | 动作 |
|---|---|
| 用户提到"上次/之前/我们做过/继续" | `mem recall "<原话关键词>" --budget 900` |
| 新会话开始、接手陌生项目 | `mem brief --budget 600` |
| 需要精确条目、ID、原文 | `mem search "<词>" -n 8` → `mem get M00042` |
| 想知道"当时是怎么定的" | `mem search ... --as-of 2026-09-01` 或 `mem history <ID>` |
| 关系/全局结构 | `mem ui`（生成单文件关系图）或 `mem graph <ID>` |
| 做了决策 / 定下偏好 / 产出文件 / 留下 TODO | 立刻 `mem add`（见下） |
| 用户纠正你、给出长期有效约束 | `mem add -k preference ... --pin` |

## 写入规范（一次调用换下次省 10 次搜索）

```bash
M=~/.dsh/memory-core/mem
# 决策（含理由与替代方案，才可复用）
$M add -k decision -t "选 SQLite FTS5 做检索" \
  -b "理由：零依赖/单文件/trigram 支持中文子串；放弃 Elasticsearch（部署重）。影响：memcore 引擎定型。" \
  --tags memory,arch --project default-workspace
# 长期偏好（用户口味，需 pin）
$M add -k preference -t "视频要竖屏 1080x1920" -b "所有短视频默认竖屏；横屏只在明确要求时用。" --tags video --pin
# 事实 / 环境
$M add -k fact -t "OpenViking 端口" -b "http://localhost:1933，服务未起时 mem* 工具不可用，改用本地 memcore。" --tags infra
# 产物
$M add -k artifact -t "直播平台方案" -b "路径：.../直播实现方案.md；要点：粒子位图+GPU 放大。" --tags live
# 待办
$M add -k todo -t "补抖音适配" -b "斗鱼已通，抖音/快手走通用桥。" --tags live
```

结论被推翻时不要删，改关窗：

```bash
$M retire M00010 --by M00021 --reason "改用 B 方案"   # 旧值仍能按时点查回
$M history M00021                                    # 版本链
$M consolidate --llm                                 # 查重复/矛盾（--apply 落库）
$M ui                                                # 关系图，直观看关联
```

写完可 `mem link A B --rel depends-on` 建边；`mem graph A` 看邻域。

## 检索语义

分数 = 0.50·BM25 + 0.22·语义(哈希 n-gram 余弦) + 0.10·时近 + 0.10·显著度 + 0.08·复用 + pin 加成，
再走 MMR 去冗余，最后按 token 预算打包。中文查询自动切成 3-gram OR，无需分词。
无相关证据的条目会被过滤，所以 pin 的偏好不会淹没检索结果。

## 维护

```bash
$M ingest            # 增量导入 ~/.dsh/sessions 历史会话（幂等，带噪声过滤）
$M gc --dry-run      # 过期/冷数据体检
$M reindex           # 索引重建（向量与 FTS 不一致时）
$M doctor            # 自检
$M export            # 重建 MEMORY.md 人类可读镜像
```

## 配置与维护（优先可视化）

用户要改配置时，**开 `mem serve --open` 让他点 ⚙**，不要让他敲终端命令：

```bash
mem serve --open     # 设置面板：LLM Key / 自动维护开关与间隔 / 隐私排除 / 一键维护
```

## 纪律

1. 记忆是**证据**，不是猜测：只写可验证的事实、决策、偏好、路径。
2. 一条记忆一个主题；标题 ≤ 44 字，正文写"为什么 + 影响"。
3. 同主题更新优先 `mem update <ID>`，不要堆重复条目。
4. 检索结果为空就放宽（去掉 project/kind、加大 budget），仍空则说明确实没记忆过。
5. 用户偏好可视配置：凡是"配置/开关/密钥/排除项"，给页面入口，不给命令行作业。
