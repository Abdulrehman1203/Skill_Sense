# Interview Analysis B3 secure transport handoff

Completed **18 October 2026** (Asia/Karachi). Scope: secure Channels transport using B1/B2. No inference, production frame scheduling/storage, deployment, migration reset, user-data deletion, historical score/ranking change, or frontend auth-interface change.

## Changed files in B3

| File | Change |
|---|---|
| `ai_interview_recruitment/asgi.py` | Preserve Django HTTP; route publisher/observer WebSockets through strict security entry middleware. |
| `ai_interview_recruitment/socket_server.py` | Pinned Daphne entry point with bounded WebSocket ASGI input queue and explicit frame/message defaults; upstream HTTP path preserved. |
| `ai_interview_recruitment/settings.py`, `.env.example` | Redis channel layer and limits/origin/guard/decoder configuration; no in-memory production fallback. |
| `requirements.txt` | Pin `channels-redis==4.3.0` and `websockets==16.1.1` wire-test client (compatible with existing google-genai constraint). |
| `interview_system/routing.py`, `interview_system/consumers.py` | Distinct authorized consumers, pre-accept admission, independent watchdog, bounded transport feedback and fenced cleanup. |
| `interview_system/socket_security.py` | Strict origin/host/query/route checks; credential scope scrubbing. |
| `interview_system/socket_db.py` | Async-safe existing B1/B2 DB calls with statement/lock budgets. No duplicate JWT verifier. |
| `interview_system/socket_guard.py` | Atomic shared Redis connection reservations and message/frame rate limits with TTLs. |
| `interview_system/frame_validation.py` | Exact JSON/schema, finite values, strict base64/JPEG/dimension checks; bounded no-wait decoder pool. |
| `interview_system/frame_admission.py` | B4 async integration seam; production default drops capacity and never pretends metadata-only acceptance is processing. |
| `interview_system/observer_delivery.py` | B6 persisted-snapshot seam and strict bounded projection validation; production seam returns no observations. |
| `interview_system/video_lifecycle.py` | Add fenced idempotent transport-drop counter service, without jobs or inactivity renewal. |
| `interview_system/tests/test_frame_transport.py` | 34 B3 tests: synthetic JPEGs, explicitly test-only admission/snapshot fixtures, real Redis, communicators and actual localhost Daphne wire traffic. |
| `.gitignore` | Keep B3 tests/handoff reviewable. |
| `docs/interview_analysis_shared_contract.md`, `docs/frame_transport_handoff.md` | Actual implemented contracts, resource bounds, next interfaces and this record. |

Both repository statuses and current code were inspected before editing. No applicable AGENTS.md was found in the repositories or inspected ancestors. Existing B1/B2/Retell uncommitted work was retained; frontend status remains its existing untracked docs. No B3 schema migration is needed, and no application migration was applied. Existing B1/B2 migrations remain as found.

## Configuration / local run contract

Use the bounded entry point:

```powershell
.venv\Scripts\python.exe -m ai_interview_recruitment.socket_server -b 127.0.0.1 -p 8000 ai_interview_recruitment.asgi:application
```

This command was used only in an ephemeral localhost test process, which was stopped afterward. No persistent service or deployment was started. A generic Daphne invocation does not include the new queue bound. Pinned Daphne supports `--websocket-max-message-size` and `--websocket-max-frame-size`; both default to 307200 here. They are forwarded to Autobahn, while application limits independently remain enforced. Do not disable/increase them. Custom server internals are pinned/version-sensitive.

Socket middleware requires the custom server's trusted `interview_analysis_bounded_input=True` ASGI marker. Generic server invocations reject analysis handshakes instead of silently using an unbounded input queue; HTTP remains available. Headers cannot spoof this marker. The custom server also rejects disabled or greater-than-contract wire sizes. Communicator tests explicitly set the marker for controlled synthetic input; the real wire test obtains it from the actual server.

| Configuration | Default |
|---|---|
| `INTERVIEW_ANALYSIS_REDIS_URL` | Existing `REDIS_URL`; one primary shared by all ASGI processes |
| `INTERVIEW_ANALYSIS_REDIS_PREFIX` / `INTERVIEW_ANALYSIS_CHANNEL_PREFIX` | `skillsense.analysis.guard` / `skillsense.analysis.channels` |
| `INTERVIEW_ANALYSIS_WS_ALLOWED_ORIGINS` | Explicit existing localhost frontend ports; supply the actual frontend origin for integration |
| `INTERVIEW_ANALYSIS_MAX_USER_SOCKETS` / `INTERVIEW_ANALYSIS_MAX_SESSION_OBSERVERS` / `INTERVIEW_ANALYSIS_MAX_SOCKETS` | 4 / 4 / 64 |
| `INTERVIEW_ANALYSIS_IO_TIMEOUT_SECONDS` / `INTERVIEW_ANALYSIS_DECODE_WORKERS` | 2 / 2 |
| Fixed v1 message / JPEG / total pixels / width / height | 307200 / 204800 / 921600 / 1280 / 1280 |
| Session/user frame FPS, burst | 3 / 6, burst one |
| Session/user message rate, burst | 20 / 30 per second, burst one second |
| Guard lease / decoder wait / input queue | 15 seconds / 2 seconds / 2 messages |
| Channel capacity / message expiry / group expiry | 16 / 10 seconds / 120 seconds |
| Output JSON bytes | 65536 |

Existing B1 authorization expiry/renewal policy and B2 transport/inactivity/grace/duration policy still apply. Watchdog cadence is capped by the auth check interval and one third of both guard/transport leases, normally five seconds. Pings renew only the generation's original authorization-capped transport lease. Origin rejection occurs before ticket consumption; later connection-limit or publisher-conflict failures require a new ticket. Redis outage/reservation loss is fail-closed. Redis Cluster, production TLS/proxy/APM redaction and distributed target load were not configured/tested.

Raw frames remain transient: bounded queue/current handler, at most two decoding workers, then a private argument to the not-yet-implemented B4 seam. A cancelled decoder waiter cannot free a still-running worker slot. A stuck native decoder cannot be forcibly killed by a Python thread timeout; bounded capacity can remain unavailable until process recovery. SQL await cancellation also cannot forcibly stop a running sync transaction, so statement/lock budgets, generation fences and late-acquisition compensation matter. No raw bytes are persisted or published by B3.

## Exact verification

Final environment: Python **3.12**, Django **6.0.6**, Channels **4.3.2**, Daphne **4.2.2**, Autobahn **26.6.2**, channels-redis **4.3.0**, redis-py **8.0.1**, local Redis server **8.2.10**, Pillow **11.2.1**, websockets client **16.1.1**, PyJWT **2.10.1**. The B1/B2 PyJWT version gap is closed for this run: `.venv` was aligned to the repository's existing 2.10.1 pin, and all auth regressions were repeated. `channels-redis==4.3.0` was installed. The wire client was already present and is now explicitly pinned.

Pip reported that an **already installed**, unused `djangorestframework-simplejwt==5.5.0` has an incompatible `<2.10.0` PyJWT constraint. That preexisting extra package was left untouched; repository requirements, imports and authentication configuration contain no SimpleJWT integration. This local environment-hygiene conflict must not be mistaken for a second application authentication provider. No SimpleJWT package/provider/configuration or identity migration was added by B3. A clean requirements-only deployment environment still needs its own dependency validation.

Final test invocation:

```powershell
.venv\Scripts\python.exe -c "import os, uuid; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_interview_recruitment.settings'); from django.conf import settings; settings.DATABASES['default']['TEST']={'NAME':'test_interview_analysis_b3_'+uuid.uuid4().hex[:12]}; import django; django.setup(); from django.core.management import call_command; call_command('test','interview_system.tests.test_frame_transport','interview_system.tests.test_video_lifecycle','interview_system.tests.test_socket_authentication','interview_system.tests.test_account_roles','interview_system.tests.test_retell_lifecycle','interview_system.tests.test_phase6_scoring_task',interactive=False)"
```

**143 tests in 47.567 seconds: OK; zero failures/errors/skips.** This includes 34 B3 tests and 109 B1/B2/Clerk/Retell/scoring regressions. Real PostgreSQL concurrency gates executed. Local Redis on database 15 was a required dependency, not silently skipped. Redis keys used freshly generated `interview-analysis-test:<uuid>` prefixes; cleanup removed only those test-created keys, never FLUSHDB. A newly randomly named PostgreSQL test database was created/migrated/removed by Django; no preexisting test or application database was reset.

Test progression: initial 28-test B3 run had two failures (a new fixture incorrectly replaced an invalid empty list with a valid JPEG; initial snapshot validation used the generic availability error classification). Both were corrected. Next 32-test run had one wire-test error because the test assumed Autobahn always sent a 1009 close frame; actual oversize abort may contain no received close frame. That assumption was corrected and wire queue overflow was exercised. A full **141-test run passed in 45.766 seconds** on the prior PyJWT 2.9.0 environment. After aligning the pinned auth version and adding lost-reservation enforcement, **142 tests passed in 46.069 seconds**. The final bounded-server marker/size-option safeguards then passed the 143-test run recorded above.

Additional final commands:

```powershell
.venv\Scripts\python.exe manage.py makemigrations --check --dry-run --noinput
.venv\Scripts\python.exe manage.py check
git diff --check
.venv\Scripts\python.exe -m ai_interview_recruitment.socket_server --help
```

Results: **No changes detected** (read-only PostgreSQL migration consistency check); **System check identified no issues (0 silenced)**; **no whitespace errors**; bounded entry-point CLI and both size controls available. A sandboxed help check stalled and its two verified help-only Python processes were stopped; the check passed with local execution access. No application server was stopped. Application SimpleJWT import/config searches returned no matches.

Covered behavior:

- HTTP preserved through ASGI and actual localhost server.
- Missing/null/duplicate/wrong origins, malformed hosts/query credentials, replay, wrong purpose/session, expiry and inactive accounts reject before acceptance. Origin rejection leaves the ticket usable on the correct origin.
- Publisher/observer ownership, one publisher, observer connection cap, shared user reservations/rates across session keys and atomic reservation races.
- JSON/type/key/version/finite-number/sequence constraints; full UTF-8 byte limits; strict base64 and JPEG-only content, decoded bytes, independent dimensions/pixels and truncation checks. Invalid inputs never reach the test admission mock.
- Bounded worker slots remain occupied after cancellation until actual completion; queue overflow drops buffered images and delivers disconnect.
- Explicit test-only valid admission gets server receipt time and creates only test metadata; no observations/inference are fabricated. Production adapter drops capacity and creates no jobs. Rate/availability drops count once without renewing admitted-frame activity.
- Publishers never join observer groups; only validated bounded highest-revision snapshots are delivered. Arbitrary notification fields and invalid snapshot fields cannot leak. Observers cannot send frames.
- Idle expiry/deactivation, role change during snapshot retrieval, account disable during decode, Redis failure/reservation loss, reconnect and stale callbacks stop admission/delivery without automatic video completion.
- Actual local Redis channel-layer group delivery, actual localhost origin denial, complete fragmented-message and single-frame limits, ASGI flood closure, and ticket omission from captured Daphne access logs.

## Remaining limits and next contract

No real browser/camera, real Clerk/Retell provider, GPU/model inference, external proxy/APM/TLS, production hardware capacity, or hosted deployment was verified. Local wire traffic used a Python client with compression disabled. No result from a fixture is claimed as real ML output. Default Channels notifications are bounded best-effort hints; they are not a durable job queue or authoritative report record. Log redaction must still be verified at every upstream layer before deployment.

The B4 seam is deliberately unavailable: `submit_frame` always returns dropped/capacity. Ready advertises transport/auth capability, not an operating vision pipeline. B3-only sessions with no admitted frame still reach B2's inactivity policy. B4 must implement bounded private payload ownership/TTL and durable publication recovery while reusing B2's job identity, global/per-session caps, claims and finalization fences. It must atomically recheck generation/authorization and retain server receipt time before returning accepted. Avoid double-counting attempts that B2 admission or transport-drop counting already recorded. No image belongs in task args or live groups.

The B6 snapshot seam returns None until a real authorized persisted projection exists. Implement it behind the frozen schema, maintain subscribe-first/highest-revision behavior, trim recent observations to 65536 output bytes and implement authoritative status/report retrieval. Candidates must continue to receive transport fields only. No scoring dispatch/rubric/ranking change is authorized by B3.
