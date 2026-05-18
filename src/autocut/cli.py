from __future__ import annotations

import argparse
import json
from pathlib import Path

from .llm import LLMError, generate_ordered_ids
from .media import probe_video
from .models import PipelineRequest, to_plain_dict
from .pipeline import LiveClipPipeline
from .remix import (
    build_remix_source,
    export_remix_plan,
    remix_plan_from_ordered_ids,
    write_remix_plan,
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return run_command(args)
    if args.command == "probe":
        return probe_command(args)
    if args.command == "remix":
        return remix_command(args)
    parser.print_help()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autocut", description="直播素材智能切片 MVP")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="处理单条直播素材")
    run_parser.add_argument("video", type=Path, help="直播录屏路径")
    run_parser.add_argument("--product", required=True, help="产品名称")
    run_parser.add_argument("--selling-point", action="append", default=[], help="核心卖点，可重复传入")
    run_parser.add_argument("--selling-points", default="", help="逗号分隔的卖点")
    run_parser.add_argument("--transcript", type=Path, help="已有转写文件，支持 JSON/SRT")
    run_parser.add_argument(
        "--asr",
        default="transcript",
        choices=[
            "transcript",
            "faster-whisper",
            "funasr",
            "fun-asr",
            "glm-asr",
            "glm",
            "whisper-api",
            "remote-whisper",
            "faster-whisper-api",
        ],
    )
    run_parser.add_argument("--asr-model", default="small", help="ASR 模型名，faster-whisper 默认 small")
    run_parser.add_argument("--asr-device", default="cpu", help="ASR 设备，默认 cpu；有 CUDA 环境时可传 cuda")
    run_parser.add_argument("--asr-compute-type", default="int8", help="ASR 计算类型，CPU 默认 int8")
    run_parser.add_argument("--asr-beam-size", type=int, default=5, help="ASR beam size，越大越慢但可能更稳")
    run_parser.add_argument("--brand-term", action="append", default=[], help="额外品牌词/热词，可重复传入")
    run_parser.add_argument("--brand-terms-file", type=Path, help="品牌词文件，每行一个词")
    run_parser.add_argument("--no-default-brand-terms", action="store_true", help="不使用内置品牌词")
    run_parser.add_argument("--language", default="zh")
    run_parser.add_argument("--output", type=Path, default=None)
    run_parser.add_argument("--max-candidates", type=int, default=8)
    run_parser.add_argument("--min-start-time", type=float, default=0.0, help="候选切片最早起点，默认不跳过开头")
    run_parser.add_argument("--max-overlap-ratio", type=float, default=0.45, help="候选切片最大重叠率")
    run_parser.add_argument("--no-compact-export", action="store_true", help="不额外导出去水词/紧凑版视频")
    run_parser.add_argument("--compact-merge-gap", type=float, default=0.45, help="紧凑版中保留的最大自然停顿秒数")

    probe_parser = subparsers.add_parser("probe", help="查看媒体信息")
    probe_parser.add_argument("video", type=Path)

    remix_parser = subparsers.add_parser("remix", help="生成或导出 25 秒重排脚本")
    remix_parser.add_argument("run_dir", type=Path, help="已处理批次目录")
    remix_parser.add_argument("--target-duration", type=float, default=25.0, help="目标成片时长")
    remix_parser.add_argument("--ordered-ids", default="", help="模型返回的字幕 ID 顺序，逗号分隔")
    remix_parser.add_argument("--llm", action="store_true", help="调用模型生成字幕 ID 顺序")
    remix_parser.add_argument("--llm-stream", action="store_true", help="以流式方式调用模型并聚合结果")
    remix_parser.add_argument("--llm-model", default=None, help="覆盖 AUTOCUT_LLM_MODEL")
    remix_parser.add_argument("--no-export", action="store_true", help="只生成脚本方案，不导出视频")
    remix_parser.add_argument("--output", type=Path, default=None, help="导出视频路径")
    return parser


def run_command(args: argparse.Namespace) -> int:
    output_dir = args.output or Path("data") / "runs" / args.video.stem
    selling_points = list(args.selling_point)
    if args.selling_points:
        selling_points.extend(
            point.strip() for point in args.selling_points.replace("，", ",").split(",") if point.strip()
        )

    request = PipelineRequest(
        video_path=args.video,
        product=args.product,
        selling_points=selling_points,
        output_dir=output_dir,
        transcript_path=args.transcript,
        asr_engine=args.asr,
        asr_model=args.asr_model,
        asr_device=args.asr_device,
        asr_compute_type=args.asr_compute_type,
        asr_beam_size=args.asr_beam_size,
        brand_terms=args.brand_term,
        brand_terms_path=args.brand_terms_file,
        use_default_brand_terms=not args.no_default_brand_terms,
        language=args.language,
        min_start_time=args.min_start_time,
        max_overlap_ratio=args.max_overlap_ratio,
        max_candidates=args.max_candidates,
        export_compact=not args.no_compact_export,
        compact_merge_gap=args.compact_merge_gap,
    )
    result = LiveClipPipeline().run(request)

    print(f"Output: {result.output_dir}")
    print(f"Candidates: {len(result.candidates)}")
    for candidate in result.candidates[:5]:
        print(
            f"- {candidate.clip_id} score={candidate.score} "
            f"{candidate.start_time:.1f}-{candidate.end_time:.1f}s "
            f"tags={','.join(candidate.tags)}"
        )
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"- {warning}")
    return 0


def probe_command(args: argparse.Namespace) -> int:
    info = probe_video(args.video)
    print(json.dumps(to_plain_dict(info), ensure_ascii=False, indent=2))
    return 0


def remix_command(args: argparse.Namespace) -> int:
    run_dir = args.run_dir
    result_path = run_dir / "metadata" / "result.json"
    request_path = run_dir / "metadata" / "request.json"
    if not result_path.exists():
        raise FileNotFoundError(result_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    request = json.loads(request_path.read_text(encoding="utf-8")) if request_path.exists() else {}
    remix_source = build_remix_source(result, request, target_duration=args.target_duration)
    ordered_ids = [item.strip() for item in args.ordered_ids.replace("，", ",").split(",") if item.strip()]
    model_result = None
    if args.llm and not ordered_ids:
        try:
            model_result = generate_ordered_ids(
                remix_source["prompt"],
                stream=args.llm_stream,
                model=args.llm_model,
            )
        except LLMError as exc:
            print(f"LLM error: {exc}")
            return 2
        ordered_ids = model_result["ordered_ids"]
    if ordered_ids:
        plan = remix_plan_from_ordered_ids(
            remix_source["units"],
            ordered_ids,
            target_duration=args.target_duration,
        )
        if model_result:
            plan["model_reason"] = model_result.get("reason", "")
            plan["model_raw_content"] = model_result.get("raw_content", "")
    else:
        plan = remix_source["default_plan"]

    metadata_dir = run_dir / "metadata"
    plan_path = metadata_dir / f"remix_{int(round(args.target_duration))}s_plan.json"
    prompt_path = metadata_dir / f"remix_{int(round(args.target_duration))}s_prompt.txt"
    write_remix_plan(plan_path, plan)
    prompt_path.write_text(remix_source["prompt"], encoding="utf-8")

    print(f"Plan: {plan_path}")
    print(f"Prompt: {prompt_path}")
    print(f"Duration: {plan['duration']:.1f}s")
    print(f"Ordered IDs: {','.join(plan['ordered_ids'])}")
    if args.no_export:
        print(plan["script_text"])
        return 0

    output_path = args.output or run_dir / "exports" / f"{run_dir.name}_remix_{int(round(args.target_duration))}s.mp4"
    warning = export_remix_plan(result, plan, output_path)
    if warning:
        print(f"Warning: {warning}")
    else:
        print(f"Video: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
