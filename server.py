"""Tiny live server for the memory graph UI (stdlib only, loopback only).

安全边界（默认拒绝，不依赖"本机就是可信"）：
  * 只接受 Host 为回环地址的请求 —— 挡住 DNS rebinding；
  * CORS 仅回显回环 Origin，绝不下发 `*` —— 任意网页读不到记忆；
  * 写操作要求 Origin 合法或缺失（后者等价于本机 CLI/curl）；
  * 请求体、查询参数、连接时长都有上限。
"""
from __future__ import annotations

import errno
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from memcore import Memory

try:  # 可选：没有 launchd/systemd 的后端时面板仍可用
    import autostart
except Exception:  # noqa: BLE001
    autostart = None

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "ui", "index.html")
NEBULA = os.path.join(HERE, "ui", "nebula.html")

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_BODY = 1 << 20          # 1 MiB：记忆正文远小于此
MAX_LIMIT = 2000            # 单次导出的记忆上限
SOCKET_TIMEOUT = 15         # 慢连接不能长期占用线程
GRAPH_TTL = 1.5             # 秒；面板连续刷新时不重复全表扫描
ID_RE = re.compile(r"^(M\d{1,8}|__me__)$")


def host_name(value: str) -> str:
    """从 Host 或 Origin 里取出规范化的主机名；非法输入返回空串。"""
    v = (value or "").strip()
    if not v:
        return ""
    if "://" not in v:
        v = "//" + v
    try:
        return (urlsplit(v).hostname or "").lower()
    except ValueError:
        return ""


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _bind(port: int, handler):
    """只在端口被占用时顺延；权限等其它错误立即抛出，避免误判。"""
    last = None
    for p in [port] + [port + i for i in range(1, 10)]:
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), handler), p
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
            last = e
    raise last


def _settings_problem(payload: dict):
    """Reject settings that would break the vector index or exfiltrate to a foreign host."""
    if "vector_dim" in payload:
        return "vector_dim 不可在线修改（需 mem reindex 全量重建）"
    llm = payload.get("llm") or {}
    base = llm.get("base") if isinstance(llm, dict) else None
    if base and not _safe_base(str(base)):
        return "llm.base 只支持 https 或本机回环地址"
    return None


def _safe_base(base: str) -> bool:
    """LLM 网关地址白名单：只允许公网 https 或本机回环 http。"""
    try:
        u = urlsplit(base or "")
    except ValueError:
        return False
    if u.scheme == "https":
        return bool(u.hostname)
    if u.scheme == "http":
        return (u.hostname or "").lower() in LOOPBACK
    return False


def serve(mem, port: int = 8787, open_browser: bool = False) -> None:
    home = mem.home
    cache = {"at": 0.0, "key": None, "value": None}
    lock = threading.Lock()

    def db():
        """One short-lived connection per request: sqlite objects are thread-bound."""
        return Memory(home)

    def graph(m, params):
        project = (params.get("project", [""])[0] or "")[:120]
        limit = _int(params.get("limit", [700])[0], 700, 1, MAX_LIMIT)
        retired = params.get("include_retired", ["0"])[0] in ("1", "true")
        key = (project, limit, retired)
        now = time.time()
        with lock:
            if cache["key"] == key and now - cache["at"] < GRAPH_TTL:
                return cache["value"]
        value = m.graph_export(project=project, limit=limit, include_retired=retired)
        with lock:
            cache.update(at=now, key=key, value=value)
        return value

    def state():
        if autostart is None:
            return {"agents": [], "autostart": {"enabled": False, "supported": False}, "settings": {}}
        # 后台任务状态 3 秒缓存：每次请求都 fork launchctl 会拖慢面板并堆积 fd
        now = time.time()
        with lock:
            cached = cache.get("state")
        if cached and now - cached[0] < 3:
            return cached[1]
        out = {"agents": autostart.agents(panel_alive=True),
               "autostart": autostart.ui_status(probe=False),
               "settings": mem.settings()}
        with lock:
            cache["state"] = (now, out)
        return out

    class Handler(BaseHTTPRequestHandler):
        server_version = "memcore/1.1"
        sys_version = ""
        timeout = SOCKET_TIMEOUT

        # ---- 基础设施 -------------------------------------------------
        def log_request(self, code="-", size="-"):
            try:
                path = urlsplit(self.path).path
            except Exception:  # noqa: BLE001
                path = "?"
            sys.stderr.write("%s %s -> %s\n" % (self.command or "?", path, code))

        def _cors(self):
            origin = self.headers.get("Origin") or ""
            if origin and host_name(origin) in LOOPBACK:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; font-src 'self' data:; "
                    "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                    "connect-src 'self'; frame-ancestors 'self' http://127.0.0.1:* http://localhost:*",
                )
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode())

        def _host_ok(self):
            """DNS rebinding 防线：Host 必须是回环地址。"""
            host = self.headers.get("Host") or ""
            return host_name(host) in LOOPBACK

        def _origin_ok(self):
            """True=合法回环 Origin，False=不可信 Origin，None=无 Origin（本机 CLI/curl）。"""
            origin = self.headers.get("Origin")
            if not origin:
                return None
            return host_name(origin) in LOOPBACK

        def _body(self):
            raw_len = self.headers.get("Content-Length")
            try:
                n = int(raw_len or 0)
            except (TypeError, ValueError):
                self._json({"error": "bad content-length"}, 400)
                return None
            if n < 0 or n > MAX_BODY:
                self._json({"error": "payload too large"}, 413)
                return None
            if n == 0:
                return {}
            raw = self.rfile.read(n)
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json({"error": "invalid json"}, 400)
                return None
            return obj if isinstance(obj, dict) else {}

        # ---- 路由 -----------------------------------------------------
        def do_OPTIONS(self):
            if not self._host_ok():
                self.send_response(421)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if not self._host_ok():
                self.send_response(421)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self._origin_ok() is False:
                self._json({"error": "cross-origin write refused"}, 403)
                return
            u = urllib.parse.urlparse(self.path)
            routes = {"/api/llm/test", "/api/settings", "/api/action"}
            if u.path not in routes:
                self._json({"error": "not found"}, 404)
                return
            payload = self._body()
            if payload is None:
                return
            m = db()
            try:
                if u.path == "/api/llm/test":
                    base = str(payload.get("base") or "")
                    if base and not _safe_base(base):
                        self._json({"ok": False, "error": "base 只支持 https 或本机回环地址", "models": []})
                        return
                    key = str(payload.get("key") or "")[:400]
                    p = m.llm_probe(base=base, model=str(payload.get("model") or "")[:120], key=key)
                    if p.get("ok") and key:
                        m.save_settings({"llm": {"key": key, "base": base or None,
                                                 "model": payload.get("model") or None}})
                    self._json(p)
                elif u.path == "/api/settings":
                    bad = _settings_problem(payload)
                    if bad:
                        self._json({"error": bad}, 400)
                        return
                    out = m.save_settings(payload)
                    if autostart is not None:
                        auto = out.get("maintenance") or {}
                        if "auto" in (payload.get("maintenance") or {}):
                            if auto.get("auto"):
                                out["autostart"] = autostart.enable(
                                    _int(auto.get("interval_minutes", 30), 30, 5, 1440), m.home)
                            else:
                                out["autostart"] = autostart.disable()
                        self._json({"ok": True, "settings": out, "agents": autostart.agents(panel_alive=True)})
                    else:
                        self._json({"ok": True, "settings": out, "agents": []})
                else:
                    self._action(m, payload)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._json({"error": "internal error"}, 500)
            finally:
                m.close()

        def _action(self, m, payload):
            name = str(payload.get("name") or "").strip()[:40]
            mid = str(payload.get("id") or "")
            if name in ("pin", "unpin", "retire", "forget", "consolidate"):
                pass
            elif name.startswith("autostart") or name.startswith("panel"):
                if autostart is None:
                    self._json({"error": "后台任务在当前离线平台不可用"}, 400)
                    return
            if name in ("pin", "unpin", "retire", "forget"):
                if not ID_RE.match(mid):
                    self._json({"error": "bad id"}, 400)
                    return
                row = m.get(mid)
                if not row:
                    self._json({"error": "not found"}, 404)
                    return
            if name == "maintain":
                self._json({"ok": True, "result": m.maintain()})
            elif name == "relink":
                self._json({"ok": True, "result": {"edges": m.relink(rebuild=True)}})
            elif name == "reindex":
                self._json({"ok": True, "result": {"reindexed": m.reindex()}})
            elif name == "pin":
                m.update(mid, pinned=True, salience=max(0.9, float(row.get("salience") or 0.9)))
                self._json({"ok": True, "result": {"id": mid, "pinned": True}})
            elif name == "unpin":
                m.update(mid, pinned=False)
                self._json({"ok": True, "result": {"id": mid, "pinned": False}})
            elif name == "retire":
                m.retire(mid, reason=str(payload.get("reason") or "面板操作")[:200])
                self._json({"ok": True, "result": {"id": mid, "status": "retired"}})
            elif name == "forget":
                m.forget(mid)
                self._json({"ok": True, "result": {"id": mid, "deleted": True}})
            elif name == "consolidate":
                rep = m.consolidate(dry_run=not payload.get("apply"), llm=True)
                self._json({"ok": True, "result": {"pairs": len(rep),
                            "detail": rep[:5], "applied": bool(payload.get("apply"))}})
            elif name == "export":
                self._json({"ok": True, "result": {"path": m.export_markdown()}})
            elif name in ("autostart_on", "autostart_off"):
                if name == "autostart_on":
                    interval = _int((m.cfg.get("maintenance") or {}).get("interval_minutes", 30), 30, 5, 1440)
                    r = autostart.enable(interval, m.home)
                else:
                    r = autostart.disable()
                self._json({"ok": True, "result": r, "agents": autostart.agents(panel_alive=True)})
            elif name == "panel_on":
                self._json({"ok": True, "result": autostart.enable_ui(port, m.home),
                            "agents": autostart.agents(panel_alive=True)})
            elif name == "panel_off":
                # 先回响应，再异步 bootout：否则调用方永远等不到这个 200
                from threading import Timer
                Timer(0.8, autostart.disable_ui).start()
                self._json({"ok": True, "result": {"disabled": True},
                            "agents": autostart.agents(panel_alive=True)})
            else:
                self._json({"error": "unknown action: %s" % name}, 400)

        def do_GET(self):
            if not self._host_ok():
                self.send_response(421)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            m = db()
            try:
                if u.path in ("/", "/index.html", "/nebula", "/nebula.html", "/classic"):
                    path = TPL if u.path == "/classic" else NEBULA
                    with open(path, "r", encoding="utf-8") as fh:
                        tpl = fh.read()
                    self._send(200, _inject(tpl, graph(m, qs)), "text/html; charset=utf-8")
                elif u.path == "/api/graph":
                    self._json(graph(m, qs))
                elif u.path == "/api/stats":
                    st = m.stats()
                    st["app"] = "memcore"
                    st.update(state())
                    self._json(st)
                elif u.path == "/api/agents":
                    self._json({"agents": autostart.agents(panel_alive=True) if autostart else [],
                                "app": "memcore"})
                elif u.path == "/api/settings":
                    self._json(m.settings())
                elif u.path == "/api/models":
                    now = time.time()
                    with lock:
                        cached = cache.get("models")
                    if cached and now - cached[0] < 60:
                        self._json(cached[1])
                    else:
                        out = m.llm_probe()
                        with lock:
                            cache["models"] = (now, out)
                        self._json(out)
                elif u.path == "/api/search":
                    q = (qs.get("q", [""])[0] or "")[:400]
                    self._json(m.search(q, limit=_int(qs.get("limit", [20])[0], 20, 1, 200)))
                elif u.path.startswith("/api/memory/"):
                    mid = u.path.rsplit("/", 1)[-1]
                    if not ID_RE.match(mid):
                        self._json({"error": "bad id"}, 400)
                        return
                    row = m.get(mid)
                    if not row:
                        self._json({"error": "not found"}, 404)
                    else:
                        row["edges"] = [dict(e) for e in m.db.execute(
                            "SELECT src,dst,rel,weight FROM links WHERE src=? OR dst=?", (mid, mid))]
                        self._json(row)
                else:
                    self._json({"error": "not found"}, 404)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._json({"error": "internal error"}, 500)
            finally:
                m.close()

    httpd, port = _bind(port, Handler)
    url = "http://127.0.0.1:%d/" % port
    print("memory graph: %s  (Ctrl-C 退出)" % url)
    sys.stderr.write("memory graph listening on %s\n" % url)
    if open_browser:
        webbrowser.open_new_tab(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()


def _inject(tpl: str, data) -> bytes:
    """把图数据塞进页面。

    转义 `<` 与 U+2028/U+2029：前者挡 `</script>` 与 `<!--`，后者会截断 JS 字符串字面量。
    两者都会让页面直接白屏，而正文来自不可信输入。
    """
    js = (json.dumps(data, ensure_ascii=False)
          .replace("<", "\\u003c")
          .replace("\u2028", "\\u2028")
          .replace("\u2029", "\\u2029"))
    return tpl.replace("__DATA__", js).encode()
