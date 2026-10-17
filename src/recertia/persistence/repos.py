"""Repository protocols. No SQL, paths, or ``sqlite3.Row`` on the interface."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from recertia.ports.identity import TenantId


class UnitOfWork(Protocol):
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SessionRepo(Protocol):
    def put(
        self,
        session_id: str,
        payload: Mapping[str, object],
        *,
        tenant: TenantId,
        expires_at: datetime,
    ) -> None: ...

    def get(self, session_id: str) -> Mapping[str, object] | None: ...

    def delete(self, session_id: str) -> None: ...

    def put_oidc_pending(
        self, state: str, payload: Mapping[str, object], *, expires_at: datetime
    ) -> None: ...

    def pop_oidc_pending(self, state: str) -> Mapping[str, object] | None: ...


class TenantRepo(Protocol):
    def upsert(self, tenant: TenantId, *, display_name: str, status: str) -> None: ...

    def get(self, tenant: TenantId) -> Mapping[str, object] | None: ...

    def list_ids(self) -> Sequence[TenantId]: ...


class ApiKeyRepo(Protocol):
    def insert(self, uow: UnitOfWork, row: Mapping[str, object]) -> None: ...

    def get(self, key_id: str) -> Mapping[str, object] | None: ...

    def revoke(self, uow: UnitOfWork, key_id: str, *, actor: str) -> None: ...

    def list_for_tenant(self, tenant: TenantId) -> Sequence[Mapping[str, object]]: ...


class QuotaRepo(Protocol):
    def admit(self, uow: UnitOfWork, tenant: TenantId, *, cost_usd: float) -> None: ...

    def complete(self, uow: UnitOfWork, tenant: TenantId, *, cost_usd: float) -> None: ...

    def release(self, uow: UnitOfWork, tenant: TenantId) -> None: ...


class JobRunRepo(Protocol):
    def create(self, uow: UnitOfWork, row: Mapping[str, object]) -> str: ...

    def get(self, tenant: TenantId, job_run_id: str) -> Mapping[str, object] | None: ...

    def save(self, uow: UnitOfWork, row: Mapping[str, object]) -> None: ...

    def list_for_tenant(self, tenant: TenantId, *, limit: int) -> Sequence[Mapping[str, object]]: ...


class ProgramRepo(Protocol):
    def put(
        self,
        uow: UnitOfWork,
        tenant: TenantId,
        program_id: str,
        payload: Mapping[str, object],
        *,
        expected_version: int | None,
    ) -> None: ...

    def get(self, tenant: TenantId, program_id: str) -> Mapping[str, object] | None: ...


class ProposalRepo(Protocol):
    def put(
        self, uow: UnitOfWork, tenant: TenantId, proposal_id: str, payload: Mapping[str, object]
    ) -> None: ...

    def get(self, tenant: TenantId, proposal_id: str) -> Mapping[str, object] | None: ...

    def list_for_tenant(self, tenant: TenantId) -> Sequence[Mapping[str, object]]: ...


class WorkspaceRegistryRepo(Protocol):
    def upsert(
        self, uow: UnitOfWork, tenant: TenantId, workspace_id: str, payload: Mapping[str, object]
    ) -> None: ...

    def get(self, tenant: TenantId, workspace_id: str) -> Mapping[str, object] | None: ...

    def list_for_tenant(self, tenant: TenantId) -> Sequence[Mapping[str, object]]: ...

    def delete(self, uow: UnitOfWork, tenant: TenantId, workspace_id: str) -> None: ...
