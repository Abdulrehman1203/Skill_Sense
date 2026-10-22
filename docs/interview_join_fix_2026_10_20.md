# Interview join failure, 20 October 2026

The affected interview was `ab155f8a-e9bf-4832-b36e-83cfb0525614`.
Read-only PostgreSQL inspection confirmed these events (Asia/Karachi, 19 October):

- 23:52:49.911: session started with video required.
- 23:53:21.008: video ended with `inactivity_timeout`; zero received frames and no last frame.
- 23:53:40.163: provider audio ended with `user_hangup`; interview status became DONE.

The active Flutter Chrome process used `http://localhost:54281`. Django's active
`INTERVIEW_ANALYSIS_WS_ALLOWED_ORIGINS` omitted that origin and allowed ports
3000, 50893 and 50894 instead. All four retained publisher tickets were unconsumed.
The strict socket entry rejects this origin before ticket redemption. HTTP CORS
allowing requests does not authorize the WebSocket. Video recovery then seals
the session after its configured 30-second inactivity window. The start endpoint
correctly rejects the sealed session with 409. Repeated joins do not cause the
initial video timeout.

The saved provider transcript contains two agent turns (23 and 12 words) and a
recording URL, confirming speech generation. No transcript contents were exported.
These records do not establish why the initial interviewer audio was inaudible
or what triggered the client-side hangup. No provider prompt/audio setting was
changed and no real microphone/provider acceptance test is claimed.

## Repair

- Add the current exact localhost origin to the local ignored `.env`, retaining
  existing allowlist entries. Restart the API process to load this setting.
- Add a frontend VS Code Chrome launch configuration pinned to port 50893. For
  terminal launches use `flutter run -d chrome --web-hostname=localhost --web-port=50893`.
- Reject browser video-start requests from unconfigured origins before creating
  provider calls or starting video timers. Keep the exact-origin socket policy.
- Recheck status before opening retry devices; prevent joins for sealed video;
  retain the backend's conflict message and reconcile its current state.
- Replace the unconditional claim that an interrupted session has not ended.

The local API server was restarted with its existing bounded ASGI launch command.
The new environment confirms the current origin is allowed; an unauthenticated
HTTP request returns the expected 401, confirming that the API responds.

The completed interview has not been reopened or deleted. A newly scheduled test
interview is required for live camera/microphone verification. Keep the tab visible
and verify a successful analysis WebSocket and received frames while audio joins.

## Focused checks

`python manage.py test interview_system.test_voice_origin_preflight`

`flutter test test/interview_ended_rejoin_test.dart --no-pub`

The backend tests require no database or provider access. The Flutter tests use
local fixtures and do not start a real provider call.

Results: three backend tests passed; three Flutter tests passed. For live playback,
use the existing **Enable sound** control if browser autoplay blocks the audio.
Targeted Flutter static analysis of the changed source and regression file passed
with no issues. The regression also covers video sealing during a join request,
so this race cannot leave the UI stuck in the joining state.

## Subsequent debug-port mismatch

The next Flutter launch used `http://localhost:55175`, rather than the previously
allowed `http://localhost:54281`. The same preflight correctly blocked start.
The local ignored `.env` now also includes the exact `55175` origin, and the
identified bounded ASGI development API was restarted to load it. Existing
allowlist entries were retained. No wildcard or socket-origin bypass was added.
Use the pinned port 50893 launch configuration for future launches, then reload
the lobby. Reloading clears the conservative unconfirmed-start UI state;
this repair does not create a provider call or reopen a completed interview.
