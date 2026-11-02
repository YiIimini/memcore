#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memcore 硬检索评测集（hard eval）。

为什么要有这一份
----------------
``tests_eval.py`` 的 12 条已经饱和：Recall@1 卡在 10/12、Recall@3 卡在 11/12，
加 40 条近期干扰项后 additive 与 multiplicative 两种排序的 MRR 仍是 0.881 vs 0.880。
任何排序改动在这份尺子上都测不出差异，所以排序优化（BM25 归一化、实体通道、
乘子式相关性）之前，必须先有一份能分辨的样本。

这份评测集覆盖四类，每类都是「答案在库里真实存在且可验证」的条目：

* ``para``  近义改写：query 与目标记忆几乎不同词（口语化、指代、换说法）。
* ``alias`` 跨语言/别名：用英文名、项目代号、文件名去找中文标题。
* ``time``  时间错位：query 带「最早/最后/后来/隔天」等时间线索，正确项是
            时间上对的那条，而不是最相似的那条（recency 只有 0.10 权重，
             所以「最早那次」天然和打分方向相反）。
* ``dist``  干扰项：同一主题下 3~5 条相似记忆，只有一条是真正答案。

用法
----
    python3 tests_eval_hard.py                  # 默认 k=5，快照模式
    python3 tests_eval_hard.py --k 10
    python3 tests_eval_hard.py --json           # 机器可读
    python3 tests_eval_hard.py --verbose        # 每条附带 top-k 与期望
    python3 tests_eval_hard.py --live           # 直接打真库（会写入唤醒计数）
    python3 tests_eval_hard.py --home /path     # 指定 MEM_HOME

默认走**快照模式**：把 ``memory.db`` 用 SQLite backup API 复制到临时目录再检索。
原因见 docs/Eval-Hard.md——``search()`` 命中沉睡条目时会写 wake_count/last_wake_at，
直接在真库上跑会让下一轮的分数漂移，评测的尺子不能自己动。

退出码恒为 0：这是量尺，不是闸门。分数由 --json 消费方自己判断。
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memcore import Memory  # noqa: E402

# --------------------------------------------------------------------------
# 用例表
# --------------------------------------------------------------------------
# 字段：
#   q             query 原文
#   expect_ids    正确条目 id（优先，精确匹配）
#   expect_titles 兜底：标题子串（id 全 miss 时才用）
#   kind          四类之一：para / alias / time / dist
#   why           一句话说明：为什么这条是答案、别的条为什么不算
#
# 纪律：每条的正确项都已在库中核实存在（见 docs/Eval-Hard.md 的核查记录），
# 不写「库里没有答案」或「预期会失败」的用例。
CASES = [
    # ---------------- 近义改写 ----------------
    {
        "q": "自己搭一套记忆，不靠外面的服务",
        "expect_ids": ["M00049"],
        "expect_titles": ["自建本地记忆系统 memcore"],
        "kind": "para",
        "why": "问的是「为什么自研、放弃了什么」；M00049 正文写明放弃向量库/OpenViking 的理由，"
               "同题干扰 M00324 只是「怎么安装」、M00050 只是文档产物，都不含这个取舍。",
    },
    {
        "q": "按钮上的字怎么就没了",
        "expect_ids": ["M00477"],
        "expect_titles": ["界面配色：token 名字像配套不算数"],
        "kind": "para",
        "why": "症状式提问（字「没了」）指向配色血案那条 preference；M00471/M00481 是「看不清」的现象记录、"
               "M00459 是面板按钮位移，都不是「字消失」的根因与约束。",
    },
    {
        "q": "别让我手打模型名",
        "expect_ids": ["M00109"],
        "expect_titles": ["模型 id 用实时列表"],
        "kind": "para",
        "why": "要的是「模型 id 从 /v1/models 实时取、不写死」这条偏好；"
               "M00095 是「配置走可视化」的相邻偏好（同一诉求但不同对象），M00110 讲网关地址，都不是模型 id 政策。",
    },
    {
        "q": "浏览器那块用完要收掉",
        "expect_ids": ["M00354"],
        "expect_titles": ["ego lite 空间用完必须关闭"],
        "kind": "para",
        "why": "口语「收掉」= 关闭 TaskSpace；正确答案是那条硬性 preference。"
               "M00106 是「后台常驻项要命名并清理」（清理对象是 launchd，不是浏览器空间），语义最近但结论不同。",
    },
    {
        "q": "沉睡了还能被叫醒吗",
        "expect_ids": ["M00510"],
        "expect_titles": ["记忆生命周期四档"],
        "kind": "para",
        "why": "问的是唤醒机制与阈值，答案在四档生命周期的 decision（含阈值 0.45/0.26/0.15 与线索强度）；"
               "M00504 只是「用户提出这个需求」的原始消息，M00512 是「时钟不走」的 bug，都不描述唤醒规则。",
    },
    {
        "q": "踩过的坑：两个数字打架先查什么",
        "expect_ids": ["M00514"],
        "expect_titles": ["没核对全部口径就把对的结论判成错的"],
        "kind": "para",
        "why": "正确答案是「先查口径」的方法论那条；M00511 是同一事故的「现象+修法」（列集不一致），"
               "问「先查什么」应落到方法论。两条都带 pitfall 标签，query 必须自带「坑」字才会被放出来"
               "（引擎对雷区条目的门控，见 docs/Eval-Hard.md）。",
    },
    {
        "q": "审计里哪些条目才算值钱",
        "expect_ids": ["M00458"],
        "expect_titles": ["审计报告要按「可复现证据」排序"],
        "kind": "para",
        "why": "问的是「排序/取舍标准」，答案是「按可复现证据排」这条 insight；"
               "M00441/M00445/M00448 是各次审计的会话回顾，M00446/M00460 是报告产物，都是被排序的对象而非标准。",
    },
    {
        "q": "版本号该用 0 还是 1 开头",
        "expect_ids": ["M00519"],
        "expect_titles": ["memcore 版本走 0.x"],
        "kind": "para",
        "why": "正确答案是版本政策 preference（0.x，且五处一起改）；"
               "M00518 是「0.2.0 已推送」的产物记录（事实，不含政策），M00525 是「改成 0.*.*」的原始指令。",
    },
    {
        "q": "踩过的坑：别名实体其实没写进去",
        "expect_ids": ["M00532"],
        "expect_titles": ["entities 表是死表"],
        "kind": "para",
        "why": "问的是实体通道为什么没效果，答案是「entities 表没有 INSERT、0.05 权重白给」那条；"
               "M00534 是调研结论（提议做独立实体通道），是方案而非这次踩坑的实测结论。",
    },
    {
        "q": "评测样本不够的教训",
        "expect_ids": ["M00533"],
        "expect_titles": ["评测集饱和"],
        "kind": "para",
        "why": "答案是「12 条已饱和、排序改动测不出差异」这条 insight；"
               "top1 的 M00347 是拆解图不够详细的抱怨，与评测无关，正是这类改写题要顶掉的干扰。",
    },

    # ---------------- 跨语言 / 别名 ----------------
    {
        "q": "桌宠改直播的方案在哪",
        "expect_ids": ["M00052"],
        "expect_titles": ["dsh-pet-AI直播实现方案"],
        "kind": "alias",
        "why": "「桌宠」是 dsh-pet 的口语别名，正确答案是实现方案文档产物；"
               "M00005/M00006 是原始需求消息，M00058 是粒子直播平台的 README（另一个项目），M00407 是吞吞改造方案。",
    },
    {
        "q": "把 3d-printer-live 改成吞吞",
        "expect_ids": ["M00386"],
        "expect_titles": ["3d-printer-live 吞吞改造"],
        "kind": "alias",
        "why": "英文仓库名 → 中文改造记录的跨语言映射；答案那条 note 直接写了 tuntun-printer/ 的四个模块；"
               "M00397/M00398 是分析原仓库的消息，M00405 只是其中 tuntun-avatar-proxy.mjs 一个文件。",
    },
    {
        "q": "cEMI 报文那个模块",
        "expect_ids": ["M00278"],
        "expect_titles": ["产物 cemi.py"],
        "kind": "alias",
        "why": "英文协议名 cEMI → 中文产物条目；"
               "M00279 knxip.py 是传输层、M00280 dpt.py 是数据点类型，只有 cemi.py 是报文编解码本体。",
    },
    {
        "q": "抖音里那种光脑 3D 大脑",
        "expect_ids": ["M00436"],
        "expect_titles": ["鎏金大脑星云视图"],
        "kind": "alias",
        "why": "用户口头叫「光脑」，库里正式名是 3D 鎏金大脑星云视图；"
               "M00437 只是 nebula.html 的参数清单（实现细节），M00449/M00456 是「这个视频里的效果」原始消息与回顾，"
               "都不含为什么这么实现、/classic 怎么留的决策。",
    },
    {
        "q": "agro.mjs 里有哪些导出",
        "expect_ids": ["M00338"],
        "expect_titles": ["产物 agro.mjs"],
        "kind": "alias",
        "why": "文件名 → 产物条目；"
               "M00335 是几何库的任务书（要求 57 类），M00351 是队友的交付消息（81 个导出、含契约外的），"
               "问「这个文件里有什么」应落到产物条目本身。",
    },
    {
        "q": "皮套上线照着做的手册",
        "expect_ids": ["M00218"],
        "expect_titles": ["直播落地手册"],
        "kind": "alias",
        "why": "「照着做」= 操作手册，答案是 dragon-vtuber/docs/直播落地手册.md；"
               "M00217 是同目录 README（项目说明），M00052 是 dsh-pet 侧的方案（另一个项目），M00058 是粒子平台 README。",
    },
    {
        "q": "Graphiti 的 valid_at",
        "expect_ids": ["M00534"],
        "expect_titles": ["记忆系统调研结论"],
        "kind": "alias",
        "why": "英文实现名 + 字段名 → 中文调研结论（时间分两根轴 valid_at/invalid_at）；"
               "M00552/M00538 是 Zep/Graphiti 拆解的交付消息与会话回顾，M00537 是任务书，都不给该抄哪条的结论。",
    },
    {
        "q": "LureDaily 采集复盘",
        "expect_ids": ["M00145"],
        "expect_titles": ["采集故障-2026-10-30"],
        "kind": "alias",
        "why": "仓库名 → 故障复盘文档；"
               "M00133/M00144 是会话回顾（复盘的上一级摘要），M00134/M00135 是脚本产物，"
               "只有 M00145 是「一条都采不到」的复盘正文。",
    },

    # ---------------- 时间错位 ----------------
    # 同一晚（11-02 01:57→02:15）连着问了四条自驾路线，语义几乎不可分，只能靠时间线索。
    {
        "q": "那晚最早问的是哪条自驾路线",
        "expect_ids": ["M00485"],
        "expect_titles": ["理小路自驾游路线"],
        "kind": "time",
        "why": "四条自驾（理小路 01:57 / 贵州 02:06 / 云南 02:08 / 龙泉山日出 02:15）里最早的那条；"
               "M00488/M00490 是这四条的会话回顾（覆盖全部话题，因此相似度最高却是错的答案），"
               "recency 打分也偏向最新那条，与最早相反——这就是本题的难点。",
    },
    {
        "q": "问完理小路就问贵州",
        "expect_ids": ["M00486"],
        "expect_titles": ["成都出发贵州自驾游路线"],
        "kind": "time",
        "why": "用先后顺序定位第二条（贵州）；相似度上云南那条几乎等价，时间线才是唯一判据。"
               "top1 的理小路条目是前一条，不是被问的那条。",
    },
    {
        "q": "看日出是最后问的",
        "expect_ids": ["M00489"],
        "expect_titles": ["龙泉山哪个位置看日出"],
        "kind": "time",
        "why": "四问里的最后一问（02:15）；M00490 是把四问打包的会话回顾，问「最后问的」要落到具体那条。",
    },
    {
        "q": "小爱音箱最早只是想接大模型",
        "expect_ids": ["M00359"],
        "expect_titles": ["小爱音箱如何接入热门大模型"],
        "kind": "time",
        "why": "最早指 11-01 12:13 的起点需求（接入大模型 + agent 调试）；"
               "11-02 的 M00528/M00544/M00543 都是后来踩 bug（DID、模型 id、演示模式），时间在后的不算起点。",
    },
    {
        "q": "记忆面板最开始是怎么打开的",
        "expect_ids": ["M00100"],
        "expect_titles": ["记忆面板入口"],
        "kind": "time",
        "why": "最开始指向 10-30 19:30 的入口条目（8790 + webloc + launchd）；"
               "top1 的 M00465 是 11-01 的「面板没运行」故障，M00103 是「怎么进 UI」的对话，"
               "时间在后、内容是排障，不是最初那套入口。",
    },
    {
        "q": "皮套服务后来接进 8787",
        "expect_ids": ["M00332"],
        "expect_titles": ["将此应用服务集成到"],
        "kind": "time",
        "why": "「后来」把两件事分开：10-31 是皮套本体制作（M00206），11-01 才要求接进 8787 工作台管理；"
               "M00333 是同会话回顾、M00334 是同一条消息的重复条目（也在答案集内）。",
    },
    {
        "q": "隔天才轮到捡漏不全",
        "expect_ids": ["M00435"],
        "expect_titles": ["捡漏鱼讯未完整输出"],
        "kind": "time",
        "why": "鱼讯问题分两次：10-30 是「一条都采不到」（M00145），11-01 才轮到「捡漏不全部输出」（M00435）；"
               "隔天要求选后一次。同样的字面（捡漏/鱼讯）会把 10-30 那批拉上来，时间线索才是判据。",
    },
    {
        "q": "记忆模糊和沉睡是今天早上才要的",
        "expect_ids": ["M00504"],
        "expect_titles": ["记忆模糊+部分记忆沉睡"],
        "kind": "time",
        "why": "需求提出在 11-02 10:02（原始消息）；"
               "M00509/M00526 是同一批工作的会话回顾（时间点更靠后且覆盖更宽），M00510 是当天定下的方案。"
               "问「才要的」（谁提的、什么时候）应落到原始需求消息。",
    },

    # ---------------- 干扰项 ----------------
    {
        "q": "服务层的审计文档路径",
        "expect_ids": ["M00446"],
        "expect_titles": ["产物 audit-service.md"],
        "kind": "dist",
        "why": "两份审计报告并存：audit-service.md（服务层 45 条）与 audit-engine.md（引擎 60 条）。"
               "问服务层必须选前者；M00448/M00445 是服务层审计的会话回顾（无路径），M00460 是引擎那份。",
    },
    {
        "q": "两台车分开做爆炸图",
        "expect_ids": ["M00430", "M00431"],
        "expect_titles": ["电车分开来出爆炸图"],
        "kind": "dist",
        "why": "答案是「油车/电车拆成两个项目各出一张」那条（同一用户消息有两个重复条目，都算对）；"
               "M00356 是「所有项目都要爆炸图」的通用规则、M00429 是「爆炸图缺零件」、M00417 是「没在高光圈里」，"
               "都是爆炸图主题下的邻居但不是分成两张。",
    },
    {
        "q": "爆炸图太紧凑了",
        "expect_ids": ["M00353", "M00355"],
        "expect_titles": ["拆解图有点紧凑"],
        "kind": "dist",
        "why": "「紧凑/散不开」的抱怨对应「悬浮簇 + 爆炸求解」那两条（同一用户消息的连续两轮）；"
               "M00345/M00347 抱怨的是「不够详细、要气泡」，M00356 是「要爆炸图」的通用要求，"
               "字面都含拆解/爆炸，语义不同。",
    },
    {
        "q": "memory-core 推到哪个仓库",
        "expect_ids": ["M00099"],
        "expect_titles": ["memory-core GitHub 仓库"],
        "kind": "dist",
        "why": "问仓库坐标（私有 YiIimini/memcore + 分支 + CI）；"
               "M00102 是「建 git 并上传」的对话、M00524/M00518 是推送动作、M00502 是 LureDaily 的仓库，"
               "都能命中推送/仓库但不是这个坐标。",
    },
    {
        "q": "0.2.0 已推送",
        "expect_ids": ["M00518"],
        "expect_titles": ["memcore 0.2.0 已推送"],
        "kind": "dist",
        "why": "问的是这次发布的事实记录（提交号、CI、压缩截图）；"
               "M00519 是版本政策（0.x）、M00525 是「改成 0.*.*」的指令，都与已推送不同。",
    },
    {
        "q": "鳟鱼没图和没识别",
        "expect_ids": ["M00141"],
        "expect_titles": ["鳟鱼没有图片"],
        "kind": "dist",
        "why": "正确答案是那次现场故障（鳟鱼既没图也没识别，且顺手查出黄颡鱼/鳙鱼/白鲢同类隐患）；"
               "M00143/M00162 是之后「换七种鱼图」的需求，M00148 是回归测试脚本，M00149 是事后总结的流程文档。",
    },
    {
        "q": "新鱼种要走哪三步",
        "expect_ids": ["M00149"],
        "expect_titles": ["新增鱼种流程"],
        "kind": "dist",
        "why": "答案是流程文档（词表/素材/测试三处必须同时到位）；"
               "M00146/M00147 是流程涉及的脚本、M00148 是测试、M00141 是触发这次总结的故障，都不是走哪几步。",
    },
    {
        "q": "采集一条都采不到",
        "expect_ids": ["M00145"],
        "expect_titles": ["采集故障-2026-10-30"],
        "kind": "dist",
        "why": "与「捡漏只输出一部分」是两个不同故障：这条是 10-30 的零条故障（标题格式变更）；"
               "M00133/M00144 是会话回顾、M00435 是 11-01 的输出不全，主题相邻但现象不同。",
    },
    {
        "q": "捡漏只输出一部分",
        "expect_ids": ["M00435"],
        "expect_titles": ["捡漏鱼讯未完整输出"],
        "kind": "dist",
        "why": "答案是 11-01 的「带省略、漏项」那条；"
               "top1 的 M00002 是「输出风格」偏好（用户口味，不是故障），M00144/M00142 是更早的采集与手工添加，"
               "M00145 是零条故障——四个都含输出/鱼讯，只有一条现象对得上。",
    },
    {
        "q": "farm.mjs 有几个导出",
        "expect_ids": ["M00238"],
        "expect_titles": ["产物 farm.mjs"],
        "kind": "dist",
        "why": "问文件本身的导出数量，答案在产物条目（40 个 = 38 契约 + 2 扩展）；"
               "M00253/M00242/M00265 是队友交付消息里反复变动的数字（39/38+1/40），"
               "M00228 是任务书要求的 38 个——都在同一串数字上，容易互相顶掉。",
    },
    {
        "q": "评测集饱和的坑",
        "expect_ids": ["M00533"],
        "expect_titles": ["评测集饱和"],
        "kind": "dist",
        "why": "答案是那份「12 条已饱和、MRR 0.881 vs 0.880」的实测结论；"
               "M00553/M00522 是记忆子系统改造里的其他条目、M00534 是竞品调研结论，"
               "同样带 pitfall/评测字面但不是这次饱和的量测。",
    },
]

KIND_LABEL = {
    "para": "近义改写",
    "alias": "跨语言/别名",
    "time": "时间错位",
    "dist": "干扰项",
}
KIND_ORDER = ["para", "alias", "time", "dist"]


# --------------------------------------------------------------------------
# 取库：默认快照，避免检索过程改写真库
# --------------------------------------------------------------------------
def make_snapshot(src_home):
    """用 SQLite backup API 把真库复制到临时目录，返回临时 home 路径。

    直接连真库会踩两件事：search() 命中沉睡条目会写 wake_count/last_wake_at，
    于是同一天跑两次分数会漂；评测期间的写也会和他人的并发使用互相干扰。
    """
    src_db = os.path.join(src_home, "memory.db")
    if not os.path.exists(src_db):
        raise SystemExit("找不到记忆库：%s" % src_db)
    tmp_home = tempfile.mkdtemp(prefix="memcore-hard-eval-")
    # WAL 库里 -shm 缺失时 mode=ro 打不开（sqlite 需要写目录来建 -shm），
    # 退一步用 mode=rw：它只做 WAL 的共享内存映射，不改任何一行数据。
    src = None
    for uri in ("file:%s?mode=ro" % src_db, "file:%s?mode=rw" % src_db):
        try:
            src = sqlite3.connect(uri, uri=True, timeout=30)
            src.execute("SELECT 1 FROM memories LIMIT 1")
            break
        except sqlite3.Error:
            if src is not None:
                src.close()
            src = None
    if src is None:
        raise SystemExit("打不开记忆库（只读与 mode=rw 都失败）：%s" % src_db)
    dst = sqlite3.connect(os.path.join(tmp_home, "memory.db"))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    # config.json 里有权重与候选上限，必须跟真库一致，否则测的不是同一套参数
    cfg = os.path.join(src_home, "config.json")
    if os.path.exists(cfg):
        shutil.copy2(cfg, os.path.join(tmp_home, "config.json"))
    return tmp_home


def open_store(live, home=None):
    """返回 (Memory, 来源说明, 清理函数)。"""
    src_home = os.path.abspath(
        home or os.environ.get("MEM_HOME") or os.path.expanduser("~/.dsh/memory"))
    if live:
        return Memory(home=src_home), "live:%s" % src_home, lambda: None
    tmp = make_snapshot(src_home)
    return Memory(home=tmp), "snapshot:%s" % src_home, lambda: shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------
def find_rank(hits, expect_ids, expect_titles):
    """返回命中位置（0 起）或 None。

    expect_ids 优先：先在整个 top-k 里找 id；一条都没有时再用标题子串兜底。
    """
    want = set(expect_ids or ())
    for i, h in enumerate(hits):
        if h.get("id") in want:
            return i
    for i, h in enumerate(hits):
        title = h.get("title") or ""
        for needle in (expect_titles or ()):
            if needle and needle in title:
                return i
    return None


def run(k, live=False, home=None):
    store, source, cleanup = open_store(live, home)
    try:
        rows = []
        t_start = time.time()
        for case in CASES:
            hits = store.search(case["q"], limit=k)
            rank = find_rank(hits, case.get("expect_ids"), case.get("expect_titles"))
            rows.append({
                "q": case["q"],
                "kind": case["kind"],
                "expect_ids": case.get("expect_ids") or [],
                "expect_titles": case.get("expect_titles") or [],
                "why": case.get("why", ""),
                "rank": None if rank is None else rank + 1,   # 对外统一用 1 起
                "hit_at_1": rank == 0,
                "hit_at_3": rank is not None and rank < 3,
                "rr": 0.0 if rank is None else 1.0 / (rank + 1),
                "top": [{"id": h.get("id"), "title": h.get("title")} for h in hits],
            })
        elapsed = time.time() - t_start
    finally:
        cleanup()
    return rows, source, elapsed


def summarize(rows):
    total = len(rows)
    r1 = sum(1 for r in rows if r["hit_at_1"])
    r3 = sum(1 for r in rows if r["hit_at_3"])
    mrr = sum(r["rr"] for r in rows) / total if total else 0.0
    by_kind = collections.OrderedDict()
    for kind in KIND_ORDER:
        sub = [r for r in rows if r["kind"] == kind]
        if not sub:
            continue
        by_kind[kind] = {
            "label": KIND_LABEL.get(kind, kind),
            "total": len(sub),
            "recall_at_1": sum(1 for r in sub if r["hit_at_1"]),
            "recall_at_3": sum(1 for r in sub if r["hit_at_3"]),
            "mrr": round(sum(r["rr"] for r in sub) / len(sub), 4),
        }
    return {
        "total": total,
        "recall_at_1": r1,
        "recall_at_3": r3,
        "mrr": round(mrr, 4),
        "by_kind": by_kind,
    }


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def print_text(rows, summary, k, source, elapsed, verbose):
    n = summary["total"]
    print("memcore 硬检索评测 · k=%d · 用例 %d 条 · 库 %s" % (k, n, source))
    print("=" * 78)
    for r in rows:
        # ✓ 排第 1；· 进了 top-k 但没排第 1（可提升空间）；✗ 根本没命中
        mark = "✓" if r["hit_at_1"] else ("·" if r["rank"] else "✗")
        top1 = r["top"][0]["title"] if r["top"] else "(无结果)"
        where = ("#%d" % r["rank"]) if r["rank"] else "未命中"
        print("[%s] %-5s %-28s → %s  (%s)" % (mark, r["kind"], r["q"][:28], top1[:32], where))
        if verbose:
            print("      期望 id=%s 标题=%s" % (",".join(r["expect_ids"]),
                                             " / ".join(r["expect_titles"])))
            print("      为什么：%s" % r["why"])
            for i, h in enumerate(r["top"]):
                print("        #%d %s %s" % (i + 1, h["id"], (h["title"] or "")[:46]))
    print("-" * 78)
    print("总体   Recall@1=%d/%d (%.1f%%)  Recall@3=%d/%d (%.1f%%)  MRR=%.3f  (%.0f ms/条)" % (
        summary["recall_at_1"], n, 100.0 * summary["recall_at_1"] / n,
        summary["recall_at_3"], n, 100.0 * summary["recall_at_3"] / n,
        summary["mrr"], elapsed / max(1, n) * 1000))
    for kind, v in summary["by_kind"].items():
        print("  %-10s Recall@1=%d/%d  Recall@3=%d/%d  MRR=%.3f" % (
            v["label"], v["recall_at_1"], v["total"], v["recall_at_3"], v["total"], v["mrr"]))
    hard = [r for r in rows if r["rank"] and r["rank"] > 1]
    if hard:
        print("  （已命中但没排到第 1 的 %d 条就是排序优化的可提升空间：%s）" % (
            len(hard), "，".join("%s#%d" % (r["q"][:10], r["rank"]) for r in hard)))
    miss = [r for r in rows if r["rank"] is None]
    if miss:
        print("  （top-%d 内未命中 %d 条：%s）" % (k, len(miss), "，".join(r["q"][:14] for r in miss)))


def print_json(rows, summary, k, source, elapsed):
    payload = {
        "k": k,
        "store": source,
        "total": summary["total"],
        "recall_at_1": summary["recall_at_1"],
        "recall_at_1_rate": round(summary["recall_at_1"] / summary["total"], 4),
        "recall_at_3": summary["recall_at_3"],
        "recall_at_3_rate": round(summary["recall_at_3"] / summary["total"], 4),
        "mrr": summary["mrr"],
        "ms_per_query": round(elapsed / max(1, summary["total"]) * 1000, 1),
        "by_kind": summary["by_kind"],
        "cases": rows,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="memcore 硬检索评测集（近义改写 / 跨语言别名 / 时间错位 / 干扰项）")
    ap.add_argument("--k", type=int, default=5, help="每条取前 k 个结果（默认 5，至少 3）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--verbose", action="store_true", help="打印期望、理由与完整 top-k")
    ap.add_argument("--live", action="store_true",
                    help="直接检索真库（默认走快照；真库模式会写入沉睡唤醒计数）")
    ap.add_argument("--home", default=None, help="记忆库目录（默认取 MEM_HOME 或 ~/.dsh/memory）")
    args = ap.parse_args(argv)

    k = max(3, args.k)   # Recall@3 需要至少 3 条
    rows, source, elapsed = run(k, live=args.live, home=args.home)
    summary = summarize(rows)
    if args.json:
        print_json(rows, summary, k, source, elapsed)
    else:
        print_text(rows, summary, k, source, elapsed, args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
