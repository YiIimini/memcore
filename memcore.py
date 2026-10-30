"""
memcore — local, dependency-free long-term memory for the agent.

Design goals
------------
1. Durable: SQLite (WAL) + append-only JSONL journal + Markdown mirror.
2. Retrievable: hybrid lexical (FTS5/trigram/BM25) + semantic (hashed
   char/word n-gram TF-IDF vectors) + graph + recency/salience priors,
   fused with RRF and reranked, then MMR-diversified and budget-packed.
3. Cheap: no external services, no model downloads, stdlib only.
4. Self-maintaining: decay/GC, dedupe on write, usage reinforcement,
   session-log ingestion, Markdown digest regeneration.

Python 3.9+ compatible. Import as:  from memcore import Memory
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import string
import subprocess
import sys
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# constants / config
# --------------------------------------------------------------------------

DEFAULT_HOME = os.path.expanduser("~/.dsh/memory")
VECTOR_DIM = 512
SCHEMA_VERSION = 3
DAY = 86400.0

KIND_DEFAULTS = {
    # kind: (salience, halflife_days)
    "identity": (1.00, 3650.0),
    "preference": (0.90, 720.0),
    "fact": (0.70, 540.0),
    "decision": (0.80, 240.0),
    "project": (0.85, 120.0),
    "artifact": (0.60, 180.0),
    "insight": (0.70, 400.0),
    "todo": (0.75, 30.0),
    "episode": (0.45, 45.0),
    "note": (0.50, 180.0),
}
KINDS = tuple(KIND_DEFAULTS)

DEFAULT_CONFIG = {
    "version": 5,
    "weights": {          # validated on the real store (12/12 paraphrase queries)
        "lexical": 0.50,
        "semantic": 0.22,
        "overlap": 0.12,
        "title": 0.16,
        "recency": 0.10,
        "salience": 0.10,
        "reinforce": 0.08,
        "pinned": 0.15,
    },
    "mmr_lambda": 0.72,
    "dedupe_exact": True,
    "dedupe_fuzzy": 0.965,
    "candidate_limit": 240,
    "vector_dim": VECTOR_DIM,
    "autolink": {"enabled": True, "min_sim": 0.20, "max_links": 4, "max_scan": 4000},
    "consolidate": {"dup_sim": 0.93, "conflict_sim": 0.74},
    "maintenance": {"auto": False, "interval_minutes": 30, "last_run": 0},
    "exclude": {"projects": [], "paths": [], "keywords": []},
    "llm": {"base": "http://127.0.0.1:8326/v1", "model": "deepseek-account/deepseek-flash",
            "gateway": True},
}

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
WORD_RE = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.\-]*")
SPLIT_RE = re.compile(r"[\s,;:!?、，。；：！？·|/\\(){}\[\]<>\"'`~@#$%^&*+=]+")


def now() -> int:
    return int(time.time())


def norm_text(s: str) -> str:
    s = s or ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def est_tokens(s: str) -> int:
    """Rough token estimate that is conservative for CJK."""
    if not s:
        return 0
    cjk = len(CJK_RE.findall(s))
    rest = max(0, len(s) - cjk)
    return int(math.ceil(cjk / 1.5 + rest / 3.6)) + 1


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()


# --------------------------------------------------------------------------
# hashing-trick vectorizer
# --------------------------------------------------------------------------

def _features(text: str) -> Iterable[str]:
    t = unicodedata.normalize("NFKC", norm_text(text)).lower()
    if not t:
        return []
    feats: List[str] = []
    words = WORD_RE.findall(t)
    for w in words:
        feats.append("w:" + w)
        if len(w) > 4:
            feats.append("p:" + w[:4])
    for a, b in zip(words, words[1:]):
        feats.append("b:%s_%s" % (a, b))
    # contiguous CJK runs -> char bi/tri grams
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]+", t):
        n = len(run)
        for i in range(n - 1):
            feats.append("c2:" + run[i:i + 2])
        for i in range(n - 2):
            feats.append("c3:" + run[i:i + 3])
    return feats


def embed(text: str, dim: int = VECTOR_DIM) -> bytes:
    """Signed feature hashing + sublinear TF + L2 normalisation -> float32 blob."""
    acc: Dict[int, float] = {}
    for f in _features(text):
        h = int(hashlib.blake2b(f.encode("utf-8", "ignore"), digest_size=8).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if (h >> 63) & 1 else -1.0
        acc[idx] = acc.get(idx, 0.0) + sign
    if not acc:
        return array.array("f", [0.0] * dim).tobytes()
    vec = [0.0] * dim
    norm = 0.0
    for i, s in acc.items():
        if s == 0.0:
            continue
        val = math.copysign(1.0 + math.log(abs(s)), s)
        vec[i] = val
        norm += val * val
    if norm <= 0:
        return array.array("f", [0.0] * dim).tobytes()
    inv = 1.0 / math.sqrt(norm)
    return array.array("f", [v * inv for v in vec]).tobytes()


def vec_of(blob: Optional[bytes], dim: int = VECTOR_DIM) -> array.array:
    if not blob:
        return array.array("f", [0.0] * dim)
    a = array.array("f")
    a.frombytes(blob)
    if len(a) != dim:
        return array.array("f", [0.0] * dim)
    return a


POPCOUNT = bytes(bin(i).count("1") for i in range(256))


CJK_STOP_CHARS = set("的了是在和与及或就都也还又而但却并很太更最我你他她它这那哪个些么什怎如何为以于对从到把被给让使需要会能可要是有没不")
CJK_STOP_BIGRAMS = {"我们", "你们", "他们", "这个", "那个", "什么", "怎么", "如何", "哪个",
                    "哪些", "可以", "需要", "现在", "已经", "然后", "因为", "所以", "还是",
                    "就是", "不是", "一个", "一下", "上次", "之前"}


def _grams(text: str) -> set:
    t = unicodedata.normalize("NFKC", norm_text(text)).lower()
    g = set()
    for run in re.findall(r"[\u3400-\u9fff]{2,}", t):
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg in CJK_STOP_BIGRAMS:
                continue
            if bg[0] in CJK_STOP_CHARS and bg[1] in CJK_STOP_CHARS:
                continue
            g.add(bg)
    for w in WORD_RE.findall(t):
        if len(w) >= 3:
            g.add(w)
    return g


def dice(a: set, b: set) -> float:
    """Query-side coverage: how much of the query's n-grams the doc contains.

    Asymmetric on purpose: short queries against long docs must not be diluted
    by doc length (a symmetric Dice score would hide them behind a 0.12 gate).
    """
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a))


def simhash(text: str) -> int:
    """64-bit SimHash over the same feature space; cheap prefilter for cosine."""
    votes = [0] * 64
    for f in _features(text):
        h = int.from_bytes(hashlib.blake2b(f.encode("utf-8", "ignore"), digest_size=8).digest(), "big")
        for b in range(64):
            votes[b] += 1 if (h >> b) & 1 else -1
    v = 0
    for b in range(64):
        if votes[b] > 0:
            v |= (1 << b)
    return v - (1 << 64) if v >= (1 << 63) else v


def ham(a: int, b: int) -> int:
    x = (a ^ b) & 0xFFFFFFFFFFFFFFFF
    raw = x.to_bytes(8, "big")
    return sum(POPCOUNT[c] for c in raw)


def cosine(a: array.array, b: array.array) -> float:
    # both are L2-normalised, so dot == cosine
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'global',
  tags TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
  source_ref TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  last_used_at INTEGER NOT NULL DEFAULT 0, use_count INTEGER NOT NULL DEFAULT 0,
  salience REAL NOT NULL DEFAULT 0.5, confidence REAL NOT NULL DEFAULT 0.7,
  pinned INTEGER NOT NULL DEFAULT 0, ttl_days INTEGER NOT NULL DEFAULT 0,
  hash TEXT UNIQUE, meta TEXT NOT NULL DEFAULT '{}', seq INTEGER,
  sim INTEGER NOT NULL DEFAULT 0,
  valid_from INTEGER NOT NULL DEFAULT 0, valid_to INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_mem_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_mem_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_mem_updated ON memories(updated_at);
CREATE TABLE IF NOT EXISTS tags (memory_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(memory_id, tag));
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE TABLE IF NOT EXISTS entities (memory_id TEXT NOT NULL, name TEXT NOT NULL, etype TEXT NOT NULL DEFAULT 'term', PRIMARY KEY(memory_id, name));
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE TABLE IF NOT EXISTS links (src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL DEFAULT 'related',
  weight REAL NOT NULL DEFAULT 1.0, created_at INTEGER NOT NULL, PRIMARY KEY(src, dst, rel));
CREATE TABLE IF NOT EXISTS vectors (memory_id TEXT PRIMARY KEY, v BLOB NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(memory_id UNINDEXED, text, tokenize='trigram');
CREATE TABLE IF NOT EXISTS access_log (id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT, ts INTEGER, q TEXT);
"""


class Memory:
    def __init__(self, home: Optional[str] = None, create: bool = True):
        self.home = os.path.abspath(home or os.environ.get("MEM_HOME") or DEFAULT_HOME)
        self.db_path = os.path.join(self.home, "memory.db")
        self.journal_path = os.path.join(self.home, "journal.jsonl")
        self.md_path = os.path.join(self.home, "MEMORY.md")
        self.config_path = os.path.join(self.home, "config.json")
        self.secrets_path = os.path.join(self.home, "secrets.json")
        self.cache_dir = os.path.join(self.home, "cache")
        if create:
            os.makedirs(self.home, exist_ok=True)
            os.makedirs(self.cache_dir, exist_ok=True)
        self.cfg = self._load_config()
        self.dim = int(self.cfg.get("vector_dim", VECTOR_DIM))
        self.db = sqlite3.connect(self.db_path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(memories)")]
        if "sim" not in cols:
            self.db.execute("ALTER TABLE memories ADD COLUMN sim INTEGER NOT NULL DEFAULT 0")
        for col, ddl in (("valid_from", "INTEGER NOT NULL DEFAULT 0"),
                         ("valid_to", "INTEGER NOT NULL DEFAULT 0"),
                         ("status", "TEXT NOT NULL DEFAULT 'active'")):
            if col not in cols:
                self.db.execute("ALTER TABLE memories ADD COLUMN %s %s" % (col, ddl))
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status)")
        self.db.commit()
        self._meta_init()

    # -- config / meta ----------------------------------------------------
    def _load_config(self) -> Dict[str, Any]:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            stale = int(user.get("version", 1)) < int(DEFAULT_CONFIG["version"])
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    if stale and k in ("weights", "consolidate", "autolink", "llm"):
                        continue  # scoring/algorithm changes come from code, not old files
                    cfg[k].update(v)
                else:
                    cfg[k] = v
            if stale:
                cfg["version"] = DEFAULT_CONFIG["version"]
                try:
                    with open(self.config_path, "w", encoding="utf-8") as fh:
                        json.dump(cfg, fh, ensure_ascii=False, indent=2)
                except Exception:
                    pass
        except Exception:
            with open(self.config_path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
        return cfg

    # -- settings ----------------------------------------------------------
    def settings(self, redact: bool = True) -> Dict[str, Any]:
        cfg = json.loads(json.dumps(self.cfg))
        key = self._llm_key()
        cfg["llm"]["configured"] = bool(key)
        if redact:
            cfg["llm"].pop("key", None)
        else:
            cfg["llm"]["key"] = key
        return cfg

    def _llm_key(self, base: str = "") -> str:
        """Local gateway keys come from the Harness itself; external bases use the stored key."""
        local = (base or (self.cfg.get("llm", {}) or {}).get("base", "")).startswith("http://127.0.0.1")
        gw = os.path.expanduser("~/.dsh/openai-gateway/api-key")
        if local:
            try:
                with open(gw, "r", encoding="utf-8") as fh:
                    k = fh.read().strip()
                if k:
                    return k
            except Exception:
                pass
        if os.environ.get("MEM_LLM_KEY"):
            return os.environ["MEM_LLM_KEY"]
        try:
            with open(self.secrets_path, "r", encoding="utf-8") as fh:
                k = (json.load(fh) or {}).get("MEM_LLM_KEY", "")
            if k:
                return k
        except Exception:
            pass
        # zero-config default: the Harness' own local OpenAI-compatible gateway
        gw = os.path.expanduser("~/.dsh/openai-gateway/api-key")
        try:
            with open(gw, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except Exception:
            return ""

    def save_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge a settings patch; the LLM key goes to secrets.json (0600)."""
        llm = patch.pop("llm", None) or {}
        key = llm.pop("key", None)
        if key is not None:
            data = {}
            if os.path.exists(self.secrets_path):
                try:
                    data = json.load(open(self.secrets_path, encoding="utf-8")) or {}
                except Exception:
                    data = {}
            if str(key).strip():
                data["MEM_LLM_KEY"] = str(key).strip()
            else:
                data.pop("MEM_LLM_KEY", None)
            with open(self.secrets_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.chmod(self.secrets_path, 0o600)
        if llm:
            self.cfg.setdefault("llm", {}).update(llm)

        def merge(dst, src):
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    merge(dst[k], v)
                else:
                    dst[k] = v

        merge(self.cfg, patch)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(self.cfg, fh, ensure_ascii=False, indent=2)
        self.dim = int(self.cfg.get("vector_dim", VECTOR_DIM))
        return self.settings()

    # -- exclusion ---------------------------------------------------------
    def _excluded(self, text: str = "", cwd: str = "", project: str = "") -> bool:
        ex = self.cfg.get("exclude", {}) or {}
        for p in ex.get("paths", []):
            if p and cwd and cwd.startswith(os.path.expanduser(p)):
                return True
        for p in ex.get("projects", []):
            if p and project and p == project:
                return True
        low = (text or "").lower()
        for kw in ex.get("keywords", []):
            if kw and kw.lower() in low:
                return True
        return False

    def backup(self, keep: int = 7) -> str:
        """Consistent SQLite snapshot into backups/, keeping the newest `keep` files."""
        bdir = os.path.join(self.home, "backups")
        os.makedirs(bdir, exist_ok=True)
        dest = os.path.join(bdir, "memory-%s.db" % time.strftime("%Y%m%d"))
        tmp = dest + ".tmp"
        try:
            with sqlite3.connect(tmp) as tgt:
                self.db.backup(tgt)
            os.replace(tmp, dest)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return ""
        old = sorted(f for f in os.listdir(bdir) if f.startswith("memory-") and f.endswith(".db"))
        for f in old[:-keep]:
            try:
                os.remove(os.path.join(bdir, f))
            except OSError:
                pass
        return dest

    def _rotate(self) -> None:
        """Keep the append-only journal bounded and the session cache small."""
        try:
            if os.path.getsize(self.journal_path) > 5 * 1024 * 1024:
                os.replace(self.journal_path, self.journal_path + ".1")
        except OSError:
            pass
        try:
            files = [os.path.join(self.cache_dir, f) for f in os.listdir(self.cache_dir) if f.endswith(".jsonl")]
            files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for p in files[20:]:
                os.remove(p)
        except OSError:
            pass

    def maintain(self) -> Dict[str, Any]:
        """One-shot routine: ingest new sessions, add edges, gc, rebuild mirror."""
        ing = self.ingest_sessions()
        edges = self.relink(rebuild=False)
        g = self.gc(dry_run=False)
        self.export_markdown()
        brief = self.brief(budget_tokens=700)
        try:
            with open(os.path.join(self.home, "BRIEF.md"), "w", encoding="utf-8") as fh:
                fh.write(brief)
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        self._rotate()
        backup = self.backup()
        self.cfg.setdefault("maintenance", {})["last_run"] = now()
        try:
            with open(self.config_path, "w", encoding="utf-8") as fh:
                json.dump(self.cfg, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return {"ingest": ing, "new_edges": edges, "gc": g["removed"],
                "backup": os.path.basename(backup) if backup else "",
                "total": self.stats()["total"]}

    def _meta_init(self) -> None:
        cur = self.db.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        if not cur:
            self.db.execute("INSERT INTO meta(k,v) VALUES('schema',?)", (str(SCHEMA_VERSION),))
            self.db.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('seq','0')")
            self.db.commit()

    def _next_id(self) -> str:
        row = self.db.execute("SELECT v FROM meta WHERE k='seq'").fetchone()
        n = int(row["v"]) + 1 if row else 1
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('seq',?)", (str(n),))
        return "M%05d" % n

    # -- write ------------------------------------------------------------
    def add(
        self,
        body: str,
        title: str = "",
        kind: str = "note",
        tags: Sequence[str] = (),
        project: str = "",
        source: str = "manual",
        source_ref: str = "",
        salience: Optional[float] = None,
        confidence: float = 0.7,
        pinned: bool = False,
        ttl_days: int = 0,
        meta: Optional[Dict[str, Any]] = None,
        ts: Optional[int] = None,
        dedupe: bool = True,
        valid_from: Optional[int] = None,
        valid_to: int = 0,
        status: str = "active",
        autolink: Optional[bool] = None,
    ) -> Dict[str, Any]:
        kind = kind if kind in KIND_DEFAULTS else "note"
        body = norm_text(body)
        title = norm_text(title) or self._auto_title(body)
        if not body and not title:
            raise ValueError("empty memory")
        if salience is None:
            salience = KIND_DEFAULTS[kind][0]
        tags = [t.strip().lower() for t in tags if t and t.strip()]
        h = sha1("%s|%s|%s" % (kind, title, body))

        if dedupe and self.cfg.get("dedupe_exact", True):
            row = self.db.execute("SELECT id, use_count, body FROM memories WHERE hash=?", (h,)).fetchone()
            if row:
                self.reinforce(row["id"])
                return {"id": row["id"], "deduped": "exact", "body": row["body"]}

        if dedupe and kind == "artifact":
            pm = re.search(r"路径：(.+)", body)
            if pm:
                pth = pm.group(1).strip()
                row = self.db.execute(
                    "SELECT id, body FROM memories WHERE kind='artifact' AND body LIKE ? LIMIT 1",
                    ("路径：" + pth + "%",)).fetchone()
                if row:
                    if len(body) > len(row["body"]):
                        self.update(row["id"], body=body)
                    self.reinforce(row["id"])
                    return {"id": row["id"], "deduped": "path", "body": row["body"]}

        v = embed(title + "\n" + body, self.dim)
        if dedupe and self.cfg.get("dedupe_fuzzy", 0) > 0:
            dup = self._fuzzy_duplicate(kind, v)
            if dup:
                self.reinforce(dup)
                return {"id": dup, "deduped": "fuzzy"}

        mid = self._next_id()
        t = int(ts or now())
        sim = simhash(title + "\n" + body)
        with self.db:
            self.db.execute(
                "INSERT INTO memories(id,kind,title,body,project,scope,tags,source,source_ref,"
                "created_at,updated_at,last_used_at,use_count,salience,confidence,pinned,ttl_days,hash,meta,seq,sim,"
                "valid_from,valid_to,status)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, kind, title, body, project, "global", ",".join(tags), source, source_ref,
                 t, t, 0, 0, float(salience), float(confidence), 1 if pinned else 0, int(ttl_days),
                 h, json.dumps(meta or {}, ensure_ascii=False), int(mid[1:]), sim,
                 int(valid_from or t), int(valid_to), status),
            )
            self.db.execute("INSERT INTO mem_fts(memory_id, text) VALUES(?,?)",
                            (mid, " ".join([title, body, " ".join(tags), project])))
            self.db.execute("INSERT INTO vectors(memory_id, v) VALUES(?,?)", (mid, v))
            for tg in tags:
                self.db.execute("INSERT OR IGNORE INTO tags(memory_id,tag) VALUES(?,?)", (mid, tg))
        do_link = self.cfg.get("autolink", {}).get("enabled", True) if autolink is None else autolink
        if do_link:
            try:
                self._auto_links(mid, title + "\n" + body, v)
            except Exception:
                pass
        self._journal("add", mid, {"kind": kind, "title": title})
        return {"id": mid, "deduped": None}

    def update(self, mid: str, **fields: Any) -> bool:
        row = self.get(mid)
        if not row:
            return False
        allowed = {"title", "body", "kind", "project", "salience", "confidence", "pinned", "ttl_days", "tags"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "body":
                v = norm_text(v)
            if k == "pinned":
                v = 1 if v else 0
            sets.append("%s=?" % k)
            vals.append(v)
        if not sets:
            return False
        sets.append("updated_at=?")
        vals.append(now())
        vals.append(mid)
        with self.db:
            self.db.execute("UPDATE memories SET %s WHERE id=?" % ",".join(sets), vals)
            if "tags" in fields and fields["tags"] is not None:
                self.db.execute("DELETE FROM tags WHERE memory_id=?", (mid,))
                for tg in [t.strip().lower() for t in fields["tags"] if t.strip()]:
                    self.db.execute("INSERT OR IGNORE INTO tags(memory_id,tag) VALUES(?,?)", (mid, tg))
            r = self.db.execute("SELECT title, body, tags, project FROM memories WHERE id=?", (mid,)).fetchone()
            self.db.execute("DELETE FROM mem_fts WHERE memory_id=?", (mid,))
            self.db.execute("INSERT INTO mem_fts(memory_id, text) VALUES(?,?)",
                            (mid, " ".join([r["title"], r["body"], r["tags"], r["project"]])))
            self.db.execute("UPDATE vectors SET v=? WHERE memory_id=?",
                            (embed(r["title"] + "\n" + r["body"], self.dim), mid))
            self.db.execute("UPDATE memories SET sim=? WHERE id=?",
                            (simhash(r["title"] + "\n" + r["body"]), mid))
        self._journal("update", mid, {k: v for k, v in fields.items() if k != "body"})
        return True

    def forget(self, mid: str) -> bool:
        row = self.get(mid)
        if not row:
            return False
        with self.db:
            for t in ("memories", "tags", "entities", "vectors", "links"):
                if t == "links":
                    self.db.execute("DELETE FROM links WHERE src=? OR dst=?", (mid, mid))
                elif t == "vectors":
                    self.db.execute("DELETE FROM vectors WHERE memory_id=?", (mid,))
                elif t == "tags":
                    self.db.execute("DELETE FROM tags WHERE memory_id=?", (mid,))
                elif t == "entities":
                    self.db.execute("DELETE FROM entities WHERE memory_id=?", (mid,))
                else:
                    self.db.execute("DELETE FROM memories WHERE id=?", (mid,))
            self.db.execute("DELETE FROM mem_fts WHERE memory_id=?", (mid,))
        self._journal("forget", mid, {"title": row["title"]})
        return True

    def reinforce(self, mid: str, amount: float = 0.03) -> None:
        with self.db:
            self.db.execute(
                "UPDATE memories SET use_count=use_count+1, last_used_at=?, "
                "salience=min(1.0, salience+?) WHERE id=?", (now(), amount, mid))
            self.db.execute("INSERT INTO access_log(memory_id, ts, q) VALUES(?,?,?)", (mid, now(), "reinforce"))

    def retire(self, mid: str, by: Optional[str] = None, at: Optional[int] = None,
               reason: str = "") -> bool:
        """Close a memory's validity window (superseded by another, or simply expired)."""
        row = self.get(mid)
        if not row:
            return False
        ts = int(at or now())
        with self.db:
            self.db.execute("UPDATE memories SET valid_to=?, status=?, updated_at=? WHERE id=?",
                            (ts, "superseded" if by else "retired", now(), mid))
            if reason:
                self.db.execute("UPDATE memories SET meta=? WHERE id=?",
                                (json.dumps({"retire_reason": reason}, ensure_ascii=False), mid))
            if by:
                self.db.execute(
                    "INSERT OR REPLACE INTO links(src,dst,rel,weight,created_at) VALUES(?,?,?,?,?)",
                    (by, mid, "supersedes", 1.0, now()))
        self._journal("retire", mid, {"by": by, "reason": reason})
        return True

    def history(self, mid: str) -> Dict[str, Any]:
        """Supersedes chain around one memory, ordered oldest -> newest."""
        older: List[Dict[str, Any]] = []
        cur, seen = mid, {mid}
        while True:
            row = self.db.execute(
                "SELECT dst FROM links WHERE src=? AND rel='supersedes' LIMIT 1", (cur,)).fetchone()
            if not row or row["dst"] in seen:
                break
            seen.add(row["dst"])
            r = self.get(row["dst"])
            if not r:
                break
            older.append(r)
            cur = row["dst"]
        older.reverse()
        newer: List[Dict[str, Any]] = []
        cur = mid
        while True:
            row = self.db.execute(
                "SELECT src FROM links WHERE dst=? AND rel='supersedes' LIMIT 1", (cur,)).fetchone()
            if not row or row["src"] in seen:
                break
            seen.add(row["src"])
            r = self.get(row["src"])
            if not r:
                break
            newer.append(r)
            cur = row["src"]
        me = self.get(mid)
        return {"chain": older + ([me] if me else []) + newer, "current": mid}

    def _auto_links(self, mid: str, text: str, vec: Optional[bytes] = None) -> int:
        cfg = self.cfg.get("autolink", {})
        qs = simhash(text)
        pre = [(ham(qs, int(r["sim"] or 0)), r["id"]) for r in self.db.execute(
            "SELECT id, sim FROM memories WHERE id<>? AND status='active' ORDER BY updated_at DESC LIMIT ?",
            (mid, int(cfg.get("max_scan", 4000))))]
        pre.sort()
        ids = [i for _, i in pre[:40]]
        if not ids:
            return 0
        qv = vec_of(vec if vec is not None else embed(text, self.dim), self.dim)
        rows = {r["memory_id"]: r["v"] for r in self.db.execute(
            "SELECT memory_id, v FROM vectors WHERE memory_id IN (%s)" % ",".join("?" * len(ids)), ids)}
        scored = sorted(((cosine(qv, vec_of(v, self.dim)), k) for k, v in rows.items()), reverse=True)
        n = 0
        for sim, other in scored:
            if sim < float(cfg.get("min_sim", 0.30)) or n >= int(cfg.get("max_links", 5)):
                break
            a, b = (mid, other) if mid < other else (other, mid)
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO links(src,dst,rel,weight,created_at) VALUES(?,?,?,?,?)",
                    (a, b, "related", round(sim, 3), now()))
            n += 1
        return n

    def link(self, src: str, dst: str, rel: str = "related", weight: float = 1.0) -> bool:
        if not self.get(src) or not self.get(dst):
            return False
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO links(src,dst,rel,weight,created_at) VALUES(?,?,?,?,?)",
                            (src, dst, rel, weight, now()))
        return True

    # -- read -------------------------------------------------------------
    def get(self, mid: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        return dict(row) if row else None

    def stats(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"home": self.home, "db": self.db_path, "total": 0}
        d["total"] = self.db.execute("SELECT count(*) c FROM memories").fetchone()["c"]
        d["by_kind"] = {r["kind"]: r["c"] for r in self.db.execute(
            "SELECT kind, count(*) c FROM memories GROUP BY kind ORDER BY c DESC")}
        d["by_project"] = {r["project"] or "(none)": r["c"] for r in self.db.execute(
            "SELECT project, count(*) c FROM memories GROUP BY project ORDER BY c DESC LIMIT 12")}
        d["by_status"] = {r["status"]: r["c"] for r in self.db.execute(
            "SELECT status, count(*) c FROM memories GROUP BY status")}
        d["retired"] = self.db.execute(
            "SELECT count(*) c FROM memories WHERE status!='active'").fetchone()["c"]
        d["pinned"] = self.db.execute("SELECT count(*) c FROM memories WHERE pinned=1").fetchone()["c"]
        d["links"] = self.db.execute("SELECT count(*) c FROM links").fetchone()["c"]
        d["db_bytes"] = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        row = self.db.execute("SELECT max(updated_at) m FROM memories").fetchone()
        d["last_write"] = row["m"] if row and row["m"] else None
        return d

    # -- retrieval --------------------------------------------------------
    def _select_candidates(self, query: str, project: str = "", kinds: Sequence[str] = (),
                           tags: Sequence[str] = (), limit: int = 240,
                           as_of: Optional[int] = None) -> Dict[str, Dict[str, float]]:
        cand: Dict[str, Dict[str, float]] = {}
        q = norm_text(query)
        where = []
        params: List[Any] = []
        if project:
            where.append("m.project=?")
            params.append(project)
        if kinds:
            where.append("m.kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend(kinds)
        if as_of:
            where.append("(m.valid_from=0 OR m.valid_from<=?)")
            where.append("(m.valid_to=0 OR m.valid_to>?)")
            params.extend([as_of, as_of])
        else:
            where.append("m.valid_to=0")
            where.append("m.status='active'")
        filt = (" AND " + " AND ".join(where)) if where else ""

        # 1. lexical: word tokens (>=3) + CJK 3-gram sliding windows
        tokens = [t for t in SPLIT_RE.split(q) if t]
        fts_terms: List[str] = []
        for t in tokens:
            if CJK_RE.search(t):
                for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]+", t):
                    if len(run) < 3:
                        continue
                    for i in range(len(run) - 2):
                        fts_terms.append('"%s"' % run[i:i + 3])
            elif len(t) >= 3:
                fts_terms.append('"%s"' % t.replace('"', '""'))
        fts_terms = list(dict.fromkeys(fts_terms))[:32]
        seen_lex: Dict[str, float] = {}
        if fts_terms:
            sql = ("SELECT f.memory_id AS id, bm25(mem_fts) AS s FROM mem_fts f "
                   "JOIN memories m ON m.id=f.memory_id WHERE mem_fts MATCH ?%s "
                   "ORDER BY s LIMIT ?" % filt)
            try:
                for r in self.db.execute(sql, [" OR ".join(fts_terms)] + params + [limit]):
                    seen_lex[r["id"]] = float(r["s"])
            except sqlite3.OperationalError:
                pass
        # LIKE fallback for short queries / when trigram retrieval is thin
        like_terms: List[str] = []
        if len(seen_lex) < 3:
            like_terms = [t for t in tokens if len(t) < 3]
            if not like_terms:
                for run in re.findall(r"[\u3400-\u9fff]{2,}", q):
                    like_terms.extend(run[i:i + 2] for i in range(min(6, len(run) - 1)))
            like_terms = list(dict.fromkeys(like_terms))[:6]
        for t in like_terms:
            sql = ("SELECT m.id AS id FROM memories m WHERE (m.title LIKE ? OR m.body LIKE ?)%s LIMIT 40" % filt)
            for r in self.db.execute(sql, ["%%%s%%" % t, "%%%s%%" % t] + params):
                seen_lex.setdefault(r["id"], -0.5)
        if not seen_lex and q:
            sql = ("SELECT m.id AS id FROM memories m WHERE (m.title LIKE ? OR m.body LIKE ?)%s LIMIT 60" % filt)
            for r in self.db.execute(sql, ["%%%s%%" % q, "%%%s%%" % q] + params):
                seen_lex.setdefault(r["id"], -0.5)

        for mid, s in seen_lex.items():
            cand.setdefault(mid, {})["bm25"] = s

        # 2. tag + entity signals
        tset = [t.lower() for t in tags] or [t.lower() for t in tokens if t]
        for t in tset[:10]:
            for r in self.db.execute("SELECT memory_id FROM tags WHERE tag=?", (t,)):
                cand.setdefault(r["memory_id"], {})["tag"] = 1.0
            for r in self.db.execute("SELECT memory_id FROM entities WHERE name=?", (t,)):
                cand.setdefault(r["memory_id"], {})["ent"] = 1.0

        # 2b. title bigram scan (catches short CJK terms that trigram FTS cannot)
        qb = [g for g in list(_grams(q)) if len(g) == 2][:8]
        if qb and len(cand) < 80:
            for r in self.db.execute("SELECT id, title FROM memories"):
                tl = (r["title"] or "").lower()
                if any(g in tl for g in qb):
                    cand.setdefault(r["id"], {}).setdefault("title", 1.0)

        # 3. semantic: 64-bit simhash prefilter (O(N) popcount), then exact cosine
        qs = simhash(q)
        pre: List[Tuple[int, str]] = []
        where_plain = [w.replace("m.", "") for w in where]
        sql_sim = ("SELECT id, sim FROM memories WHERE 1=1" +
                   (" AND " + " AND ".join(where_plain) if where_plain else ""))
        for r in self.db.execute(sql_sim, params):
            pre.append((ham(qs, int(r["sim"] or 0)), r["id"]))
        pre.sort()
        shortlist = [mid for _, mid in pre[: max(160, limit)]]
        if shortlist:
            qv = vec_of(embed(q, self.dim), self.dim)
            rows = {r["memory_id"]: r["v"] for r in self.db.execute(
                "SELECT memory_id, v FROM vectors WHERE memory_id IN (%s)" % ",".join("?" * len(shortlist)),
                shortlist)}
            scored = sorted(((cosine(qv, vec_of(v, self.dim)), mid) for mid, v in rows.items()), reverse=True)
            for c, mid in scored[: max(60, limit // 2)]:
                if c > 0.08:
                    cand.setdefault(mid, {})["sem"] = c

        # 4. pinned / recent always considered
        for r in self.db.execute(
                "SELECT id FROM memories WHERE pinned=1 OR updated_at>? ORDER BY updated_at DESC LIMIT 40",
                (now() - 14 * DAY,)):
            cand.setdefault(r["id"], {}).setdefault("rec", 1.0)
        return cand

    def search(self, query: str, limit: int = 10, project: str = "", kinds: Sequence[str] = (),
               tags: Sequence[str] = (), min_score: float = 0.0,
               as_of: Optional[int] = None) -> List[Dict[str, Any]]:
        q = norm_text(query)
        if not q:
            return []
        w = self.cfg["weights"]
        cand = self._select_candidates(q, project, kinds, tags, int(self.cfg["candidate_limit"]),
                                       as_of=as_of)
        if not cand:
            return []
        ids = list(cand.keys())
        rows = {}
        for chunk in _chunks(ids, 400):
            for r in self.db.execute(
                    "SELECT m.*, (SELECT v FROM vectors WHERE memory_id=m.id) AS v FROM memories m "
                    "WHERE m.id IN (%s)" % ",".join("?" * len(chunk)), chunk):
                if project and r["project"] != project:
                    continue
                if kinds and r["kind"] not in kinds:
                    continue
                if as_of:
                    if (r["valid_from"] or 0) > as_of or (r["valid_to"] or 0) and r["valid_to"] <= as_of:
                        continue
                elif r["valid_to"] or r["status"] != "active":
                    continue
                rows[r["id"]] = r
        qv = vec_of(embed(q, self.dim), self.dim)
        rows_vec = {mid: vec_of(r["v"], self.dim) for mid, r in rows.items()}
        sem = {mid: cosine(qv, rows_vec[mid]) for mid in rows}
        qg = _grams(q)
        ovl = {mid: dice(qg, _grams(r["title"] + " " + r["body"][:900])) for mid, r in rows.items()}
        tovl = {mid: dice(qg, _grams(r["title"])) for mid, r in rows.items()}
        # lexical normalisation from candidate bm25 values (lower = better)
        lex_raw = {mid: -float(cand[mid].get("bm25", 0.0)) for mid in rows}
        lo, hi = min(lex_raw.values(), default=0.0), max(lex_raw.values(), default=0.0)
        lexn = {m: ((lex_raw[m] - lo) / (hi - lo) if hi > lo else 0.0) for m in lex_raw}

        intent_file = bool(re.search(r"(文件|文档|路径|目录|哪份|哪篇|file|doc|path|directory)", q, re.I))
        t = now()
        out = []
        for mid, r in rows.items():
            halflife = KIND_DEFAULTS.get(r["kind"], (0.5, 180.0))[1]
            age_days = max(0.0, (t - int(r["updated_at"])) / DAY)
            rec = 0.5 ** (age_days / halflife)
            reinf = min(1.0, math.log1p(int(r["use_count"])) / math.log1p(25))
            s = (w["lexical"] * lexn.get(mid, 0.0)
                 + w["semantic"] * max(0.0, sem.get(mid, 0.0))
                 + w.get("overlap", 0.0) * ovl.get(mid, 0.0)
                 + w.get("title", 0.0) * tovl.get(mid, 0.0)
                 + w["recency"] * rec
                 + w["salience"] * float(r["salience"])
                 + w["reinforce"] * reinf
                 + (w["pinned"] if r["pinned"] else 0.0))
            # tag/entity bonus
            s += 0.05 * (cand[mid].get("tag", 0.0) + cand[mid].get("ent", 0.0))
            if project and r["project"] == project:
                s += 0.06
            if intent_file and r["kind"] in ("artifact", "project"):
                s += 0.10
            evidence = (lexn.get(mid, 0.0) > 0.0 or sem.get(mid, 0.0) >= 0.12
                        or cand[mid].get("title")
                        or ovl.get(mid, 0.0) >= 0.22
                        or tovl.get(mid, 0.0) >= 0.20
                        or cand[mid].get("tag") or cand[mid].get("ent"))
            if not evidence:
                continue
            if s >= min_score:
                out.append({
                    "id": mid, "kind": r["kind"], "title": r["title"], "body": r["body"],
                    "project": r["project"], "tags": [x for x in (r["tags"] or "").split(",") if x],
                    "score": round(s, 4), "semantic": round(sem.get(mid, 0.0), 3),
                    "lexical": round(lexn.get(mid, 0.0), 3), "recency": round(rec, 3),
                    "salience": float(r["salience"]), "pinned": bool(r["pinned"]),
                    "use_count": int(r["use_count"]), "created_at": int(r["created_at"]),
                    "updated_at": int(r["updated_at"]), "_sim": int(r["sim"] or 0),
                })
        out.sort(key=lambda x: x["score"], reverse=True)
        # MMR diversification on 64-bit simhash (hamming ~ cosine, ~50x cheaper)
        sel: List[Dict[str, Any]] = []
        pool = out[: max(limit * 3, 30)]
        lam = float(self.cfg.get("mmr_lambda", 0.72))
        while pool and len(sel) < limit:
            best, best_val = None, -1e9
            for c in pool:
                maxsim = 0.0
                for s0 in sel:
                    sim = 1.0 - ham(c["_sim"], s0["_sim"]) / 64.0
                    if sim > maxsim:
                        maxsim = sim
                val = lam * c["score"] - (1 - lam) * maxsim
                if val > best_val:
                    best, best_val = c, val
            pool.remove(best)
            sel.append(best)
        for s0 in sel:
            s0.pop("_sim", None)
        return sel

    def recall(self, query: str, budget_tokens: int = 900, project: str = "",
               kinds: Sequence[str] = (), compact: bool = False,
               as_of: Optional[int] = None) -> Dict[str, Any]:
        hits = self.search(query, limit=24, project=project, kinds=kinds, as_of=as_of)
        items, used = [], 0
        for h in hits:
            line = self._format_hit(h, compact=compact)
            cost = est_tokens(line)
            if used + cost > budget_tokens and items:
                break
            items.append({"id": h["id"], "line": line, "score": h["score"]})
            used += cost
            self.reinforce(h["id"], 0.01)
        header = "# 记忆召回 · %s · %d 条 · ~%d tok" % (query[:60], len(items), used)
        text = "\n".join([header] + [i["line"] for i in items])
        if not items:
            text = header + "\n(无匹配；可放宽 --budget / 去掉过滤，或先 mem add 写入事实)"
        text += "\n> 展开：mem get <ID>；补充：mem add -k fact -t 标题 -b 正文 --tags a,b"
        return {"text": text, "items": items, "tokens": used, "hits": len(hits)}

    @staticmethod
    def _format_hit(h: Dict[str, Any], compact: bool = False) -> str:
        day = time.strftime("%Y-%m-%d", time.localtime(h["created_at"]))
        body = re.sub(r"\s+", " ", h["body"])
        if compact:
            body = body[:110] + ("…" if len(body) > 110 else "")
        else:
            body = body[:240] + ("…" if len(body) > 240 else "")
        star = "★" if h["pinned"] else ""
        return "[%s] %s%s %s %s (%.2f) — %s — %s" % (
            h["id"], star, h["kind"], h["project"] or "-", day, h["score"], h["title"], body)

    # -- brief ------------------------------------------------------------
    def brief(self, budget_tokens: int = 800, project: str = "") -> str:
        t = now()
        lines: List[str] = ["# 记忆简报 · %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(t))]
        used = est_tokens(lines[0])

        def sect(title: str, rows: List[Dict[str, Any]], fmt=None, cap: int = 8) -> None:
            nonlocal used
            if not rows:
                return
            block = ["## " + title]
            for r in rows[:cap]:
                line = fmt(r) if fmt else self._format_hit(self._row_to_hit(r), compact=True)
                if used + est_tokens(line) > budget_tokens:
                    break
                block.append("- " + line)
                used += est_tokens(line)
            if len(block) > 1:
                lines.extend(block)

        def q(sql: str, params=()) -> List[Dict[str, Any]]:
            return [dict(r) for r in self.db.execute(sql, params)]

        pin_filter = " AND project=?" if project else ""
        pp = (project,) if project else ()
        sect("身份与长期偏好", q(
            "SELECT * FROM memories WHERE kind IN ('identity','preference') AND status='active'%s "
            "ORDER BY pinned DESC, salience DESC, use_count DESC LIMIT 12" % pin_filter, pp), cap=7)
        sect("进行中的项目", q(
            "SELECT * FROM memories WHERE kind='project' AND status='active'%s AND updated_at>? "
            "ORDER BY updated_at DESC LIMIT 10" % pin_filter, pp + (t - 21 * DAY,)), cap=5)
        sect("未闭环 / 待办", q(
            "SELECT * FROM memories WHERE kind='todo' AND status='active' AND meta NOT LIKE ?" + pin_filter +
            " ORDER BY salience DESC, updated_at DESC LIMIT 8",
            ('%"done": true%',) + pp), cap=5)
        sect("最近决策", q(
            "SELECT * FROM memories WHERE kind='decision' AND status='active'%s AND updated_at>? ORDER BY updated_at DESC LIMIT 6"
            % pin_filter, pp + (t - 45 * DAY,)), cap=4)
        sect("关键事实", q(
            "SELECT * FROM memories WHERE kind IN ('fact','insight') AND status='active'%s AND salience>=0.7 "
            "ORDER BY salience DESC, use_count DESC LIMIT 8" % pin_filter, pp), cap=4)
        if len(lines) == 1:
            lines.append("## 空\n- 记忆库尚无条目：用 mem add / mem ingest-sessions 建立。")
        st = self.stats()
        lines.append("> %d 条记忆 · %d 项目 · 扩展：mem recall \"<query>\" --budget 1200｜mem get <ID>"
                     % (st["total"], len([k for k in st["by_project"] if k != "(none)"])))
        return "\n".join(lines)

    @staticmethod
    def _row_to_hit(r: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": r["id"], "kind": r["kind"], "title": r["title"], "body": r["body"],
                "project": r["project"], "score": float(r.get("salience", 0.5)),
                "pinned": bool(r.get("pinned", 0)), "created_at": int(r["created_at"])}

    # -- graph / timeline -------------------------------------------------
    def graph(self, mid: str, depth: int = 1) -> Dict[str, Any]:
        nodes, edges = {}, []
        frontier = {mid}
        for d in range(depth + 1):
            nxt = set()
            for n in frontier:
                r = self.get(n)
                if not r:
                    continue
                nodes[n] = {"id": n, "kind": r["kind"], "title": r["title"], "depth": d}
                for e in self.db.execute(
                        "SELECT src,dst,rel,weight FROM links WHERE src=? OR dst=? LIMIT 60", (n, n)):
                    edges.append({"src": e["src"], "dst": e["dst"], "rel": e["rel"], "w": e["weight"]})
                    other = e["dst"] if e["src"] == n else e["src"]
                    if other not in nodes:
                        nxt.add(other)
            frontier = nxt
            if not frontier:
                break
        return {"nodes": list(nodes.values()), "edges": edges}

    def timeline(self, days: int = 30, project: str = "", limit: int = 40) -> List[Dict[str, Any]]:
        since = now() - days * DAY
        sql = "SELECT id,kind,title,project,created_at FROM memories WHERE created_at>=?"
        params: List[Any] = [since]
        if project:
            sql += " AND project=?"
            params.append(project)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.execute(sql, params)]

    # -- maintenance ------------------------------------------------------
    def _fuzzy_duplicate(self, kind: str, v: bytes) -> Optional[str]:
        thr = float(self.cfg.get("dedupe_fuzzy", 0.965))
        nv = vec_of(v, self.dim)
        for r in self.db.execute(
                "SELECT m.id, x.v FROM vectors x JOIN memories m ON m.id=x.memory_id "
                "WHERE m.kind=? ORDER BY m.updated_at DESC LIMIT 4000", (kind,)):
            if cosine(nv, vec_of(r["v"], self.dim)) >= thr:
                return r["id"]
        return None

    def gc(self, dry_run: bool = False) -> Dict[str, int]:
        t = now()
        expired = [dict(r) for r in self.db.execute(
            "SELECT id, ttl_days, created_at FROM memories WHERE ttl_days>0 AND pinned=0")]
        dead = [r["id"] for r in expired if r["created_at"] + r["ttl_days"] * DAY < t]
        cold = [dict(r) for r in self.db.execute(
            "SELECT id FROM memories WHERE kind='episode' AND use_count=0 AND created_at<?", (t - 120 * DAY,))]
        dead += [r["id"] for r in cold]
        dead = sorted(set(dead))
        if not dry_run:
            for mid in dead:
                self.forget(mid)
        return {"removed": len(dead), "ids": dead[:20]}

    def reindex(self) -> int:
        n = 0
        with self.db:
            self.db.execute("DELETE FROM mem_fts")
            self.db.execute("DELETE FROM vectors")
            for r in self.db.execute("SELECT * FROM memories"):
                self.db.execute("INSERT INTO mem_fts(memory_id,text) VALUES(?,?)",
                                (r["id"], " ".join([r["title"], r["body"], r["tags"], r["project"]])))
                self.db.execute("INSERT INTO vectors(memory_id,v) VALUES(?,?)",
                                (r["id"], embed(r["title"] + "\n" + r["body"], self.dim)))
                self.db.execute("UPDATE memories SET sim=? WHERE id=?",
                                (simhash(r["title"] + "\n" + r["body"]), r["id"]))
                n += 1
        return n

    def _journal(self, op: str, mid: str, extra: Dict[str, Any]) -> None:
        rec = {"ts": now(), "op": op, "id": mid}
        rec.update(extra)
        try:
            with open(self.journal_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def _auto_title(body: str) -> str:
        s = re.sub(r"\s+", " ", body).strip()
        return s[:42] + ("…" if len(s) > 42 else "")

    # -- export -----------------------------------------------------------
    def export_markdown(self, path: Optional[str] = None, limit: int = 4000) -> str:
        path = path or self.md_path
        st = self.stats()
        L = ["# MEMORY · 长期记忆索引", "",
             "> 自动生成 %s · %d 条 · 项目 %d 个" % (
                 time.strftime("%Y-%m-%d %H:%M"), st["total"],
                 len([k for k in st["by_project"] if k != "(none)"])), "",
             "## 索引导航", "- 身份/偏好 → `mem brief`", "- 语义检索 → `mem recall \"<query>\"`",
             "- 原始条目 → `mem get <ID>`", ""]
        for kind in ("identity", "preference", "project", "decision", "fact", "insight", "artifact", "todo", "note"):
            rows = [dict(r) for r in self.db.execute(
                "SELECT * FROM memories WHERE kind=? ORDER BY pinned DESC, salience DESC, updated_at DESC LIMIT ?",
                (kind, 200 if kind in ("preference", "fact") else 80))]
            if not rows:
                continue
            L.append("## %s (%d)" % (kind, len(rows)))
            for r in rows:
                day = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
                body = re.sub(r"\s+", " ", r["body"])[:180]
                L.append("- `%s` %s %s — **%s** — %s" % (r["id"], day, r["project"] or "-", r["title"], body))
            L.append("")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(L))
        return path

    def export_jsonl(self, path: str) -> int:
        n = 0
        with open(path, "w", encoding="utf-8") as fh:
            for r in self.db.execute("SELECT * FROM memories ORDER BY id"):
                fh.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
                n += 1
        return n

    # -- session ingestion ------------------------------------------------
    def ingest_sessions(self, sessions_root: Optional[str] = None, limit: int = 0,
                        project: str = "", force: bool = False) -> Dict[str, int]:
        root = sessions_root or os.path.expanduser("~/.dsh/sessions")
        helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "sessions_dump.js")
        if not os.path.exists(helper) or not os.path.exists(root):
            return {"sessions": 0, "added": 0, "note": "no sessions root or helper"}
        node = shutil.which("node") or next(
            (p for p in ("/Users/x/.local/bin/node", "/opt/homebrew/bin/node",
                         "/usr/local/bin/node", "/usr/bin/node") if os.path.exists(p)), "node")
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([os.path.dirname(node), env.get("PATH", "/usr/bin:/bin")])
        out = subprocess.run([node, helper, "--root", root, "--out", self.cache_dir],
                             capture_output=True, text=True, env=env)
        if out.returncode != 0:
            return {"sessions": 0, "added": 0, "note": (out.stderr or "")[:400]}
        files = [ln for ln in out.stdout.splitlines() if ln.endswith(".jsonl")]
        if force:
            with self.db:
                self.db.execute("DELETE FROM meta WHERE k LIKE 'ingest:%'")
        if limit:
            files = files[:limit]
        added = 0
        for f in files:
            try:
                added += self._ingest_one(f)
            except Exception:
                continue
        return {"sessions": len(files), "added": added}

    NOISE_PREFIX = (
        "Current runtime context", "<system-reminder", "Time sampled while preparing",
        "[model changed", "A skill is a reusable", "<available_skills", "<skill_content",
        "<project_instructions", "<|im_start|>", "[MODE:", "Current working directory",
        "background job ", "Job ", "[job ", "已完成后台任务", "后台任务 ",
    )
    WRITE_TOOLS = {"write", "edit", "apply_patch", "create_file", "str_replace", "multiedit",
                   "notebook_edit", "write_file", "append_file"}

    @classmethod
    def _is_noise(cls, text: str) -> bool:
        t = (text or "").lstrip()
        if not t:
            return True
        if len(t) > 6000:
            return True
        head = t[:40]
        for p in cls.NOISE_PREFIX:
            if head.startswith(p) or t.startswith(p):
                return True
        if "system-reminder" in t[:200]:
            return True
        return False

    def _ingest_one(self, path: str) -> int:
        sid = os.path.basename(path)[:-6] if path.endswith(".jsonl") else os.path.basename(path)
        st = os.stat(path)
        marker = "ingest:" + sid
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (marker,)).fetchone()
        if row and row["v"] == str(int(st.st_mtime)):
            return 0
        events = []
        with open(path, "r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    events.append(json.loads(ln))
                except Exception:
                    pass
        if not events:
            return 0
        meta = next((e for e in events if e.get("type") == "session"), {}) or {}
        cwd = meta.get("cwd", "")
        proj = os.path.basename(cwd.rstrip("/")) if cwd else ""
        if self._excluded("", cwd, proj):
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                                (marker, str(int(st.st_mtime))))
            return 0
        title = ""
        for e in events:
            if e.get("type") == "session/title":
                title = (e.get("data") or {}).get("title") or title
        added = 0
        pending = None
        assistant_buf: List[str] = []
        artifacts: Dict[str, str] = {}
        intents: List[str] = []

        def flush():
            nonlocal added, pending, assistant_buf
            if not pending:
                assistant_buf = []
                return
            utext, utime = pending
            atext = norm_text("\n".join(assistant_buf))[-800:]
            body = "用户：" + utext[:700] + ("\n回应：" + atext if atext.strip() else "")
            ttl = utext.replace("\n", " ").strip()
            r = self.add(body, title=ttl[:44], kind="episode", project=proj,
                         source="session", source_ref=sid, ts=utime,
                         tags=["session", proj] if proj else ["session"], salience=0.4)
            if not r.get("deduped"):
                added += 1
            pending = None
            assistant_buf = []

        for e in events:
            t = e.get("type")
            d = e.get("data") or {}
            if t == "user/message":
                texts = [c.get("text", "") for c in d.get("content", []) if c.get("type") == "text"]
                txt = norm_text(" ".join(texts))
                if self._is_noise(txt) or self._excluded(txt, cwd, proj):
                    continue
                flush()
                if len(txt.strip()) < 6:
                    continue
                pending = (txt, int(e.get("time", now()) // 1000))
                if len(txt) > 8:
                    intents.append(txt.replace("\n", " ")[:110])
            elif t == "assistant/message":
                msg = d.get("message") or {}
                for c in msg.get("content", []):
                    if c.get("type") == "text" and c.get("text"):
                        assistant_buf.append(c["text"])
                    elif c.get("type") == "tool-call":
                        name = (c.get("name") or "").lower()
                        if name not in self.WRITE_TOOLS:
                            continue
                        raw = c.get("arguments") or "{}"
                        if isinstance(raw, dict):
                            a = raw
                        else:
                            try:
                                a = json.loads(raw)
                            except Exception:
                                a = {}
                        pth = a.get("file_path") or a.get("path") or a.get("target") or a.get("filename")
                        if isinstance(pth, str) and 3 < len(pth) < 240:
                            artifacts.setdefault(pth, name)
            elif t == "session/end-seed":
                flush()
        flush()

        if intents:
            summary = "本会话意图序列：\n" + "\n".join("· " + i for i in intents[:10])
            r = self.add(summary, title="会话回顾：" + (title or (intents[0] if intents else sid))[:50],
                         kind="note", project=proj, source="session", source_ref=sid,
                         tags=["session-summary", proj] if proj else ["session-summary"],
                         salience=0.55)
            if not r.get("deduped"):
                added += 1

        skip_roots = (os.path.expanduser("~/.dsh/memory-core"), os.path.expanduser("~/.dsh/memory"))
        for pth, tool in list(artifacts.items())[:10]:
            ap = os.path.abspath(pth if os.path.isabs(pth) else os.path.join(cwd, pth))
            if ap.startswith(skip_roots):
                continue  # never let the memory system index itself
            base = os.path.basename(pth)
            head = self._doc_head(pth, cwd)
            r = self.add("路径：%s\n写入工具：%s\n会话：%s%s" % (pth, tool, sid, head),
                         title="产物 " + base, kind="artifact", project=proj,
                         source="session", source_ref=sid, salience=0.45,
                         tags=["artifact", proj] if proj else ["artifact"])
            if not r.get("deduped"):
                added += 1
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (marker, str(int(st.st_mtime))))
        return added

    # -- conflict resolution ------------------------------------------------
    def _llm_judge(self, a: Dict[str, Any], b: Dict[str, Any], timeout: int = 25):
        """Optional LLM adjudication of two near-duplicate memories.

        Configured entirely by env: MEM_LLM_BASE (OpenAI-compatible), MEM_LLM_KEY,
        MEM_LLM_MODEL. Returns (decision|None, note). Never raises.
        """
        cfg = self.cfg.get("llm", {}) or {}
        base = (os.environ.get("MEM_LLM_BASE") or cfg.get("base")
                or "https://api.deepseek.com/v1").rstrip("/")
        key = os.environ.get("MEM_LLM_KEY") or self._llm_key(base)
        model = os.environ.get("MEM_LLM_MODEL") or cfg.get("model") or "deepseek-flash"
        if not key:
            return None, "no MEM_LLM_KEY/DEEPSEEK_API_KEY"
        import urllib.request
        prompt = (
            "你是记忆库的冲突裁判。两条记忆可能重复、互相矛盾或后者细化前者。\n"
            '只输出 JSON：{"relation":"duplicate|contradicts|refines|unrelated",'
            '"keep":"a|b|both","merged":"<若互补则给出合并后的一句话，否则空>"}\n\n'
            "A(%s/%s): %s\n%s\n\nB(%s/%s): %s\n%s"
            % (a["id"], a["kind"], a["title"], a["body"][:600],
               b["id"], b["kind"], b["title"], b["body"][:600])
        )
        payload = {"model": model, "temperature": 0,
                   "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request(
            base + "/chat/completions", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            txt = data["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", txt, re.S)
            return (json.loads(m.group(0)) if m else None), "llm"
        except Exception as e:  # noqa: BLE001
            return None, "llm error: %s" % e

    def llm_probe(self, base: str = "", model: str = "", key: str = "", timeout: int = 15) -> Dict[str, Any]:
        """Verify an OpenAI-compatible endpoint + list its model ids."""
        import urllib.error
        import urllib.request
        cfg = self.cfg.get("llm", {}) or {}
        base = (base or cfg.get("base") or "https://api.deepseek.com/v1").rstrip("/")
        key = key or self._llm_key(base)
        model = model or cfg.get("model") or "deepseek-flash"
        if not key:
            return {"ok": False, "error": "未配置 API Key", "models": []}
        bases = [base]
        if re.match(r"^http://127\.0\.0\.1:\d+", base or ""):
            port = int(re.search(r":(\d+)", base).group(1))
            bases += ["http://127.0.0.1:%d/v1" % p for p in range(port + 1, port + 6)]
        for b in bases:
            out = self._probe_one(b, key, model, timeout)
            if out.get("ok") or "HTTP 4" in str(out.get("error")):
                out["base"] = b
                return out
        return self._probe_one(base, key, model, timeout)

    def _probe_one(self, base: str, key: str, model: str, timeout: int = 15) -> Dict[str, Any]:
        import urllib.error
        import urllib.request
        try:
            req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            models = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            return {"ok": True, "models": models, "model": model,
                    "model_ok": (not models) or (model in models)}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except Exception:
                pass
            return {"ok": False, "error": "HTTP %s %s" % (e.code, detail), "models": [], "model": model}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "models": [], "model": model}

    def consolidate(self, dry_run: bool = True, llm: bool = False, limit: int = 400,
                    kinds: Sequence[str] = ("fact", "decision", "preference", "note", "insight")) -> List[Dict[str, Any]]:
        """Find duplicate / conflicting memories; optionally merge or retire them.

        Duplicates (cos >= dup_sim) are auto-retired into the newer row.
        Conflicts (conflict_sim <= cos < dup_sim) are reported; with llm=True an
        adjudicator decides duplicate/refines/contradicts and the action follows.
        """
        cfg = self.cfg.get("consolidate", {})
        dup_sim = float(cfg.get("dup_sim", 0.94))
        conf_sim = float(cfg.get("conflict_sim", 0.80))
        rows = [dict(r) for r in self.db.execute(
            "SELECT * FROM memories WHERE status='active' AND kind IN (%s) "
            "ORDER BY updated_at DESC LIMIT ?" % ",".join("?" * len(kinds)), tuple(kinds) + (limit,))]
        if not rows:
            return []
        vecs = {r["memory_id"]: r["v"] for r in self.db.execute(
            "SELECT memory_id, v FROM vectors WHERE memory_id IN (%s)" % ",".join("?" * len(rows)),
            [r["id"] for r in rows])}
        report: List[Dict[str, Any]] = []
        used = set()
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if a["hash"] == b["hash"] or a["id"] in used or b["id"] in used:
                    continue
                if ham(int(a["sim"]), int(b["sim"])) > 12:
                    continue
                va, vb = vecs.get(a["id"]), vecs.get(b["id"])
                if not va or not vb:
                    continue
                sim = cosine(vec_of(va, self.dim), vec_of(vb, self.dim))
                if sim < conf_sim:
                    continue
                older, newer = (a, b) if a["created_at"] <= b["created_at"] else (b, a)
                action, verdict = "review", None
                if sim >= dup_sim:
                    action = "merge"
                elif llm:
                    verdict, note = self._llm_judge(older, newer)
                    if verdict:
                        rel = verdict.get("relation")
                        action = "merge" if rel in ("duplicate", "refines") else (
                            "retire" if rel == "contradicts" else "keep")
                report.append({"a": older["id"], "b": newer["id"], "sim": round(sim, 3),
                               "action": action, "verdict": verdict,
                               "titles": [older["title"], newer["title"]]})
                if not dry_run and action in ("merge", "retire"):
                    self.retire(older["id"], by=newer["id"],
                                reason="consolidate: %s (sim=%.3f)" % (action, sim))
                    used.add(older["id"])
                    if verdict and verdict.get("merged"):
                        merged = ("%s\n\n[合并] %s" % (newer["body"].rstrip(), verdict["merged"])).strip()
                        self.update(newer["id"], body=merged)
                elif action in ("merge", "retire"):
                    used.add(older["id"])
        return report

    def relink(self, rebuild: bool = True, limit: int = 4000) -> int:
        """(Re)build semantic `related` edges across the whole store."""
        if rebuild:
            with self.db:
                self.db.execute("DELETE FROM links WHERE rel='related'")
        rows = [dict(r) for r in self.db.execute(
            "SELECT m.id, m.title, m.body, x.v FROM memories m JOIN vectors x ON x.memory_id=m.id "
            "WHERE m.status='active' ORDER BY m.updated_at DESC LIMIT ?", (limit,))]
        n = 0
        for r in rows:
            n += self._auto_links(r["id"], r["title"] + "\n" + r["body"], r["v"])
        return n

    # -- graph / UI ---------------------------------------------------------
    def graph_export(self, project: str = "", kinds: Sequence[str] = (), limit: int = 700,
                     include_retired: bool = False) -> Dict[str, Any]:
        sql = "SELECT * FROM memories WHERE 1=1"
        params: List[Any] = []
        if not include_retired:
            sql += " AND status='active'"
        if project:
            sql += " AND project=?"
            params.append(project)
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            params.extend(kinds)
        sql += " ORDER BY pinned DESC, salience DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in self.db.execute(sql, params)]
        ids = {r["id"] for r in rows}
        degree = {r["id"]: 0 for r in rows}
        edges, seen = [], set()
        for e in self.db.execute("SELECT src,dst,rel,weight FROM links"):
            if e["src"] in ids and e["dst"] in ids:
                key = (min(e["src"], e["dst"]), max(e["src"], e["dst"]), e["rel"])
                if key in seen:
                    continue
                seen.add(key)
                edges.append({"source": e["src"], "target": e["dst"], "rel": e["rel"],
                              "w": round(float(e["weight"]), 3)})
                degree[e["src"]] += 1
                degree[e["dst"]] += 1
        nodes = [{
            "id": r["id"], "title": r["title"], "kind": r["kind"], "project": r["project"],
            "tags": [t for t in (r["tags"] or "").split(",") if t],
            "salience": round(float(r["salience"]), 3), "pinned": bool(r["pinned"]),
            "status": r["status"], "created_at": int(r["created_at"]),
            "valid_to": int(r["valid_to"] or 0), "degree": degree[r["id"]],
            "preview": re.sub(r"\s+", " ", r["body"])[:220],
        } for r in rows]
        return {"nodes": nodes, "edges": edges,
                "meta": {"generated": now(), "total": len(nodes), "links": len(edges)}}

    @staticmethod
    def _doc_head(path: str, cwd: str = "", limit: int = 260) -> str:
        """Short content digest for artifact memories, so they are findable by meaning."""
        cands = [path]
        if cwd and not os.path.isabs(path):
            cands.append(os.path.join(cwd, path))
        for p in cands:
            try:
                if not os.path.isfile(p) or os.path.getsize(p) > 400_000:
                    continue
                with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                    txt = fh.read(4000)
                if not txt.strip():
                    continue
                m = re.search(r"^#{1,3}\s+(.+)$", txt, re.M)
                heading = m.group(1).strip() if m else ""
                flat = re.sub(r"(?m)^\s*[-*+]\s+", "", txt)
                flat = re.sub(r"[#*`>\s]+", " ", flat)
                flat = re.sub(r"\s+", " ", flat).strip()
                return "\n内容摘要：%s%s" % (heading + " · " if heading else "", flat[:limit])
            except Exception:
                continue
        return ""

    def close(self) -> None:
        try:
            self.db.commit()
            self.db.close()
        except Exception:
            pass


def _chunks(seq: Sequence[Any], n: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
