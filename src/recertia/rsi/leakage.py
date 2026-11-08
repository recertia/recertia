"""Pure leakage screen. Spec R2, section 9.2.

Findings carry a hash of the offending span, never the span text.
"""

from __future__ import annotations

import hashlib
import re

from recertia.rsi.models import LeakageFinding

SCAN_VERSION = "leak-v1"

_PRIVATE = re.compile(r"PRIV_GUIDANCE:|<private-guidance>")
_SYS = re.compile(r"SYSTEM_PROMPT:|<<SYS>>")
_HOST = re.compile(r"(?:^|[\s\"'=])(?:/home/|/Users/|/etc/|/var/|/root/)|[A-Za-z]:\\")
_SECRET = re.compile(r"AWS_SECRET|API_KEY=|SECRET_TOKEN")


def _hash_span(span: str) -> str:
    return hashlib.sha256(span.encode("utf-8")).hexdigest()


def _scan(text: str, pattern: re.Pattern, code: str) -> list[LeakageFinding]:
    findings = []
    for match in pattern.finditer(text):
        findings.append(LeakageFinding(code=code, span_hash=_hash_span(match.group(0))))
    return findings


def screen_leakage(
    actions: list,
    observations: list,
    scan_version: str = SCAN_VERSION,
) -> list[LeakageFinding]:
    if scan_version != SCAN_VERSION:
        raise ValueError(f"unsupported scan version: {scan_version}")
    blob = "\n".join(str(x) for x in list(actions) + list(observations))
    findings: list[LeakageFinding] = []
    findings.extend(_scan(blob, _PRIVATE, "private_guidance"))
    findings.extend(_scan(blob, _SYS, "system_prompt_fragment"))
    findings.extend(_scan(blob, _HOST, "host_path"))
    findings.extend(_scan(blob, _SECRET, "secret_env"))
    return findings
