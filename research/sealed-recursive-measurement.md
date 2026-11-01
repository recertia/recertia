**Sealed Recursive Measurement: Closing the Process-Audit Gap in Recursive Language Models and Agent Harnesses**

**Abstract**  
Recursive scaffolds (RLM, RAH) externalize bulk state and permit programmatic sub-calls. Process audits show that acceptance decisions for changes to those programs are rarely computed on proposer-inaccessible data, and intermediate returns are rarely retained for re-execution. Sealed Recursive Measurement adds two runtime invariants to any such scaffold: every raw sub-call return is content-addressed before aggregation, and the acceptance gate’s fixtures and scoring code are outside the writable set of every agent. The gate re-executes on the preserved returns. This note defines the two invariants, states the claims they license, and specifies the experiment that tests them. The composition uses existing LLMs; its effect on acceptance is testable by the experiment below.

![Figure 1. Sealed Recursive Measurement architecture](srm-architecture.png)

**Figure 1.** Sealed Recursive Measurement architecture. The environment runtime may not return from aggregation until the raw sub-call return has been content-addressed. Only the gate service can read the store and the frozen hold-out. If the gate is unreachable or times out, the proposal is aborted.

**1. Gap**  
Zhang et al. and Lumer et al. supply the execution pattern. Chen et al. show full-loop process audits are uncommon. Verifier and sabotage results show self-authored scores diverge from sealed ones. What is missing is a mechanical guarantee that raw returns stay available to a judge the proposer cannot rewrite.

**2. Invariants**  
- Retention: before any aggregation the exact sub-call return is stored under the SHA-256 hash of its canonical serialization; the hash and a monotonic log index are recorded. The object remains readable by the gate until the gate has logged its decision for that proposal. Storage failure aborts the proposal.  
- Gate: receives only `(proposal_id, delta_ref, fixture_set_version)`. It checks the delta out into a fresh environment that already contains the content-addressed returns, then runs the aggregation entry point against those returns (or replays the returns directly) on a hold-out the proposer cannot modify. Returns a pre-registered observable. Scoring code and hold-out are immutable to all agents that can emit proposals.  
- Determinism: identical `(delta_ref, fixture_set_version)` produces identical observables; otherwise the gate returns `NONDETERMINISTIC`.

**Implementation sketch**  
Two processes: an environment runtime (the RLM/RAH scaffold) and a separate gate service. The runtime must successfully write the SHA-256 content-addressed return and receive the log index before it is allowed to return from aggregation. The gate service holds the only credentials that can read the content-addressed store and the hold-out; those credentials are not available inside the environment. If the gate service is unreachable or does not respond before a pre-registered timeout, the runtime aborts the proposal and records the failure; it does not proceed with an un-audited aggregation.

**3. Claims**  
Implementable with current LLMs plus content-addressable storage. When the gate re-executes on the retained raw returns, its verdict does not depend on the proposer’s aggregation. The acceptance step thereby satisfies matched re-execution on proposer-inaccessible data.

**4. Experiment**  
Attach the two invariants to an existing RLM or RAH codebase. Freeze a proposer-blind hold-out. Submit proposals that alter retrieval or aggregation until 50 have been scored by both the proposer’s internal check and the gate. The sample size targets a 95 % CI half-width ≤ 0.1 on a proportion near 0.2. Run a 10-proposal pilot first; if the pilot proportion falls outside [0.1, 0.3] or the pilot itself shows unstable gate behavior (non-determinism or repeated timeouts), recompute the required n or halt and inspect the gate before the main run.  

Primary endpoint: fraction accepted by the proposer and rejected by the gate. Kill if the 95 % CI (exact binomial) includes 0 or median added latency > 2× baseline. Secondary: gate decisions identical across two full reruns from the same starting artifacts.

**5. Limitations**  
No protection against hold-out-to-deployment shift or side channels outside the schema. Depth 1 is the practical default. Experiment specified, not run.