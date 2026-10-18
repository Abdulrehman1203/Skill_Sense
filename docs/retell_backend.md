# Retell backend integration

Development shortcut: the existing `start-voice-session/` request also accepts
`start_now:true`. With Django `DEBUG=True`, this explicitly skips the
candidate's scheduled joining window. If a scheduled interview has no approved
questions and no provider call, it prepares up to six local template prompts for
the test action (two per category), with an audit entry. Existing drafts are not
approved or edited; existing approved questions are reused. It calls no external
question generator and needs no Celery worker for this preparation.
It preserves ownership, immutable session mode, existing-call reuse, and completion checks, and does not
rewrite the scheduled time. `DEBUG=False` rejects the shortcut with HTTP 403.
Omitting `start_now` keeps the ordinary scheduled behavior. The Flutter debug
lobby/localhost release build exposes this through **Start interview now**.
Ordinary starts still require recruiter-approved questions; live interviews cannot
use the shortcut to inject a replacement question set.

The backend owns call creation, approved questions, authentication, credential
delivery, provider termination, transcript persistence, and recovery. Browser
audio transport is a separate frontend responsibility.

## Configuration

Set RETELL_API_KEY to the workspace key marked Webhook in Retell; it must also
authorize API requests for RETELL_AGENT_ID. Keep secrets in .env or the deployment
secret store. Configure the agent prompt with {{interview_questions}}, ask its
JSON questions in order, and enable the end_call tool.

Agent webhook URL:

    https://YOUR-PUBLIC-HOST/api/webhooks/retell/call-ended/

Subscribe to call_ended and call_analyzed to persist transcripts and Retell's
post-call analysis. The dashboard call_started test is authenticated
and acknowledged with 204. Unknown events return 400; invalid signatures return
401. An accepted terminal event returns 202 after its database inbox row is saved.
No transcript is put into broker task arguments. Database failure returns a
non-success response so Retell can retry. Broker failure retains the inbox row.

Run migrations before deploying the updated server and workers:

    .\.venv\Scripts\python.exe manage.py migrate

Run Django, Redis, and Celery workers. Run ngrok for local HTTPS delivery.
Normal delivery uses workers; automatic outage and missed-webhook recovery also
requires Celery Beat:

    .\.venv\Scripts\celery.exe -A ai_interview_recruitment worker --pool=solo --loglevel=info
    .\.venv\Scripts\celery.exe -A ai_interview_recruitment beat --loglevel=info

Windows solo workers are suitable for local development; they do not provide
production process isolation or a hard execution-time guarantee. Use a supported
production worker pool in deployment. A manual recovery pass is available:

    .\.venv\Scripts\python.exe manage.py recover_retell

Keep a stable, private DJANGO_SECRET_KEY. Browser credentials are encrypted using
RETELL_CREDENTIAL_ENCRYPTION_KEY when set, otherwise Django's SECRET_KEY. Changing
that encryption secret invalidates saved credentials; keep it consistent between
server and workers. Expired credentials are not returned and completed calls have
their encrypted credentials cleared.

## Authenticated endpoints

All routes are under /api/interviews/{interview_uuid}/ and accept Clerk bearer
authentication. The linked candidate and owning recruiter can use the voice
routes; other users cannot access the session or its transcripts.

| Method and suffix | Behavior |
| --- | --- |
| POST start-voice-session/ | Start or rejoin a call. Body: {} for audio-only, or {"video_required":true}. Returns 201 for a new call, 200 for reusable credentials, 409 for a completed/active call that cannot be renewed, or 503 for provider failure. |
| GET voice-session/ | Read status, transcript, transcript_object, recording_url, call_analysis, disconnection_reason, and local start/end timestamps. Contains no access token. |
| POST end-voice-session/ | Ask Retell to stop the call. Returns 202; completion waits for authenticated provider results. Client-supplied transcript/completion claims are never trusted. |
| POST sync-voice-session/ | Retrieve provider state and durably ingest terminal results if a webhook was missed. Also reconciles uncertain call creation. |

Successful start/rejoin responses include interview_session_id,
retell_session_id, call_id, access_token, transport, ice_servers, and expires_at
(Unix milliseconds). Responses carrying credentials use Cache-Control: no-store.
The frontend must connect using these provider credentials immediately. It must
never receive the Retell API key or create a separate provider call itself.

Candidates can initially join from 15 minutes before scheduled_at; the owning
recruiter can start sooner. Only approved questions are sent. Questions cannot
be edited once LIVE. Repeat start calls reuse the provider call and credentials
until expiry. An expired unconnected attempt can be replaced after provider
state checks; an ongoing call is never automatically replaced. Retired attempt
webhooks cannot complete the replacement session.

Known provider failure leaves a session retryable instead of pretending voice
started. A timeout or ambiguous create response blocks another creation until
sync/recovery finds the provider call using server-supplied interview metadata.
If no unique match can be found, reconciliation stays pending for inspection in
Retell's call history; it never guesses that creating another call is safe.

Audio-only is the default for newly created sessions. Its terminal audio event
sets audio_ended_at, ended_at, and DONE. Existing sessions retain video-required
semantics. Explicit video-required sessions wait for mark_video_ended(session_id)
as well as audio completion. The future video pipeline must invoke that hook;
this change does not implement camera capture or computer vision.

Terminal events are deduplicated by call_id + event. The first accepted transcript
is preserved; later call_analyzed events can enrich analysis and recording fields.
Provider recording URLs may expire; this integration does not archive recordings.
Retell analysis is provider output, not an applicant grade or recommendation.
Answer grading, final scoring, and behavioral analysis remain separate features;
no sample behavioral scores are generated by this voice lifecycle.

## Verification

Run the isolated provider/broker regression suite against PostgreSQL:

    .\.venv\Scripts\python.exe manage.py test interview_system.tests.test_retell_lifecycle interview_system.tests.test_phase9_retell_client --noinput

Verification completed: 60 focused Retell and scheduling/question-review tests
passed against a fresh PostgreSQL test database. A signed synthetic completion
also passed through the public ngrok tunnel, the running Django server, Redis,
and a real Celery worker, producing the expected persisted transcript and DONE
state. Its temporary application/interview records were removed afterward.
No real Retell audio call was placed by this verification.

The older Phase 9 start/webhook tests describe the superseded recruiter-only,
call-ID-only, ephemeral-queue contract. The lifecycle tests cover the new contract.
Finish live acceptance with an actual candidate browser connection: speak, end
the call, then confirm voice-session/ returns DONE and the actual transcript.
The Retell dashboard's Test button only proves webhook reachability/authentication.

Official API references:
- https://docs.retellai.com/api-references/create-web-call
- https://docs.retellai.com/api-references/get-call
- https://docs.retellai.com/api-references/stop-call
- https://docs.retellai.com/api-references/list-calls
- https://docs.retellai.com/features/webhook-overview
