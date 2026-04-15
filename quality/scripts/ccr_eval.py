#!/usr/bin/env python3
"""Deterministic local eval runner for CCR.

Runs repo-local regression cases for routing, consolidation, verification
preparation, and posting without requiring live providers or network access.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ccr_consolidate import build_candidates_manifest
from ccr_post_comments import apply_posting_plan, prepare_posting_manifest
from ccr_routing import RoutingInput, build_routing_plan
from ccr_run_init import _build_manifest, _write_json
from ccr_verify_prepare import _load_candidates_manifest, prepare_verification_artifacts


_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
_EVAL_ROOT = _REPO_ROOT / "evals" / "ccr"
_CASE_ROOTS = {
    "routing": _EVAL_ROOT / "routing_cases",
    "consolidation": _EVAL_ROOT / "consolidation_cases",
    "verification_prepare": _EVAL_ROOT / "verification_prepare_cases",
    "posting": _EVAL_ROOT / "posting_cases",
}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(_REPO_ROOT))
    except ValueError:
        return str(path.resolve())



def _resolve_case_path(case_dir: Path, value: str) -> Path:
    raw = Path(value)
    if raw.is_absolute():
        return raw
    case_relative = (case_dir / raw).resolve()
    if case_relative.exists():
        return case_relative
    repo_relative = (_REPO_ROOT / raw).resolve()
    return repo_relative


def _load_case_config(case_dir: Path) -> dict[str, Any]:
    config = _read_json(case_dir / "case.json")
    if not isinstance(config, dict):
        raise ValueError(f"case config must be an object: {case_dir / 'case.json'}")
    return config


def discover_cases(suite: str, case_name: str | None = None) -> list[tuple[str, Path]]:
    suites = [suite] if suite != "all" else list(_CASE_ROOTS.keys())
    discovered: list[tuple[str, Path]] = []
    for suite_name in suites:
        root = _CASE_ROOTS[suite_name]
        if not root.is_dir():
            continue
        for case_dir in sorted(path for path in root.iterdir() if path.is_dir() and (path / "case.json").is_file()):
            if case_name and case_dir.name != case_name:
                continue
            discovered.append((suite_name, case_dir))
    return discovered


def _normalize_verification_prepare(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(payload)
    if "prepared_at" in normalized:
        normalized["prepared_at"] = "<ts>"
    for batch in normalized.get("batches", []) if isinstance(normalized.get("batches"), list) else []:
        if isinstance(batch, dict) and batch.get("batch_file"):
            batch["batch_file"] = Path(str(batch["batch_file"])).name
    return normalized


def _normalize_posting_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(payload)
    for key in ("prepared_at", "started_at", "finished_at"):
        if key in normalized:
            normalized[key] = "<ts>"
    if "duration_ms" in normalized:
        normalized["duration_ms"] = "<ms>"

    approved_findings = normalized.get("approved_findings")
    if isinstance(approved_findings, list):
        for item in approved_findings:
            if not isinstance(item, dict):
                continue
            if item.get("payload_file"):
                item["payload_file"] = Path(str(item["payload_file"])).name

    results = normalized.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            for key in ("payload_file", "response_file"):
                if item.get(key):
                    item[key] = Path(str(item[key])).name
    return normalized


def _normalize_actual(suite: str, payload: dict[str, Any]) -> dict[str, Any]:
    if suite == "verification_prepare":
        return _normalize_verification_prepare(payload)
    if suite == "posting":
        return _normalize_posting_payload(payload)
    return payload


def _write_fake_glab(tmp_dir: Path, *, get_payload: Any, post_payload: Any) -> Path:
    script_path = tmp_dir / "fake_glab"
    script_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        f"get_payload = {json.dumps(get_payload)!r}\n"
        f"post_payload = {json.dumps(post_payload)!r}\n"
        "is_post = '-X' in sys.argv and sys.argv[sys.argv.index('-X') + 1] == 'POST'\n"
        "if is_post:\n"
        "    sys.stdout.write(post_payload)\n"
        "else:\n"
        "    sys.stdout.write(get_payload)\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    return script_path


def _run_routing_case(case_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    input_path = _resolve_case_path(case_dir, str(config["input_file"]))
    route_input = _read_json(input_path)
    return build_routing_plan(RoutingInput.model_validate(route_input)).model_dump()


def _run_consolidation_case(case_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    reviewer_results = _read_json(_resolve_case_path(case_dir, str(config["reviewer_results_file"])))
    route_plan = _read_json(_resolve_case_path(case_dir, str(config["route_plan_file"])))
    static_analysis_payload = _read_json(_resolve_case_path(case_dir, str(config["static_analysis_file"])))
    return build_candidates_manifest(
        reviewer_results,
        route_plan=route_plan,
        static_analysis_payload=static_analysis_payload,
    )


def _run_verification_prepare_case(case_dir: Path, config: dict[str, Any], tmp_dir: Path) -> dict[str, Any]:
    candidates_path = _resolve_case_path(case_dir, str(config["candidates_file"]))
    artifact_path = _resolve_case_path(case_dir, str(config["artifact_file"]))
    requirements_path = _resolve_case_path(case_dir, str(config["requirements_file"]))
    project_dir = _resolve_case_path(case_dir, str(config["project_dir"])) if config.get("project_dir") else None

    candidates, _summary = _load_candidates_manifest(candidates_path)
    prepared = prepare_verification_artifacts(
        candidates,
        artifact_text=artifact_path.read_text(encoding="utf-8"),
        project_dir=project_dir,
        requirements_text=requirements_path.read_text(encoding="utf-8"),
        verify_batch_dir=tmp_dir / "verify_batches",
        output_file=tmp_dir / "verification_prepare.json",
    )
    return prepared["payload"]


def _run_posting_case(case_dir: Path, config: dict[str, Any], tmp_dir: Path) -> dict[str, Any]:
    manifest = _build_manifest(tmp_dir, f"eval-{case_dir.name}")
    manifest_file = Path(manifest["manifest_file"])
    _write_json(manifest_file, manifest)

    target = str(config.get("target") or "https://gitlab.com/group/project/-/merge_requests/200")
    project = str(config.get("project") or "group/project")
    mr_iid = int(config.get("mr_iid") or 200)

    _write_json(
        Path(manifest["summary_file"]),
        {
            "contract_version": "ccr.run_summary.v1",
            "run_id": manifest["run_id"],
            "mode": "mr",
            "target": target,
        },
    )
    _write_json(
        Path(manifest["mr_metadata_file"]),
        {
            "iid": mr_iid,
            "diff_refs": {
                "base_sha": "base-sha",
                "start_sha": "start-sha",
                "head_sha": "head-sha",
            },
        },
    )

    diff_file = _resolve_case_path(case_dir, str(config["diff_file"]))
    Path(manifest["diff_file"]).write_text(diff_file.read_text(encoding="utf-8"), encoding="utf-8")

    verified_payload = _read_json(_resolve_case_path(case_dir, str(config["verified_findings_file"])))
    _write_json(Path(manifest["verified_findings_file"]), verified_payload)
    _write_json(
        Path(manifest["posting_approval_file"]),
        {
            "contract_version": "ccr.posting_approval.v1",
            "run_id": manifest["run_id"],
            "project": project,
            "mr_iid": mr_iid,
            "approved_finding_numbers": list(config.get("approved_finding_numbers") or []),
            "approved_all": bool(config.get("approved_all", False)),
            "approved_at": "2026-04-15T00:00:00Z",
            "source": "eval_runner",
        },
    )

    mode = str(config.get("mode") or "apply")
    if mode == "prepare":
        return prepare_posting_manifest(manifest_file)

    get_payload = _read_json(_resolve_case_path(case_dir, str(config.get("glab_get_payload_file") or "glab_get.json")))
    post_payload = _read_json(_resolve_case_path(case_dir, str(config.get("glab_post_payload_file") or "glab_post.json")))
    fake_glab = _write_fake_glab(tmp_dir, get_payload=get_payload, post_payload=post_payload)
    return apply_posting_plan(manifest_file, glab_bin=str(fake_glab))


def _run_case_payload(suite: str, case_dir: Path, config: dict[str, Any], tmp_dir: Path) -> dict[str, Any]:
    if suite == "routing":
        return _run_routing_case(case_dir, config)
    if suite == "consolidation":
        return _run_consolidation_case(case_dir, config)
    if suite == "verification_prepare":
        return _run_verification_prepare_case(case_dir, config, tmp_dir)
    if suite == "posting":
        return _run_posting_case(case_dir, config, tmp_dir)
    raise ValueError(f"unsupported eval suite: {suite}")


def _expected_path(case_dir: Path, config: dict[str, Any]) -> Path:
    return _resolve_case_path(case_dir, str(config.get("expected_file") or "expected.json"))


def run_case(suite: str, case_dir: Path, output_root: Path) -> dict[str, Any]:
    config = _load_case_config(case_dir)
    case_output_dir = output_root / suite / case_dir.name
    tmp_dir = case_output_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    actual_file = case_output_dir / "actual.json"
    diff_file = case_output_dir / "diff.txt"

    started = time.monotonic()
    expected_path = _expected_path(case_dir, config)
    try:
        actual_payload = _run_case_payload(suite, case_dir, config, tmp_dir)
        normalized_actual = _normalize_actual(suite, actual_payload)
        expected_payload = _read_json(expected_path)
        actual_text = _json_dump(normalized_actual)
        expected_text = _json_dump(expected_payload)
        _write_text(actual_file, actual_text)
        if actual_text == expected_text:
            return {
                "suite": suite,
                "case": case_dir.name,
                "status": "passed",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "case_dir": _display_path(case_dir),
                "expected_file": _display_path(expected_path),
                "actual_file": _display_path(actual_file),
                "diff_file": None,
                "error": None,
            }
        diff_lines = difflib.unified_diff(
            expected_text.splitlines(),
            actual_text.splitlines(),
            fromfile="expected",
            tofile="actual",
            lineterm="",
        )
        _write_text(diff_file, "\n".join(diff_lines) + "\n")
        return {
            "suite": suite,
            "case": case_dir.name,
            "status": "failed",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "case_dir": _display_path(case_dir),
            "expected_file": _display_path(expected_path),
            "actual_file": _display_path(actual_file),
            "diff_file": _display_path(diff_file),
            "error": "actual output did not match expected fixture",
        }
    except Exception as exc:  # noqa: BLE001
        error_file = case_output_dir / "error.txt"
        _write_text(error_file, f"{type(exc).__name__}: {exc}\n")
        return {
            "suite": suite,
            "case": case_dir.name,
            "status": "failed",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "case_dir": _display_path(case_dir),
            "expected_file": _display_path(expected_path) if expected_path.exists() else str(expected_path),
            "actual_file": _display_path(actual_file) if actual_file.exists() else None,
            "diff_file": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_eval(suite: str, *, case_name: str | None, output_dir: Path) -> dict[str, Any]:
    cases = discover_cases(suite, case_name=case_name)
    if not cases:
        raise ValueError(f"no eval cases found for suite={suite!r} case={case_name!r}")

    output_dir.mkdir(parents=True, exist_ok=True)
    results = [run_case(case_suite, case_dir, output_dir) for case_suite, case_dir in cases]
    passed_count = sum(1 for item in results if item["status"] == "passed")
    failed_count = len(results) - passed_count
    summary = {
        "contract_version": "ccr.eval_summary.v1",
        "generated_at": _utc_now(),
        "suite": suite,
        "case_filter": case_name,
        "case_count": len(results),
        "passed_count": passed_count,
        "failed_count": failed_count,
        "results_dir": _display_path(output_dir),
        "cases": results,
    }
    _write_text(output_dir / "summary.json", _json_dump(summary))
    return summary


def _default_output_dir() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return _EVAL_ROOT / "results" / stamp


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-eval",
        description="Run deterministic local CCR eval suites.",
    )
    parser.add_argument("--suite", choices=["routing", "consolidation", "verification_prepare", "posting", "all"], default="all")
    parser.add_argument("--case", default=None, help="Optional single case name to run.")
    parser.add_argument("--output-dir", default=None, help="Optional directory where eval results should be written.")
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else _default_output_dir()
    try:
        summary = run_eval(args.suite, case_name=args.case, output_dir=output_dir)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary.get("failed_count") == 0 else 1)


if __name__ == "__main__":
    main()
