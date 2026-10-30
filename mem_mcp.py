#!/usr/bin/env python3
"""Minimal MCP (stdio) server exposing the memcore memory as tools.

Wire it into any MCP client:
  command: python3
  args: ["/Users/x/.dsh/memory-core/mem_mcp.py"]

Protocol: JSON-RPC 2.0 over stdio, newline-delimited JSON (Content-Length
framing is also accepted). Tools: memory_search, memory_recall, memory_brief,
memory_add, memory_get, memory_stats.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memcore import Memory  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "memory_search",
        "description": "Hybrid (lexical+semantic+recency) search over the agent's long-term memory. "
                       "Use before answering anything about past work, decisions, preferences or files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "natural language or keywords (CJK ok)"},
                "limit": {"type": "integer", "default": 8},
                "project": {"type": "string", "default": ""},
                "kind": {"type": "string", "description": "filter: fact|preference|decision|project|todo|episode|artifact|note|insight|identity"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_recall",
        "description": "Budget-packed context block for a query; returns ready-to-use lines with IDs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "budget": {"type": "integer", "default": 900},
                "project": {"type": "string", "default": ""},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_brief",
        "description": "Session-start capsule: identity, preferences, active projects, open loops, recent decisions.",
        "inputSchema": {
            "type": "object",
            "properties": {"budget": {"type": "integer", "default": 700}, "project": {"type": "string", "default": ""}},
        },
    },
    {
        "name": "memory_add",
        "description": "Persist one durable memory (deduped automatically). Use for decisions, facts, preferences, artifacts, TODOs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
                "title": {"type": "string", "default": ""},
                "kind": {"type": "string", "default": "note"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "project": {"type": "string", "default": ""},
                "pinned": {"type": "boolean", "default": False},
            },
            "required": ["body"],
        },
    },
    {
        "name": "memory_get",
        "description": "Fetch one memory verbatim by ID (M00042).",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "memory_stats",
        "description": "Counts, kinds, projects and storage size.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _read_message(stream):
    line = stream.readline()
    if not line:
        return None
    line = line.decode("utf-8", "replace").strip()
    if line.lower().startswith("content-length:"):
        length = int(line.split(":")[1].strip())
        while True:
            h = stream.readline()
            if not h or h in (b"\r\n", b"\n"):
                break
        body = stream.read(length)
        return json.loads(body.decode("utf-8"))
    if not line:
        return None
    return json.loads(line)


def _write(obj) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def _text(s: str):
    return {"content": [{"type": "text", "text": s}]}


def call_tool(m: Memory, name: str, args: dict):
    if name == "memory_search":
        hits = m.search(args.get("query", ""), limit=int(args.get("limit", 8)),
                        project=args.get("project", ""),
                        kinds=[args["kind"]] if args.get("kind") else [])
        if not hits:
            return _text("(no hits)")
        return _text("\n".join(
            "%s  %.2f  %s/%s  %s — %s" % (h["id"], h["score"], h["kind"],
                                         h["project"] or "-", h["title"], h["body"][:200])
            for h in hits))
    if name == "memory_recall":
        return _text(m.recall(args.get("query", ""), budget_tokens=int(args.get("budget", 900)),
                              project=args.get("project", ""))["text"])
    if name == "memory_brief":
        return _text(m.brief(budget_tokens=int(args.get("budget", 700)), project=args.get("project", "")))
    if name == "memory_add":
        r = m.add(args.get("body", ""), title=args.get("title", ""), kind=args.get("kind", "note"),
                  tags=args.get("tags") or [], project=args.get("project", ""),
                  source="mcp", pinned=bool(args.get("pinned")))
        m.export_markdown()
        return _text("stored %s%s" % (r["id"], " (deduped: %s)" % r["deduped"] if r.get("deduped") else ""))
    if name == "memory_get":
        r = m.get(args.get("id", ""))
        return _text(json.dumps(r, ensure_ascii=False, indent=2) if r else "not found")
    if name == "memory_stats":
        return _text(json.dumps(m.stats(), ensure_ascii=False, indent=2))
    raise KeyError("unknown tool: " + name)


def main() -> int:
    m = Memory()
    while True:
        try:
            msg = _read_message(sys.stdin.buffer)
        except Exception:
            break
        if msg is None:
            break
        mid = msg.get("id")
        method = msg.get("method", "")
        if method == "initialize":
            _write({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "memcore", "version": "1.0.0"}}})
        elif method in ("tools/list", "tools/list_request"):
            _write({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                res = call_tool(m, p.get("name", ""), p.get("arguments") or {})
                _write({"jsonrpc": "2.0", "id": mid, "result": res})
            except Exception as e:  # noqa: BLE001
                _write({"jsonrpc": "2.0", "id": mid,
                        "result": {"content": [{"type": "text", "text": "error: %s" % e}], "isError": True}})
        elif method in ("notifications/initialized", "initialized"):
            continue
        elif method == "ping":
            _write({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            _write({"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": "method not found: %s" % method}})
    m.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
