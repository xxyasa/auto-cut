import json
import os
import re
from pathlib import Path

from . import media as media_tools
from .llm import LLMError, generate_ordered_ids
from .models import PipelineRequest, to_plain_dict
from .pipeline import LiveClipPipeline
from .remix import build_remix_source, export_remix_plan, remix_plan_from_items, remix_plan_from_ordered_ids, write_remix_plan

try:
    from fastapi import HTTPException
except ImportError:  # pragma: no cover
    HTTPException = RuntimeError


def create_app():
    try:
        from fastapi import Body, FastAPI, HTTPException, Query
        from fastapi.responses import FileResponse
        from pydantic import BaseModel
    except ImportError as exc:
        raise RuntimeError("Install API dependencies with: pip install -e '.[api]'") from exc

    class RunPayload(BaseModel):
        video_path: str
        product: str
        selling_points: list[str] = []
        transcript_path: str | None = None
        output_dir: str | None = None
        asr_engine: str = "transcript"
        asr_model: str = "small"
        asr_device: str = "cpu"
        asr_compute_type: str = "int8"
        asr_beam_size: int = 5
        brand_terms: list[str] = []
        brand_terms_path: str | None = None
        use_default_brand_terms: bool = True
        min_start_time: float = 0.0
        max_overlap_ratio: float = 0.45
        export_compact: bool = True
        compact_merge_gap: float = 0.45

    class RemixPayload(BaseModel):
        ordered_ids: list[str] | None = None
        items: list[dict] | None = None
        target_duration: float = 25.0
        output_name: str | None = None
        use_llm: bool = False
        stream: bool = False
        model: str | None = None

    app = FastAPI(title="Auto Cut API", version="0.1.0")
    project_root = Path(os.environ.get("AUTOCUT_ROOT", Path.cwd())).resolve()
    runs_root = Path(os.environ.get("AUTOCUT_RUNS_DIR", project_root / "data" / "runs")).resolve()
    static_root = Path(__file__).resolve().parent / "static"

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(static_root / "index.html")

    @app.get("/api/runs")
    def list_runs():
        runs_root.mkdir(parents=True, exist_ok=True)
        runs = []
        for run_dir in sorted(runs_root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
            if not run_dir.is_dir():
                continue
            result_path = run_dir / "metadata" / "result.json"
            result = _read_json(result_path) if result_path.exists() else {}
            candidates = result.get("candidates", [])
            scores = [item.get("score", 0) for item in candidates]
            runs.append(
                {
                    "id": run_dir.name,
                    "path": str(run_dir),
                    "updated_at": run_dir.stat().st_mtime,
                    "candidate_count": len(candidates),
                    "best_score": max(scores) if scores else 0,
                    "duration": result.get("media", {}).get("duration"),
                }
            )
        return {"runs": runs}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        run_dir = _safe_run_dir(runs_root, run_id)
        result_path = run_dir / "metadata" / "result.json"
        if not result_path.exists():
            raise HTTPException(status_code=404, detail="result.json not found")

        result = _read_json(result_path)
        reviews = _read_json(run_dir / "metadata" / "reviews.json", default={})
        candidates = []
        for item in result.get("candidates", []):
            clip_id = item.get("clip_id", "")
            exports = item.get("exports", {})
            candidates.append(
                {
                    "clip_id": clip_id,
                    "start_time": item.get("start_time"),
                    "end_time": item.get("end_time"),
                    "duration": round((item.get("end_time", 0) or 0) - (item.get("start_time", 0) or 0), 2),
                    "compact_duration": item.get("compact_duration"),
                    "removed_duration": item.get("removed_duration", 0),
                    "edit_decision": item.get("edit_decision", {}),
                    "score": item.get("score", 0),
                    "tags": item.get("tags", []),
                    "reason": item.get("reason", ""),
                    "risk": item.get("risk", []),
                    "penalty": item.get("penalty", []),
                    "transcript": item.get("transcript", ""),
                    "clean_transcript": item.get("clean_transcript", ""),
                    "dimension_scores": item.get("dimension_scores", {}),
                    "status": reviews.get(clip_id, {}).get("status", item.get("status", "pending")),
                    "note": reviews.get(clip_id, {}).get("note", ""),
                    "video_url": _media_url(exports.get("video")),
                    "compact_video_url": _media_url(exports.get("compact_video")),
                    "cover_url": _media_url(exports.get("cover")),
                    "subtitle_url": _media_url(exports.get("subtitle")),
                    "compact_subtitle_url": _media_url(exports.get("compact_subtitle")),
                    "metadata_url": _media_url(exports.get("metadata")),
                    "edit_decision_url": _media_url(exports.get("edit_decision")),
                    "video_path": exports.get("video", ""),
                    "compact_video_path": exports.get("compact_video", ""),
                    "cover_path": exports.get("cover", ""),
                    "subtitle_path": exports.get("subtitle", ""),
                    "compact_subtitle_path": exports.get("compact_subtitle", ""),
                    "metadata_path": exports.get("metadata", ""),
                    "edit_decision_path": exports.get("edit_decision", ""),
                }
            )
        result["candidates"] = candidates
        result["run_id"] = run_id
        return result

    @app.get("/api/runs/{run_id}/timeline")
    def get_run_timeline(run_id: str):
        run_dir = _safe_run_dir(runs_root, run_id)
        result_path = run_dir / "metadata" / "result.json"
        if not result_path.exists():
            raise HTTPException(status_code=404, detail="result.json not found")
        result = _read_json(result_path)
        overrides = _read_json(run_dir / "metadata" / "timeline_overrides.json", default={})
        return _build_full_timeline(result, overrides.get("__source__", {}))

    @app.get("/media")
    def media(path: str = Query(...)):
        media_path = _safe_media_path(project_root, runs_root, path)
        if not media_path.exists() or not media_path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(media_path)

    @app.patch("/api/runs/{run_id}/clips/{clip_id}/review")
    def review_clip(run_id: str, clip_id: str, payload: dict = Body(...)):
        status = str(payload.get("status", "pending"))
        note = str(payload.get("note", ""))
        if status not in {"pending", "approved", "rejected"}:
            raise HTTPException(status_code=400, detail="status must be pending/approved/rejected")
        run_dir = _safe_run_dir(runs_root, run_id)
        review_path = run_dir / "metadata" / "reviews.json"
        reviews = _read_json(review_path, default={})
        reviews[clip_id] = {"status": status, "note": note}
        review_path.write_text(json.dumps(reviews, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "clip_id": clip_id, "status": status}

    @app.get("/api/runs/{run_id}/clips/{clip_id}/timeline")
    def get_clip_timeline(run_id: str, clip_id: str):
        run_dir = _safe_run_dir(runs_root, run_id)
        result_path = run_dir / "metadata" / "result.json"
        if not result_path.exists():
            raise HTTPException(status_code=404, detail="result.json not found")
        result = _read_json(result_path)
        candidate = _find_candidate(result, clip_id)
        if not candidate:
            raise HTTPException(status_code=404, detail="clip not found")
        overrides = _read_json(run_dir / "metadata" / "timeline_overrides.json", default={})
        return _build_timeline(result, candidate, overrides.get(clip_id, {}))

    @app.patch("/api/runs/{run_id}/clips/{clip_id}/timeline/{piece_id}")
    def update_timeline_piece(run_id: str, clip_id: str, piece_id: str, payload: dict = Body(...)):
        if "enabled" not in payload:
            raise HTTPException(status_code=400, detail="enabled is required")
        enabled = bool(payload["enabled"])
        run_dir = _safe_run_dir(runs_root, run_id)
        override_path = run_dir / "metadata" / "timeline_overrides.json"
        overrides = _read_json(override_path, default={})
        clip_overrides = overrides.setdefault(clip_id, {})
        clip_overrides[piece_id] = {"enabled": enabled}
        override_path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "clip_id": clip_id, "piece_id": piece_id, "enabled": enabled}

    @app.patch("/api/runs/{run_id}/timeline/{piece_id}")
    def update_run_timeline_piece(run_id: str, piece_id: str, payload: dict = Body(...)):
        if "enabled" not in payload:
            raise HTTPException(status_code=400, detail="enabled is required")
        enabled = bool(payload["enabled"])
        run_dir = _safe_run_dir(runs_root, run_id)
        override_path = run_dir / "metadata" / "timeline_overrides.json"
        overrides = _read_json(override_path, default={})
        timeline_overrides = overrides.setdefault("__source__", {})
        timeline_overrides[piece_id] = {"enabled": enabled}
        override_path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "run_id": run_id, "piece_id": piece_id, "enabled": enabled}

    @app.post("/api/runs/{run_id}/export")
    def export_run_timeline(run_id: str):
        run_dir = _safe_run_dir(runs_root, run_id)
        result_path = run_dir / "metadata" / "result.json"
        if not result_path.exists():
            raise HTTPException(status_code=404, detail="result.json not found")
        result = _read_json(result_path)
        overrides = _read_json(run_dir / "metadata" / "timeline_overrides.json", default={})
        timeline = _build_full_timeline(result, overrides.get("__source__", {}))
        source_video = Path(timeline.get("video_path") or "").resolve()
        if not source_video.exists():
            raise HTTPException(status_code=404, detail="source video not found")
        ranges = _merge_export_ranges(
            [
                {"start": piece["start"], "end": piece["end"]}
                for piece in timeline.get("pieces", [])
                if piece.get("enabled")
            ]
        )
        if not ranges:
            raise HTTPException(status_code=400, detail="no enabled ranges to export")
        output_dir = run_dir / "exports"
        output_path = output_dir / f"{run_id}_enabled.mp4"
        warning = media_tools.export_clip_segments(source_video, output_path, ranges)
        if warning:
            raise HTTPException(status_code=500, detail=warning)
        return {
            "ok": True,
            "path": str(output_path),
            "url": _media_url(str(output_path)),
            "ranges": ranges,
            "duration": round(sum(item["end"] - item["start"] for item in ranges), 3),
        }

    @app.get("/api/runs/{run_id}/remix")
    def get_run_remix(run_id: str, target_duration: float = Query(25.0)):
        run_dir = _safe_run_dir(runs_root, run_id)
        result_path = run_dir / "metadata" / "result.json"
        if not result_path.exists():
            raise HTTPException(status_code=404, detail="result.json not found")
        result = _read_json(result_path)
        request = _read_json(run_dir / "metadata" / "request.json", default={})
        return build_remix_source(result, request, target_duration=target_duration)

    @app.post("/api/runs/{run_id}/remix/export")
    def export_run_remix(run_id: str, payload: RemixPayload | None = None):
        payload = payload or RemixPayload()
        run_dir = _safe_run_dir(runs_root, run_id)
        result_path = run_dir / "metadata" / "result.json"
        if not result_path.exists():
            raise HTTPException(status_code=404, detail="result.json not found")
        result = _read_json(result_path)
        request = _read_json(run_dir / "metadata" / "request.json", default={})
        remix_source = build_remix_source(result, request, target_duration=payload.target_duration)
        model_result = None
        if payload.use_llm and not payload.ordered_ids:
            try:
                model_result = generate_ordered_ids(
                    remix_source["prompt"],
                    stream=payload.stream,
                    model=payload.model,
                )
            except LLMError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            payload.ordered_ids = model_result["ordered_ids"]
        if payload.items:
            plan = remix_plan_from_items(
                payload.items,
                target_duration=payload.target_duration,
                strategy="manual_order",
            )
        elif payload.ordered_ids:
            plan = remix_plan_from_ordered_ids(
                remix_source["units"],
                payload.ordered_ids,
                target_duration=payload.target_duration,
            )
            if model_result:
                plan["model_reason"] = model_result.get("reason", "")
                plan["model_raw_content"] = model_result.get("raw_content", "")
        else:
            plan = remix_source["default_plan"]
        if not plan.get("items"):
            raise HTTPException(status_code=400, detail="no remix items to export")
        output_dir = run_dir / "exports"
        name = payload.output_name or f"{run_id}_remix_{int(round(payload.target_duration))}s.mp4"
        output_path = output_dir / _safe_output_name(name)
        warning = export_remix_plan(result, plan, output_path)
        if warning:
            raise HTTPException(status_code=500, detail=warning)
        plan_path = run_dir / "metadata" / f"{output_path.stem}_plan.json"
        write_remix_plan(plan_path, {**plan, "video_path": str(output_path), "video_url": _media_url(str(output_path))})
        return {
            "ok": True,
            "path": str(output_path),
            "url": _media_url(str(output_path)),
            "plan_path": str(plan_path),
            "duration": plan.get("duration"),
            "ordered_ids": plan.get("ordered_ids"),
            "model_reason": plan.get("model_reason", ""),
            "script_text": plan.get("script_text"),
            "items": plan.get("items", []),
        }

    @app.post("/api/runs/{run_id}/remix/llm")
    def generate_run_remix_with_llm(run_id: str, payload: RemixPayload | None = None):
        payload = payload or RemixPayload(use_llm=True)
        run_dir = _safe_run_dir(runs_root, run_id)
        result_path = run_dir / "metadata" / "result.json"
        if not result_path.exists():
            raise HTTPException(status_code=404, detail="result.json not found")
        result = _read_json(result_path)
        request = _read_json(run_dir / "metadata" / "request.json", default={})
        remix_source = build_remix_source(result, request, target_duration=payload.target_duration)
        try:
            model_result = generate_ordered_ids(
                remix_source["prompt"],
                stream=payload.stream,
                model=payload.model,
            )
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        plan = remix_plan_from_ordered_ids(
            remix_source["units"],
            model_result["ordered_ids"],
            target_duration=payload.target_duration,
        )
        plan["model_reason"] = model_result.get("reason", "")
        plan["model_raw_content"] = model_result.get("raw_content", "")
        plan_path = run_dir / "metadata" / f"remix_{int(round(payload.target_duration))}s_llm_plan.json"
        write_remix_plan(plan_path, plan)
        return {
            "ok": True,
            "plan_path": str(plan_path),
            "duration": plan.get("duration"),
            "ordered_ids": plan.get("ordered_ids"),
            "model_reason": plan.get("model_reason", ""),
            "script_text": plan.get("script_text"),
            "items": plan.get("items", []),
        }

    @app.patch("/api/runs/{run_id}/remix/reorder")
    def reorder_remix(run_id: str, payload: dict = Body(...)):
        ordered_ids = payload.get("ordered_ids")
        items = payload.get("items")
        if not items and (not ordered_ids or not isinstance(ordered_ids, list)):
            raise HTTPException(status_code=400, detail="ordered_ids or items is required")
        run_dir = _safe_run_dir(runs_root, run_id)
        result_path = run_dir / "metadata" / "result.json"
        if not result_path.exists():
            raise HTTPException(status_code=404, detail="result.json not found")
        result = _read_json(result_path)
        request = _read_json(run_dir / "metadata" / "request.json", default={})
        remix_source = build_remix_source(result, request)
        if items:
            plan = remix_plan_from_items(items, strategy="manual_order")
        else:
            plan = remix_plan_from_ordered_ids(remix_source["units"], ordered_ids)
        plan_path = run_dir / "metadata" / "remix_reorder_plan.json"
        write_remix_plan(plan_path, plan)
        return {
            "ok": True,
            "plan_path": str(plan_path),
            "duration": plan.get("duration"),
            "ordered_ids": plan.get("ordered_ids"),
            "items": plan.get("items", []),
        }

    @app.post("/runs")
    def create_run(payload: RunPayload):
        video_path = Path(payload.video_path)
        output_dir = Path(payload.output_dir) if payload.output_dir else Path("data") / "runs" / video_path.stem
        request = PipelineRequest(
            video_path=video_path,
            product=payload.product,
            selling_points=payload.selling_points,
            transcript_path=Path(payload.transcript_path) if payload.transcript_path else None,
            output_dir=output_dir,
            asr_engine=payload.asr_engine,
            asr_model=payload.asr_model,
            asr_device=payload.asr_device,
            asr_compute_type=payload.asr_compute_type,
            asr_beam_size=payload.asr_beam_size,
            brand_terms=payload.brand_terms,
            brand_terms_path=Path(payload.brand_terms_path) if payload.brand_terms_path else None,
            use_default_brand_terms=payload.use_default_brand_terms,
            min_start_time=payload.min_start_time,
            max_overlap_ratio=payload.max_overlap_ratio,
            export_compact=payload.export_compact,
            compact_merge_gap=payload.compact_merge_gap,
        )
        result = LiveClipPipeline().run(request)
        return to_plain_dict(result)

    return app


def _read_json(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_run_dir(runs_root: Path, run_id: str) -> Path:
    run_dir = (runs_root / run_id).resolve()
    try:
        run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid run id") from exc
    if not run_dir.exists() or not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="run not found")
    return run_dir


def _safe_media_path(project_root: Path, runs_root: Path, path: str) -> Path:
    media_path = Path(path).resolve()
    allowed_roots = [project_root, runs_root]
    if not any(_is_relative_to(media_path, root) for root in allowed_roots):
        if not _is_known_source_media(runs_root, media_path):
            raise HTTPException(status_code=403, detail="media path not allowed")
    return media_path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _media_url(path: str | None) -> str:
    if not path:
        return ""
    return f"/media?path={path}"


def _safe_output_name(name: str) -> str:
    safe_name = Path(name).name
    if not safe_name.lower().endswith(".mp4"):
        safe_name += ".mp4"
    return safe_name


def _build_full_timeline(result: dict, overrides: dict) -> dict:
    media = result.get("media", {})
    source_path = media.get("path", "")
    start = 0.0
    end = float(media.get("duration") or _infer_duration(result))
    transcript = result.get("transcript", [])
    disabled_sources = _full_timeline_disabled_sources(result)
    boundaries = {round(start, 3), round(end, 3)}

    for segment in transcript:
        if _range_overlaps(start, end, segment.get("start", 0), segment.get("end", 0)):
            boundaries.add(round(max(start, float(segment.get("start") or start)), 3))
            boundaries.add(round(min(end, float(segment.get("end") or end)), 3))
    for source in disabled_sources:
        boundaries.add(round(max(start, source["start"]), 3))
        boundaries.add(round(min(end, source["end"]), 3))
    for grid in _grid_boundaries(start, end, 4.0):
        boundaries.add(round(grid, 3))

    sorted_boundaries = sorted(value for value in boundaries if start <= value <= end)
    pieces = []
    for index, (piece_start, piece_end) in enumerate(zip(sorted_boundaries, sorted_boundaries[1:]), 1):
        if piece_end - piece_start < 0.08:
            continue
        text = _text_for_range(transcript, piece_start, piece_end)
        source = _matching_disabled_source(piece_start, piece_end, disabled_sources)
        if source is None and _is_short_empty_piece(piece_start, piece_end, text):
            source = {
                "start": piece_start,
                "end": piece_end,
                "kind": "silence",
                "terms": [{"text": "短空档/无有效口播", "kind": "silence"}],
            }
        default_enabled = source is None
        piece_id = f"p{index:03d}_{int(round(piece_start * 1000))}_{int(round(piece_end * 1000))}"
        enabled = bool(overrides.get(piece_id, {}).get("enabled", default_enabled))
        pieces.append(
            {
                "piece_id": piece_id,
                "index": index,
                "start": round(piece_start, 3),
                "end": round(piece_end, 3),
                "local_start": round(piece_start, 3),
                "local_end": round(piece_end, 3),
                "duration": round(piece_end - piece_start, 3),
                "enabled": enabled,
                "default_enabled": default_enabled,
                "kind": source.get("kind", "content") if source else "content",
                "reason": _disabled_reason(source) if source else "算法建议保留",
                "summary": _summary(text),
                "transcript": text,
                "removed_terms": source.get("terms", []) if source else [],
                "candidate_ids": [],
            }
        )

    return {
        "clip_id": "__source__",
        "source_mode": "full_video",
        "start_time": round(start, 3),
        "end_time": round(end, 3),
        "duration": round(end - start, 3),
        "score": max([item.get("score", 0) for item in result.get("candidates", [])] or [0]),
        "tags": [],
        "reason": "完整原视频轨道，默认禁用不可用、不完整和异常内容",
        "video_path": source_path,
        "video_url": _media_url(source_path),
        "cover_path": "",
        "piece_count": len(pieces),
        "enabled_duration": round(sum(piece["duration"] for piece in pieces if piece["enabled"]), 3),
        "disabled_duration": round(sum(piece["duration"] for piece in pieces if not piece["enabled"]), 3),
        "pieces": pieces,
        "candidates": [],
    }


def _find_candidate(result: dict, clip_id: str) -> dict | None:
    for candidate in result.get("candidates", []):
        if candidate.get("clip_id") == clip_id:
            return candidate
    return None


def _build_timeline(result: dict, candidate: dict, overrides: dict) -> dict:
    start = float(candidate.get("start_time") or 0)
    end = float(candidate.get("end_time") or start)
    transcript = result.get("transcript", [])
    indexes = set(candidate.get("segment_indexes") or [])
    candidate_segments = [
        segment
        for index, segment in enumerate(transcript)
        if index in indexes and _range_overlaps(start, end, segment.get("start", 0), segment.get("end", 0))
    ]
    disabled_sources = _timeline_disabled_sources(candidate, candidate_segments)
    boundaries = {round(start, 3), round(end, 3)}
    for segment in candidate_segments:
        boundaries.add(round(max(start, float(segment.get("start") or start)), 3))
        boundaries.add(round(min(end, float(segment.get("end") or end)), 3))
    for source in disabled_sources:
        boundaries.add(round(max(start, source["start"]), 3))
        boundaries.add(round(min(end, source["end"]), 3))

    for grid in _grid_boundaries(start, end, 4.0):
        boundaries.add(round(grid, 3))

    sorted_boundaries = sorted(value for value in boundaries if start <= value <= end)
    pieces = []
    for index, (piece_start, piece_end) in enumerate(zip(sorted_boundaries, sorted_boundaries[1:]), 1):
        if piece_end - piece_start < 0.08:
            continue
        source = _matching_disabled_source(piece_start, piece_end, disabled_sources)
        default_enabled = source is None
        piece_id = f"p{index:03d}_{int(round((piece_start - start) * 1000))}_{int(round((piece_end - start) * 1000))}"
        enabled = bool(overrides.get(piece_id, {}).get("enabled", default_enabled))
        text = _text_for_range(candidate_segments, piece_start, piece_end)
        pieces.append(
            {
                "piece_id": piece_id,
                "index": index,
                "start": round(piece_start, 3),
                "end": round(piece_end, 3),
                "local_start": round(piece_start - start, 3),
                "local_end": round(piece_end - start, 3),
                "duration": round(piece_end - piece_start, 3),
                "enabled": enabled,
                "default_enabled": default_enabled,
                "kind": source.get("kind", "content") if source else "content",
                "reason": _disabled_reason(source) if source else "算法建议保留",
                "summary": _summary(text),
                "transcript": text,
                "removed_terms": source.get("terms", []) if source else [],
            }
        )

    exports = candidate.get("exports", {})
    return {
        "clip_id": candidate.get("clip_id"),
        "start_time": round(start, 3),
        "end_time": round(end, 3),
        "duration": round(end - start, 3),
        "score": candidate.get("score", 0),
        "tags": candidate.get("tags", []),
        "reason": candidate.get("reason", ""),
        "video_path": exports.get("video", ""),
        "video_url": _media_url(exports.get("video")),
        "cover_path": exports.get("cover", ""),
        "piece_count": len(pieces),
        "enabled_duration": round(sum(piece["duration"] for piece in pieces if piece["enabled"]), 3),
        "disabled_duration": round(sum(piece["duration"] for piece in pieces if not piece["enabled"]), 3),
        "pieces": pieces,
    }


def _timeline_disabled_sources(candidate: dict, candidate_segments: list[dict]) -> list[dict]:
    sources: list[dict] = []
    edit_decision = candidate.get("edit_decision") or {}
    terms = edit_decision.get("removed_terms") or []
    for item in edit_decision.get("removed_ranges") or []:
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        matched_terms = [
            term
            for term in terms
            if _range_overlaps(start, end, term.get("start", start), term.get("end", end))
        ]
        sources.append(
            {
                "start": start,
                "end": end,
                "kind": matched_terms[0].get("kind", "cut_suggestion") if matched_terms else "cut_suggestion",
                "terms": matched_terms,
            }
        )
    for segment in candidate_segments:
        invalid_reasons = segment.get("invalid_reasons") or []
        if invalid_reasons:
            sources.append(
                {
                    "start": float(segment.get("start") or 0),
                    "end": float(segment.get("end") or 0),
                    "kind": "low_value",
                    "terms": [{"text": ",".join(invalid_reasons), "kind": "low_value"}],
                }
            )
    return sorted(sources, key=lambda source: source["start"])


def _full_timeline_disabled_sources(result: dict) -> list[dict]:
    sources: list[dict] = []
    transcript = result.get("transcript", [])
    media_info = result.get("media", {})
    start = 0.0
    end = float(media_info.get("duration") or _infer_duration(result))
    sources.extend(_leading_incomplete_disabled_sources(transcript, start))
    for candidate in result.get("candidates", []):
        indexes = set(candidate.get("segment_indexes") or [])
        candidate_segments = [segment for index, segment in enumerate(transcript) if index in indexes]
        sources.extend(_timeline_disabled_sources(candidate, candidate_segments))
    sources.extend(_operational_phrase_disabled_sources(transcript))
    sources.extend(_transcript_quality_disabled_sources(transcript))
    sources.extend(_silence_disabled_sources(transcript, start, end))
    return _merge_disabled_sources(sources)


def _merge_disabled_sources(sources: list[dict]) -> list[dict]:
    normalized = sorted(
        (source for source in sources if source.get("end", 0) > source.get("start", 0)),
        key=lambda source: (source["start"], source["end"]),
    )
    merged: list[dict] = []
    for source in normalized:
        if not merged or source["start"] - merged[-1]["end"] > 0.08 or source.get("kind") != merged[-1].get("kind"):
            merged.append({**source, "terms": list(source.get("terms", []))})
            continue
        merged[-1]["end"] = max(merged[-1]["end"], source["end"])
        merged[-1]["terms"].extend(source.get("terms", []))
    return merged


def _leading_incomplete_disabled_sources(transcript: list[dict], media_start: float = 0.0) -> list[dict]:
    segment = _first_speech_segment(transcript)
    if not segment:
        return []
    try:
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or 0)
    except (TypeError, ValueError):
        return []
    first_word_start = _first_word_start(segment)
    speech_start = first_word_start if first_word_start is not None else start
    if end <= start or speech_start - media_start > 0.18:
        return []
    text = str(segment.get("clean_text") or segment.get("text") or "")
    if _has_opening_cue(text):
        return []
    fragment_end = _leading_fragment_end(segment, start, end)
    if fragment_end - start < 0.4:
        return []
    return [
        {
            "start": max(media_start, start),
            "end": fragment_end,
            "kind": "leading_incomplete",
            "terms": [{"text": "片头疑似半句话", "kind": "leading_incomplete"}],
        }
    ]


def _first_speech_segment(transcript: list[dict]) -> dict | None:
    for segment in transcript:
        try:
            start = float(segment.get("start") or 0)
            end = float(segment.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        if str(segment.get("text") or segment.get("clean_text") or "").strip() or segment.get("words"):
            return segment
    return None


def _first_word_start(segment: dict) -> float | None:
    for word in segment.get("words") or []:
        try:
            return float(word.get("start"))
        except (TypeError, ValueError):
            continue
    return None


def _leading_fragment_end(segment: dict, start: float, end: float) -> float:
    words = []
    for word in segment.get("words") or []:
        try:
            word_start = float(word.get("start"))
            word_end = float(word.get("end"))
        except (TypeError, ValueError):
            continue
        if word_end > word_start:
            words.append((word_start, word_end))
    if end - start <= LEADING_INCOMPLETE_MAX_SEGMENT_DURATION:
        return end
    for (_, previous_end), (next_start, _) in zip(words, words[1:]):
        if previous_end - start >= 1.5 and next_start - previous_end >= 0.45:
            return min(end, previous_end + 0.08)
    return min(end, start + LEADING_INCOMPLETE_MAX_DISABLE_DURATION)


def _has_opening_cue(text: str) -> bool:
    normalized = _normalize_timeline_text(text)
    opening_window = normalized[:18]
    return any(opening_window.startswith(cue) or cue in opening_window for cue in OPENING_CUE_PATTERNS)


def _operational_phrase_disabled_sources(transcript: list[dict]) -> list[dict]:
    phrase_items = sorted(
        ((phrase, _normalize_timeline_text(phrase)) for phrase in OPERATIONAL_IRRELEVANT_PHRASES),
        key=lambda item: len(item[1]),
        reverse=True,
    )
    sources: list[dict] = []
    for segment in transcript:
        words = segment.get("words") or []
        if words:
            sources.extend(_operational_phrase_sources_from_words(words, phrase_items))
            continue
        try:
            start = float(segment.get("start") or 0)
            end = float(segment.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if end <= start or end - start > 8:
            continue
        text = _normalize_timeline_text(segment.get("clean_text") or segment.get("text") or "")
        for phrase, normalized_phrase in phrase_items:
            if normalized_phrase and normalized_phrase in text:
                sources.append(
                    {
                        "start": start,
                        "end": end,
                        "kind": "irrelevant_topic",
                        "terms": [{"text": phrase, "kind": "irrelevant_topic"}],
                    }
                )
                break
    return _dedupe_disabled_sources(sources)


def _operational_phrase_sources_from_words(
    words: list[dict],
    phrase_items: list[tuple[str, str]],
) -> list[dict]:
    tokens: list[dict] = []
    parts: list[str] = []
    offset = 0
    for word in words:
        text = _normalize_timeline_text(word.get("word") or "")
        if not text:
            continue
        try:
            start = float(word.get("start"))
            end = float(word.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        next_offset = offset + len(text)
        tokens.append(
            {
                "start_offset": offset,
                "end_offset": next_offset,
                "start": start,
                "end": end,
            }
        )
        parts.append(text)
        offset = next_offset
    combined = "".join(parts)
    if not combined:
        return []

    sources: list[dict] = []
    for phrase, normalized_phrase in phrase_items:
        if not normalized_phrase:
            continue
        search_from = 0
        while True:
            match_start = combined.find(normalized_phrase, search_from)
            if match_start < 0:
                break
            match_end = match_start + len(normalized_phrase)
            matched_tokens = [
                token
                for token in tokens
                if token["end_offset"] > match_start and token["start_offset"] < match_end
            ]
            if matched_tokens:
                sources.append(
                    {
                        "start": max(0.0, matched_tokens[0]["start"] - 0.04),
                        "end": matched_tokens[-1]["end"] + 0.04,
                        "kind": "irrelevant_topic",
                        "terms": [{"text": phrase, "kind": "irrelevant_topic"}],
                    }
                )
            search_from = match_start + 1
    return _dedupe_disabled_sources(sources)


def _dedupe_disabled_sources(sources: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    for source in sorted(sources, key=lambda item: (item["start"], -(item["end"] - item["start"]))):
        if any(
            kept["start"] <= source["start"] + 0.001 and kept["end"] >= source["end"] - 0.001
            for kept in deduped
        ):
            continue
        deduped.append(source)
    return sorted(deduped, key=lambda item: item["start"])


def _transcript_quality_disabled_sources(transcript: list[dict]) -> list[dict]:
    sources: list[dict] = []
    for segment in transcript:
        try:
            start = float(segment.get("start") or 0)
            end = float(segment.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        text = str(segment.get("text") or "")
        clean_text = str(segment.get("clean_text") or "").strip()
        invalid_reasons = list(segment.get("invalid_reasons") or [])
        kind = ""
        reason_text = ""
        if _has_abnormal_scene(text) and end - start <= 6:
            kind = "abnormal_scene"
            reason_text = "异常场景/直播事故"
        elif invalid_reasons:
            kind = "low_value"
            reason_text = ",".join(invalid_reasons)
        elif _is_incomplete_dialogue(text, clean_text, end - start):
            kind = "incomplete_dialogue"
            reason_text = "不完整对话"
        elif _is_low_value_segment(segment, clean_text):
            kind = "low_value"
            reason_text = "低价值口播"
        if not kind:
            continue
        sources.append(
            {
                "start": start,
                "end": end,
                "kind": kind,
                "terms": [{"text": reason_text, "kind": kind}],
            }
        )
    return sources


def _silence_disabled_sources(transcript: list[dict], start: float, end: float) -> list[dict]:
    speech_ranges: list[tuple[float, float]] = []
    for segment in transcript:
        words = segment.get("words") or []
        if words:
            for word in words:
                try:
                    word_start = float(word.get("start"))
                    word_end = float(word.get("end"))
                except (TypeError, ValueError):
                    continue
                if word_end > word_start:
                    speech_ranges.append((word_start, word_end))
        else:
            try:
                seg_start = float(segment.get("start"))
                seg_end = float(segment.get("end"))
            except (TypeError, ValueError):
                continue
            if seg_end > seg_start:
                speech_ranges.append((seg_start, seg_end))

    protected = _merge_ranges(
        [(max(start, item_start - 0.12), min(end, item_end + 0.12)) for item_start, item_end in speech_ranges],
        max_gap=0.22,
    )
    sources: list[dict] = []
    cursor = start
    for speech_start, speech_end in protected:
        if speech_start - cursor >= 0.65:
            sources.append(
                {
                    "start": cursor,
                    "end": speech_start,
                    "kind": "silence",
                    "terms": [{"text": "停顿/无有效口播", "kind": "silence"}],
                }
            )
        cursor = max(cursor, speech_end)
    if end - cursor >= 0.65:
        sources.append(
            {
                "start": cursor,
                "end": end,
                "kind": "silence",
                "terms": [{"text": "停顿/无有效口播", "kind": "silence"}],
            }
        )
    return sources


def _matching_disabled_source(start: float, end: float, sources: list[dict]) -> dict | None:
    center = start + (end - start) / 2
    matches: list[tuple[dict, float]] = []
    for source in sources:
        overlap = max(0.0, min(end, source["end"]) - max(start, source["start"]))
        overlap_ratio = overlap / max(0.001, end - start)
        if overlap_ratio >= 0.35 or source["start"] <= center <= source["end"]:
            matches.append((source, overlap_ratio))
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: (
            DISABLED_KIND_PRIORITY.get(str(item[0].get("kind")), 0),
            item[1],
            item[0]["end"] - item[0]["start"],
        ),
    )[0]


def _merge_ranges(ranges: list[tuple[float, float]], max_gap: float) -> list[tuple[float, float]]:
    normalized = sorted((start, end) for start, end in ranges if end > start)
    if not normalized:
        return []
    merged: list[tuple[float, float]] = [normalized[0]]
    for start, end in normalized[1:]:
        last_start, last_end = merged[-1]
        if start - last_end <= max_gap:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _text_for_range(segments: list[dict], start: float, end: float) -> str:
    words: list[str] = []
    fallback: list[str] = []
    for segment in segments:
        if not _range_overlaps(start, end, segment.get("start", 0), segment.get("end", 0)):
            continue
        for word in segment.get("words") or []:
            if _word_belongs_to_range(start, end, word):
                text = str(word.get("word") or "").strip()
                if text:
                    words.append(text)
    if words:
        return "".join(words).strip()
    for segment in segments:
        if not _range_overlap_ratio(start, end, segment.get("start", 0), segment.get("end", 0)) >= 0.75:
            continue
        text = str(segment.get("clean_text") or segment.get("text") or "").strip()
        if text:
            fallback.append(text)
    return "\n".join(dict.fromkeys(fallback)).strip()


def _word_belongs_to_range(start: float, end: float, word: dict) -> bool:
    try:
        word_start = float(word.get("start"))
        word_end = float(word.get("end"))
    except (TypeError, ValueError):
        return False
    overlap = max(0.0, min(end, word_end) - max(start, word_start))
    word_duration = max(0.001, word_end - word_start)
    return overlap / word_duration >= 0.5


def _is_short_empty_piece(start: float, end: float, text: str) -> bool:
    return end - start < 0.7 and not str(text or "").strip()


def _disabled_reason(source: dict | None) -> str:
    if not source:
        return "算法建议保留"
    labels = {
        "short_filler": "水词/语气词，默认不播放",
        "live_filler_phrase": "直播模板水词，默认不播放",
        "audio_cough_like": "疑似咳嗽或突发噪声，默认不播放",
        "cough": "咳嗽音，默认不播放",
        "low_value": "低价值或无效口播，默认不播放",
        "irrelevant_topic": "与产品介绍无关，默认不播放",
        "leading_incomplete": "片头疑似半句话，默认不播放",
        "incomplete_dialogue": "不完整对话，默认不播放",
        "abnormal_scene": "异常场景，默认不播放",
        "silence": "停顿或无有效口播，默认不播放",
        "cut_suggestion": "算法建议跳过，默认不播放",
    }
    return labels.get(source.get("kind"), "算法建议跳过，默认不播放")


def _summary(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text[:48] + ("..." if len(text) > 48 else "")


TIMELINE_TEXT_CLEAN_RE = re.compile(r"""[\s,，。.!！?？、~～…:：;；"'“”‘’()\[\]{}<>《》]+""")

OPERATIONAL_IRRELEVANT_PHRASES = [
    "辛苦我们的后台务实看一下大屏幕",
    "辛苦我们的后台看一下大屏幕",
    "辛苦后台看一下大屏幕",
    "后台务实看一下大屏幕",
    "后台看一下大屏幕",
    "看一下大屏幕",
    "辛苦我们的后台",
    "辛苦后台",
    "后台务实",
    "大屏幕",
    "看一下后台",
    "后台老师",
    "中控看一下",
    "中控老师",
    "麻烦后台",
    "麻烦中控",
    "上链接",
    "改价格",
    "改库存",
    "马上下播",
    "准备下播",
    "快下播",
    "要下播",
    "下播",
    "排单",
    "排一下单",
    "排队",
    "排到了",
    "截单",
    "停播",
    "不播",
    "直播间",
    "库存",
    "公域",
    "私域",
    "拼手速",
    "粉丝团",
    "点关注",
    "公屏",
    "弹幕",
]

DISABLED_KIND_PRIORITY = {
    "irrelevant_topic": 90,
    "abnormal_scene": 80,
    "audio_cough_like": 70,
    "cough": 70,
    "leading_incomplete": 65,
    "live_filler_phrase": 60,
    "short_filler": 55,
    "incomplete_dialogue": 50,
    "low_value": 40,
    "silence": 30,
    "cut_suggestion": 20,
}

LEADING_INCOMPLETE_MAX_SEGMENT_DURATION = 7.0
LEADING_INCOMPLETE_MAX_DISABLE_DURATION = 6.0

OPENING_CUE_PATTERNS = [
    "大家好",
    "哈喽",
    "hello",
    "欢迎",
    "今天",
    "这款",
    "这把",
    "这一款",
    "这一把",
    "我们今天",
    "接下来",
    "先来看",
    "来看一下",
    "给大家介绍",
    "给大家看",
]


def _normalize_timeline_text(text: str) -> str:
    return TIMELINE_TEXT_CLEAN_RE.sub("", str(text or "").lower())


ABNORMAL_PATTERNS = [
    "等一下",
    "听得到吗",
    "能听到吗",
    "卡了吗",
    "卡了",
    "看得到吗",
    "黑屏",
    "没声音",
    "没有声音",
    "断了",
    "等我一下",
    "我看一下",
    "喝口水",
    "后台",
    "大屏幕",
]

INCOMPLETE_STARTERS = [
    "然后",
    "就是",
    "这个",
    "那个",
    "所以",
    "因为",
    "但是",
    "如果",
    "那么",
    "那",
    "包括",
    "的",
    "了",
    "和",
    "还有",
]

INCOMPLETE_ENDINGS = [
    "然后",
    "因为",
    "所以",
    "但是",
    "如果",
    "这个",
    "那个",
    "就是",
    "可以",
    "的话",
    "什么呢",
    "的",
    "到",
    "一个",
    "一款",
    "这款",
    "那款",
]

WEAK_LIVE_PATTERNS = [
    "看一下",
    "看一看",
    "说一下",
    "讲一下",
    "给大家",
    "再来",
    "这边",
]


def _has_abnormal_scene(text: str) -> bool:
    normalized = str(text or "").replace(" ", "")
    return any(pattern in normalized for pattern in ABNORMAL_PATTERNS)


def _is_incomplete_dialogue(text: str, clean_text: str, duration: float) -> bool:
    normalized = (clean_text or text or "").replace(" ", "")
    if not normalized:
        return True
    if len(normalized) <= 5 and duration <= 3.5:
        return True
    if any(normalized.endswith(ending) for ending in INCOMPLETE_ENDINGS):
        return True
    if duration <= 4.5 and len(normalized) <= 16 and any(normalized.startswith(starter) for starter in INCOMPLETE_STARTERS):
        return True
    return False


def _is_low_value_segment(segment: dict, clean_text: str) -> bool:
    try:
        filler_ratio = float(segment.get("filler_ratio") or 0)
        repeat_ratio = float(segment.get("repeat_ratio") or 0)
    except (TypeError, ValueError):
        filler_ratio = 0
        repeat_ratio = 0
    raw_text = str(segment.get("text") or "").replace(" ", "")
    clean_length = len((clean_text or raw_text).replace(" ", ""))
    if clean_length >= 12 and _has_opening_cue(raw_text):
        return False
    if filler_ratio >= 0.16 and clean_length <= 14:
        return True
    if repeat_ratio >= 0.18:
        return True
    if clean_length <= 9 and float(segment.get("end") or 0) - float(segment.get("start") or 0) <= 3.2:
        return True
    if clean_length <= 18 and any(pattern in raw_text for pattern in WEAK_LIVE_PATTERNS):
        return True
    return False


def _candidate_hits(candidates: list[dict], start: float, end: float) -> list[dict]:
    return [
        candidate
        for candidate in candidates
        if _range_overlaps(start, end, candidate.get("start_time", 0), candidate.get("end_time", 0))
    ]


def _select_recommended_candidates(
    candidates: list[dict],
    *,
    limit: int = 4,
    max_overlap_ratio: float = 0.28,
) -> list[dict]:
    selected: list[dict] = []
    sorted_candidates = sorted(candidates, key=lambda item: item.get("score", 0), reverse=True)
    for candidate in sorted_candidates:
        if len(selected) >= limit:
            break
        if not selected or all(_overlap_ratio(candidate, item) <= max_overlap_ratio for item in selected):
            selected.append(candidate)
    if len(selected) < min(limit, len(sorted_candidates)):
        for candidate in sorted_candidates:
            if len(selected) >= limit:
                break
            if candidate not in selected:
                selected.append(candidate)
    return sorted(selected, key=lambda item: float(item.get("start_time") or 0))


def _overlap_ratio(left: dict, right: dict) -> float:
    left_start = float(left.get("start_time") or 0)
    left_end = float(left.get("end_time") or left_start)
    right_start = float(right.get("start_time") or 0)
    right_end = float(right.get("end_time") or right_start)
    overlap = max(0.0, min(left_end, right_end) - max(left_start, right_start))
    shorter = max(0.001, min(left_end - left_start, right_end - right_start))
    return overlap / shorter


def _merge_export_ranges(ranges: list[dict[str, float]]) -> list[dict[str, float]]:
    normalized = sorted(
        ({"start": float(item["start"]), "end": float(item["end"])} for item in ranges if item["end"] > item["start"]),
        key=lambda item: item["start"],
    )
    merged: list[dict[str, float]] = []
    for item in normalized:
        if not merged or item["start"] - merged[-1]["end"] > 0.08:
            merged.append({"start": round(item["start"], 3), "end": round(item["end"], 3)})
        else:
            merged[-1]["end"] = round(max(merged[-1]["end"], item["end"]), 3)
    return merged


def _candidate_summary(candidates: list[dict]) -> str:
    if not candidates:
        return ""
    best = max(candidates, key=lambda item: item.get("score", 0))
    return _summary(best.get("clean_transcript") or best.get("transcript") or best.get("reason") or "")


def _infer_duration(result: dict) -> float:
    values = [float(segment.get("end") or 0) for segment in result.get("transcript", [])]
    values.extend(float(candidate.get("end_time") or 0) for candidate in result.get("candidates", []))
    return max(values or [0.0])


def _is_known_source_media(runs_root: Path, media_path: Path) -> bool:
    if not runs_root.exists():
        return False
    for result_path in runs_root.glob("*/metadata/result.json"):
        try:
            result = _read_json(result_path)
            source = Path(result.get("media", {}).get("path", "")).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if source == media_path:
            return True
    return False


def _range_overlaps(left_start, left_end, right_start, right_end) -> bool:
    return min(float(left_end), float(right_end)) > max(float(left_start), float(right_start))


def _range_overlap_ratio(left_start, left_end, right_start, right_end) -> float:
    left_start = float(left_start)
    left_end = float(left_end)
    right_start = float(right_start)
    right_end = float(right_end)
    overlap = max(0.0, min(left_end, right_end) - max(left_start, right_start))
    return overlap / max(0.001, left_end - left_start)


def _grid_boundaries(start: float, end: float, step: float) -> list[float]:
    values = []
    cursor = start + step
    while cursor < end:
        values.append(cursor)
        cursor += step
    return values


app = create_app()
