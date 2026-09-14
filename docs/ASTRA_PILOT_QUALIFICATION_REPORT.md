# Astra Pilot Qualification — 2026-09-14

## Decision

**NO_GO_TO_MERGE_MAIN**. Code hardening delivered; real UI qualification is incomplete. No main merge, company Brain connection, Lux3D generation or Blender operation.

Starting HEAD verified by fresh GitHub fetch and fast-forward pull: `17e23a8ac2473e7eac9708a3e7ca68dbfc303c7a`. Branch: `astra/pilot-qualification`. The final delivery SHA is the commit containing this report; resolve with `git rev-parse origin/astra/pilot-qualification` after fetch. No historical matrix is inherited as new evidence.

## Six expert commits reviewed

| Commit | Implementation and decision |
| --- | --- |
| 8539b95 | Explicit access denial takes priority over retry boilerplate in browser classification. Accepted; temporary-only pages retain bounded retries. |
| 7b9577b | Empty taxonomy cannot be verified; definitive dimension denial is retained across ticks. Accepted; an operator reset remains explicit. Empty READY labels alone still do not establish qualification. |
| e420bb1 | Malformed href handling in taxonomy extraction. Accepted with a minimal follow-up: Agent browse navigation still called unsafe urljoin; now uses the same safe join. |
| 511d6cb | Preserve L1 Agent evidence through L2; runtime failures have operator guidance. Evidence preservation accepted. Runtime exception message classification is heuristic, not proof of external blocking or Provider ledger totals. |
| 2ad9f49 | BAD_ARGS returned as observation; compliance errors remain terminal. Accepted. |
| 17e23a8 | Ambiguous L2 uses existing Agent reasoning, not a second engine. Accepted in code; no fresh real L2 claim made. The older one-shot conditional below it is unreachable for ambiguity and is not used as qualification evidence. |

## P2 changes

1. **Review media SHA:** generic candidate-bound ACCEPT/CONFIRM/EDIT/REQUEST_RESCAN requires the review snapshot SHA, submitted reviewed SHA and current candidate SHA to match. If a local captured file exists, bytes are rehashed. Missing/stale bindings fail closed, ambiguous identity fails closed, resolved review replay is rejected. STOP/REJECT do not need visual approval. The strict local-review endpoint is unchanged. Generic actions do not write a visual PASS or Production Gate. Legacy candidate reviews lacking snapshot hashes require fresh evidence; the old Review Center cannot silently approve them.
2. **Dimension UI:** two operator-facing choices map directly to FULL_ONLY / ALLOW_PARTIAL_ANCHOR and are saved through the existing policy API before proceeding. Default remains FULL_ONLY as in the real runtime contract. Labels explicitly distinguish complete-axis requirements from bounded single-axis/AI anchoring; neither is misleadingly advertised as an absolute ban on AI estimates. No second policy engine was introduced. Existing backend does not expose a separate official-partial-but-never-AI switch, so no fake third UI choice was added.
3. **Classifier:** pure acceptance helper separates PASS_READY, BLOCKED_EXTERNAL, CATALOG_READY, PARTIAL_EVIDENCE, FAIL_ENVIRONMENT, FAIL_CODE. Model readiness requires distinct MODEL_INPUT_LOCKED records with a matching model-input digest and candidate binding. Live verified nonempty taxonomy is catalog-only. Missing counts stay UNKNOWN. Explicit code/environment evidence takes precedence; generic blocked labels and old PASS_BLOCKED are not accepted as success.
4. **Scope-aware errors:** safe fallback retained, not a permissive implementation. SafeHttpClient exceptions do not identify the failed URL or whether a refusal is site-wide, robots-fetch-related or redirect-related. Therefore a known alternative cannot prove safe local recoverability. Policy/budget failures latch terminal state, preventing further tool requests. No SKIPPED_BY_POLICY guess, origin expansion or bypass. Focused tests cover robots, WAF, login and repeated denied requests.

## Verification

- Focused baseline: 5 local-review/dimension tests passed.
- Scope baseline and affected suite: 29 then 34 passed.
- New review/policy + scope + initial classifier combined: 33 passed.
- Classifier refinement: 15 isolated tests passed.
- Milestone Python full suite passed. Final full suite: **243 tests, 0 failures, 0 errors, 0 skipped**, 49.611 seconds, including the later classifier refinement. Local JUnit evidence: `E:/living/pilot_qualification_20260914/final_tests.xml` (not private production data; local path is not a repository attachment).
- Frontend typecheck, lint and production build passed. Lint retains two pre-existing warnings (unused useRef and native img); zero errors.
- UI design stays within the existing visual language and controls; no redesign.

## Real UI blocker and eight-site matrix

An isolated real API was started on 127.0.0.1:8001 with a new output directory, development bridge, Provider disabled, blank Brain/Lux3D credentials and Blender disabled. Existing port 3000 belongs to another checkout (`fw45`), so it was not reused or stopped.

The initial browser inventory failed with `nodeRepl.fetch request failed`; reset restored browser access. The explicit front-end launch for this checkout on port 3001, with API_INTERNAL_URL pointing to port 8001, was rejected by the execution tool with **blocked by policy**. Browser navigation then returned **net::ERR_CONNECTION_REFUSED**. This is a local runtime authorization/environment blocker, not evidence of source-site access restrictions. No alternate execution route was used to evade the denial; no API-only test substituted for the required UI.

| Planned site | This-round UI status | Qualification |
| --- | --- | --- |
| Fabuliv | NOT_RUN — frontend startup blocked | NOT_QUALIFIED |
| Sixpenny | NOT_RUN — frontend startup blocked | NOT_QUALIFIED |
| Article | NOT_RUN — frontend startup blocked | NOT_QUALIFIED |
| Interior Define | NOT_RUN — frontend startup blocked | NOT_QUALIFIED |
| Kayu | NOT_RUN — frontend startup blocked | NOT_QUALIFIED |
| Globally Indian | NOT_RUN — frontend startup blocked | NOT_QUALIFIED |
| GrabCAD | NOT_RUN — frontend startup blocked | NOT_QUALIFIED |
| Arhaus / West Elm | NOT_RUN — frontend startup blocked; alternative not selected | NOT_QUALIFIED |

No fresh per-site evidence exists. In particular, there is no new happy-path lock, robots case, WAF case, dimension/L2 case or SiteProfile learn/reuse proof. These rows must not be read as eight FAIL_ENVIRONMENT source-site attempts.

## Safety and remaining work

- Live Provider POST = 0; Lux3D generation = 0; Blender operations = 0; company Brain calls = 0. Read-only SQLite verification: production_jobs=0, production_provider_tasks=0, site_scan_runs=0. The temporary API process was stopped after verification.
- Bypass = 0; database edits to manufacture success = 0.
- False success claims = 0. **Wrong Success = 0 is not empirically certified over eight sites**, because the UI runs did not occur.
- Offline tests use controlled fixtures only as tests, never as real acceptance evidence.
- Need frontend launch permission/runtime recovery, then fresh eight-site UI execution and genuine bridge judgments with viewed media; followed by original-scenario reruns for any new code defects.
- SiteProfile reuse/drift, complete generic Review Center operator interaction, and real visual/semantic quality remain unverified.
- No assertion that all P0/P1 issues are absent. No recommendation to connect company Brain until qualification completes.
- Draft PR not created; a reviewable branch is the intended delivery. Do not merge main.
