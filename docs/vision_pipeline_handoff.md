# Interview Analysis B5 local vision and temporal rules handoff

Implemented 18 October 2026 (Asia/Karachi). Scope: B5 only. B1–B4/Retell and unrelated uncommitted work were retained. No applicable AGENTS.md was found in the repositories/inspected ancestors. Backend/frontend git status and current B4 code/shared contract were inspected before edits. The frontend remains its existing untracked docs; no frontend/auth interface changed.

The user explicitly selected **expression disabled**, provided no approved footage, prohibited assuming consent/downloading interview recordings, and requested a proposed `tests/fixtures/vision/` directory. Implementation and synthetic mechanism/runtime validation are complete; **real-world vision accuracy validation remains outstanding** until those staged clips arrive. This is an explicit verification gap, not a passed real-clip gate.

## Changed files

| Files | B5 change |
|---|---|
| `ai/vision/__init__.py`, `types.py`, `decoding.py`, `schema.py` | Django-independent bounded decoding, quality/UNKNOWN/result types and strict persistence schema. No import-time model/framework initialization. |
| `ai/vision/runtime.py`, `gaze.py`, `yolo.py` | Process/thread-owned reusable IMAGE-mode MediaPipe + YOLO ONNX CPU adapters, multiple faces, explicit calibration, conservative UNKNOWN gaze, confidence/box preservation and bounded NMS. |
| `ai/vision/rules.py` | Pure per-session episode fold with confidence hysteresis, supported durations, gap/UNKNOWN boundaries, stable episode keys, recent-event cap and versioned configuration. |
| `ai/vision/model_manifest.json`, `artifacts.py` | Pinned official source/derived artifacts, integrity checks, explicit export/fetch tooling and no inference-time downloads. |
| `ai/vision/evaluate.py` | Offline synthetic benchmark/local consented-clip review harness; no raw frame outputs/network footage retrieval. |
| `requirements-vision.txt`, `requirements-vision-export.txt`, `requirements-vision-win-py312.lock` | Worker-only runtime overlay, separate one-time export tooling and exact measured Windows environment lock. Root web requirements are unchanged by B5. |
| `interview_system/vision_inference.py` | Non-eager dedicated-task context guard, lazy warmed resources, short-lived matching readiness lease, cleanup and B5 observation/provenance packing. |
| `interview_system/vision_calibration.py` | Internal Clerk/B2-owned generation-fenced calibration service, explicit target confirmation and freeze at first admission. No new browser/API route. |
| `interview_system/models.py`, `migrations/0024_interview_analysis_vision_details.py` | Add per-session calibration/per-frame quality/provenance JSON and allow NULL face_count for unavailable observations. No backfill/rewrite of old values. |
| `interview_system/video_lifecycle.py` | Validate optional B5 details and real availability before the existing atomic fenced settlement; legacy three-field test/history interfaces preserved. |
| `interview_system/frame_admission.py`, `vision_scheduling.py` | Require current warm-worker readiness before admission; supply only server-owned session calibration to the worker interface. B4 caps/deadlines/recovery stay intact. |
| `interview_system/tasks/vision.py`, `tasks/__init__.py` | Explicit bounded warm task registration alongside reference-only frame processing; no model initialization on module import. |
| `ai_interview_recruitment/vision_worker.py`, `settings.py`, `.env.example` | Warm bootstrap/shutdown, optional bounded beat warming, CPU model directory, multiple-face limit and dedicated routing. Admission stays disabled by default. |
| `interview_system/tests/test_vision_pipeline.py` | 31 B5 synthetic/persistence/ASGI-isolation tests. |
| `interview_system/tests/test_vision_scheduling.py`, `test_frame_transport.py` | Explicit test-only readiness fixture for existing B4 admission tests; deterministic inference mocks remain test-only. |
| `tests/fixtures/vision/README.md`, `manifest.example.json` | Proposed local consented/staged conditions, hashes/labels and evaluation procedure. No clips supplied or committed. |
| `.gitignore` | Ignore local environments/model weights/raw clips/private reports; include B5 code/tests/docs/fixture instructions. |
| `docs/interview_analysis_shared_contract.md`, `vision_cpu_benchmark.json`, this file | Section 16 actual B5 schema/rules/limits/next contract, measured CPU summary and this handoff. |

Migration 0024 is schema-only: add JSON fields with empty defaults and drop face_count NOT NULL. Original numeric historical counts, reports/emotion values, session/timestamp index, audio fields and parent-lock ordering remain. UNKNOWN is NULL rather than fabricated zero; a usable model run detecting no faces can still honestly report observed zero. Migration was applied only in newly created disposable test databases, not the application database. No identity/auth migration, SimpleJWT, deployment, reset, data deletion or destructive cutover occurred. Clerk provisioning/webhooks and scoring/ranking/rubric behavior were not modified.

## Dependency/artifact compatibility and setup

Final validated standalone runtime: Windows 11 x64, Python 3.12.7, MediaPipe Tasks 0.10.21, ONNX Runtime 1.20.1 CPUExecutionProvider, NumPy 1.26.4, opencv-contrib-python 4.11.0.86, Pillow 11.2.1. Core versions are checked before model initialization; wrong/missing dependencies or artifact hashes fail readiness. pip check passed. The final environment contains one OpenCV package and no torch/torchvision/ultralytics/TensorFlow/FER package. MediaPipe internally uses its bundled TensorFlow Lite/XNNPACK CPU runtime; that does not enable expression inference.

An initial PyTorch/Ultralytics prototype exposed conflicting OpenCV package requirements. It was kept only as export tooling; final inference uses ONNX, not that prototype. Initial CPU-wheel downloads were interrupted/slow; using cached official CPU wheels with `--no-deps` and resolving tooling dependencies from PyPI succeeded. No web-environment packages were changed. The final model was exported from the SHA-pinned official YOLOv8n source with Ultralytics 8.3.70, torch 2.5.1+cpu, torchvision 0.20.1+cpu and ONNX 1.17.0, fixed batch one / 640 / opset 12 / no simplify / no dynamic axes. Volatile export-date metadata was removed before deterministic serialization. A second local export in `vision-artifacts/repro` reproduced the exact pinned ONNX checksum.

The manifest records full source URLs, hashes and sizes. Models remain ignored local assets:

- FaceLandmarker: 3758596 bytes, SHA256 `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff`.
- YOLOv8n ONNX: 12823570 bytes, SHA256 `6f987f27fdd41b29c907c81e2ffa56de79f5096cc17dfde929b3fbeb2fdc8d0a`.
- Export-only YOLO .pt: 6549796 bytes, SHA256 `f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36`.

Example standalone runtime setup (instructions, not a deployment):

```powershell
.venv/Scripts/python.exe -m venv .venv-vision-runtime
.venv-vision-runtime/Scripts/python.exe -m pip install -r requirements-vision.txt
.venv-vision-runtime/Scripts/python.exe -m pip check
```

Use the Windows lock for that measured platform, not as a claim of validated Linux/GPU compatibility. For a full dedicated Django/Celery worker image, install the application's appropriate service dependencies separately with the vision overlay; do not install the overlay into ASGI. That full image was not built or RSS-benchmarked here. Existing root requirements still include unrelated legacy Phase 5 ML dependencies; B5 does not remove them or claim the existing web image is universally Torch-free. The standalone vision runtime itself needs neither Torch nor Ultralytics.

Build/provision artifacts explicitly with the separate export environment, or provide the matching built ONNX artifact from a controlled build:

```powershell
# Separate export tooling environment, never the web/runtime image:
python -m pip install --no-deps torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements-vision-export.txt
python -m ai.vision.artifacts vision-artifacts --export-yolo
# Runtime only verifies/uses already provisioned local artifacts:
.venv-vision-runtime/Scripts/python.exe -m ai.vision.artifacts vision-artifacts
```

The export helper rejects existing mismatched files and any newly built bytes that differ from the pinned result. Other platforms/exporters need explicit compatibility review rather than automatic hash updates. Runtime never fetches models, uses an image API or installs dependencies. Preserve upstream model/runtime licensing terms with builds; no legal/deployment certification is claimed.

## Configuration and ownership

INTERVIEW_ANALYSIS_VISION_ENABLED remains False. INTERVIEW_ANALYSIS_MODEL_DIRECTORY defaults to the ignored local `vision-artifacts` folder. INTERVIEW_ANALYSIS_MAX_FACES defaults to 3 and supports only 2..5; IMAGE mode, detection/presence confidence 0.6, YOLO confidence 0.4, NMS IoU 0.7, max 300 proposals / 20 retained detections, 640x640 float32 CPU export and ORT intra/inter threads 2/1 are frozen in vision-v1 config. MediaPipe has additional native threads; no process-wide two-thread guarantee is made. No GPU provider is initialized/supported by B5; changing the device requires a measured new runtime configuration rather than an unverified VRAM promise.

Use the existing `python -m ai_interview_recruitment.vision_worker` entry in a supported production prefork environment, with app/service dependencies plus the vision overlay. It still subscribes only to the dedicated capped queue and sets the execution marker. The adapter also checks actual non-eager process_frame/warm_vision task context before loading models. Warming is a task in the child, avoiding Celery's four-second worker_process_init signal timeout. Both models execute a warm-up input before readiness. Bootstrap and an enabled-only five-second beat entry publish warm tasks with no image arguments, no retries, short expiry and existing queue cap. A readiness lease must have an actual positive TTL <=15 seconds and matching model/config identity; no warm/matching lease means capacity drop. Worker exceptions revoke resources/readiness; shutdown removes only its own token. A dead worker can remain represented until lease expiry; ready is not a latency SLA. The B4 claims/deadlines, one-processing/one-pending/session and global/payload/list caps remain authoritative.

Per-process models have no session tracker: MediaPipe IMAGE mode and stateless YOLO ONNX prediction are reused only in the owning PID/thread. Gaze receives a per-session immutable baseline; episode fold state is local to one supplied session stream. The internal calibration service requires current publisher credentials/generation, explicit camera-target confirmation, 5..30 stable usable samples and no prior admitted frame. No calibration route/UI is claimed. Without calibration gaze is UNKNOWN, even when ratios can be computed. Reconnecting or moving the camera does not silently reset calibration. Raw images/landmarks stay transient; only bounded quality/reason/ratios/detections/provenance are stored. Native allocator remnants are not a forensic erasure guarantee.

Expression was explicitly disabled by the user. `vision_details.expression={status:disabled,label:null}`, legacy emotion remains null for new observations, and no FER/emotion replacement/model was installed or enabled. No unnamed expression runtime was evaluated or represented as neutral. B6 must project expression unavailable. Section 16 freezes all actual observation keys, limits, UNKNOWN semantics and episode rules.

## Measurement: actual CPU execution on synthetic inputs only

Exact command in the final isolated runtime:

```powershell
.venv-vision-runtime/Scripts/python.exe -m ai.vision.evaluate --models vision-artifacts --synthetic --report vision-reports/b5_cpu_onnx_synthetic.json
```

The pinned FaceLandmarker and YOLO ONNX models actually warmed/executed. Fifteen generated 640x480 inputs exercised five dark quality drops, five flat quality drops and five noise images that ran both models. There were no people/phones/interview clips or accuracy ground truth. The tracked summary is `docs/vision_cpu_benchmark.json`; the ignored local full report contains only synthetic-input observation metadata, no raw frames.

| Actual measurement | Result |
|---|---|
| CPU | Intel Core i5-11400H, 6 physical / 12 logical cores (local hardware query) |
| OS/Python | Windows 11 x64 / 3.12.7 |
| Model/config identity | mp1-64184e229b26-y8nonnx-6f987f27fdd4 / config-v1-ab14d086e77d7811 |
| Cold import/load + both-model warm-up | 2.1749 seconds |
| Noise, both models: median / max, n=5 | 111.1721 / 117.7785 ms |
| Dark quality path: median / max, n=5 | 12.8597 / 13.6423 ms |
| Flat quality path: median / max, n=5 | 12.8052 / 13.2453 ms |
| Baseline process RSS | 26402816 bytes |
| Sampled peak process RSS (20ms sampling) | 232910848 bytes = 222.12 MiB |
| GPU/VRAM | Not measured; CPU provider only |

This is one measured synthetic workload. Noise has no detected-face landmark/gaze workload representative of real interviews, and it does not establish worst-case/production throughput or memory bounds. Sampling can miss short peaks. Full Django/Celery processes, ASGI/broker/payload memory, concurrent sessions, real multiple faces/phones, Linux prefork kills/native runtimes and GPU/VRAM were not benchmarked. An RTX 3050 Laptop GPU and Intel UHD were enumerated locally, but enumeration is not inference verification and no GPU performance/budget claim is made. The earlier Torch prototype measurement is superseded and is not used as the final-runtime benchmark.

## Tests and exact results

Tests use fresh random `test_interview_analysis_b5_<uuid>` PostgreSQL databases and isolated random Redis test namespaces; each test DB is destroyed afterward. No application migration or data deletion, FLUSHDB, deployment or persistent service was performed. Synthetic landmarks/detector tensors and deterministic `test-only` persistence outputs test mechanisms, not real ML accuracy. Existing B4 tests explicitly mock readiness only in tests; production has no synthetic backend or readiness bypass setting.

Full-suite command:

```powershell
.venv/Scripts/python.exe -c "import os,uuid; os.environ.setdefault('DJANGO_SETTINGS_MODULE','ai_interview_recruitment.settings'); from django.conf import settings; settings.DATABASES['default']['TEST']={'NAME':'test_interview_analysis_b5_'+uuid.uuid4().hex[:12]}; import django; django.setup(); from django.core.management import call_command; call_command('test','interview_system.tests.test_vision_pipeline','interview_system.tests.test_vision_scheduling','interview_system.tests.test_frame_transport','interview_system.tests.test_video_lifecycle','interview_system.tests.test_socket_authentication','interview_system.tests.test_account_roles','interview_system.tests.test_retell_lifecycle','interview_system.tests.test_phase6_scoring_task',interactive=False)"
```

The full suite contains 31 B5 tests plus 173 existing regressions. Coverage includes bounded JPEG format/size/dimensions/corruption, quality/UNKNOWN, synthetic iris/pose/calibration isolation, YOLO tensor shape/class-aware NMS/padding/labels/confidence/boxes, strict expression-disabled/quality persistence, current generation/calibration freeze, readiness identity/TTL, per-session episode isolation, duplicate/time rejection, hysteresis, gap/UNKNOWN boundaries, stable reproducibility and bounded event counts. An independent ASGI-import subprocess proves MediaPipe/ONNX Runtime/Ultralytics are not imported. The standalone pure-package import check initializes neither Django nor model frameworks.

Intermediate verification: the first 29-test B5 run took 5.762 seconds and had one failure because removing ten trailing JPEG bytes and inserting EOI still produced a Pillow-decodable JPEG. The corrupt fixture now has a genuinely malformed truncated header; valid Pillow behavior was not treated as cryptographic corruption detection. A 202-test broad run passed in 84.409 seconds. After the ONNX adapter/readiness changes, 204 tests passed in 85.058 seconds; a focused final-schema B5 run passed 31 tests in 6.044 seconds. No test was skipped or weakened to bypass a failure.

**Final full-suite result, including nullable UNKNOWN face-count persistence: 204 tests in 86.595 seconds, OK; zero failures/errors/skips.** This is 31 B5 tests plus 173 existing B1–B4/Clerk/Retell/scoring regressions. The disposable database was destroyed after the run. Final migration consistency, Django system and whitespace checks passed; new Python/JSON sources parsed and both shared-contract/handoff files were verified readable as UTF-8.

Additional checks performed:

```powershell
.venv-vision-runtime/Scripts/python.exe -m pip check
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run --noinput
.venv/Scripts/python.exe manage.py check
git diff --check
```

pip check: no broken requirements; migration check: no changes detected; Django check: no issues (0 silenced); whitespace check passed. Initial migration generation had sandbox-blocked database-history access, then the read-only history/consistency check succeeded with approved local DB access. Application migrations were not run. The ONNX export was reproduced locally against its pinned hash. Model benchmarks ran in a separate standalone process, not ASGI or an application-data task. Actual full Django/Celery vision-model execution in a Linux prefork deployment was not performed; prior Celery integration tests use explicit test-only inference.

## Outstanding staged clips and B6 contract

`tests/fixtures/vision/README.md` and `manifest.example.json` propose the user-provided consented/staged local fixtures: normal/dim lighting, glasses, head turns, leaving/returning, second person, phone and unusable images. The manifest requires explicit consent attestation, local paths, SHA256 and human-reviewed intervals/boxes/ambiguity. The harness limits local video to 1920x1080 input, <=120 FPS and <=1800 sampled frames, samples near 3 FPS and emits observations for human review. It does not manufacture calibration, compute a misleading accuracy score from synthetic pixels, or fetch footage. No clips are currently supplied; the real observation/accuracy gate is outstanding exactly as the user directed.

B6 must consume the sealed/drained ledger and schema-v1 details, distinguish NULL/unavailable/unknown from negative evidence, and fold one session's persisted server-time/admission-ordered observations with the same `episodes-v1` configuration for live/final results. Use observed_seconds (not episode span) and stable session-scoped episode keys, at most 100 recent events, full counts and truncation flag. Historical empty details are unavailable quality provenance. Processed jobs are not automatically usable gaze/object evidence. Implement authorized status/report/snapshot revisions and actual aggregate provenance without model/GPU initialization or same-queue ordering assumptions. Keep candidate messages transport-only, expression unavailable, no automatic cheating verdict and no silent changes to scores/rubric weights/ranking. B7 owns a real explicit calibration/camera UI; no such client exists in B5. Keep admission disabled until appropriate staged accuracy and target-runtime deployment checks support enabling it.
