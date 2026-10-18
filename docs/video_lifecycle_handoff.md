# Interview Analysis B2 persistence and lifecycle handoff

Completed 17 October 2026. Implements metadata/lifecycle only; [shared contract section 13](interview_analysis_shared_contract.md#13-b2-persistence-and-lifecycle-implementation) is the implementation reference. Existing B1, Retell and frontend uncommitted work were retained. Git status/code/contracts were inspected first; no applicable AGENTS.md was found in the repositories or inspected ancestors. No frontend file, authentication provider, rubric, score, ranking policy or identity model was changed by B2.

## B2 changed files

| File | Change |
|---|---|
| `interview_system/models.py` | Managed lifecycle marker; separate connection/video/analysis states, generation, leases/deadlines, seal/boundary/counters, finalization claims/provenance; FrameJob and admission gate; optional one-to-one job observation link. Existing audio fields, report relationship and frame timestamp index retained. |
| `interview_system/migrations/0022_interview_analysis_video_lifecycle.py` | Additive persistence/constraints/indexes; existing sessions receive version 0, future rows default to version 1; no historical adoption or identity migration. Depends on `0021` and earlier uncommitted Retell migration. |
| `interview_system/video_lifecycle.py` | Parent/session locking; generations and transport ownership; recoverable disconnects; finish/seal/barrier; bounded metadata admission; worker claims and terminal writes; aggregate claim/write guard; inbox/deadline recovery; async-safe transport wrappers. |
| `interview_system/video_views.py`, `interview_system/urls.py` | Clerk-authenticated candidate finish route; exact empty-object request, idempotent reason/boundary, no-store and fixed errors. |
| `interview_system/socket_auth.py` | Publisher eligibility rejects legacy, sealed/finalizing/ended sessions. |
| `interview_system/session_completion.py` | Explicit-finish compatibility hook delegates managed video to sealing; disconnect docstring corrected. Audio-only coordinator retained. |
| `interview_system/voice.py`, `interview_system/voice_views.py` | Initialize managed video timers after voice start/reconciliation; canonical verified audio application seals video. |
| `interview_system/tasks/interview_tasks.py` | Preserve registered transcript task/import/retry policy while sealing managed video after audio persistence. |
| `interview_system/tasks/analysis.py` | Prevent existing fake aggregate/scoring dispatch for managed sessions; real aggregation stays B6. No legacy data rewrite. |
| `interview_system/tasks/video.py`, `interview_system/tasks/__init__.py` | Register bounded recovery task intentionally. |
| `interview_system/management/commands/recover_video.py` | Direct database-only bounded recovery pass. |
| `ai_interview_recruitment/settings.py`, `.env.example` | Documented B2 settings and expiring 5-second Beat scan. |
| `interview_system/tests/test_video_lifecycle.py` | 38 state/security/recovery/concurrency tests. All observations/provider events are explicitly test-only. |
| `interview_system/tests/test_retell_lifecycle.py` | Update one audio-first assertion: managed verified audio completion now seals video and reaches DONE without browser finish. |
| `.gitignore` | Keep B2 test/handoff reviewable. |
| `docs/interview_analysis_shared_contract.md`, `docs/video_lifecycle_handoff.md` | Actual schemas, state ownership, services, counters, limits, handoff and verification record. |

## Migration and configuration

`0022` was applied only in newly created disposable PostgreSQL test databases. It has **not** been applied to the application database. No migration reset, user-data deletion, deployment or cutover was performed. Existing sessions are not silently enrolled; the migration adds the lifecycle marker with 0, then changes the new-row Python default to 1. Historical frames have no fabricated job link, and historical reports retain their original values with blank/zero provenance. Recovery skips version-0 sessions. A forward adoption policy is a separate decision.

| B2 setting | Default |
|---|---:|
| `INTERVIEW_ANALYSIS_CONNECTION_LEASE_SECONDS` | 15 |
| `INTERVIEW_ANALYSIS_RECONNECT_GRACE_SECONDS` | 30 |
| `INTERVIEW_ANALYSIS_VIDEO_INACTIVITY_SECONDS` | 30 |
| `INTERVIEW_ANALYSIS_MAX_VIDEO_SECONDS` | 3600 |
| `INTERVIEW_ANALYSIS_VIDEO_RECOVERY_SECONDS` | 5 |
| `INTERVIEW_ANALYSIS_VIDEO_RECOVERY_BATCH` | 100 |
| `INTERVIEW_ANALYSIS_GLOBAL_FRAME_CAP` | 32 |
| `INTERVIEW_ANALYSIS_FRAME_DEADLINE_SECONDS` | 60 |
| `INTERVIEW_ANALYSIS_FRAME_CLAIM_SECONDS` | 30 |
| `INTERVIEW_ANALYSIS_FINALIZATION_CLAIM_SECONDS` | 30 |

Counts/durations clamp to at least one. Existing B1 ticket/auth lease settings are unchanged. Absolute video deadline is persisted from original activation; heartbeat and reconnect do not extend it or inactivity. Transport deadline never extends beyond B1 credential authorization. No independent OS scheduler was installed. Beat registration is configuration, not proof of a running worker/Beat service.

Normal recovery uses the registered `interview_system.tasks.video.recover_video` every five seconds; stale scan messages expire. During broker problems, an authorized operator/external scheduler can invoke:

```powershell
.venv\Scripts\python.exe manage.py recover_video
```

Each invocation processes at most the configured batch and consumes already verified durable inbox events via the canonical service, without provider or broker calls. With a backlog, run more frequently or increase provisioned capacity; no fixed latency is claimed through DB/worker/broker outages. The command was tested on fixtures, not run against application sessions.

## Exact tests and checks

Environment: Windows repository `.venv`, Django **6.0.6**, Channels **4.3.2**, Celery **5.6.3**, PyJWT **2.9.0**, real local PostgreSQL. Requirements still pin PyJWT **2.10.1**, which was not installed/tested in this run. No dependency change or package installation occurred. Official Django/PostgreSQL locking and Celery periodic-task behavior were checked; references are linked in the shared contract.

Final test invocation used a random test database name, avoiding reuse/recreation of any preexisting database:

```powershell
.venv\Scripts\python.exe -c "import os, uuid; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_interview_recruitment.settings'); from django.conf import settings; settings.DATABASES['default']['TEST'] = {'NAME': 'test_interview_analysis_b2_' + uuid.uuid4().hex[:12]}; import django; django.setup(); from django.core.management import call_command; call_command('test', 'interview_system.tests.test_video_lifecycle', 'interview_system.tests.test_socket_authentication', 'interview_system.tests.test_account_roles', 'interview_system.tests.test_retell_lifecycle', 'interview_system.tests.test_phase6_scoring_task', interactive=False)"
```

**109 tests in 14.216 seconds: OK, zero failures/errors/skips.** B2 contributes 38 tests; B1/Clerk/Retell contribute 64 and unchanged scoring tests contribute 7. PostgreSQL concurrency cases actually executed. The runner created a new randomly named test database, applied migrations there and removed only that disposable database afterward. Application migrations/data were untouched.

Earlier successful iterations: 93 tests in 11.047 seconds; then 107 tests in 13.524 seconds after extra race/registered-worker tests and scoring regressions; then 108 tests in 13.293 seconds including verified inbox recovery without per-event broker delivery. The final added case proves that a newer credential cannot extend a generation's persisted original authorization deadline. No failed test iteration occurred in B2.

Final additional checks:

```powershell
.venv\Scripts\python.exe manage.py makemigrations --check --dry-run --noinput
.venv\Scripts\python.exe manage.py check
git diff --check
```

Results respectively: **No changes detected** (including read-only migration consistency lookup with PostgreSQL access); **System check identified no issues (0 silenced)**; **no whitespace errors**. An initial sandboxed migration check reported blocked PostgreSQL access and was repeated successfully with local database access. No application migration was applied. Frontend status remained its preexisting untracked docs only.

Covered gates:

- Audio-first seals directly, video-first waits for audio, audio-only preserves completion timestamps; report readiness is independent of DONE.
- Disconnect stays recoverable; stale disconnect/heartbeat/admission cannot mutate the new generation. Transport expiry permits only policy-bounded takeover.
- Explicit finish is candidate-owned, authenticated, strict JSON, no-store and idempotent. Reason, first seal timestamp and admission count remain immutable while status may advance.
- No-frame sessions terminate with no usable evidence and no fabricated observation/report. Worker loss/expired jobs drain explicitly.
- Heartbeats do not refresh frame inactivity/duration; recovery handles lost transport, elapsed grace, inactivity, duration, persisted audio and already verified inbox events.
- Identity retry is idempotent; pending replacement, global capacity drops and worker retries do not double-count. Pending metadata is never an observation.
- Worker claim rotation rejects late writes; persisted observations have one job and explicit test-only provenance. Already admitted work may finish after reconnect/seal.
- Concurrent finish, admission/seal, worker settlement/seal, audio/video completion, worker claims, aggregate claims, global capacity across sessions and stale disconnect/takeover pass under PostgreSQL.
- Aggregate write context rejects expired/replaced claims and enforces the sealed/drained barrier. Actual measured aggregation is not implemented or claimed.
- Managed sessions cannot invoke the old fake aggregate/scoring dispatch. Historical aggregates/frames are not adopted or rewritten. Existing scoring-policy tests pass unchanged.

## Remaining limits and B3/B4 handoff

No B3 consumers/ASGI routing, browser/camera, real Clerk/Retell provider, Redis transport, image payload storage, broker frame-publication recovery, inference, GPU benchmark, proxy redaction or production scheduler was verified. All test results/observations are local fixtures, never real ML output. There is no new raw-frame storage and no accepted-frame socket path yet. Existing historical fake aggregation code remains for version-0 behavior until B6; B2 never dispatches it and blocks it for all managed sessions. Existing score/ranking read policy was not changed.

`admit_frame_async` is metadata-only. B3 must first implement frozen origin/schema/image/rate checks, then integrate this service with B4's private payload and durable publication contract before acknowledging production frames as accepted. Payload keys may derive from job UUID; no image/JWT/ticket belongs in task args or persisted job metadata. B4 must recover publication and claim loss without replacing this ledger or counting retries as new admissions. B2 already bounds nonterminal metadata to one processing plus one pending per session and the configured global cap; retained terminal history/deletion policy still requires B7/product retention decisions.

B3 must redeem a fresh B1 ticket before accept, acquire the publisher generation, retain its UUID in the trusted consumer, use the async wrappers, call heartbeat checks, and invoke fenced disconnect cleanup on transport/auth loss. Never call `mark_video_ended` on disconnect. Observers get separate B1 leases/groups, no publisher membership or lifecycle ownership, and no inference runs in ASGI. B1 auth watchdog/origin/TLS/log-redaction requirements remain mandatory and unverified until integrated. Internal `stale_connection` is now a frozen post-accept 4409 reason; stale aggregate claims remain internal.

B6 must compute a real report only after `analysis_ready_at`, acquire `claim_finalization`, and commit provenance, revision, coverage and final timestamp inside `finalization_write_lock`. Sessions with processed observations deliberately remain finalizing awaiting that implementation; B2 cannot truthfully mark them complete/partial. Zero-processed sessions already have explicit failed/unavailable finalization. No automatic score dispatch or historical score rewrite is authorized by these lifecycle services.
