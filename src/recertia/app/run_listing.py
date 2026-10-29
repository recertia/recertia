"""The run list behind ``GET /v1/runs`` (issue #136).

Order is ``created_at DESC, run_id DESC``. A page is read by keyset from each durable
source and merged. The checkpoint and in-memory sources are read for ``limit + 1`` rows.
The registry is read only down to the page's last row (see ``_registry_stream``), so a
page reads about ``limit`` rows from each table however many runs exist, plus the runs
whose registry date falls between the page's top and bottom but whose checkpoint date does
not (accepted shortly before their first checkpoint).

Sources, for one tenant:

* checkpoint summaries (``run_summaries``): the durable state of every run that has
  started, kept current by each hop commit;
* the run registry (``runs``): a row is reserved when a run is accepted. It is never
  updated afterwards, so it is listed only for a run that has no checkpoint yet
  (status ``accepted``). A registry row never replaces a checkpoint row;
* in-memory runs of this process, which override the stored fields of the same run.

A run has one position in the order, whatever sources hold it: its checkpoint
``created_at`` if it has a checkpoint, else its registry ``created_at``, else the
in-memory record's. The position is stable between pages for runs whose sort key does
not change. A run that gets its first checkpoint during a walk can be listed twice or
skipped: its position moves from its registry date to its checkpoint date, and
checkpoint dates are cut to whole seconds.

A run whose date is missing (an empty registry ``created_at``) comes after every dated run.
The ``task_class`` and ``terminal`` filters apply to every source. A registry-only run
carries neither value, so it matches a ``terminal`` filter only when that filter is
empty and never matches a ``task_class`` filter.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any

from recertia.graph.store import CheckpointStore
from recertia.persistence.import_legacy import live_checkpoint_db
from recertia.persistence.layout import RuntimeLayout
from recertia.runtime.cursor import RunPosition, decode_run_cursor, encode_run_cursor
from recertia.runtime.registry import RunRegistry

Row = dict[str, Any]
RunKey = tuple[bool, datetime, str]
Keyed = tuple[RunKey, Row]

_NO_DATE = datetime.min.replace(tzinfo=UTC)


def parse_created_at(value: object) -> datetime | None:
    """``value`` as an aware UTC datetime. Naive values are UTC; missing or unparseable is ``None``."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    try:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def run_key(created_at: datetime | None, run_id: str) -> RunKey:
    """Total order key. Sort descending; a run without a date sorts after every dated run."""

    return (created_at is not None, created_at or _NO_DATE, run_id)


def list_run_page(
    tenant_id: str,
    *,
    runs_root: Path,
    runs: dict[tuple[str, str], Any],
    summarize: Callable[[Any], Row],
    limit: int,
    cursor: str | None,
    task_class: str | None,
    terminal: str | None,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    boundary = decode_run_cursor(cursor) if cursor else None
    records = _tenant_records(runs, tenant_id)
    with ExitStack() as stack:
        store = _open_checkpoints(stack, runs_root)
        registry = _open_registry(stack, runs_root)
        known = [
            _memory_stream(
                tenant_id,
                records,
                summarize,
                store,
                registry,
                boundary,
                task_class,
                terminal,
            ),
            _checkpoint_stream(
                tenant_id,
                store,
                set(records),
                boundary,
                task_class,
                terminal,
                limit + 1,
            ),
        ]
        head = list(islice(heapq.merge(*known, key=_by_key, reverse=True), limit + 1))
        # A registry-only run below the (limit + 1)th row of ``head`` cannot be on this page.
        floor = head[limit][0] if len(head) > limit else None
        accepted = _registry_stream(
            tenant_id,
            registry,
            store,
            set(records),
            boundary,
            floor,
            task_class,
            terminal,
            limit + 1,
        )
        taken = list(islice(heapq.merge(iter(head), accepted, key=_by_key, reverse=True), limit + 1))
    items = [row for _, row in taken[:limit]]
    next_cursor = None
    if len(taken) > limit:
        has_date, created, run_id = taken[limit - 1][0]
        next_cursor = encode_run_cursor(RunPosition(created if has_date else None, run_id))
    return {"items": items, "next_cursor": next_cursor}


def _by_key(item: Keyed) -> RunKey:
    return item[0]


def _tenant_records(runs: dict[tuple[str, str], Any], tenant_id: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for (tid, run_id), rec in list(runs.items()):
        if tid != tenant_id or rec is None:
            continue
        if getattr(rec, "tenant_id", tenant_id) != tenant_id:
            continue
        found[run_id] = rec
    return found


def _open_checkpoints(stack: ExitStack, runs_root: Path) -> CheckpointStore | None:
    db = live_checkpoint_db(runs_root)
    if db is None:
        return None
    store = CheckpointStore(db)
    stack.callback(store.close)
    return store


def _open_registry(stack: ExitStack, runs_root: Path) -> RunRegistry | None:
    # The API reserves a run in RuntimeLayout(root).tenant(t).runtime_db, which is
    # ``<tenant runs root>/runtime.db``: the file read here for that tenant's runs root.
    path = RuntimeLayout(runs_root).local_runs().runtime_db
    if not path.is_file():
        return None
    registry = RunRegistry(path)
    stack.callback(registry.close)
    return registry


def _memory_stream(
    tenant_id: str,
    records: dict[str, Any],
    summarize: Callable[[Any], Row],
    store: CheckpointStore | None,
    registry: RunRegistry | None,
    boundary: RunPosition | None,
    task_class: str | None,
    terminal: str | None,
) -> Iterator[Keyed]:
    matching = {
        run_id: rec
        for run_id, rec in records.items()
        if (not task_class or getattr(rec, "task_class", None) == task_class)
        and (not terminal or (getattr(rec, "terminal", None) or "") == terminal)
    }
    ids = list(matching)
    durable = store.get_summaries(ids) if store is not None and ids else {}
    reserved = registry.get_many(tenant_id, ids) if registry is not None and ids else {}
    cut = run_key(boundary.created_at, boundary.run_id) if boundary is not None else None
    keyed: list[Keyed] = []
    for run_id, rec in matching.items():
        row: Row = {**summarize(rec), "tenant_id": tenant_id}
        stored = durable.get(run_id) or reserved.get(run_id)
        if stored is not None:
            row["created_at"] = stored["created_at"]
        key = run_key(parse_created_at(row.get("created_at")), run_id)
        if cut is None or key < cut:
            keyed.append((key, row))
    keyed.sort(key=_by_key, reverse=True)
    return iter(keyed)


def _checkpoint_stream(
    tenant_id: str,
    store: CheckpointStore | None,
    skip: set[str],
    boundary: RunPosition | None,
    task_class: str | None,
    terminal: str | None,
    chunk: int,
) -> Iterator[Keyed]:
    if store is None:
        return
    before_at: str | None = None
    before_id: str | None = None
    if boundary is not None and boundary.created_at is None:
        # An undated row has an empty ``created_at``, the lowest text: the rest of the walk
        # is the undated rows with a smaller run id.
        before_at, before_id = "", boundary.run_id
    elif boundary is not None and boundary.created_at is not None:
        before_at = boundary.created_at.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
        # A position with a sub-second part is after every row of its own second.
        before_id = None if boundary.created_at.microsecond else boundary.run_id
    first = True
    while True:
        rows = store.list_summaries_page(
            limit=chunk,
            created_at_before=before_at,
            run_id_before=before_id,
            task_class=task_class or None,
            terminal=terminal or None,
            backfill=first and boundary is None,
        )
        first = False
        for row in rows:
            run_id = str(row["run_id"])
            if run_id not in skip:
                key = run_key(parse_created_at(row["created_at"]), run_id)
                yield key, {**row, "tenant_id": tenant_id}
        if len(rows) < chunk:
            return
        before_at, before_id = str(rows[-1]["created_at"]), str(rows[-1]["run_id"])


def _registry_stream(
    tenant_id: str,
    registry: RunRegistry | None,
    store: CheckpointStore | None,
    skip: set[str],
    boundary: RunPosition | None,
    floor: RunKey | None,
    task_class: str | None,
    terminal: str | None,
    chunk: int,
) -> Iterator[Keyed]:
    """Registry rows of runs that have no checkpoint yet, below ``boundary`` and above ``floor``.

    ``floor`` is the key of the page's last candidate from the other sources, so the rows
    scanned here are the registry rows between the page's top and bottom. Without it, every
    run that has a checkpoint would be read and skipped on every page. A registry-only run
    has no ``task_class`` or ``terminal``, so no registry row matches either filter.
    """

    if registry is None or task_class or terminal:
        return
    before_at: str | None = None
    before_id: str | None = None
    if boundary is not None:
        # An undated registry row has an empty ``created_at``, the lowest text.
        stamp = boundary.created_at.isoformat() if boundary.created_at is not None else ""
        before_at, before_id = stamp, boundary.run_id
    after_at: str | None = None
    after_id: str | None = None
    if floor is not None:
        has_date, created, after_id = floor
        after_at = created.isoformat() if has_date else ""
    while True:
        rows = registry.list_page(
            tenant_id,
            limit=chunk,
            created_at_before=before_at,
            run_id_before=before_id,
            created_at_after=after_at,
            run_id_after=after_id,
        )
        checkpointed = store.get_summaries([str(row["run_id"]) for row in rows]) if store and rows else {}
        for row in rows:
            run_id = str(row["run_id"])
            if run_id in skip or run_id in checkpointed:
                continue
            key = run_key(parse_created_at(row["created_at"]), run_id)
            yield key, {**row, "tenant_id": tenant_id}
        if len(rows) < chunk:
            return
        before_at, before_id = str(rows[-1]["created_at"]), str(rows[-1]["run_id"])
