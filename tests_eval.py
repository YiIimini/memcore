#!/usr/bin/env python3
"""Retrieval evaluation on the real store: paraphrase / keyword / mixed-CJK queries."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memcore import Memory

CASES = [
    ("上次插件清单那份文档", ["已安装插件清单"]),
    ("直播平台怎么接国内热门平台", ["直播平台需要国内", "直播"]),
    ("粒子特效太糊了怎么改的", ["粒子效果很差", "粒子特效"]),
    ("双击启动别用终端", ["双击启动"]),
    ("记忆系统存在哪里", ["记忆系统"]),
    ("我想要什么输出风格", ["输出风格"]),
    ("rea 当 mcp 装上", ["rea"]),
    ("新对话分支有什么意义", ["分支"]),
    ("设置页要做成可配置", ["设置页面"]),
    ("声音太难听", ["声音好难听"]),
    ("弹幕不是复读是回复", ["回复弹幕消息"]),
    ("直播宠物整体方案写到哪个文件", ["dsh-pet-AI直播实现方案", "AI直播实现方案"]),
]

def main(k=5):
    m = Memory()
    t1 = t3 = 0
    t0 = time.time()
    for q, expects in CASES:
        hits = m.search(q, limit=k)
        titles = [h["title"] for h in hits]
        pos = next((i for i, ti in enumerate(titles) if any(e in ti for e in expects)), None)
        ok1 = pos == 0
        ok3 = pos is not None and pos < 3
        t1 += ok1
        t3 += ok3
        mark = "1" if ok1 else ("3" if ok3 else "-")
        print(f"[{mark}] {q:<28} -> {titles[0][:40] if titles else '(none)'}")
    dt = (time.time() - t0) / len(CASES) * 1000
    print(f"\nRecall@1={t1}/{len(CASES)} Recall@3={t3}/{len(CASES)} avg={dt:.0f}ms/query")

main()
