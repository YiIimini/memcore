# 记忆模糊 / 沉睡 · 数据契约（UI 与引擎的唯一约定）

引擎侧实现：`memcore.py`（状态推导 `memory_state()`）、`server.py`（HTTP 出口）。
UI 侧只需要吃下面这些字段，**不要自己重新定义阈值或状态名**。

## 1. 四种状态

| state | 中文 | 含义 | 检索行为 |
|---|---|---|---|
| `awake` | 清醒 | 激活度 ≥ 0.45 | 正常召回，正文完整 |
| `fuzzy` | 模糊 | 0.26 ≤ 激活度 < 0.45 | 仍可召回，但正文被雾化（首尾保留、中段遮罩），得分打折 |
| `dormant` | 沉睡 | 0.15 ≤ 激活度 < 0.26 | 只有强线索（触发强度 ≥ 0.30）才召回，召回即唤醒 |
| `deep` | 深度沉睡 | 激活度 < 0.15 | 需极强线索（触发强度 ≥ 0.55）或标签/实体精确命中才可能唤醒 |

**激活度 activation ∈ [0,1]** = 时间衰减(按 kind 半衰期) 0.52 + 显著度 0.20 + 复用(use_count) 0.12 + 唤醒权重 0.16，再叠加：
- `pinned=1` → 下限 0.80（永不沉睡）
- `sleep_override='dormant'`（人工按下的"让它沉睡"）→ 强制落进沉睡档
- `sleep_override='awake'`（人工"钉醒"）→ 抬到模糊档以上

**触发词（triggers）**：写在记忆 `meta.triggers` 里的暗号词。查询串里出现任何一条，
触发强度直接顶到 1.0，无论这记忆睡得多深都会被唤醒。界面上可以显示成「暗号：直播、推流」。

**唤醒权重 wake_weight** = 距上次唤醒的小时数按 72h 半衰期衰减 ×（0.8 + 0.07×(wake_count-1)），封顶 1。

## 2. `/api/graph`（GET，已存在）新增字段

`nodes[]` 每条新增（注意两个时间字段的区别）：
`age_days` = 这段记忆存在多久了（从写入算）；`since_touch_days` = 多久没被想起（写入/召回/唤醒里最近的一次）——
界面上"已沉睡 N 天"要显示 `since_touch_days`，不要用 `age_days`。

```json
{
  "state": "fuzzy",
  "state_label": "模糊",
  "activation": 0.371,
  "age_days": 41.2,
  "since_touch_days": 12.0,
  "halflife_days": 180.0,
  "wake_count": 2,
  "last_wake_at": 1762000000,
  "wake_weight": 0.28,
  "sleep_override": "",
  "next_state_in_days": 12.4,
  "triggers": ["直播", "推流"],
  "preview": "……（模糊态下 preview 已被引擎雾化，UI 直接显示即可）"
}
```

`meta` 新增：

```json
{
  "states": {"awake": 120, "fuzzy": 41, "dormant": 12, "deep": 4},
  "dormancy": {
    "enabled": true,
    "thresholds": {"fuzzy": 0.45, "dormant": 0.26, "deep": 0.15},
    "wake_halflife_hours": 72.0
  }
}
```

## 3. 新接口

### GET `/api/pitfalls?file=&limit=200`
雷区总表（`kind=insight` + `tags=pitfall,<文件名>`），面板「雷区」页签用：

```json
{"total": 5,
 "items": [{"id":"M00511","title":"…","file":"memcore.py","files":["memcore.py","memcore"],
            "what":"现象…","fix":"修法…","kind":"insight","state":"awake","state_label":"清醒",
            "activation":0.73,"updated_at":1762000000}],
 "files": [{"file":"memcore.py","count":3}]}
```

`file` 是主归属（第一个像路径的键）；`what`/`fix` 已从正文里拆好，UI 直接显示。

### GET `/api/preflight?paths=a,b&topic=&limit=10`
动手前查询：按文件名命中坑，再语义兜底；返回 `{text, items, keys}`，`items[]` 比 pitfalls 多 `pitfall`、`score`。

### GET `/api/sleepers?limit=60`
沉睡区列表（dormant + deep，按 activation 升序）：

```json
{
  "items": [{"id":"M00042","title":"…","kind":"fact","project":"p1","state":"deep",
             "state_label":"深度沉睡","activation":0.081,"age_days":311.0,"since_touch_days":42.0,
             "wake_count":0,"last_wake_at":0,"preview":"……","next_state_in_days":null}],
  "states": {"awake":120,"fuzzy":41,"dormant":12,"deep":4}
}
```

### GET `/api/stats`（已存在）新增
`by_state: {"awake":n,"fuzzy":n,"dormant":n,"deep":n}`。

### POST `/api/action`（已存在）新增 action
```json
{"name":"wake","id":"M00042"}      // 唤醒：wake_count+1，last_wake_at=now
{"name":"sleep","id":"M00042"}     // 让它沉睡：sleep_override='dormant'
{"name":"unsleep","id":"M00042"}   // 取消人工干预：sleep_override=''
```
返回 `{"ok":true,"result":{"id":"M00042","state":"awake","activation":0.55,...}}`。

### GET `/api/memory/<ID>`（已存在）新增字段
同 node 的状态字段；`body` 为**完整正文**（显式打开 = 看一眼就点亮，同时会触发唤醒）。
详情面板里正文是明文，不是雾化文本。

## 4. 视觉规范（星云面板）

- 清醒：原色，alpha 0.9。
- 模糊：原色向灰蓝（`#8FA3C8`）混 45%，alpha 降到 0.42，尺寸 ×0.86，外圈加一层静态雾环（呼吸周期 6s，幅度极小）。
- 沉睡：混灰 70%，alpha 0.20，尺寸 ×0.70，不发光（不加 halo）。
- 深度沉睡：混灰 85%，alpha 0.10，尺寸 ×0.55，画成断续的冷灰微点（抖动周期 14s，只为"还没死透"）。
- hover / 选中 / 搜索命中时状态色不改，只抬亮度 —— **可读性优先**。
- 左栏新增"状态"分组 chips：清醒 / 模糊 / 沉睡 / 深度沉睡，带计数，可多选过滤（与"节点类型"chip 同款样式，互不干扰）。
- 左栏"记忆体征"新增一条进度条：**清醒率**（awake/总），显示百分比。
- 右栏新增 tab：`沉睡`（数据来自 `/api/sleepers`），条目显示状态徽标 + 激活度 + 已沉睡天数 + `唤醒` 按钮。
- hover 气泡与详情面板都要显示：状态徽标、激活度、唤醒次数、最近唤醒时间（`从未` / `x 天前`）、`唤醒`/`沉睡` 两个操作按钮。

## 5. 文案

全部中文。状态名固定为：清醒 / 模糊 / 沉睡 / 深度沉睡。
不要出现 "fuzzy"、"dormant"、"deep" 等英文词（JSON 字段名除外）。
