# Interview Analysis B7 — deployment preparation and acceptance

Prepared 18 October 2026, Asia/Karachi. **No external deployment, publishing, application migration, Redis reconfiguration, user-data deletion, camera access or provider call was performed.** Keep admission disabled. This guide implements the current B7 operations request; the frontend capture/calibration integration previously called B7 in older handoffs remains outstanding. Clerk remains the only application authentication provider.

## Inspected environment and acceptance boundary

Backend: `D:\Programming\FYP\AI_Interview_Recruitment`; frontend: `D:\Programming\FYP\Skill_Sense_frontend`. Git status and existing B1–B6 code/contract were inspected before editing. No applicable AGENTS.md was found in either repository/inspected ancestors. Existing extensive uncommitted work was preserved. Frontend source was not edited.

Actual host: Windows 11 build 26300, Python 3.12.7, Intel Core i5-11400H (6 cores/12 logical processors), 16,948,453,376 bytes RAM. `nvidia-smi` detected NVIDIA GeForce RTX 3050 Laptop GPU, 4096 MiB VRAM, driver 617.14; inspection sample showed 0 MiB used/0% utilization. Ubuntu-22.04 appears in the WSL inventory; it was not started or certified. No Docker executable was found in the inspected PATH.

The installed pinned ONNX Runtime reports AzureExecutionProvider/CPUExecutionProvider and **no CUDAExecutionProvider**. B5 explicitly permits CPU only, uses MediaPipe IMAGE mode on CPU and FP32 YOLO ONNX (640 input), ORT intra-op threads 2/inter-op 1, max_faces 3. Expression remains disabled. Model/config: `mp1-64184e229b26-y8nonnx-6f987f27fdd4`, `config-v1-ab14d086e77d7811`; full artifact hashes/sizes are in `ai/vision/model_manifest.json`. No model was converted, repinned or silently switched to GPU. GPU presence is not GPU inference acceptance. A GPU runtime needs a separately pinned, validated CUDA/cuDNN/provider/model configuration and compatibility/accuracy/load run.

Celery 5.6.3 does not support Windows as a production target. Actual local tests use a test-only embedded solo worker and real Redis/PostgreSQL; offline real-model runs use one serialized runtime in its owning Windows process/thread. The proposed production process model is **Linux prefork**, still unverified on the intended deployment host. Both explicit production worker entry points reject native Windows rather than imply support. No native Linux hard-kill/recycling result is claimed.

## Processes and queues

Use separate web/general and vision environments. Base web requirements do not install MediaPipe/ONNX/OpenCV. The vision environment installs base requirements plus `requirements-vision.txt`; export tooling stays separate. The Windows lock file is Windows-specific; do not claim it is a Linux lock. Build/reproduce Linux wheels and artifact verification in a staging image before rollout. Never initialize models before fork or in Django/ASGI startup.

The following are Linux **operator instructions, not commands executed for deployment**. Set private environment configuration through the service manager; never paste credentials into a shell history, command line, ticket URL, report or support log. From the backend checkout, activate the appropriate environment and export:

```sh
export DJANGO_SETTINGS_MODULE=ai_interview_recruitment.production_settings
# Supply DJANGO_SECRET_KEY privately (>=32 chars), database/Clerk/Retell secrets,
# INTERVIEW_ANALYSIS_DEPLOY_HOSTS=api.example.invalid
# INTERVIEW_ANALYSIS_DEPLOY_ORIGINS=https://app.example.invalid
# Distinct Redis service URLs, INTERVIEW_ANALYSIS_MODEL_DIRECTORY and INTERVIEW_ANALYSIS_VISION_ENABLED=False.
python manage.py check --deploy
python manage.py behavioral_analysis_ops health --require-ready
```

Replace example domains, never use wildcard origins. The opt-in overlay disables DEBUG, requires a private secret and exact hosts/HTTPS origins, makes cookies secure, trusts the overwritten proxy HTTPS header, enables HTTPS redirect/HSTS, and preserves Clerk authentication/provisioning. Bind ASGI to loopback or a private socket inaccessible to clients; trusting forwarded TLS headers is unsafe on a publicly reachable backend. Webhook/provider reachability must be tested in staging before changing live proxy/TLS behavior. The development settings remain unchanged.

Run independently under a service supervisor:

```sh
# WEB: bounded Daphne, HTTP + WebSocket; access logging suppressed at source.
python -m ai_interview_recruitment.socket_server -b 127.0.0.1 -p 8000 \
  --access-log /dev/null --verbosity 0 --websocket_connect_timeout 5 \
  --websocket_timeout 60 --application-close-timeout 10 \
  ai_interview_recruitment.asgi:application
# GENERAL: separate base environment, exact queue 'celery', prefork concurrency 2.
python -m ai_interview_recruitment.general_worker
# VISION: separate pinned vision environment, exact default queue 'interview.analysis.vision'.
python -m ai_interview_recruitment.vision_worker
# One beat scheduler only; protect its local state file, do not run beat in every worker.
celery -A ai_interview_recruitment beat --loglevel=WARNING
```

`INTERVIEW_ANALYSIS_VISION_QUEUE` changes the vision queue consistently across producers/worker/beat; default is **interview.analysis.vision**. General remains **celery**; it does not consume vision. `process_frame` and `warm_vision` are explicitly registered/routed. General hosts recovery, report aggregation, Retell completion, notification and existing CPU jobs. No GPU is needed for reports. General-queue contention from other long tasks remains an acceptance risk; measure it before deciding whether to add a completion queue rather than changing current routing silently.

Vision is concurrency 1, Redis disable-prefetch enabled, prefetch multiplier 1, early acknowledgments, no Celery autoretry for frames, 20/25-second soft/hard task limits. PostgreSQL claims/outcomes own loss recovery: 30-second claim, 60-second deadline, max 2 worker attempts/3 publications, 10-second publication lease. Corrupt/missing input is terminal. No same-queue ordering is a finalization barrier. Runtime is initialized/warmed by a task inside its owning child and reused serially; no global session tracker. Readiness token expires in 15 seconds and is refreshed by bounded warm tasks (beat every 5 seconds when vision is enabled). Missing/stale/mismatched readiness rejects admission.

`INTERVIEW_ANALYSIS_VISION_MAX_TASKS_PER_CHILD=0` preserves runtime reuse. An explicit value 1..10000 enables Linux child recycling; **warm tasks also count**. Each recycle pays cold load/warm-up, temporarily loses readiness and can discard work if killed; deadline/claim recovery still applies. RSS after native initialization is not a fixed ceiling. Measure leak slope, cold-start impact, readiness loss and process-tree RAM before choosing recycling. Child shutdown attempts cleanup, but signal hooks are not guaranteed after a hard kill; TTL remains authoritative.

## TLS, origins, wire limits and log privacy

Review `deploy/behavioral_analysis/nginx.conf.example` and validate using `nginx -t` on the target (not available/tested here). TLS 1.2/1.3 terminates at nginx; proxy forwards Upgrade/Connection/Host/Origin and overwrites forwarded TLS/client-address headers. Exact application origins still authorize before socket accept. Proxy read/send/connect timeouts are 75/15/3 seconds; application socket lifetime 60 seconds is separate from ticket TTL 30 seconds and credential-capped lease up to 60 seconds. Reconnect obtains a fresh Clerk-authenticated ticket/generation. No custom rejected-browser-handshake close code is promised.

Wire message/frame cap is **307200 bytes**, decoded JPEG **204800 bytes**, max width/height 1280 and max pixels 921600; JSON/base64 v1 only. The custom pinned Daphne entry point bounds ASGI input to two messages; ordinary Daphne/another server is not an equivalent verified configuration. nginx `client_max_body_size` does not limit WebSocket frames. Production proxy/network fragmentation, TLS and browser buffering still need target tests.

Tickets travel in a query string by the frozen v1 contract. Never log `$request`, `$request_uri`, `$args`, Authorization, bodies, socket messages, payload Redis values or task argument dumps. Template access logs use `$uri` without query; session-bearing paths require restricted access/short retention and must not become metric labels. nginx error logs can include original request queries and are disabled in the template; instrument fixed application errors and safe upstream health instead. Daphne access output is disabled. The opt-in Python handler redacts recognizable ticket/Bearer/Redis URL patterns and suppresses traceback details. This is defense in depth, not permission to log arbitrary bodies or a claim that every external agent/APM/service log is sanitized. Audit load balancers, CDN/WAF, APM, crash dumps and support captures separately. Disable DEBUG/task-event/HTTP-wire logging. Do not store secret-bearing logs while testing rejection paths.

## Redis, storage and retention

Use three process-level services/roles: durable broker, transient channel/guard state, and a **dedicated nonpersistent payload primary**. Different DB numbers on one Redis server do not isolate persistence/backups. `deploy/behavioral_analysis/redis-broker.conf.example` proposes AOF everysec/noeviction/256 MiB; the payload template proposes no RDB/no AOF/no replicas/no snapshots/noeviction/128 MiB on a different port/process. These are staging starting points, not measured production memory budgets. Bind privately, enforce ACL/firewall and TLS for remote connections, protect configuration/credentials, monitor memory overhead and failed writes. Channel/guard Redis should use no persistence/noeviction and an explicit memory cap as well; loss denies admission/requires reconnect. Do not use an evicting cache for correctness state. Do not change running Redis settings as part of reading this guide.

Bounded payloads: 64 JPEG slots * 204800 bytes = 13,107,200 raw bytes plus Redis/key/index/client overhead. Global active jobs <=32, each session <=one processing/one latest pending, <=10800 admitted metadata rows/session, vision list <=128 references * <=8192 serialized bytes (1,048,576 bytes before overhead), list TTL 120 seconds. PostgreSQL contains metadata/derived observations only, not images. General queue has no new B7 hard cap; capacity and other task recovery need independent monitoring. These bounds are not a whole-system RSS guarantee.

Payload TTL follows its admission deadline (default <=60 seconds). Terminal/replaced payload cleanup already deletes only its own keys; rollback/crash or Redis outage can leave TTL-bounded orphans. B7 emits one fixed cleanup-deferred warning per failed explicit deletion, with no IDs, URLs, bytes or exception text; collect its rate in a log counter. A missing warning does not prove all cleanup paths ran, and a stopped Redis cannot expire data until it runs again. No AOF/backup/replica may preserve those bytes. Hard-memory forensic zeroization is not certified.

Periodically run the existing bounded recovery and expired-ticket cleanup, under private operations access:

```sh
python manage.py recover_video
python manage.py recover_retell
python manage.py cleanup_socket_tickets
python manage.py behavioral_analysis_ops metrics
python manage.py behavioral_analysis_ops retention-plan --days 30  # inventory only; 30 is NOT an adopted policy
```

Beat already scans video every 5 seconds, Retell every 60 and screening every 15; a supervisor can run a bounded command pass during worker/broker failure. Do not add a second scheduler that floods the queues. B6 report/notification recovery is coalesced, batch default10, claim30s, notification retry10s/send timeout2s, invariant backoff30s. Finalization waits for sealed terminal jobs. Never “repair” by manually changing terminal states/counters or marking video ended on disconnect.

`retention-plan` is a read-only derived-data hook: explicit days, aggregate counts, no IDs, no deletion of reports/jobs/observations and no guessed retention policy. Legal hold, consent, scoring dependencies, audit requirements and approved retention periods remain an operator/product decision. Actual derived deletion is intentionally not implemented without that decision; preserving the job/report boundary and PROTECT relationship needs a reviewed design. No user data was deleted in B7.

## Metrics and evidence

`behavioral_analysis_ops health` returns dependency/migration/vision-lease and payload persistence/eviction/isolation findings without URLs/secrets. `--require-ready` exits nonzero for dependency or payload-policy failure; it is not an HTTP public endpoint and does not certify model accuracy, broker worker consumption, TLS or GPU. Restricted CONFIG/INFO ACLs yield unverified policy, not a false pass. Run a separate privileged read-only configuration audit if needed. No model is initialized by the command.

`behavioral_analysis_ops metrics` returns sampled scope (latest <=1000 sessions, detailed latest <=10000 jobs), receipt/admission/drop totals, terminal/pending/processing counts, oldest active age, retry totals, processed receive-to-terminal p50/p95, and seal-to-final p50/p95. Truncation is explicit. Rates require two snapshots with the same session scope and positive elapsed time; scope churn/reset invalidates deltas. Snapshot percentiles are not mergeable histograms. Permanent service-start timestamps do not exist, so processing latency is NULL here (not inferred from a claim deadline). General-queue latency and cleanup-failure totals are NULL, not invented zero; use actual instrumented runs and the fixed cleanup-event counter. Collect process-tree CPU/RAM, Redis memory/evictions/failed writes, PostgreSQL wait/lock times, queue length and GPU per-process metrics at low cardinality on the target. Session UUIDs belong only in restricted diagnosis, not labels.

The acceptance JSON records actual offline real-model runs: deterministic 640×480 synthetic noise, JPEG 157359 bytes, 3 FPS/session, 20-second offering schedules, one CPU model runtime, latest-pending replacement and round-robin selection. Budget stated before comparison: end-to-end p95 <=500 ms and replacement <=5%. No camera/person/phone accuracy claim. “End-to-end” in those records means scheduled offline receipt through inference only, not HTTP/Redis/Celery/report delivery. It excludes cold load, which is recorded separately. No tracker/calibration history or outcomes persist.

| Simulated sessions | Offered / processed / replaced | Processing p50 / p95 ms | Offline end-to-end p50 / p95 ms | Peak sampled RSS | CPU, one-core % | Budget |
|---|---|---|---|---|---|---|
| 1 | 60 / 60 / 0 | 118.103 / 126.861 | 119.304 / 127.431 | 228298752 bytes | 86.68 | pass |
| 2 | 120 / 120 / 0 | 116.008 / 126.922 | 182.424 / 250.073 | 225673216 bytes | 153.72 | pass |
| 4 | 240 / 180 / 60 | 111.943 / 114.471 | 288.091 / 427.092 | 225673216 bytes | 200.04 | fail: 25% replacement |

Four-session overload selected 45 frames/session, fairness min/max1.0, max pending4, oldest selected age0.435s. This tests the offline harness's round-robin fairness, **not actual database/broker admission fairness**. 2 sessions is the largest tested passing offline CPU case; 3 was not measured. No production concurrency or duration-long interview capacity is certified. Sampled RSS is current-process RSS after operations, not allocator peak/process-tree memory. GPU runtime/VRAM contribution, frame-cleanup failure rate and concurrent general-queue latency were not measured by this harness.

Reproduce each offline run in the existing separate runtime environment (no Django dependency needed):

```powershell
.venv-vision-runtime/Scripts/python.exe -m ai.vision.load --models vision-artifacts --sessions 1 --seconds 20 --report local-load-1.json
.venv-vision-runtime/Scripts/python.exe -m ai.vision.load --models vision-artifacts --sessions 2 --seconds 20 --report local-load-2.json
.venv-vision-runtime/Scripts/python.exe -m ai.vision.load --models vision-artifacts --sessions 4 --seconds 20 --report local-load-4.json
```

The command caps sessions1..16/duration1..60 and retains <=one pending reference/session plus one in flight, with no image/result files. Do not count it as a consented fixture run. No synthetic data enters the application database.

## Migration, rollback, shutdown and failure runbook

Before staging: pin/check out the reviewed code; inventory actual applied migrations; take a normal private **database** backup; verify model hashes, dependency locks, secret configuration and isolated Redis; run migration plan/checks. Pending chain0020..0025 includes earlier lifecycle/auth changes; B7 adds **no migration**. Application DB migrations were not applied here. Test the plan and schema-lock duration on a staging database before scheduling `migrate`. No reset, fake migration or identity cutover is needed.

Rollback: keep admission disabled; stop new publishers, allow current leases/grace/jobs to drain, snapshot migration/code versions and private diagnostics. Prefer rolling code forward/fixing configuration while retaining additive schema. Older code can contain the fake aggregate task: **never restart an old worker/beat that writes fake aggregates or automatically scores B6 observations**. Do not reverse the pending chain/drop new tables/columns or rewrite scores to roll back transport. Any restoration/schema downgrade needs a separate reviewed data plan. Existing reports stay authoritative and immutable.

Graceful shutdown: remove the web instance from the proxy, stop new admissions, allow sockets to close/reconnect through B2 grace (disconnect is not finish), stop beat publication, send TERM once to workers and allow >=60 seconds for a 25-second vision task/cleanup plus supervisor margin. General tasks may need more time; measure their actual limits. Do not send repeated TERM/QUIT/KILL as a routine drain. Native crashes/forced kills may skip cleanup; restart DB/Redis first, then workers/readiness/recovery, verify terminal jobs/finalization rather than trusting queue emptiness.

| Symptom | Safe operator action / evidence |
|---|---|
| Admission capacity drops | Check enabled flag, strict readiness lease, private Redis availability/cap, global32/per-session slots, payload64 and broker128; never raise caps to conceal overload. |
| Worker crash/restart | Restart intended Linux child environment, wait for warm lease, run bounded recovery after expired claim; old tokens must remain fenced. Expect possible unknown/partial evidence, not fabricated success. |
| Broker outage | Keep DB/payload service healthy; committed publication_due_at recovers within caps/attempts/deadline. Restore broker then scan; missing/expired payload is a terminal explanation. Do not republish image blobs or replay every frame. |
| Private Redis outage/missing payload | Deny new work, restore a clean nonpersistent primary, let missing payloads fail/deadlines expire; reports can become partial/failed. Do not restore image backups. |
| Publisher disconnect | Reconnect with fresh Clerk ticket/new generation within grace; stale disconnect cannot mutate replacement. Do not call finish on every disconnect. |
| Audio completed, browser vanished | Existing verified Retell inbox/recovery seals video by its bounded policy; run recover_retell/recover_video, inspect due state. Do not synthesize provider completion. |
| Report delayed | Inspect sealed boundary/active jobs, claim expiry/source revisions, dirty/due state and regular worker latency. Recover; an invariant mismatch requires ledger diagnosis, not guessed counter edits. |
| Observer missed update | Reconnect/fetch authorized snapshot/report, apply highest revision only. Persisted report and polling recover notification loss; no duplicate score/inbox effect is dispatched. |
| Cleanup-deferred warnings | Count fixed warnings; inspect TTL/persistence privately, restore Redis; never log GET payloads or FLUSH shared services. |
| Deactivated/logged-out user | Local active checks enforce existing lease cadence; client closes on logout and reconnect needs a fresh verified ticket. Clerk remote revocation detection limits remain B1; no instant global revocation claim. |

## Tests and explicit unmet checks

Exact final results are in `behavioral_analysis_acceptance.json`: **299 backend tests / 158.228s / OK / exit0** (289 existing regression tests plus the initial 10 B7 tests); final focused **15 B7 tests / 6.454s / OK / exit0**, including the added overlay/redaction/probe checks. There are 304 distinct tests across those overlapping runs, not 314. Both new disposable DBs were destroyed. PostgreSQL suites use random newly created test DBs; Redis tests use random prefixes/queues in DB15 and delete only their own keys, never FLUSHDB. Local RSA Clerk signatures and deterministic test-only inference are not provider/ML acceptance. Existing real Daphne fragmentation/origin/HTTP/log-redaction tests and real Redis/Kombu/solo-worker scheduling tests are included. Worker loss, broker failure and missing payload tests inject failures; they are not a production Redis outage or Linux process-kill experiment.

The B7 probe measured ten real Redis deliveries through a Windows embedded solo **test-only** worker on its own random queue: p50 **2.775ms**, p95 **9.878ms**. It carries only a timestamp and ignores task results. This is not production general-queue latency or contention with a real vision worker. The actual queue name in production remains celery; the probe deliberately does not consume that live queue.

Frontend command `flutter test --reporter expanded` finished at reported **00:43**, **155 passed / 1 skipped / 27 failed / exit1**. The acceptance JSON lists all 27 names. Failures include profile/resume/report/role/ranking layout assertions, navigation, scheduling and sign-out expectations. Source was not edited; this run does not establish when those failures originated. They were recorded rather than patched outside B7 scope. Full-stack Flutter-to-InterviewAnalysis acceptance was not possible because no InterviewAnalysis client implementation is present.

Reproduce the broad backend run from the backend root:

```powershell
.venv/Scripts/python.exe -c "import os,uuid; os.environ.setdefault('DJANGO_SETTINGS_MODULE','ai_interview_recruitment.settings'); from django.conf import settings; settings.DATABASES['default']['TEST']={'NAME':'test_interview_analysis_b7_'+uuid.uuid4().hex[:12]}; import django; django.setup(); from django.core.management import call_command; call_command('test','interview_system.tests.test_behavioral_analysis_operations','interview_system.tests.test_behavioral_reporting','interview_system.tests.test_vision_pipeline','interview_system.tests.test_vision_scheduling','interview_system.tests.test_frame_transport','interview_system.tests.test_video_lifecycle','interview_system.tests.test_socket_authentication','interview_system.tests.test_account_roles','interview_system.tests.test_retell_lifecycle','interview_system.tests.test_phase6_scoring_task','interview_system.test_phase10_admin','interview_system.test_phase10_notifications','interview_system.test_phase10_recruiter_reads',interactive=False)"
```

The first broad run discovered the initial 10 B7 tests. Repeating that command against the final files discovers all 15 B7 tests. The final focused command uses the identical random-database setup with only `interview_system.tests.test_behavioral_analysis_operations` as its test label. Logs remain local/ignored: `.interview-analysis-b7-backend-tests.log`, `.interview-analysis-b7-ops-tests.log`, and the frontend `.interview-analysis-b7-frontend-tests.log`.

`manage.py check`: no issues. `makemigrations --check --dry-run --noinput`: No changes detected. `git diff --check`: passed with LF/CRLF conversion advisories. Test-only production-overlay `check --deploy` returned five warnings: two drf-spectacular integrity type hints, one admin serializer schema issue, and HSTS includeSubDomains/preload deliberately left off without domain-wide approval. It used an explicit nonproduction test secret/domain, not live configuration; it is not a clean production certification. Both Windows worker guards were exercised and refused startup before loading models.

Current local health: PostgreSQL reachable, migrations pending, vision disabled/unready, Redis8.2.10 reachable/noeviction/maxmemory8 GiB, but payload is on the same Redis instance and is persistent. `ready=false`, payload_policy_compliant=false. No configuration was changed to hide this result. Application tables remain unmigrated.

Unmet acceptance checks:

- Target Linux/container/prefork initialization, termination, recycling and deployment packaging; CUDA/provider compatibility, real GPU inference/VRAM/CPU fallback behavior.
- Full-stack 3 FPS load using approved staged clips, duration-long concurrent interviews, database/broker fairness, real worker restart/broker outage under load, process-tree memory and concurrent general-queue p50/p95. Offline tests do not establish these.
- Consented webcam/camera/browser capture, calibration, TLS/proxy execution and no-token-leak checks across every upstream log/APM; nginx is a template, not a tested running proxy here.
- Approved Retell/Clerk test tenant/call/identity and live webhook/provisioning/logout acceptance; no real provider calls were made.
- Frontend InterviewAnalysis ticket/capture/lease/calibration/finish/revisioned-report integration is absent; existing Flutter regression results do not certify that flow.
- Staged lighting/glasses/head turns/no face/second person/phone accuracy gate; expression stays disabled. No recordings were assumed consented or downloaded.
- Apply/rehearse reviewed migrations, isolate nonpersistent payload service, approve derived retention/legal-hold/deletion policy, establish general task/cleanup telemetry and production latency SLOs. Admission stays disabled until those gates pass.

Next integration contract: retain B1–B6 exact v1 routes/auth/lease/lifecycle/coverage/null schemas; implement the authorized frontend flow and staged accuracy/target runtime acceptance before enablement. Keep candidates transport/availability-only and recruiter observations explicitly approximate/unvalidated; no ranking penalties, cheating verdicts, historical rewrites or rubric changes.

## Changed files and verification references

New: `ai_interview_recruitment/general_worker.py`, `production_settings.py`, `safe_logging.py`; `ai/vision/load.py`, `measurements.py`; `interview_system/behavioral_analysis_operations.py`, `management/commands/behavioral_analysis_ops.py`, `tests/test_behavioral_analysis_operations.py`; `deploy/behavioral_analysis/*.conf.example`; this guide and acceptance JSON. Modified: `vision_worker.py` (Windows guard/opt-in recycling), `vision_payloads.py` (fixed cleanup event), `.env.example`, `.gitignore`, shared contract. No frontend source changes or B7 migrations.

Official behavior verified against [Celery platform FAQ](https://docs.celeryq.dev/en/stable/faq.html), [worker shutdown/recycling](https://docs.celeryq.dev/en/stable/userguide/workers.html), [nginx WebSocket proxying](https://nginx.org/en/docs/http/websocket.html), [nginx access-log variables](https://nginx.org/en/docs/http/ngx_http_log_module.html), [Redis persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/), [eviction/noeviction](https://redis.io/docs/latest/develop/reference/eviction/), and [ONNX CUDA provider requirements](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html). Installed Daphne/Kombu/Channels behavior remains pinned and covered by the existing transport tests. These sources justify preparation choices, not an external-deployment certification.
