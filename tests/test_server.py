"""HTTP 面板的安全回归测试。

覆盖审计中发现、且今后可能被改回去的边界：Host/Origin 白名单、请求体上限、
查询参数上限、内嵌 JSON 的转义、安全响应头。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memcore import Memory, safe_base_url  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(port: int, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.12)
    return False


class PanelSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memcore-test-")
        cls.env = dict(os.environ, MEM_HOME=cls.tmp)
        subprocess.run([sys.executable, os.path.join(ROOT, "mem"), "add",
                        "-k", "identity", "-t", "测试身份",
                        "-b", "面板安全回归用的临时记忆", "--pin"],
                       env=cls.env, check=True, capture_output=True)
        cls.port = _free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "mem"), "serve", "--port", str(cls.port)],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait(cls.port):
            cls.proc.kill()
            raise unittest.SkipTest("面板服务未在超时内启动")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    # -- helpers -----------------------------------------------------------
    def req(self, method: str, path: str, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or ("127.0.0.1:%d" % self.port)}
        h.update(headers or {})
        payload = None
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
            h["Content-Length"] = str(len(payload))
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in h.items():
                conn.putheader(k, v)
            conn.endheaders(payload)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    # -- 来源校验 ----------------------------------------------------------
    def test_foreign_origin_gets_no_cors_grant(self):
        for evil in ("http://localhost.evil.com", "http://127.0.0.1.evil.com",
                     "http://localhost@evil.com", "https://evil.com"):
            code, hdrs, _ = self.req("OPTIONS", "/api/graph", headers={
                "Origin": evil, "Access-Control-Request-Method": "POST"})
            self.assertNotIn("Access-Control-Allow-Origin", hdrs,
                             "不应为外来 Origin 下发 CORS 授权：%s" % evil)

    def test_loopback_origin_is_echoed(self):
        origin = "http://127.0.0.1:%d" % self.port
        code, hdrs, _ = self.req("OPTIONS", "/api/graph",
                                 headers={"Origin": origin,
                                          "Access-Control-Request-Method": "POST"})
        self.assertEqual(code, 204)
        self.assertEqual(hdrs.get("Access-Control-Allow-Origin"), origin)

    def test_wildcard_cors_is_never_emitted(self):
        code, hdrs, _ = self.req("GET", "/api/graph?limit=1")
        self.assertEqual(code, 200)
        self.assertNotEqual(hdrs.get("Access-Control-Allow-Origin"), "*")
        self.assertNotIn("Access-Control-Allow-Origin", hdrs)

    def test_foreign_origin_cannot_write(self):
        code, _, _ = self.req("POST", "/api/action", body={"name": "forget", "id": "M00001"},
                              headers={"Origin": "http://localhost.evil.com"})
        self.assertEqual(code, 403)

    # -- Host / DNS rebinding ---------------------------------------------
    def test_rebinding_host_is_rejected(self):
        code, _, _ = self.req("GET", "/api/graph", host="rebind.evil.com")
        self.assertEqual(code, 421)

    def test_loopback_host_variants_accepted(self):
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port):
            code, _, _ = self.req("GET", "/api/stats", host=host)
            self.assertEqual(code, 200, host)

    # -- 资源上限 ----------------------------------------------------------
    def test_oversized_body_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest("POST", "/api/action", skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", "127.0.0.1:%d" % self.port)
            conn.putheader("Origin", "http://127.0.0.1:%d" % self.port)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(64 << 20))
            conn.endheaders()
            status = conn.getresponse().status
        finally:
            conn.close()
        self.assertEqual(status, 413)

    def test_limit_is_clamped(self):
        code, _, body = self.req("GET", "/api/graph?limit=999999999")
        self.assertEqual(code, 200)
        self.assertLessEqual(len(json.loads(body).get("nodes", [])), 2000)

    def test_bad_memory_id_is_refused(self):
        code, _, _ = self.req("GET", "/api/memory/%27%20OR%201=1")
        self.assertEqual(code, 400)

    # -- 注入与响应头 ------------------------------------------------------
    def test_embedded_json_escapes_script_breakers(self):
        code, hdrs, body = self.req("GET", "/")
        self.assertEqual(code, 200)
        html = body.decode("utf-8")
        marker = "window.__MEM__="
        self.assertIn(marker, html, "页面应内嵌图数据")
        start = html.index(marker) + len(marker)
        end = html.index("</script>", start)
        payload = html[start:end]
        # 内嵌 JSON 里不能出现裸 "<"（挡 </script> 与 <!--），也不能有裸行分隔符
        self.assertNotIn("<", payload)
        self.assertNotIn("\u2028", payload)
        self.assertNotIn("\u2029", payload)

    def test_security_headers_present(self):
        code, hdrs, _ = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff")
        csp = hdrs.get("Content-Security-Policy", "")
        self.assertTrue(csp, "应下发 CSP")
        # 不限制嵌入来源：面板要嵌进 Harness 的 Electron 页面（origin 可能是 file:/null），
        # frame-ancestors 白名单会让 iframe 直接显示空白
        self.assertNotIn("frame-ancestors", csp)

    def test_favicon_beacon_for_host_probe(self):
        """宿主页用 <img> 判活，需要一个不受 CORS 约束的 200 响应。"""
        code, hdrs, body = self.req("GET", "/favicon.ico")
        self.assertEqual(code, 200)
        self.assertTrue(hdrs.get("Content-Type", "").startswith("image/png"))
        self.assertGreater(len(body), 0)

    def test_unknown_route_is_404(self):
        code, _, _ = self.req("GET", "/api/nope")
        self.assertEqual(code, 404)


class EngineGuards(unittest.TestCase):
    """不经过 HTTP 的纯逻辑护栏。"""

    def test_safe_base_url(self):
        self.assertTrue(safe_base_url("https://api.deepseek.com/v1"))
        self.assertTrue(safe_base_url("http://127.0.0.1:11434/v1"))
        self.assertTrue(safe_base_url("http://localhost:8080/v1"))
        self.assertFalse(safe_base_url("http://169.254.169.254/latest/meta-data"))
        self.assertFalse(safe_base_url("http://10.0.0.1/v1"))
        self.assertFalse(safe_base_url("file:///etc/passwd"))
        self.assertFalse(safe_base_url(""))

    def test_doc_head_skips_credentials(self):
        for p in ("/tmp/.ssh/id_rsa", "/home/u/.aws/credentials", "/proj/.env",
                  "/proj/secrets.json", "/proj/.netrc"):
            self.assertFalse(Memory._indexable_doc(p), p)
        for p in ("/proj/README.md", "/proj/src/main.py", "/proj/conf/app.yaml"):
            self.assertTrue(Memory._indexable_doc(p), p)


class LifecycleApi(unittest.TestCase):
    """记忆模糊/沉睡的 HTTP 出口：状态统计、沉睡区、手动唤醒与沉睡。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memcore-life-")
        cls.env = dict(os.environ, MEM_HOME=cls.tmp)
        run = lambda *a: subprocess.run([sys.executable, os.path.join(ROOT, "mem")] + list(a),
                                        env=cls.env, check=True, capture_output=True)
        run("add", "-k", "episode", "-t", "沉睡的会话片段",
            "-b", "斗鱼直播平台接入用的是原生桥，推流参数在 config/stream.yaml。")
        run("add", "-k", "fact", "-t", "清醒的事实", "-b", "记忆面板配色要用配对前景 token。")
        # 把 episode 拨回 6 天前：它应该沉下去，fact 还醒着
        subprocess.run([sys.executable, "-c",
                        "import sqlite3,time,os;"
                        "db=sqlite3.connect(os.path.join(os.environ['MEM_HOME'],'memory.db'));"
                        "db.execute(\"UPDATE memories SET updated_at=?, last_used_at=0, use_count=0 "
                        "WHERE kind='episode'\",(int(time.time()-86400*6),));db.commit()"],
                       env=cls.env, check=True)
        cls.port = _free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "mem"), "serve", "--port", str(cls.port)],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait(cls.port):
            cls.proc.kill()
            raise unittest.SkipTest("面板服务未在超时内启动")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def setUp(self):
        """每个用例前把那条 episode 重新拨回 6 天前：翻看/唤醒是有副作用的，不能串味。"""
        m = Memory(self.tmp)
        m.db.execute(
            "UPDATE memories SET updated_at=?, last_used_at=0, use_count=0, wake_count=0,"
            " last_wake_at=0, sleep_override='' WHERE kind='episode'",
            (int(time.time() - 86400 * 6),))
        m.db.commit()
        m.close()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % self.port})
        r = conn.getresponse()
        body = json.loads(r.read().decode() or "{}")
        conn.close()
        return r.status, body

    def post(self, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        raw = json.dumps(payload).encode()
        conn.request("POST", path, body=raw,
                     headers={"Host": "127.0.0.1:%d" % self.port,
                              "Content-Type": "application/json"})
        r = conn.getresponse()
        body = json.loads(r.read().decode() or "{}")
        conn.close()
        return r.status, body

    def test_stats_reports_state_breakdown(self):
        code, st = self.get("/api/stats")
        self.assertEqual(code, 200)
        self.assertIn("by_state", st)
        self.assertEqual(sum(st["by_state"].values()), 2)
        self.assertGreaterEqual(st["by_state"]["deep"] + st["by_state"]["dormant"], 1)

    def test_sleepers_lists_the_sleeping_one(self):
        code, d = self.get("/api/sleepers?limit=10")
        self.assertEqual(code, 200)
        self.assertEqual(len(d["items"]), 1)
        item = d["items"][0]
        self.assertIn(item["state"], ("dormant", "deep"))
        self.assertIn("since_touch_days", item)
        self.assertIn("░░", item["preview"])          # 沉睡的正文是雾化的

    def test_graph_nodes_carry_lifecycle_fields(self):
        code, g = self.get("/api/graph")
        self.assertEqual(code, 200)
        self.assertIn("states", g["meta"])
        self.assertIn("thresholds", g["meta"]["dormancy"])
        node = g["nodes"][0]
        for k in ("state", "state_label", "activation", "since_touch_days", "wake_count",
                  "last_wake_at", "wake_weight", "sleep_override", "next_state_in_days"):
            self.assertIn(k, node)

    def test_wake_and_sleep_actions(self):
        _, d = self.get("/api/sleepers?limit=10")
        mid = d["items"][0]["id"]
        code, r = self.post("/api/action", {"name": "wake", "id": mid})
        self.assertEqual(code, 200)
        self.assertEqual(r["result"]["state"], "awake")
        self.assertGreater(r["result"]["wake_count"], 0)
        code, r = self.post("/api/action", {"name": "sleep", "id": mid})
        self.assertEqual(code, 200)
        self.assertEqual(r["result"]["state"], "dormant")
        self.assertEqual(r["result"]["sleep_override"], "dormant")
        code, r = self.post("/api/action", {"name": "unsleep", "id": mid})
        self.assertEqual(r["result"]["sleep_override"], "")

    def test_detail_returns_full_body_and_wakes(self):
        _, d = self.get("/api/sleepers?limit=10")
        if not d["items"]:
            self.skipTest("沉睡区已空")
        mid = d["items"][0]["id"]
        code, row = self.get("/api/memory/" + mid)
        self.assertEqual(code, 200)
        self.assertIn("原生桥", row["body"])          # 显式打开是明文
        self.assertEqual(row["state"], "awake")       # 翻看即点亮
        self.assertTrue(row["last_wake_at"] > 0)


class PitfallApi(unittest.TestCase):
    """雷区出口：面板「雷区」页签靠这两个端点，不能悄悄 404。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memcore-pit-")
        cls.env = dict(os.environ, MEM_HOME=cls.tmp)
        run = lambda *a: subprocess.run([sys.executable, os.path.join(ROOT, "mem")] + list(a),
                                        env=cls.env, check=True, capture_output=True)
        run("learn", "--file", "memcore.py", "-t", "查询列集不一致", "--what", "两处分布打架",
            "--fix", "统一列集常量")
        run("learn", "--file", "ui/nebula.html", "-t", "面板数字回跳", "--what", "刷新拿到快照",
            "--fix", "先请求接口再回落")
        cls.port = _free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "mem"), "serve", "--port", str(cls.port)],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait(cls.port):
            cls.proc.kill()
            raise unittest.SkipTest("面板服务未在超时内启动")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % self.port})
        r = conn.getresponse()
        body = json.loads(r.read().decode() or "{}")
        conn.close()
        return r.status, body

    def test_pitfalls_grouped_by_file(self):
        code, d = self.get("/api/pitfalls")
        self.assertEqual(code, 200)
        self.assertEqual(d["total"], 2)
        files = {f["file"]: f["count"] for f in d["files"]}
        self.assertEqual(files.get("memcore.py"), 1)
        self.assertIn("nebula.html", files)
        item = d["items"][0]
        for k in ("id", "title", "what", "fix", "file", "state_label"):
            self.assertIn(k, item)
        self.assertTrue(item["fix"])

    def test_preflight_by_path(self):
        code, d = self.get("/api/preflight?paths=memcore.py,server.py&limit=5")
        self.assertEqual(code, 200)
        self.assertTrue(d["items"])
        self.assertEqual(d["items"][0]["file"], "memcore.py")
        self.assertIn("修法", d["text"])

    def test_preflight_unknown_path_is_empty_not_error(self):
        code, d = self.get("/api/preflight?paths=no_such_module.py")
        self.assertEqual(code, 200)
        self.assertEqual(d["items"], [])


class FilesApi(unittest.TestCase):
    """文件索引的 HTTP 出口：搜索、状态、越界拒绝。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memcore-files-")
        cls.ws = os.path.join(cls.tmp, "ws")
        os.makedirs(os.path.join(cls.ws, "docs"), exist_ok=True)
        with open(os.path.join(cls.ws, "docs", "note.md"), "w", encoding="utf-8") as fh:
            fh.write("# 笔记\n关键词 ZEBRAWORD 在这里。\n")
        cls.env = dict(os.environ, MEM_HOME=cls.tmp)
        subprocess.run([sys.executable, "-c",
                        "import sys;sys.path.insert(0,%r);from memcore import Memory;"
                        "m=Memory(%r);m.add_file_root(%r);print(m.index_files()['files']);m.close()"
                        % (ROOT, cls.tmp, cls.ws)], env=cls.env, check=True, capture_output=True)
        cls.port = _free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "mem"), "serve", "--port", str(cls.port)],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait(cls.port):
            cls.proc.kill()
            raise unittest.SkipTest("面板服务未在超时内启动")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % self.port})
        r = conn.getresponse()
        raw = r.read().decode()
        conn.close()
        return r.status, (json.loads(raw) if raw else {})

    def test_status_and_search(self):
        code, st = self.get("/api/files/status")
        self.assertEqual(code, 200)
        self.assertEqual(st["files"], 1)
        code, r = self.get("/api/files/search?q=ZEBRAWORD&mode=content")
        self.assertEqual(code, 200)
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["results"][0]["hit"], "content")

    def test_file_read_containment(self):
        code, _ = self.get("/api/file?path=/etc/passwd")
        self.assertEqual(code, 403)
        code, r = self.get("/api/file?path=" + os.path.join(self.ws, "docs", "note.md"))
        self.assertEqual(code, 200)
        self.assertIn("ZEBRAWORD", r["text"])

    def test_open_rejects_outside(self):
        code, _ = self.get("/api/open?path=/etc")
        self.assertEqual(code, 403)


if __name__ == "__main__":
    unittest.main()
