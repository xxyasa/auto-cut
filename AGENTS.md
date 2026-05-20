# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Commands

```bash
# Install (core only)
pip install -e .

# Install with API server + ASR
pip install -e ".[business,asr]"

# Run tests
python -m unittest discover

# Run a single test file
python -m unittest tests.test_pipeline

# Lint
ruff check src/ tests/

# Start server (local, no Docker)
./scripts/start.sh                        # 127.0.0.1:8765
./scripts/start.sh --host 0.0.0.0 --port 8770

# CLI — process a video with a pre-existing transcript
autocut run path/to/video.mp4 --product "产品名" --transcript tests/fixtures/transcript.json

# CLI — remix/reorder
autocut remix data/runs/<run_id> --target-duration 25 --llm

# Docker
docker compose up -d --build
docker compose logs -f
```

`scripts/start.sh` auto-loads `.env` and auto-generates a dev token from `.dev-token` if `AUTOCUT_API_TOKEN` is not set.

## Architecture

### Pipeline (core processing flow)

`LiveClipPipeline.run()` in `pipeline.py` is the single entry point for all processing:

```
video → FFprobe → extract WAV → ASR → clean_segments → build_candidates → score_candidates → trim_leading_fillers → trim_trailing_fillers → export
```

All pipeline inputs/outputs are typed dataclasses in `models.py`: `PipelineRequest`, `PipelineResult`, `TranscriptSegment`, `CandidateClip`. Results are written to `<output_dir>/metadata/result.json` and `<output_dir>/metadata/request.json`.

### ASR engines (`asr.py`)

All engines implement the `ASREngine` protocol (`transcribe(audio_path, language, asr_terms) -> list[TranscriptSegment]`). Available engines:
- `TranscriptFileEngine` — loads `.json` or `.srt`; default when `--transcript` is passed
- `FasterWhisperEngine` — offline Whisper; auto-resolves local models from `models/faster-whisper-<name>/`
- `FunASREngine` — Chinese-optimised; requires `funasr` extras
- `GlmAsrEngine` — two-stage: faster-whisper for timestamps, glm-asr API for text correction

`create_asr_engine()` is the factory; the pipeline always calls this, never engines directly.

### Two API surfaces

**Legacy review API** (`api.py`, `create_app()`):
- `/api/runs/*` — read/review pipeline results stored on disk
- `/api/runs/{id}/timeline` — piece-based timeline editing (enabled/disabled blocks)
- `/api/runs/{id}/remix/*` — LLM-assisted reorder export
- No auth required; reads `AUTOCUT_RUNS_DIR` for data

**Business self-service API** (`business_api.py`, `router` mounted under `/api/business/`):
- All routes require `AUTOCUT_API_TOKEN` via `Depends(require_token)`
- Brand/product CRUD backed by `brand_repo.py` (single JSON file, `AUTOCUT_BRAND_REPO`)
- `POST /api/business/upload` — multipart video upload
- `POST /api/business/jobs` — enqueues a full pipeline+remix job; supports `remix.plan_count` (1–5) for multi-plan LLM remix
- `GET /api/business/jobs/{id}` / `jobs/{id}/log` — job status / log tail
- `GET /api/business/jobs/{id}/artifacts/{name}` — serves MP4s and on-demand ZIP exports

The business router is mounted inside `create_app()` with a try/except guard; if `business` extras are missing, the legacy routes still work.

### Async job system (`jobs.py`)

Single-process, single-worker-thread model. Jobs are persisted to `data/jobs/<job_id>/job.json` via atomic write (tmp + `os.replace`). On FastAPI startup, `recover_orphaned_jobs()` marks non-terminal jobs as `failed (recovered)` before the new worker starts. The `runner` callable is provided by the route layer (no circular imports); `jobs.py` never imports `pipeline`, `oss`, or `remix`.

Status flow: `queued → downloading → running → remixing → done / partial_success / failed`

`partial_success` applies when the `enabled` track succeeded but `remix` track failed.

### Multi-plan remix (`remix.py`)

`RemixSpec.plan_count` (1–5, default 1) controls how many distinct LLM remix variants are generated per job. Each plan calls `generate_ordered_ids()` with a `_variant_prompt()` that injects diversity constraints and tracks `seen_orders` across plans. Plans are stored as `artifacts.remix_plans: list[{index, label, mp4, duration, duplicate}]`; `artifacts.remix_mp4` always points to plan 1 for backward compatibility.

Script role order: `hook → appearance → identity → selling_point → demo → proof → close`. The `appearance` role is new and prioritises colour/style/aesthetic sentences at the start.

### Timeline system (`api.py` lower half)

Timelines are computed on-demand from `result.json` — not stored. Boundaries come from: transcript segment edges, disabled-source ranges, and a 4-second grid. Each piece gets a stable `piece_id` (`p{index}_{start_ms}_{end_ms}`). User overrides (enabled/disabled) are persisted to `metadata/timeline_overrides.json`.

Disabled-kind priority (highest first): `irrelevant_topic > abnormal_scene > cough-like > leading_incomplete > live_filler_phrase > short_filler > incomplete_dialogue > low_value > silence > cut_suggestion`

### Compact / jumpcut (`jumpcut.py`)

`build_compact_plan()` builds a filler-removal plan at the word level. `build_compact_subtitles()` aligns SRT output to compact timing. `audio_events.py` provides `detect_cough_like_events()` used to tag cough/noise ranges that feed into the disabled-kind priority above.

### Segments ZIP export (`exporter.py`)

`export_segments_zip(source_video, output_path, segments)` slices the source video into per-segment MP4 files inside a ZIP, named `001_<label>_<timecode>.mp4`. Called on-demand from `GET /artifacts/{name}` when the requested filename matches `*_segments.zip`.

### LLM integration (`llm.py`)

Used for remix reorder only. Configured via:
- `AUTOCUT_LLM_API_URL` — defaults to `https://model-api.ecmax.cn/v1/chat/completions`
- `AUTOCUT_LLM_MODEL` — defaults to `deepseek-v3.2`
- `AUTOCUT_LLM_API_KEY`

`generate_ordered_ids()` sends the remix prompt and expects JSON `{"ordered_ids": [...]}` back. Streaming mode collects SSE chunks then parses the assembled content.

### Auth (`auth.py`)

`require_token` is a FastAPI dependency. Token is read from (in priority order): `Authorization: Bearer <t>`, `X-Autocut-Token` header, `autocut_token` cookie. Uses `secrets.compare_digest` for constant-time comparison. Only applies to `/api/business/*`.

### Brand repo (`brand_repo.py`)

Single JSON file at `data/brand_repo.json` (overridable via `AUTOCUT_BRAND_REPO`). Protected by an in-process `threading.RLock`. Atomic writes only. `merge_terms()` combines brand associations + product associations + caller-supplied extra terms for ASR hotwords.

### OSS / video source (`oss.py`)

Three source types: `upload` (already on disk), `url` (HTTPS only, SSRF-protected via DNS check), `oss` (MinIO SDK fallback). Max 2 GiB. Allowed extensions: `.mp4 .mov .mkv .webm .flv .ts`. URL allowlist controlled by `AUTOCUT_URL_ALLOWLIST`.

### Output directory layout

```
<run_dir>/
  metadata/
    result.json         # PipelineResult (all candidates, transcript, scores)
    request.json        # PipelineRequest (serialized, on_progress=null)
    reviews.json        # clip_id → {status, note}
    timeline_overrides.json   # piece_id → {enabled}
  subtitles/
    source.srt
    <clip_id>.srt
    <clip_id>_compact.srt
  clips/
    <clip_id>.mp4
    <clip_id>_compact.mp4
  frames/
    <clip_id>_cover.jpg
  exports/              # remix and enabled-track exports + segment ZIPs
```

## Key environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AUTOCUT_API_TOKEN` | — | Required for business API routes |
| `AUTOCUT_LLM_API_URL` | `https://model-api.ecmax.cn/v1/chat/completions` | LLM endpoint |
| `AUTOCUT_LLM_MODEL` | `deepseek-v3.2` | LLM model name |
| `AUTOCUT_LLM_API_KEY` | — | LLM bearer token |
| `AUTOCUT_RUNS_DIR` | `data/runs` | Pipeline output root |
| `AUTOCUT_UPLOAD_DIR` | `data/uploads` | Multipart upload dir |
| `AUTOCUT_JOBS_DIR` | `data/jobs` | Job persistence dir |
| `AUTOCUT_BRAND_REPO` | `data/brand_repo.json` | Brand/product data |
| `AUTOCUT_DISABLE_WORKER` | — | Set to `1` to skip worker thread (tests) |

## Testing notes

Tests use `tests/fixtures/transcript.json` and `tests/fixtures/sample.mp4`. The `jobs._reset_for_tests()` helper clears module-level worker/queue state between test cases. Set `AUTOCUT_DISABLE_WORKER=1` in test environments that don't need the worker thread.

## Skills

`skills/` contains markdown spec files for each pipeline stage as Agent-callable skills: `live-ingest`, `asr-transcribe`, `filler-clean`, `semantic-segment`, `clip-ranker`, `review-export`, `qa-check`. These are documentation only, not importable Python.
