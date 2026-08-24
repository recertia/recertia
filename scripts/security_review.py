#!/usr/bin/env python3
"""Static Python system security review.

Scans a tree for high-signal dangerous calls (eval/exec, pickle, shell=True, …),
secret/PII patterns, and — when the Recertia layout is present — attestations that
known hardening controls still exist in source.

This is a defensive review. It does not generate exploits, payloads, or attack
procedures.

Usage:
  python3 scripts/security_review.py
  python3 scripts/security_review.py --check
  python3 scripts/security_review.py --root /path/to/project --all
  python3 scripts/security_review.py --json report.json

Exit 0 when there are no high findings and (for Recertia) all controls pass.
Suppress a finding with ``# recertia-security-ok`` on the same line or the line above.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".recertia",
        "node_modules",
        ".tox",
        "dist",
        "build",
        ".eggs",
        ".cursor",
    }
)

RECERTIA_SCAN_DIRS = ("contracts", "src", "scripts", "policy")
SECRET_SUFFIXES = frozenset({".py", ".json", ".yml", ".yaml", ".toml", ".env", ".ini", ".cfg"})
MAX_FILE_BYTES = 1_000_000

SUPPRESS_RE = re.compile(r"recertia-security-ok|security-review:\s*ok")

# Aligned with recertia.memory.procedural.hygiene — high-signal only; a match is a finding,
# not a silent scrub (specs §15.3).
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "aws_secret",
        re.compile(r"(?i)aws[_-]?secret[_-]?access[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}"),
    ),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("github_oauth", re.compile(r"\bgho_[A-Za-z0-9]{36}\b")),
    ("github_fine_grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*\b")),
    (
        "api_key_assignment",
        re.compile(r"(?i)(?:api[_-]?key|access[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}"),
    ),
    ("secret_assignment", re.compile(r"(?i)secret\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)

# Builtin / qualified calls that are high-signal for code execution or insecure deser.
_HIGH_CALLS: dict[tuple[str, ...], tuple[str, str]] = {
    ("eval",): ("dynamic-eval", "eval() executes arbitrary Python"),
    ("exec",): ("dynamic-exec", "exec() executes arbitrary Python"),
    ("builtins", "eval"): ("dynamic-eval", "builtins.eval() executes arbitrary Python"),
    ("builtins", "exec"): ("dynamic-exec", "builtins.exec() executes arbitrary Python"),
    ("os", "system"): ("os-system", "os.system() invokes a shell"),
    ("os", "popen"): ("os-popen", "os.popen() invokes a shell"),
    ("pickle", "loads"): ("pickle-deserialize", "pickle.loads deserializes untrusted data"),
    ("pickle", "load"): ("pickle-deserialize", "pickle.load deserializes untrusted data"),
    ("pickle", "Unpickler"): ("pickle-deserialize", "pickle.Unpickler deserializes untrusted data"),
    ("_pickle", "loads"): ("pickle-deserialize", "_pickle.loads deserializes untrusted data"),
    ("marshal", "loads"): ("marshal-deserialize", "marshal.loads deserializes untrusted data"),
    ("marshal", "load"): ("marshal-deserialize", "marshal.load deserializes untrusted data"),
    ("tempfile", "mktemp"): ("insecure-tempfile", "tempfile.mktemp is race-prone; use mkstemp"),
    ("ssl", "_create_unverified_context"): (
        "tls-verify-disabled",
        "ssl._create_unverified_context disables certificate verification",
    ),
    ("asyncio", "create_subprocess_shell"): (
        "subprocess-shell",
        "asyncio.create_subprocess_shell invokes a shell",
    ),
}

_SUBPROCESS_FNS = frozenset(
    {
        ("subprocess", "run"),
        ("subprocess", "Popen"),
        ("subprocess", "call"),
        ("subprocess", "check_call"),
        ("subprocess", "check_output"),
        ("subprocess", "getoutput"),
        ("subprocess", "getstatusoutput"),
    }
)

_YAML_LOAD = frozenset({("yaml", "load"), ("yaml", "unsafe_load")})
_SAFE_YAML_LOADERS = frozenset({"SafeLoader", "CSafeLoader"})


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    path: str
    line: int
    message: str


@dataclass(frozen=True)
class ControlResult:
    id: str
    ok: bool
    path: str
    message: str


@dataclass
class Report:
    root: str
    files_scanned: int = 0
    findings: list[Finding] = field(default_factory=list)
    controls: list[ControlResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {"high": 0, "medium": 0, "low": 0}
        for finding in self.findings:
            out[finding.severity] = out.get(finding.severity, 0) + 1
        return out

    def failed_controls(self) -> list[ControlResult]:
        return [c for c in self.controls if not c.ok]

    def ok(self, fail_on: str = "high") -> bool:
        if self.failed_controls():
            return False
        if fail_on == "never":
            return True
        counts = self.counts()
        if fail_on == "any":
            return not self.findings
        if fail_on == "medium":
            return counts["high"] == 0 and counts["medium"] == 0
        return counts["high"] == 0

    def to_dict(self) -> dict[str, object]:
        counts = self.counts()
        return {
            "root": self.root,
            "files_scanned": self.files_scanned,
            "ok": self.ok(),
            "summary": {
                **counts,
                "controls_failed": len(self.failed_controls()),
                "controls_total": len(self.controls),
            },
            "findings": [asdict(f) for f in self.findings],
            "controls": [asdict(c) for c in self.controls],
        }


def recertia_layout(root: Path) -> bool:
    return (root / "src" / "recertia").is_dir() and (root / "contracts").is_dir()


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _suppressed(lines: list[str], lineno: int) -> bool:
    idx = lineno - 1
    if 0 <= idx < len(lines) and SUPPRESS_RE.search(lines[idx]):
        return True
    if idx > 0 and SUPPRESS_RE.search(lines[idx - 1]):
        return True
    return False


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _kw(node: ast.Call, name: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == name and keyword.value is not None:
            return keyword.value
    return None


def _loader_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _SecurityVisitor(ast.NodeVisitor):
    def __init__(self, relpath: str, lines: list[str], findings: list[Finding]) -> None:
        self.relpath = relpath
        self.lines = lines
        self.findings = findings
        self.aliases: dict[str, tuple[str, ...]] = {}

    def _note(self, node: ast.AST, severity: str, rule: str, message: str) -> None:
        lineno = getattr(node, "lineno", 1) or 1
        if _suppressed(self.lines, lineno):
            return
        self.findings.append(
            Finding(severity=severity, rule=rule, path=self.relpath, line=lineno, message=message)
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".", 1)[0]
            self.aliases[bound] = (alias.name.split(".", 1)[0],)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.level == 0:
            base = tuple(node.module.split("."))
            for alias in node.names:
                if alias.name == "*":
                    continue
                self.aliases[alias.asname or alias.name] = base + (alias.name,)
        self.generic_visit(node)

    def _qualname(self, node: ast.AST) -> tuple[str, ...]:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, (node.id,))
        if isinstance(node, ast.Attribute):
            return self._qualname(node.value) + (node.attr,)
        return ()

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "shell"
                and value is not None
                and _is_true(value)
            ):
                self._note(
                    value,
                    "high",
                    "subprocess-shell",
                    'dict sets shell=True (subprocess will invoke a shell)',
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        path = self._qualname(node.func)
        # Foo().eval() is an Attribute whose value is a Call; _qualname then
        # collapses to ("eval",). Only a bare Name is the builtin.
        if path in {("eval",), ("exec",)} and not isinstance(node.func, ast.Name):
            path = ()
        if path in _HIGH_CALLS:
            rule, message = _HIGH_CALLS[path]
            self._note(node, "high", rule, message)
        elif path in _SUBPROCESS_FNS:
            shell = _kw(node, "shell")
            if shell is not None and _is_true(shell):
                self._note(
                    node,
                    "high",
                    "subprocess-shell",
                    f"{'.'.join(path)}(..., shell=True) invokes a shell",
                )
        elif path in _YAML_LOAD:
            loader = _kw(node, "Loader")
            loader_name = _loader_name(loader) if loader is not None else None
            if path == ("yaml", "unsafe_load") or loader_name not in _SAFE_YAML_LOADERS:
                self._note(
                    node,
                    "high",
                    "yaml-unsafe-load",
                    "yaml.load without SafeLoader can execute arbitrary constructors",
                )
            else:
                self._note(
                    node,
                    "medium",
                    "yaml-load",
                    "prefer yaml.safe_load over yaml.load even with SafeLoader",
                )
        verify = _kw(node, "verify")
        if verify is not None and _is_false(verify):
            self._note(
                node,
                "high",
                "tls-verify-disabled",
                "verify=False disables TLS certificate verification",
            )
        self.generic_visit(node)


def iter_files(root: Path, *, include_tests: bool, scan_all: bool) -> list[Path]:
    root = root.resolve()
    if scan_all or not recertia_layout(root):
        bases = [root]
    else:
        bases = [root / name for name in RECERTIA_SCAN_DIRS if (root / name).exists()]
        if include_tests and (root / "tests").is_dir():
            bases.append(root / "tests")
    files: list[Path] = []
    for base in bases:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            if not include_tests and not scan_all and "tests" in path.parts:
                continue
            if path.suffix.lower() not in SECRET_SUFFIXES and path.suffix.lower() != ".py":
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(path)
    return sorted(files)


def scan_python(path: Path, root: Path, findings: list[Finding]) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return
    _SecurityVisitor(_rel(path, root), text.splitlines(), findings).visit(tree)


def scan_secrets(path: Path, root: Path, findings: list[Finding]) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    rel = _rel(path, root)
    for lineno, line in enumerate(lines, start=1):
        if _suppressed(lines, lineno):
            continue
        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(
                    Finding(
                        severity="high",
                        rule=f"secret-{label}",
                        path=rel,
                        line=lineno,
                        message=f"possible {label} in source (store-time hygiene refuses these)",
                    )
                )


def _file_contains(root: Path, rel: str | tuple[str, ...], needles: tuple[str, ...]) -> bool:
    paths = rel if isinstance(rel, tuple) else (rel,)
    chunks: list[str] = []
    for item in paths:
        path = root / item
        if not path.is_file():
            return False
        chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    text = "\n".join(chunks)
    return all(needle in text for needle in needles)


_RECERTIA_CONTROLS: tuple[tuple[str, str | tuple[str, ...], tuple[str, ...], str], ...] = (
    (
        "container-no-new-privileges",
        "src/recertia/solver/container.py",
        ("--security-opt", "no-new-privileges"),
        "container backend must pass --security-opt no-new-privileges",
    ),
    (
        "container-cap-drop-all",
        "src/recertia/solver/container.py",
        ("--cap-drop=ALL",),
        "container backend must drop all capabilities",
    ),
    (
        "container-network-none",
        "src/recertia/solver/container.py",
        ('raise SandboxError("container backend refuses allow_network=True")',),
        "container backend must refuse allow_network=True",
    ),
    (
        "container-no-root",
        "src/recertia/solver/container.py",
        ("container root user is not allowed",),
        "container backend must refuse a root user",
    ),
    (
        "local-exec-capability",
        "src/recertia/solver/container.py",
        ("explicit LocalExecutionCapability",),
        "local executor must require an explicit LocalExecutionCapability",
    ),
    (
        "api-local-fail-closed",
        "src/recertia/solver/container.py",
        ("not allowed for the HTTP API", "RECERTIA_API_ALLOW_LOCAL_EXEC"),
        "HTTP API must refuse local exec without break-glass",
    ),
    (
        "assertion-sandbox",
        "src/recertia/validation/assertions.py",
        ("UnsafeAssertionError", "disallowed expression node"),
        "assertion evaluator must reject disallowed AST nodes",
    ),
    (
        "api-key-pbkdf2",
        "src/recertia/api/auth.py",
        ("pbkdf2_hmac", "key_hash", "salt"),
        "API keys must be stored as salted PBKDF2 hashes, not plaintext",
    ),
    (
        "hygiene-refuse-store",
        "src/recertia/memory/procedural/hygiene.py",
        ("refusing to store", "secret_scan"),
        "store-time hygiene must refuse secrets rather than scrub silently",
    ),
    (
        "t3-import-boundary",
        "src/recertia/governance/tiers.py",
        ("T3_FORBIDDEN_FOR_RUNS_AND_JOBS",),
        "T3 surfaces must remain listed as import-forbidden for runs/jobs",
    ),
    (
        "console-auth-default-off",
        "src/recertia/api/console_auth.py",
        ('RECERTIA_CONSOLE_AUTH", "off"', "dev_login_enabled"),
        "console auth must default off; dev-login is a second explicit flag",
    ),
    (
        "fetch-https-no-redirect",
        ("src/recertia/solver/registry.py", "src/recertia/solver/handlers.py"),
        ("_RefuseRedirect", 'parsed.scheme != "https"', "_https_get"),
        "fetch must be HTTPS-only and refuse redirects",
    ),
    (
        "no-legacy-key-scan",
        "src/recertia/api/auth.py",
        ("unstructured key rejected", "_STRUCTURED_SECRET_RE"),
        "API key auth must not scan every key for unstructured secrets",
    ),
    (
        "promote-requires-scope",
        "src/recertia/api/console_library_routes.py",
        ("_require_library_write", 'scope="promote"', 'scope="jobs"'),
        "promote and jobs must require a dedicated scope or admin, not runs alone",
    ),
)


def check_recertia_controls(root: Path) -> list[ControlResult]:
    results: list[ControlResult] = []
    for control_id, rel, needles, message in _RECERTIA_CONTROLS:
        ok = _file_contains(root, rel, needles)
        path = rel if isinstance(rel, str) else ", ".join(rel)
        results.append(
            ControlResult(
                id=control_id,
                ok=ok,
                path=path,
                message=message if ok else f"MISSING: {message}",
            )
        )
    return results


def review(
    root: Path,
    *,
    include_tests: bool = False,
    scan_all: bool = False,
    profile: str = "auto",
) -> Report:
    root = root.resolve()
    report = Report(root=str(root))
    files = iter_files(root, include_tests=include_tests, scan_all=scan_all)
    report.files_scanned = len(files)
    for path in files:
        if path.suffix == ".py":
            scan_python(path, root, report.findings)
        if path.suffix.lower() in SECRET_SUFFIXES:
            scan_secrets(path, root, report.findings)
    use_recertia = profile == "recertia" or (profile == "auto" and recertia_layout(root))
    if use_recertia:
        report.controls = check_recertia_controls(root)
    report.findings.sort(key=lambda f: (f.severity, f.path, f.line, f.rule))
    return report


def render(report: Report) -> str:
    counts = report.counts()
    lines = [
        f"Python security review: {report.root}",
        f"  scanned {report.files_scanned} files",
        "",
    ]
    if report.findings:
        lines.append("Findings")
        for finding in report.findings:
            loc = f"{finding.path}:{finding.line}"
            lines.append(
                f"  {finding.severity.upper():<6} {finding.rule:<24} {loc}  {finding.message}"
            )
        lines.append("")
    else:
        lines.append("Findings: none")
        lines.append("")
    if report.controls:
        lines.append("Controls (Recertia hardening attestations)")
        for control in report.controls:
            status = "PASS" if control.ok else "FAIL"
            lines.append(f"  {status:<4} {control.id:<32} {control.path}")
            if not control.ok:
                lines.append(f"       {control.message}")
        lines.append("")
    failed = report.failed_controls()
    result = "PASS" if report.ok() else "FAIL"
    lines.append(
        f"Result: {result}  high={counts['high']} medium={counts['medium']} "
        f"low={counts['low']} controls_failed={len(failed)}"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO, help="Tree to scan (default: repo root)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="CI mode: exit 1 on high findings or failed Recertia controls",
    )
    parser.add_argument("--json", type=Path, dest="json_path", help="Write a JSON report to PATH")
    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="Also scan tests/ when using Recertia production paths",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="scan_all",
        help="Scan the entire tree instead of Recertia production paths",
    )
    parser.add_argument(
        "--profile",
        choices=("auto", "recertia", "python"),
        default="auto",
        help="auto runs Recertia control checks when src/recertia + contracts/ exist",
    )
    parser.add_argument(
        "--fail-on",
        choices=("high", "medium", "any", "never"),
        default="high",
        help="Severity that makes the process fail (controls always fail closed)",
    )
    args = parser.parse_args(argv)

    report = review(
        args.root,
        include_tests=args.include_tests,
        scan_all=args.scan_all,
        profile=args.profile,
    )
    text = render(report)
    sys.stdout.write(text)
    if args.json_path is not None:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")

    passed = report.ok(args.fail_on)
    if args.check or args.fail_on != "never":
        return 0 if passed else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
