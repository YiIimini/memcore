"""Scale + latency benchmark on a synthetic corpus (unique, self-retrievable rows)."""
import os, random, shutil, statistics, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memcore import Memory

TOPICS = ["直播平台接入", "粒子特效渲染", "插件清单导出", "记忆检索排序", "会话分支实现",
          "WebSocket 握手", "弹幕回复策略", "TTS 音色选择", "Docker 部署", "SQLite 索引优化"]
VERBS = ["实现了", "重构了", "排查并修复", "设计并落地", "评估后放弃", "临时绕过"]
CONS = ["低延迟", "零依赖", "本地优先", "可回滚"]

def main(n=5000):
    home = tempfile.mkdtemp(prefix="mem-bench-")
    m = Memory(home)
    random.seed(7)
    t0 = time.time()
    ids = []
    for i in range(n):
        t = random.choice(TOPICS)
        body = ("编号 %d：%s%s。约束：%s；结论：采用方案%s；证据：logs/run-%d.log 第 %d 行。"
                % (i, random.choice(VERBS), t, random.choice(CONS), random.choice("ABC"), i, random.randint(1, 999)))
        ids.append(m.add(body, title="%s-%d" % (t, i), kind="fact", tags=["bench", t],
                         project="bench", dedupe=False)["id"])
    build = time.time() - t0
    lat = []
    for q in ["粒子特效 渲染", "直播平台 接入 方案", "SQLite 索引 优化 结论", "弹幕 回复 策略",
              "docker 部署 回滚", "编号 4242", "TTS 音色"] * 7:
        s = time.time(); m.search(q, limit=10); lat.append((time.time() - s) * 1000)
    lat.sort()
    ok = 0; N = 100
    for i in random.sample(range(n), N):
        res = m.search("编号 %d" % i, limit=3)
        if any(h["id"] == ids[i] for h in res):
            ok += 1
    print("n=%d build=%.1fs db=%.1fMB" % (n, build, os.path.getsize(m.db_path) / 1e6))
    print("search p50=%.0fms p95=%.0fms p99=%.0fms" % (lat[len(lat) // 2], lat[int(len(lat) * .95)], lat[int(len(lat) * .99)]))
    print("unique-id Recall@3=%.0f%%" % (100.0 * ok / N))
    m.close(); shutil.rmtree(home, ignore_errors=True)

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5000)
