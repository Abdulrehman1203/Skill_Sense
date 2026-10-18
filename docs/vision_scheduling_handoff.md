# Interview Analysis B4 bounded scheduling and recovery handoff

Implemented 18 October 2026 (Asia/Karachi). Scope: B4 only, on top of the existing uncommitted B1–B3/Retell work. Both repository statuses and current code were inspected. No applicable AGENTS.md was found. The Downloads attachment was read as proposal/reference material; the actual repository shared contract and the user's B4 scope guided implementation. No frontend source, Clerk provisioning/authentication, scores, rubrics or ranking policy was changed.

## Changed files in B4

| File | Change |
|---|---|
| `interview_system/frame_admission.py` | Replace the B3 placeholder with opt-in durable admission/private payload retention, off the event loop; no broker or inference in ASGI. |
| `interview_system/video_lifecycle.py` | Reuse admission locking/counters with a transactional retention hook, server receive deadlines, per-session ledger cap and B4 recovery dispatch after B2 lifecycle/deadline recovery; use one fresh timestamp after credential checks so an equal auth/transport expiry boundary has a consistent reason. |
| `interview_system/models.py` | Four FrameJob publication/payload intent fields; no identity/auth model change. |
| `interview_system/migrations/0023_interview_analysis_frame_scheduling.py` | Add payload_ready, publication_token, publication_attempts and publication_due_at, safely defaulting existing rows to no retained payload. |
| `interview_system/vision_payloads.py` | Atomic globally capped private Redis payloads and slot index, strict TTL, bounded network attempts and best-effort deletion. |
| `interview_system/vision_transport.py` | Pinned Kombu Redis transport; atomic bounded publication/restoration and message-byte cap for vision only. |
| `interview_system/vision_scheduling.py` | Durable outbox fields on the existing job, bounded publication/claim attempts, token/lease fencing, idempotent observation settlement and explicit failure recovery. |
| `interview_system/vision_inference.py` | B5 boundary that raises unavailable; no real or fake production ML implementation. |
| `interview_system/tasks/vision.py`, `tasks/__init__.py` | Explicit reference-only vision task registration with early acknowledgement, no Celery retries and execution limits. |
| `interview_system/tasks/video.py`, `management/commands/recover_video.py` | Describe extended lifecycle/dispatch recovery accurately; existing periodic/command recovery now supplies B4 liveness. |
| `ai_interview_recruitment/vision_worker.py` | Explicit vision-only subscription, concurrency one, prefetch disabled, prefork execution and no remote control. |
| `ai_interview_recruitment/settings.py`, `.env.example` | Opt-in admission, payload/list/session caps, Redis transport/routing and bounded broker socket/connect options. |
| `requirements.txt` | Pin installed Kombu 5.6.2, whose transport hooks are version-sensitive. No new auth dependency. |
| `interview_system/tests/test_vision_scheduling.py` | B4 deterministic TEST-ONLY inference, real PostgreSQL/Redis, concurrency/failure/load tests and actual Celery solo worker. |
| `interview_system/tests/test_frame_transport.py` | Enabled B4 admission test; isolate private keys and explicitly disable B4 in existing B3-only fixtures/wire subprocess. |
| `.gitignore` | Include B4 test/handoff in reviewable files. |
| `docs/interview_analysis_shared_contract.md`, this file | Actual B4 choices, internal reason/limit freeze, exact verification and B5 boundary. |

No application migration was applied. Migration 0023 was applied only to newly created disposable test databases. Existing schema/data, historical observations/reports, original session/timestamp index, audio fields and parent-row lock order are preserved. No reset, destructive cutover, deployment, persistent worker or Redis-server configuration change was performed.

## Configuration and run contract

B4 ships with `INTERVIEW_ANALYSIS_VISION_ENABLED=False`. Existing sockets remain usable and return dropped/capacity until admission is deliberately enabled. B5 is not implemented: enabling admission now exercises real scheduling but ends inference with inference_failed; no synthetic production result is available. Keep `CELERY_TASK_ALWAYS_EAGER=False`; the dispatcher refuses eager execution even if an environment attempts to enable it.

Defaults are 32 active jobs globally, one processing and one latest pending per session, 64 private payload slots, 128 vision broker messages and 10800 admitted ledger rows per session. Raw bytes are at most 64 * 204800 = 13107200 bytes (12.5 MiB), plus Redis overhead; the list holds at most 128 * 8192 = 1048576 serialized bytes (1 MiB), plus Redis overhead. Worker reservation/model/decoded-runtime memory is additional. These are configured logical bounds, not measured system memory or performance. Terminal job history is bounded per session, not across all sessions ever stored. No retention deletion was added.

`INTERVIEW_ANALYSIS_PAYLOAD_REDIS_URL` defaults to localhost database 14 only for development. Production needs a dedicated private, non-persistent Redis primary with no raw-image backup/AOF/snapshots, access controls and appropriate network/TLS policy. Database-number separation alone does not establish privacy. The implementation did not certify or alter a server's persistence/ACL/deployment configuration. Payloads have independent TTLs, so broker outage does not extend image retention. Redis Cluster is not supported by these multi-key scripts.

`INTERVIEW_ANALYSIS_VISION_QUEUE` defaults to `interview.analysis.vision`; all publishers/workers must retain `CELERY_BROKER_TRANSPORT=interview_system.vision_transport:Transport`. The configured producer/restorer caps that queue atomically and rejects nonzero priority or serialized messages over 8192 bytes. Avoid outside producers that bypass the transport. Normal queues retain upstream transport behavior. Kombu is now pinned to 5.6.2 alongside Celery 5.6.3 and redis-py 8.0.1.

Run the dedicated production worker in a supported Linux environment with the project dependencies/environment (commands are instructions, not deployment actions performed):

```bash
python -m ai_interview_recruitment.vision_worker
```

The entry point subscribes only to the configured vision queue, uses prefork/concurrency one, disables Redis prefetch, sets multiplier one and disables remote control/gossip/mingle. Task soft/hard limits are 20/25 seconds and claim lease is 30 seconds by default. Keep the claim longer than the hard runtime bound. Production prefork/time limits and native/GPU memory need validation in the target environment. Celery's prefork deployment is not verified on this Windows host. The integration test uses a solo worker, explicitly test-only inference and an isolated queue.

Keep a separate normal worker subscribed to `celery` (not vision) and beat, for existing recovery/completion/aggregation work:

```bash
python -m celery -A ai_interview_recruitment worker -Q celery --loglevel=WARNING
python -m celery -A ai_interview_recruitment beat --loglevel=WARNING
```

The existing recovery period is 5 seconds, bounded due-session batch 100. For operator-triggered recovery without depending on beat, run:

```powershell
.venv\Scripts\python.exe manage.py recover_video
```

This command can attempt broker publication, but broker failure is persisted/recoverable and DB deadline expiry still works. Default-queue aggregation does not require GPU. Existing default-queue latency under unrelated CPU tasks was not measured; isolate control/notification work if measurements require it. The B3 bounded Daphne entry point remains required for sockets.

## Recovery and terminal semantics

An admitted job records a retained-payload flag, publication token/attempt count and next publication deadline. The scanner commits its intent before reference-only Celery publication. Publication failure, unknown success and publisher death all retry on a new token, at most three attempts, with a 10-second publication lease and no hidden publish retry. A live worker claim blocks another claim. Early acknowledgement is intentional: worker death recovers through the database lease, not a Redis visibility-timeout/redelivery loop. Claims are capped at two; old tokens/expired claims cannot write observations or retry mutations. New connection generations cannot admit under old ownership; already admitted immutable jobs can complete after reconnect or seal.

Transient payload Redis failures and explicitly typed local transient inference errors get bounded retry, with two-second backoff eligible on the next scan. Missing payload is immediately payload_missing. Corrupt/unavailable/invalid inference is inference_failed without retry. Exhausted worker attempts are worker_failed, exhausted publications are publication_failed, and expired jobs are deadline_expired with outcome expired. Superseded pending jobs are terminally recorded once. A failed raw-payload cleanup can leave a capped TTL orphan. No error messages include credentials or frame bytes.

Received/admitted/dropped counters remain B2's definitions. A storage failure rolls back replacement/admission, and B3 records the transport drop exactly once. Replacement and retries cannot double-count admissions. Queue order is never a finalization proof. Finish-video seals the ledger and can mark interview DONE independently; all admitted jobs must be terminal before analysis_ready_at. No usable processed evidence yields explicit unavailable/failed analysis, not invented numbers. Real aggregate creation remains B6.

## Verification

Tests always create a random new PostgreSQL database and destroy only that test database. Redis uses database 15 and random per-test prefixes/queue names; cleanup deletes only test-created keys/bindings. No FLUSHDB, application migration or user-data deletion occurred. Test worker and wire-server processes/threads are stopped by fixture cleanup.

Exact final suite invocation (PowerShell; the random name prevents collisions with any existing test database):

```powershell
.venv/Scripts/python.exe -c "import os,uuid; os.environ.setdefault('DJANGO_SETTINGS_MODULE','ai_interview_recruitment.settings'); from django.conf import settings; settings.DATABASES['default']['TEST']={'NAME':'test_interview_analysis_b4_'+uuid.uuid4().hex[:12]}; import django; django.setup(); from django.core.management import call_command; call_command('test','interview_system.tests.test_vision_scheduling','interview_system.tests.test_frame_transport','interview_system.tests.test_video_lifecycle','interview_system.tests.test_socket_authentication','interview_system.tests.test_account_roles','interview_system.tests.test_retell_lifecycle','interview_system.tests.test_phase6_scoring_task',interactive=False)"
```

The suite includes 29 B4 scheduling tests, 35 transport tests and 109 existing lifecycle/Clerk/account/Retell/scoring regressions. B4 assertions cover concurrent producers/replacement/dispatch, active and lifetime-ledger caps, rollback orphans, concurrent Redis payload saturation, concurrent capped broker publication/restoration, duplicate and stale deliveries, early-acked worker loss/abrupt exit simulation, bounded transient retry, permanent corrupt/missing/expired payloads, broker outage/publication exhaustion, caller disappearance, eager-mode refusal, finish with pending jobs, and the real enabled socket path without inline inference.

Synthetic overload admits at most 100 configured test ledger rows across 120 attempts, with 20 recorded capacity drops, one latest pending job and at most one surviving raw payload. Separate 40-producer Redis tests with cap three retain exactly three payloads or broker messages. A real isolated Celery worker consumes the reference task and persists one observation marked `test-only`; that is test infrastructure output, not real model output. Physical prefork process kills, provider/browser camera, GPU, production throughput/RSS and deployed Redis privacy were not verified.

Intermediate runs: first B4-only run had 21 tests in 23.965 seconds with one task-app isolation assertion failure; this was corrected. The next broad run passed 164 tests in 75.995 seconds. A 173-test run in 81.090 seconds found one eager-mode test error from patching a Celery mapping as an attribute; it was replaced with the real Django configuration override and the production guard checks both Django and Celery settings. No failed assertion was skipped or weakened.

A subsequent 173-test run in 80.547 seconds exposed an existing auth/transport deadline boundary race: an idle socket stopped as required but could report stale_connection after credential validation crossed the equal credential/connection deadline. `_publisher` now refreshes one timestamp after account/ownership checks and checks authorization expiry first, preserving the original lease_expired assertion and policy.

**Final full-suite result: 173 tests in 81.197 seconds, OK, zero failures/errors/skips.** This includes all 29 B4 tests and the 35 transport tests. A final focused real-Redis publication/restoration test added explicit oversize-restoration rejection and passed: **1 test in 0.741 seconds, OK**. Its exact invocation is the same disposable-database wrapper above with only `interview_system.tests.test_vision_scheduling.SchedulingTests.test_real_broker_list_and_restoration_are_atomically_bounded` as the test label. No additional inference or hardware verification is implied.

Static/consistency commands:

```powershell
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run --noinput
.venv/Scripts/python.exe manage.py check
git diff --check
```

Migration consistency returned `No changes detected`; Django reported `System check identified no issues (0 silenced)`; whitespace check passed. Initial migration generation encountered sandbox-denied DB history access but generated the additive file; the subsequent read-only consistency check used approved local DB access successfully. Application migrations were not applied.

## Next milestone: B5

Implement real private local inference at `vision_inference.analyze_jpeg(data) -> (observation, versions)` within the vision worker. Load model/GPU dependencies lazily inside that function, so default-queue recovery importing exception/interface definitions never needs GPU access. Preserve B2 exact output/provenance, explicitly extend quality/unknown semantics before schema changes, and classify only known transient local resource errors as retryable. Measure runtime/model memory against claim/deadline/cap policies before enabling real admission. Keep raw bytes transient, no hosted image API/ASGI inference/production fake backend, and no automatic cheating/ranking verdict. B6 later creates real aggregates/reports after the sealed ledger drains, without GPU dependency or queue-order assumptions. Section 15 of the shared contract is the authoritative B4-to-B5 interface.
