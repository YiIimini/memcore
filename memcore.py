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
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# constants / config
# --------------------------------------------------------------------------

__version__ = "0.2.0"

DEFAULT_HOME = os.path.expanduser("~/.dsh/memory")
VECTOR_DIM = 512
SCHEMA_VERSION = 4
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
    "version": 6,
    "weights": {          # validated on the real store (12/12 paraphrase queries)
        # additive：老公式，相关性/时效/显著度线性相加（用近期性替代相关性）
        # multiplicative：LangMem 口径 —— 相关性 ×（重要度·强度），由生命周期激活度承担后半段
        "mode": "additive",
        "lexical": 0.50,
        "semantic": 0.22,
        "overlap": 0.12,
        "title": 0.16,
        "recency": 0.10,
        "salience": 0.10,
        "reinforce": 0.08,
        "pinned": 0.15,
        # 标签/实体是两条独立通道，不是同一个 0.05 常数（旧代码把两者相加乘 0.05，
        # 等于给任何信号都稀释成噪声 —— 实体表因此一直是死表也没人发现）
        "tag": 0.05,
        "entity": 0.12,
        # 派生摘要的排序折价（1.0 = 不折价）。会话回顾包含全部关键词，
        # 不折价时它会把一手条目全压下去。
        "derived": 0.90,
    },
    # 检索空结果时是否回落（只在"该有答案"的通道上开：recall 开，preflight 永不开）
    "recall": {"fallback": True, "fallback_n": 6},
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
    # 记忆的生命周期：激活度 → 清醒 / 模糊 / 沉睡 / 深度沉睡。
    # 时间衰减是主项（按 kind 半衰期），显著度是"重要权重"，
    # 唤醒权重给最近被想起的记忆加权，人工干预（pin / 让它沉睡）优先。
    "dormancy": {
        "enabled": True,
        "thresholds": {"fuzzy": 0.45, "dormant": 0.26, "deep": 0.15},
        "weights": {"recency": 0.52, "salience": 0.20, "reinforce": 0.12, "wake": 0.10},
        "pinned_floor": 0.80,
        # 每类记忆的"保质期"（天）：活多久才从清醒走到深度沉睡。
        # 和检索排序用的 KIND_DEFAULTS 半衰期是两回事 —— 排序关心"相关性新鲜度"，
        # 这里关心"这段记忆还活着吗"。会话片段本来就是快消品，所以只有一天半。
        "life_days": {"identity": 3650.0, "preference": 720.0, "decision": 180.0,
                      "project": 90.0, "fact": 120.0, "artifact": 10.0, "insight": 150.0,
                      "todo": 14.0, "episode": 1.2, "note": 10.0},
        "wake": {"halflife_hours": 72.0, "cooldown_minutes": 10},
        "trigger": {"dormant": 0.30, "deep": 0.55},
        "score": {"awake": 1.0, "fuzzy": 0.72, "dormant": 0.55, "deep": 0.40},
        "fuzzy": {"keep_head": 0.35, "keep_tail": 0.12, "density": 0.55, "title_keep": 6},
        "wake_on_search": True,
    },
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


# --------------------------------------------------------------------------
# 记忆生命周期：清醒 / 模糊 / 沉睡 / 深度沉睡
# --------------------------------------------------------------------------

STATE_LABELS = {"awake": "清醒", "fuzzy": "模糊", "dormant": "沉睡", "deep": "深度沉睡"}
# 踩过的坑用这个标签存，mem preflight 靠它把"雷区"捞出来
PITFALL_TAG = "pitfall"
STATE_ORDER = ("awake", "fuzzy", "dormant", "deep")


def clamp01(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def mask_text(text: str, seed: str, keep_head: float = 0.35, keep_tail: float = 0.12,
              density: float = 0.55) -> str:
    """雾化一段文字：首尾保留，中段按稳定伪随机遮罩。

    seed 决定花纹，所以同一条记忆每次看起来一样 —— 否则每刷新一次就换一副面孔，
    读起来像噪声而不是"记不清"。首尾保留是因为人回忆时开头和结尾最清楚。
    """
    s = re.sub(r"\s+", " ", text or "").strip()
    if not s:
        return ""
    if len(s) <= 12:
        return s[: max(2, len(s) - 2)] + "…"
    h = int(hashlib.sha1((seed or "").encode("utf-8")).hexdigest()[:16], 16)
    head = max(3, int(len(s) * float(keep_head)))
    tail = max(2, int(len(s) * float(keep_tail)))
    if head + tail >= len(s):
        head, tail = max(2, len(s) // 2), 1
    buf: List[str] = []
    run = 0
    for ch in s[head:len(s) - tail]:
        h = (h * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        if ((h >> 33) & 0xFFFF) / 65535.0 < float(density):
            run += 1
            if run <= 2:
                buf.append("░")
            elif run == 3:
                buf.append("…")
        else:
            buf.append(ch)
            run = 0
    return (s[:head] + "".join(buf) + s[len(s) - tail:]).strip()


LATIN_TOK_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,24}")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def extract_entities(title: str, tags: Sequence[str] = (), limit: int = 14) -> List[str]:
    """写入期抽取"这条记忆能被什么词叫醒"。

    名字即别名：标题里的 CJK 2~4 元、拉丁词与文件名主干。
    中文没有分词，所以按 n 元存 —— 查询侧同样按 n 元去撞，长句才命中得了。
    """
    text = " ".join([title or "", " ".join(tags or [])])
    out: List[str] = []
    for w in LATIN_TOK_RE.findall(text):
        w = w.lower()
        out.append(w)
        stem = w.split(".")[0]
        if stem and stem != w:
            out.append(stem)
    for run in CJK_RUN_RE.findall(title or ""):
        for n in (2, 3, 4):
            for i in range(len(run) - n + 1):
                out.append(run[i:i + n])
    seen: List[str] = []
    for e in out:
        e = e.strip("._-")
        if len(e) >= 2 and e not in seen:
            seen.append(e)
    return seen[: max(1, int(limit))]


def safe_base_url(base: str) -> bool:
    """Only public https or loopback http gateways are acceptable targets.

    Blocks the SSRF shape: an arbitrary base would receive our Authorization
    header and could probe LAN hosts / cloud metadata endpoints.
    """
    try:
        u = urllib.parse.urlsplit(base or "")
    except ValueError:
        return False
    if u.scheme == "https":
        return bool(u.hostname)
    if u.scheme == "http":
        return (u.hostname or "").lower() in ("127.0.0.1", "localhost", "::1")
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a 30x would forward the bearer token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
  status TEXT NOT NULL DEFAULT 'active',
  wake_count INTEGER NOT NULL DEFAULT 0, last_wake_at INTEGER NOT NULL DEFAULT 0,
  sleep_override TEXT NOT NULL DEFAULT ''
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
        # 逐列重读：一次读到的 cols 不会包含刚 ADD 的列，多列迁移会互相遮蔽
        for col, ddl in (("valid_from", "INTEGER NOT NULL DEFAULT 0"),
                         ("valid_to", "INTEGER NOT NULL DEFAULT 0"),
                         ("status", "TEXT NOT NULL DEFAULT 'active'"),
                         ("wake_count", "INTEGER NOT NULL DEFAULT 0"),
                         ("last_wake_at", "INTEGER NOT NULL DEFAULT 0"),
                         ("sleep_override", "TEXT NOT NULL DEFAULT ''")):
            have = {r[1] for r in self.db.execute("PRAGMA table_info(memories)")}
            if col not in have:
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
            tmp = self.secrets_path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self.secrets_path)
            try:
                os.chmod(self.secrets_path, 0o600)
            except OSError:
                pass
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
        ents = self.backfill_entities()
        edges = self.relink(rebuild=False)
        # 先落快照再执行破坏性 gc：否则误判只能对着「删完之后」的库叹气
        pre = None
        try:
            pre = self.backup()
        except Exception:  # noqa: BLE001
            pre = None
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
        return {"ingest": ing, "entities": ents, "new_edges": edges, "gc": g["removed"],
                "backup": os.path.basename(backup) if backup else "",
                "total": self.stats()["total"]}

    def _meta_init(self) -> None:
        cur = self.db.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        if not cur:
            self.db.execute("INSERT INTO meta(k,v) VALUES('schema',?)", (str(SCHEMA_VERSION),))
            self.db.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('seq','0')")
            self.db.commit()

    def _next_id(self) -> str:
        """Reserve the next sequence number atomically.

        读-改-写在两个进程之间会读到同一个值，第二个写入撞 PRIMARY KEY
        （实测 4×40 并发只落 110 行）。这里用单语句自增，必要时回退重试。
        """
        try:
            with self.db:
                row = self.db.execute(
                    "UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k='seq' RETURNING v"
                ).fetchone()
                if row is None:
                    self.db.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('seq','0')")
                    row = self.db.execute(
                        "UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k='seq' RETURNING v"
                    ).fetchone()
            return "M%05d" % int(row[0])
        except (sqlite3.OperationalError, TypeError):
            # SQLite < 3.35 没有 RETURNING：退回到加锁的读改写
            with self.db:
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
        touch: bool = True,
        event_at: Optional[int] = None,
    ) -> Dict[str, Any]:
        kind = kind if kind in KIND_DEFAULTS else "note"
        body = norm_text(body)
        title = norm_text(title) or self._auto_title(body)
        if not body and not title:
            raise ValueError("empty memory")
        if salience is None:
            salience = KIND_DEFAULTS[kind][0]
        tags = [t.strip().lower() for t in tags if t and t.strip()]
        h = sha1("%s|%s|%s|%s" % (kind, project or "", title, body))

        if dedupe and self.cfg.get("dedupe_exact", True):
            row = self.db.execute(
                "SELECT id, use_count, body FROM memories "
                "WHERE hash=? AND status='active' AND valid_to=0", (h,)).fetchone()
            if row:
                # touch=False 来自 ingest：灌库不等于"被想起"，否则记忆时钟永远不走
                if touch:
                    self.reinforce(row["id"])
                return {"id": row["id"], "deduped": "exact", "body": row["body"]}

        if dedupe and kind == "artifact":
            pm = re.search(r"路径：(.+)", body)
            if pm:
                pth = pm.group(1).strip()
                row = self.db.execute(
                    "SELECT id, body FROM memories WHERE kind='artifact' AND status='active' "
                    "AND valid_to=0 AND body LIKE ? LIMIT 1",
                    ("路径：" + pth + "%",)).fetchone()
                if row:
                    if len(body) > len(row["body"]):
                        self.update(row["id"], body=body)
                    if touch:
                        self.reinforce(row["id"])
                    return {"id": row["id"], "deduped": "path", "body": row["body"]}

        v = embed(title + "\n" + body, self.dim)
        if dedupe and self.cfg.get("dedupe_fuzzy", 0) > 0:
            dup = self._fuzzy_duplicate(kind, v, project)
            if dup:
                if touch:
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
                 # event_at = 事实发生的时间（双时态的"业务时间"轴）；
                 # 不传就回落到写入时间 ts —— 绝不写 0，0 在 as_of 语义里等于"无下界"
                 int(event_at or valid_from or t), int(valid_to), status),
            )
            self.db.execute("INSERT INTO mem_fts(memory_id, text) VALUES(?,?)",
                            (mid, " ".join([title, body, " ".join(tags), project])))
            self.db.execute("INSERT INTO vectors(memory_id, v) VALUES(?,?)", (mid, v))
            for tg in tags:
                self.db.execute("INSERT OR IGNORE INTO tags(memory_id,tag) VALUES(?,?)", (mid, tg))
            for ent in extract_entities(title, tags):
                self.db.execute(
                    "INSERT OR IGNORE INTO entities(memory_id,name,etype) VALUES(?,?,?)",
                    (mid, ent, "alias"))
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
        allowed = {"title", "body", "kind", "project", "salience", "confidence", "pinned",
                   "ttl_days", "tags", "meta"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "body":
                v = norm_text(v)
            if k == "pinned":
                v = 1 if v else 0
            if k == "meta":
                # dict 走合并而不是覆盖：triggers 之外还有 done/prio/retire_reason 这些键
                cur = {}
                try:
                    cur = json.loads(row.get("meta") or "{}")
                except (TypeError, ValueError):
                    cur = {}
                if not isinstance(cur, dict):
                    cur = {}
                if isinstance(v, dict):
                    cur.update(v)
                else:
                    try:
                        cur.update(json.loads(v) or {})
                    except (TypeError, ValueError):
                        continue
                v = json.dumps(cur, ensure_ascii=False)
            if k == "tags":
                # 标签写 tags 表，同时把 memories.tags 同步成逗号串；
                # 直接把 list 绑进 TEXT 列会抛 InterfaceError（标签因此永远改不动）
                tags_norm = [str(t).strip().lower() for t in v if str(t).strip()]
                sets.append("tags=?")
                vals.append(",".join(tags_norm))
                continue
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
            r = self.db.execute(
                "SELECT title, body, tags, project, kind FROM memories WHERE id=?", (mid,)).fetchone()
            self.db.execute("DELETE FROM entities WHERE memory_id=?", (mid,))
            for ent in extract_entities(r["title"], (r["tags"] or "").split(",")):
                self.db.execute(
                    "INSERT OR IGNORE INTO entities(memory_id,name,etype) VALUES(?,?,?)",
                    (mid, ent, "alias"))
            self.db.execute("DELETE FROM mem_fts WHERE memory_id=?", (mid,))
            self.db.execute("INSERT INTO mem_fts(memory_id, text) VALUES(?,?)",
                            (mid, " ".join([r["title"], r["body"], r["tags"], r["project"]])))
            self.db.execute("UPDATE vectors SET v=? WHERE memory_id=?",
                            (embed(r["title"] + "\n" + r["body"], self.dim), mid))
            if (self.db.execute("SELECT status FROM memories WHERE id=?", (mid,)).fetchone()
                    or {"status": "active"})["status"] == "active":
                self.db.execute("UPDATE memories SET hash=? WHERE id=?",
                                (sha1("%s|%s|%s|%s" % (r["kind"], r["project"] or "",
                                                       r["title"], r["body"])), mid))
            self.db.execute("UPDATE memories SET sim=? WHERE id=?",
                            (simhash(r["title"] + "\n" + r["body"]), mid))
        self._journal("update", mid, {k: v for k, v in fields.items() if k != "body"})
        return True

    def replace_in_body(self, mid: str, old: str, new: str,
                        require_unique: bool = True, replace_all: bool = False) -> Dict[str, Any]:
        """定点改记忆：old 必须逐字出现（且默认必须唯一）。

        借 Letta `memory_replace` 的契约：整字段覆盖是"幻觉原文"和"改错位置"的温床 ——
        模型记错一个字就静默改到别处，而定点替换会直接失败并把歧义行号摊出来。
        """
        row = self.get(mid)
        if not row:
            return {"ok": False, "error": "not found", "count": 0}
        body = row.get("body") or ""
        if not old:
            return {"ok": False, "error": "old 不能为空", "count": 0}
        count = body.count(old)
        if count == 0:
            return {"ok": False, "error": "old 未逐字找到", "count": 0,
                    "hint": "把要改的片段原样贴进来（不要凭印象重写）"}
        if count > 1 and require_unique and not replace_all:
            lines = [i + 1 for i, ln in enumerate(body.splitlines()) if old in ln]
            return {"ok": False, "error": "old 出现 %d 次，不唯一" % count,
                    "count": count, "lines": lines,
                    "hint": "多带一点上下文让它唯一，或显式 replace_all"}
        new_body = body.replace(old, new) if replace_all else body.replace(old, new, 1)
        ok = self.update(mid, body=new_body)
        return {"ok": bool(ok), "count": count, "replaced": count if replace_all else 1}

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
            # 让出去重 hash：否则同一事实重新写入会撞 UNIQUE，新记忆静默丢失
            freed = sha1("%s|retired|%s" % (row.get("hash") or "", mid))
            self.db.execute("UPDATE memories SET hash=? WHERE id=?", (freed, mid))
            if reason:
                try:
                    old_meta = json.loads(row.get("meta") or "{}")
                    if not isinstance(old_meta, dict):
                        old_meta = {}
                except (TypeError, ValueError):
                    old_meta = {}
                old_meta["retire_reason"] = reason
                self.db.execute("UPDATE memories SET meta=? WHERE id=?",
                                (json.dumps(old_meta, ensure_ascii=False), mid))
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
        d["by_state"] = self.state_counts()
        d["links"] = self.db.execute("SELECT count(*) c FROM links").fetchone()["c"]
        d["db_bytes"] = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        row = self.db.execute("SELECT max(updated_at) m FROM memories").fetchone()
        d["last_write"] = row["m"] if row and row["m"] else None
        return d

    # -- lifecycle: 清醒 / 模糊 / 沉睡 / 深度沉睡 ---------------------------
    def _dorm(self) -> Dict[str, Any]:
        d = self.cfg.get("dormancy")
        return d if isinstance(d, dict) else json.loads(json.dumps(DEFAULT_CONFIG["dormancy"]))

    def _dorm_thresholds(self) -> Tuple[float, float, float]:
        th = (self._dorm().get("thresholds") or {})
        return (float(th.get("fuzzy", 0.45)), float(th.get("dormant", 0.26)),
                float(th.get("deep", 0.12)))

    def _life_days(self, kind: str) -> float:
        """这类记忆的保质期（天）。配置缺失时退回检索用的半衰期。"""
        lives = self._dorm().get("life_days") or {}
        try:
            return max(0.01, float(lives.get(kind)))
        except (TypeError, ValueError):
            return float(KIND_DEFAULTS.get(kind, (0.5, 180.0))[1])

    def _recency_anchor(self, row: Dict[str, Any]) -> int:
        """最近一次"被想起"的时间：写入、被召回、或主动唤醒。

        召回也会算数 —— 用得着的记忆才活得久，没人碰的自然凉下去。
        """
        anchor = max(int(row.get("updated_at") or 0), int(row.get("last_wake_at") or 0),
                     int(row.get("last_used_at") or 0))
        # created_at 只作兜底：拿它当锚点，老记忆永远"刚出生"，年龄白算
        return anchor or int(row.get("created_at") or 0)

    def wake_weight(self, row: Dict[str, Any], t: Optional[int] = None) -> float:
        """唤醒权重：距上次唤醒按半衰期衰减，唤醒次数越多底子越厚。"""
        d = (self._dorm().get("wake") or {})
        wc = int(row.get("wake_count") or 0)
        lw = int(row.get("last_wake_at") or 0)
        if wc <= 0 or lw <= 0:
            return 0.0
        t = int(t or now())
        hl = max(0.5, float(d.get("halflife_hours", 72.0)))
        hours = max(0.0, (t - lw) / 3600.0)
        decay = 0.5 ** (hours / hl)
        return round(clamp01(decay * min(1.0, 0.8 + 0.07 * (wc - 1))), 4)

    def activation(self, row: Dict[str, Any], t: Optional[int] = None) -> float:
        """激活度：时间衰减（按 kind 半衰期）+ 显著度 + 复用 + 唤醒权重。

        pinned / 人工干预在这之后覆盖 —— 人按下的钉子比公式优先。
        """
        d = self._dorm()
        if not d.get("enabled", True):
            return 1.0
        t = int(t or now())
        w = d.get("weights") or {}
        kind = row.get("kind") or "note"
        halflife = self._life_days(kind)
        age_days = max(0.0, (t - self._recency_anchor(row)) / DAY)
        rec = 0.5 ** (age_days / halflife) if halflife > 0 else 0.0
        sal = clamp01(row.get("salience")) if row.get("salience") is not None else 0.5
        uses = max(0, int(row.get("use_count") or 0))
        reinf = min(1.0, math.log1p(uses) / math.log1p(25))
        act = (float(w.get("recency", 0.52)) * rec
               + float(w.get("salience", 0.20)) * sal
               + float(w.get("reinforce", 0.12)) * reinf
               + float(w.get("wake", 0.10)) * self.wake_weight(row, t))
        act = clamp01(act)
        fz, dm, _dp = self._dorm_thresholds()
        if int(row.get("pinned") or 0):
            act = max(act, clamp01(d.get("pinned_floor", 0.80)))
        ov = str(row.get("sleep_override") or "")
        if ov == "awake":
            act = max(act, min(1.0, fz + 0.05))
        elif ov == "dormant":
            act = min(act, max(0.0, dm - 0.02))
        return round(act, 4)

    def _next_state_in_days(self, row: Dict[str, Any], act: float,
                            t: Optional[int] = None) -> Optional[float]:
        """按纯时间衰减推算：还要几天掉到下一档。None = 已经到底 / 被钉住。"""
        d = self._dorm()
        if int(row.get("pinned") or 0) and not row.get("sleep_override"):
            return None
        fz, dm, dp = self._dorm_thresholds()
        target = fz if act >= fz else dm if act >= dm else dp if act >= dp else None
        if target is None:
            return None
        w = d.get("weights") or {}
        wr = float(w.get("recency", 0.52))
        if wr <= 0:
            return None
        kind = row.get("kind") or "note"
        halflife = self._life_days(kind)
        if halflife <= 0:
            return None
        t = int(t or now())
        sal = clamp01(row.get("salience")) if row.get("salience") is not None else 0.5
        uses = max(0, int(row.get("use_count") or 0))
        reinf = min(1.0, math.log1p(uses) / math.log1p(25))
        rest = (float(w.get("salience", 0.20)) * sal
                + float(w.get("reinforce", 0.12)) * reinf
                + float(w.get("wake", 0.10)) * self.wake_weight(row, t))
        need = (target - rest) / wr
        if need <= 0.0:
            return 0.0
        if need >= 1.0:
            return None
        age_days = max(0.0, (t - self._recency_anchor(row)) / DAY)
        return round(max(0.0, halflife * math.log2(1.0 / need) - age_days), 1)

    def memory_state(self, row: Dict[str, Any], t: Optional[int] = None) -> Dict[str, Any]:
        """一条记忆此刻的生命体征。UI 面板吃的就是这份结构。"""
        t = int(t or now())
        act = self.activation(row, t)
        fz, dm, dp = self._dorm_thresholds()
        state = ("awake" if act >= fz else "fuzzy" if act >= dm
                 else "dormant" if act >= dp else "deep")
        kind = row.get("kind") or "note"
        halflife = self._life_days(kind)
        touched_days = max(0.0, (t - self._recency_anchor(row)) / DAY)
        created = int(row.get("created_at") or self._recency_anchor(row))
        return {
            "state": state,
            "state_label": STATE_LABELS[state],
            "activation": act,
            # age_days = 这段记忆存在多久了；since_touch_days = 多久没被想起
            "age_days": round(max(0.0, (t - created) / DAY), 2),
            "since_touch_days": round(touched_days, 2),
            "halflife_days": halflife,
            "wake_count": int(row.get("wake_count") or 0),
            "last_wake_at": int(row.get("last_wake_at") or 0),
            "wake_weight": self.wake_weight(row, t),
            "sleep_override": str(row.get("sleep_override") or ""),
            "next_state_in_days": self._next_state_in_days(row, act, t),
        }

    @staticmethod
    def is_summary(row: Dict[str, Any]) -> bool:
        """派生摘要（会话回顾这类）不是一手事实，排序时要让位给它的来源条目。

        它们天生**包含所有关键词**（把整场对话的意图抄了一遍），在纯词法排序里
        必然压过原始条目 —— 评测集里最稳定的错误模式就是这个。
        """
        tags = (row.get("tags") or "")
        title = row.get("title") or ""
        return ("session-summary" in tags) or title.startswith("会话回顾")

    @staticmethod
    def memory_triggers(row: Dict[str, Any]) -> List[str]:
        """显式触发词：写在 meta.triggers 里，命中即唤醒（不受弱线索门槛限制）。"""
        raw = row.get("meta")
        if not raw:
            return []
        try:
            meta = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return []
        if not isinstance(meta, dict):
            return []
        out = []
        for t in (meta.get("triggers") or []):
            t = str(t).strip().lower()
            if t and t not in out:
                out.append(t)
        return out[:12]

    def sleep_scale(self, state: str) -> float:
        """模糊/沉睡的记忆仍能被召回，但分数要打折 —— 这就是"想不全"。"""
        return float((self._dorm().get("score") or {}).get(state, 1.0))

    def mask_body(self, row: Dict[str, Any], state: str) -> Tuple[str, str]:
        """返回 (标题, 正文) 的可见版本。清醒态原样返回。"""
        title = row.get("title") or ""
        body = row.get("body") or ""
        if state == "awake" or not self._dorm().get("enabled", True):
            return title, body
        fz = self._dorm().get("fuzzy") or {}
        density = {"fuzzy": float(fz.get("density", 0.55)),
                   "dormant": 0.75, "deep": 0.90}.get(state, 0.55)
        keep = int(fz.get("title_keep", 6))
        t_out = title if len(title) <= keep else title[:keep] + "…"
        b_out = mask_text(body, str(row.get("id") or ""), float(fz.get("keep_head", 0.35)),
                          float(fz.get("keep_tail", 0.12)), density)
        return t_out, b_out

    def wake(self, mid: str, reason: str = "manual") -> Optional[Dict[str, Any]]:
        """唤醒：唤醒次数与唤醒权重上去，激活度里的"新鲜度"重新计时。"""
        row = self.get(mid)
        if not row:
            return None
        if reason == "trigger":
            cd = float((self._dorm().get("wake") or {}).get("cooldown_minutes", 10)) * 60.0
            lw = int(row.get("last_wake_at") or 0)
            if cd > 0 and lw and now() - lw < cd:
                return None            # 冷却中：一次线索不该连点唤醒同一段记忆
        t = now()
        with self.db:
            if reason == "manual":
                # 人手点"唤醒"= 明确撤销之前按下的"让它沉睡"；线索唤醒不动人工干预
                self.db.execute(
                    "UPDATE memories SET wake_count=wake_count+1, last_wake_at=?, sleep_override='' "
                    "WHERE id=?", (t, mid))
            else:
                self.db.execute(
                    "UPDATE memories SET wake_count=wake_count+1, last_wake_at=? WHERE id=?", (t, mid))
        self._journal("wake", mid, {"reason": reason})
        return self.memory_state(self.get(mid) or row, t)

    def sleep(self, mid: str, reason: str = "manual") -> Optional[Dict[str, Any]]:
        """让它沉睡：人工把激活度压到沉睡档，但内容不删，强线索仍可唤回。"""
        row = self.get(mid)
        if not row:
            return None
        with self.db:
            self.db.execute("UPDATE memories SET sleep_override='dormant' WHERE id=?", (mid,))
        self._journal("sleep", mid, {"reason": reason})
        return self.memory_state(self.get(mid) or row)

    def unsleep(self, mid: str, reason: str = "manual") -> Optional[Dict[str, Any]]:
        """撤销人工干预，交还给时间与权重。"""
        row = self.get(mid)
        if not row:
            return None
        with self.db:
            self.db.execute("UPDATE memories SET sleep_override='' WHERE id=?", (mid,))
        self._journal("unsleep", mid, {"reason": reason})
        return self.memory_state(self.get(mid) or row)

    # 生命周期只认这几列。少一列（尤其 last_used_at）会让同一份库在
    # state_counts / graph_export / sleepers 之间算出三套不同的分布来。
    LIFE_COLS = ("id, kind, title, body, tags, project, salience, pinned, use_count, "
                 "created_at, updated_at, last_used_at, last_wake_at, wake_count, "
                 "sleep_override, meta")

    def state_counts(self, t: Optional[int] = None) -> Dict[str, int]:
        t = int(t or now())
        out = {k: 0 for k in STATE_ORDER}
        for r in self.db.execute(
                "SELECT %s FROM memories WHERE status='active' AND valid_to=0" % self.LIFE_COLS):
            out[self.memory_state(dict(r), t)["state"]] += 1
        return out

    def sleepers(self, limit: int = 60, t: Optional[int] = None) -> List[Dict[str, Any]]:
        """沉睡区：越接近遗忘的排前面。"""
        t = int(t or now())
        rows: List[Dict[str, Any]] = []
        for r in self.db.execute(
                "SELECT * FROM memories WHERE status='active' AND valid_to=0"):
            row = dict(r)
            st = self.memory_state(row, t)
            if st["state"] not in ("dormant", "deep"):
                continue
            _tt, b_out = self.mask_body(row, st["state"])
            rows.append({
                "id": row["id"], "title": row["title"], "kind": row["kind"],
                "project": row["project"], "tags": [x for x in (row["tags"] or "").split(",") if x],
                "preview": b_out[:180], "state": st["state"],
                "state_label": st["state_label"], "activation": st["activation"],
                "age_days": st["age_days"], "since_touch_days": st["since_touch_days"],
                "wake_count": st["wake_count"],
                "last_wake_at": st["last_wake_at"], "wake_weight": st["wake_weight"],
                "next_state_in_days": st["next_state_in_days"],
                "sleep_override": st["sleep_override"], "created_at": int(row["created_at"]),
                "triggers": self.memory_triggers(row),
            })
        rows.sort(key=lambda x: (x["activation"], x["created_at"]))
        return rows[: max(1, int(limit))]

    # -- preflight: 动手前的雷区清单 ---------------------------------------
    @staticmethod
    def _path_keys(paths: Sequence[str]) -> List[str]:
        """把文件路径压成可用于 tag 匹配的键：文件名、去扩展名、父目录名。"""
        out: List[str] = []
        for p in paths or ():
            p = str(p or "").strip()
            if not p:
                continue
            base = os.path.basename(p.rstrip("/\\"))
            if not base:
                continue
            out.append(base.lower())
            stem = os.path.splitext(base)[0].lower()
            if stem and stem != base.lower():
                out.append(stem)
            # 只取调用者自己写的目录前缀（ui/nebula.html → ui）；
            # 用 abspath 会把仓库根目录名（memory-core）当键，一查就命中全库的坑
            rel_dir = os.path.dirname(p.rstrip("/"))
            parent = os.path.basename(rel_dir.rstrip("/")) if rel_dir else ""
            if parent:
                out.append(parent.lower())
        return list(dict.fromkeys([k for k in out if k]))

    def preflight(self, paths: Sequence[str] = (), topic: str = "",
                  limit: int = 10, budget_tokens: int = 900) -> Dict[str, Any]:
        """开工前查一次：这块代码/这个主题以前踩过什么坑，结论是什么。

        记忆不会自己浮出来 —— 检索总得有人按开关。这条命令就是开关：
        tag 精确命中优先（坑是按文件名存的），语义检索兜底。
        """
        t = now()
        keys = self._path_keys(paths)
        scored: Dict[str, float] = {}

        def bump(mid: str, amount: float) -> None:
            scored[mid] = scored.get(mid, 0.0) + amount

        # 1) 按文件名/目录名的 tag 精确命中
        for k in keys:
            for r in self.db.execute("SELECT memory_id FROM tags WHERE tag=?", (k,)):
                bump(r["memory_id"], 1.0)
        # 2) 没给文件名（只给了主题）时，所有雷区都摊开；
        #    给了文件名就只认命中的 —— 查 X 文件不该被别的模块的坑淹掉
        if not keys:
            for r in self.db.execute("SELECT memory_id FROM tags WHERE tag=?", (PITFALL_TAG,)):
                bump(r["memory_id"], 0.35)
        # 3) 语义/词法兜底：主题词 + 文件名本身
        q = " ".join([topic or ""] + keys).strip()
        if q:
            for h in self.search(q, limit=max(6, limit), min_score=0.0)[:10]:
                if h.get("state") != "awake":
                    continue
                bump(h["id"], 0.45 * min(1.0, float(h.get("score") or 0) / 1.2))
        # 4) pinned 偏好永远值得看
        for r in self.db.execute(
                "SELECT id FROM memories WHERE pinned=1 AND status='active' AND kind='preference'"):
            bump(r["id"], 0.12)

        if not scored:
            return {"text": "# 动手前检查 · %s\n(没有相关记录；这次踩的坑请用 mem learn 写回来)"
                            % ("、".join(keys[:6]) or topic or "-"),
                    "items": [], "keys": keys}

        rows = {}
        for mid in scored:
            row = self.get(mid)
            if not row or row.get("status") != "active":
                continue
            rows[mid] = row

        def rank(mid: str) -> float:
            r = rows[mid]
            age = max(0.0, (t - int(r["updated_at"])) / DAY)
            rec = 0.5 ** (age / 180.0)
            return scored[mid] + 0.25 * rec + 0.15 * clamp01(r.get("salience"))

        # 坑优先铺满；非坑的只留最相关 3 条，否则一次检查就变成"再来十篇背景资料"
        ordered_all = sorted(rows, key=rank, reverse=True)
        pits = [x for x in ordered_all if PITFALL_TAG in set(
            y for y in (rows[x].get("tags") or "").split(",") if y)]
        rest = [x for x in ordered_all if x not in set(pits)][:3]
        ordered = (pits + rest)[: max(1, int(limit))]
        items, lines, used = [], [], 0
        label = "、".join(keys[:6]) + (" 等 %d 个" % len(keys) if len(keys) > 6 else "")
        head = "# 动手前检查 · %s" % (label or topic or "-")
        for mid in ordered:
            r = rows[mid]
            tags = set(x for x in (r.get("tags") or "").split(",") if x)
            is_pit = PITFALL_TAG in tags
            mark = "⚠" if is_pit else ("★" if r.get("pinned") else "·")
            body = re.sub(r"\s+", " ", r.get("body") or "")
            line = "%s [%s] %s — %s" % (mark, mid, r.get("title") or "", body[:220])
            if used + est_tokens(line) > budget_tokens and items:
                break
            lines.append(line)
            used += est_tokens(line)
            keys_of = [x for x in tags if x != PITFALL_TAG]
            primary = next((k for k in keys_of if "." in k or "/" in k),
                           keys_of[0] if keys_of else "")
            items.append({"id": mid, "title": r.get("title"), "kind": r.get("kind"),
                          "pitfall": is_pit, "score": round(rank(mid), 3),
                          "file": primary, "files": sorted(keys_of), "tags": sorted(tags)})
        pit_n = sum(1 for i in items if i["pitfall"])
        text = "\n".join([head + " · %d 条（坑 %d）" % (len(items), pit_n)] + lines)
        if pit_n:
            text += "\n> 命中的坑按上面写的修法做，不要重新发明一遍。"
        return {"text": text, "items": items, "keys": keys, "tokens": used}

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
            rows_e = list(self.db.execute(
                "SELECT memory_id FROM entities WHERE name=?", (t,)))
            if rows_e:
                # 整词命中也要做 df 抑制：否则"方案"这种词给每条记忆都加满同样的分
                df = len(rows_e)
                wgt = round(1.0 / math.log(1 + df + 1), 4)
                for r in rows_e:
                    cur = cand.setdefault(r["memory_id"], {})
                    if cur.get("ent", 0.0) < wgt:
                        cur["ent"] = wgt
                    cur["ent_exact"] = max(cur.get("ent_exact", 0.0), wgt)

        # 2a) 实体通道：中文没有分词，整句当词只能撞到完全同名的实体，
        #     所以像写入端一样按 n 元去撞。权重按 df 抑制（HippoRAG 的 hub 抑制）：
        #     "方案""实现"这种到处都有的实体不该给任何一条记忆加分。
        grams = sorted(_grams(q))[:16]
        if grams:
            # 一次拿齐 df 与命中行：每个 gram 单独查两次会变成 32 条 SQL，5k 条时能吃掉一半延迟
            marks = ",".join("?" * len(grams))
            hit_rows = list(self.db.execute(
                "SELECT name, memory_id FROM entities WHERE name IN (%s)" % marks, grams))
            df_cache = {r["name"]: 0 for r in hit_rows}
            for r in hit_rows:
                df_cache[r["name"]] += 1
            for r in hit_rows:
                df = df_cache.get(r["name"]) or 0
                if df <= 0:
                    continue
                wgt = round(1.0 / math.log(1 + df + 1), 4)   # df=1 → 0.91，df=10 → 0.40
                cur = cand.setdefault(r["memory_id"], {})
                if cur.get("ent", 0.0) < wgt:
                    cur["ent"] = wgt

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
                rows[r["id"]] = dict(r)      # 生命周期逻辑用 dict 访问（.get），Row 不支持
        qv = vec_of(embed(q, self.dim), self.dim)
        rows_vec = {mid: vec_of(r["v"], self.dim) for mid, r in rows.items()}
        sem = {mid: cosine(qv, rows_vec[mid]) for mid in rows}
        qg = _grams(q)
        ovl = {mid: dice(qg, _grams(r["title"] + " " + r["body"][:900])) for mid, r in rows.items()}
        tovl = {mid: dice(qg, _grams(r["title"])) for mid, r in rows.items()}
        # 词法归一化：sigmoid 而不是 min-max。
        # min-max 依赖候选集，只有一条候选时恒等于 1.0（旧代码注释里踩过这个坑）；
        # sigmoid 是绝对刻度：raw≈阈值 → 0.5，与同批有几条候选无关。
        # 参数按查询词数取（Mem0 scoring.py 同一思路）：短查询更容易是精确查询，阈值低一点。
        lex_raw = {mid: -float(cand[mid].get("bm25", 0.0)) for mid in rows}
        norm_mode = str(w.get("norm") or "sigmoid")
        if norm_mode == "minmax":
            # 老公式，留着做 A/B：依赖候选集，单条候选时恒等于 1.0
            lo = min(lex_raw.values(), default=0.0)
            hi = max(lex_raw.values(), default=0.0)
            lexn = {m: ((lex_raw[m] - lo) / (hi - lo) if hi > lo else 0.0) for m in lex_raw}
        else:
          # 查询越短越可能是精确查询：阈值放低；长句里关键词被稀释，阈值抬高
          qlen = len(q or "")
          if qlen <= 6:
              m0, k = 5.0, 0.7
          elif qlen <= 16:
              m0, k = 9.0, 0.6
          else:
              m0, k = 12.0, 0.5
          lexn = {m: 1.0 / (1.0 + math.exp(-k * (lex_raw[m] - m0))) for m in lex_raw}

        intent_file = bool(re.search(r"(文件|文档|路径|目录|哪份|哪篇|file|doc|path|directory)", q, re.I))
        t = now()
        dorm = self._dorm()
        dorm_on = bool(dorm.get("enabled", True))
        trig_cfg = dorm.get("trigger") or {}
        wake_on_search = bool(dorm.get("wake_on_search", True))
        rank_mode = str(w.get("mode") or "additive")
        # 雷区是"关于记忆的记忆"，不该跟业务记忆抢普通召回的席位：
        # 只有查询本身在问坑/雷区/教训时才带出来（preflight 走自己的通道）
        pit_hint = bool(re.search(r"坑|雷区|教训|pitfall|preflight|踩过", q, re.I))
        ql = (q or "").lower()
        out = []
        for mid, r in rows.items():
            halflife = KIND_DEFAULTS.get(r["kind"], (0.5, 180.0))[1]
            anchor = self._recency_anchor(r)
            age_days = max(0.0, (t - anchor) / DAY)
            rec = 0.5 ** (age_days / halflife)
            reinf = min(1.0, math.log1p(int(r["use_count"])) / math.log1p(25))
            sim_part = (w["lexical"] * lexn.get(mid, 0.0)
                        + w["semantic"] * max(0.0, sem.get(mid, 0.0))
                        + w.get("overlap", 0.0) * ovl.get(mid, 0.0)
                        + w.get("title", 0.0) * tovl.get(mid, 0.0))
            if rank_mode == "multiplicative":
                # 相关性是乘子的一半；另一半交给生命周期激活度（时间×重要度×唤醒）
                act = self.activation(r, t) if dorm_on else 1.0
                s = sim_part * (0.55 + 0.45 * act)
            else:
                s = (sim_part
                     + w["recency"] * rec
                     + w["salience"] * float(r["salience"])
                     + w["reinforce"] * reinf
                     + (w["pinned"] if r["pinned"] else 0.0))
            if not pit_hint and PITFALL_TAG in (r["tags"] or ""):
                continue
            if self.is_summary(r):
                s *= float(w.get("derived", 0.85))
            # 标签与实体是两条独立通道，各有权重（不再是同一个 0.05 常数）
            s += (w.get("tag", 0.05) * cand[mid].get("tag", 0.0)
                  + w.get("entity", 0.12) * cand[mid].get("ent", 0.0))
            if project and r["project"] == project:
                s += 0.06
            if intent_file and r["kind"] in ("artifact", "project"):
                s += 0.10
            tl = (r["title"] or "").lower()
            bl = (r["body"] or "").lower()
            # 正文里精确出现算 0.50：够叫醒"沉睡"，不够叫醒"深度沉睡"（0.55 门槛）
            contain = 0.80 if (ql and ql in tl) else 0.50 if (ql and ql in bl) else 0.0
            # 显式触发词：这条记忆自己声明的"想起它的暗号"
            cues = [c for c in self.memory_triggers(r) if c and c in ql]
            has_lex = "bm25" in cand[mid]     # 走过词法通道即算证据，强度由 lexn 负责排序
            evidence = (has_lex or sem.get(mid, 0.0) >= 0.12
                        or cand[mid].get("title")
                        or ovl.get(mid, 0.0) >= 0.22
                        or tovl.get(mid, 0.0) >= 0.20
                        or cand[mid].get("tag") or cand[mid].get("ent")
                        or contain > 0.0            # 精确短语命中：归一化后可能为 0，但它是硬证据
                        or bool(cues))              # 触发词命中：本来就是"这条记忆的钥匙"
            if not evidence:
                continue

            # ---- 生命周期：沉睡的记忆不是不存在，是"线索不够就叫不醒" ----
            st = self.memory_state(r, t) if dorm_on else {
                "state": "awake", "state_label": STATE_LABELS["awake"], "activation": 1.0,
                "age_days": round(age_days, 2), "since_touch_days": round(age_days, 2),
                "halflife_days": halflife, "wake_count": 0,
                "last_wake_at": 0, "wake_weight": 0.0, "sleep_override": "",
                "next_state_in_days": None}
            state = st["state"]
            exact = bool(cand[mid].get("tag") or cand[mid].get("ent_exact") or cues)
            # 触发强度只看绝对量：批量归一化过的 lexn 会让"只有一条结果的短查询"
            # 显得极端匹配，一句话就把整库叫醒。
            trigger = max(0.60 * ovl.get(mid, 0.0) + 0.40 * max(0.0, sem.get(mid, 0.0)), contain)
            if cues:
                trigger = max(trigger, 0.95)
            if exact:
                trigger += 0.20
            trigger = round(clamp01(trigger), 4)
            if not exact:
                if state == "dormant" and trigger < float(trig_cfg.get("dormant", 0.30)):
                    continue
                if state == "deep" and trigger < float(trig_cfg.get("deep", 0.55)):
                    continue
            s *= self.sleep_scale(state)
            if s < min_score:
                continue
            woke = False
            if state in ("dormant", "deep") and wake_on_search:
                if self.wake(mid, "trigger") is not None:
                    woke = True
                    st = self.memory_state(self.get(mid) or r, now())
                    state = st["state"]
            title_out, body_out = self.mask_body(r, state)
            out.append({
                "id": mid, "kind": r["kind"], "title": title_out, "body": body_out,
                "project": r["project"], "tags": [x for x in (r["tags"] or "").split(",") if x],
                "score": round(s, 4), "semantic": round(sem.get(mid, 0.0), 3),
                "lexical": round(lexn.get(mid, 0.0), 3), "recency": round(rec, 3),
                "salience": float(r["salience"]), "pinned": bool(r["pinned"]),
                "use_count": int(r["use_count"]), "created_at": int(r["created_at"]),
                "updated_at": int(r["updated_at"]), "_sim": int(r["sim"] or 0),
                "state": state, "state_label": st["state_label"],
                "activation": st["activation"], "trigger": trigger, "woke": woke,
                "age_days": st["age_days"], "wake_count": st["wake_count"],
                "last_wake_at": st["last_wake_at"], "sleep_override": st["sleep_override"],
                "triggers": self.memory_triggers(r),
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
        items, used, woke = [], 0, []
        for h in hits:
            line = self._format_hit(h, compact=compact)
            cost = est_tokens(line)
            remaining = budget_tokens - used
            if cost > remaining:
                # 单条太大就截断正文塞进去；确实塞不下才跳过（不是整段 break）
                line = self._format_hit(h, compact=compact, max_chars=max(60, remaining * 3))
                cost = est_tokens(line)
                if cost > remaining:
                    if items:
                        continue
                    line = line[: max(20, remaining)]
                    cost = est_tokens(line)
            items.append({"id": h["id"], "line": line, "score": h["score"],
                          "state": h.get("state", "awake"), "woke": bool(h.get("woke"))})
            used += cost
            if h.get("woke"):
                woke.append(h["id"])
            self.reinforce(h["id"], 0.01)
        fallback_used = False
        if not items and bool((self.cfg.get("recall") or {}).get("fallback", True)) and (q := query.strip()):
            # 明确标注的回落：没命中就明说"这是最近/最重要的，不是匹配结果"，
            # 绝不伪装成命中（preflight 不这么做：没坑就是没坑）
            n_fb = int((self.cfg.get("recall") or {}).get("fallback_n", 6))
            for row in self.db.execute(
                    "SELECT * FROM memories WHERE status='active' AND valid_to=0 "
                    "ORDER BY pinned DESC, salience DESC, updated_at DESC LIMIT ?", (n_fb,)):
                h = self._row_to_hit(dict(row))
                h["fallback"] = True
                line = self._format_hit(h, compact=True)
                cost = est_tokens(line)
                if used + cost > budget_tokens and items:
                    break
                items.append({"id": h["id"], "line": line, "score": h.get("score", 0.0),
                              "state": h.get("state", "awake"), "woke": False,
                              "fallback": True})
                used += cost
            fallback_used = bool(items)

        header = "# 记忆召回 · %s · %d 条 · ~%d tok" % (query[:60], len(items), used)
        if fallback_used:
            header += " · 无匹配，已回落到最近/最重要"
        if woke:
            header += " · 线索唤醒 %d 条" % len(woke)
        text = "\n".join([header] + [i["line"] for i in items])
        if fallback_used:
            text += "\n> 这些都不是匹配结果，只是最近写入/最重要的 — 换关键词，或先 mem add 补事实"
        if not items:
            text = header + "\n(无匹配；可放宽 --budget / 去掉过滤，或先 mem add 写入事实)"
        if woke:
            text += "\n> 本次由线索唤醒（已迁回清醒）：%s；想主动叫醒：mem wake <ID>" % "、".join(woke[:8])
        text += "\n> 展开：mem get <ID>；补充：mem add -k fact -t 标题 -b 正文 --tags a,b"
        return {"text": text, "items": items, "tokens": used, "hits": len(hits), "woken": woke}

    @staticmethod
    def _format_hit(h: Dict[str, Any], compact: bool = False, max_chars: Optional[int] = None) -> str:
        day = time.strftime("%Y-%m-%d", time.localtime(h["created_at"]))
        body = re.sub(r"\s+", " ", h.get("body") or "")
        cap = int(max_chars) if max_chars else (110 if compact else 240)
        body = body[:cap] + ("…" if len(body) > cap else "")
        star = "★" if h.get("pinned") else ""
        state = h.get("state") or "awake"
        mark = ""
        if state != "awake":
            mark = "〔%s%s〕" % (STATE_LABELS.get(state, state), "·已唤醒" if h.get("woke") else "")
        return "[%s]%s %s%s %s %s (%.2f) — %s — %s" % (
            h["id"], mark, star, h["kind"], h["project"] or "-", day, h["score"],
            h.get("title") or "", body)

    def _split_pitfall_body(self, body: str) -> Tuple[str, str]:
        """把"现象：…\n修法：…"拆成两段，面板和 CLI 都要单独显示。"""
        what = fix = ""
        m = re.search(r"现象[：:]\s*(.+)", body or "")
        if m:
            what = m.group(1).strip()
        m = re.search(r"修法[：:]\s*(.+)", body or "")
        if m:
            fix = m.group(1).strip()
        return what, fix

    def pitfalls(self, file: str = "", limit: int = 200) -> Dict[str, Any]:
        """雷区总表：所有 pitfall 条目，按文件归组。面板的「雷区」页签吃这份。"""
        t = now()
        rows: List[Dict[str, Any]] = []
        for r in self.db.execute(
                "SELECT m.* FROM memories m JOIN tags t ON t.memory_id=m.id "
                "WHERE t.tag=? AND m.status='active' ORDER BY m.updated_at DESC",
                (PITFALL_TAG,)):
            row = dict(r)
            keys = [x for x in (row.get("tags") or "").split(",") if x and x != PITFALL_TAG]
            # 主归属：第一个像路径的键（带 . 或 /），没有就取第一个
            primary = next((k for k in keys if "." in k or "/" in k), keys[0] if keys else "")
            if file and file.lower() not in [k.lower() for k in keys]:
                continue
            st = self.memory_state(row, t)
            what, fix = self._split_pitfall_body(row.get("body") or "")
            rows.append({
                "id": row["id"], "title": row["title"], "kind": row["kind"],
                "file": primary, "files": keys, "what": what, "fix": fix,
                "project": row["project"], "updated_at": int(row["updated_at"]),
                "created_at": int(row["created_at"]), "salience": round(float(row["salience"]), 3),
                "state": st["state"], "state_label": st["state_label"],
                "activation": st["activation"],
            })
        groups: Dict[str, int] = {}
        for r in rows:
            if r["file"]:
                groups[r["file"]] = groups.get(r["file"], 0) + 1
        files = sorted(({"file": k, "count": n} for k, n in groups.items()),
                       key=lambda x: (-x["count"], x["file"]))
        return {"total": len(rows), "items": rows[: max(1, int(limit))], "files": files,
                "generated": t}

    def preflight_payload(self, paths: Sequence[str] = (), topic: str = "",
                          limit: int = 10) -> Dict[str, Any]:
        """给 HTTP 用的 preflight：文本 + 结构化条目。"""
        r = self.preflight(paths, topic=topic, limit=limit)
        out = []
        for i in r.get("items") or []:
            row = self.get(i["id"]) or {}
            what, fix = self._split_pitfall_body(row.get("body") or "")
            out.append(dict(i, what=what, fix=fix))
        return {"text": r.get("text", ""), "items": out, "keys": r.get("keys", [])}

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
        pit = self.db.execute(
            "SELECT count(*) c FROM tags t JOIN memories m ON m.id=t.memory_id "
            "WHERE t.tag=? AND m.status='active'", (PITFALL_TAG,)).fetchone()["c"]
        if pit:
            lines.append("> 已知雷区 %d 条：动手前先 `mem preflight <文件名>`，命中的按修法做" % pit)
        by = st.get("by_state") or {}
        if by:
            lines.append("> 状态：清醒 %d · 模糊 %d · 沉睡 %d · 深度沉睡 %d（沉睡区可用 mem states 查，"
                         "mem wake <ID> 叫醒）"
                         % (by.get("awake", 0), by.get("fuzzy", 0),
                            by.get("dormant", 0), by.get("deep", 0)))
        return "\n".join(lines)

    def _row_to_hit(self, r: Dict[str, Any]) -> Dict[str, Any]:
        """把一行记忆做成召回用的 hit；模糊/沉睡态下标题与正文已经是雾化版本。"""
        st = self.memory_state(r)
        title, body = self.mask_body(r, st["state"])
        return {"id": r["id"], "kind": r["kind"], "title": title, "body": body,
                "project": r["project"], "score": float(r.get("salience", 0.5)),
                "pinned": bool(r.get("pinned", 0)), "created_at": int(r["created_at"]),
                "state": st["state"], "state_label": st["state_label"],
                "activation": st["activation"], "woke": False}

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
    def _fuzzy_duplicate(self, kind: str, v: bytes, project: str = "") -> Optional[str]:
        thr = float(self.cfg.get("dedupe_fuzzy", 0.965))
        nv = vec_of(v, self.dim)
        # 同一句话写进不同项目是两条事实，不能合并掉归属
        for r in self.db.execute(
                "SELECT m.id, x.v FROM vectors x JOIN memories m ON m.id=x.memory_id "
                "WHERE m.kind=? AND m.status='active' AND m.valid_to=0 AND m.project=? "
                "ORDER BY m.updated_at DESC LIMIT 4000", (kind, project or "")):
            if cosine(nv, vec_of(r["v"], self.dim)) >= thr:
                return r["id"]
        return None

    def gc(self, dry_run: bool = False) -> Dict[str, int]:
        t = now()
        expired = [dict(r) for r in self.db.execute(
            "SELECT id, ttl_days, created_at FROM memories WHERE ttl_days>0 AND pinned=0")]
        dead = [r["id"] for r in expired if r["created_at"] + r["ttl_days"] * DAY < t]
        cold = [dict(r) for r in self.db.execute(
            "SELECT id FROM memories WHERE kind='episode' AND use_count=0 AND wake_count=0"
            " AND created_at<?", (t - 120 * DAY,))]
        # 有边的片段即使很旧也是网络的一部分，不能按"冷"清掉
        linked = {r["id"] for r in self.db.execute(
            "SELECT src AS id FROM links UNION SELECT dst AS id FROM links")}
        dead += [r["id"] for r in cold if r["id"] not in linked]
        dead = sorted(set(dead))
        if not dry_run:
            for mid in dead:
                self.forget(mid)
        return {"removed": len(dead), "ids": dead[:20]}

    def backfill_entities(self, limit: int = 0) -> int:
        """给存量记忆补实体索引（老库 entities 是空的，新增写入路径不会回溯）。"""
        sql = ("SELECT id, title, tags FROM memories WHERE status='active' "
               "AND id NOT IN (SELECT memory_id FROM entities)")
        params: List[Any] = []
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = [dict(r) for r in self.db.execute(sql, params)]
        n = 0
        with self.db:
            for r in rows:
                for ent in extract_entities(r["title"], (r["tags"] or "").split(",")):
                    self.db.execute(
                        "INSERT OR IGNORE INTO entities(memory_id,name,etype) VALUES(?,?,?)",
                        (r["id"], ent, "alias"))
                n += 1
        if n:
            self._journal("backfill_entities", "-", {"rows": n})
        return n

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
                self.db.execute("DELETE FROM entities WHERE memory_id=?", (r["id"],))
                for ent in extract_entities(r["title"], (r["tags"] or "").split(",")):
                    self.db.execute(
                        "INSERT OR IGNORE INTO entities(memory_id,name,etype) VALUES(?,?,?)",
                        (r["id"], ent, "alias"))
                n += 1
        return n

    def _journal(self, op: str, mid: str, extra: Dict[str, Any]) -> None:
        rec = {"ts": now(), "op": op, "id": mid}
        rec.update(extra)
        try:
            fresh = not os.path.exists(self.journal_path)
            with open(self.journal_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if fresh:
                os.chmod(self.journal_path, 0o600)
        except Exception as e:  # noqa: BLE001
            # 审计日志写不进去不能静默，但也不能让写操作失败
            sys.stderr.write("journal write failed: %s\n" % e)

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
        try:
            os.chmod(path, 0o600)      # 索引里是全部记忆正文，不接受同机其他用户可读
        except OSError:
            pass
        return path

    def export_jsonl(self, path: str) -> int:
        n = 0
        with open(path, "w", encoding="utf-8") as fh:
            for r in self.db.execute("SELECT * FROM memories ORDER BY id"):
                fh.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
                n += 1
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return n

    # -- session ingestion ------------------------------------------------
    def ingest_sessions(self, sessions_root: Optional[str] = None, limit: int = 0,
                        project: str = "", force: bool = False) -> Dict[str, int]:
        root = sessions_root or os.path.expanduser("~/.dsh/sessions")
        helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "sessions_dump.js")
        if not os.path.exists(helper) or not os.path.exists(root):
            return {"sessions": 0, "added": 0, "note": "no sessions root or helper"}
        home = os.path.expanduser("~")
        node = shutil.which("node") or next(
            (p for p in (os.path.join(home, ".local/bin/node"), "/opt/homebrew/bin/node",
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
                         tags=["session", proj] if proj else ["session"], salience=0.4,
                         touch=False)
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
                         salience=0.55, touch=False)
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
                         tags=["artifact", proj] if proj else ["artifact"], touch=False)
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
        if not safe_base_url(base):
            return None, "unsafe llm base"
        req = urllib.request.Request(
            base + "/chat/completions", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
                data = json.loads(resp.read(1 << 20).decode())
            txt = data["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", txt, re.S)
            return (json.loads(m.group(0)) if m else None), "llm"
        except Exception as e:  # noqa: BLE001
            return None, "llm error: %s" % e

    def llm_probe(self, base: str = "", model: str = "", key: str = "", timeout: int = 15) -> Dict[str, Any]:
        """Verify an OpenAI-compatible endpoint + list its model ids.

        The stored key is only attached to the configured base or a loopback
        gateway; any other address must supply its own key explicitly.
        """
        cfg = self.cfg.get("llm", {}) or {}
        configured = (cfg.get("base") or "").rstrip("/")
        base = (base or configured or "https://api.deepseek.com/v1").rstrip("/")
        if not safe_base_url(base):
            return {"ok": False, "models": [], "error": "base 只支持 https 或本机回环地址"}
        model = model or cfg.get("model") or "deepseek-flash"
        if not key:
            # 只对"已配置的 base"或本机回环网关使用本机保存的 key
            if base == configured or base.startswith("http://"):
                key = self._llm_key(base)
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
        if not safe_base_url(base):
            return {"ok": False, "error": "unsafe base", "models": [], "model": model}
        try:
            req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + key})
            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read(1 << 20).decode())
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
        # 默认只合并文本、绝不自动 retire：512 维余弦 + simhash 预筛会把
        # "模板相同、载荷不同"的真实记忆判成重复（审计实测 60 条误删 56 条）。
        auto_retire = bool(cfg.get("auto_retire", False))
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
                elif llm and not dry_run:
                    verdict, note = self._llm_judge(older, newer)
                    if verdict:
                        rel = verdict.get("relation")
                        action = "merge" if rel in ("duplicate", "refines") else (
                            "retire" if rel == "contradicts" else "keep")
                report.append({"a": older["id"], "b": newer["id"], "sim": round(sim, 3),
                               "action": action, "verdict": verdict,
                               "titles": [older["title"], newer["title"]]})
                if not dry_run and action in ("merge", "retire"):
                    if verdict and verdict.get("merged"):
                        merged = ("%s\n\n[合并] %s" % (newer["body"].rstrip(), verdict["merged"])).strip()
                        self.update(newer["id"], body=merged)
                    if action == "merge" and auto_retire:
                        self.retire(older["id"], by=newer["id"],
                                    reason="consolidate: merge (sim=%.3f)" % sim)
                        used.add(older["id"])
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
        t_now = now()
        nodes, counts = [], {k: 0 for k in STATE_ORDER}
        for r in rows:
            st = self.memory_state(r, t_now)
            counts[st["state"]] += 1
            _t, body_out = self.mask_body(r, st["state"])
            nodes.append({
                "id": r["id"], "title": r["title"], "kind": r["kind"], "project": r["project"],
                "tags": [t for t in (r["tags"] or "").split(",") if t],
                "salience": round(float(r["salience"]), 3), "pinned": bool(r["pinned"]),
                "status": r["status"], "created_at": int(r["created_at"]),
                "valid_to": int(r["valid_to"] or 0), "degree": degree[r["id"]],
                "preview": re.sub(r"\s+", " ", body_out)[:220],
                "state": st["state"], "state_label": st["state_label"],
                "activation": st["activation"], "age_days": st["age_days"],
                "since_touch_days": st["since_touch_days"],
                "halflife_days": st["halflife_days"], "wake_count": st["wake_count"],
                "last_wake_at": st["last_wake_at"], "wake_weight": st["wake_weight"],
                "sleep_override": st["sleep_override"],
                "next_state_in_days": st["next_state_in_days"],
                "triggers": self.memory_triggers(r),
            })
        dorm = self._dorm()
        return {"nodes": nodes, "edges": edges,
                "meta": {"generated": t_now, "total": len(nodes), "links": len(edges),
                         "states": counts,
                         "dormancy": {
                             "enabled": bool(dorm.get("enabled", True)),
                             "thresholds": dict(dorm.get("thresholds") or {}),
                             "wake_halflife_hours": float((dorm.get("wake") or {}).get(
                                 "halflife_hours", 72.0)),
                             "life_days": dict(dorm.get("life_days") or {}),
                         }}}

    # 绝不让记忆索引去读可能是凭据的文件
    _SECRET_NAMES = (
        "id_rsa", "id_ed25519", "id_ecdsa", "authorized_keys", ".netrc", ".pgpass",
        "secrets.json", ".env", "credentials", "cookie", "keychain", "known_hosts",
    )
    _SECRET_DIRS = ("/.ssh/", "/.aws/", "/.gnupg/", "/.config/gh/", "/.docker/config", "/.kube/")
    _DOC_EXT = (
        ".md", ".markdown", ".txt", ".rst", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx",
        ".jsx", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".html", ".css",
        ".sh", ".sql", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".rb", ".php", ".swift",
    )

    @classmethod
    def _indexable_doc(cls, path: str) -> bool:
        low = path.lower()
        if any(d in low for d in cls._SECRET_DIRS):
            return False
        base = os.path.basename(low)
        if any(base == n or base.startswith(n + ".") or base.endswith("." + n) for n in cls._SECRET_NAMES):
            return False
        if base.startswith("."):          # .env / .npmrc 之类
            return False
        return low.endswith(cls._DOC_EXT)

    @staticmethod
    def _doc_head(path: str, cwd: str = "", limit: int = 260) -> str:
        """Short content digest for artifact memories, so they are findable by meaning.

        Guarded: only document-like files are read. A path recorded in a memory
        could otherwise pull credentials into the index (and out to an LLM).
        """
        cands = [path]
        if cwd and not os.path.isabs(path):
            cands.append(os.path.join(cwd, path))
        for p in cands:
            try:
                if not Memory._indexable_doc(p):
                    continue
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
