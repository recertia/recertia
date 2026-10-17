"""Lease token stub for REL-3 fencing. Full ``RunLease`` lives in a later slice."""

from __future__ import annotations

from dataclasses import dataclass

from recertia.ports.identity import TenantId


@dataclass(frozen=True)
class LeaseToken:
    tenant_id: TenantId | str
    run_id: str
    owner_id: str
    fencing_token: int
    incarnation: int
