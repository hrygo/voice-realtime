from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .dataset import EvaluationDataset, EvaluationQuestion, Evidence, load_dataset


def build_payload(question: EvaluationQuestion, evidence: list[Evidence]) -> dict[str, Any]:
    evidence_text = "\n".join(
        f"{index + 1:04d}: {item.text}" for index, item in enumerate(evidence)
    )
    return {
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Inner OS P0 benchmark")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    dataset = load_dataset(args.dataset)
    if not args.dry_run:
        raise SystemExit("real P0 execution requires local LM Studio and offline reviewer files")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(
        json.dumps(report_for_dataset(dataset), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
