import json, os, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memcore
from memcore import Memory


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="mem-test-")
        self.m = Memory(self.home)

    def tearDown(self):
        self.m.close()
        shutil.rmtree(self.home, ignore_errors=True)


class TestWrite(Base):
    def test_add_and_get(self):
        r = self.m.add("直播平台接入完成，斗鱼原生桥已通。", title="直播平台", kind="decision",
                       tags=["live"], project="p1")
        self.assertTrue(r["id"].startswith("M"))
        got = self.m.get(r["id"])
        self.assertEqual(got["kind"], "decision")
        self.assertIn("斗鱼", got["body"])
        self.assertEqual(got["project"], "p1")

    def test_exact_dedupe(self):
        a = self.m.add("同一条事实", kind="fact")
        b = self.m.add("同一条事实", kind="fact")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(b["deduped"], "exact")
        self.assertEqual(self.m.stats()["total"], 1)

    def test_fuzzy_dedupe(self):
        a = self.m.add("直播平台接入使用斗鱼原生桥加抖音通用桥方案", kind="fact")
        b = self.m.add("直播平台接入使用斗鱼原生桥加抖音通用桥方案。", kind="fact")
        self.assertEqual(a["id"], b["id"])

    def test_update_and_forget(self):
        mid = self.m.add("旧结论", kind="fact")["id"]
        self.assertTrue(self.m.update(mid, body="新结论", title="改过", salience=0.9))
        self.assertEqual(self.m.get(mid)["title"], "改过")
        self.assertTrue(self.m.forget(mid))
        self.assertIsNone(self.m.get(mid))
        self.assertEqual(self.m.search("新结论"), [])


class TestSearch(Base):
    def setUp(self):
        super().setUp()
        self.m.add("记忆系统代码在 ~/.dsh/memory-core，数据在 ~/.dsh/memory。", title="记忆系统位置",
                   kind="fact", tags=["memory"], salience=0.9)
        self.m.add("用户要求：所有输出直接给可执行结果，不写免责声明。", title="输出风格",
                   kind="preference", pinned=True)
        self.m.add("粒子特效太糊，根因是采样密度不足，改为粒子位图 + GPU 放大。", title="粒子特效修复",
                   kind="decision", tags=["live"])
        self.m.add("斗鱼原生桥已通，抖音和快手走通用桥。", title="直播平台接入", kind="fact", tags=["live"])

    def test_cjk_paraphrase(self):
        hits = self.m.search("记忆数据存在哪个目录")
        self.assertTrue(hits)
        self.assertIn("记忆系统", hits[0]["title"])

    def test_title_priority(self):
        hits = self.m.search("直播平台都接了哪些")
        self.assertIn("直播平台", hits[0]["title"])

    def test_pinned_without_evidence_is_filtered(self):
        hits = self.m.search("粒子特效 采样密度")
        self.assertTrue(all("输出风格" not in h["title"] for h in hits[:2]))

    def test_filters(self):
        self.assertEqual(self.m.search("直播", kinds=["preference"]), [])
        self.assertTrue(self.m.search("直播", kinds=["fact"]))

    def test_empty_query(self):
        self.assertEqual(self.m.search("  "), [])


class TestPacking(Base):
    def test_budget_respected(self):
        for i in range(12):
            self.m.add("第 %d 条：直播平台接入细节与回滚方案说明。" % i, title="直播-%d" % i, kind="fact")
        r = self.m.recall("直播平台接入", budget_tokens=120)
        self.assertLessEqual(r["tokens"], 140)
        self.assertGreaterEqual(len(r["items"]), 1)
        self.assertIn("记忆召回", r["text"])

    def test_brief_sections(self):
        self.m.add("我是本地编码智能体", title="身份", kind="identity", pinned=True)
        self.m.add("待补抖音适配", title="TODO 抖音", kind="todo")
        self.m.add("项目：直播平台", title="项目：live-pet", kind="project")
        txt = self.m.brief(budget_tokens=400)
        self.assertIn("身份", txt)
        self.assertIn("未闭环", txt)


class TestGraphAndMaintenance(Base):
    def test_link_graph(self):
        a = self.m.add("A 决策", kind="decision")["id"]
        b = self.m.add("B 产物", kind="artifact")["id"]
        self.assertTrue(self.m.link(a, b, "produces"))
        g = self.m.graph(a, depth=1)
        self.assertEqual(len(g["nodes"]), 2)
        self.assertEqual(g["edges"][0]["rel"], "produces")

    def test_gc_expired(self):
        mid = self.m.add("临时", kind="note", ttl_days=1)["id"]
        self.m.db.execute("UPDATE memories SET created_at=created_at-200000 WHERE id=?", (mid,))
        self.m.db.commit()
        self.assertEqual(self.m.gc(dry_run=True)["removed"], 1)
        self.m.gc()
        self.assertIsNone(self.m.get(mid))

    def test_export(self):
        self.m.add("导出测试", title="导出", kind="fact")
        p = self.m.export_markdown()
        self.assertTrue(os.path.exists(p))
        self.assertIn("导出", open(p, encoding="utf-8").read())


class TestIngest(Base):
    def _write_session(self, events):
        p = os.path.join(self.home, "session-x.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        return p

    def test_ingest_filters_noise_and_is_idempotent(self):
        ev = [
            {"type": "session", "id": "session-x", "cwd": "/tmp/proj"},
            {"type": "session/title", "data": {"title": "接入直播平台"}},
            {"type": "user/message", "time": 1793000000000,
             "data": {"content": [{"type": "text", "text": "直播平台接入要支持斗鱼和抖音"}]}},
            {"type": "user/message", "time": 1793000001000,
             "data": {"content": [{"type": "text", "text": "<system-reminder> 忽略"}]}},
            {"type": "user/message", "time": 1793000002000,
             "data": {"content": [{"type": "text", "text": "Current runtime context. snapshot"}]}},
            {"type": "assistant/message", "data": {"message": {"content": [
                {"type": "text", "text": "已接好斗鱼原生桥。"},
                {"type": "tool-call", "name": "write", "arguments": json.dumps({"file_path": "/tmp/proj/bridge.mjs"})},
                {"type": "tool-call", "name": "read", "arguments": json.dumps({"file_path": "/tmp/proj/ignored.txt"})}]}}},
        ]
        p = self._write_session(ev)
        n1 = self.m._ingest_one(p)
        self.assertGreaterEqual(n1, 2)
        n2 = self.m._ingest_one(p)
        self.assertEqual(n2, 0, "re-ingest must be a no-op")
        titles = [r["title"] for r in self.m.db.execute("SELECT title FROM memories")]
        self.assertFalse(any("system-reminder" in t for t in titles))
        self.assertFalse(any("runtime context" in t for t in titles))
        self.assertTrue(any("bridge.mjs" in r["body"] for r in self.m.db.execute("SELECT body FROM memories")))
        self.assertFalse(any("ignored.txt" in r["body"] for r in self.m.db.execute("SELECT body FROM memories")))


class TestMCP(Base):
    def test_mcp_tools(self):
        import mem_mcp
        out = mem_mcp.call_tool(self.m, "memory_add", {"body": "mcp 写入测试", "kind": "fact"})
        self.assertIn("stored", out["content"][0]["text"])
        res = mem_mcp.call_tool(self.m, "memory_search", {"query": "mcp 写入"})
        self.assertIn("mcp 写入测试", res["content"][0]["text"])


class TestVectorizer(unittest.TestCase):
    def test_semantic_neighbours(self):
        a = memcore.embed("直播平台接入方案")
        b = memcore.embed("直播平台的接入方案讨论")
        c = memcore.embed("SQLite 索引优化")
        sim = memcore.cosine(memcore.vec_of(a), memcore.vec_of(b))
        far = memcore.cosine(memcore.vec_of(a), memcore.vec_of(c))
        self.assertGreater(sim, far)

    def test_simhash_stability(self):
        self.assertEqual(memcore.simhash("同一段文本"), memcore.simhash("同一段文本"))




class TestTemporal(Base):
    def test_as_of_window(self):
        old = self.m.add("部署在 A 机房", title="机房位置", kind="fact", ts=1000)["id"]
        self.m.retire(old, at=2000, reason="迁移")
        new = self.m.add("部署在 B 机房", title="机房位置(新)", kind="fact", ts=2500)["id"]
        self.assertEqual([h["id"] for h in self.m.search("机房位置")], [new])
        past = [h["id"] for h in self.m.search("机房位置", as_of=1500)]
        self.assertEqual(past, [old])
        self.assertEqual(self.m.search("机房位置", as_of=100), [])

    def test_history_chain(self):
        a = self.m.add("v1 结论", kind="decision", ts=1000)["id"]
        b = self.m.add("v2 结论", kind="decision", ts=2000)["id"]
        self.m.retire(a, by=b)
        h = self.m.history(b)
        self.assertEqual([r["id"] for r in h["chain"]], [a, b])

    def test_retired_hidden_from_brief(self):
        t = self.m.add("待办：补测试", title="补测试", kind="todo")["id"]
        self.m.retire(t)
        self.assertNotIn("补测试", self.m.brief(budget_tokens=500))


class TestGraphFeatures(Base):
    def test_autolink_bidirectional_dedupe(self):
        a = self.m.add("直播平台接入：斗鱼原生桥已通，抖音走通用桥", title="直播接入",
                       kind="fact", tags=["live"])
        b = self.m.add("直播平台接入：斗鱼原生桥通过验证，抖音通用桥", title="直播接入验证",
                       kind="fact", tags=["live"])
        pairs = [dict(r) for r in self.m.db.execute(
            "SELECT src,dst FROM links WHERE rel='related'")]
        self.assertTrue(any({p["src"], p["dst"]} == {a["id"], b["id"]} for p in pairs))
        self.assertLessEqual(len(pairs), 2)

    def test_graph_export_shape(self):
        a = self.m.add("节点 A", kind="fact")["id"]
        b = self.m.add("节点 B", kind="fact")["id"]
        self.m.link(a, b, "related", 0.5)
        g = self.m.graph_export()
        ids = {n["id"] for n in g["nodes"]}
        self.assertTrue({a, b} <= ids)
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in g["edges"]))
        self.assertEqual(g["meta"]["total"], len(g["nodes"]))

    def test_consolidate_merges_duplicates(self):
        a = self.m.add("部署脚本位于 ops/deploy.sh，使用 ssh 推送", title="部署脚本", kind="fact", dedupe=False)
        b = self.m.add("部署脚本位于 ops/deploy.sh，使用 ssh 推送。", title="部署脚本(重复)",
                       kind="fact", dedupe=False)
        rep = self.m.consolidate(dry_run=True)
        self.assertTrue(any({r["a"], r["b"]} == {a["id"], b["id"]} for r in rep))
        # 默认写入模式只合并不退休：误判时不能毁掉真实事实
        self.m.consolidate(dry_run=False)
        self.assertEqual(self.m.get(a["id"])["status"], "active")
        # 显式开启 auto_retire 才按合并结果关闭旧条目的有效期
        self.m.cfg.setdefault("consolidate", {})["auto_retire"] = True
        self.m.consolidate(dry_run=False)
        self.assertEqual(self.m.get(a["id"])["status"], "superseded")
        self.assertEqual([h["id"] for h in self.m.search("部署脚本")][0], b["id"])

    def test_consolidate_keeps_distinct_facts(self):
        """模板相同、载荷不同的事实不能被当成重复删掉。"""
        ids = [self.m.add("服务器 %d 的用途：备份" % i, title="服务器 %d" % i,
                          kind="fact", dedupe=False)["id"] for i in range(12)]
        self.m.cfg.setdefault("consolidate", {})["auto_retire"] = True
        self.m.consolidate(dry_run=False)
        alive = [i for i in ids if self.m.get(i)["status"] == "active"]
        self.assertGreaterEqual(len(alive), 10, "批量 retire 误伤了独立事实")

    def test_retired_memory_does_not_swallow_new_fact(self):
        """退休后重新写同一事实，必须产生新的 active 行。"""
        first = self.m.add("缓存目录在 /tmp/mc，重启后清空", title="缓存目录", kind="fact")
        self.m.retire(first["id"], reason="测试")
        again = self.m.add("缓存目录在 /tmp/mc，重启后清空", title="缓存目录", kind="fact")
        self.assertNotEqual(again["id"], first["id"])
        self.assertIsNone(again.get("deduped"))
        self.assertEqual(self.m.get(again["id"])["status"], "active")

    def test_update_recomputes_dedupe_hash(self):
        """改过正文之后，原文再写入不能被判成同一条。"""
        a = self.m.add("端口 8080 用于本地预览", title="预览端口", kind="fact")
        self.m.update(a["id"], body="端口 9090 用于本地预览")
        b = self.m.add("端口 8080 用于本地预览", title="预览端口", kind="fact")
        self.assertNotEqual(b["id"], a["id"])

    def test_retire_keeps_existing_meta(self):
        mid = self.m.add("带元信息的条目", title="元信息", kind="todo", meta={"done": False, "prio": 3})["id"]
        self.m.retire(mid, reason="改用别的方案")
        meta = json.loads(self.m.get(mid)["meta"])
        self.assertEqual(meta.get("prio"), 3)
        self.assertEqual(meta.get("retire_reason"), "改用别的方案")


if __name__ == "__main__":
    unittest.main(verbosity=2)
