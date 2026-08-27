from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from voice_realtime.lm_studio import LMStudioClient, NativeChatRequest

from .dataset import EvaluationDataset, EvaluationQuestion, Evidence, load_dataset


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
    parts: list[str] = []
    saw_end = False
    async for event in client.stream_chat(request):
        if event.type == "message.delta" and event.content:
            parts.append(event.content)
        elif event.type == "chat.end":
            saw_end = True
    if not saw_end or not parts:
        raise ValueError("LM Studio stream did not produce a complete message")
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Inner OS P0 benchmark")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--base-url", default="http://127.0.0.1:1234")
    parser.add_argument("--api-key", default="lm-studio")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    dataset = load_dataset(args.dataset)
    if not args.dry_run:
        model = args.model
        if not model:
            raise SystemExit("--model is required for real P0 execution")
        async def run() -> None:
            client = LMStudioClient(base_url=args.base_url, api_key=args.api_key)
            try:
                for question in dataset.questions:
                    meeting = next(
                        item for item in dataset.meetings if item.meeting_id == question.meeting_id
                    )
                    await evaluate_question(client, question, list(meeting.segments), model=model)
            finally:
                await client.aclose()
        asyncio.run(run())
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(
        json.dumps(report_for_dataset(dataset), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
