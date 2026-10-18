# Interview analysis naming

The production feature is **Real-time Interview Analysis**. Use **Behavioral
Analysis** for observations and reporting, **Interview Media** for camera and
microphone controls, **Frame Streaming** for transport, and **Vision Pipeline**
for model inference. Application authentication remains Clerk.

| Responsibility | Name |
| --- | --- |
| Frontend API client | `InterviewAnalysisService` in `lib/services/interview_analysis_service.dart` |
| Frontend wire models | `lib/models/interview_analysis_contract.dart` |
| JSON validation helpers | `requireJsonObject`, `requireJsonString`, `requireInterviewIdentity` |
| Cross-repository contract | `interview_analysis_shared_contract.md` |
| Backend implementation guide | `interview_analysis_backend_guide.md` |
| Frontend implementation guide | `interview_analysis_frontend_guide.md` |
| Integration guide | `interview_analysis_integration_and_acceptance.md` |
| Backend operations module | `interview_system/behavioral_analysis_operations.py` |
| Management command | `python manage.py behavioral_analysis_ops health` |
| Deployment examples | `deploy/behavioral_analysis/` |
| Settings and build flags | `INTERVIEW_ANALYSIS_*` |
| Vision task queue default | `interview.analysis.vision` |
| Redis namespace defaults | `skillsense.analysis.private`, `.guard`, `.channels` |
| Report index | `analysis_report_due` |

Backend tests describe their responsibility: `test_socket_authentication`,
`test_video_lifecycle`, `test_frame_transport`, `test_vision_scheduling`,
`test_vision_pipeline`, `test_behavioral_reporting`,
`test_behavioral_analysis_operations`, and `test_interview_analysis_integration`.
Frontend integration tests use `interview_analysis_integration_test.dart`; browser
fixtures use `interview_analysis_browser_probe.dart`.

## Running the renamed configuration

Update any externally supplied settings, IDE run configurations, service-manager
environment variables, and Flutter `--dart-define` flags to the documented
`INTERVIEW_ANALYSIS_*` names. `.env.example`, operators' instructions, imports,
and active references in both repositories use these names. No authentication
provider, endpoint path, wire field, or protocol version is changed.

Do not mix old and new worker processes. Finish active interviews and drain
pending work before restarting the backend, ASGI server, Celery workers, and
beat together: queue and temporary Redis namespace defaults have changed. No
Redis keys or queued application work are deleted or migrated by this refactor.
Rebuild/restart Flutter with the renamed build flags, for example
`--dart-define=INTERVIEW_ANALYSIS_CONTRACT_CONFIRMED=true` where the existing
integration configuration previously enabled the contract.

Migration `0026_analysis_report_index_name` renames the index using Django's
`RenameIndex`; it does not recreate tables or rewrite stored observations.
Existing migrations retain their original IDs and dependency strings because
the database records those identities. Recorded audit output also retains the
identifiers used when those historical tests actually ran; it is not rewritten
to imply the new modules existed at that time. Ignored local refactor backups
are recovery material and are not production code.

## Verification

The backend audit uses an isolated disposable database and test-owned Redis
keys, with external provider connections blocked. The frontend regression suite
checks the renamed contract, service, media, voice, and navigation imports.
See `test_audit_2026_10_18/analysis_naming_regression.json` for the new backend
test record; older audit evidence remains historical.

Results for this refactor: 250 backend tests and 55 frontend tests passed.
Frontend analysis and the cross-repository Dart probe's static analysis found
no issues. The release web build succeeded. Django system checks and migration
drift checks passed, and the index-name migration was applied to the local
development database. No live provider or camera test was performed.
