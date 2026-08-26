# RW-RC4 design — miner candidate from Recuris

**Do not build.** Design only. Parent: [`2026-08-26-recuris-backlog.md`](2026-08-26-recuris-backlog.md).

## Intent

One candidate skill, provenance `mined_from_paper:2608.24876`, body = checked-WM commit rule. Golden gate still applies. No free lift.

## Skill sketch

```yaml
id: checked-wm-commit
provenance: mined_from_paper:2608.24876
status: candidate
task_class: repo-chore
failure_modes:
  - asserting done without an environment observation
  - marking blocked without a named blocker + evidence
steps:
  - name: require_env_hash
    rule: status transitions to done|blocked need criterion pass + env observation hash
```

## Gate

1. Lint + golden eval on `repo-chore` fixtures.
2. Lift vs `bare` and vs `$M_0`. Promote only if treatment beats both or the report says `not established` and a human still accepts candidate (not approved).
3. Recertifier: if contribution interval_high < −τ after N_min, retire. Recuris did not do this. Recertia must.

## Out of scope

Importing Recuris's +51 skill pile. Auto-distill from the PDF. Cross-benchmark claims.
