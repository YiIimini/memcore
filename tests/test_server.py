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
        self.assertIn("Content-Security-Policy", hdrs)
        self.assertIn("frame-ancestors", hdrs.get("Content-Security-Policy", ""))

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


if __name__ == "__main__":
    unittest.main()
