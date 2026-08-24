"""Derived prefix-tree view over a run's trajectory JSONL (ClawGym II / ADR-0011).

Does not write a second event stream. Retries of the same node at a later attempt
become siblings. Dead-leaf pruning is a view: the JSONL is left intact.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from contracts.trajectory import TrajectoryEvent

_CALL_KINDS = frozenset({"step_started", "plan_choice", "distill_candidate", "evolve_decision"})


@dataclass
class PrefixNode:
    key: str
    event_kind: str
    node: str
    attempt_no: int
    seq: int
    children: list[PrefixNode] = field(default_factory=list)
    dead: bool = False

    def leaf_seqs(self) -> list[int]:
        if not self.children:
            return [] if self.dead else [self.seq]
        seqs: list[int] = []
        for child in self.children:
            seqs.extend(child.leaf_seqs())
        return seqs


@dataclass
class PrefixTree:
    roots: list[PrefixNode]
    event_count: int
    kept_leaf_seqs: list[int]
    pruned_leaf_seqs: list[int]

    @property
    def reconstructability(self) -> float:
        if self.event_count == 0:
            return 1.0
        kept = set(self.kept_leaf_seqs)
        return 1.0 if kept or not self.pruned_leaf_seqs else len(kept) / self.event_count

    def reconstruct_hash(self) -> str:
        payload = ",".join(str(s) for s in self.kept_leaf_seqs)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def build_prefix_tree(events: Sequence[TrajectoryEvent], *, prune_dead: bool = False) -> PrefixTree:
    """One node per call-like event; later attempts of the same node become siblings."""

    calls = [e for e in events if e.event_kind in _CALL_KINDS]
    roots: list[PrefixNode] = []
    by_node: dict[str, list[PrefixNode]] = {}
    parent_stack: list[PrefixNode] = []

    for event in calls:
        pnode = PrefixNode(
            key=f"{event.node}:{event.attempt_no}:{event.event_kind}:{event.seq}",
            event_kind=event.event_kind,
            node=event.node,
            attempt_no=event.attempt_no,
            seq=event.seq,
        )
        siblings = by_node.setdefault(event.node, [])
        if siblings and event.attempt_no > siblings[-1].attempt_no:
            parent = _parent_of(siblings[-1], roots)
            if parent is None:
                roots.append(pnode)
            else:
                parent.children.append(pnode)
            siblings.append(pnode)
            parent_stack = [pnode]
            continue
        if parent_stack:
            parent_stack[-1].children.append(pnode)
        else:
            roots.append(pnode)
        siblings.append(pnode)
        parent_stack.append(pnode)

    pruned: list[int] = []
    if prune_dead:
        pruned = _mark_dead_leaves(roots, events)
    kept: list[int] = []
    for root in roots:
        kept.extend(root.leaf_seqs())
    return PrefixTree(
        roots=roots,
        event_count=len(calls),
        kept_leaf_seqs=kept,
        pruned_leaf_seqs=pruned,
    )


def reconstructability_rate(original: Sequence[TrajectoryEvent], tree: PrefixTree) -> float:
    """Hash-stable check: kept leaf seqs match those events' seqs in the original stream."""

    by_seq = {e.seq: e for e in original}
    for seq in tree.kept_leaf_seqs:
        if seq not in by_seq:
            return 0.0
    original_hash = hashlib.sha256(
        ",".join(str(s) for s in tree.kept_leaf_seqs).encode()
    ).hexdigest()[:16]
    return 1.0 if original_hash == tree.reconstruct_hash() else 0.0


def _parent_of(node: PrefixNode, roots: list[PrefixNode]) -> PrefixNode | None:
    def walk(cur: PrefixNode, parent: PrefixNode | None) -> PrefixNode | None:
        if cur is node:
            return parent
        for child in cur.children:
            found = walk(child, cur)
            if found is not None or child is node:
                return found if found is not None else cur
        return None

    for root in roots:
        found = walk(root, None)
        if found is not None or root is node:
            return found
    return None


def _mark_dead_leaves(roots: list[PrefixNode], events: Sequence[TrajectoryEvent]) -> list[int]:
    """A leaf is dead when its attempt failed and a later attempt exists."""

    failed_attempts = {e.attempt_no for e in events if e.event_kind == "failure_classified"}
    pruned: list[int] = []

    def mark(node: PrefixNode) -> None:
        if node.children:
            for child in node.children:
                mark(child)
            return
        if node.attempt_no in failed_attempts and any(
            sib.attempt_no > node.attempt_no for sib in _siblings(node, roots)
        ):
            node.dead = True
            pruned.append(node.seq)

    for root in roots:
        mark(root)
    return pruned


def _siblings(node: PrefixNode, roots: list[PrefixNode]) -> Iterable[PrefixNode]:
    parent = _parent_of(node, roots)
    if parent is None:
        return roots
    return parent.children
