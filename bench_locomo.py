#!/usr/bin/env python3
"""LoCoMo retrieval benchmark for memcore.

Protocol (retrieval-only, no generation):
  * each LoCoMo conversation becomes its own memory store (as in the paper's setup)
  * every dialogue turn is ingested as one episode memory, timestamped by session date
  * for each QA we retrieve top-k memories and check whether the gold evidence
    dia_id is among them -> Recall@k; plus "gold answer appears in retrieved text"

Usage: python3 bench_locomo.py [path-to-locomo10.json] [--per-conv N] [--k 10]
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memcore import Memory  # noqa: E402

CAT = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


def parse_date(s: str) -> int:
    """'1:56 pm on 8 May, 2023' -> epoch seconds (best effort)."""
    if not s:
        return 0
    import datetime
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?.*?(\d{1,2})\s+(\w+),?\s*(\d{4})", s, re.I)
    if not m:
        return 0
    hour = int(m.group(1)) % 12
    if (m.group(3) or "").lower() == "pm":
        hour += 12
    month = {v: i for i, v in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], 1)}.get(m.group(5).lower(), 1)
    try:
        return int(datetime.datetime(int(m.group(6)), month, int(m.group(4)), hour,
                                     int(m.group(2) or 0)).timestamp())
    except Exception:
        return 0


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", (s or "").lower()).strip()


def run(path: str, per_conv: int = 60, k: int = 10) -> dict:
    data = json.load(open(path, encoding="utf-8"))
    total = defaultdict(int)
    hit = defaultdict(int)
    ans = defaultdict(int)
    lat = []
    t0 = time.time()
    for ci, sample in enumerate(data):
        conv = sample.get("conversation") or {}
        cid = sample.get("sample_id") or ("conv-%d" % ci)
        home = tempfile.mkdtemp(prefix="locomo-")
        m = Memory(home)
        id_to_dia = {}
        try:
            sessions = sorted([key for key in conv if re.fullmatch(r"session_\d+", key)],
                              key=lambda x: int(x.split("_")[1]))
            for sk in sessions:
                date = conv.get(sk + "_date_time", "")
                ts = parse_date(date)
                for turn in conv.get(sk) or []:
                    text = turn.get("text") or ""
                    if not text.strip():
                        continue
                    dia = turn.get("dia_id") or ""
                    r = m.add("%s: %s" % (turn.get("speaker", "?"), text),
                              title=text[:48], kind="episode", project=cid,
                              source_ref="%s#%s" % (cid, dia), ts=ts or None,
                              tags=["locomo", sk], salience=0.45)
                    id_to_dia[r["id"]] = dia
            qas = (sample.get("qa") or [])[:per_conv]
            for qa in qas:
                q = qa.get("question") or ""
                cat = int(qa.get("category") or 0)
                if cat == 5:  # adversarial: gold answer is "no answer"
                    continue
                s = time.time()
                hits = m.search(q, limit=k)
                lat.append(time.time() - s)
                got = {id_to_dia.get(h["id"], "") for h in hits}
                ev = set(qa.get("evidence") or [])
                ok = bool(ev & got)
                total[cat] += 1
                hit[cat] += ok
                gold = norm(str(qa.get("answer") or ""))
                ctx = norm(" ".join(h["title"] + " " + h["body"] for h in hits))
                ans[cat] += bool(gold) and len(gold) > 1 and gold in ctx
        finally:
            m.close()
            shutil.rmtree(home, ignore_errors=True)
        print("  %-12s qa=%d  recall@%d=%.2f" % (
            cid, sum(1 for c in total if False) or len([1]), k,
            (sum(hit.values()) / max(1, sum(total.values())))), flush=True)

    overall = sum(hit.values()) / max(1, sum(total.values()))
    res = {"n": sum(total.values()), "recall": overall, "by_cat": {},
           "answer_rate": sum(ans.values()) / max(1, sum(total.values())),
           "latency_ms": (sum(lat) / max(1, len(lat))) * 1000,
           "seconds": time.time() - t0}
    for c in sorted(total):
        res["by_cat"][CAT.get(c, str(c))] = {"n": total[c],
                                             "recall": hit[c] / max(1, total[c]),
                                             "answer": ans[c] / max(1, total[c])}
    return res


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "/tmp/locomo10.json"
    per_conv = 60
    k = 10
    if "--per-conv" in sys.argv:
        per_conv = int(sys.argv[sys.argv.index("--per-conv") + 1])
    if "--k" in sys.argv:
        k = int(sys.argv[sys.argv.index("--k") + 1])
    print("LoCoMo: %s (per-conv=%d, k=%d)" % (path, per_conv, k))
    r = run(path, per_conv, k)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
