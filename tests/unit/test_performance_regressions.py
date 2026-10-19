"""Behavioral regression tests for the performance refactor.

Each test pins the externally visible contract of an optimization: caches must
invalidate on external change, incremental index paths must match full rebuilds,
and the differential restore must still produce an exact mirror.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts.criteria import CriterionResult, TaskCriterion
from contracts.fact import Fact, FactProvenance
from contracts.resources import ResourceClaim
from contracts.run import RunManifest, RunState, SkillCandidateRef, Task
from recertia.bootstrap import build_default_orchestrator
from recertia.evals.store import EvalStore
from recertia.ledger import HashChainLedger
from recertia.memory.episodic import CaseRecord, DeadEnd, EpisodicStore
from recertia.memory.procedural.seeds import (
    add_gitignore_entry,
    add_pytest_config,
    seed_approved_for_tests,
)
from recertia.memory.procedural.store import SkillStore
from recertia.memory.semantic import FactStore
from recertia.retrieval.index import SkillIndex, embed_text
from recertia.retrieval.pipeline import Retriever
from recertia.solver.claims import ClaimScheduler
from recertia.solver.tools import ToolRuntime, default_registry
from recertia.workspace import WorkspaceManager


def _fact(fact_id: str, slug: str, assertion: str, confidence: float) -> Fact:
    return Fact(
        fact_id=fact_id,
        scope="project",
        slug=slug,
        assertion=assertion,
        confidence=confidence,
        provenance=FactProvenance(asserting_run="test"),
        authored_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Ledger: cached tip with file-stat invalidation
# ---------------------------------------------------------------------------


def test_ledger_append_notices_external_appends(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    first = HashChainLedger(path)
    second = HashChainLedger(path)

    e0 = first.append(actor="a", action="write", target="t0")
    assert e0.seq == 0
    e1 = second.append(actor="b", action="write", target="t1")
    assert e1.seq == 1
    # The first instance's cached tip is stale here; the stat check must force a rescan.
    e2 = first.append(actor="a", action="write", target="t2")
    assert e2.seq == 2
    assert e2.prev_hash == e1.entry_hash
    first.verify()
    second.verify()


def test_ledger_tip_hash_tracks_appends(tmp_path: Path) -> None:
    ledger = HashChainLedger(tmp_path / "ledger.jsonl")
    entry = ledger.append(actor="a", action="write", target="t")
    assert ledger.tip_hash() == entry.entry_hash


# ---------------------------------------------------------------------------
# SkillIndex: freshness, upsert, batch fetch, precomputed query embeddings
# ---------------------------------------------------------------------------


def _seeded_store(root: Path) -> tuple[SkillStore, list]:
    store = SkillStore(root)
    version = add_pytest_config()
    seed_approved_for_tests(store, version, active=True)
    return store, store.iter_loaded()


def test_index_freshness_round_trip(tmp_path: Path) -> None:
    store, entries = _seeded_store(tmp_path / "skills")
    index = SkillIndex(tmp_path / "idx.db")
    fingerprint = store.library_fingerprint()
    assert not index.is_fresh(fingerprint)
    index.rebuild(entries, library_fingerprint=fingerprint)
    assert index.is_fresh(fingerprint)

    seed_approved_for_tests(store, add_gitignore_entry(), active=True)
    assert not index.is_fresh(store.library_fingerprint())
    index.close()


def test_index_upsert_matches_full_rebuild_for_search(tmp_path: Path) -> None:
    store, entries = _seeded_store(tmp_path / "skills")
    index = SkillIndex(tmp_path / "idx.db")
    index.rebuild(entries, library_fingerprint=store.library_fingerprint())

    new_version = add_gitignore_entry()
    seed_approved_for_tests(store, new_version, active=True)
    status = store.get_status(new_version.skill_id, new_version.version)
    stats = store.get_stats(new_version.skill_id, new_version.version)
    snap_after_upsert = index.upsert(
        new_version, status, stats, library_fingerprint=store.library_fingerprint()
    )

    fresh = SkillIndex(tmp_path / "fresh.db")
    snap_after_rebuild = fresh.rebuild(store.iter_loaded())
    assert snap_after_upsert == snap_after_rebuild

    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / ".gitignore").write_text("")
    bundle, _ = Retriever(index).search("add a gitignore entry", workdir=workdir)
    assert any(c.skill_id == "add-gitignore-entry" for c in bundle.skills)
    index.close()
    fresh.close()


def test_index_get_rows_matches_get_row(tmp_path: Path) -> None:
    _store, entries = _seeded_store(tmp_path / "skills")
    index = SkillIndex(tmp_path / "idx.db")
    index.rebuild(entries)
    keys = [(v.skill_id, v.version) for v, _s, _st in entries]
    rows = index.get_rows(keys)
    assert set(rows) == set(keys)
    for key in keys:
        assert rows[key] == index.get_row(*key)
    index.close()


def test_vector_top_k_accepts_precomputed_query_embedding(tmp_path: Path) -> None:
    _store, entries = _seeded_store(tmp_path / "skills")
    index = SkillIndex(tmp_path / "idx.db")
    index.rebuild(entries)
    query = "pytest configuration"
    assert index.vector_top_k(query, 3) == index.vector_top_k(query, 3, q_emb=embed_text(query))
    index.close()


def test_embed_text_is_deterministic_and_normalised() -> None:
    a = embed_text("add pytest config to the repo")
    b = embed_text("add pytest config to the repo")
    assert a == b
    assert abs(sum(v * v for v in a) ** 0.5 - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Bootstrap: fresh libraries skip the rebuild
# ---------------------------------------------------------------------------


def test_second_bootstrap_skips_index_rebuild(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    facts_root = tmp_path / "facts"
    seed_approved_for_tests(SkillStore(skills_root), add_pytest_config(), active=True)

    first = build_default_orchestrator(
        tmp_path / "runs", skills_root=skills_root, facts_root=facts_root
    )
    first.close()

    calls = 0
    original = SkillIndex.rebuild

    def counting_rebuild(self, entries, *, library_fingerprint=None):
        nonlocal calls
        calls += 1
        return original(self, entries, library_fingerprint=library_fingerprint)

    try:
        SkillIndex.rebuild = counting_rebuild  # type: ignore[method-assign]
        second = build_default_orchestrator(
            tmp_path / "runs", skills_root=skills_root, facts_root=facts_root
        )
        second.close()
    finally:
        SkillIndex.rebuild = original  # type: ignore[method-assign]
    assert calls == 0


# ---------------------------------------------------------------------------
# Workspace: differential restore still produces an exact mirror
# ---------------------------------------------------------------------------


def test_restore_rewrites_only_what_changed(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    untouched = workdir / "untouched.txt"
    untouched.write_text("keep me")
    changed = workdir / "changed.txt"
    changed.write_text("original")
    deleted = workdir / "deleted.txt"
    deleted.write_text("will be removed then restored")

    mgr = WorkspaceManager(tmp_path / "snapshots")
    ref = mgr.snapshot(workdir, run_id="r1", attempt_no=0)
    untouched_ino = untouched.stat().st_ino

    changed.write_text("mutated")
    stale = workdir / "stale.txt"
    stale.write_text("not in snapshot")
    deleted.unlink()

    mgr.restore(workdir, ref)

    assert changed.read_text() == "original"
    assert not stale.exists()
    assert deleted.read_text() == "will be removed then restored"
    # The differential restore must not rewrite files the attempt left alone.
    assert untouched.stat().st_ino == untouched_ino


def test_restore_into_missing_workdir_full_copies(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "a.txt").write_text("a")
    mgr = WorkspaceManager(tmp_path / "snapshots")
    ref = mgr.snapshot(workdir, run_id="r1", attempt_no=0)

    import shutil

    shutil.rmtree(workdir)
    mgr.restore(workdir, ref)
    assert (workdir / "a.txt").read_text() == "a"


# ---------------------------------------------------------------------------
# Episodic / semantic caches: invalidate on internal and external writes
# ---------------------------------------------------------------------------


def _case(case_id: str) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        run_id="r",
        attempt_no=1,
        task_class="repo-chore",
        outcome="failed",
    )


def test_episodic_index_cache_tracks_writes_and_external_appends(tmp_path: Path) -> None:
    store = EpisodicStore(tmp_path / "episodic")
    store.write(_case("c1"))
    assert [r["case_id"] for r in store.list_index()] == ["c1"]
    # Warm the cache, then append behind the store's back.
    store.list_index()
    with store.index_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"case_id": "external", "hash": "x", "run_id": "r"}) + "\n")
    assert [r["case_id"] for r in store.list_index()] == ["c1", "external"]


def test_fact_retrieve_reflects_writes_and_external_files(tmp_path: Path) -> None:
    store = FactStore(tmp_path / "facts")
    store.write(_fact("f1", "uses-pytest", "This repository uses pytest for testing", 0.9))
    assert [f.fact_id for f in store.retrieve("pytest", scope="project")] == ["f1"]

    external = _fact("f2", "uses-ruff", "This repository uses ruff for linting", 0.8)
    (tmp_path / "facts" / "project" / "uses-ruff.json").write_text(
        external.model_dump_json(), encoding="utf-8"
    )
    ids = {f.fact_id for f in store.retrieve("ruff", scope="project")}
    assert "f2" in ids


# ---------------------------------------------------------------------------
# Eval store: bulk contribution samples match the per-skill queries
# ---------------------------------------------------------------------------


def _observed_state(run_id: str, *, arm: str, chosen=False, suppressed=False) -> RunState:
    criterion = TaskCriterion(id="req", kind="command", run="true", source="caller")
    return RunState(
        run_id=run_id,
        task=Task(
            task_id=run_id,
            request="do the chore",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
        manifest=RunManifest(index_snapshot_id="snap", criteria_hash="locked"),
        arm=arm,  # type: ignore[arg-type]
        criteria=[criterion],
        criteria_locked_at=datetime.now(timezone.utc),
        chosen=SkillCandidateRef(skill_id="sk", version=1, score=1.0) if chosen else None,
        suppressed_skill=(
            SkillCandidateRef(skill_id="sk", version=1, score=1.0) if suppressed else None
        ),
        attempt_no=1,
        results=[CriterionResult(criterion_id="req", kind="command", passed=True)],
        terminal="solved",
    )


def test_contribution_samples_bulk_matches_per_skill(tmp_path: Path) -> None:
    store = EvalStore(tmp_path / "evals.sqlite")
    store.append_run(_observed_state("shadow-1", arm="shadow", chosen=True))
    store.append_run(_observed_state("supp-1", arm="control", suppressed=True))

    per_skill = store.contribution_samples(skill_id="sk", version=1, task_class="repo-chore")
    bulk = store.contribution_samples_bulk(task_class="repo-chore")
    assert bulk[("sk", 1)] == per_skill
    assert ("absent", 9) not in bulk
    store.close()


# ---------------------------------------------------------------------------
# Claim scheduler: condition-based wait wakes on release
# ---------------------------------------------------------------------------


def test_claim_waiter_wakes_promptly_on_release() -> None:
    scheduler = ClaimScheduler(claim_timeout_s=10.0)
    claim = ResourceClaim(kind="file", id="shared", mode="write")
    scheduler.acquire("holder", [claim])

    acquired: list[str] = []
    started = time.monotonic()

    def waiter() -> None:
        scheduler.acquire("waiter", [claim])
        acquired.append("waiter")

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.15)
    scheduler.release("holder", [claim])
    thread.join(timeout=5)

    assert acquired == ["waiter"]
    # Well under the 10s timeout: the condition wakes the waiter on release.
    assert time.monotonic() - started < 5
    conflicts = [c for c in scheduler.conflicts if c.waiting == "waiter"]
    assert conflicts and conflicts[-1].resolution == "acquired"
    scheduler.release("waiter", [claim])


# ---------------------------------------------------------------------------
# Tool handlers: bounded reads
# ---------------------------------------------------------------------------


def test_grep_skips_oversized_and_binary_files(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "small.txt").write_text("needle in a small file\n")
    big = work / "big.txt"
    big.write_text("needle at start\n" + ("padding line\n" * 200_000))
    (work / "blob.bin").write_bytes(b"\x00\x01needle\x00")

    tools = ToolRuntime(default_registry(), require_approval_for_non_read=True)
    result = tools.invoke("grep", {"pattern": "needle", "path": "."}, workdir=work, step_id="s1")
    assert result.ok and result.exit_code == 0
    assert "small.txt" in result.stdout
    assert "big.txt" not in result.stdout
    assert "blob.bin" not in result.stdout


def test_read_file_large_file_tail(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    marker = "TAIL-MARKER-12345"
    content = "filler line\n" * 20_000 + marker + "\n"
    (work / "large.log").write_text(content)

    tools = ToolRuntime(default_registry(), require_approval_for_non_read=True)
    result = tools.invoke("read_file", {"path": "large.log"}, workdir=work, step_id="s1")
    assert result.ok
    assert marker in result.stdout
    assert len(result.stdout) <= 8000


# ---------------------------------------------------------------------------
# Critical-path scaling contracts: consulting memory must not cost what it holds
# ---------------------------------------------------------------------------


def _reference_fact_retrieve(facts: list[Fact], query: str, *, limit: int) -> list[Fact]:
    """The scan-and-sort scoring `FactStore.retrieve` had before it was made incremental.

    Kept verbatim so the optimised path can be held to producing the same ranking, ties
    included, rather than merely a plausible one.
    """

    q = query.lower()
    scored: list[tuple[float, Fact]] = []
    for fact in facts:
        if fact.status in ("contradicted", "demoted"):
            trust = 0.1
        elif fact.status == "verified":
            trust = 0.9
        else:
            trust = fact.confidence
        hay = f"{fact.assertion} {fact.slug}".lower()
        score = trust * (1.0 if q in hay else 0.2)
        if any(tok in hay for tok in q.split() if len(tok) > 2):
            score += 0.3
        scored.append((score, fact))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [fact for _, fact in scored[:limit]]


def test_fact_retrieve_ranking_matches_the_scan_it_replaced(tmp_path: Path) -> None:
    """Precomputed scoring rows and the top-k heap must not perturb the ranking.

    Facts feed the memory bundle that solving and lift measurement read, so a reordering
    here would quietly change behaviour rather than just timings.
    """

    rng = random.Random(20260810)
    words = ["pytest", "ruff", "editorconfig", "coverage", "mypy", "docker", "sqlite", "lint"]
    store = FactStore(tmp_path / "facts")
    for i in range(120):
        fact = Fact(
            fact_id=f"f-{i}",
            scope="project",
            slug=f"{rng.choice(words)}-{i}",
            assertion=" ".join(rng.choices(words, k=6)) + f" number {i}",
            status=rng.choice(["asserted", "verified", "contradicted", "demoted"]),
            confidence=round(rng.random(), 3),
            provenance=FactProvenance(asserting_run="test"),
            authored_at=datetime.now(timezone.utc),
        )
        store.write(fact)

    ordered = store.list_facts(scope="project")
    for _ in range(25):
        query = " ".join(rng.choices(words, k=rng.randint(1, 3)))
        for limit in (1, 5, 10):
            expected = _reference_fact_retrieve(ordered, query, limit=limit)
            actual = store.retrieve(query, scope="project", limit=limit)
            assert [f.fact_id for f in actual] == [f.fact_id for f in expected], (query, limit)


class _CountingDirEntry:
    """Wraps a ``DirEntry`` so per-file stats are counted like any other syscall."""

    __slots__ = ("_entry", "_counts")

    def __init__(self, entry, counts: dict[str, int]) -> None:
        self._entry = entry
        self._counts = counts

    def __getattr__(self, name: str):
        return getattr(self._entry, name)

    def stat(self, *args, **kwargs):
        self._counts["entry_stat"] = self._counts.get("entry_stat", 0) + 1
        return self._entry.stat(*args, **kwargs)


class _CountingScandir:
    """``os.scandir`` result that counts the entries it hands out, and still works in ``with``."""

    def __init__(self, inner, counts: dict[str, int]) -> None:
        self._inner = inner
        self._counts = counts

    def __iter__(self):
        for entry in self._inner:
            self._counts["entries"] = self._counts.get("entries", 0) + 1
            yield _CountingDirEntry(entry, self._counts)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._inner.__exit__(*exc_info)

    def close(self) -> None:
        self._inner.close()


def test_fact_retrieve_filesystem_work_does_not_grow_with_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache validation must examine the same number of files at 40 facts as at 400.

    Counting filesystem work rather than measuring time: the defect this pins -- validating
    the cache by stat'ing every fact on every call -- is a change in how much work happens,
    and that is what a test can assert without depending on how fast the machine is.
    """

    counts: dict[str, int] = {}
    real_stat, real_scandir = os.stat, os.scandir

    def counting_stat(*args, **kwargs):
        counts["stat"] = counts.get("stat", 0) + 1
        return real_stat(*args, **kwargs)

    def counting_scandir(*args, **kwargs):
        return _CountingScandir(real_scandir(*args, **kwargs), counts)

    monkeypatch.setattr(os, "stat", counting_stat)
    monkeypatch.setattr(os, "scandir", counting_scandir)

    def filesystem_work_for(count: int) -> int:
        store = FactStore(tmp_path / f"facts-{count}", revalidate_interval_s=3600.0)
        for i in range(count):
            store.write(
                _fact(f"f{i}", f"slug-{i}", f"Assertion {i} about tooling in this repo", 0.5)
            )
        # Seeding just touched the fact directories, and a sweep taken in the same coarse
        # mtime tick as a write cannot trust the gate afterwards. Wait out that window, then
        # warm up, so what gets counted is steady state rather than the settling sweep.
        time.sleep(0.05)
        store.retrieve("tooling", scope="project")
        store.retrieve("tooling", scope="project")
        counts.clear()
        for _ in range(5):
            store.retrieve("tooling", scope="project")
        return sum(counts.values())

    small = filesystem_work_for(40)
    large = filesystem_work_for(400)
    assert small > 0, "expected the cheap gate to still validate something"
    assert large == small, (
        f"cache validation scaled with library size: {small} filesystem operations at "
        f"40 facts, {large} at 400"
    )


def test_fact_retrieve_notices_external_in_place_edit_after_the_interval(
    tmp_path: Path,
) -> None:
    """The one freshness case the tiered validation defers, pinned explicitly.

    An external process rewriting an existing fact changes neither the store's own state nor
    any directory mtime, so it becomes visible on the next full sweep. Zero interval here
    means the next call; in production the window is `DEFAULT_REVALIDATE_INTERVAL_S`.
    """

    store = FactStore(tmp_path / "facts", revalidate_interval_s=0.0)
    store.write(_fact("f1", "uses-ruff", "This repository uses ruff for linting", 0.9))
    assert [f.fact_id for f in store.retrieve("ruff", scope="project")] == ["f1"]

    edited = _fact("f1", "uses-ruff", "This repository uses ruff and enforces import order", 0.9)
    (tmp_path / "facts" / "project" / "uses-ruff.json").write_text(
        edited.model_dump_json(), encoding="utf-8"
    )
    [found] = store.retrieve("import order", scope="project")
    assert "import order" in found.assertion


def _episodic_case(index: int, *, task_class: str, solved: bool) -> CaseRecord:
    return CaseRecord(
        case_id=f"case-{index}",
        run_id=f"run-{index}",
        attempt_no=1,
        task_class=task_class,
        outcome="solved" if solved else "failed",
        failure_class=None if solved else "tool",
        dead_end=None if solved else DeadEnd(approach=f"approach-{index}", why_failed="no"),
    )


def test_episodic_bucketed_lookups_match_a_reverse_scan(tmp_path: Path) -> None:
    """Bucketing must return exactly what scanning the index backwards returned."""

    store = EpisodicStore(tmp_path / "episodic")
    rng = random.Random(7)
    classes = ["repo-chore", "research-synthesis", None]
    rows: list[CaseRecord] = []
    for i in range(60):
        case = _episodic_case(
            i, task_class=rng.choice(classes) or "", solved=rng.random() < 0.4
        )
        rows.append(case)
        store.write(case)

    index = store.list_index()
    for task_class in ("repo-chore", "research-synthesis", "never-seen", None):
        for limit in (1, 3, 10):
            expected_dead_ends = []
            for row in reversed(index):
                if not row.get("has_dead_end"):
                    continue
                if task_class and row.get("task_class") != task_class:
                    continue
                expected_dead_ends.append(row["case_id"])
                if len(expected_dead_ends) >= limit:
                    break
            actual = store.dead_ends_for(task_class=task_class, limit=limit)
            assert [c.case_id for c in actual] == expected_dead_ends, (task_class, limit)

            expected_solved = []
            for row in reversed(index):
                if row.get("outcome") != "solved":
                    continue
                if task_class and row.get("task_class") != task_class:
                    continue
                expected_solved.append(row["case_id"])
                if len(expected_solved) >= limit:
                    break
            assert (
                store.solved_case_ids_for(task_class=task_class, limit=limit)
                == expected_solved
            ), (task_class, limit)


def test_episodic_task_class_counts_match_a_scan_and_track_writes(tmp_path: Path) -> None:
    """Distill's "how often does this task class recur" must not cost the whole history."""

    store = EpisodicStore(tmp_path / "episodic")
    for i in range(30):
        store.write(
            _episodic_case(
                i,
                task_class="repo-chore" if i % 3 else "research-synthesis",
                solved=i % 2 == 0,
            )
        )

    index = store.list_index()
    for task_class in ("repo-chore", "research-synthesis", "never-seen"):
        expected = sum(1 for row in index if row.get("task_class") == task_class)
        assert store.count_for_task_class(task_class) == expected, task_class

    # Counting warms the cache; a later write must still be reflected.
    store.write(_episodic_case(99, task_class="never-seen", solved=True))
    assert store.count_for_task_class("never-seen") == 1


def test_episodic_buckets_stay_current_across_writes_and_external_appends(
    tmp_path: Path,
) -> None:
    """Buckets are a cache: a write must be visible, and so must an external append."""

    store = EpisodicStore(tmp_path / "episodic")
    store.write(_episodic_case(1, task_class="repo-chore", solved=False))
    assert [c.case_id for c in store.dead_ends_for(task_class="repo-chore")] == ["case-1"]

    # Write after the buckets are warm: the incremental update must include it.
    store.write(_episodic_case(2, task_class="repo-chore", solved=False))
    assert [c.case_id for c in store.dead_ends_for(task_class="repo-chore")] == [
        "case-2",
        "case-1",
    ]

    store.write(_episodic_case(3, task_class="repo-chore", solved=True))
    assert store.solved_case_ids_for(task_class="repo-chore") == ["case-3"]

    # Append behind the store's back: the whole cache, buckets included, must rebuild.
    with store.index_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "case_id": "external",
                    "hash": "x",
                    "run_id": "r",
                    "outcome": "solved",
                    "task_class": "repo-chore",
                    "has_dead_end": False,
                }
            )
            + "\n"
        )
    assert store.solved_case_ids_for(task_class="repo-chore") == ["external", "case-3"]


def test_retrieve_does_not_read_the_whole_episodic_index(tmp_path: Path) -> None:
    """`retrieve` runs on every task, so it must use the bounded lookups, not `list_index`.

    Pinned structurally: `list_index` copies every row ever recorded, so reaching for it on
    the critical path reintroduces growth with total history no matter how fast it is today.
    """

    from recertia.graph.ops import OperationLedger
    from recertia.nodes.context import NodeContext
    from recertia.nodes.retrieve import retrieve

    store = EpisodicStore(tmp_path / "episodic")
    for i in range(20):
        store.write(_episodic_case(i, task_class="research-synthesis", solved=i % 2 == 0))

    def forbidden() -> list[dict]:
        raise AssertionError("retrieve must not scan the full episodic index")

    store.list_index = forbidden  # type: ignore[method-assign]

    workdir = tmp_path / "work"
    workdir.mkdir()
    ctx = NodeContext(
        run_id="r1",
        attempt_no=0,
        node="retrieve",
        workdir=workdir,
        workspaces=WorkspaceManager(tmp_path / "snapshots"),
        ledger=HashChainLedger(tmp_path / "ledger.jsonl"),
        ops=OperationLedger(tmp_path / "ops.sqlite"),
        episodic=store,
    )
    state = RunState(
        run_id="r1",
        task=Task(
            task_id="t1",
            request="add an editorconfig file",
            task_class="repo-chore",
            submitted_at=datetime.now(timezone.utc),
        ),
    )
    outcome = retrieve(state, ctx)
    assert outcome.state.bundle is not None


def test_list_runs_parses_at_most_limit_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F21: list reads summary_json and does not parse checkpoint state."""

    from recertia.app.runs import RunService
    from recertia.runtime.registry import RunRegistry

    class _Quota:
        def admit(self, tenant_id: str, *, quota: object = None) -> None:
            del tenant_id, quota

        def release_inflight(self, tenant_id: str) -> None:
            del tenant_id

        def complete(self, tenant_id: str, *, cost_usd: float = 0.0) -> None:
            del tenant_id, cost_usd

    parses = 0
    original = RunState.model_validate_json

    def _count(cls: object, raw: object) -> object:
        nonlocal parses
        parses += 1
        return original(raw)  # type: ignore[misc]

    monkeypatch.setattr(RunState, "model_validate_json", classmethod(_count))
    root = tmp_path / "runs"
    registry = RunRegistry(root / "runtime.db")
    try:
        for i in range(2000):
            run_id = f"r{i:04d}"
            created = f"2026-01-01T00:00:{i % 60:02d}.{i:06d}+00:00"
            registry.reserve(
                "t1",
                run_id,
                request_hash=run_id,
                summary_json={
                    "run_id": run_id,
                    "task_class": "repo-chore",
                    "terminal": "solved",
                    "status": "solved",
                    "tenant_id": "t1",
                    "created_at": created,
                },
            )
    finally:
        registry.close()

    service = RunService(_Quota(), {}, lambda _tenant, _run: None)
    started = time.perf_counter()
    page = service.list("t1", runs_root=root, limit=50)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert parses == 0
    assert len(page["items"]) == 50
    assert page["next_cursor"]
    assert elapsed_ms < 100

