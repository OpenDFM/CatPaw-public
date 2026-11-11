#!/usr/bin/env python3
"""Grade CatPaw benchmark runs against answer-key acceptance criteria.

Pairs the runner state written by ``run_benchmarks.py`` (one
``.py_state/<category>__<case>.json`` file per case, carrying the agent's filled
``benchmark_result`` snapshot) with the matching ``answer/<case>_answer.json``
and checks every metric in ``acceptance_criteria`` against its tolerance.

A case passes iff its run finished ``OK`` and *every* scored metric lands within
tolerance. Output is a human-readable report plus an optional machine-readable
JSON (``--json``).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Candidate locations for the runner state dir, in priority order. The first
# matches run_benchmarks.py's default (workspace/_benchmark_logs/.py_state); the
# second is the copy that may sit next to this script.
STATE_DIR_CANDIDATES = (
    Path(__file__).resolve().parent.parent / "workspace" / "_benchmark_logs" / ".py_state",
    Path(__file__).resolve().parent / ".py_state",
)
TERMINAL_OK = "OK"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object in {path}")
    return payload


@dataclass
class MetricResult:
    name: str
    expected: float
    tolerance: float
    tolerance_type: str
    unit: str
    actual: Any
    passed: bool
    reason: str = ""
    magnitude: bool = False

    @property
    def delta(self) -> float | None:
        if isinstance(self.actual, (int, float)) and not isinstance(self.actual, bool) and is_number(self.expected):
            a, e = float(self.actual), float(self.expected)
            return (abs(a) - abs(e)) if self.magnitude else (a - e)
        return None


@dataclass
class CaseResult:
    category: str
    case: str
    status: str
    state_path: Path
    answer_path: Path | None
    metrics: list[MetricResult] = field(default_factory=list)
    tags: dict[str, Any] = field(default_factory=dict)
    verdict: str = "PENDING"  # PASS | FAIL | ERROR
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def fmt_cell(value: Any) -> str:
    """Render a metric value for human/CSV output; '' for None/NaN."""
    if value is None:
        return ""
    if is_number(value):
        return "" if (isinstance(value, float) and math.isnan(value)) else f"{value:g}"
    return str(value)


def norm_cat(value: Any) -> str:
    """Normalise a categorical value (e.g. element symbol) for exact matching."""
    if value is None:
        return "none"
    s = str(value).strip().lower()
    return "none" if s in {"", "none", "null", "n/a", "na"} else s


def within_tolerance(actual: float, expected: float, tol: float, tol_type: str) -> bool:
    if not math.isfinite(actual):
        return False
    diff = abs(actual - expected)
    if tol_type == "relative":
        return diff <= tol * abs(expected)
    # default / "absolute"
    return diff <= tol


def grade_metric(name: str, spec: dict[str, Any], result: dict[str, Any] | None) -> MetricResult:
    expected = spec.get("value")
    tol = spec.get("tolerance")
    tol_type = spec.get("tolerance_type", "absolute")
    unit = spec.get("unit", "")

    # Categorical / exact-match metric (e.g. which element carries the spin).
    # Signalled by a string `value`; tolerance does not apply.
    if isinstance(expected, str):
        if result is None:
            return MetricResult(
                name, expected, float("nan"), tol_type, unit, None, False, "no benchmark_result in state"
            )
        actual = result.get(name)
        if actual is None or (isinstance(actual, str) and not actual.strip()):
            return MetricResult(
                name, expected, float("nan"), tol_type, unit, actual, False, "metric absent / null / empty in result"
            )
        ok = norm_cat(actual) == norm_cat(expected)
        return MetricResult(
            name, expected, float("nan"), tol_type, unit, actual, ok, "" if ok else "categorical mismatch"
        )

    if not is_number(expected) or not is_number(tol):
        return MetricResult(
            name, float("nan"), float("nan"), tol_type, unit, None, False, "answer spec missing numeric value/tolerance"
        )

    if result is None:
        return MetricResult(name, expected, tol, tol_type, unit, None, False, "no benchmark_result in state")

    actual = result.get(name)
    if actual is None:
        return MetricResult(name, expected, tol, tol_type, unit, None, False, "metric absent / null in result")
    if not is_number(actual):
        return MetricResult(name, expected, tol, tol_type, unit, actual, False, "result value is not numeric")

    # `magnitude: true` compares |actual| vs |expected| so a sign/sublattice
    # choice (e.g. an AFM site moment reported as -m) still grades correctly.
    magnitude = bool(spec.get("magnitude"))
    if magnitude:
        ok = within_tolerance(abs(float(actual)), abs(expected), tol, tol_type)
    else:
        ok = within_tolerance(float(actual), expected, tol, tol_type)
    return MetricResult(name, expected, tol, tol_type, unit, float(actual), ok, magnitude=magnitude)


def resolve_answer_path(benchmark_root: Path, category: str, case_name: str) -> Path:
    base = case_name[: -len("_test")] if case_name.endswith("_test") else case_name
    return benchmark_root / category / "answer" / f"{base}_answer.json"


def grade_case(state_path: Path, benchmark_root: Path) -> CaseResult:
    try:
        state = load_json(state_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return CaseResult("?", state_path.stem, "?", state_path, None, verdict="ERROR", note=f"unreadable state: {exc}")

    category = str(state.get("category", "?"))
    case_name = str(state.get("case_name", state_path.stem))
    status = str(state.get("status", "UNKNOWN"))
    result = state.get("benchmark_result")
    if not isinstance(result, dict):
        result = None

    answer_path = resolve_answer_path(benchmark_root, category, case_name)
    case = CaseResult(category, case_name, status, state_path, answer_path)

    if not answer_path.is_file():
        case.verdict = "ERROR"
        case.note = f"answer key not found: {answer_path}"
        return case
    try:
        answer = load_json(answer_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        case.verdict = "ERROR"
        case.note = f"unreadable answer: {exc}"
        return case

    criteria = answer.get("acceptance_criteria")
    if not isinstance(criteria, dict) or not criteria:
        case.verdict = "ERROR"
        case.note = "answer has no acceptance_criteria"
        return case

    if isinstance(answer.get("tags"), dict):
        case.tags = answer["tags"]
    case.metrics = [grade_metric(name, spec, result) for name, spec in criteria.items()]

    if status != TERMINAL_OK:
        case.verdict = "FAIL"
        case.note = f"run status={status}"
        return case

    case.verdict = "PASS" if all(m.passed for m in case.metrics) else "FAIL"
    return case


def discover_state_dir(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_dir():
            raise NotADirectoryError(f"--state-dir is not a directory: {path}")
        return path
    for candidate in STATE_DIR_CANDIDATES:
        if candidate.is_dir() and any(candidate.glob("*.json")):
            return candidate
    raise FileNotFoundError("Could not locate a .py_state directory with state files; pass --state-dir")


def fmt_metric(m: MetricResult) -> str:
    mark = "ok  " if m.passed else "FAIL"
    actual = "—" if m.actual is None else fmt_cell(m.actual)
    tol = fmt_cell(m.tolerance)
    line = f"      [{mark}] {m.name}: got {actual}  expected {fmt_cell(m.expected)}"
    if tol:
        line += f" ±{tol}"
    line += f" ({m.tolerance_type})"
    if m.delta is not None:
        line += f"  Δ={m.delta:+g}"
    if m.reason:
        line += f"  <- {m.reason}"
    return line


def print_report(cases: list[CaseResult], *, verbose: bool) -> None:
    by_cat: dict[str, list[CaseResult]] = {}
    for c in cases:
        by_cat.setdefault(c.category, []).append(c)

    print("=== CatPaw benchmark grading ===\n")
    for category in sorted(by_cat):
        group = by_cat[category]
        passed = sum(c.passed for c in group)
        gradable = sum(c.verdict in {"PASS", "FAIL"} for c in group)
        print(
            f"## {category}   {passed}/{gradable} passed"
            + (f"   ({len(group) - gradable} errored)" if len(group) - gradable else "")
        )
        for c in sorted(group, key=lambda x: x.case):
            tag = {"PASS": "PASS", "FAIL": "FAIL", "ERROR": "ERR "}.get(c.verdict, c.verdict)
            note = f"  ({c.note})" if c.note else ""
            print(f"  [{tag}] {c.case}{note}")
            if verbose or c.verdict != "PASS":
                for m in c.metrics:
                    print(fmt_metric(m))
        print()

    total = len(cases)
    gradable = sum(c.verdict in {"PASS", "FAIL"} for c in cases)
    passed = sum(c.passed for c in cases)
    errored = total - gradable
    rate = f"{100 * passed / gradable:.1f}%" if gradable else "n/a"
    print("=== summary ===")
    print(
        f"cases: {total}   gradable: {gradable}   passed: {passed}   failed: {gradable - passed}   errored: {errored}"
    )
    print(f"pass rate (of gradable): {rate}")


def to_payload(cases: list[CaseResult]) -> dict[str, Any]:
    gradable = sum(c.verdict in {"PASS", "FAIL"} for c in cases)
    passed = sum(c.passed for c in cases)
    return {
        "summary": {
            "cases": len(cases),
            "gradable": gradable,
            "passed": passed,
            "failed": gradable - passed,
            "errored": len(cases) - gradable,
            "pass_rate": (passed / gradable) if gradable else None,
        },
        "cases": [
            {
                "category": c.category,
                "case": c.case,
                "status": c.status,
                "verdict": c.verdict,
                "note": c.note,
                "answer_path": str(c.answer_path) if c.answer_path else None,
                "metrics": [
                    {
                        "name": m.name,
                        "expected": m.expected,
                        "actual": m.actual,
                        "tolerance": m.tolerance,
                        "tolerance_type": m.tolerance_type,
                        "unit": m.unit,
                        "delta": m.delta,
                        "passed": m.passed,
                        "reason": m.reason,
                    }
                    for m in c.metrics
                ],
            }
            for c in cases
        ],
    }


def write_csv(cases: list[CaseResult], path: Path, encoding: str) -> None:
    """Accuracy-only CSV mirroring CatBench_ReAct.csv (no modeling/param columns).

    Top block: per-category pass rate + mean relative error. Then one detail
    block per category, one row per scored metric (so multi-metric cases like
    bader_charge get a row each).
    """
    by_cat: dict[str, list[CaseResult]] = {}
    for c in cases:
        by_cat.setdefault(c.category, []).append(c)

    with path.open("w", encoding=encoding, newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["任务类型", "结果准确率", "通过数", "可评分数"])
        for category in sorted(by_cat):
            group = by_cat[category]
            gradable = [c for c in group if c.verdict in {"PASS", "FAIL"}]
            passed = sum(c.passed for c in gradable)
            rate = passed / len(gradable) if gradable else None
            w.writerow([category, "" if rate is None else f"{100 * rate:.1f}%", passed, len(gradable)])
        w.writerow([])
        for category in sorted(by_cat):
            sorted_cases = sorted(by_cat[category], key=lambda x: x.case)
            correction_tags = {
                str(c.tags.get("correction_difficulty", ""))
                for c in sorted_cases
                if c.tags.get("correction_difficulty")
            }
            correction_axis = "P" if correction_tags and all(tag.startswith("P") for tag in correction_tags) else "C"
            w.writerow(
                [
                    category,
                    "指标",
                    "计算结果",
                    "标准结果",
                    "容差",
                    "T/F",
                    "M",
                    correction_axis,
                    "W",
                ]
            )
            for c in sorted_cases:
                tags = c.tags
                m_tag = tags.get("modeling_difficulty", "")
                c_tag = tags.get("correction_difficulty", "")
                w_tag = tags.get("workflow_complexity", "")
                if not c.metrics:
                    w.writerow([c.case, "", "", "", "", "ERR", m_tag, c_tag, w_tag])
                    continue
                for m in c.metrics:
                    actual = fmt_cell(m.actual)
                    expected = fmt_cell(m.expected)
                    tolc = fmt_cell(m.tolerance)
                    tol = f"±{tolc}" if tolc else ""
                    w.writerow(
                        [
                            c.case,
                            m.name,
                            actual,
                            expected,
                            tol,
                            "T" if m.passed else "F",
                            m_tag,
                            c_tag,
                            w_tag,
                        ]
                    )
            w.writerow([])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Grade CatPaw benchmark runs against answer keys.")
    parser.add_argument("--state-dir", help="directory of run_benchmarks.py state JSONs (default: auto-detect)")
    parser.add_argument(
        "--benchmark-root",
        default=str(Path(__file__).resolve().parent),
        help="benchmark root holding <category>/answer/*.json (default: this script's dir)",
    )
    parser.add_argument("--category", action="append", help="only grade this category (repeatable)")
    parser.add_argument("--json", dest="json_out", help="write machine-readable report to this path")
    parser.add_argument("--csv", dest="csv_out", help="write accuracy-only CSV (CatBench_ReAct.csv style)")
    parser.add_argument(
        "--csv-encoding",
        default="utf-8-sig",
        help="CSV encoding (default: utf-8-sig for Excel; use gbk to match CatBench_ReAct.csv)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show metric detail for passing cases too")
    return parser


def main(argv: list[str]) -> None:
    args = build_parser().parse_args(argv)
    state_dir = discover_state_dir(args.state_dir)
    benchmark_root = Path(args.benchmark_root).expanduser().resolve()
    only = set(args.category) if args.category else None

    cases: list[CaseResult] = []
    for state_path in sorted(state_dir.glob("*.json")):
        case = grade_case(state_path, benchmark_root)
        if only is None or case.category in only:
            cases.append(case)

    if not cases:
        print(f"(no benchmark state files found in {state_dir})")
        return

    print_report(cases, verbose=args.verbose)

    if args.json_out:
        out = Path(args.json_out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(to_payload(cases), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nwrote {out}")

    if args.csv_out:
        out = Path(args.csv_out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        write_csv(cases, out, args.csv_encoding)
        print(f"wrote {out}")

    if any(c.verdict != "PASS" for c in cases):
        raise RuntimeError("One or more benchmark cases did not pass")


if __name__ == "__main__":
    main(sys.argv[1:])
