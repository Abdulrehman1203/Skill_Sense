# Interview Analysis — Shared integration contract (Clerk only)

Status: protocol v1 frozen by B1/B2 on 17 October and extended by B3–B7 on 18 October 2026. B6 implements persisted, revisioned reports/status/snapshots; B7 adds opt-in deployment preparation and records local acceptance gaps in section 18. Admission remains disabled by default; accuracy validation still awaits consented staged clips. Expression remains explicitly disabled. Sections 12–17 resolve earlier illustrative wording; section 17 freezes actual B6 reporting/recovery and excludes B6 observations from legacy scoring/integrity policy.

This guide set replaces earlier Interview Analysis advice proposing SimpleJWT. The user's confirmed requirement is **Clerk only for application authentication**. Preserve Clerk users, provisioning, webhooks, and Flutter authentication. An opaque socket ticket is a temporary connection credential, not a new identity provider or locally issued application JWT.

Documents:

- [Backend implementation guide](interview_analysis_backend_guide.md)
- [Frontend implementation guide](../../Skill_Sense_frontend/docs/interview_analysis_frontend_guide.md)

The older `interview_analysis_implementation_playbook.md` is historical context. Where it conflicts with this guide set, follow this guide set for new Interview Analysis work. Existing code must still be inspected before editing. No original SRS/SDD compliance certification is implied.

## 1. Scope and non-negotiable rules

Build sampled JPEG analysis on private backend infrastructure. Retell handles audio separately. This does not build video calls, video recording, playback, identity verification, or cheating detection.

- Keep raw frames away from Retell, Gemini, hosted image APIs, public storage, and routine logs.
- Candidate receives transport/availability feedback only. Recruiter receives authorized observations.
- Models run outside the ASGI web process.
- Queue and temporary image storage must remain bounded under overload.
- Disconnect is recoverable; it is not an immediate interview-end instruction.
- Aggregate only after admitted frames are accounted for.
- Unknown/unusable observations are not zero attention or absence.
- Never publish synthetic model results through production report paths.
- Behavioral observations do not automatically become ranking penalties in this implementation. Existing scoring behavior must be inventoried; any scoring-policy change is explicit and separately reviewed, not silently applied to existing rubrics or scores.

## 2. Existing project constraints

Prior inspection found Clerk-linked application users; existing `InterviewSession.ended_at`, `audio_ended_at`, and `video_required`; completion services with row locks; a `FrameAnalysis(session, ts)` index; a one-to-one `BehavioralAnalysis`; and an aggregation stub with fake values. Verify these against the current checkout.

Candidate ownership is `session.interview.application.candidate.user_id == user.pk`. Recruiter ownership is `session.interview.application.job.recruiter.user_id == user.pk`.

Preserve existing audio-only completion. Use existing Retell start/read/end/sync routes instead of introducing duplicate voice APIs. A video-enabled start must explicitly request the existing video-required mode before that session mode becomes immutable.

## 3. Frozen interfaces

These paths are frozen contracts. POST ws-ticket, POST finish-video and both WebSocket routes exist after B3. Status/report HTTP routes remain reserved for B6. Ready means authorized transport, not available inference or durable frame processing.

| Interface | Permission | Purpose |
|---|---|---|
| `POST /api/interview-sessions/{id}/ws-ticket/` | Clerk authenticated, purpose-specific ownership | Obtain socket credential |
| `WS /ws/analysis/{id}/?ticket=...` | Candidate publisher ticket | Submit frames |
| `WS /ws/analysis/{id}/observe/?ticket=...` | Owning recruiter observer ticket | Receive observations |
| `GET /api/interview-sessions/{id}/analysis-status/` | Owning candidate or recruiter | Role-filtered availability |
| `POST /api/interview-sessions/{id}/finish-video/` | Owning candidate; any recruiter control must be explicitly specified | Seal video admission |
| `GET /api/interview-sessions/{id}/behavioral-analysis/` | Owning recruiter | Snapshot/final report |

Ticket request: `{ "purpose": "publisher" }` or `{ "purpose": "observer" }`.

Ticket response example:

```json
{
  "ticket": "opaque-random-secret",
  "expires_at": "2026-10-17T10:00:30Z",
  "socket_path": "/ws/analysis/SESSION_ID/",
  "protocol_version": 1
}
```

Authenticate this HTTP request with the existing Clerk bearer token. Bind the ticket to local user, Clerk session context where available, interview session, purpose, and expiry. Consume it atomically, then recheck relevant account/session permissions before acceptance. Store a digest if tickets are persisted. Ticket responses use `Cache-Control: no-store`; redact the ticket query parameter in proxy/APM/application logs.

Reject unauthorized handshakes before acceptance. Browser receipt of a custom close code is not an acceptance criterion. Friendly authorization errors come from the ticket HTTP request. Reconnection requires a new ticket.

## 4. Socket authorization lifetime

Ticket expiration controls ticket redemption, not the lifetime of an established connection. Section 12 defines the lease, renewal and revocation bounds. The B1 helpers implement these checks; B3 must schedule and enforce them on actual sockets.

- Bound socket authorization to verified session credentials and a configurable maximum lease.
- Revalidate account/session eligibility on renewal and periodically where supported.
- At lease expiry, stop admission and require a refreshed Clerk-authenticated ticket/reconnection; do not mark the interview ended immediately.
- Client logout closes sockets and stops capture; server enforcement cannot depend only on that client action.
- Local account deactivation must affect connected sessions within a documented bounded interval.
- Document the actual Clerk logout/revocation detection behavior; do not promise instant revocation from offline JWT signature checking alone.

## 5. Publisher protocol v1

Initial server message:

```json
{
  "type": "ready",
  "protocol_version": 1,
  "connection_generation": "server-issued-identifier",
  "target_fps": 3,
  "max_message_bytes": 307200,
  "max_jpeg_bytes": 204800,
  "max_pixels": 921600
}
```

Limits are frozen initial defaults, not performance guarantees. Maximum width and height are each 1280; total pixels remain 921600. The client should normally send substantially smaller frames.

Candidate message:

```json
{
  "type": "frame",
  "protocol_version": 1,
  "sequence": 17,
  "capture_ts": 1790000000.25,
  "data": "BASE64_JPEG_WITHOUT_DATA_URL_PREFIX"
}
```

- Sequence is strictly increasing within a server-issued connection generation. Reconnection may reset it.
- Frame identity is `(session, generation, sequence)`; do not identify frames by client timestamp.
- `capture_ts` is finite Unix seconds and untrusted metadata. Record server receive time separately.
- Server validates JSON shape, size, strict base64, JPEG decoding, dimensions, rate, sequence, session eligibility, and ownership.
- Apply transport-level size controls where supported and application-level checks regardless. Verify the selected ASGI server's actual controls.
- Decode expensive/untrusted image content away from the event loop with bounded resources.

Admission response:

```json
{
  "type": "ack",
  "connection_generation": "server-issued-identifier",
  "sequence": 17,
  "status": "accepted"
}
```

Other feedback: `status: "dropped"` with bounded reason codes such as `rate_limited`, `stale`, or `capacity`. Protocol errors use a documented `error` message where the connection is already accepted. Avoid reflecting arbitrary client input.

Accepted means admitted for processing, not analyzed or durably reported. An accepted pending frame may later become superseded, failed, or expired; account for that server-side. If sending later candidate processing feedback, include identifiers and status only, never observations.

Use application heartbeat/response messages if browser-visible liveness requires them. Heartbeats must not keep a session with no frames alive forever; define separate maximum session duration and video inactivity policies.

## 6. Capacity and admission

Initial target: one executing frame and one latest pending frame per session, plus a configured global admission cap. Replace older pending work atomically and record its terminal outcome. Do not create unlimited metadata jobs merely because raw payload storage is bounded.

Handle two tabs explicitly: reject a second publisher with a clear conflict initially. A reconnect takeover may occur only under the documented stale-connection lease policy. Server generations fence off old publishers and stale disconnect callbacks.

Worker claims have leases. Persist observations idempotently. Broker publication failure must not strand admitted work: recover from persisted job state. Missing temporary image data becomes an explicit expired/failed outcome, not a fabricated observation.

Do not use Redis pub/sub as the durable frame-job queue or completion record. Redis/Channels notifications are best-effort live delivery; PostgreSQL is the source of report truth.

## 7. Lifecycle and finalization

Suggested separate states:

```text
Video:    NOT_STARTED -> ACTIVE <-> DISCONNECTED -> FINALIZING -> ENDED
Analysis: PENDING -> PROCESSING -> FINALIZING -> COMPLETE / PARTIAL / FAILED
```

1. Disconnect records loss of transport and a grace deadline.
2. Successful reconnect advances generation without restarting the interview.
3. Explicit finish or an expired grace policy seals admission under a shared lock.
4. Record the final admission boundary and finish reason.
5. Wait for all admitted jobs to reach terminal outcomes: processed, superseded/dropped, failed, or expired.
6. Finalize coverage-aware analysis idempotently.
7. Coordinate video completion with the existing audio/video completion service. Report readiness remains independent of interview DONE.

Define behavior for audio ending while camera transport remains connected: request/perform video sealing according to the documented session policy, with a bounded recovery path. Do not wait forever for a browser finish call. Do not trust a client transcript or client claim that Retell ended.

Use explicit terminal reasons and recovery deadlines. A single-concurrency queue is not a finalization barrier; retries and delayed publication can arrive later.

## 8. Recruiter events and reports

Use a separate authorized Channels group/consumer for recruiters. Never add publishers to it. No browser connects directly to Redis.

Frozen event envelope (revisioned full bounded snapshots, never deltas):

```json
{
  "type": "analysis.update",
  "session_id": "SESSION_ID",
  "revision": 12,
  "server_ts": "2026-10-17T10:00:00Z",
  "status": "processing",
  "coverage": { "processed": 35, "valid_gaze": 29 },
  "observations": []
}
```

Freeze whether updates are full snapshots or deltas. Prefer revisioned bounded snapshots initially. Include cumulative totals and only a bounded recent-event window; paginate full timelines if needed. Prevent lost-update races when assigning revisions.

On connect/reconnect, retrieve an authorized snapshot and ignore lower revisions. Ensure snapshot/subscription ordering cannot leave a permanent gap, e.g. subscribe then apply the highest-revision snapshot/update.

Final report includes:

- Status and finalization reason.
- Received/admitted/processed/dropped/failed/expired counts with explicitly defined, non-double-counted meanings.
- Valid and unknown observation counts; coverage definition.
- Centered percentage among valid gaze observations, or null when unavailable.
- Event types, episode counts, bounded observed durations, and timestamps.
- Expression summary only if enabled and actually measured.
- Pipeline/model/rule versions, analysis revision, and finalized time.

Do not interpolate across long observation gaps. A report with no usable evidence is unavailable/partial under the specified rule, not 0% attention. Detection labels describe observations, not intent.

## 9. Privacy and retention decisions

Before production use, define and document the camera notice, recorded acknowledgement, allowed viewers, raw-frame TTL, derived-record retention, deletion behavior, and expression-analysis policy. These are product/deployment decisions; do not invent a legal compliance certification.

Raw-frame defaults: private temporary storage, short TTL, deletion after processing, orphan cleanup, no backups or logs containing raw images unless explicitly required and documented. Derived records also need retention and authorization; absence of raw video does not make them non-sensitive.

## 10. Cross-repository acceptance matrix

| Scenario | Required result |
|---|---|
| Wrong user/purpose/session ticket | No accepted publisher/observer connection |
| Replayed/expired ticket | Rejected |
| Account disabled during session | Admission/subscription ends within documented bound |
| Network blip | Reconnect without premature interview completion |
| Stale socket disconnect | New publisher unaffected |
| Input exceeds inference capacity | Bounded work and memory; documented drops |
| Worker/broker failure | Recovery or explicit terminal failure; no hanging finalization |
| Retry after finalization | No silent mutation of finalized revision |
| No valid gaze | Null percentage and coverage explanation |
| Recruiter misses updates | Snapshot restores latest state |
| Candidate subscribes as recruiter | Denied |
| Audio-only session | Existing completion behavior preserved |
| Real interview finish | Media cleaned up; video sealed; eventual report state visible |

## 11. Sequence and handoff

Backend B1 freezes this contract; B2-B4 establish lifecycle/transport/scheduling. Frontend F1-F3 can use explicit fixtures after B1, then integrate against B3/B4. Backend B5-B6 add real models and reports; frontend F4-F5 complete voice/observer integration. B7 and F6 jointly validate the actual target environment.

Every handoff records route schemas, reason enums, config defaults, migrations, test results, and remaining limitations. Update this contract whenever both sides agree to a change; do not let either implementation silently invent another protocol.

## 12. B1 normative freeze and implementation boundary

This section is authoritative for v1. All timestamps on HTTP/JSON responses are UTC RFC 3339 strings; IDs are UUID strings except Clerk identifiers. JSON numbers must be finite. Object keys described below are exact unless a field is explicitly optional. Client requests reject unknown fields. No credential, raw image, transcript, or arbitrary client text belongs in errors.

### Implemented HTTP ticket API

`POST /api/interview-sessions/{id}/ws-ticket/` accepts `Content-Type: application/json`, the existing `Authorization: Bearer <Clerk token>` header and exactly `{"purpose":"publisher"}` or `{"purpose":"observer"}`. It returns HTTP 200 with exactly `ticket` (43 URL-safe characters, 256 random bits), `expires_at`, `socket_path` (relative path without query string), and `protocol_version` (integer 1), as shown in section 3. The client constructs the query using URL encoding. Do not append an application JWT to any socket URL.

The endpoint uses `ClerkJWTAuthentication.authenticate()` → public `authenticate_credentials()` → the existing single RS256/JWKS verification implementation. Required signed claims are `sub`, `exp`, `iss`; issuer must equal `CLERK_ISSUER` (defaults to the configured Frontend API JWKS URL without `/.well-known/jwks.json`). Existing 5-second JWT clock tolerance remains for HTTP verification; ticket/lease deadlines never extend beyond the actual `exp`. PyJWT checks `nbf` and `iat` when present. A pending Clerk `sts` is rejected. Audience verification remains disabled, preserving the existing session-token interface; these endpoints do not promise acceptance of arbitrary custom JWT templates. `CLERK_AUTHORIZED_PARTIES`, when nonempty, requires an exact matching `azp` (including rejecting missing `azp`). Set it to the actual supported frontend origins before production; empty preserves existing native/browser compatibility. This is a signed-claim check, not a replacement for B3 WebSocket Origin validation.

Existing verified provisioning and Clerk webhook ordering/deactivation are preserved. Missing users may be provisioned only after verified HTTP credentials through the existing Clerk backend lookup and webhook handler. Tickets never provision identities. `sid` is bound when present, stored as an empty string when absent, and is not invented from browser data. Reassigning a local user's Clerk subject invalidates old tickets/leases.

Publisher permission requires active local `CANDIDATE` role and ownership through `application.candidate.user_id`. Observer requires active local `RECRUITER` role and ownership through `application.job.recruiter.user_id`. No implicit admin bypass. A profile is a separate domain object even though its primary key currently equals its user's ID; never authorize from client-supplied candidate/profile IDs. Both purposes require `video_required=true`. Publishers additionally require interview `LIVE`, non-null session `started_at`, null `ended_at` and `audio_ended_at`, and `creation_uncertain=false`. Observers may connect to a completed video session for report retrieval; they cannot publish. A nonexistent session and a foreign session produce the same access denial. B2 must extend the shared eligibility function to reject sealed/finalizing video admission.

Handled errors have exactly `{"error":"ENUM"}`:

| HTTP status | Error enums |
|---|---|
| 400 | `invalid_request` (including JSON parser errors) |
| 401 | `authentication_failed`, `credential_invalid`, `credential_expired` |
| 403 | `access_denied`, `account_inactive` (deactivation during issuance recheck) |
| 409 | `video_not_enabled`, `session_not_publishable` |
| 429 | `ticket_limit` |
| 503 | `authorization_unavailable` (database unavailable) |
| 405 / 406 / 415 | `method_not_allowed` / `not_acceptable` / `unsupported_media_type` |

`request_failed` is reserved for other framework-handled failures. Authentication failures, including an already inactive user, normally return 401 before issuance. All handled ticket responses, including errors, have `Cache-Control: no-store` and `Pragma: no-cache`. Unexpected process/proxy failures are operational failures, never successful authorization.

Ticket storage is the PostgreSQL `socket_tickets` table: SHA-256 digest as unique primary key, local user FK, session FK, purpose, Clerk subject/session ID, verified credential expiry, ticket expiry, and consumed time. No raw ticket/JWT is persisted. Expiry is `min(issue time + 30 seconds, credential exp)`. Issuance takes a user-row lock and caps unconsumed, unexpired tickets at four per user across all sessions/purposes; consumed records do not count. Expired records for that user are removed at issuance. Operators must run `manage.py cleanup_socket_tickets` at least once a minute (bounded to 1000 expired records per invocation; increase invocation frequency if cleanup lags). This command is provided, not automatically scheduled by B1. No frames or work queue are introduced.

`redeem_ticket(secret, session_id, purpose)` uses one conditional database UPDATE matching digest, route session, route purpose, unconsumed state and both future deadlines. Exactly one process can update it. It then loads identity from the row and rechecks current local activity, subject, ownership, role and session eligibility. A correctly scoped ticket remains spent if this recheck fails; wrong route/purpose does not consume it. All malformed/expired/replayed/wrong-scope tickets use the same internal `ticket_invalid` reason. The browser does not supply a separate user identity. Before accept, the consumer rejects failed redemption; no custom browser-visible close code is guaranteed. Redemption errors must not expose the ticket or row contents.

### Authorization lease, renewal, logout and outages

Successful redemption returns an immutable `SocketLease`: local user/session IDs, purpose, Clerk subject/session ID, and `expires_at = min(original verified credential exp, redemption time + INTERVIEW_ANALYSIS_SOCKET_LEASE_SECONDS)`. Default maximum lease is 60 seconds. Redemption does not restart the original credential lifetime; ticket TTL and socket TTL are separate. Lease objects are internal and have no diagnostic representation containing their fields.

B1 provides `redeem_ticket_async`, `check_lease_async`, and `authenticate_clerk_credentials_async`, all using Channels `database_sync_to_async` around the entire synchronous operation. No ORM, JWKS HTTP, or provisioning HTTP may run directly on the event loop. B3 handshakes must use ticket redemption; the credential wrapper is provided for verified non-HTTP integrations, not an invitation to add JWT socket messages.

B3 must check the lease before every frame admission, observer snapshot and observer update, and run an independent watchdog at most every `INTERVIEW_ANALYSIS_AUTH_CHECK_SECONDS` (default 5 seconds), including while idle. Expiry immediately stops further admission/delivery; the watchdog removes observer membership and closes the connection no later than the next tick. Local deactivation, role/ownership change, subject change and session ineligibility have the same check-before-use behavior and a default 5-second idle detection bound, plus scheduling/DB latency. A hung database/runtime cannot promise a hard real-time close bound; failures/timeouts must disable admission/delivery and close, not reuse a cached successful check. B3 must bound its watchdog I/O timeouts. B1 does not yet have connected consumers and therefore does not claim live enforcement of this bound.

Renewal is **new Clerk-authenticated HTTP ticket + reconnect only**. There is no in-band `renew` message and a heartbeat never extends authorization. The client refreshes its Clerk token before requesting another ticket, stops capture on expiry, and reconnects with a new ticket. No expired lease may admit a final frame. Publisher replacement must follow B2 generation/active-publisher fencing, not simply accept two publishers. Neither auth expiry nor disconnect invokes interview/video completion directly.

On client logout: stop capture, discard pending image buffers and tickets, close publisher and observer sockets, then complete Clerk sign-out. Server enforcement does not trust this action. B1 verifies JWTs offline; it does not call Clerk session introspection or subscribe to session revocation events. A stolen, still-unexpired token may still obtain tickets after Clerk logout/revocation until its signed `exp`, but cannot extend any lease beyond that `exp`. Existing local user-deletion webhooks deactivate users when delivered; webhook delivery itself has no instant guarantee. No stronger logout/revocation bound is claimed, especially for custom tokens with long expiry. An absent `sid` further prevents session-specific correlation. Instant provider revocation would require a separately implemented online/session-event policy. Do not claim this exists.

### Socket protocol and reserved downstream HTTP schemas

These are frozen consumer targets for B2–B6, not additional B1 endpoints:

* Publisher `ready` is the section 5 object plus `max_width:1280`, `max_height:1280`, `lease_expires_at` (RFC 3339), and `auth_check_seconds:5`. Observer `ready` contains only `type`, `protocol_version`, `session_id`, `lease_expires_at`, `auth_check_seconds`. Server generations are opaque UUID strings. No ready response is sent before authorization.
* `frame` has exactly the five fields in section 5. `sequence` is an integer from 0 through 9007199254740991, never a boolean; `capture_ts` is finite Unix seconds. `data` is strict base64 without a data URL prefix. Wire message limit 307200 bytes, decoded JPEG limit 204800 bytes, maximum 1280 by 1280 with at most 921600 pixels, target/maximum admission rate 3 FPS. Reconnection resets sequence only under a new server generation.
* `ack` has `type`, `connection_generation`, `sequence`, `status` (`accepted` or `dropped`); only a dropped ack adds required `reason` (`rate_limited`, `stale`, `capacity`). Accepted means admitted, not processed. Malformed messages do not get accepted acks.
* Post-accept protocol errors are exactly `{"type":"error","reason":"ENUM"}` with `invalid_message`, `message_too_large`, `invalid_frame`, `dimensions_exceeded`, `invalid_sequence`, `unsupported_version`, `lease_expired`, `account_inactive`, `access_denied`, `session_not_publishable`, `publisher_conflict`, `authorization_unavailable`, `internal_error`. No arbitrary detail text. Auth/eligibility failures and protocol version violations close after the error if sending is still possible. Pre-accept failures have no JSON guarantee. Close categories: 4401 credential/lease, 4403 account/ownership, 4409 publisher conflict/session ineligibility, 1009 size, 1008 other protocol violations, 1011 service failure. These categories apply only after acceptance.
* Either role may send exactly `{"type":"ping"}`; server replies `{"type":"pong","server_ts":"RFC3339"}`. Heartbeat interval 5 seconds. Heartbeats convey no video activity or authorization renewal. Observer frames and renewal messages are invalid messages. Publishers never receive observations.
* B2 defaults to freeze in its persistence/configuration: one publisher, heartbeat/stale transport lease 15 seconds, reconnect grace 30 seconds, video inactivity 30 seconds since last admitted frame (or activation if no frames), maximum video duration 3600 seconds, provider-verified audio end seals admission with recovery within 5 seconds plus scanner/DB latency. Watchdogs run independently of heartbeat traffic. Finish reasons: `candidate_finished`, `audio_ended`, `reconnect_timeout`, `inactivity_timeout`, `duration_limit`, `service_failure`. Auth loss first records a recoverable disconnect; it is not an immediate finish reason. B2 must fence old generations and preserve existing audio-only completion.
* Scheduling target is one executing and one latest pending frame per session, with initial global admitted nonterminal job cap 32. Admission exceeding capacity drops explicitly. Terminal job outcomes: `processed`, `superseded`, `failed`, `expired`. B4 implements durable claims and bounded raw payload storage; B1 implements neither.
* `POST finish-video/` accepts exactly `{}` and returns `{"session_id":"UUID","video_state":"finalizing|ended","finish_reason":"ENUM"}`; repeated authorized calls return the same sealed reason. Only owning candidates may call it. B2 implements it.
* `GET analysis-status/` returns exactly `session_id`, `video_state` (`not_started`, `active`, `disconnected`, `finalizing`, `ended`), `analysis_status` (`pending`, `processing`, `finalizing`, `complete`, `partial`, `failed`), `revision` (nonnegative integer), and `availability_reason` (null, `not_started`, `processing`, `no_usable_evidence`, `partial_coverage`, `pipeline_failed`). Both owners may read this transport projection; candidates receive no observations or scores. B6 implements it.
* Observer `analysis.update` uses the section 8 envelope, with `status` using the analysis-status enum, `revision` monotonically increasing per session and `observations` capped at 100 most recent observations. It is a full snapshot of cumulative coverage and a bounded recent window, never a delta. Subscribe first, then apply the highest-revision persisted snapshot/update; ignore equal or older revisions. No subscription/group membership for publishers.
* Each observation contains exactly `id` (UUID), `kind` (`face`, `gaze`, `person`, `phone`, `expression`), `value` (`unknown`, `none`, `one`, `multiple`, `centered`, `away`, `present`, `absent`, or `unavailable`), `confidence` (null or number 0..1), `observed_at` (server timestamp), `boxes` (at most 20 normalized `[x,y,width,height]` arrays with all extents inside 0..1). `expression` remains `unavailable` until an enabled, named measured model and label-contract amendment exists; no invented emotion labels. These describe observations, never cheating intent.
* `coverage` contains nonnegative integer counts `received`, `admitted`, `processed`, `dropped`, `superseded`, `failed`, `expired`, `valid_gaze`, `unknown_gaze`. `received` counts identified well-shaped frame submissions; malformed protocol traffic is separate operational telemetry. `received = admitted + dropped`; `superseded` is an admitted terminal outcome, not an admission drop. At finalization `admitted = processed + superseded + failed + expired`; `valid_gaze + unknown_gaze = processed`. Pending snapshots can have a smaller terminal sum. Retries never increment admission or terminal counters twice.
* `GET behavioral-analysis/` returns a recruiter-only snapshot with exactly `session_id`, `revision`, `status`, `finish_reason` (null until sealed), `coverage`, `centered_percentage` (null or 0..100 among valid gaze samples only), `events` (at most 100 recent episodes), `expression_status` (`disabled` or `unavailable` initially), `versions` (`pipeline`, `model`, `rules`: strings, or null before available), `finalized_at` (timestamp or null). An episode has `id`, `kind` (`gaze_away`, `multiple_faces`, `multiple_people`, `phone_present`, `no_face`), `started_at`, `ended_at` (nullable for active episodes), and `observed_duration_seconds` (finite nonnegative). Durations must not bridge unknown/long gaps; B5 must version the measured gap/rule policy. Full timeline pagination requires an explicit later route amendment; never send an unbounded array. `complete` requires all admitted jobs terminal, usable evidence and no failed/expired/unknown samples; otherwise usable evidence is `partial`, no usable evidence is `failed` with `no_usable_evidence`. No-frame reports have null percentage. B6 must distinguish these from interview DONE. No automatic ranking penalty or historical score rewrite is authorized.

### Credential and image logging requirements

Application services never log raw JWTs/tickets, decoded claims, ticket responses, authorization headers, raw ASGI scopes, frame bodies or raw frames. Auth verifier exceptions/logs are fixed messages; token values and untrusted signing-key IDs are not interpolated. Django sensitive-variable annotations protect named credential locals in production exception reports; they do not make DEBUG pages or third-party tracing safe. Run production with DEBUG disabled and disable request/response body capture for ticket/analysis routes.

Before B3 traffic, configure every proxy, ASGI access logger, load balancer, APM and error collector to omit query strings for `/ws/analysis/` entirely (preferred), or redact every occurrence of `ticket`, including duplicate/encoded parameters. Also scrub `Authorization`, `Cookie`, `Set-Cookie`, and fields named `ticket`, `token`, `access_token` from tracing. Never use `$request`, `$request_uri`, full scope repr, response body logging, or query values as metrics labels on these routes. Log route templates, status/reason enums and generated correlation IDs only. A downstream log filter is insufficient if an upstream proxy already stored the secret. These external systems have not been configured or verified by B1; ticket transport must not be deployed until redaction and TLS/origin checks are verified.

### Verified references and handoff

Behavior checked against [Channels database access](https://channels.readthedocs.io/en/stable/topics/databases.html), [PyJWT verification API](https://pyjwt.readthedocs.io/en/2.10.1/api.html), [Django queryset update](https://docs.djangoproject.com/en/6.0/ref/models/querysets/#update), [Clerk session-token claims](https://clerk.com/docs/guides/sessions/session-tokens), and [Clerk manual verification](https://clerk.com/docs/guides/sessions/manual-jwt-verification). In particular, PyJWT validates expiration only when present unless required; Channels supplies the DB-connection cleanup boundary; conditional SQL UPDATE avoids a read-then-delete replay race. See [B1 handoff](socket_authentication_handoff.md) for exact tests, migration/configuration and verification gaps.

## 13. B2 persistence and lifecycle implementation

### Historical boundary and state ownership

Migration `0022_interview_analysis_video_lifecycle` adds fields/models/constraints without replacing users, duplicating audio fields, changing scores, or copying historical observations into jobs. Its AddField gives existing sessions `lifecycle_version=0`; a subsequent AlterField sets the Python default to 1 for new sessions. There is no adoption/backfill of old sessions. Legacy fields and historical aggregate values remain intact. New defaults on historical metadata are unmeasured placeholders, not evidence of a previously executed pipeline. Publishers for version-0 sessions are denied until a separately reviewed adoption policy exists; B2 recovery skips those rows. Observer ownership still uses B1 rules, but B3/B6 must not present version-0 defaults as a measured report.

For managed video sessions, state is held on the existing `InterviewSession`:

| State | Values and ownership |
|---|---|
| `connection_state` | `not_connected` → `connected` ↔ `disconnected` → `closed`; publisher transport only. Observers have independent B1 leases and no publisher ownership. |
| `video_state` | `not_started` → `active` ↔ `disconnected` → `finalizing` → `ended`. `finalizing` means video admission is sealed while admitted jobs drain. |
| `analysis_state` | `pending` → `processing` → `finalizing` → `complete` / `partial` / `failed`. B2 emits terminal `failed/no_usable_evidence` when sealed jobs drain with zero processed observations. Complete/partial measured reports are B6. |

`started_at`, `audio_ended_at`, `ended_at`, `video_required` and the existing completion coordinator retain their roles. `ended_at` is the first **seal time**, never a disconnect timestamp. Audio-only sessions continue to set `ended_at` from verified audio completion using the existing service and do not enter B2 video recovery. The old `mark_video_ended` entry point remains a trusted explicit-finish compatibility hook; consumers must never call it from disconnect callbacks.

`video_activated_at` is initialized from the original session `started_at`, including when no camera ever connects. The absolute `video_deadline` is activation plus 3600 seconds by default. The inactivity baseline is `last_frame_at` (server admission time), or activation until the first admitted frame. Connecting, reconnecting, pings, capacity drops and retries do not move this baseline. No-frame managed interviews therefore have a terminal recovery path. Pending/uncertain voice creation without a `started_at` is still handled by existing Retell creation recovery, not by pretending video began.

### Publisher generations, auth and transport leases

B3 calls `acquire_publisher_async(lease)` only after a B1 publisher ticket has been redeemed. The service rechecks the B1 lease/current account/ownership and takes parent/session row locks. A currently live publisher lease rejects another publisher with `publisher_conflict`. A disconnected publisher, or one whose transport lease has expired but whose grace/inactivity/duration windows have not, can be replaced. Each acquisition returns a new server UUID generation and resets only that generation's sequence cursor. It does not restart the interview or video timers.

Transport lease: `min(now + INTERVIEW_ANALYSIS_CONNECTION_LEASE_SECONDS, B1 authorization expires_at)`, default 15 seconds. Acquisition also persists the generation's original `connection_auth_expires_at`. `heartbeat_publisher_async(lease, generation)` rechecks authorization, session eligibility, current generation and unexpired transport ownership, then renews only transport ownership under that original stored bound. Even supplying a newer credential to this internal function cannot extend the original generation authorization. B3 must call it on the frozen 5-second ping cadence; frame admission alone does not renew transport ownership. Failed auth checks must lead B3 to call the fenced disconnect service and stop capture/admission.

`disconnect_publisher_async(session_id, generation)` records `disconnected` and a grace deadline, without ending video or the interview. It is allowed after credential expiry because cleanup is fenced by the trusted consumer's server generation, not by a still-valid JWT. Repeated or stale generation callbacks return false without changing the current publisher. Recovery detects a lost transport lease even if no callback arrives. Grace is anchored to the earlier of callback time and the expired lease deadline, plus 30 seconds; a delayed recovery pass never grants fresh grace. Reconnect attempts past a deadline are denied even if the recovery scanner has not sealed yet.

All policy timers remain independent. The earliest elapsed duration, reconnect-grace or frame-inactivity deadline wins, with duration then reconnect then inactivity as equal-time tie breakers. Inactivity can therefore end the recoverable window before the full 30-second disconnect grace if the last admitted frame is older. A verified persisted audio end takes precedence when the first seal is evaluated. Once sealed, its first reason is immutable. For B3, add post-accept protocol reason `stale_connection` (4409) for a superseded/expired transport generation; `stale_finalization` is an internal worker error and is never a browser protocol message.

### Implemented finish-video and sealed boundary

`POST /api/interview-sessions/{uuid}/finish-video/` uses the existing Clerk authenticator, JSON parser and no-store/error envelope from B1. Request must be exactly `{}`. Only the active owning candidate may finish; owning recruiters and unrelated users are denied. An unstarted/non-live or legacy session cannot be newly sealed by this API. Audio-only returns 409 `video_not_enabled`.

Success is HTTP 200 with exactly:

```json
{"session_id":"UUID","video_state":"finalizing","finish_reason":"candidate_finished"}
```

`video_state` is `finalizing` or `ended` according to the current drain barrier. A repeat may advance from finalizing to ended but never changes seal time, admission boundary or first reason. Existing B1 errors/statuses and no-store behavior apply. A missing and an inaccessible session both return access denial.

Every sealing writer takes **Interview → InterviewSession** locks, then records `admission_sealed_at`, `sealed_admission_count=admitted_count`, first `finish_reason`, closed transport and `ended_at` atomically. The shared completion service may mark the interview DONE once audio/video completion permits it, even while analysis is finalizing. DONE does not mean a measured report exists. Admission and sealing share those locks: a racing frame is either committed inside the sealed count or rejected. No later admission is allowed, including when old B1 tickets remain unexpired.

### Durable jobs, counters and fencing

`FrameJob` is admitted metadata, distinct from the existing `FrameAnalysis` observations. It contains a UUID ID; session, UUID generation, safe integer sequence; session-monotonic admission number; untrusted finite client `capture_ts`; server `received_at`; processing deadline; state; claim token/lease/attempts; and terminal time/reason. No raw frame, base64 body, application token or ticket is stored. The future private payload key can be derived from the job UUID by B4.

Unique constraints enforce `(session,generation,sequence)` identity and `(session,admission_number)`. Partial unique constraints permit at most one pending and one processing job per session. The singleton `FrameAdmissionGate` row serializes admission's global nonterminal-count check, default 32. Lock order is **admission gate → Interview → InterviewSession → FrameJob → observation/report**. Only admission needs the gate. Never invoke admission while already holding parent/session locks. Workers and recovery lock the parent/session before changing jobs; terminal transitions only reduce global occupancy.

`admit_frame_async(lease,generation,sequence,capture_ts)` is an **internal metadata interface**, not a public socket endpoint. It returns `(job,None)` or `(None,"capacity")`. It rechecks account, B1 lease, current publisher generation, live/unsealed session and deadlines. Existing admitted identity returns the same row without new counters; lower unseen sequence is rejected. B3 must still enforce strictly increasing wire sequences and image/rate/schema limits before calling it. A dropped capacity sequence is remembered by the generation cursor, so resubmitting it does not increment counters again. Replacing the previous pending row marks that old admitted row `superseded` and timestamps it before admitting the latest row. Superseded work is never a completed observation and remains in the durable ledger.

Only session `received_count`, `admitted_count`, `dropped_count` are incremented at admission, atomically with job creation/replacement. A check constraint enforces `received=admitted+dropped`. Malformed, unauthorized and stale protocol traffic is outside these counters. Other terminal/coverage counts are derived from distinct job rows (and their one-to-one observations), not incremented on task attempts. At the final boundary `admitted=processed+superseded+failed+expired`. `valid_gaze` counts processed observations with centered/away gaze, while other processed gaze is unknown. Capacity drops are never included again among admitted terminal outcomes. The existing `(session,ts)` observation index is retained.

`claim_frame(job_id)` returns a new UUID claim token or none. Processing claims expire after at most 30 seconds and never beyond the admission deadline (default 60 seconds). A still-live claim or another processing row blocks a claim. Retrying the **same** job after its claim expires rotates its token; an old worker cannot settle after rotation or expiry. Already admitted jobs retain their immutable generation and may still finish after socket reconnect/seal; they are fenced by their worker token, not the new publisher generation.

`settle_frame` accepts only `processed`, `failed`, `expired` from workers. Failed/expired reasons are `worker_failed`, `inference_failed`, `payload_missing`, `deadline_expired`; pending replacement uses `superseded`. Actual processing must persist an observation and terminal transition in one transaction with the valid claim. The B2 persistence interface accepts exactly gaze direction (`centered`, `away`, `unknown`), face count (0..100) and at most 20 normalized person/phone detection objects (`label`, `confidence`, `box`), plus nonempty pipeline/model/rules versions. It implements no detection or inference and leaves expression unavailable. B5 must extend quality/unknown semantics explicitly before real model integration. Tests use `test-only` provenance; no test inference is exported as real ML output. An identical terminal retry does not write again; a different outcome or stale token is rejected.

### Drain, aggregate provenance and recovery

A session is drain-ready only after sealing, no pending/processing rows remain, and its job count equals the sealed boundary. Boundary mismatch is an invariant failure, never permission to invent missing outcomes. Once drained, video state is ended and `analysis_ready_at` is a durable B6 handoff. Zero processed rows produce `analysis_state=failed`, `analysis_failure_reason=no_usable_evidence`, one finalized revision and a timestamp, with **no invented BehavioralAnalysis/percentage**. Some processed observations remain finalizing for B6's real aggregate; B2 does not pretend to compute complete/partial reports.

`claim_finalization` returns a UUID lease (default 30 seconds) only at the sealed/drained barrier. `finalization_write_lock(session_id,token)` checks current lease, readiness and boundary under parent/session locks and locks any existing aggregate. B6 must compute outside these locks and commit its real result **inside** that context, setting revision/finalized time and matching sealed boundary/provenance atomically. The context rejects stale/replaced claims and rolls back a write held past its lease. A finalized session cannot be reclaimed. New `BehavioralAnalysis` fields hold pipeline/model/rules versions, revision, finalization timestamp, sealed admission count and coverage snapshot. Historical rows keep blank/zero provenance; B2 never rewrites them. The legacy fake aggregate task returns unavailable for managed sessions and dispatches no scoring. Replacing the historical stub and governing measured report/scoring use remains B6; no B2 task publishes fake results or invokes that stub as a finalizer.

Verified Retell inbox processing and the preserved registered transcript worker seal managed video within their existing parent/session transaction when they persist audio completion. First video-finish reason is retained if already sealed. Recovery also applies up to two pending already signature-verified inbox events for each managed session through the existing canonical `voice.apply_event`, before taking its own locks; it does not reimplement webhook verification or call a provider. This handles missed per-event broker publication without a browser callback. Existing Retell retry/creation reconciliation remain intact.

`interview_system.tasks.video.recover_video` is registered for Beat every 5 seconds, with stale scan messages expiring after that interval. `manage.py recover_video` performs the same pass directly without Redis/Celery. Each pass examines at most 100 due managed video sessions, ordered by indexed `recovery_due_at` and ID, and advances their next-check time for fair subsequent scans. It detects expired transport/grace/inactivity/duration/audio state, seals under shared locks, marks overdue pending/processing jobs expired, and rechecks the drain barrier. New admissions and claims do not depend on broker task order. This is a bounded metadata scanner, not B4's frame scheduler or broker-publication recovery.

The intended recovery latency is one 5-second healthy scan interval, plus backlog, task queue/DB/scheduling latency. It is not a hard real-time or outage guarantee. Broker/worker outage requires an independently invoked recovery command or restored workers; a missing provider webhook still depends on existing Retell provider reconciliation. No inference, report generation, external scheduler installation or deployment occurred in B2. See [B2 handoff](video_lifecycle_handoff.md) for exact results and gaps.

B2 locking and recovery behavior were checked against [Django row-lock semantics](https://docs.djangoproject.com/en/6.0/ref/models/querysets/#select-for-update), [Django transactions](https://docs.djangoproject.com/en/6.0/topics/db/transactions/), [PostgreSQL locking](https://www.postgresql.org/docs/current/explicit-locking.html), and [Celery periodic tasks](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html). Locks are held for transactional mutations; race tests use real PostgreSQL TransactionTestCase connections. Periodic task overlap is possible, so the shared state transitions are idempotent and fenced rather than relying on one scanner or task queue order.

## 14. B3 implemented transport boundary — 18 October 2026

### Routing, origins, credentials and groups

ASGI now dispatches HTTP to the existing Django application and the two frozen UUID WebSocket routes to distinct publisher/observer consumers. No session/password/JWT replacement authentication stack was added. Ticket redemption calls the existing B1 verifier-backed ticket services through a synchronous database boundary; socket messages never provision identities or authenticate from decoded JWT claims.

Each handshake requires one `Host` allowed by Django `ALLOWED_HOSTS` and exactly one serialized `http://` or `https://` Origin matching `INTERVIEW_ANALYSIS_WS_ALLOWED_ORIGINS` by scheme, hostname and effective port. Missing/null origins, duplicate headers, wildcard settings, credentials, paths, query/fragment components and malformed/zero ports are rejected. Explicit default ports normalize to 80/443. The default origin list is the existing localhost frontend ports; CORS DEBUG wildcard behavior does not relax sockets. Add the actual frontend origin explicitly in environment configuration. Native clients without Origin require a separately reviewed policy, not a permissive fallback.

The query must be exactly one literal `ticket=URL_SAFE_SECRET` (43 unreserved characters). Duplicate/unknown parameters, percent-encoded aliases, JWT query parameters and malformed/oversized ticket values are denied. The middleware validates route shape and origin before consuming a ticket. Before entering Channels it removes the raw query/path, credentials/cookies/other headers and unused subprotocol strings from the consumer scope. The consumer removes the temporary ticket field before continuing. No query credential or frame body is logged by these services.

After atomic B1 redemption, Redis connection reservation and B2 publisher acquisition/observer authorization complete **before accept**. A valid ticket does not reserve a publisher slot or guarantee connection acceptance. Failed pre-accept handshakes have no JSON or browser-visible custom close-code guarantee. Friendly ownership/auth errors remain available from HTTP ticket issuance. A ticket consumed during a failed later reservation/connection must be replaced.

Publishers have no observer group membership. Observers alone join `analysis.observe.{session_uuid}` after authorization, and leave it on close/revocation. The client cannot send subscribe/group commands. Snapshot delivery rechecks current authorization/reservation before fetching and again before sending. Direct analysis notifications addressed accidentally to a publisher are ignored. Channel notification fields are never forwarded to browsers.

### Limits and actual frame validation

The section 12 ready/frame/ack/ping/error shapes remain in force. B3 adds pre-accept connection-limit denial internally and post-accept reason `connection_limit` (4409) if needed by future integrations. `stale_connection` retains B2's 4409 category. Message abuse closes with `invalid_message`/1008; unsupported versions, duplicate/out-of-order sequences and binary messages are protocol errors. Errors contain only the fixed `type`/`reason` envelope. Application-detected full-message excess closes with `message_too_large`/1009 when a frame can still be sent; transport-level termination can be an abrupt disconnect without a close frame.

Defaults:

| Limit | Enforced value / scope |
|---|---|
| Incoming complete message / single wire frame | 307200 bytes each; application and bounded Daphne entry point |
| Decoded JPEG | 204800 bytes |
| Width / height / total pixels | 1280 / 1280 / 921600 |
| Frame rate | Session 3 FPS, user 6 FPS, burst one, shared across reconnects/processes |
| Message rate | Session 20/sec, user 30/sec, burst equal to one second of allowance |
| Authenticated connection reservations | User 4 across roles/sessions; session publisher 1; session observers 4; global 64 |
| Redis guard lease | 15 seconds, additionally capped by B1 credential authorization |
| Watchdog cadence | At most configured B1 interval, guard lease / 3 and B2 transport lease / 3; defaults 5 seconds |
| I/O / decode wait | 2 seconds per operation / 2 seconds |
| JPEG workers / queued decode jobs | 2 per process / zero |
| Daphne queued ASGI input messages | 2 per socket, plus current handler |
| Channel notification capacity / expiry / group expiry | 16 / 10 seconds / 120 seconds |
| Outgoing JSON envelope | 65536 UTF-8 bytes; snapshot window still at most 100 observations/20 boxes each |

Limits are resource policies, not measured concurrency/performance claims. Shared Redis Lua scripts use Redis TIME, atomic multi-key checks and TTLs; they hold only connection IDs/counts/rate tokens. All ASGI processes must share one Redis primary and prefixes. This implementation is not configured for Redis Cluster multi-slot scripting. Reservation loss or Redis failure stops an existing socket on the next before-use/watchdog check. Reserve/refresh never bypass the original B1 expiry. No production in-memory fallback exists; tests explicitly override only notification delivery where stated.

Incoming JSON must be an object with exact allowed fields, unique keys, finite numbers, integer protocol/sequence fields (booleans rejected), and no NaN/Infinity constants. `capture_ts` remains untrusted metadata, never identity or lifecycle authority. Sequence is strictly increasing within the trusted consumer generation, including dropped frames. A per-generation client replay is an error, while internal B2/B4 retries remain idempotent.

After inexpensive schema/rate checks, a shared nonblocking worker slot validates canonical strict base64, JPEG SOI/EOI, JPEG-only decoding, header dimensions, verify() and actual raster load(). Oversize dimensions fail before raster allocation. Pixel validation does not perform face/object/model inference. The consumer captures an aware UTC server receive timestamp separately from client capture time and passes it with the validated JPEG and generation to the admission interface. Expensive base64/JPEG work runs off the event loop. Malformed images never reach admission.

If all decoder slots are occupied, the frame is capacity-dropped without adding a decode job. A timed-out/cancelled waiter does not free a worker slot until the actual decoder exits. Threads cannot forcibly terminate a wedged native decoder; the pool remains bounded and can become unavailable until worker/process recovery. No raw frame is persisted, backed up, sent over Channels/Redis, logged or submitted to an external API in B3. Active raw buffers exist only transiently in the bounded wire queue/current handlers/decoder/admission arguments.

### Required bounded ASGI server

Use the provided entry point rather than a generic server invocation when relying on B3's buffer bound:

```powershell
.venv\Scripts\python.exe -m ai_interview_recruitment.socket_server -b 127.0.0.1 -p 8000 ai_interview_recruitment.asgi:application
```

This is a local run instruction, not a deployment performed by B3. It subclasses the pinned Daphne 4.2.2 application-queue construction for WebSockets only; HTTP follows upstream unchanged. Defaults bind both `--websocket-max-message-size` and `--websocket-max-frame-size` to 307200, handshake timeout to 5 seconds, socket duration to 60 seconds, and verbosity to 0. Do not disable/increase transport limits or replace this entry point without retesting the bounds.

The bounded server places an internal `interview_analysis_bounded_input=True` marker in the ASGI scope. The socket security entry requires that marker, so a generic/unbounded server invocation rejects analysis handshakes while continuing to serve HTTP. A client header cannot supply it. The custom server rejects disabled or oversized wire-limit options. Another server may be integrated only after its queue/size behavior is verified and its trusted scope integration explicitly implemented; never add an unconditional marker inside application middleware.

Installed Daphne forwards those two size controls to Autobahn's complete-message/frame options, but upstream creates an unbounded ASGI input Queue. The provided queue retains at most two input messages; overflow clears buffered frames, closes the protocol, mutes further input and delivers a disconnect to trigger B2 cleanup. Normal disconnect also discards queued images. Application checks remain necessary on any ASGI server; size checks alone do not bound a server's queued messages.

Actual localhost wire tests verified HTTP dispatch, origin denial without spending the ticket, valid transport/drop feedback, rejection of an oversized fragmented message, rejection of an oversized single frame, queue-overflow closure and absence of ticket values in captured Daphne access output. Oversize Autobahn termination sometimes arrives as an abrupt connection close, so client receipt of 1009 is not required. Tests used an ordinary Python WebSocket client with compression disabled; they are not real-browser, TLS/proxy or hardware verification. Installed Autobahn's default compression acceptor declines offers; no compression policy was enabled by this entry point. The server subclass depends on pinned internals and must be revisited on Daphne upgrades.

### Authorization watchdog, I/O and disconnect semantics

Authorization is rechecked before each client operation, after JPEG decoding, before admission acknowledgement, and before every observer snapshot. The independent watchdog also runs when no client messages arrive. It rechecks B1 account/subject/ownership/session state, B2 publisher generation/transport policy and Redis reservation; observers refresh group membership. Client pings renew only B2 transport ownership under the generation's original stored authorization cap. The watchdog never renews transport ownership automatically. Logout/revocation limits from B1 still apply; no online Clerk revocation introspection was added.

The watchdog interval is clamped so guard/transport leases cannot be silently outlived by a longer configured auth polling interval. On failure the consumer fences further admission/delivery, removes observer membership, calls B2's generation-specific disconnect for publishers, releases its own Redis reservation and closes. It never calls mark_video_ended on disconnect. Failed cleanup is recoverable through B2's expiring leases/scanner. Cleanup of a late timed-out publisher acquisition uses that acquisition's exact generation, not the currently active generation.

All synchronous database services are behind Channels `database_sync_to_async`, with PostgreSQL statement/lock time budgets restored after each call. Redis uses async I/O with connection/socket timeouts; application I/O is also bounded by a wait deadline. Cancelling an await cannot forcibly cancel already executing synchronous SQL. B2's transactional fencing, statement/lock budgets and late-acquisition compensation handle this honestly; a hung runtime/DB has no hard real-time close guarantee. No new authentication/identity migration or HTTP frontend auth interface change occurred.

### B4 admission and B6 observation interfaces

`frame_admission.submit_frame(lease,generation,sequence,capture_ts,received_at,jpeg)` is asynchronous and receives only a validated private transient JPEG plus verified/fenced context. It returns `AdmissionDecision(status,reason)` where accepted has no reason, and dropped uses the frozen `capacity`, `rate_limited`, `stale` enums. **The production B3 implementation always returns dropped/capacity.** It creates no frame job, stores no payload and publishes no task because B4's private-payload/publication recovery does not yet exist. Ready therefore does not imply analysis availability. Tests patch this seam explicitly; no production test backend/configuration or fake inference result is provided.

B3 counts well-shaped transport/availability drops through `record_frame_drop` under B2's locks, once per increasing generation sequence. Rate/decoder-capacity drops may be counted without decoding the refused image; their outcome is a transport drop, not a claim that image content was valid or analyzed. Malformed protocol/image errors are excluded. `received=admitted+dropped` remains enforced. This drop service never creates pending jobs, advances frame inactivity, or double-counts an attempt already handled by B4's B2 metadata admission. A default B3-only session receiving only dropped frames still reaches B2's no-admitted-frame inactivity policy.

B4 must recheck authorization/generation/session eligibility atomically at actual admission, coordinate B2 metadata with private short-lived payload ownership and recover broker publication failure before returning accepted. It must preserve server receive time distinctly from untrusted capture time and reuse the existing job ledger/claim fences/counter definitions. Accepted remains admitted, never analyzed. No raw image belongs in broker task args or observer notifications. Consumer I/O cancellation must not strand admitted work; durable B4 state must explain it. Do not replace the default adapter with metadata-only acceptance in production.

`observer_delivery.get_snapshot(lease)` currently returns None. B6 must supply an authorized persisted projection matching the frozen exact envelope; B3 validates allowed fields/enums, UTC timestamps, bounded normalized boxes, finite confidence, counts, window and output bytes before sending. Notifications act only as a wake-up to fetch that projection, never as an arbitrary browser payload. Subscribe first, then apply the highest revision; equal/older revisions are ignored. B6 must trim its bounded recent window to the output byte limit and implement authoritative reconnect/status/report retrieval. No measured observation/report is fabricated by B3, and candidate messages contain transport fields only.

References verified: [Channels security](https://channels.readthedocs.io/en/stable/topics/security.html), [channel layers](https://channels.readthedocs.io/en/stable/topics/channel_layers.html), [database access](https://channels.readthedocs.io/en/stable/topics/databases.html), [channels-redis 4.3.0](https://github.com/django/channels_redis/blob/4.3.0/README.rst), [Daphne 4.2.2 source](https://github.com/django/daphne/blob/4.2.2/daphne/server.py), [Autobahn options](https://autobahn.readthedocs.io/en/latest/websocket/programming.html), [Pillow image validation](https://pillow.readthedocs.io/en/stable/reference/Image.html), [Redis async cleanup](https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html), [Redis scripting](https://redis.io/docs/latest/commands/eval/) and [ASGI WebSockets](https://asgi.readthedocs.io/en/stable/specs/www.html). See [B3 handoff](frame_transport_handoff.md) for exact commands, versions/results, environment notes and outstanding target-environment checks.

## 15. B4 implemented scheduling, payload and recovery contract

Implemented 18 October 2026. This section supersedes the B3 production admission placeholder above. It implements scheduling infrastructure, not real ML. No scoring/ranking policy, Clerk provisioning, frontend authentication or browser protocol change is included.

### Admission, payload ownership and bounded work

`frame_admission.submit_frame` retains the B3 signature and frozen accepted/dropped envelope. With `INTERVIEW_ANALYSIS_VISION_ENABLED=False` (default), it still returns dropped/capacity and creates no job. Enabling B4 is an explicit operator configuration; until B5 implements `vision_inference.analyze_jpeg`, an enabled job terminates with inference_failed, never synthetic observations. There is no configurable production test-inference backend.

With admission enabled, validated JPEG retention occurs under the existing gate -> Interview -> InterviewSession locks, through `video_lifecycle.admit_frame`'s retention hook. The service atomically rechecks B1/B2 ownership, account, generation, connection lease and seal policy, then assigns the durable frame identity/ordinal. The aware server receive timestamp supplied by the consumer is persisted separately from untrusted capture_ts; expiry is based on the earlier of receive/admission time plus 60 seconds, so delayed processing does not refresh its lifetime. Accepted means committed admission with payload retained at that moment; later replacement, payload loss or worker failure remains possible.

There is at most one processing and one latest pending row per session and 32 active jobs globally by default. A replacement terminally marks the previous pending row superseded in the same PostgreSQL transaction, with no observation and no retry increment to received/admitted counters. Processing rows are never replaced. `received=admitted+dropped` and the sealed admission boundary retain their B2 definitions. Internal repeats of an admitted identity return its existing row. Drops after storage failure are counted once by B3's existing drop service. Payload failure rolls back the prospective job, replacement and counters.

Raw JPEG bytes are stored only in the private `INTERVIEW_ANALYSIS_PAYLOAD_REDIS_URL` store, under an opaque job UUID. They are not task arguments, channel-layer messages, result-backend values, report fields or public cache entries. Atomic Redis slot reservation permits at most 64 retained JPEG keys, including orphans from rolled-back transactions and failed cleanup. Every image has a TTL no longer than its remaining frame deadline. Terminal/replaced payloads are deleted promptly where possible; cleanup failure leaves a bounded TTL orphan. A slot index expires after its longest member TTL, and pruning does not extend image lifetimes. Old image slots still count while a replacement is being retained, so a full payload store can reject a replacement rather than exceed capacity.

PostgreSQL and Redis do not share a transaction. Redis failure before commit rejects admission; process/transaction failure after retention can leave an orphan, which the independent payload cap and TTL cover. Acknowledgement cancellation cannot roll back a committed job: the persisted job is sufficient for recovery without the caller or browser.

The Redis URL's default localhost database 14 is a development address, not evidence of an appropriately isolated production store. Production requires a private dedicated primary, application-only ACLs/network access, appropriate TLS, and persistence/backups disabled for raw payloads. Separate Redis database numbers alone do not provide those guarantees. No Redis server security/persistence configuration was changed or certified by B4; no raw frame files or public media objects are created. Redis Cluster multi-slot scripting is not supported.

| Resource | Default bound |
|---|---|
| Active jobs / session | 1 processing + 1 pending |
| Active jobs globally | 32 |
| Raw payloads, including TTL orphans | 64 x 204800 bytes = 12.5 MiB image bytes, plus Redis overhead |
| Dedicated vision broker list | 128 messages x at most 8192 serialized bytes = 1 MiB, plus Redis overhead |
| Worker | Explicit dedicated subscription; concurrency 1, Redis disable-prefetch, multiplier 1 |
| Broker list idle TTL | 120 seconds; Celery message expiry also equals frame deadline |
| Publications / job | At most 3; new publication token every attempt; no Celery publish retry |
| Worker claims / job | At most 2 in B4 execution |
| Publication lease / transient backoff | 10 seconds / 2 seconds, eligible on next scanner pass |
| Processing claim / soft and hard task limit | 30 seconds / 20 and 25 seconds (production prefork worker) |
| Absolute admitted-frame deadline | 60 seconds from server receive/admission, whichever is earlier |
| Ledger rows / session | 10800 admitted identities, including terminal replacements; further attempts are capacity-dropped |
| Recovery scan | Existing indexed due-session batch of 100 every 5 seconds, or management command |

These are logical resource bounds, not measured throughput/RSS/GPU performance. Broker-reserved/unacked metadata and one active decoded/model input are additional to the list/payload bounds. With the provided single worker and prefetch disabled, only the current task is requested; startup/restoration races are still fenced by the ledger and capped Redis restoration. Adding workers increases runtime/model memory and must be measured. Other Celery queues and their legacy workloads are outside the dedicated list cap. Terminal metadata remains a durable explanation of admitted outcomes; it is bounded per session, not globally over the lifetime of the application. No historical ledger/report retention deletion is performed.

### Publication, worker fencing and durable recovery

The existing FrameJob row is the publication intent: `payload_ready`, `publication_token`, `publication_attempts`, `publication_due_at`. There is no growing retry/outbox table. Migration 0023 adds these fields without touching identities or historical observations. Pre-B4 jobs default to payload_ready=false; recovery can expire them but never invent a retained image.

ASGI performs no broker operation or inference. After a committed job becomes due, the existing recovery scan (or a finishing worker) chooses the expired processing claim first, otherwise the pending job only when no processing claim is live. It locks parent/session/job, records a new publication token and next due time, commits, then publishes `process_frame(job_uuid, publication_uuid)` to `interview.analysis.vision`. Task arguments contain only these references. The current token must match before claiming; stale, duplicate, replaced or terminal deliveries do not reach inference. The claim token and claim/job deadlines must still match when writing. A new connection generation never mutates an old job's identity; work admitted before reconnect/seal can finish, while new admissions require the current connection generation.

Successful delivery is deliberately not assumed from a broker return value. Death after intent commit, failed publication and uncertain broker success all recover after the publication lease, subject to the attempt/deadline caps. Re-publication rotates its token, fencing older unclaimed deliveries. The PostgreSQL parent lock prevents two simultaneous dispatchers publishing the same attempt. Pending replacement can leave stale broker references; the Redis list cap bounds those independently of the active-job cap.

The pinned Kombu Redis transport enforces atomic length-check/push and an 8192-byte serialized message limit on vision publication, at priority zero only. Broker restoration also uses the bounded push script: an over-capacity restore may discard a reference, with PostgreSQL recovery responsible for its job. Queue expiry and Celery expires are not used as the sole backlog bound. All publishers/workers must use this configured transport; an external producer bypassing it is outside the guarantee. Do not add priority lists, raw image arguments, direct LPUSH producers or eager execution. `dispatch_session` refuses eager execution rather than running a model in the recovery process. The extension depends on pinned Kombu 5.6.2 internals and must be retested on upgrades.

The vision task uses early acknowledgement, ignore_result, no Celery retries and no reject-on-worker-lost loop. This is intentional: the durable claim/deadline scan recovers loss, rather than depending on Redis visibility-timeout redelivery. After an early-acked worker dies, a valid live claim blocks overlapping processing; after its lease expires a new publication/claim can recover, up to two claims total. The dedicated prefork hard time limit is shorter than the claim lease. Do not shorten the claim below task runtime limits. Physical native-runtime termination and production prefork behavior were not verified on this Windows host; the actual Celery integration test used a solo test worker. Stale live native computation may consume resources, but cannot commit after its lease expires.

Transient payload Redis errors and explicitly typed TransientInferenceError release only the current valid claim for bounded retry after a short delay. A permanently missing payload is payload_missing, not infinitely retried. Corrupt input, unavailable inference, invalid observation/provenance and other permanent inference errors become inference_failed without automatic retry. Exhausted claims become worker_failed; exhausted publication attempts become publication_failed (new internal terminal reason). The existing outcomes remain processed/superseded/failed/expired; deadline_expired remains authoritative when the absolute deadline is reached. Failed payload cleanup is not a reason to rewrite a terminal outcome. Errors/logs do not include raw bytes, Clerk tokens, ticket values, broker URLs or arbitrary provider exceptions.

The existing recovery scan first applies B2 policy, verified audio-event recovery and deadline expiry, then attempts B4 dispatch. Database deadline recovery needs neither a functioning broker nor payload store to terminally account for jobs and drain a sealed session. Broker attempts are outside session locks and have configured connection/socket timeouts and no publish retry. Recovery can also run with `manage.py recover_video` when beat/default workers are unavailable. Neither recovery nor disconnect fabricates observations. Session DONE and analysis readiness remain separate, and aggregation must use the sealed durable ledger barrier, not queue order. Default-queue B6 aggregation has no GPU dependency. No latency claim is made for existing default-queue recovery under unrelated long CPU work; measure and isolate control/notification work if necessary before production.

### B5 interface and verification boundary

Implement real local `vision_inference.analyze_jpeg(data) -> (observation, versions)` inside the dedicated worker only. Load model/GPU dependencies lazily inside that function; importing the interface/exception definitions during default-queue recovery must not require GPU access. Return the exact B2 observation schema and nonempty pipeline/model/rules provenance, extending quality/unknown semantics explicitly in the shared contract before adding fields. Use TransientInferenceError only for a known retryable local resource condition; permanent decode/model/input failures must not request repeated retries. Do not supply a production synthetic backend, perform inference in the consumer, silently change scoring or bypass settle_frame's claim fence. Add bounded native/model memory and measured runtime before enabling admission for real interviews. B6 owns real persisted aggregation and report/observer projections; it must wait for the sealed drain barrier and cannot require GPU access.

Tests use synthetic JPEG fixtures and explicitly patched deterministic TEST-ONLY inference with `test-only` versions. PostgreSQL transaction races, real Redis TTL/Lua/list bounds, and an explicitly subscribed real Celery solo worker are exercised; provider, browser camera, GPU, production prefork kill/timeout and deployment verification are not claimed. Exact commands/results and the changed-file/configuration list are in [B4 handoff](vision_scheduling_handoff.md).

Official behavior checked: [Celery 5.6 routing](https://docs.celeryq.dev/en/stable/userguide/routing.html), [prefetch and early/late acknowledgement](https://docs.celeryq.dev/en/stable/userguide/optimizing.html), [worker-loss/limits configuration](https://docs.celeryq.dev/en/stable/userguide/configuration.html), [Redis visibility-timeout caveats](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html), [Redis SET TTL](https://redis.io/docs/latest/commands/set/) and [Redis scripting](https://redis.io/docs/latest/commands/eval/). Installed Kombu 5.6.2 publication/restoration source was inspected and the overridden paths exercised against Redis.

## 16. B5 local vision and pure episode rules

Implemented 18 October 2026. The user explicitly chose **expression disabled**, supplied no approved footage, prohibited downloading interview footage, and requested a synthetic harness plus a proposed local `tests/fixtures/vision/` fixture directory. This scope implements the runtime and mechanisms while leaving the real-clip accuracy gate outstanding. No synthetic fixture, result or benchmark counts as real-world accuracy validation. No public or existing interview recordings were used.

### Runtime and artifact freeze

`ai/vision/` contains no Django imports. Importing runtime/rules/types does not import MediaPipe, ONNX Runtime, OpenCV or load models. Actual model loading is lazy inside a dedicated execution process. The runtime is CPU-only in B5: MediaPipe 0.10.21 FaceLandmarker Tasks IMAGE mode and ONNX Runtime 1.20.1 CPUExecutionProvider running YOLOv8n COCO80. Python 3.12.7, NumPy 1.26.4, opencv-contrib-python 4.11.0.86 and Pillow 11.2.1 were exercised on Windows. `requirements-vision.txt` pins core/ABI dependencies; `requirements-vision-win-py312.lock` records the exact measured isolated Windows environment. Linux production images/prefork/GPU behavior are not certified by that Windows lock or these measurements.

PyTorch 2.5.1+cpu, TorchVision 0.20.1+cpu, Ultralytics 8.3.70 and ONNX 1.17.0 are separate **export tooling**, not required in the final vision runtime. This avoids MediaPipe/Ultralytics pulling two overlapping OpenCV distributions into one runtime. The measured final environment has only opencv-contrib-python, and no torch/torchvision/ultralytics/TensorFlow/FER distributions. MediaPipe's bundled TensorFlow Lite/XNNPACK CPU delegate can log its name; that is not an enabled emotion/FER runtime or a TensorFlow Python installation. Existing web requirements, including legacy Phase 5 dependencies, are preserved; vision requirements are not added to the web image. Full Django/Celery worker-image packaging remains a deployment concern; the measured pure runtime does not include their memory overhead.

Artifacts are pinned by bytes and full SHA256 in `ai/vision/model_manifest.json` and checked before any native model parser:

| Artifact | Bytes | SHA256 |
|---|---|---|
| MediaPipe FaceLandmarker float16 version 1 | 3758596 | `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff` |
| YOLOv8n ONNX, fixed 640, batch 1, opset 12 | 12823570 | `6f987f27fdd41b29c907c81e2ffa56de79f5096cc17dfde929b3fbeb2fdc8d0a` |
| YOLO source .pt, export tooling only | 6549796 | `f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36` |

The manifest records official upstream model URLs, source/export provenance and removal of volatile export-date metadata before deterministic serialization. A second local export reproduced the pinned ONNX hash. Runtime never downloads weights or invokes pip. `python -m ai.vision.artifacts DIRECTORY` verifies existing local assets/downloads only the explicit upstream non-derived assets; a missing ONNX file requires the pinned build output or an explicit `--export-yolo` invocation in the separate export environment. Different build bytes fail closed and need review; no silent checksum update. The artifacts/virtual environments and reports containing test observation details are ignored, not public media. No model-licensing or deployment certification is implied; retain upstream license terms with packaged artifacts.

### Quality, observations and UNKNOWN semantics

The worker revalidates JPEG bytes: <=204800, JPEG envelope/format, Pillow verify/load, edge <=1280, pixels <=921600, minimum edge 64. Quality uses RGB-channel-mean brightness/contrast and a discrete Laplacian variance. Fixed vision-v1 thresholds: dark <35, bright >225, contrast <12, sharpness <25. These are initial engineering thresholds, not validated across real cameras/skin tones/lighting/glasses. Priority is dark, bright, contrast, blur. Unusable input skips both per-frame models, has face_count=null and gaze unknown/quality_unusable; no object absence or neutral expression is fabricated. Rejected corrupt images are permanent inference failures; usable images with no face detection produce an observed count of zero, not proof of physical absence or intent.

Migration 0024 adds `InterviewSession.vision_calibration` and `FrameAnalysis.vision_details` (default empty dict for historical rows) and permits NULL in `FrameAnalysis.face_count`. Existing numeric historical values, original `(session,ts)` index, emotion field, audio fields, scores, rubric and ranking policy are unchanged. The B2 settlement interface accepts its original three fields for legacy/test callers or the following B5 extension:

```text
result = {
  gaze_direction: unknown|centered|away,
  face_count: integer|null,
  detected_objects: [{label: person|phone, confidence: 0..1, box: [x,y,w,h]}],
  vision_details: {
    schema_version: 1,
    quality: {usable: bool, reason: usable|too_dark|too_bright|low_contrast|blurred,
              brightness: finite 0..255, contrast: finite 0..255, sharpness: finite 0..10000000},
    face_count: integer|null,
    face_limit: integer 2..5,
    gaze: {direction: unknown|centered|away,
           reason: uncalibrated|no_face|multiple_faces|geometry_unavailable|head_pose|
                   eyes_unusable|eyes_disagree|quality_unusable|approximate,
           horizontal: 0..1|null, vertical: 0..1|null},
    expression: {status: disabled, label: null},
    calibration: null|{horizontal: 0..1, vertical: 0..1, version: iris-baseline-v1},
    config_version: config-v1-<16 hex>
  }
}
versions = {pipeline_version: vision-v1, model_version: mp1-<hash12>-y8nonnx-<hash12>,
            rules_version: episodes-v1-<16 hex>}
```

Exact keys, finite values, availability consistency and a 4096-byte detail limit are validated before the existing fenced atomic observation/terminal write. B5 pipeline output must include details; quality-unusable results require NULL counts, UNKNOWN gaze and no detections. Historical empty details mean quality/feature availability was not captured; B6 must not reinterpret those as confirmed usable/negative evidence. A technically processed quality-rejected row still counts as processed in B2's job ledger; B6 must distinguish technical completion from usable feature coverage when deciding real report availability.

MediaPipe maximum faces defaults to **3**, configurable via INTERVIEW_ANALYSIS_MAX_FACES only in 2..5. Detection/presence thresholds are 0.6. IMAGE mode avoids video tracking histories across interleaved sessions; smoothing is not relied on (MediaPipe applies it only for one face). Face count is the number of detections, capped by face_limit. At that cap it is a lower bound, not an enumeration of everybody present. No face identity or full landmark mesh is persisted. There is no fabricated per-face/gaze confidence: B6 can expose confidence null for those observations; detector thresholds are configuration rather than calibrated accuracy probabilities.

YOLO preprocessing is fixed RGB letterbox to 640x640, fill 114, float32 NCHW /255. The verified export takes [1,3,640,640] and returns [1,84,8400]: xywh plus COCO80 class probabilities without separate objectness. Choose the best class, filter IDs 0/person and 67/cell phone, retain scores >=0.4, apply class-aware NMS IoU 0.7 over at most 300 proposals, keep at most 20 detections, undo letterbox, clip to image and emit normalized xywh. cell phone/cellphone/mobile phone aliases normalize to phone. Confidence is the model's score, not a guarantee of true presence. No global YOLO tracker or identity history exists. No images are saved by the adapter or included in broker/channel messages.

### Approximate gaze and isolated calibration

Gaze is approximate iris position relative to eye corners/lids, not measured attention, intention, eye contact or a behavioral score. Two-eye ratios must be finite, within the eye envelope, have sufficient opening, and agree within horizontal 0.2/vertical 0.25. Tiny/closed/occluded eye geometry is UNKNOWN. A broad matrix-based pose gate rejects roughly >25 degrees yaw / >20 degrees pitch; these are camera-assumption heuristics, not calibrated 3-D pose measurements. More than one detected face returns UNKNOWN, avoiding candidate selection by size/order or cross-frame identity guesses.

Without a per-session baseline, even usable ratios produce unknown/uncalibrated. `calibrate_center` requires 5..30 explicitly camera-target samples, at least five usable ratios and bounded spread (horizontal <=0.15, vertical <=0.2). It does not automatically declare the first frames centered. `set_session_calibration` is an internal B1/B2-authorized, generation-fenced service requiring explicit target confirmation and no previously admitted jobs. The baseline is persisted per session, immutable after first admission, and copied into observation provenance. Another session receives no inherited baseline/history. No calibration HTTP/socket/UI command is claimed to exist in B5; B7 must provide that explicit capture/confirmation flow. Until then production gaze stays UNKNOWN unless an authorized internal integration supplies the calibration. Reconnect/device/camera movement does not silently recalculate it; if the capture geometry changes its prior calibration cannot be assumed valid.

With a baseline, centered means horizontal deviation <=0.18 and vertical <=0.25; otherwise away. Thresholds depend on image framing, eye/face geometry, glare/glasses, camera placement, display target and pose. No gaze confidence probability is invented. Landmarks and transformation matrices are transient; only the bounded ratios/baseline/reason needed to explain the result are retained.

### Process ownership, readiness and cleanup

B4's `analyze_jpeg(data)` boundary remains, with an optional server-owned calibration keyword. The dedicated worker entry sets INTERVIEW_ANALYSIS_VISION_PROCESS=1. The Django adapter additionally requires an actual non-eager Celery process_frame/warm_vision task context before initializing anything; ASGI/default recovery/eager calls cannot load models. The pure offline evaluation command explicitly owns its separate execution process.

Each runtime is owned by its creating PID/thread, reused sequentially, guarded against fork/thread reuse and serialized with a lock. CPUExecutionProvider is explicit, fallback disabled, ORT intra-op threads 2/inter-op 1; MediaPipe may create additional native threads. No GPU provider/state is initialized or supported by B5 config. Close releases FaceLandmarker and the ONNX session reference. Temporary pixels/tensors are local to analysis; no Python model cache keeps session images or tracker state. Native allocator remnants are not a forensic zeroization guarantee.

The dedicated queue now also registers warm_vision, with small no-image arguments, early ack, no retries and 20/25-second soft/hard limits. Worker-ready bootstrap and beat every five seconds (only configured when vision is enabled) schedule warm tasks to the capped vision queue. Warming happens inside a task in the intended child, rather than blocking Celery's four-second worker_process_init signal. Both models run on a warm-up image before readiness. A private readiness record has an enforced 15-second TTL and exact model/config provenance; admission drops capacity if that lease is missing, stale, malformed, non-expiring, mismatched or Redis is unavailable. Warm failure leaves admission unavailable; the next bounded warm task can recover. Stale/dead workers may remain represented until the short lease expires; this is not a processing-latency guarantee. Child shutdown removes only its own readiness token and closes resources. Model exceptions revoke that runtime/lease; corrupt/permanent failures retain B4's terminal policy. Claim, generation, deadline, global/session/payload/list caps remain B4's authority; ready is never permission to bypass them.

### Pure temporal rules and B6 handoff

`ai.vision.rules.evaluate` consumes one session's ordered `(server_seconds, admission_number, persisted_observation)` stream. No process-global session history is maintained. Use identical persisted inputs/configuration for both live-prefix and final folds. Duplicate/out-of-order ordinals or backward server times are rejected rather than fabricating duration. Caller must use the bounded B4 ledger and not combine sessions. Rules/config are versioned: episodes-v1 plus a hash of max_gap=1.0s, onset=2.0s, release=1.0s, enter_confidence=0.65 and retain_confidence=0.45. Config hash changes with parameters.

Episode kinds are phone_visible, multiple_people_observed, no_face_observed and gaze_away. Object thresholds have hysteresis; multiple people can be supported by at least two detected faces or two qualifying person detections. Face/gaze conditions use valid quality/geometry rather than invented confidence. UNKNOWN/unusable/legacy-without-details breaks an episode; a gap >1 second does not contribute duration or bridge an episode. Only intervals with known positive observations at both endpoints contribute observed_seconds. Negative release hysteresis never counts negative intervals. No duration is extended to disconnect, seal, worker receipt or the next unrelated session. These observations are not cheating findings or ranking penalties.

There is one stable session-scoped episode_key (`kind:first_admission_number`) and one event per episode once supported duration reaches onset. Recomputing an active prefix updates the same key; final replay is reproducible. The result retains at most the most recent 100 events plus full per-kind episode counts, supported observed seconds, truncation flag and rule version. Episode span may contain short negative hysteresis holes; observed_seconds, not end-start, is the supported duration. B6 owns persistence/authorized projection, usable/unknown coverage, report finalization and revisioned notifications. It must wait for B2's sealed drained ledger and must not run ML or require GPU during aggregation. No report/status/snapshot consumer is claimed implemented here. Candidate responses remain transport-only; expression projects as unavailable under the existing observer contract, never neutral.

### Verification boundary and next gate

The measured final CPU runtime report is [B5 CPU benchmark](vision_cpu_benchmark.json); exact commands/results and changed files are in [B5 handoff](vision_pipeline_handoff.md). It executed real pinned models on synthetic noise and warmed them explicitly; dark/flat synthetic images exercised quality rejection. It does not test accuracy for actual faces, phones, lighting, glasses, pose, multiple people or webcams. The proposed fixture README/manifest documents consented local staged clips and labels. No approved real clips were supplied, so the real-world observation/accuracy gate remains **outstanding by the user's explicit direction**, not passed by synthetic results. GPU/VRAM, full Django/Celery worker RSS, Linux prefork/native kill behavior, browser/provider/camera and deployment privacy remain unverified. Expression remains explicitly disabled as requested; no expression model/runtime/product purpose was approved and none was substituted.

Official references checked: [MediaPipe setup](https://developers.google.com/edge/mediapipe/solutions/setup_python), [FaceLandmarker options/IMAGE mode](https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker/python), [MediaPipe 0.10.21 wheel metadata](https://pypi.org/pypi/mediapipe/0.10.21/json), [Ultralytics export](https://docs.ultralytics.com/modes/export/), [ONNX Runtime sessions/providers](https://onnxruntime.ai/docs/api/python/api_summary.html), [OpenCV package selection](https://pypi.org/project/opencv-python/), [PyTorch export-tool pairing](https://pytorch.org/get-started/previous-versions/), and [Celery worker initialization signals](https://docs.celeryq.dev/en/stable/userguide/signals.html). B5 uses no hosted vision API and makes no general legal/medical/compliance/accuracy certification.

## 17. B6 real aggregation and authorized delivery

Implemented 18 October 2026. The **existing** `interview_system.tasks.analysis.aggregate_behavioral(session_id)` task now aggregates persisted admitted evidence; its old constant 94.5/two-look-away/confident/neutral implementation and automatic scoring dispatch are removed for all session versions. No parallel aggregate task or production sample generator was added. The return value is `{status,revision}` (or an explicit unavailable/pending reason), not the former fake behavioral payload. Actual report bodies are retrieved from the authorized read APIs below.

### Durable computation, revisions and finalization

Migration 0025 adds a session source revision, dirty flag and due time, plus report schema/content/source/notification state. It allows NULL legacy look_away_count so new reports never disguise unavailable counts as zero. Historical scalar values/defaults remain unchanged. A partial `analysis_report_due` index contains dirty managed video sessions only; the existing due-lifecycle and session/timestamp/job identity indexes are preserved. No auth/identity migration, data reset, destructive cutover, application migration or deployment was performed.

Every authorized receipt/drop, admission/replacement, frame terminal outcome, recovered frame expiry and seal/drain transition updates `analysis_source_revision` and sets `report_dirty` under the existing parent/session lock. Idempotent repeats do not advance it. B4 retries/claims remain authoritative. The report worker claims the existing finalization token/lease, captures source revision/SQL counters under parent locks, commits its claim, then streams the admission-ordered ledger in chunks of 200 outside those locks. CPU-only rules/schema code is used; no model/GPU/decoding dependency initializes during aggregation.

Persistence reacquires Interview -> InterviewSession -> report locks, rechecks token, source revision and lease, and verifies the sealed/terminal/count boundary for final output. Normal final writes use B2's finalization_write_lock; the lease is also checked after writing to roll back a long-held/expired writer. The same token/lease fences live writes. Changed source revisions discard the optimistic calculation rather than publishing mixed counters/evidence. Expired/abandoned computation is recoverable from the indexed due time; the streaming fold checks its own lease every 200 rows. Continuous changes can postpone a live revision until a quieter interval or sealing; five seconds is a scan/coalescing cadence, not a worst-case delivery SLA.

Live persistence increments analysis_revision once per new source revision. Repeating an unchanged live task returns the existing revision. A seal/final result gets a later revision; B2's already allocated zero-processed failure revision is reused when its explicit unavailable report is materialized. A final B6 report is immutable: duplicate/late tasks return it, late frame writes remain fenced, and no automatic reprocessing/new final revision is allowed. A future correction/version migration needs explicit policy and an audited revision mechanism; B6 does not silently reopen finalized reports.

Finalization requires a sealed session, analysis_ready_at, no pending/processing jobs and exact job count equal to sealed_admission_count. Same-queue ordering is never evidence of readiness. Zero-frame/zero-processed B2 failures can be materialized as explicit failed reports with actual transport counters and NULL feature metrics. A boundary mismatch is an invariant error, not permission to invent an outcome; recovery backs it off for 30 seconds, leaves the report dirty and requires diagnosis/repair if the ledger itself is corrupt. Interview DONE remains independent of report readiness and is still controlled by B2/provider-verified audio/video completion. Disconnect never finalizes video or a report.

The existing recover_video task/management scanner also processes up to INTERVIEW_ANALYSIS_REPORT_RECOVERY_BATCH (default 10, bounded 1..100) due reports and notification intents per pass. It invokes the existing aggregate task's run path directly on the normal CPU worker/management process. There is no per-frame Celery report publication and no extra growing outbox/queue. Report calculation needs no vision worker, broker or GPU. Claims recover after the configured 30-second finalization lease; normal live coalescing is five seconds. This path still depends on beat/normal-worker availability or an operator running recover_video; normal-worker load, DB latency and outages were not given a production throughput/SLA certification.

### Evidence and coverage semantics

Only job-linked processed observations with vision-v1, strict B5 details and the frozen episodes-v1 rule configuration enter the feature fold. Missing/corrupt/legacy/incompatible evidence becomes unsupported UNKNOWN, never a fabricated negative. Nonprocessed admitted jobs are explicit UNKNOWN boundaries in the temporal stream, so failed/expired/superseded work cannot silently support gaze/object duration. Capture time is never used; the immutable server receive time/admission ordinal is used. Backward server time fails the temporal report explicitly with pipeline_failed and NULL temporal metrics rather than silently correcting time or extrapolating duration.

Persisted report schema 1 has:

```text
{
  schema_version: 1,
  revision: positive integer,
  generated_at: UTC ISO timestamp,
  finalized_at: UTC ISO timestamp|null,
  status: pending|processing|finalizing|complete|partial|failed,
  final: bool,
  reason: null|no_usable_evidence|mixed_provenance|pipeline_failed,
  coverage: {
    received, admitted, processed, dropped, superseded, failed, expired,
    valid_gaze, unknown_gaze, usable_frames, unusable_frames, unsupported_frames,
    known_seconds: {gaze,objects,face}
  },
  gaze_centered_pct: 0..100|null,
  episodes: {
    rules_version, events, episode_counts, observed_seconds, events_truncated
  },
  versions: {pipeline_version:[...],model_version:[...],rules_version:[...],config_version:[...]},
  provenance_limit: 16, versions_truncated: bool,
  validation_status: real_world_accuracy_outstanding,
  expression: {status:disabled,label:null},
  scoring_eligible: false,
  observations: [bounded observer observations],
  ledger_rows_seen: integer|null
}
```

The technical counters remain B2's `received=admitted+dropped`; terminal counts count jobs once, not retries. valid_gaze counts supported processed B5 centered/away records; all other processed records count as unknown_gaze. Quality and unsupported counts explain technical completion versus usable evidence. Under an unavailable/corrupt timeline, feature-coverage counts/seconds can be NULL; the frozen technical snapshot counters stay actual nonnegative integers and unsupported gaze is represented as unknown. No raw frames, meshes, calibration ratios, Clerk credentials, ticket values or candidate identities are placed in report/snapshot output.

`gaze_centered_pct` is centered supported seconds divided by supported gaze seconds, **not attention or a ranking signal**. A supported gaze interval requires consecutive admitted endpoints with the same known gaze class, usable compatible evidence and gap <=1 second. A class-transition interval is not allocated to either class; unsupported endpoints and long gaps contribute no duration. If the denominator is zero (including one isolated centered frame), the percentage is NULL, not 0/100. Legitimate 0/100 values require positive supported exposure and remain approximate/unvalidated. Face/object known seconds likewise require consecutive usable endpoints with bounded gap. Missing observation rows are unsupported evidence even if a job is technically processed.

The B5 pure episode fold is used unchanged for live and final calculation: 2-second onset, 1-second negative-release hysteresis, object enter/retain scores 0.65/0.45, max gap 1 second. The episode key is session-scoped `kind:first_admission_number`. Types remain phone_visible, multiple_people_observed, no_face_observed and gaze_away. Counts/durations are NULL when the relevant feature has no supported exposure; a measured zero requires supported exposure with no qualifying episode. Short negative-release holes do not contribute observed_seconds; never substitute end-start or extend to seal/disconnect. At most 100 recent events and full counts/truncation metadata are retained. Version/config lists retain at most 16 distinct values each, explicitly flag truncation, and include unsupported source versions where available. Mixed provenance produces partial output, not an implicit claim of uniform model behavior.

Final status is failed if no usable compatible frames exist, partial if there are dropped/replaced/failed/expired/quality-rejected/unsupported/unknown-gaze frames, insufficient supported gaze duration or mixed provenance, otherwise complete. A corrupt timeline yields failed/pipeline_failed. Complete means computation/coverage under these rules, not validated real-world accuracy, true attention or proof of behavior. Expression-disabled is intentional and does not itself downgrade coverage. B5's staged-clip accuracy gate remains outstanding for every report.

New B6 rows leave legacy attention_pct and look_away_count NULL, integrity_flags empty (no adjudication policy applied) and emotion_summary empty; expression disabled is explicit in the report. B2's model/rule/revision/finalized/sealed/coverage columns are populated, while actual source model/config lists live in the report. New reports never infer legacy attention or silently rewrite old aggregates. Existing rows with report_schema_version=0, or lifecycle_version=0, are legacy-unverified regardless of their numeric values. A legacy one-to-one conflict is preserved and not replaced. No guessed fake-value signature is used to rewrite history.

### Authorized HTTP status/report/snapshot interfaces

All reads use the existing ClerkJWTAuthentication public verification path, current active-user/role/profile ownership checks, fixed errors and no-store/no-cache headers. No new token/auth/provisioning mechanism exists. GET/HEAD/OPTIONS only; no new write/calibration API is introduced.

| Route | Permission | Response |
|---|---|---|
| GET /api/interview-sessions/{id}/analysis-status/ | Owning candidate or recruiter | Role-filtered status below |
| GET /api/interview-sessions/{id}/behavioral-analysis/ | Owning recruiter only | `{session_id,availability,revision,report}` |
| GET /api/interview-sessions/{id}/analysis-snapshot/ | Owning recruiter only | `{session_id,availability,revision,snapshot}` |

availability is disabled (audio-only), legacy_unverified, pending (no persisted B6 result yet), or ready (a live or terminal persisted B6 result). Pending/unverified/disabled report/snapshot values are NULL and revision is zero. Ready does not mean final or successful; inspect report.status/final. Legacy values are not exposed through these new APIs. Missing/wrong-owner/candidate report requests fail access_denied without exposing another session. Deactivated accounts cannot read persisted data.

Candidate status has exactly session_id, video_state, connection_state, analysis_state, revision, report_ready, final, video_required and availability. It contains no gaze, event, detector, frame-quality, model-version or integrity information. Recruiter status adds coverage (or NULL). A report may be processing/finalizing while interview DONE is already true; clients must not conflate interview/audio/video completion with report readiness. Candidate-safe availability handles audio-only/unverified sessions without pretending ML is pending forever.

### Bounded persisted observer delivery and recovery

The frozen analysis.update envelope is unchanged: `{type,session_id,revision,server_ts,status,coverage,observations}`. Its coverage contains only the original nine technical/gaze counters. The recent projection contains at most 100 observations from at most 20 supported processed frames and is trimmed to <=65536 serialized bytes before persistence. Whole report JSON is bounded to <=131072 bytes; event/observation trimming preserves full aggregate counts and marks event truncation. IDs are deterministic UUIDs per frame/kind, values/enums/boxes follow B3 validation, and expression always maps unavailable. Face/gaze confidence is NULL; detector confidence/normalized boxes are retained. A detector not seeing a class maps none (no detection), not a physical-absence or cheating verdict. No calibration/landmark/raw-image content is exposed.

Notifications are only a wake-up `{type:analysis.update,session_id,revision}` to the recruiter-only group. The snapshot service rechecks the observer lease and active ownership, then fetches persisted state; clients never receive arbitrary event bodies. The consumer subscribes before initial fetch, serializes snapshot sends, reauthorizes before delivery, and ignores equal/older revisions. Stale/reordered wake-ups fetch the newest persisted revision. An independent observer watchdog also polls the stored revision at the existing auth-check cadence (default five seconds), so an already-open authorized connection converges even if Channels silently drops a notification. Reconnect and HTTP reads fetch the same authority. Candidate publishers never join recruiter groups or receive observations.

The report row holds a coalesced durable notification intent: notified_revision and notify_after. It is written with the report, not inferred from one-to-one existence. Both task and direct notification-service callers defer to the real outermost transaction commit; rollback sends nothing. Send claims a 10-second next-attempt lease, releases DB locks, performs a bounded two-second group_send, and records only the successfully attempted revision. Failure/crash leaves recoverable intent; newer reports cannot be cleared by an old revision's acknowledgement. Repeats coalesce after success. Uncertain send/ack failures may repeat a reference, which is harmless under revision suppression. A successful Channels send is not proof of browser receipt; group sends are at-most-once and may silently drop, hence persisted polling/read APIs. No email/inbox/score task is dispatched from B6. There is one intent per report, not growing per-frame/per-retry event rows or broker publications.

### Scoring, integrity and legacy inventory / policy boundary

Current legacy scoring (unchanged formula) reads the latest non-NULL attention_pct aggregate, normalizes attention/100 minus 0.10 per integrity flag, clamps to 0..1 and applies the active behavioral rubric weight. Missing components already contribute zero weighted value without rebalancing; the rubric must total one. CandidateScore writes can queue score_ready notifications. Ranking sorts stored final_score descending/nulls last, then application created_at and PK. None of those weights, stored scores, ranking order or historical signal meanings was rewritten by B6.

B6 adds an explicit schema-version exclusion to the scoring reader: report_schema_version=1 can never become a behavioral signal, even if somebody later populates its legacy numeric field. B6 does not dispatch score generation. Legacy schema-zero scoring behavior remains as found, including risks from unverified historical inputs if somebody independently reruns scoring; changing missing-signal weighting, auditing/quarantining legacy signals or retroactive recalculation needs a separate approved policy/data decision. Excluding B6 is not a claim that all legacy inputs were accurate, nor a hidden change to existing missing-component weighting.

The existing recruiter integrity presentation is read-only: thresholds default look-away 5 / flag count 1 and a warning appears when counts exceed them. It already excludes absent frame backing and the two numeric historical stub patterns 94.5/2 and 80/0. B6 preserves that legacy heuristic but **does not use it as proof for data migration/deletion**; new schema-one reports are explicitly excluded from those legacy count/threshold warnings. The Flutter review screen currently labels a panel Proctoring flags, reads proctoring_flags or integrity_flags, and displays No proctoring flags were reported when empty. B6 observation episodes are not injected into that panel. B7 must explain observations/UNKNOWN/coverage without converting them into cheating labels.

A read-only inventory of the configured local application DB found zero behavioral-analysis rows, zero frame rows, zero candidate-score rows, zero rows matching either numeric stub pattern and no active rubric. Its behavioral table still has the legacy seven columns because application migrations were not applied. These counts certify only that inspected database/time, not other environments or historical provenance. No synthetic production records, default rubric, user-data cleanup or guessed-signature migration was created. B6 tests create explicitly test-only evidence in disposable databases.

### Verification and next milestone

Exact tests/results, migration/configuration/files and remaining gaps are recorded in [B6 handoff](behavioral_reporting_handoff.md). Tests cover empty/quality/partial/unsupported/missing-row coverage, supported percentages versus NULL, temporal replay/gaps, live/final revisions, claim/source/late-worker races, repeat finalization, transaction rollback, notification outages/loss/reconnect polling, byte/window caps, signed local Clerk credentials, roles/ownership/deactivation, candidate-safe output and duplicate scoring/notification effects. They use synthetic persisted observations with test-only model provenance, not real ML accuracy or provider/browser verification.

B7 should consume these exact role-filtered APIs and highest-revision snapshot replacement, keep capture on v1 JSON/base64 limits, honor no-store/auth/reconnect/finish semantics, and build the explicit per-session target-calibration UX. It must keep expression unavailable and show approximate/UNKNOWN/coverage limitations. Do not enable new behavioral ranking effects or auto-cheating presentation. Consented staged real-clip validation, target worker/Redis/browser/GPU performance/privacy verification and any legacy policy/migration decision remain separate gates. Admission remains disabled by default; B6 does not deploy or enable it.

Library behavior checked against official [Django transactions/on_commit](https://docs.djangoproject.com/en/6.0/topics/db/transactions/), [QuerySet locking/iterator](https://docs.djangoproject.com/en/6.0/ref/models/querysets/), [Channels at-most-once/group capacity](https://channels.readthedocs.io/en/stable/channel_layer_spec.html), and [Celery task retries](https://docs.celeryq.dev/en/stable/userguide/tasks.html). These support the implementation choices; no exactly-once browser delivery, automatic policy approval or production latency guarantee is claimed.

## 18. B7 deployment preparation and local acceptance boundary

The user's current B7 milestone is deployment preparation/acceptance, not the frontend UX work previously labeled B7 in earlier planning sections. No B1–B6 route, schema, authentication, score/ranking policy, model artifact or temporal rule changed. Admission and expression remain disabled; B7 adds no migration. The existing application DB was not migrated and no external deployment/publishing/provider/camera action occurred.

Runnable process/proxy/Redis/recovery/retention/migration/rollback instructions, exact test commands, changed files and unmet checks are in [B7 operator guide](behavioral_analysis_operator_guide.md). Machine-readable local evidence is [B7 acceptance](behavioral_analysis_acceptance.json). New opt-in production_settings requires a private secret plus exact HTTPS origins/hosts and preserves Clerk. Explicit Linux worker subscriptions are general queue `celery` (prefork concurrency2) and vision queue `interview.analysis.vision` (configurable INTERVIEW_ANALYSIS_VISION_QUEUE, prefork concurrency1). The workers reject native Windows startup. INTERVIEW_ANALYSIS_VISION_MAX_TASKS_PER_CHILD defaults0; recycling/rewarming needs target measurement. Model warm-up remains inside the owning execution child, not ASGI/parent pre-fork.

The nginx template strips queries from access logs and disables query-bearing error logs; bounded Daphne suppresses access output, preserving the 307200-byte wire cap/two-message input queue. The Python production handler redacts recognizable ticket/Bearer/Redis URL patterns and suppresses raw exception details; no body/frame logging is authorized. Remote logging/APM/CDN and target proxy execution still require verification. Proxy read/send/connect timeout75/15/3 seconds is separate from application connection lifetime60, credential-capped lease60 and ticketTTL30. Reconnect still obtains a new ticket/generation; these are not interchangeable TTLs.

Production private payload storage must be its own Redis primary with no RDB/AOF/replica/backup, explicit memory cap and noeviction. Broker is separate, with proposed AOF everysec/noeviction; channel/guard storage is transient/noeviction. DB-number isolation does not isolate persistence. Templates are reviewable preparation only; no running Redis configuration was changed. Existing 32 active jobs, 64 payloads, 128 vision references, per-session processing/latest-pending and 10800 ledger limits remain authoritative. Cleanup failures now emit a fixed warning without job/session ID, exception/URL or bytes. TTL remains the orphan fallback, not a zeroization or backup-purge guarantee.

Local-only `manage.py behavioral_analysis_ops health [--require-ready]` returns dependency/migration/vision-lease and payload isolation/persistence/eviction findings without credentials. It initializes no model and is not a public readiness API/accuracy certificate. `behavioral_analysis_ops metrics` bounds its detailed scope to 1000 sessions/10000 latest jobs, reports scope/truncation, counters, active age, retries, real receive-to-terminal/finalization p50/p95. Processing-service latency/general-queue latency/cleanup totals remain explicitly NULL when unavailable; do not fabricate zeros or infer service time from claim expiry. Rates need same-scope snapshot deltas. `behavioral_analysis_ops retention-plan --days N` is inventory-only: no guessed retention policy or deletion, no historical rewriting. Existing bounded recovery/expired-ticket cleanup commands remain the supported operator paths.

Actual host inspection: Windows11/Python3.12.7, i5-11400H6core/12logical, 16948453376 bytes RAM; NVIDIA RTX3050Laptop4GiB/driver617.14 detected. Pinned ONNX runtime has no CUDA provider and B5 supports CPU only. No GPU inference/VRAM acceptance is claimed. Real pinned CPU models ran on synthetic640x480/JPEG157359-byte noise at3FPS/session, 20-second schedules: one session60/60 processed, p95offline latency127.431ms; two120/120, p95250.073ms; four180/240, p95427.092ms and25% replaced. Budget was p95<=500ms and replacement<=5%, so four failed. Two is the largest **tested passing offline case**, not certified production concurrency; three was unmeasured. Offline round-robin fairness1.0 is not database/broker admission fairness. Model/precision/process scope and RSS/CPU/coverage limitations are recorded with the evidence. No synthetic records entered the application DB and these tests do not validate real faces/phones/gaze.

Backend299 tests/158.228s passed; final B7-focused15/6.454s passed (overlapping suites,304 distinct tests). Real Redis8.2.10/PostgreSQL/Daphne and a Windows solo test worker were exercised with explicitly test-only inference where needed. An isolated ten-message real Redis metadata probe measured p502.775/p959.878ms, not production general-queue contention. Frontend155 passed/1skipped/27failed at00:43; all failing test names are recorded and frontend source was preserved. No end-to-end frontend InterviewAnalysis flow exists yet. Local health found pending migrations and shared persistent payload Redis, so readiness is false; admission remains disabled.

Unmet gates include target Linux/container native process lifecycle and restart/outage load, GPU runtime compatibility/real inference/VRAM, full-stack3FPS concurrency/fairness/general-queue latency/process-tree resource metrics, approved camera/browser/calibration/staged real-clip accuracy, approved Clerk/Retell live acceptance, TLS/proxy/upstream log audits, frontend capture/lease/finish/highest-revision integration, migration rehearsal/private Redis isolation and approved derived retention policy. These gaps are explicit, not successful benchmarks or implicit enablement. The next integration must consume the unchanged B1–B6 contracts and complete these gates before admission; no automatic integrity/ranking effect is authorized.
