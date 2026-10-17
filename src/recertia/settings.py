"""Process settings. The only ``src/recertia`` module that reads ``os.environ``."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from recertia.config import ModelConfig
from recertia.errors import ConfigError, ExecutionUnavailable
from recertia.ports.execution import ExecutionBackend, ExecutionConfig

_log = logging.getLogger(__name__)

DurabilityName = Literal["strict", "normal", "fast"]
DeploymentMode = Literal["single_node", "multi_node"]
DbBackend = Literal["sqlite", "postgres"]
BlobStoreKind = Literal["filesystem", "s3"]
GoldenBackend = Literal["inherit", "container", "local"]
ConsoleAuth = Literal["off", "dev", "oidc"]
MigrateMode = Literal["auto", "manual", "off"]
ProcessRole = Literal["api", "worker", "cli", "executor"]

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})

_DEPLOYMENT_CHECKS: list[Callable[["Settings"], None]] = []

# Names read by load_settings. The inventory test compares this set to src/.
KNOWN_ENV_KEYS = frozenset(
    {
        "RECERTIA_ENV",
        "RECERTIA_DEPLOYMENT_MODE",
        "RECERTIA_DURABILITY",
        "RECERTIA_DB_BACKEND",
        "RECERTIA_DATABASE_URL",
        "DATABASE_URL",
        "RECERTIA_BLOB_STORE",
        "RECERTIA_RUNTIME_DB_MIGRATE",
        "RECERTIA_ALLOW_FAULTS",
        "RECERTIA_FAULTS",
        "RECERTIA_TENANT_ENCRYPTION",
        "RECERTIA_API_DEFAULT_ASYNC",
        "RECERTIA_EXECUTION_BACKEND",
        "RECERTIA_CONTAINER_RUNTIME",
        "RECERTIA_CONTAINER_IMAGE",
        "RECERTIA_ALLOW_CUSTOM_IMAGE",
        "RECERTIA_API_ALLOW_LOCAL_EXEC",
        "RECERTIA_WORKDIR_WORLD_WRITE",
        "RECERTIA_LOCAL_HOME_PASSTHROUGH",
        "RECERTIA_COMMAND_POLICY",
        "RECERTIA_COMMAND_ALLOWLIST",
        "RECERTIA_GOLDEN_BACKEND",
        "RECERTIA_MODEL_PROVIDER",
        "RECERTIA_MODEL_ID",
        "RECERTIA_VERIFIER_MODEL_ID",
        "RECERTIA_API_KEY_ENV",
        "RECERTIA_MODEL_TIMEOUT_S",
        "RECERTIA_MODEL_MAX_RETRIES",
        "RECERTIA_MODEL_BACKOFF_BASE_S",
        "RECERTIA_MODEL_BACKOFF_CAP_S",
        "RECERTIA_CIRCUIT_FAILURES",
        "RECERTIA_CIRCUIT_WINDOW_S",
        "RECERTIA_CIRCUIT_OPEN_S",
        "RECERTIA_ALLOW_STUB_MODEL",
        "RECERTIA_ANTHROPIC_BASE_URL",
        "RECERTIA_OPENAI_BASE_URL",
        "RECERTIA_MODEL_ALLOWLIST",
        "RECERTIA_MODEL_ALLOWLIST_PATH",
        "RECERTIA_OPENAI_HTTP_REFERER",
        "RECERTIA_OPENAI_TITLE",
        "RECERTIA_OPENROUTER_TITLE",
        "RECERTIA_OPENAI_EXTRA_HEADERS",
        "RECERTIA_OPENAI_EXTRA_BODY",
        "RECERTIA_OPENAI_MAX_TOKENS",
        "RECERTIA_DEFAULT_INPUT_USD_PER_MTOK",
        "RECERTIA_DEFAULT_OUTPUT_USD_PER_MTOK",
        "RECERTIA_MAX_BLOB_BYTES",
        "RECERTIA_API_MAX_CRITERION_TIMEOUT_S",
        "RECERTIA_API_MAX_IN_FLIGHT_RUNS",
        "RECERTIA_CONSOLE_AUTH",
        "RECERTIA_CONSOLE_DEV_LOGIN",
        "RECERTIA_CONSOLE_DEV_ADMIN",
        "RECERTIA_CONSOLE_COOKIE_SECURE",
        "RECERTIA_CONSOLE_SESSION_SECRET",
        "RECERTIA_OIDC_ISSUER",
        "RECERTIA_OIDC_CLIENT_ID",
        "RECERTIA_OIDC_CLIENT_SECRET",
        "RECERTIA_OIDC_SCOPE",
        "RECERTIA_OIDC_ROLE_ALLOWLIST",
        "RECERTIA_OIDC_DEFAULT_ROLES",
        "RECERTIA_OIDC_DEFAULT_TENANTS",
        "RECERTIA_OIDC_TOKEN_URL",
        "RECERTIA_OIDC_USERINFO_URL",
        "RECERTIA_TENANT_MAX_RUNS_PER_DAY",
        "RECERTIA_TENANT_MAX_COST_USD_PER_DAY",
        "RECERTIA_TENANT_MAX_IN_FLIGHT",
        "RECERTIA_TENANT_SKILLS",
        "RECERTIA_POLICY_PATH",
        "RECERTIA_POLICY_VERSION",
        "RECERTIA_LIBRARY_COMMIT",
        "RECERTIA_SCRATCH_MAX_STEPS",
        "RECERTIA_FETCH_ALLOWLIST",
        "RECERTIA_FETCH_MAX_BYTES",
        "RECERTIA_FETCH_TIMEOUT_S",
        "RECERTIA_PDF_EXTRACT",
        "RECERTIA_ALLOW_POSIX_WORKSPACE_ROOTS",
        "RECERTIA_WORKSPACE_ROOTS",
        "RECERTIA_ALLOW_FULL_REPLAY",
        "RECERTIA_LOG_FORMAT",
        "RECERTIA_LOG_LEVEL",
        "RECERTIA_METRICS_ENABLED",
        "RECERTIA_API_KEY_PEPPER",
        "RECERTIA_REQUIRE_IMAGE_DIGEST",
        "RECERTIA_EXEC_MAX_OUTPUT_BYTES",
        "RECERTIA_JSONL_FSYNC_INTERVAL_S",
        "RECERTIA_LEDGER_LOCK_TIMEOUT_S",
        "RECERTIA_LEDGER_VERIFY_FULL",
        "RECERTIA_LEDGER_HMAC_KEY",
        "RECERTIA_EXECUTOR_MODE",
        "RECERTIA_QUOTA_RESERVE_TTL_S",
        "RECERTIA_TENANT_MODEL_KEYS",
        "RECERTIA_TENANT_MODEL_KEYS_FILE",
    }
)


def register_deployment_check(fn: Callable[["Settings"], None]) -> None:
    """Later phases (REL-10, REL-11) append startup rules here."""

    _DEPLOYMENT_CHECKS.append(fn)


def _raw(env: Mapping[str, str], key: str, default: str = "") -> str:
    return env.get(key, default)


def _bool(env: Mapping[str, str], key: str, *, default: bool = False) -> bool:
    if key not in env or env[key].strip() == "":
        return default
    value = env[key].strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{key} must be 1/true/yes/on or 0/false/no/off, got {env[key]!r}")


def _choice(env: Mapping[str, str], key: str, allowed: set[str], default: str) -> str:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    if raw not in allowed:
        raise ConfigError(f"{key} must be one of {sorted(allowed)}, got {raw!r}")
    return raw


def _path_list(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    """Colon-separated (``os.pathsep``) absolute directory list."""

    raw = env.get(key, "").strip()
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(os.pathsep) if part.strip())


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number") from exc


def _optional_int(env: Mapping[str, str], key: str) -> int | None:
    raw = env.get(key, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc


def _scan_model_prices(
    env: Mapping[str, str],
) -> dict[tuple[str, str, Literal["IN", "OUT"]], float]:
    prefix = "RECERTIA_MODEL_PRICE_"
    prices: dict[tuple[str, str, Literal["IN", "OUT"]], float] = {}
    for key, raw in env.items():
        if not key.startswith(prefix):
            continue
        body = key[len(prefix) :]
        if body.endswith("_IN"):
            kind: Literal["IN", "OUT"] = "IN"
            body = body[: -len("_IN")]
        elif body.endswith("_OUT"):
            kind = "OUT"
            body = body[: -len("_OUT")]
        else:
            continue
        provider, sep, model = body.partition("_")
        if not sep or not provider or not model:
            continue
        try:
            prices[(provider.lower(), model.lower(), kind)] = float(raw)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a number") from exc
    return prices


@dataclass(frozen=True)
class Settings:
    env: Literal["dev", "prod"] = "dev"
    deployment_mode: DeploymentMode = "single_node"
    durability: DurabilityName = "normal"
    db_backend: DbBackend = "sqlite"
    database_url: str | None = None
    blob_store: BlobStoreKind = "filesystem"
    runtime_db_migrate: MigrateMode = "auto"
    allow_faults: bool = False
    faults: str = ""
    tenant_encryption: Literal["off", "required"] = "off"
    # Stays false until one deprecation release. ``POST /v1/runs`` still runs
    # on the request thread unless the body sets ``mode=async``. Enqueue-and-wait
    # is not wired, so this flag must not become the production default.
    api_default_async: bool = False

    execution_backend: ExecutionBackend = "container"
    execution_backend_was_set: bool = False
    container_runtime: Literal["docker", "podman"] | None = None
    container_image: str = "python:3.12-slim"
    allow_custom_image: bool = False
    api_allow_local_exec: bool = False
    workdir_world_write: bool = False
    local_home_passthrough: bool = False
    command_policy: Literal["on", "off"] = "on"
    command_allowlist: str = ""
    golden_backend: GoldenBackend = "inherit"

    model_provider: Literal["stub", "anthropic", "openai"] = "stub"
    model_id: str = "stub"
    verifier_model_id: str | None = None
    api_key_env: str = ""
    model_timeout_s: float = 60.0
    model_max_retries: int = 2
    model_backoff_base_s: float = 0.25
    model_backoff_cap_s: float = 8.0
    circuit_failures: int = 5
    circuit_window_s: float = 30.0
    circuit_open_s: float = 30.0
    allow_stub_model: bool = False
    anthropic_base_url: str | None = None
    openai_base_url: str | None = None
    model_allowlist: str = ""
    model_allowlist_path: str = ""
    openai_http_referer: str = ""
    openai_title: str = ""
    openrouter_title: str = ""
    openai_extra_headers: str = ""
    openai_extra_body: str = ""
    openai_max_tokens: int | None = None
    default_input_usd_per_mtok: float = 1.0
    default_output_usd_per_mtok: float = 3.0
    model_prices: Mapping[tuple[str, str, Literal["IN", "OUT"]], float] = field(default_factory=dict)

    max_blob_bytes: int = 16 * 1024 * 1024
    api_max_criterion_timeout_s: int = 300
    api_max_in_flight_runs: int = 4

    console_auth: ConsoleAuth = "off"
    console_dev_login: bool = False
    console_dev_admin: bool = False
    console_cookie_secure: bool | None = None
    console_session_secret: str = ""
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scope: str = "openid profile email"
    oidc_role_allowlist: str = "operator,reviewer"
    oidc_default_roles: str = "operator"
    oidc_default_tenants: str = "default"
    oidc_token_url: str = ""
    oidc_userinfo_url: str = ""

    tenant_max_runs_per_day: int = 100
    tenant_max_cost_usd_per_day: float = 50.0
    tenant_max_in_flight: int = 4
    tenant_skills: bool = False

    policy_path: str = ""
    policy_version: str = ""
    library_commit: str = ""
    scratch_max_steps: int = 5
    fetch_allowlist: str = "pypi.org,files.pythonhosted.org,raw.githubusercontent.com,api.github.com"
    fetch_max_bytes: int = 512 * 1024
    fetch_timeout_s: float = 20.0
    pdf_extract: bool = True
    allow_posix_workspace_roots: bool = False
    workspace_roots: tuple[str, ...] = ()
    allow_full_replay: bool = False

    log_format: Literal["json", "text"] = "json"
    log_level: str = "INFO"
    metrics_enabled: bool = True
    api_key_pepper: str = ""
    require_image_digest: bool = False
    exec_max_output_bytes: int = 262144
    jsonl_fsync_interval_s: float = 1.0
    ledger_lock_timeout_s: float = 5.0
    ledger_verify_full: bool = False
    ledger_hmac_key: str = ""
    executor_mode: str = ""
    quota_reserve_ttl_s: int = 86400
    tenant_model_keys: str = ""
    tenant_model_keys_file: str = ""

    def validate_deployment(self, *, role: ProcessRole) -> None:
        """Refuse an unsafe ``multi_node`` process.

        ``multi_node`` refuses sqlite and the local execution backend.
        ``single_node`` has no extra rules.
        """

        del role  # reserved for REL-10 role-specific checks
        if self.deployment_mode != "multi_node":
            return
        if self.api_allow_local_exec:
            _log.warning("RECERTIA_API_ALLOW_LOCAL_EXEC is ignored in multi_node")
        if self.execution_backend == "local" or self.resolved_golden_backend() == "local":
            raise ExecutionUnavailable("multi_node refuses the local execution backend")
        if self.db_backend != "postgres" or not (self.database_url or "").strip():
            raise ConfigError(
                "multi_node refuses sqlite: set RECERTIA_DB_BACKEND=postgres and RECERTIA_DATABASE_URL"
            )
        if self.blob_store != "s3":
            raise ConfigError("multi_node requires RECERTIA_BLOB_STORE=s3")
        if len(self.console_session_secret) < 32:
            raise ConfigError("multi_node requires RECERTIA_CONSOLE_SESSION_SECRET of at least 32 characters")
        if self.durability == "fast":
            raise ConfigError("multi_node refuses RECERTIA_DURABILITY=fast")
        if not self.tenant_skills:
            raise ConfigError(
                "multi_node requires RECERTIA_TENANT_SKILLS=1 (shared skills/ is refused)"
            )
        if not self.workspace_roots:
            raise ConfigError("multi_node requires RECERTIA_WORKSPACE_ROOTS")
        if not self.api_key_pepper:
            raise ConfigError("multi_node requires RECERTIA_API_KEY_PEPPER")
        for check in _DEPLOYMENT_CHECKS:
            check(self)

    def resolved_golden_backend(self) -> ExecutionBackend:
        if self.golden_backend == "inherit":
            return self.execution_backend
        return self.golden_backend

    def execution_config(self) -> ExecutionConfig:
        runtime = self.container_runtime
        return ExecutionConfig(
            backend=self.execution_backend,
            runtime=runtime,
            image=self.container_image,
            allow_custom_image=self.allow_custom_image,
            workdir_world_write=self.workdir_world_write,
            api_allow_local_exec=self.api_allow_local_exec,
            local_home_passthrough=self.local_home_passthrough,
            deployment_mode=self.deployment_mode,
            output_cap_bytes=self.exec_max_output_bytes,
            require_image_digest=self.require_image_digest or self.deployment_mode == "multi_node",
        )

    def model_config(self) -> ModelConfig:
        provider = self.model_provider
        return ModelConfig(
            provider=provider,
            model_id=self.model_id or ("stub" if provider == "stub" else ""),
            api_key_env=self.api_key_env,
            verifier_model_id=self.verifier_model_id,
            timeout_s=self.model_timeout_s,
            max_retries=self.model_max_retries,
        )


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read ``environ`` or ``os.environ`` exactly once per call."""

    env = os.environ if environ is None else environ
    database_url = _raw(env, "RECERTIA_DATABASE_URL").strip() or None
    alias = _raw(env, "DATABASE_URL").strip() or None
    if database_url and alias and database_url != alias:
        raise ConfigError("RECERTIA_DATABASE_URL and DATABASE_URL are both set and differ")
    if database_url is None:
        database_url = alias

    runtime_raw = _raw(env, "RECERTIA_CONTAINER_RUNTIME").strip().lower()
    runtime: Literal["docker", "podman"] | None
    if not runtime_raw:
        runtime = None
    elif runtime_raw in {"docker", "podman"}:
        runtime = runtime_raw  # type: ignore[assignment]
    else:
        raise ConfigError("RECERTIA_CONTAINER_RUNTIME must be docker or podman")

    auth = _choice(env, "RECERTIA_CONSOLE_AUTH", {"off", "dev", "oidc", "development"}, "off")
    if auth == "development":
        auth = "dev"

    cookie_raw = _raw(env, "RECERTIA_CONSOLE_COOKIE_SECURE").strip().lower()
    cookie: bool | None
    if not cookie_raw:
        cookie = None
    elif cookie_raw in _TRUE:
        cookie = True
    elif cookie_raw in _FALSE:
        cookie = False
    else:
        raise ConfigError("RECERTIA_CONSOLE_COOKIE_SECURE must be a boolean flag")

    provider = _choice(env, "RECERTIA_MODEL_PROVIDER", {"stub", "anthropic", "openai", ""}, "stub")
    if provider == "":
        provider = "stub"
    model_id = _raw(env, "RECERTIA_MODEL_ID").strip() or ("stub" if provider == "stub" else "")
    verifier = _raw(env, "RECERTIA_VERIFIER_MODEL_ID").strip() or None
    api_key_env = _raw(env, "RECERTIA_API_KEY_ENV").strip()
    if not api_key_env:
        api_key_env = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider, "")

    anthropic_base = _raw(env, "RECERTIA_ANTHROPIC_BASE_URL").strip() or None
    openai_base = _raw(env, "RECERTIA_OPENAI_BASE_URL").strip() or None
    backend = _choice(env, "RECERTIA_EXECUTION_BACKEND", {"container", "local"}, "container")

    process_env = _raw(env, "RECERTIA_ENV", "dev").strip().lower() or "dev"
    if process_env not in {"dev", "prod"}:
        raise ConfigError("RECERTIA_ENV must be dev or prod")

    return Settings(
        env=process_env,  # type: ignore[arg-type]
        deployment_mode=_choice(  # type: ignore[arg-type]
            env, "RECERTIA_DEPLOYMENT_MODE", {"single_node", "multi_node"}, "single_node"
        ),
        durability=_choice(env, "RECERTIA_DURABILITY", {"strict", "normal", "fast"}, "normal"),  # type: ignore[arg-type]
        db_backend=_choice(env, "RECERTIA_DB_BACKEND", {"sqlite", "postgres"}, "sqlite"),  # type: ignore[arg-type]
        database_url=database_url,
        blob_store=_choice(env, "RECERTIA_BLOB_STORE", {"filesystem", "s3"}, "filesystem"),  # type: ignore[arg-type]
        runtime_db_migrate=_choice(env, "RECERTIA_RUNTIME_DB_MIGRATE", {"auto", "manual", "off"}, "auto"),  # type: ignore[arg-type]
        allow_faults=_bool(env, "RECERTIA_ALLOW_FAULTS"),
        faults=_raw(env, "RECERTIA_FAULTS").strip(),
        tenant_encryption=_choice(env, "RECERTIA_TENANT_ENCRYPTION", {"off", "required"}, "off"),  # type: ignore[arg-type]
        api_default_async=_bool(env, "RECERTIA_API_DEFAULT_ASYNC"),
        execution_backend=backend,  # type: ignore[arg-type]
        execution_backend_was_set=bool(env.get("RECERTIA_EXECUTION_BACKEND", "").strip()),
        container_runtime=runtime,
        container_image=_raw(env, "RECERTIA_CONTAINER_IMAGE", "python:3.12-slim").strip()
        or "python:3.12-slim",
        allow_custom_image=_raw(env, "RECERTIA_ALLOW_CUSTOM_IMAGE").strip().lower() in _TRUE,
        api_allow_local_exec=_bool(env, "RECERTIA_API_ALLOW_LOCAL_EXEC"),
        workdir_world_write=_bool(env, "RECERTIA_WORKDIR_WORLD_WRITE"),
        local_home_passthrough=_bool(env, "RECERTIA_LOCAL_HOME_PASSTHROUGH"),
        command_policy=_choice(env, "RECERTIA_COMMAND_POLICY", {"on", "off"}, "on"),  # type: ignore[arg-type]
        command_allowlist=_raw(env, "RECERTIA_COMMAND_ALLOWLIST").strip(),
        golden_backend=_choice(env, "RECERTIA_GOLDEN_BACKEND", {"inherit", "container", "local"}, "inherit"),  # type: ignore[arg-type]
        model_provider=provider,  # type: ignore[arg-type]
        model_id=model_id,
        verifier_model_id=verifier,
        api_key_env=api_key_env,
        model_timeout_s=_float(env, "RECERTIA_MODEL_TIMEOUT_S", 60.0),
        model_max_retries=_int(env, "RECERTIA_MODEL_MAX_RETRIES", 2),
        model_backoff_base_s=_float(env, "RECERTIA_MODEL_BACKOFF_BASE_S", 0.25),
        model_backoff_cap_s=_float(env, "RECERTIA_MODEL_BACKOFF_CAP_S", 8.0),
        circuit_failures=_int(env, "RECERTIA_CIRCUIT_FAILURES", 5),
        circuit_window_s=_float(env, "RECERTIA_CIRCUIT_WINDOW_S", 30.0),
        circuit_open_s=_float(env, "RECERTIA_CIRCUIT_OPEN_S", 30.0),
        allow_stub_model=_bool(env, "RECERTIA_ALLOW_STUB_MODEL"),
        anthropic_base_url=anthropic_base,
        openai_base_url=openai_base,
        model_allowlist=_raw(env, "RECERTIA_MODEL_ALLOWLIST").strip(),
        model_allowlist_path=_raw(env, "RECERTIA_MODEL_ALLOWLIST_PATH").strip(),
        openai_http_referer=_raw(env, "RECERTIA_OPENAI_HTTP_REFERER").strip(),
        openai_title=_raw(env, "RECERTIA_OPENAI_TITLE").strip(),
        openrouter_title=_raw(env, "RECERTIA_OPENROUTER_TITLE").strip(),
        openai_extra_headers=_raw(env, "RECERTIA_OPENAI_EXTRA_HEADERS").strip(),
        openai_extra_body=_raw(env, "RECERTIA_OPENAI_EXTRA_BODY").strip(),
        openai_max_tokens=_optional_int(env, "RECERTIA_OPENAI_MAX_TOKENS"),
        default_input_usd_per_mtok=_float(env, "RECERTIA_DEFAULT_INPUT_USD_PER_MTOK", 1.0),
        default_output_usd_per_mtok=_float(env, "RECERTIA_DEFAULT_OUTPUT_USD_PER_MTOK", 3.0),
        model_prices=_scan_model_prices(env),
        max_blob_bytes=_int(env, "RECERTIA_MAX_BLOB_BYTES", 16 * 1024 * 1024),
        api_max_criterion_timeout_s=_int(env, "RECERTIA_API_MAX_CRITERION_TIMEOUT_S", 300),
        api_max_in_flight_runs=_int(env, "RECERTIA_API_MAX_IN_FLIGHT_RUNS", 4),
        console_auth=auth,  # type: ignore[arg-type]
        console_dev_login=_bool(env, "RECERTIA_CONSOLE_DEV_LOGIN"),
        console_dev_admin=_bool(env, "RECERTIA_CONSOLE_DEV_ADMIN"),
        console_cookie_secure=cookie,
        console_session_secret=_raw(env, "RECERTIA_CONSOLE_SESSION_SECRET").strip(),
        oidc_issuer=_raw(env, "RECERTIA_OIDC_ISSUER").strip(),
        oidc_client_id=_raw(env, "RECERTIA_OIDC_CLIENT_ID").strip(),
        oidc_client_secret=_raw(env, "RECERTIA_OIDC_CLIENT_SECRET").strip(),
        oidc_scope=_raw(env, "RECERTIA_OIDC_SCOPE", "openid profile email").strip() or "openid profile email",
        oidc_role_allowlist=_raw(env, "RECERTIA_OIDC_ROLE_ALLOWLIST", "operator,reviewer").strip()
        or "operator,reviewer",
        oidc_default_roles=_raw(env, "RECERTIA_OIDC_DEFAULT_ROLES", "operator").strip() or "operator",
        oidc_default_tenants=_raw(env, "RECERTIA_OIDC_DEFAULT_TENANTS", "default").strip() or "default",
        oidc_token_url=_raw(env, "RECERTIA_OIDC_TOKEN_URL").strip(),
        oidc_userinfo_url=_raw(env, "RECERTIA_OIDC_USERINFO_URL").strip(),
        tenant_max_runs_per_day=_int(env, "RECERTIA_TENANT_MAX_RUNS_PER_DAY", 100),
        tenant_max_cost_usd_per_day=_float(env, "RECERTIA_TENANT_MAX_COST_USD_PER_DAY", 50.0),
        tenant_max_in_flight=_int(env, "RECERTIA_TENANT_MAX_IN_FLIGHT", 4),
        tenant_skills=_bool(env, "RECERTIA_TENANT_SKILLS"),
        policy_path=_raw(env, "RECERTIA_POLICY_PATH").strip(),
        policy_version=_raw(env, "RECERTIA_POLICY_VERSION").strip(),
        library_commit=_raw(env, "RECERTIA_LIBRARY_COMMIT").strip(),
        scratch_max_steps=_int(env, "RECERTIA_SCRATCH_MAX_STEPS", 5),
        fetch_allowlist=_raw(
            env,
            "RECERTIA_FETCH_ALLOWLIST",
            "pypi.org,files.pythonhosted.org,raw.githubusercontent.com,api.github.com",
        ).strip(),
        fetch_max_bytes=_int(env, "RECERTIA_FETCH_MAX_BYTES", 512 * 1024),
        fetch_timeout_s=_float(env, "RECERTIA_FETCH_TIMEOUT_S", 20.0),
        pdf_extract=_bool(env, "RECERTIA_PDF_EXTRACT", default=True),
        allow_posix_workspace_roots=_bool(env, "RECERTIA_ALLOW_POSIX_WORKSPACE_ROOTS"),
        workspace_roots=_path_list(env, "RECERTIA_WORKSPACE_ROOTS"),
        allow_full_replay=_bool(env, "RECERTIA_ALLOW_FULL_REPLAY"),
        log_format=_choice(env, "RECERTIA_LOG_FORMAT", {"json", "text"}, "json"),  # type: ignore[arg-type]
        log_level=_raw(env, "RECERTIA_LOG_LEVEL", "INFO").strip() or "INFO",
        metrics_enabled=_bool(env, "RECERTIA_METRICS_ENABLED", default=True),
        api_key_pepper=_raw(env, "RECERTIA_API_KEY_PEPPER").strip(),
        require_image_digest=_bool(env, "RECERTIA_REQUIRE_IMAGE_DIGEST"),
        exec_max_output_bytes=_int(env, "RECERTIA_EXEC_MAX_OUTPUT_BYTES", 262144),
        jsonl_fsync_interval_s=_float(env, "RECERTIA_JSONL_FSYNC_INTERVAL_S", 1.0),
        ledger_lock_timeout_s=_float(env, "RECERTIA_LEDGER_LOCK_TIMEOUT_S", 5.0),
        ledger_verify_full=_bool(env, "RECERTIA_LEDGER_VERIFY_FULL"),
        ledger_hmac_key=_raw(env, "RECERTIA_LEDGER_HMAC_KEY").strip(),
        executor_mode=_raw(env, "RECERTIA_EXECUTOR_MODE").strip(),
        quota_reserve_ttl_s=_int(env, "RECERTIA_QUOTA_RESERVE_TTL_S", 86400),
        tenant_model_keys=_raw(env, "RECERTIA_TENANT_MODEL_KEYS").strip(),
        tenant_model_keys_file=_raw(env, "RECERTIA_TENANT_MODEL_KEYS_FILE").strip(),
    )
