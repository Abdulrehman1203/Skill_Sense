# Interview Analysis B6 aggregation and authorized report delivery handoff

Implemented 18 October 2026 (Asia/Karachi). Scope: B6 only. Repository statuses, current B1–B5/Retell code, shared contract, scoring/ranking/integrity presentation and relevant tests were inspected before editing. No applicable AGENTS.md was found in inspected repositories/ancestors. Existing unrelated/uncommitted work was preserved. Frontend source/auth interfaces were not changed; its existing untracked docs remain.

## Changed files

| Files | B6 change |
|---|---|
| `interview_system/tasks/analysis.py` | Replace the existing fake aggregate task for every session version. Keep its task/import path, use real persisted aggregation and commit-deferred reference notification, remove automatic score dispatch, retry DB connection/operational errors only. |
| `interview_system/analysis_aggregation.py` | SQL-backed ordered admitted-ledger iteration, strict B5 evidence/provenance handling, unchanged pure temporal rules, supported coverage/NULL metrics, bounded source-version/recent-observation output. No model imports/inference. |
| `interview_system/analysis_reporting.py` | Source/claim fencing, final barrier/immutable final reports, revisions, candidate-safe/owner reads, byte bounds, durable coalesced notification state and recovery. |
| `interview_system/analysis_views.py`, `urls.py` | Clerk-only GET status, recruiter report and recruiter snapshot routes, explicit response serializers, fixed errors and no-store responses. |
| `interview_system/observer_delivery.py`, `consumers.py` | Authorized persisted snapshot retrieval; existing observer revision suppression plus periodic persisted-revision polling for lost wake-ups. Publisher isolation preserved. |
| `interview_system/models.py`, `migrations/0025_interview_analysis_report_delivery.py` | Session source/dirty/due fields; report schema/body/source/notification fields; partial dirty-session due index; nullable legacy look_away_count. Existing rows/indexes/identities/scores retained. |
| `interview_system/video_lifecycle.py`, `vision_scheduling.py` | Dirty reports transactionally with meaningful source transitions; reuse existing scanner/management recovery to run due report and notification batches, without per-frame report task publication. |
| `interview_system/tasks/scoring_tasks.py`, `integrity.py` | Explicit schema-one report exclusion from legacy behavioral scoring and legacy integrity-count threshold presentation. Legacy formula/weights/read behavior otherwise preserved. |
| `ai_interview_recruitment/settings.py`, `.env.example` | INTERVIEW_ANALYSIS_REPORT_RECOVERY_BATCH default 10, clamped 1..100. Existing recovery cadence/claim lease reused; vision admission remains disabled by default. |
| `interview_system/tests/test_behavioral_reporting.py` | B6 synthetic evidence, authorization, empty/partial/unsupported coverage, races, notifications/reconnect/loss, byte bounds and policy isolation tests. |
| `interview_system/tests/test_video_lifecycle.py` | Replace the old not-implemented-stub expectation with the actual empty live report/null semantics, retaining no-scoring assertions. |
| `interview_system/tests/test_frame_transport.py` | Explicit test-only B6 projection isolation in legacy B3 transport fixtures; actual persisted projection is tested in B6 integration tests. |
| `interview_system/test_phase10_admin.py` | Update the stale seven-field database-schema assertion to explicitly include intended B2/B6 fields; legacy integrity/privacy assertions remain. |
| `.gitignore` | Include B6 tests/handoff and the existing updated Phase 10 admin test in reviewable files. |
| `docs/interview_analysis_shared_contract.md`, this file | Section 17 freezes actual report/status/snapshot/recovery schemas, policy inventory and next integration contract. |

Migration 0025 is additive except allowing NULL in legacy look_away_count. It adds no identity model, auth provider, local passwords or identity migration. It does not rewrite numeric history, scores, rubric weights or ranking policy. A partial index on report_due_at includes only dirty lifecycle-version-one video sessions, so finalized/legacy rows do not accumulate in the report recovery scan. Existing session/timestamp, job identity and lifecycle recovery indexes remain. Application migrations were not applied; all migration execution occurred only in newly created disposable test databases. Index lock/runtime behavior in a production database was not certified; review/apply the pending migration chain separately before deploying these reads/workers.

## Aggregation, revisions and NULL semantics

The existing aggregate task now returns status/revision and uses job-linked stored observations. It never fabricates 94.5 attention, a fixed look-away count, confidence/neutral expressions or a fake successful percentage. Legacy version-zero sessions return unavailable/legacy_unverified; schema-zero one-to-one conflicts are preserved and return legacy_report_conflict. Neither numeric signatures nor an existing one-to-one row establishes real model provenance.

Mutations advance a source revision and a coalesced dirty flag under the B2 parent/session lock. The aggregate claims the existing finalization token/lease, captures source revision/SQL counters, releases the locks and streams the admitted ledger in chunks of 200. A changed source/expired/stolen claim cannot commit. Computation checks its lease every 200 records. Final persistence reuses the sealed/drained/count barrier and B2 finalization_write_lock; a post-write lease check rolls back late writers. A boundary mismatch fails explicitly and backs off, rather than inventing outcomes. Interview DONE stays separate and disconnect behavior is unchanged.

Live reports advance revision only for a new persisted source; repeats coalesce. Final reports are immutable to duplicate/late tasks. B2's already finalized zero-processed failure can acquire its explicit B6 failed/null report at the existing allocated revision. A future correction/reprocessing flow requires an explicit reviewed policy/migration/revision design; no silent reopening is provided.

Coverage preserves B2's actual received/admitted/drop/terminal counts and adds usable/unusable/unsupported/valid/unknown evidence and supported seconds. Missing observations, incompatible rules and legacy/invalid details are UNKNOWN boundaries, not negative detections. Failed/expired/superseded admitted records also break supported intervals. Non-monotonic server time produces an explicit failed/pipeline_failed temporal report with NULL metrics. No client capture timestamp is used for aggregation.

Gaze-centered percentage uses supported same-class gaze endpoint intervals, not sample counts, attention or ranking. Both endpoints must have valid calibrated gaze, usable compatible evidence and a gap <=1 second. Transitions, UNKNOWN and gaps are not allocated to a gaze class. Zero supported duration yields NULL, including an isolated centered sample. Valid 0/100 percentages need positive exposure and retain their denominator/coverage. The unchanged B5 episode fold applies confidence hysteresis, max gaps, duration onset/release and one stable event per episode. Counts/durations are NULL without relevant exposure; supported zero events are genuine measured zeros under those rules. Do not substitute span for observed_seconds or extrapolate to seal/disconnect.

Complete means all admitted evidence is compatible/usable, no receipt/job loss, no unknown gaze and positive supported gaze exposure. Partial identifies losses/replacement/quality/unknown/insufficient or mixed evidence. Failed identifies no usable evidence or an invalid timeline. These are computation states, not real-world accuracy certification. Every B6 report keeps validation_status=real_world_accuracy_outstanding and scoring_eligible=false. Expression remains disabled/null. New rows leave legacy attention_pct/look_away_count NULL rather than reusing those fields for approximate gaze/duration episodes.

The exact schema, status rules and capped provenance are in section 17 of the shared contract. Source version lists include unsupported versions where available, cap each list at 16 and explicitly flag truncation/mixing. At most 100 recent episode records and 100 observation records are retained. Stored report JSON is <=131072 bytes and the frozen socket snapshot is <=65536 bytes; trimming preserves complete summary counts/truncation metadata. No image, landmark mesh, calibration ratios, credential or candidate identity is emitted in report/snapshot output.

## HTTP and observer delivery contract

All new routes use the existing Clerk verifier and active account/profile ownership checks with no-store/no-cache responses:

- `GET /api/interview-sessions/{id}/analysis-status/`: owning candidate/recruiter. Candidate output is exactly session_id, video_state, connection_state, analysis_state, revision, report_ready, final, video_required, availability. Recruiter adds coverage. No candidate detector/gaze/event/integrity data.
- `GET /api/interview-sessions/{id}/behavioral-analysis/`: owning recruiter only; `{session_id,availability,revision,report}`.
- `GET /api/interview-sessions/{id}/analysis-snapshot/`: owning recruiter only; `{session_id,availability,revision,snapshot}`.

Availability is disabled, legacy_unverified, pending or ready. A ready live report need not be final or successful. Unavailable bodies are NULL/revision zero. Historical values are not returned as real B6 reports. GET/HEAD/OPTIONS only. No calibration/write route or frontend integration was added.

Observer groups remain separate from publishers. After persistence, the report row contains durable notified_revision/notify_after state. Both task and direct-service notification calls defer to the outermost commit; rollback sends nothing. Sending claims a 10-second attempt window, releases DB locks, sends only type/session/revision through Channels with a two-second wait, then acknowledges only that revision. Failure/crash leaves coalesced recoverable intent. A newer report cannot be acknowledged away by an older send. A repeated uncertain reference is suppressed by the client's/server consumer's highest-revision behavior; no exactly-once browser receipt is promised.

Subscribe-before-fetch is preserved. Wake-ups fetch the newest authorized persisted snapshot, never forward arbitrary event payloads. The observer watchdog also polls the persisted revision at the existing auth cadence (default five seconds), covering silently lost group sends for already-connected clients. Reconnect and HTTP always use persisted state. Existing bounded group/window/byte/lease limits and active-user checks remain. A group_send return value means a successful attempt, not browser delivery. No B6 inbox/email/score task is dispatched.

## Configuration and recovery

INTERVIEW_ANALYSIS_REPORT_RECOVERY_BATCH=10 (1..100) controls due reports/notification intents per existing scan. The existing recover_video task/command processes those batches after lifecycle/frame dispatch recovery. It executes the existing aggregate task run path directly on the normal worker/management process; there is no per-frame aggregate publication, new report queue or growing retry table. The durable dirty marker/claim/due fields recover abandoned computations. This path needs no GPU, vision worker or broker for calculation; Channels notification failure does not lose the report.

Typical local recovery instruction (not a deployment action performed):

```powershell
.venv/Scripts/python.exe manage.py recover_video
```

Normal live coalescing is five seconds; computation lease is the existing INTERVIEW_ANALYSIS_FINALIZATION_CLAIM_SECONDS (default 30), notification retry claim 10 seconds, group-send wait two seconds, invariant-error backoff 30 seconds. The scanner's batch count is eligible attempts, not a guarantee every report finalized. DB/normal-worker load/outage and optimistic source changes can delay live projection until quiet/seal; no production latency or worst-case CPU/RSS guarantee is claimed. HTTP/socket reads fetch bounded stored JSON rather than recalculating temporal rules in ASGI. Admission remains disabled by default and B5 staged-clip/model-readiness gates remain.

## Scoring, integrity presentation and actual local-data inventory

Inspected behavior:

- The former task used constant attention 94.5, look-away 2 and confident/neutral emotion values, then called generate_candidate_score. That emitter/dispatch is removed, including its legacy-session branch.
- `_behavioral_signal` selected the latest non-NULL attention_pct. The existing rubric converts it to clamp(attention/100 - 0.10*flag_count,0,1), applies weight_behavioral and does not rebalance missing components; missing values contribute zero weighted value. Those formulas/weights are unchanged.
- B6 reports are explicitly excluded by report_schema_version=0 in the legacy scoring reader, even if a numeric legacy field is later accidentally filled. Legacy schema-zero behavior otherwise remains. No existing CandidateScore was recalculated and no default rubric was created.
- Ranking still orders stored final_score descending/nulls-last, then application created_at/PK. Scoring still can queue score_ready if independently invoked; B6 never invokes it. Fixing legacy scoring-notification deduplication or changing missing-signal policy is a separate task, not an implied B6 approval.
- Existing read-time recruiter integrity presentation uses look-away/flag thresholds (defaults 5/1), requires frame backing and already ignores 94.5/2 and 80/0 stub-like patterns. This heuristic is preserved for legacy presentation, not treated as proof for data deletion/migration. B6 schema-one episodes are explicitly excluded from those warnings.
- Flutter review currently displays a Proctoring flags panel, reads proctoring_flags/integrity_flags and says No proctoring flags were reported when empty. B6 observations are not injected into that legacy panel. B7 needs observation/UNKNOWN/coverage presentation, not cheating labels. Candidate application serialization already omits recruiter integrity context; tests protect it.

Read-only inventory of the configured local application DB: behavioral rows **0**, frame rows **0**, candidate-score rows **0**, active rubrics **0**, numeric 94.5/2 pattern **0**, numeric 80/0 pattern **0**. The behavioral table still has its legacy seven columns; pending application migrations were not applied. Queries exposed only aggregate counts/schema/weight tuples, no identities, tokens or footage. These results describe only that inspected DB/time, not other environments or the provenance of all historical data.

Required separate policy decisions: whether any validated behavioral measure should ever affect ranking; feature validation/coverage thresholds; missing-component weighting; adjudication/consent/presentation; source-provenance audit/quarantine and any approved historical score migration. B6 authorizes none of those changes implicitly. No guessed-signature historical rewrite or synthetic production data was introduced.

## Tests and results

Every database suite uses a random new `test_interview_analysis_b6_<uuid>` PostgreSQL test DB, then destroys only that DB. Redis-related transport fixtures use random prefixes on local database 15; no FLUSHDB, application migration or user-data deletion. B6 evidence uses synthetic fixture metadata with model_version=test-only-not-real-ML; no real inference/accuracy/provider/browser/hardware verification is claimed. The existing B3 fixtures explicitly isolate the B6 seam, while B6 tests exercise actual persisted snapshot retrieval/reconnect/polling.

Final broad command:

```powershell
.venv/Scripts/python.exe -c "import os,uuid; os.environ.setdefault('DJANGO_SETTINGS_MODULE','ai_interview_recruitment.settings'); from django.conf import settings; settings.DATABASES['default']['TEST']={'NAME':'test_interview_analysis_b6_'+uuid.uuid4().hex[:12]}; import django; django.setup(); from django.core.management import call_command; call_command('test','interview_system.tests.test_behavioral_reporting','interview_system.tests.test_vision_pipeline','interview_system.tests.test_vision_scheduling','interview_system.tests.test_frame_transport','interview_system.tests.test_video_lifecycle','interview_system.tests.test_socket_authentication','interview_system.tests.test_account_roles','interview_system.tests.test_retell_lifecycle','interview_system.tests.test_phase6_scoring_task','interview_system.test_phase10_admin','interview_system.test_phase10_notifications','interview_system.test_phase10_recruiter_reads',interactive=False)"
```

Coverage includes no-frame/quality/partial/unsupported/missing-observation cases, finite-duration versus NULL percentages, gaps/episode replay, pending/sealed barrier, source/lease/parallel/late-result fencing, repeat revisions/finalization, signed local RSA Clerk verification, candidates/owners/other recruiters/deactivation, no-store/method restrictions, byte/window limits, failed/missed notifications, actual persisted reconnect/polling, commit rollback, explicit schema/policy isolation and zero B6 scoring dispatch. Existing Phase 10 tests protect stored scores/ranking/integrity presentation and notification privacy.

Intermediate results: initial B6 **28 tests / 23.950s / OK**. Expanded **285 tests / 115.073s** had one failure: the Phase 10 schema test still expected only seven legacy database fields, already incompatible with intended Interview Analysis provenance fields. It was updated to an exact explicit set including B2/B6 fields, not skipped or weakened. Next expanded run: **287 tests / 121.380s / OK**. Final-status focused run: **34 tests / 30.468s / OK**. Recovery-backoff/missing-observation focused run: **35 tests / 30.275s / OK**.

Final broad result: **289 tests / 121.341s / OK**, exit code 0, including all **36 B6 tests** and the direct-service outer-commit test. The disposable database was destroyed successfully. Full output is retained locally in the ignored `.interview-analysis-b6-test-result.log`; the exact command above is reproducible without that log. The five new B6 Python files also passed UTF-8 decoding and AST syntax validation.

Read-only/static commands:

```powershell
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run --noinput
.venv/Scripts/python.exe manage.py check
git diff --check
```

Migration consistency: No changes detected. Django: no issues (0 silenced). Whitespace check passed; Git emits only existing LF-to-CRLF conversion advisories for edited files. Initial migration generation encountered sandbox-denied DB history access but generated the additive file; approved read-only consistency access then succeeded. No application migration was run. No real clip/model/GPU/browser/provider or production deployment/throughput test was performed by B6.

## Next milestone: B7

Integrate the existing Clerk ticket/lease flow, v1 JSON/base64 capture/backpressure, explicit finish/reconnect semantics and these exact role-filtered read APIs. Apply only the highest snapshot revision and replace bounded windows; reconnect/status/report retrieval is authoritative after a missed notification. Keep candidate UI transport/availability-only, recruiter UI explicit about approximate gaze, supported/UNKNOWN coverage, disabled expression and unvalidated observations. B7 must implement the explicit per-session target-calibration capture/confirmation flow rather than assume initial frames are centered. Keep any rank/integrity policy decision separate. Consented staged webcam accuracy validation, target worker/Redis/native-runtime/privacy/performance verification and any legacy audit/data migration remain outstanding separate gates; B6 did not deploy or enable admission.
