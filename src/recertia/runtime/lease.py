"""Lease token for REL-3 fencing.

File-lock ``RunLease`` keeps ``incarnation=1`` because flock cannot tell a
clean release from a crash. Steal bumps land with ``run_leases`` rows.
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
