# References

Literature this design draws on, and — more usefully — where it **contradicted** an earlier
draft and forced a change.

Two honesty notes. First, the architecture was designed before this survey was run, so the
citations below are post-hoc grounding rather than provenance; where the evidence disagreed with
the design, the design changed (§1). Second, verification status is marked per entry, because
several 2026 entries were read through a citing paper's bibliography rather than fetched
directly: **[F]** = primary source fetched and read, **[B]** = citation taken from a fetched
paper's bibliography and not independently verified, and **[F, practitioner]** = non-academic
source read directly, carrying no measurements of its own and weighted as argument rather than
evidence.

## 1. Findings that changed the design

### 1.1 Self-authored skills showed no benefit; curation was the bottleneck

> "human-curated skills deliver +16.2pp over a no-skill baseline, while LLM-self-generated
> skills deliver +0.0pp"

— **SkillsBench: Benchmarking how well agent skills work across diverse tasks**, Li et al.,
arXiv:2602.12670, 2026 **[B]**

This is the most important result for us, because a naive reading says the entire premise of
this system does not work. The reframing comes from:

> "The bottleneck, across two dozen surveyed systems, is not the author but the librarian:
> lifecycle management (versioning, conflict detection, deprecation) is 'largely neglected'."

— **Ratchet: A Minimal Hygiene Recipe for Self-Evolving LLM Agents**, arXiv:2605.22148, 2026
**[F]**

Ratchet holds the author fixed (frozen model, no weight updates) and varies **only** lifecycle
management, lifting held-out pass@1 on MBPP+ hard-100 from 0.258 to a late-window 0.584
(+0.328 ± 0.018 rolling-mean gain across 100 rounds and 3 seeds), with a no-skill control at
+0.002 ± 0.005. The same recipe transfers to an agentic solver on SWE-bench Verified (+0.22 peak
lift). It names the failure mode **library drift**: silent degradation of a library's effective
quality through unchecked growth, redundancy, or premature pruning.

**Changes made:** bounded active cap and contribution-score retirement
([ADR-0006](adr/0006-bounded-library-and-retirement.md)); `curation` provenance with a higher
evidence bar for self-distilled skills than for human-authored or human-artifact-mined ones;
the Miner promoted from cold-start convenience to a primary source of library quality.

### 1.2 Premature pruning is worse than no library at all

Ratchet's ablation A4 (harsh retirement: evidence floor lowered to 20 trials, threshold
tightened to 0) posts **−0.019 ± 0.010** — *below* the no-skill floor. Retirement and the
authoring prior are load-bearing; explicit deduplication is subsumed by the authoring prior at
their scale.

**Changes made:** the earlier `min_trust = 0.4` filter after only 3 applications was a textbook
harsh-retirement setting, so demotion now requires an evidence floor of 30 trials and a loose
threshold, and low-evidence skills are score-demoted rather than dropped. Deduplication was
downgraded from a primary Curator mechanism to a secondary one.

### 1.3 An authoring prior is the single most valuable component

Ablation A3 (removing the meta-skill authoring prior from the synthesiser) costs **−0.141**,
retaining only 57% of the gain — the largest single-component effect measured.

**Change made:** the authoring prior moved from M7 (correction mining) into M3, so the distiller
has explicit, versioned authoring guidance from the moment it first writes a skill.

### 1.4 Skills synthesised from failures, not successes

Ratchet synthesises skills from *failure clusters*, making them pitfall-oriented, and connects
this to the guardrails result below. Our design distilled only from successes and kept failures
merely as retrievable dead ends.

> "Do agent rules shape or distort? Guardrails beat guidance in coding agents"

— Zhang et al., arXiv:2604.11088, 2026 **[B]**

**Change made:** a second distillation path that authors skills from recurring failure clusters,
and a preference for pitfall-shaped `failure_modes` content over aspirational step prose.

### 1.5 Flat retrieval degrades at modest library size

> "flat retrieval can degrade in the moderate-library-size regime, often around tens to hundreds
> of skills"

— **Dynamic Agent Skills: A Lifecycle Survey and Taxonomy of Evolving Skill Libraries**,
arXiv:2607.10113, 2026 **[F]** (124-paper audit, 2023–2026)

The same survey finds verifier quality materially affects skill-aware RL, and that current
benchmarks under-report library trajectories, usage-utility gaps, and safety surfaces.

**Confirmed rather than changed:** `retrieval_decay` as an early-warning metric, composition and
Curator abstraction as the response, criteria integrity as load-bearing, and `library_yield`
plus `causal_lift` as the reported-trajectory metrics the survey says are missing.

### 1.6 A non-divergence floor requires finite cap and threshold

Ratchet's Proposition 1: with a bounded active cap `C`, retirement threshold `τ`, and evidence
floor `N_min`, expected eval pass@1 is lower-bounded by `E[p0] − (τ + ε) − Cδ`. Systems with
unbounded `C` and no `τ` "have no finite analogue: the bound collapses."

**Change made:** finiteness of cap and threshold is now a structural invariant (T3), and the
floor property is stated as a design goal rather than an aspiration.

### 1.7 Graph execution: fake edges, verifier isolation, hidden edges, silent merges

> **"Graph Engineering explained: what it is, when to use it and when not to"** — Anatoli Kopadze,
> X long-form article, 24 July 2026,
> [x.com/AnatoliKopadze/status/2080668775796314331](https://x.com/AnatoliKopadze/status/2080668775796314331)
> **[F, practitioner]**

A practitioner explainer rather than research: no citations, no measurements of its own, and a
promotional close. Weighted accordingly — but it names four execution problems the design either
had or had solved only partially, and the reasoning behind each survives scrutiny independent of
the source.

Section numbers in the right-hand column refer to the [specifications index](specifications.md).

| Claim | Our response |
| --- | --- |
| The **fake-edge test**: a dependency is real only if the later step consumes the earlier one's output; steps ordered by habit serialise for nothing | Skill step edges are derived from typed `input_bindings` onto named predecessor `outputs`, so independent steps run concurrently and free-floating `depends_on` cannot be authored (§26.1) |
| Fan-out for **decomposition** — split, reduce, synthesise — is distinct from racing strategies | Added `decomposition` branches alongside the existing `portfolio` kind, with all-must-complete join semantics (§18) |
| **A worker and its verifier must never share a context**, or the check is agreement in a different font | `judge` criteria now require `fresh_context`, and the producing model instance may not score its own artifact (§26.3) |
| **Split the checking three ways** — correct, current, is the source real — since different lenses catch what identical ones miss | Multiple `judge` criteria must use distinct `lens` values (§26.3) |
| **Hidden edges**: two steps look independent while sharing a file, lock, or rate-limited API | Declared resource claims; overlapping `write`/`exclusive` claims forbid concurrency regardless of workspace isolation (§26.2) |
| **Silent node failure**: in a graph one dead branch can vanish into a result that looks complete | Merge audits recording expected against received, failing on gaps for decomposition joins (§26.4) |
| **Context collapse**: feeding a large raw fan-in into one synthesis exhausts the window | Layered fan-in — batch, summarise, combine — with deterministic code reduction where mechanical (§26.4) |
| **Anchors**: topology does not buy truth; verification needs facts that cannot argue back, and rules an optimiser would weaken must be frozen | Independent corroboration of two decisions already made: the non-`judge` criterion requirement (ADR-0003) and the T3 boundary (ADR-0005). No change |

Its framing that graphs supersede loops is timeline commentary and was ignored; a graph of loops
is still loops. Its cost illustration is worth recording, though: the Bun runtime rewrite it cites
ran roughly 50 workflows at up to 64 concurrent agents for about $165,000 in usage, with human
supervision throughout — which is the scale at which our budget and approval controls stop being
theoretical.

### 1.8 A biased judge silently disables retirement

> "A biased judge does not merely add noise; it *silently switches off the curator*."

— **The Blind Curator: How a Biased Judge Silently Disables Skill Retirement in Self-Evolving
Agents**, Zhang et al., arXiv:2607.07436, 2026 **[B]** (applicability score 10 in the preprint
survey; not previously cited here)

Ratchet's floor property assumes an unbiased reward. On reference-free tasks the reward is an
LLM judge, and that assumption fails. The paper's corrupted-reward analysis and behavioral study
show that *symmetric* noise leaves contribution-based retirement intact, but **false-pass bias**
— failures scored as passes — disables it past a sharp threshold that more data cannot cross.
The system still looks healthy: contribution stays high, the active set looks curated, and the
library drifts below the no-skill floor with nobody watching, because the mechanism that was
supposed to notice has been switched off.

**Change made:** contribution estimates (§24.2) are computed from required non-`judge`
criteria only, on the per-skill shadow-versus-suppression contrast — not by subtracting a
class control baseline from a selected skill. A skill whose only required criteria are
model-scored has `contribution = null` and MUST NOT be retired (or protected from retirement)
on contribution grounds — the same honesty rule that applies when either randomization arm
lacks observations. Judge isolation (§26.3) remains necessary but is no longer treated as
sufficient: an isolated judge that is still false-pass-biased would disable the curator just
as quietly.

### 1.9 Trajectory events are the missing measurement substrate — without weight updates

> Current agentic online RL systems lack "(i) a standardized Agent Trajectory Data Protocol
> (ATDP) that carries RL learning signals at step granularity across heterogeneous agent
> paradigms, (ii) an enterprise-grade data proxy that converts real workloads into governed
> learning substrates, and (iii) a unified agent evolution control plane that can automatically
> decide when and how to update policy weights or evolve in-context harnesses based on
> trajectory statistics."

— **Next-Generation Agentic Reinforcement Learning Systems Enable Self-Evolving Agents**,
Yan et al., arXiv:2607.01120, 2026 **[F]**

The paper's diagnosis of the trajectory gap is the useful part for us. Causal lift and
run-start ablation answer treatment-versus-control at admission time; they cannot answer
whether a *library change* would have altered outcomes on traffic already observed.
`RunState` is a live state object, not a decision event stream, and the memory ledger
records only storage mutations. That left contribution retirement and Curator proposals
without offline counterfactual evidence.

**Changes made:** ADR-0011 introduces an append-only decision-level trajectory event stream
(engine-emitted, not node-written) and offline replay under a candidate `WorldState`
(`retrieval_only` first; `validate_only` / gated `full_execution` as extensions). Replay packs
are additive Curator evidence; golden gates remain mandatory for promote-to-approved.

**What we deliberately did not take:** AReaL2.0's agent-oriented online RL loop for policy
weight updates, multi-tenant data proxies, and multi-surface evolution control planes remain
out of scope (ADR-0005). The adaptation is scaffolding-only and non-parametric: trajectories
measure and support library hygiene; they do not train weights.

## 2. Skill libraries and lifecycle management

| Work | Relevance |
| --- | --- |
| **Voyager: An Open-Ended Embodied Agent with LLMs**, Wang et al., arXiv:2305.16291, 2023 **[F]** | The origin of this architecture pattern: ever-growing skill library of verified executable code, automatic curriculum, self-verification, compositional skills, retrieval by description embedding, no fine-tuning |
| **MUSE-Autoskill: Self-Evolving Agents via Skill Creation, Memory, Management, and Evaluation**, arXiv:2605.27366, 2026 **[F]** | Five-stage skill lifecycle; per-skill memory accumulating known failure modes and input quirks — close kin to our `failure_modes` plus affordance plane |
| **Experience Compression Spectrum: Unifying Memory, Skills, and Rules in LLM Agents**, Zhang et al., arXiv:2604.15877, 2026 **[B]** | L0 raw traces → L1 episodic → L2 procedural → L3 declarative rules; independent support for plural memory (ADR-0002) |
| **Trace2Skill: Parallel Inductive Skill Distillation**, Ni et al., arXiv:2603.25158, 2026 **[B]** | Distillation from execution traces with a pruning/merging gate |
| **AutoSkill: Experience-Driven Lifelong Learning via Skill Self-Evolution**, arXiv:2603.01145, 2026 **[B]** | Skill self-evolution lifecycle |
| **SkillRL: Evolving Agents via Recursive Skill-Augmented RL**, arXiv:2602.08234, 2026 **[B]** | Skill-augmented RL; relevant to the deferred policy-learning line |
| **CASCADE: Cumulative Agentic Skill Creation**, Huang et al., arXiv:2512.23880, 2025 **[B]** | Autonomous skill development and evolution |
| **Self-Evolving LLM Agents through an Experience-Driven Lifecycle**, Wu et al., arXiv:2510.16079, 2025 **[B]** | Lifecycle framing of experience accumulation |
| **Self-Improvements in Modern Agentic Systems** — [survey hub](https://selfimproving-agent.github.io/) **[F]** | Taxonomy separating foundation-model improvement from scaffolding improvement (~166 scaffolding papers); our design is entirely in the scaffolding branch |

### 2.1 Improvement-plane search and packaging (ADR-0015)

These informed [ADR-0015](adr/0015-improvement-plane-search.md). Mechanisms were kept;
placement on the *task* graph was declined (T3 topology, default `max_attempts`, Phase-2
measurement first).

| Work | What we took | What we declined |
| --- | --- | --- |
| **How We Built Our Multi-Agent Research System**, Anthropic, MLSys 2026 **[F]** | Bounded investigation loop; deterministic control signals | A 16th `align_skills` node or in-run tree search |
| **A Survey of Agent Memory in the Second Half**, TMLR 2026, [arXiv:2602.06052](https://arxiv.org/abs/2602.06052) **[F]** | Plural memory planes; authoring vs application identity | Collapsing application sessions onto the immutable version |
| **SkillHEX / SkillProx / SkillAligner** (skill-library search and alignment line) **[B]** | Practice-only HEX; O(1) `PatchTemplate` apply in `evolve` | PUCT / hypothesis trees on `RunState` |
| **Feedback Dynamics; PoisonedEvolution** **[B]** | Async lineage revoke from quarantined authoring sources | Inline revoke on `record_dead_end` |
| **Packaging-lint study (~138K skills)** **[B]** | Deterministic packaging rules + `lint_content_hash` skip | Happy-path LLM lint; R1.3 as a hard error before seeds/miner are clean |

HEX and unit-level compress stay default-off until `practice_conversion` and a weekly lift
interval are numbers.

## 3. Memory and experiential learning

| Work | Relevance |
| --- | --- |
| **Reflexion: Language Agents with Verbal Reinforcement Learning**, Shinn et al., NeurIPS 2023 **[B]** | Self-reflection from failure feedback; the L0 anchor of the compression spectrum |
| **MemGPT: Towards LLMs as Operating Systems**, Packer et al., arXiv:2310.08560, 2023 **[B]** | Explicit memory management; L1 anchor |
| **Generative Agents: Interactive Simulacra of Human Behavior**, Park et al., UIST 2023 **[B]** | Episodic memory with reflection and retrieval scoring — closest prior art to our episodic plane |
| **ExpeL: LLM Agents Are Experiential Learners**, Zhao et al., AAAI 2024 **[B]** | Cross-task insight extraction without weight updates |
| **AutoManual: Generating Instruction Manuals by LLM Agents**, Chen et al., NeurIPS 2024 **[B]** | Rule/manual induction from interaction; kin to our semantic plane |
| **Agent Workflow Memory**, Wang et al., arXiv:2409.07429, 2024 **[B]** | Reusable workflows induced from experience; kin to composite skills |

## 4. Self-improving scaffolding and optimisation

| Work | Relevance |
| --- | --- |
| **DSPy**, Khattab et al., arXiv:2310.03714, 2023 **[B]** | Compiling declarative LM pipelines into self-improving programs; the T2 tier is this idea, governed |
| **Large Language Models as Optimizers (OPRO)**, Yang et al., ICLR 2024 **[B]** | Prompt optimisation as search; relevant to the deferred learned-ranker line |
| **TextGrad**, Yuksekgonul et al., arXiv:2406.07496, 2024 **[B]** | Textual "differentiation" for scaffolding improvement |
| **Self-Refine**, Madaan et al., NeurIPS 2023 **[B]** | Iterative self-feedback; the inner revise loop, with our addition that the checker is not the author |
| **ReAct**, Yao et al., ICLR 2023 **[B]** | Interleaved reasoning and acting; the solver's basic shape |

## 5. Evaluation, measurement, and background

| Work | Relevance |
| --- | --- |
| **SWE-bench**, Jimenez et al., ICLR 2024 **[B]** | Repository-task benchmark; the natural external eval for our first domain |
| **EvalPlus / MBPP+**, Liu et al., NeurIPS 2023 **[B]** | Rigorous code-correctness evaluation; the substrate for Ratchet's result |
| **Judging LLM-as-a-Judge with MT-Bench**, Zheng et al., NeurIPS 2023 **[B]** | Limits of model-scored evaluation; why `judge` criteria never gate promotion alone |
| **Overcoming Catastrophic Forgetting**, Kirkpatrick et al., PNAS 2017 **[B]** | Why external memory avoids the forgetting problem parametric learning has |
| **Retrieval-Augmented Generation**, Lewis et al., NeurIPS 2020 **[B]** | The retrieval substrate |
| **The Bitter Lesson**, Sutton, 2019 **[B]** | The standing argument against elaborate hand-built scaffolding; the reason `architecture/overview.md` defers parametric learning rather than dismissing it |
| **Next-Generation Agentic Reinforcement Learning Systems Enable Self-Evolving Agents**, Yan et al., arXiv:2607.01120, 2026 **[F]** | ATDP / trajectory substrate for step-granular learning signals and offline replay; informed ADR-0011. Weight-update loop and evolution control plane rejected (ADR-0005); scaffolding-only adaptation only |
| **On the Fragility of Self-Improving Agents: Variance, Task Order, and Underspecification**, Ye et al., arXiv:2608.18066, 2026 **[F]** | Memory-based self-improvers amplify evaluation variance (71% of cases) and degrade under shuffled task order; underspecification produces inapplicable memories. Directly supports multi-run lift reporting, order stress-tests, stronger criteria/env specification at distillation, and pre-promotion filtering |
| **Large Language Model Agents Are Not Always Faithful Self-Evolvers**, Zhao et al., arXiv:2601.22436, 2026 **[F]** | Causal interventions show agents depend on raw experience but frequently ignore or misinterpret condensed experience. Supports faithfulness tests on retrieved skills via trajectory events, more specific/actionable skill content, and uncertainty-gated retrieval |
| **Building Multi-Agent Systems: When and How to Use Them**, Morgan et al., ICIS 2025 **[F]** | Practical decision checklist (context overflow, specialization, parallelism, high risk, maintainability). Supports keeping single-agent default and using structured handoffs / local-context protection only if measurement shows a clear ceiling |

### 5.1 Long-horizon audited state and control planes (2026 confirmatory)

Independent 2026 work that converges on external verified state, independent audit before commit, and deterministic control-plane governance. These entries **confirm** rather than reshape the design; they strengthen grounding for optional MEA-style long-horizon execution and for the external-trajectories plan.

| Work | Relevance |
| --- | --- |
| **LongHorizon-Harness: Advancing Long-Horizon Agents for Real-World Tasks**, Ma et al., arXiv:2608.01964, 2026 **[F]** | Manage-Execute-Audit (MEA) loop; task state held outside the executor and updated only with independently verified environment facts; large multi-model gains on WeaveBench, Terminal-Bench, and OSWorld. Strong confirmation of external verified state + independent auditor |
| **StateM: Reaching 95.3% Raw Accuracy… via Harness Scaling**, Qin et al., arXiv:2608.15089, 2026 **[F]** | Durable states, phase-local context, checked transitions, recoverable runbooks; harness-level gains without weight changes (Terminal-Bench 2.1 up to 95.3%) |
| **A Deterministic Control Plane for LLM Coding Agents**, Madatha, arXiv:2606.26924, 2026 **[F]** | Configuration, permissions, and state transitions should live in deterministic code, not further LLM orchestration |
| **Evaluating AGENTS.md: Are Repository-Level Context Files Helpful for Coding Agents?**, Gloaguen et al., arXiv:2602.11988, 2026 **[F]** | Repository-level context files frequently increase cost without improving resolution rates; supports precise machine-checkable criteria over broad narrative dumps |
| **AgentPoison: Red-teaming LLM Agents via Poisoning Memory or Knowledge Bases**, Chen et al., arXiv:2407.12784, 2024 **[F]** | Memory / knowledge-base poisoning achieves high ASR at very low poison rates; later 2026 studies reconfirm why memory writes must remain gated and provenance-required |

## 6. Ideas used without a specific citation

These are standard practice in other fields, adapted here rather than taken from agent
literature: **pre-registration** of hypotheses (clinical trials and empirical science, the basis
of ADR-0003), **mutation testing** (software testing, the basis of sensitivity proofs),
**A/B holdout arms** (online experimentation, the basis of the ablation control),
**train/test leakage discipline** (machine learning, the basis of the eval firewall),
**sagas and compensating transactions** (distributed systems, the basis of attempt isolation),
and **append-only hash chains** (tamper-evident logging, the basis of the provenance ledger).

## 7. Scored survey and next reading

The full applicability scoring of ~117 preprints against Recertia's non-negotiables lives in
[`../research/preprints-self-improving-agents.xlsx`](../research/preprints-self-improving-agents.xlsx)
(machine-readable: [`../research/preprints-self-improving-agents.scored.json`](../research/preprints-self-improving-agents.scored.json)),
with sheets for the rubric, every entry's score and rationale, the core/high cut (7–10), and the
distribution. Score-10 papers are already absorbed above; the remaining score-9 papers are next
reading, not yet design-shaping, and are listed so they do not get lost behind the spreadsheet:

| Paper | arXiv | Why it is next |
| --- | --- | --- |
| **Falsifiable Release Gates for Self-Improving Systems** | [2607.13070](https://arxiv.org/abs/2607.13070) | Pre-declared machine-checkable acceptance suites and standing invariants — close kin to criteria preregistration and the T3 boundary |
| **Not All Skills Help: Measuring and Repairing Agent Knowledge** | [2606.15390](https://arxiv.org/abs/2606.15390) | Per-skill causal contribution via randomized measurement — independent support for `causal_lift` / contribution retirement |
| **PACE: Anytime-Valid Acceptance Tests for Self-Evolving Agents** | [2606.08106](https://arxiv.org/abs/2606.08106) | The acceptor, not the proposer, is the weak point; "keep it if the score went up" is uncontrolled adaptive testing |
| **Self-Authored Verification Is Unreliable in Heuristic Self-Improving Agents** | [2607.24300](https://arxiv.org/abs/2607.24300) | Verifier–deployment gap when the agent authors its own tests — further support for ADR-0003 |

Reference lists extracted from the four score-10 papers are in
[`../research/score10-references/`](../research/score10-references/) and
[`../research/preprints-score10-reference-lists.xlsx`](../research/preprints-score10-reference-lists.xlsx).

## 8. Open questions the literature does not settle for us

This section has moved to [`docs/assumptions.md`](assumptions.md), which turns each open
question below into a tracked claim (`a1`, `a2`, `a3`) with an explicit status and an explicit
split between the engineering gate that can be checked today and the research outcome that
cannot (refactor-plan B7). Kept here only as a pointer:

- SkillsBench's null result → [`assumptions.md#a1`](assumptions.md#a1-machine-checkable-domains-show-a-positive-causal-lift-from-skill-retrieval).
- Ratchet's evidence floor at our throughput → [`assumptions.md#a2`](assumptions.md#a2-ratchets-evidence-floor-is-reachable-at-our-traffic-volume).
- No external precedent for the ADR-0005 self-modification boundary → [`assumptions.md#a3`](assumptions.md#a3-a-tiered-self-modification-boundary-is-sufficient-without-an-externally-reported-precedent).

## 9. Attached one-pagers reviewed and declined (2026-08)

Three IEEE-styled posters/compilations were reviewed for bibliography inclusion. None met the
threshold used elsewhere in this file — a design-shaping claim with measurements or a
practitioner argument that survived independent scrutiny — so they are **not** cited in
§§1–5. Scores are 0–10 for topical *relevance* and for *importance* as evidence. Cite only if
both are high.

| Artifact | How read | Relevance | Importance | Verdict |
| --- | --- | --- | --- | --- |
| **Continuous Skill Acquisition** (poster: Scan→Filter→Curate→Extract→Evaluate→Generate→Store→Publish from public OSS) | Chat attachment (twice); **no public PDF/Drive/arXiv** under distinctive phrases | 6 | 2 | Decline |
| **Graph Engineering: The Karpathy Loop, Improved 1000x… The Anthropic Playbook** | Chat attachment + full [Drive PDF](https://drive.google.com/file/d/1-GOg0kxcp8tx1BMUECMj2yJq6JYGmfhb/view) (11 pp.) + OCR **[F, practitioner]** | 5 | 2 | Decline |
| **My 4 Steps — From Loop Engineering to Graph Engineering** | Chat attachment + LinkedIn one-pager OCR **[F, practitioner]** | 5 | 1 | Decline |

**Continuous Skill Acquisition.** Re-read from the re-attached poster. Architecture is three
layers / eight steps: Discovery (Scan GitHub trending & tech reports → Filter duplicates /
malware → Curate by risk/utility), Extraction (Workflow Extraction → Skill Evaluation via
lint/tests → Skill Generation as YAML/JSON), Deployment (Store in org library → Publish to
agents). Abstract claims agents must discover workflows across public repos; index terms
include “remote sensing” (domain-mismatched tell). Still **not citable**: no fetchable
primary, no measurements, and open-ecosystem crawl-and-install contradicts SkillsBench (§1.1)
— Miner prefers `mined_from_human_artifact` in the owner’s repo. Pipeline rhyme with Miner →
validate → promote / Curator is already covered by Dynamic Agent Skills (§1.5) and Voyager /
MUSE (§2). No change. A durable PDF upload would not change the importance score without
measurements.

**Graph Engineering / “Anthropic Playbook” [F, practitioner].** The circulating Drive PDF’s
cover and acknowledgment state **independent synthesis, July 2026 — not affiliated with
Karpathy or Anthropic**. Content maps public sources (`autoresearch` / AgentHub; Anthropic
workflows + KG Cookbook). Useful surviving claims (measured loop before swarm, typed shared
state, edge-grounded evaluators, persistence, human ownership of objectives) restate Kopadze
§1.7 and ADR-0001. The “1000×” headline has no benchmark in the PDF. Chat re-attachments
sometimes present a different surface branding (Anthropic mark, “2024” header, role-swarm
diagram); treat those as the same viral compilation family, not as Anthropic authorship. Do
not cite; cite primaries or §1.7. No change.

**“Andrew Ng / 4 Steps” [F, practitioner].** OCR: “Independently compiled, July 2026 — not
affiliated with Andrew Ng and not endorsed.” Restates Ng’s four patterns and the Batch
HumanEval anecdote; the loop→graph ladder is the compiler’s overlay. Patterns already
anchored in §§3–4 (Reflexion, ReAct, Self-Refine). Do not cite this compilation. No change.

## 10. Human-interface design for post-prompt systems

New category. Grounds the post-prompt interface exploration in
[`../research/loops-and-graphs-horizon.md`](../research/loops-and-graphs-horizon.md)
— an exploration, not a design-shaping input to the current milestone stack, but held to
the same [F]/[B] discipline as §§1–5 because that document makes citable claims about what
replaces a prompt as the point of contact between a person and this system. The full
argument lives in that research note; this entry is the bibliography record.

| Work | Relevance |
| --- | --- |
| **Direct Manipulation: A Step Beyond Programming Languages**, Shneiderman, *IEEE Computer* 16(8), 1983 **[F]** | Names the property — continuous representation, reversible action, gesture instead of command syntax — that a prompt box regresses from |
| **Direct Manipulation Interfaces**, Hutchins, Hollan & Norman, *Human–Computer Interaction* 1(4), 1985 **[F]** | Origin of the gulf of execution / gulf of evaluation; a chat window makes both gulfs linguistic instead of closing them with visible state |
| **Bridging the Gulf of Envisioning**, Subramonyam, Pondoc, Seifert, Agrawala & Pea, CHI 2024, arXiv:2309.14459 **[F]** | Extends Norman's gulfs with an LLM-specific one — capability / instruction / intentionality gaps; Goal compilation (ADR-0010) answers two of three by construction |
| **Why Johnny Can't Prompt: How Non-AI Experts Try (and Fail) to Design LLM Prompts**, Zamfirescu-Pereira, Wong, Hartmann & Yang, CHI 2023 **[B]** | Non-experts design prompts opportunistically and default to treating them as human-to-human instructions, because the interface offers no other vocabulary |
| **Principles of Mixed-Initiative User Interfaces**, Horvitz, CHI 1999 **[F]** | Coupling automated services with direct manipulation; efficient invoke / dismiss / correct as design requirements, not afterthoughts |
| **Guidelines for Human-AI Interaction**, Amershi, Weld, Vorvoreanu et al., CHI 2019 **[F]** | 18 guidelines across the interaction lifecycle; several map onto T0–T3 (ADR-0005) as an enforcement mechanism not yet surfaced as a console-legible control |
| **Levels of Autonomy for AI Agents**, arXiv:2506.12469, 2025 **[F]** | Five user roles — operator, collaborator, consultant, approver, observer — that nearly reproduce T0–T3 independently, as a user-facing rather than backend property |
| **Trust in Automation: Designing for Appropriate Reliance**, Lee & See, *Human Factors* 46(1), 2004 **[F]** | Calibration / resolution / specificity as the three properties a trust display owes; the argument against ever showing one opaque skill-quality number |
| **Graphologue: Exploring Large Language Model Responses with Interactive Diagrams**, Jiang, Rayan, Dow & Xia, UIST 2023, arXiv:2305.11473 **[F]** | Retrofits node-link diagrams onto linear LLM chat; Recertia's graph runtime (ADR-0001) produces the same structure natively |
| **Sensecape: Enabling Multilevel Exploration and Sensemaking with Large Language Models**, Suh, Min, Palani & Xia, UIST 2023, arXiv:2305.11483 **[F]** | Retrofits hierarchy/abstraction-level views onto linear chat for the same underlying reason |
| **Usability Analysis of Visual Programming Environments: A 'Cognitive Dimensions' Framework**, Green & Petre, *J. Visual Languages & Computing* 7(2), 1996 **[F]** | Vocabulary — viscosity, closeness of mapping, premature commitment, secondary notation — for evaluating Goal/DesiredState/Constraint authoring as a notation |
| **Watch What I Do: Programming by Demonstration**, Cypher et al. (eds.), MIT Press, 1993 **[F]** | Precedent for specifying desired behaviour by example rather than describing it in prose; motivates authoring-from-precedent over blank-page Goal writing |
| **End-User Programming**, Myers & Ko, invited research overview, 2006 **[F]** | Survey of end-user programming as a field distinct from professional programming; frames Goal authoring as an end-user-programming problem |
| **Malleable Software: Restoring User Agency in a World of Locked-Down Apps**, Litt, Horowitz, van Hardenberg & Matthews, Ink & Switch, 2025 **[F, practitioner]** | Names the "editable, not appliance" property the diffable, versioned memory plane (ADR-0002) already has, and the gentle-slope pattern a library browser still owes |

## 11. Loops-and-graphs horizon note (not a design change)

[`../research/loops-and-graphs-horizon.md`](../research/loops-and-graphs-horizon.md) is a
dated research essay (August 2026) on **runtime topology**.
Inner / outer / meta loops stay distinct; “graphs supersede loops” remains the overlay
already ignored in §1.7; the scarce 10-year good is a falsifiable loop, not a larger graph.
It cites primaries already in this file. It is **not** an ADR, not remaining work, and it
does not update `a1`–`a4`. No topology, engine, HEX, or C5 change follows from it.
