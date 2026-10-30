"""Tiny live server for the memory graph UI (stdlib only, loopback only)."""
from __future__ import annotations

import json
import os
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import autostart
from memcore import Memory

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "ui", "index.html")


def _bind(port: int, handler):
    """Bind the requested port, or the next few if it is taken (boot-time safety)."""
    last = None
    for p in [port] + [port + i for i in range(1, 10)]:
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), handler), p
        except OSError as e:
            last = e
    raise last


def serve(mem, port: int = 8787, open_browser: bool = False) -> None:
    home = mem.home

    def db():
        """One short-lived connection per request: sqlite objects are thread-bound."""
        return Memory(home)

    def graph(m, params):
        return m.graph_export(
            project=(params.get("project", [""])[0] or ""),
            limit=int(params.get("limit", ["700"])[0]),
            include_retired=params.get("include_retired", ["0"])[0] in ("1", "true"),
        )

    class Handler(BaseHTTPRequestHandler):
        server_version = "memcore/1.0"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _cors(self):
            origin = self.headers.get("Origin") or ""
            local = origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost")
            self.send_header("Access-Control-Allow-Origin", origin if local else "*")
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode())

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if n else b"{}"
            try:
                return json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return {}

        def do_POST(self):
            origin = self.headers.get("Origin") or ""
            if origin and not (origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost")):
                self._json({"error": "cross-origin write refused"}, 403)
                return
            u = urllib.parse.urlparse(self.path)
            payload = self._body()
            m = db()
            try:
                if u.path == "/api/llm/test":
                    p = m.llm_probe(base=payload.get("base", ""), model=payload.get("model", ""),
                                    key=payload.get("key", ""))
                    if p.get("ok") and payload.get("key"):
                        m.save_settings({"llm": {"key": payload["key"], "base": payload.get("base") or None,
                                                 "model": payload.get("model") or None}})
                    self._json(p)
                elif u.path == "/api/settings":
                    out = m.save_settings(payload)
                    auto = (out.get("maintenance") or {})
                    if "auto" in payload.get("maintenance", {}):
                        if auto.get("auto"):
                            out["autostart"] = autostart.enable(auto.get("interval_minutes", 30), m.home)
                        else:
                            out["autostart"] = autostart.disable()
                    self._json({"ok": True, "settings": out, "agents": autostart.agents()})
                elif u.path == "/api/action":
                    name = (payload.get("name") or "").strip()
                    if name == "maintain":
                        self._json({"ok": True, "result": m.maintain()})
                    elif name == "relink":
                        self._json({"ok": True, "result": {"edges": m.relink(rebuild=True)}})
                    elif name == "reindex":
                        self._json({"ok": True, "result": {"reindexed": m.reindex()}})
                    elif name in ("pin", "unpin", "retire", "forget"):
                        mid = payload.get("id") or ""
                        if not mid:
                            self._json({"error": "missing id"}, 400)
                        elif name == "pin":
                            m.update(mid, pinned=True, salience=max(0.9, float((m.get(mid) or {}).get("salience") or 0.9)))
                            self._json({"ok": True, "result": {"id": mid, "pinned": True}})
                        elif name == "unpin":
                            m.update(mid, pinned=False)
                            self._json({"ok": True, "result": {"id": mid, "pinned": False}})
                        elif name == "retire":
                            m.retire(mid, reason=payload.get("reason", "面板操作"))
                            self._json({"ok": True, "result": {"id": mid, "status": "retired"}})
                        else:
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
                            r = autostart.enable((m.cfg.get("maintenance") or {}).get("interval_minutes", 30), m.home)
                        else:
                            r = autostart.disable()
                        self._json({"ok": True, "result": r, "agents": autostart.agents()})
                    elif name == "panel_on":
                        self._json({"ok": True, "result": autostart.enable_ui(8790, m.home),
                                    "agents": autostart.agents()})
                    elif name == "panel_off":
                        self._json({"ok": True, "result": autostart.disable_ui(),
                                    "agents": autostart.agents()})
                    else:
                        self._json({"error": "unknown action: %s" % name}, 400)
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 500)
            finally:
                m.close()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            m = db()
            try:
                if u.path in ("/", "/index.html"):
                    with open(TPL, "r", encoding="utf-8") as fh:
                        tpl = fh.read()
                    data = json.dumps(graph(m, qs), ensure_ascii=False).replace("</", "<\\/")
                    self._send(200, tpl.replace("__DATA__", data).encode(), "text/html; charset=utf-8")
                elif u.path == "/api/graph":
                    self._json(graph(m, qs))
                elif u.path == "/api/stats":
                    st = m.stats()
                    st["agents"] = autostart.agents(panel_alive=True)
                    st["autostart"] = autostart.ui_status()
                    st["settings"] = m.settings()
                    self._json(st)
                elif u.path == "/api/agents":
                    self._json({"agents": autostart.agents(panel_alive=True)})
                elif u.path == "/api/settings":
                    self._json(m.settings())
                elif u.path == "/api/models":
                    self._json(m.llm_probe())
                elif u.path == "/api/search":
                    q = qs.get("q", [""])[0]
                    self._json(m.search(q, limit=int(qs.get("limit", ["20"])[0])))
                elif u.path.startswith("/api/memory/"):
                    mid = u.path.rsplit("/", 1)[-1]
                    row = m.get(mid)
                    if not row:
                        self._json({"error": "not found"}, 404)
                    else:
                        row["edges"] = [dict(e) for e in m.db.execute(
                            "SELECT src,dst,rel,weight FROM links WHERE src=? OR dst=?", (mid, mid))]
                        self._json(row)
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 500)
            finally:
                m.close()

    httpd, port = _bind(port, Handler)
    url = "http://127.0.0.1:%d/" % port
    print("memory graph: %s  (Ctrl-C 退出)" % url)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
