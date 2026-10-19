"""Lease token for REL-3 fencing.

``run_leases`` persists incarnation and the fencing-token high-water mark.
Crash-resume on the same lock inode keeps incarnation; a steal after TTL
plus a new inode bumps it.
"""

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
