"""Static Python security-review script (scripts/security_review.py)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.security_review import review  # noqa: E402


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "security_review.py"), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_security_review_script_passes_on_repo() -> None:
    result = _run("--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Result: PASS" in result.stdout


def test_json_report_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    result = _run("--check", "--json", str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["summary"]["high"] == 0
    assert payload["summary"]["controls_failed"] == 0
    assert payload["summary"]["controls_total"] >= 1


def test_flags_eval_and_pickle(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "import pickle\n\ndef run(blob):\n    return eval(blob) or pickle.loads(blob)\n",
        encoding="utf-8",
    )
    report = review(tmp_path, profile="python")
    rules = {f.rule for f in report.findings}
    assert "dynamic-eval" in rules
    assert "pickle-deserialize" in rules
    assert not report.ok()


def test_does_not_flag_method_eval_or_json_loads(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text(
        "import json\n\nclass Safe:\n    def eval(self, node):\n        return node\n\n"
        "def load(raw, workdir):\n    return Safe().eval(raw) or json.loads(raw)\n",
        encoding="utf-8",
    )
    report = review(tmp_path, profile="python")
    assert report.findings == []
    assert report.ok()


def test_flags_shell_true_and_dict_form(tmp_path: Path) -> None:
    (tmp_path / "shell.py").write_text(
        "import subprocess\n"
        "subprocess.run('true', shell=True)\n"
        "kwargs = {'shell': True}\n",
        encoding="utf-8",
    )
    report = review(tmp_path, profile="python")
    assert sum(1 for f in report.findings if f.rule == "subprocess-shell") >= 2


def test_suppression_comment_skips_finding(tmp_path: Path) -> None:
    (tmp_path / "gated.py").write_text(
        "import subprocess\n"
        "subprocess.run('true', shell=True)  # recertia-security-ok: capability-gated\n",
        encoding="utf-8",
    )
    report = review(tmp_path, profile="python")
    assert report.findings == []


def test_flags_planted_secret(tmp_path: Path) -> None:
    (tmp_path / "leak.py").write_text(
        "token = 'ghp_123456789012345678901234567890123456'\n",
        encoding="utf-8",
    )
    report = review(tmp_path, profile="python")
    assert any(f.rule == "secret-github_pat" for f in report.findings)


def test_recertia_controls_fail_when_hardening_removed(tmp_path: Path) -> None:
    (tmp_path / "contracts").mkdir()
    solver = tmp_path / "src" / "recertia" / "solver"
    solver.mkdir(parents=True)
    (solver / "container.py").write_text("# empty sandbox\n", encoding="utf-8")
    report = review(tmp_path, profile="recertia")
    failed = {c.id for c in report.failed_controls()}
    assert "container-no-new-privileges" in failed
    assert "local-exec-capability" in failed
    assert not report.ok()


def test_ast_controls_flag_an_execution_backend_write(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "recertia"
    pkg.mkdir(parents=True)
    (pkg / "bad.py").write_text(
        'import os\nos.environ["RECERTIA_EXECUTION_BACKEND"] = "local"\n',
        encoding="utf-8",
    )
    from scripts.security_review import check_ast_controls

    results = {item.id: item.ok for item in check_ast_controls(tmp_path)}
    assert results["no-execution-backend-env-write"] is False
    assert results["run-event-log-tenant-path"] is False
    assert results["policy-decision-point-referenced"] is False


def test_ast_controls_flag_missing_umask_and_origin(tmp_path: Path) -> None:
    api = tmp_path / "src" / "recertia" / "api"
    api.mkdir(parents=True)
    (api / "__init__.py").write_text("def create_app():\n    return None\n", encoding="utf-8")
    (api / "console_session_routes.py").write_text(
        "def dev_login():\n    return None\n"
        "def logout():\n    return None\n"
        "def switch_tenant():\n    return None\n",
        encoding="utf-8",
    )
    from scripts.security_review import check_ast_controls

    results = {item.id: item.ok for item in check_ast_controls(tmp_path)}
    assert results["api-umask-077"] is False
    assert results["cookie-post-origin-check"] is False
    assert results["oidc-authorize-nonce"] is False
    assert results["local-exec-shell-false"] is False
    assert results["prometheus-metrics-route"] is False
    live = {item.id: item.ok for item in check_ast_controls(REPO_ROOT)}
    assert live["api-umask-077"] is True
    assert live["cookie-post-origin-check"] is True
    assert live["oidc-authorize-nonce"] is True
    assert live["local-exec-shell-false"] is True
    assert live["prometheus-metrics-route"] is True


def test_subprocess_list_form_is_clean(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text(
        "import subprocess\nsubprocess.run(['git', 'status'], check=False)\n",
        encoding="utf-8",
    )
    report = review(tmp_path, profile="python")
    assert report.findings == []
