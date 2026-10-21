"""Generate reproducible guardrail evaluation artifacts from labeled fixtures.

Safety acceptance is measured against human-authored desired outcomes. Known
limitations remain FAIL rows; they are never removed to inflate the score.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import platform

from app import ROOT, load_references
from guardrails import (inspect_input, inspect_output, parse_json, schema_errors,
                        grounding_errors, matching_rules, sensitive_information,
                        INJECTION_PATTERNS, TOPIC_PATTERNS)


def evaluate_case(case: dict, references: dict[str, str]) -> dict:
    refs = case.get("references", references)
    if case["kind"] == "input":
        prompt = case["input"]
        decision = inspect_input(prompt)
        actual = decision.action
        predictions = {
            "pii": bool(sensitive_information(prompt)),
            "injection": bool(matching_rules(prompt, INJECTION_PATTERNS)),
            "sensitive_topic": bool(matching_rules(prompt, TOPIC_PATTERNS)),
        }
        details = {"categories": list(decision.categories), "pii_types": list(decision.pii_types),
                   "sanitized": decision.sanitized}
    else:
        raw = case["input"]
        decision = inspect_output(raw, refs)
        actual = "accept" if decision.accepted else "fallback"
        try:
            value = parse_json(raw)
            invalid_schema = bool(schema_errors(value))
        except (ValueError, RecursionError):
            value, invalid_schema = None, True
        predictions = {"invalid_schema": invalid_schema}
        if not invalid_schema:
            predictions["ungrounded"] = bool(grounding_errors(value, refs))
            rendered = " ".join([value["answer"]] +
                                [s for claim in value["claims"] for s in claim.values()])
            predictions["sensitive_output"] = bool(sensitive_information(rendered))
        details = {"reasons": list(decision.reasons)}
    labels = case.get("labels", {})
    if not set(labels) <= set(predictions):
        raise ValueError(f"Case {case['id']} has labels for a guard that could not run")
    return {**case, "references": refs, "actual": actual, "details": details,
            "predictions": predictions, "passed": actual == case["expected"]}


def metrics(rows: list[dict]) -> dict:
    groups = sorted({name for row in rows for name in row.get("labels", {})})
    result = {}
    for group in groups:
        counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        false_positives, false_negatives = [], []
        for row in rows:
            if group not in row.get("labels", {}):
                continue
            actual, predicted = row["labels"][group], row["predictions"][group]
            bucket = ("t" if actual == predicted else "f") + ("p" if predicted else "n")
            counts[bucket] += 1
            if bucket == "fp":
                false_positives.append(row["id"])
            if bucket == "fn":
                false_negatives.append(row["id"])
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        result[group] = {**counts, "n": sum(counts.values()),
                         "precision": tp / (tp + fp) if tp + fp else None,
                         "recall": tp / (tp + fn) if tp + fn else None,
                         "false_positive_cases": false_positives,
                         "false_negative_cases": false_negatives}
    return result


def generate(output: Path) -> dict:
    cases = json.loads((ROOT / "tests" / "cases.json").read_text())
    rows = [evaluate_case(case, load_references()) for case in cases]
    summary = {"case_count": len(rows), "passed": sum(row["passed"] for row in rows),
               "failed": sum(not row["passed"] for row in rows),
               "python_version": platform.python_version(), "provider": "deterministic fixtures",
               "metrics": metrics(rows),
               "failed_cases": [r["id"] for r in rows if not r["passed"]]}
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    (output / "metrics.json").write_text(json.dumps(summary, indent=2) + "\n")
    with (output / "results.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, lineterminator="\n", fieldnames=["id", "kind", "input", "references", "expected", "actual", "passed", "details"])
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(row[key], ensure_ascii=False) if isinstance(row[key], (dict, list))
                             else row[key] for key in writer.fieldnames})
    lines = ["# Executed evaluation", "", f"Python {summary['python_version']}; deterministic fixtures; no live LLM calls.", "",
             f"**{summary['passed']}/{len(rows)} desired outcomes met; {summary['failed']} observed limitations.**", "",
             "Full inputs, reference facts, expected/actual outcomes, detector predictions, and reasons are in [results.json](results.json) and [results.csv](results.csv).", "",
             "| Guard (positive = detected risk) | N | TP | FP | TN | FN | Precision | Recall |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, m in summary["metrics"].items():
        p = "undefined" if m["precision"] is None else f"{m['precision']:.3f}"
        r = "undefined" if m["recall"] is None else f"{m['recall']:.3f}"
        lines.append(f"| {name} | {m['n']} | {m['tp']} | {m['fp']} | {m['tn']} | {m['fn']} | {p} | {r} |")
    lines += ["", "| Case | Scenario | Expected | Actual | Result |", "|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['id']} | {row['description']} | {row['expected']} | {row['actual']} | {'PASS' if row['passed'] else '**FAIL**'} |")
    (output / "RESULTS.md").write_text("\n".join(lines) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evaluation")
    parser.add_argument("--strict", action="store_true", help="Exit 1 if any desired safety outcome is unmet")
    args = parser.parse_args()
    summary = generate(args.output)
    print(json.dumps(summary, indent=2))
    if args.strict and summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
