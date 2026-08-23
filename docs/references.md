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

### 1.10 Strategy lock-in is already encoded — confirming, not changing

> "What agents lack is neither experience, guidance, nor reasoning compute, but a
> mechanism for spontaneously reevaluating their strategy during execution."

— **What is Missing from AI Post-Training AI: An Empirical Analysis**, Lim, Huang, Peng,
Lu, Cong, Zhang, Sun, and Lin, arXiv:2608.19072, 19 August 2026 **[F]**

The paper separates *execution-level* capability (iterate inside a chosen training
strategy) from *strategy-level* capability (revise the high-level plan as evidence
accumulates). Across 1,338 PostTrainBench trajectories, strategy is locked in before the
first training run; remaining budget is local adjustment. Experience scaffolds lift
execution (+12.6 GSM8K, +40.8 HumanEval) but leave strategy static. Human guidance
redirects the *initial* strategy, then erodes once training starts. Extra inference
compute helps easy tasks and plateaus on hard ones.

**Confirmed rather than changed.** Recertia already encodes that split. [`plan`](../src/recertia/nodes/plan.py)
chooses `apply` / `adapt` / `scratch` / `portfolio` / `decomposition` / `abstain` once.
[`evolve`](../src/recertia/nodes/evolve.py) is class-gated local repair: only `plan` and
`retrieval` failure classes switch strategy; `environment` / `tool` / `execution` /
`merge` keep it. A Practice-published `PatchTemplate` apply is O(1) and does not change
strategy ([ADR-0015](adr/0015-improvement-plane-search.md)). Strategy-level search (HEX)
is Practice-only, offline, default-off — the paper's "experience scaffold" result is why
HEX was not put on the task graph. Full take/decline in [§12](#12-attached-papers-reviewed-for-applicability-2026-08-23).

**What we deliberately did not take:** PostTrainBench's object is weight updates of
another LLM. Recertia's explicit non-goal remains "no model weight training"
([overview](architecture/overview.md) §1). No 16th `replan` node. No new assumption —
`strategy_change_rate` is unscheduled telemetry, not a remaining-work item.

### 1.11 Phantom Gains: every transition statistic needs a measured null — confirming, not changing

> "Transition-level auditing therefore requires a separately measured null for every
> statistic it reports."

— **Phantom Gains: Auditing Self-Improvement Against a Measured Null**, Xu, Yan, Chen,
and Kechadi, arXiv:2608.20290, 20 August 2026 **[F]**

Item-level "learned / corrupted" ledgers are differences of two noisy estimates. Auditing
three rounds of LoRA self-training against a frozen copy pushed through the identical
pipeline, the authors isolate seven measurement failures, each of which **inverts a
reported finding** when its control is absent. A single greedy decode manufactures
capability changes on an untrained model (largely an inference-batching artifact). The
expansion statistic that separates acquisition from sharpening assigns that same frozen
model a rate of 0.280; the obvious threshold repair still has a non-zero measured null.
The repair is a per-problem exact test against a pooled baseline under FDR, which detects
nothing on held-out replicates. Applied to matched arms, external distillation improves
problems the base model rarely reaches; three self-training arms do not, and self-training
corrupts baseline-solved problems well above the measured floor.

**Confirmed rather than changed.** Recertia already refuses "the score went up": Wilson
intervals, `not established`, the ablation arm, and contribution from non-`judge`
criteria only ([§1.8](#18-a-biased-judge-silently-disables-retirement), ADR-0003). This
paper is the item-level version of that honesty. It supports RW-M2 / `a1` reporting:
multi-run lift, order stress, and not treating a single greedy pass as a state. Full
take/decline in [§13](#13-five-day-arxiv-scan-2026-08-18-to-2026-08-23).

**What we deliberately did not take:** the object they audit is LoRA self-training — a
Recertia non-goal. No weight-update loop. No remaining-work item for a transition ledger;
`route_log` already records per-run outcomes. A per-task ledger against the control arm
is unscheduled telemetry.

### 1.12 Skill-set packing under a token budget is retrieve-time, not a new node — not implemented

> Loading reusable skill documents into a bounded context window is now the primary way
> LLM agents acquire task-specific capabilities, which makes skill selection a first-order
> determinant of task performance and token cost.

— **Optimal Skill Selection for LLM Agents with Provable Bicriteria Guarantees**, Chen,
Chen, Wang, Li, and Huang, arXiv:2608.19993, 20 August 2026 **[F]**

Independent top-*k* / greedy packing ignores complementarity, redundancy, and context
tax. The authors maximize monotone-submodular benefit minus a linear length penalty
under a hard token budget; **Best Prefix Selection (BPS)** is a polynomial bicriteria
\((1-1/e, 1)\) guarantee. On a contamination-controlled BigCodeBench-style coding setup:
**0.73** task success vs **0.20–0.52** for released routers, retrievers, and the
executor's own selection, on **28% fewer tokens**. Wrong or extra skills can drop
success **below** the no-skill baseline.

**What this does not change today.** Recertia's bounded active set (ADR-0006) is
*library* capacity, not *prompt* capacity. `retrieve` still returns a federated bundle
behind a score floor. BPS is scaffolding (frozen executor, no weight updates, coding
domain) and would live **inside** the existing `retrieve` node as a packer — not a 16th
node, and not the deferred learned ranker (remaining-work rule 3; ADR-0015). Fit the
benefit from Recertia's own pass/fail traces before claiming lift. Full take/decline in
[§13](#13-five-day-arxiv-scan-2026-08-18-to-2026-08-23).

**What we deliberately did not take:** implementing BPS in this change; growing the
installed library because "selection is now optimal"; fitting a capability model from
live traffic without the ablation firewall.

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
| **Break It Down, Pass It On: Cross-Task Skill Transfer in LLM Agents**, Feng, Bijoy, Balasubramanian, and Zhou, arXiv:2608.20274, 2026 **[F]** | Task-level induced skills often fall *below* the no-memory baseline; subtask-level skills raise it. Text transfers better than code. Skill utility (specificity × abstractness) predicts transfer before a new run. Confirms pitfall/subtask-shaped `failure_modes` and a possible advisory retrieve pre-filter — not a new node. See [§13](#13-five-day-arxiv-scan-2026-08-18-to-2026-08-23) |
| **SkillForge: Self-Distilling Agents for Project-Specific Issue Resolution**, Chen, Li, Gu, Shi, and Guan, arXiv:2608.18933, 2026 **[F]** | Synthesizes project issues from test-covered core functions, then distills entity-grounded skills. SWE-bench Verified +5.6–5.8pp over Mini-SWE-Agent. Repo-chore cousin of Miner + Practice; do not add a synthetic-issue generator until Practice conversion is a measured number |
| **Optimal Skill Selection for LLM Agents with Provable Bicriteria Guarantees**, Chen, Chen, Wang, Li, and Huang, arXiv:2608.19993, 2026 **[F]** | Set-level packing under a token budget with a bicriteria guarantee; bad extras can beat the empty-bundle control the wrong way. Retrieve-time packer candidate; not implemented. See [§1.12](#112-skill-set-packing-under-a-token-budget-is-retrieve-time-not-a-new-node-not-implemented) |

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
| **MemTrapBench: Benchmarking Cognitive Traps in LLM Memory Use**, Wang et al., arXiv:2608.20202, 2026 **[F]** | Faithfully stored, semantically relevant memories can still distort reasoning (fixation, belief distortion); all evaluated memory stacks lost >10pp vs no-memory. Independent support for the empty-bundle floor and Zhao faithfulness tests. AdaptiveMem is a prompt, not a new plane |

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
| **Phantom Gains: Auditing Self-Improvement Against a Measured Null**, Xu, Yan, Chen, and Kechadi, arXiv:2608.20290, 2026 **[F]** | Item-level learned/corrupted ledgers invert findings without a measured null; greedy+batching manufactures transitions on a frozen model. Confirms Wilson / `not established` / ablation-arm honesty at transition grain. See [§1.11](#111-phantom-gains-every-transition-statistic-needs-a-measured-null-confirming-not-changing) |
| **Large Language Model Agents Are Not Always Faithful Self-Evolvers**, Zhao et al., arXiv:2601.22436, 2026 **[F]** | Causal interventions show agents depend on raw experience but frequently ignore or misinterpret condensed experience. Supports faithfulness tests on retrieved skills via trajectory events, more specific/actionable skill content, and uncertainty-gated retrieval |
| **Building Multi-Agent Systems: When and How to Use Them**, Morgan et al., ICIS 2025 **[F]** | Practical decision checklist (context overflow, specialization, parallelism, high risk, maintainability). Supports keeping single-agent default and using structured handoffs / local-context protection only if measurement shows a clear ceiling |

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

## 12. Attached papers reviewed for applicability (2026-08-23)

Four preprints were attached as first pages, then fetched from arXiv **[F]** and scored
against the existing rubric in
[`../research/preprints-self-improving-agents.scored.json`](../research/preprints-self-improving-agents.scored.json)
(1–10, scaffolding-only, `repo-chore` domain, no weight training, 15-node T3 graph). None
was in the 2026-07-30 scored survey. None is remaining work. Fifteen nodes stay T3. The
first domain stays repository chores
([measurement-and-scope.md](architecture/measurement-and-scope.md) §17).

| Paper | arXiv | Relevance | Importance | Verdict |
| --- | --- | --- | --- | --- |
| **What is Missing from AI Post-Training AI** | [2608.19072](https://arxiv.org/abs/2608.19072) | 8 | 7 | **Confirm.** Strategy lock-in is already encoded; see [§1.10](#110-strategy-lock-in-is-already-encoded-confirming-not-changing). |
| **What makes prompts a graph** | [2607.27578](https://arxiv.org/abs/2607.27578) | 7 | 4 | **Vocabulary.** Recertia already passes T1–T4; distinctive stance is T3 freeze of the *task* graph. |
| **MerchantBench** | [2607.28956](https://arxiv.org/abs/2607.28956) | 4 | 3 | **Decline for design.** Domain-locked e-commerce; keep the delayed-feedback analogy only. |
| **Recirculation** | [2608.17981](https://arxiv.org/abs/2608.17981) | 2 | 2 | **Decline.** Inference-time model architecture; Recertia does not own the forward pass. |

Relevance is topical fit to Recertia's scaffolding, graph, and memory planes.
Importance is whether the paper should change a load-bearing decision. Cite only if both
are high — the same bar as §9.

### 12.1 What is Missing from AI Post-Training AI (score 8) — confirm

**Lim, Huang, Peng, Lu, Cong, Zhang, Sun, and Lin**, arXiv:2608.19072, 19 August 2026
**[F]**. Empirical analysis of 1,338 PostTrainBench trajectories (seven benchmarks, four
base models, 20 agent configurations). Agents conflate execution-level capability
(iterate inside a chosen strategy) with strategy-level capability (revise the high-level
plan as evidence accumulates). Strategy is locked in before the first training run;
remaining budget is local adjustment. Experience scaffolds lift execution but leave
strategy static. Human guidance redirects the initial strategy, then erodes once training
starts. Extra inference compute helps easy tasks and plateaus on hard ones. What is
missing is a mechanism that reopens strategic choice during execution.

**Take:** independent empirical support for keeping `plan` a one-shot node, refusing
in-run tree search, treating `evolve` as execution-level except on `plan` / `retrieval`
classes, and putting genuine strategy revision on the improvement plane. Design impact is
recorded in [§1.10](#110-strategy-lock-in-is-already-encoded-confirming-not-changing);
this subsection is the longer reading note.

**Decline:** PostTrainBench's object is weight updates of another LLM. Do not add an
AI-for-AI post-training domain. Do not add a `replan` node. Do not add an assumption
unless we later choose to *measure* `strategy_change_rate` on live traffic — `route_log`
already records evolve moves; that derived metric is unscheduled.

### 12.2 What makes prompts a graph (score 7) — vocabulary

**Macedo**, *What makes prompts a graph: necessary and sufficient conditions for prompt
graph engineering*, arXiv:2607.27578, 30 July 2026 **[F]**. Definitional; no
measurements. Four conditions: (T1) explicit enumerable structure; (T2) separation of
topology from prompt text; (T3) executable semantics including cycles; (T4) graph as a
first-class artifact (inspect / version / check / optimize). Inclusion test: LangGraph,
DSPy, Prompt Flow in; AutoGen / CrewAI mode-split; Claude Code subagents out (authored
nodes, emergent flow).

Recertia against the test:

| Condition | Task graph (15 nodes, T3) | Skill step graphs |
| --- | --- | --- |
| T1 explicit | Yes: [`contracts/graph.py`](../contracts/graph.py) | Yes: `input_bindings` → waves |
| T2 structure vs content | Yes: frozen topology vs skill/prompt text | Yes: Curator parallelise/serialise vs step prose |
| T3 executable + cycles | Yes: in-house engine, back-edge `evolve → solve` | Yes: dependency waves |
| T4 first-class artifact | Partial: inspectable and versioned, **not optimizable** (T3) | Yes: Curator proposals, golden gate |

The paper's object clause is "prompt-parameterized model invocations as nodes." Recertia's
**task** graph is a concern graph (`intake`, `retrieve`, `plan`, `solve`, …) with LLM
calls as leaves — closer to a cyclic workflow engine than to DSPy modules. Skill step
graphs are the closer prompt-graph. That is a fifth design position the six evaluated
systems do not occupy: the control graph is T3-frozen; the skill graphs are the
improvable artifacts.

**Take:** cite next to Kopadze (§1.7) and DSPy (§4) as the operational definition Recertia
already satisfies, and as independent support for [ADR-0001](adr/0001-graph-with-loops.md)'s
rejection of LangGraph (own the state schema). Use T1–T4 as the vocabulary when someone
asks whether Recertia is doing prompt graph engineering.

**Decline:** the paper's research agenda (search over graph topology, emergent→explicit
lift, automatic graph optimization) is exactly what Recertia declined for the *task*
graph (remaining-work rule 3; [ADR-0005](adr/0005-self-modification-boundary.md) T3). Do
not grow `contracts.graph.NODES`. Definitional papers with no measurements do not enter
§1 as design-shaping.

### 12.3 MerchantBench (score 4) — decline for design

**Shi, Tao, Jin, Kang, Dou, Zhu, Pan, Fu, Wang, Li, Cheng, Weng, and Huo**,
*MerchantBench: Benchmarking LLM Agents for Long-Term Coherence in E-Commerce
Operations*, arXiv:2607.28956, 4 August 2026 **[F]**. 365-day seller simulation grounded
in 98,843 product records and 26 tools; best LLM configuration reaches 27.3% of human
mean final net assets.

**Long-term coherence** (preserve purpose across a long horizon while adapting to delayed
evidence) is Recertia's *outer* loop in prose: memory written by one run is read by the
next. The delayed-feedback mechanic (orders commit cash before settlement; rating damage
arrives later) is the same shape as Recertia's Recertifier + episodic dead ends: action
now, outcome later, incoherent behavior compounds.

**Why it does not shape design.** Rubric band 3–4 (domain-locked trading/ops). Recertia's
evaluation unit is a **bounded run** with machine-checkable criteria on `repo-chore`, not
a 365-day persistent operator
([measurement-and-scope.md](architecture/measurement-and-scope.md) §17). Opening
e-commerce would be a third task class before Phase 4 — remaining work lists that as out
of scope. Recertia already refused to be a long-running daemon; improvement is scheduled
jobs ([ADR-0004](adr/0004-offline-improvement-plane.md)). The headline gap (27.3% of human
net assets) does not transfer: Recertia does not optimize net assets.

**Take (one sentence):** delayed, heterogeneous feedback is why Recertifier and negative
knowledge exist; MerchantBench is a domain-locked measurement of that failure mode, not a
benchmark Recertia should run.

**Decline:** no MerchantBench evals, no cash-flow tools, no 365-day soak as a product
goal. RW-GA soak stays four weeks of `repo-chore`.

### 12.4 Recirculation (score 2) — decline

**Mozer, Siddiqui, Sawyer, Sanyal, and Liu**, *Recirculation*, arXiv:2608.17981, 18 August
2026 **[F]**. Training-free inference-time recurrence: leak a deep residual-stream
activation into a shallower layer so the model tracks belief state. Gemma 3: −23%
perplexity, +21% GSM8K. Requires changing the **forward pass** (serial prefill); vLLM has
an experimental RFC.

**Why it is out of scope.** Recertia consumes OpenAI-compatible gateways
([ADR-0013](adr/0013-openai-compat-gateways.md)). It does not own weights, KV cache, or
layer execution. Belief-state tracking is already Recertia's job **outside** the model:
`RunState`, plural memory, checkpoints. Recirculation would be a serving-stack choice for
an operator who self-hosts Gemma 3; it cannot be implemented in this repo without
violating the scaffolding-only non-goal
([overview](architecture/overview.md) §1).

**Decline.** Do not add remaining work. Do not propose a Recertia serving feature.

## 13. Five-day arXiv scan (2026-08-18 to 2026-08-23)

Papers first posted (or first seen in the cs.AI/cs.SE new listings) from 18 through 23
August 2026, scored against the same rubric as §12. Already in this file and skipped
here: Ye et al. fragility (`2608.18066`, §5), Lim et al. strategy lock-in (`2608.19072`,
§1.10 / §12), Recirculation (`2608.17981`, §12.4). None of the rows below is remaining
work. Fifteen nodes stay T3. No learned ranker, no weight-update loop, no third domain.

| Paper | arXiv | Relevance | Importance | Verdict |
| --- | --- | --- | --- | --- |
| **Optimal Skill Selection for LLM Agents with Provable Bicriteria Guarantees** | [2608.19993](https://arxiv.org/abs/2608.19993) | 9 | 8 | **Cite; packer later.** Set-level retrieve packing. See [§1.12](#112-skill-set-packing-under-a-token-budget-is-retrieve-time-not-a-new-node-not-implemented). |
| **Phantom Gains: Auditing Self-Improvement Against a Measured Null** | [2608.20290](https://arxiv.org/abs/2608.20290) | 8 | 8 | **Confirm.** Item-level measured nulls. See [§1.11](#111-phantom-gains-every-transition-statistic-needs-a-measured-null-confirming-not-changing). |
| **Break It Down, Pass It On: Cross-Task Skill Transfer in LLM Agents** | [2608.20274](https://arxiv.org/abs/2608.20274) | 8 | 7 | **Confirm.** Subtask-grain distill; utility as advisory retrieve filter. |
| **SkillForge: Self-Distilling Agents for Project-Specific Issue Resolution** | [2608.18933](https://arxiv.org/abs/2608.18933) | 7 | 5 | **Cite.** SWE cousin of Miner + Practice; no synthetic-issue generator yet. |
| **MemTrapBench: Benchmarking Cognitive Traps in LLM Memory Use** | [2608.20202](https://arxiv.org/abs/2608.20202) | 7 | 5 | **Cite.** Retrieved memory can hurt; supports the empty-bundle floor. |
| **Measuring What a Specification Determines** | [2608.19475](https://arxiv.org/abs/2608.19475) | 6 | 4 | **Decline for design.** Execution-judged spec quality; 14.4pp noise floor. Kin to criteria preregistration only. |
| **SkillGate: Training In-Policy Skill Selection in Long-Horizon Agents** | [2608.18852](https://arxiv.org/abs/2608.18852) | 5 | 3 | **Decline mechanism.** Selector credit starvation is real; the fix is RL on weights (ADR-0005). |
| **Write, Execute, Refine** | [2608.17587](https://arxiv.org/abs/2608.17587) | 5 | 3 | **Decline mechanism.** Skill optimizer via RL from execution feedback. |
| **From Agent Behaviour to Agent-Friendly Documentation** | [2608.20195](https://arxiv.org/abs/2608.20195) | 4 | 2 | **Decline.** Agents prefer instruction files over API docs; Miner already prefers owner-repo artifacts. |
| **AI4AI-Bench** | [2608.20318](https://arxiv.org/abs/2608.20318) | 4 | 2 | **Decline.** Recursive algorithm-design self-improvement; weight/search non-goal. |
| **Learning When to Think** | [2608.20256](https://arxiv.org/abs/2608.20256) | 3 | 2 | **Decline.** GRPO mode routing inside the model. |
| **Repo0 / Loreley** | [2608.19854](https://arxiv.org/abs/2608.19854) / [2608.19703](https://arxiv.org/abs/2608.19703) | 3 | 2 | **Decline.** Zero-to-all codegen and QD repo search; not Recertia's bounded-run loop. |

### 13.1 Optimal skill selection (score 9) — cite; packer not implemented

**Chen, Chen, Wang, Li, and Huang**, arXiv:2608.19993, 20 August 2026 **[F]**. Skills as
documents under a hard token budget. Independent top-*k* wastes tokens and can degrade
success. BPS packs a complementary set with a bicriteria \((1-1/e, 1)\) guarantee; 0.73
vs 0.20–0.52 on a contamination-controlled coding setup, 28% fewer tokens than the
strongest released router.

**Take:** independent evidence that retrieval is a *set* decision, that extras can beat
the empty-bundle control the wrong way, and that a retrieve-time packer belongs inside
`retrieve` — not as a 16th node and not as the deferred learned ranker. Design note in
[§1.12](#112-skill-set-packing-under-a-token-budget-is-retrieve-time-not-a-new-node-not-implemented).

**Decline for this change:** do not implement BPS; do not grow the installed library;
do not fit a capability model from live traffic without the ablation firewall. Enable
only after `a1` has an interval and a retrieve ablation can host the packer.

### 13.2 Phantom Gains (score 8) — confirm

**Xu, Yan, Chen, and Kechadi**, arXiv:2608.20290, 20 August 2026 **[F]**. Seven
measurement failures for item-level self-improvement ledgers, each reversing a finding
without its control. Repair: a separately measured null per statistic from baseline
replicates the study already owns; per-problem tests + FDR.

**Take:** confirms Wilson / `not established` / ablation-arm practice at transition
grain. Supports RW-M2 reporting. Design note in
[§1.11](#111-phantom-gains-every-transition-statistic-needs-a-measured-null-confirming-not-changing).

**Decline:** LoRA self-training remains a non-goal. No remaining-work item for a
transition ledger.

### 13.3 Cross-task skill transfer (score 8) — confirm

**Feng, Bijoy, Balasubramanian, and Zhou**, arXiv:2608.20274, 20 August 2026 **[F]**.
Controlled comparison: task-level vs subtask-level induction, text vs code, three
long-horizon benches, eleven models. Task-level skills often fall below the no-memory
baseline; subtask-level skills raise it. Text transfers better than code. Neither
specificity nor abstractness alone predicts success; their product (skill utility) does,
and can be scored from skill + task text before a new run.

**Take:** prefer pitfall/subtask-shaped `failure_modes` (already in the authoring prior).
Utility is an advisory retrieve signal later, next to the score floor and Zhao
faithfulness — still inside `retrieve` / Curator.

**Decline:** do not make executable code-skills the primary library; do not add a node.

### 13.4 SkillForge (score 7) — cite only

**Chen, Li, Gu, Shi, and Guan**, arXiv:2608.18933, 19 August 2026 **[F]**. Synthesizes
issues from test-covered core functions, distills entity-grounded skills. SWE-bench
Verified +5.6–5.8pp over Mini-SWE-Agent.

**Take:** SWE-specific cousin of Miner (human artifacts) and Practice (curriculum). Cite
in §2.

**Decline:** no synthetic-issue generator until Practice conversion is a measured number.

### 13.5 MemTrapBench (score 7) — cite only

**Wang et al.**, arXiv:2608.20202, 20 August 2026 **[F]**. Faithfully stored, semantically
relevant memories can still distort reasoning (fixation, belief distortion). Evaluated
memory stacks lost >10pp versus no-memory. AdaptiveMem (retrieve vs overwrite vs ignore)
is a prompt policy, not a Recertia plane.

**Take:** independent evidence that "always retrieve" is not free. Supports the
empty-bundle floor and Zhao faithfulness tests already in [§3](#3-memory-and-experiential-learning).

**Decline:** do not add AdaptiveMem as a node or a new memory plane.

### 13.6 Just outside the window (17 August)

Not this batch: **VCE-Skill** ([2608.16544](https://arxiv.org/abs/2608.16544)) — public
version-diff priors fused with trajectory evolution; Recertia already versions skills
and mines reviewer diffs. **HyperSkill** ([2608.16114](https://arxiv.org/abs/2608.16114))
— hypergraph memory; composition is already `uses` / step graphs.

