# arcdream-hub 拆解：哪些能力值得搬进 memcore

> 对象：https://github.com/499596849-jpg/arcdream-hub（本地"数字中枢"，HTML/JS，MIT）
> 目的：不是评判它，是把**能补 memcore 短板的机制**挑出来。
> 结论先放前面：它能给 memcore 补的只有一块，但那一块很值 —— **本机文件索引**。

## 一、它是什么（读代码得出）

```
app/server.mjs      Node 单文件服务（127.0.0.1:8799），node:sqlite，零依赖
app/public/index.html  单文件 UI：星图（canvas）+ 聊天 + 搜索 + 设置
plugin/dsh-arc-hub/    DSH 插件：Host 工具 + Client 设置页/悬浮圆
install/install.ps1    Windows 安装向导
```

数据面很小：`config.json`（模式/工作区）、`model.json`（模型 Key）、`index/hub.db`（文件索引）、`persona/`（人设卡）。

## 二、逐项对照 memcore

| arcdream-hub 的能力 | memcore 现状 | 判定 |
|---|---|---|
| **工作区登记 + 文件索引**（名字/路径/正文） | 只有"记忆"索引；`_doc_head()` 只读产物路径的一小段正文，用于把文件内容塞进记忆 | **缺口，值得搬** |
| FTS5 正文检索 + snippet | 有 FTS5（mem_fts，trigram），但索引的是记忆正文，不是本机文件 | 机制可复用 |
| 增量索引（path,size,mtime） | 无 | **搬**（大目录全量重扫很蠢） |
| `/api/open` `/api/reveal` 由本机服务打开目录 | 无（只能 `mem get` 看路径） | **搬**：记忆里记了路径，应该能一键打开 |
| 工作区归属校验（`path.relative` + `realpath`） | 无文件读取入口，暂无越权面；加文件索引后**必须有** | 一起搬 |
| 模型多 provider（DeepSeek/豆包/OpenAI/自定义） | 已有 `llm.base/model/key`，本地 gateway 默认 | 不需要 |
| Key 加密（DPAPI / AES-GCM） | `secrets.json` 0600 | 不需要 |
| H5（`/m` 手机端） | 面板已自适应窄屏 | 不需要 |
| 星图（canvas 力导向，四层节点） | nebula 面板已是 3D 星云 + 状态生命周期 | 不需要，且 memcore 更强 |
| 人设卡加密 + 摘要工具 | 不在 memcore 职责内 | 不搬 |
| 安装向导（PowerShell） | memcore 走 CLI + launchd | 不搬 |

## 三、搬什么、怎么搬（落到 memcore 的函数级）

### 1. 文件索引子系统 `files` —— 主菜

- 表：`files(path PK, name, dir, ext, size, mtime, root, project, indexed_at)` +
  `files_fts(path UNINDEXED, name, content, tokenize='trigram')`。
- 只对**文本扩展名**且 ≤256KB 的文件建正文；其余只进 `files`（与 arcdream 的白名单同构）。
- **增量**：按 `(path, size, mtime)` 三元组比对，未变连正文都不读；删除在整轮结束后清扫
  （照抄它 v0.2.0 修过的"upsert + 收尾清扫"，别学它 v0.1.2 的"先删表再扫"）。
- 复用 memcore 自己的 FTS5/trigram、`_grams()`、`_chunks()`，不引新依赖。

### 2. 检索与打开

- `mem files search <q> [--mode name|content|all] [--ext] [--limit]`；
- 服务端 `/api/files/search`、`/api/files/status`、`POST /api/files/index`；
- `mem open <path>` / `GET /api/reveal?path=`：macOS `open -R`、Windows `explorer /select,`、Linux `xdg-open`。
- 面板右栏加「文件」页签：搜索 + 每条「打开目录」。

### 3. 安全边界（搬它的教训，别搬它的实现）

- 工作区归属判定用 `path.relative` + `realpath`（**不是** `startsWith`）——
  arcdream v0.1.2 就是被 `/work` 放行 `/work-evil` 撞过的。
- 读文件 ≤256KB、只读文本类、路径必须在登记根内。
- 打开目录只接受登记根内路径。

## 四、明确不搬的

- 它的星图/聊天/人设卡/安装器：memcore 已经有更合适的形态（3D 星云 + 生命周期、MCP、launchd）。
- 它的 `*` CORS / 无 Host 校验 / 无体上限：正好相反，这些是 memcore 早就修掉的坑。
