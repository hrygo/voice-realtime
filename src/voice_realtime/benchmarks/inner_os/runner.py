from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from voice_realtime.config import InteractionSettings
from voice_realtime.lm_studio import LMStudioClient, NativeChatRequest

from .dataset import EvaluationDataset, EvaluationQuestion, Evidence, load_dataset
from .metrics import HumanRating


def build_payload(
    question: EvaluationQuestion, evidence: list[Evidence], *, model: str = ""
) -> dict[str, Any]:
    evidence_text = "\n".join(
        f"{index + 1:04d}: {item.text}" for index, item in enumerate(evidence)
    )
    return {
        "model": model,
        "system_prompt": "仅依据给定合成会议证据回答；证据不足时明确拒答。",
        "input": f"问题：{question.question}\n证据：\n{evidence_text}",
        "stream": True,
        "store": False,
        "reasoning": "off",
    }


def report_for_dataset(dataset: EvaluationDataset) -> dict[str, Any]:
    return {
        "status": "pending",
        "question_count": len(dataset.questions),
        "meeting_types": sorted({meeting.meeting_type for meeting in dataset.meetings}),
        "expected_insufficient_count": sum(q.expected_insufficient for q in dataset.questions),
        "metrics": None,
        "failure_categories": {},
    }


def load_ratings(path: Path) -> list[HumanRating]:
    """Load only the content-free offline reviewer schema."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("ratings file must contain a list")
    try:
        return [HumanRating.model_validate(item) for item in raw]
    except Exception as exc:
        raise ValueError("ratings file contains unknown or invalid fields") from exc


async def evaluate_question(
    client: LMStudioClient,
    question: EvaluationQuestion,
    evidence: list[Evidence],
    *,
    model: str = "",
) -> str:
    """Consume one native stream in memory; never persist its content."""
    request = NativeChatRequest(
        model=model,
        input=build_payload(question, evidence, model=model)["input"],
        system_prompt="仅依据给定合成会议证据回答；证据不足时明确拒答。",
        reasoning="off",
        stream=True,
        store=False,
    )
    completion = await client.complete_chat(request)
    if not completion.text:
        raise ValueError("LM Studio stream did not produce a complete message")
    return completion.text


async def run_evaluation(
    client: LMStudioClient, dataset: EvaluationDataset, *, model: str
) -> dict[str, Any]:
    completed = 0
    failures: dict[str, int] = {}
    for question in dataset.questions:
        meeting = next(
            item for item in dataset.meetings if item.meeting_id == question.meeting_id
        )
        try:
            await evaluate_question(client, question, list(meeting.segments), model=model)
        except Exception as exc:
            category = type(exc).__name__
            failures[category] = failures.get(category, 0) + 1
        else:
            completed += 1
        print(
            f"evaluated {completed + sum(failures.values())}/{len(dataset.questions)}",
            file=sys.stderr,
        )
    return {
        "status": "pending_review",
        "question_count": len(dataset.questions),
        "completed_count": completed,
        "failed_count": len(dataset.questions) - completed,
        "failure_categories": failures,
        "review_required": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Inner OS P0 benchmark")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--api-key-stdin", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    dataset = load_dataset(args.dataset)
    if not args.dry_run:
        model = args.model
        if not model:
            raise SystemExit("--model is required for real P0 execution")
        settings = InteractionSettings()
        api_key = (
            sys.stdin.readline().strip()
            if args.api_key_stdin
            else args.api_key or settings.llm_api_key
        )
        if not api_key:
            raise SystemExit("an API key is required")
        result: dict[str, Any]

        async def run() -> None:
            nonlocal result
            client = LMStudioClient(
                base_url=args.base_url or settings.llm_base_url,
                api_key=api_key,
            )
            try:
                result = await run_evaluation(client, dataset, model=model)
            finally:
                await client.aclose()
        asyncio.run(run())
        summary = result
    else:
        summary = report_for_dataset(dataset)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
