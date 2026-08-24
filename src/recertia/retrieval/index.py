"""Procedural-plane index: FTS5 lexical + hashed bag-of-words embeddings (M1).

Embeddings are deterministic and dependency-free: a fixed-dim hashed unigram/bigram vector
over ``title + intent + tags + tool names``. Good enough for ranking and for a reproducible
``index_snapshot_id``; a real embedding model can replace ``embed_text`` later without
changing the index schema.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import sqlite3
import threading
import zlib
from pathlib import Path
from typing import Iterable, Sequence

from contracts.skill import SkillVersion
from contracts.stats import SkillStats
from contracts.status import SkillStatus

EMBED_DIM = 64
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def embed_text(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Hashed bag-of-words embedding; L2-normalised. Deterministic across processes.

    Feature hashing uses CRC32: it is orders of magnitude cheaper than a cryptographic
    hash per n-gram, and ranking only needs a stable bucket/sign, not collision resistance.
    """

    vec = [0.0] * dim
    tokens = tokenize(text)
    grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
    for gram in grams:
        h = zlib.crc32(gram.encode())
        idx = h % dim
        sign = 1.0 if (h >> 8) & 1 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def skill_document(version: SkillVersion) -> str:
    tools = " ".join(step.tool or "" for step in version.steps)
    tags = " ".join(version.tags)
    return f"{version.title}\n{version.intent}\n{tags}\n{tools}\n{version.task_class}"


class SkillIndex:
    """SQLite FTS5 + embedding store for one library snapshot.

    Document embeddings are mirrored into an in-memory matrix on first vector search, so
    ``vector_top_k`` does no SQL or JSON work per row after warm-up. The mirror is rebuilt
    lazily after any mutation (``rebuild`` / ``upsert``).
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._emb_cache: dict[tuple[str, int], tuple[float, ...]] | None = None
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS skills (
                    skill_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    task_class TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'project',
                    lifecycle TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    curation TEXT NOT NULL,
                    applications INTEGER NOT NULL DEFAULT 0,
                    last_used_at TEXT,
                    tool_fingerprint_json TEXT NOT NULL DEFAULT '{}',
                    preconditions_json TEXT NOT NULL DEFAULT '[]',
                    document TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    PRIMARY KEY (skill_id, version)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
                    skill_id, version UNINDEXED, document, tokenize='porter'
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    @staticmethod
    def _row_params(
        version: SkillVersion, status: SkillStatus, stats: SkillStats
    ) -> tuple:
        doc = skill_document(version)
        emb = embed_text(doc)
        fp = json.dumps(status.certification.tool_fingerprint)
        preconditions = json.dumps([p.model_dump() for p in version.preconditions])
        return (
            version.skill_id,
            version.version,
            version.task_class,
            version.scope,
            status.lifecycle,
            1 if status.active else 0,
            version.provenance.curation,
            stats.predictive_trust.applications,
            (
                stats.predictive_trust.last_used_at.isoformat()
                if stats.predictive_trust.last_used_at
                else None
            ),
            fp,
            preconditions,
            doc,
            json.dumps(emb),
        )

    _INSERT_SQL = """
        INSERT INTO skills (
            skill_id, version, task_class, scope, lifecycle, active, curation,
            applications, last_used_at, tool_fingerprint_json, preconditions_json,
            document, embedding_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def rebuild(
        self,
        entries: list[tuple[SkillVersion, SkillStatus, SkillStats]],
        *,
        library_fingerprint: str | None = None,
    ) -> str:
        """Replace the index contents; return the new ``index_snapshot_id``.

        ``library_fingerprint`` records what the indexed library looked like, so later
        processes can skip the rebuild entirely when nothing changed (``is_fresh``).
        """

        with self._lock:
            self._conn.execute("DELETE FROM skills")
            self._conn.execute("DELETE FROM skills_fts")
            rows = [self._row_params(v, s, st) for v, s, st in entries]
            self._conn.executemany(self._INSERT_SQL, rows)
            self._conn.executemany(
                "INSERT INTO skills_fts (skill_id, version, document) VALUES (?, ?, ?)",
                [(r[0], r[1], r[11]) for r in rows],
            )
            snapshot_id = self._compute_snapshot_id_unlocked()
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('snapshot_id', ?)",
                (snapshot_id,),
            )
            if library_fingerprint is not None:
                self._set_meta_unlocked("library_fingerprint", library_fingerprint)
            self._conn.commit()
            self._emb_cache = None
        return snapshot_id

    def upsert(
        self,
        version: SkillVersion,
        status: SkillStatus,
        stats: SkillStats,
        *,
        library_fingerprint: str | None = None,
    ) -> str:
        """Index (or re-index) a single skill version without a full rebuild.

        Used by the store node: one new candidate should not cost a full library rescan.
        """

        with self._lock:
            params = self._row_params(version, status, stats)
            self._conn.execute(
                "DELETE FROM skills WHERE skill_id=? AND version=?",
                (params[0], params[1]),
            )
            self._conn.execute(
                "DELETE FROM skills_fts WHERE skill_id=? AND version=?",
                (params[0], params[1]),
            )
            self._conn.execute(self._INSERT_SQL, params)
            self._conn.execute(
                "INSERT INTO skills_fts (skill_id, version, document) VALUES (?, ?, ?)",
                (params[0], params[1], params[11]),
            )
            snapshot_id = self._compute_snapshot_id_unlocked()
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('snapshot_id', ?)",
                (snapshot_id,),
            )
            if library_fingerprint is not None:
                self._set_meta_unlocked("library_fingerprint", library_fingerprint)
            self._conn.commit()
            if self._emb_cache is not None:
                self._emb_cache[(params[0], int(params[1]))] = tuple(
                    json.loads(params[12])
                )
        return snapshot_id

    def _set_meta_unlocked(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )

    def is_fresh(self, library_fingerprint: str) -> bool:
        """Whether the index was built from a library with exactly this fingerprint."""

        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='library_fingerprint'"
            ).fetchone()
        return row is not None and row[0] == library_fingerprint

    def snapshot_id(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='snapshot_id'"
            ).fetchone()
        return row[0] if row else self._compute_snapshot_id_unlocked()

    def _compute_snapshot_id_unlocked(self) -> str:
        rows = self._conn.execute(
            "SELECT skill_id, version, document FROM skills ORDER BY skill_id, version"
        ).fetchall()
        blob = json.dumps(rows, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def lexical_top_k(self, query: str, k: int) -> list[tuple[str, int, float]]:
        """Return ``(skill_id, version, rank_score)`` by FTS5 BM25 (lower BM25 is better)."""

        tokens = tokenize(query)
        if not tokens:
            return []
        # Quote each token for FTS5; OR-combine so partial matches still retrieve.
        match = " OR ".join(f'"{t}"' for t in tokens[:32])
        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT skills_fts.skill_id, skills_fts.version,
                           bm25(skills_fts) AS score
                    FROM skills_fts
                    WHERE skills_fts MATCH ?
                    ORDER BY score
                    LIMIT ?
                    """,
                    (match, k),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        # Convert BM25 (lower=better) to a descending rank score in (0, 1].
        out: list[tuple[str, int, float]] = []
        for i, (sid, ver, _bm25) in enumerate(rows):
            out.append((sid, int(ver), 1.0 / (1.0 + i)))
        return out

    def _embeddings_unlocked(self) -> dict[tuple[str, int], tuple[float, ...]]:
        """In-memory embedding matrix, decoded once and reused across searches."""

        if self._emb_cache is None:
            rows = self._conn.execute(
                "SELECT skill_id, version, embedding_json FROM skills"
            ).fetchall()
            self._emb_cache = {
                (sid, int(ver)): tuple(json.loads(emb)) for sid, ver, emb in rows
            }
        return self._emb_cache

    def unload_embeddings(self) -> int:
        """Drop the in-memory embedding matrix (ADR-0018 cold retrieval pages)."""

        with self._lock:
            n = 0 if self._emb_cache is None else len(self._emb_cache)
            self._emb_cache = None
        return n

    def vector_top_k(
        self, query: str, k: int, *, q_emb: Sequence[float] | None = None
    ) -> list[tuple[str, int, float]]:
        q = q_emb if q_emb is not None else embed_text(query)
        with self._lock:
            embeddings = self._embeddings_unlocked()
            # -i tiebreak keeps the old stable-sort tie order (scan order) exactly.
            scored = (
                (sid, ver, cosine(q, emb), -i)
                for i, ((sid, ver), emb) in enumerate(embeddings.items())
            )
            top = heapq.nlargest(k, scored, key=lambda t: (t[2], t[3]))
        top.sort(key=lambda t: (t[2], t[3]), reverse=True)
        return [(sid, ver, score) for sid, ver, score, _ in top]

    def embedding_for(self, skill_id: str, version: int) -> tuple[float, ...] | None:
        """The stored document embedding for one version, if indexed."""

        with self._lock:
            return self._embeddings_unlocked().get((skill_id, version))

    def get_rows(self, keys: Iterable[tuple[str, int]]) -> dict[tuple[str, int], dict]:
        """Batch ``get_row``: one query for many ``(skill_id, version)`` keys."""

        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        where = " OR ".join("(skill_id=? AND version=?)" for _ in keys)
        params: list[object] = []
        for sid, ver in keys:
            params.extend((sid, ver))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT skill_id, version, task_class, scope, lifecycle, active, curation,
                       applications, last_used_at, tool_fingerprint_json, preconditions_json,
                       document
                FROM skills WHERE {where}
                """,
                params,
            ).fetchall()
        out: dict[tuple[str, int], dict] = {}
        for row in rows:
            parsed = self._parse_row(row)
            out[(parsed["skill_id"], int(parsed["version"]))] = parsed
        return out

    @staticmethod
    def _parse_row(row: tuple) -> dict:
        keys = (
            "skill_id", "version", "task_class", "scope", "lifecycle", "active", "curation",
            "applications", "last_used_at", "tool_fingerprint_json", "preconditions_json",
            "document",
        )
        return dict(zip(keys, row))

    def get_row(self, skill_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT skill_id, version, task_class, scope, lifecycle, active, curation,
                       applications, last_used_at, tool_fingerprint_json, preconditions_json,
                       document
                FROM skills WHERE skill_id=? AND version=?
                """,
                (skill_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._parse_row(row)

    def all_rows(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT skill_id, version, task_class, scope, lifecycle, active, curation,
                       applications, last_used_at, tool_fingerprint_json, preconditions_json,
                       document
                FROM skills
                """
            ).fetchall()
        return [self._parse_row(r) for r in rows]
