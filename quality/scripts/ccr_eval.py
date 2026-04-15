#!/usr/bin/env python3
"""Deterministic local eval runner and fixture scaffold for CCR.

Runs repo-local regression cases for routing, consolidation, verification
preparation, and posting without requiring live providers or network access.
Can also scaffold new eval cases from a completed CCR run directory.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ccr_consolidate import build_candidates_manifest
from ccr_post_comments import apply_posting_plan, prepare_posting_manifest
from ccr_routing import RoutingInput, build_routing_plan
from ccr_runtime.common import display_path, load_json_file, read_text, utc_now, write_json, write_text
from ccr_runtime.manifest import build_manifest
from ccr_verify_prepare import load_candidates_manifest, prepare_verification_artifacts

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
_EVAL_ROOT = _REPO_ROOT / "evals" / "ccr"
_CASE_ROOTS = {
    "routing": _EVAL_ROOT / "routing_cases",
    "consolidation": _EVAL_ROOT / "consolidation_cases",
    "verification_prepare": _EVAL_ROOT / "verification_prepare_cases",
    "posting": _EVAL_ROOT / "posting_cases",
}
_SCAFFOLD_CONTRACT = "ccr.eval_scaffold.v1"
_MR_URL_RE = re.compile(r"^https?://[^/]+/(?P<project>.+)/-/merge_requests/(?P<iid>\d+)(?:[/?#].*)?$")


def _read_json(path: Path) -> Any:
    return load_json_file(path)


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _repo_display_path(path: Path) -> str:
    return display_path(path, relative_to=_REPO_ROOT)


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

    candidates, _summary = load_candidates_manifest(candidates_path)
    prepared = prepare_verification_artifacts(
        candidates,
        artifact_text=read_text(artifact_path),
        project_dir=project_dir,
        requirements_text=read_text(requirements_path),
        verify_batch_dir=tmp_dir / "verify_batches",
        output_file=tmp_dir / "verification_prepare.json",
    )
    return prepared["payload"]


def _run_posting_case(case_dir: Path, config: dict[str, Any], tmp_dir: Path) -> dict[str, Any]:
    manifest = build_manifest(tmp_dir, f"eval-{case_dir.name}")
    manifest_file = Path(manifest["manifest_file"])
    write_json(manifest_file, manifest)

    target = str(config.get("target") or "https://gitlab.com/group/project/-/merge_requests/200")
    project = str(config.get("project") or "group/project")
    mr_iid = int(config.get("mr_iid") or 200)

    write_json(
        Path(manifest["summary_file"]),
        {
            "contract_version": "ccr.run_summary.v1",
            "run_id": manifest["run_id"],
            "mode": "mr",
            "target": target,
        },
    )
    write_json(
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
    Path(manifest["diff_file"]).write_text(read_text(diff_file), encoding="utf-8")

    verified_payload = _read_json(_resolve_case_path(case_dir, str(config["verified_findings_file"])))
    write_json(Path(manifest["verified_findings_file"]), verified_payload)
    write_json(
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
        write_text(actual_file, actual_text)
        if actual_text == expected_text:
            return {
                "suite": suite,
                "case": case_dir.name,
                "status": "passed",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "case_dir": _repo_display_path(case_dir),
                "expected_file": _repo_display_path(expected_path),
                "actual_file": _repo_display_path(actual_file),
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
        write_text(diff_file, "\n".join(diff_lines) + "\n")
        return {
            "suite": suite,
            "case": case_dir.name,
            "status": "failed",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "case_dir": _repo_display_path(case_dir),
            "expected_file": _repo_display_path(expected_path),
            "actual_file": _repo_display_path(actual_file),
            "diff_file": _repo_display_path(diff_file),
            "error": "actual output did not match expected fixture",
        }
    except Exception as exc:  # noqa: BLE001
        error_file = case_output_dir / "error.txt"
        write_text(error_file, f"{type(exc).__name__}: {exc}\n")
        return {
            "suite": suite,
            "case": case_dir.name,
            "status": "failed",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "case_dir": _repo_display_path(case_dir),
            "expected_file": _repo_display_path(expected_path) if expected_path.exists() else str(expected_path),
            "actual_file": _repo_display_path(actual_file) if actual_file.exists() else None,
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
        "generated_at": utc_now(),
        "suite": suite,
        "case_filter": case_name,
        "case_count": len(results),
        "passed_count": passed_count,
        "failed_count": failed_count,
        "results_dir": _repo_display_path(output_dir),
        "cases": results,
    }
    write_text(output_dir / "summary.json", _json_dump(summary))
    return summary


def _default_output_dir() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return _EVAL_ROOT / "results" / stamp


def _scaffold_case_dir(scaffold_root: Path, suite: str, case_name: str, *, overwrite: bool) -> Path:
    case_dir = scaffold_root / f"{suite}_cases" / case_name
    if case_dir.exists():
        if not overwrite:
            raise ValueError(f"eval case already exists: {case_dir}")
        for child in sorted(case_dir.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
        for child in sorted(case_dir.rglob("*"), reverse=True):
            if child.is_dir():
                child.rmdir()
    case_dir.mkdir(parents=True, exist_ok=True)
    return case_dir


def _normalize_case_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-")
    return slug or "scaffolded-case"


def _parse_mr_target(target: str) -> tuple[str, int] | None:
    match = _MR_URL_RE.match(target.strip())
    if not match:
        return None
    return match.group("project"), int(match.group("iid"))


def _run_file(run_dir: Path, name: str) -> Path:
    return (run_dir / name).resolve()


def _load_run_context(run_dir: Path) -> dict[str, Any]:
    effective_run_dir = run_dir.expanduser().resolve()
    if not effective_run_dir.is_dir():
        raise ValueError(f"run_dir does not exist: {effective_run_dir}")

    summary = load_json_file(_run_file(effective_run_dir, "run_summary.json"), default={})
    if not isinstance(summary, dict):
        summary = {}
    manifest = load_json_file(_run_file(effective_run_dir, "run_manifest.json"), default={})
    if not isinstance(manifest, dict):
        manifest = {}

    def path_for(key: str, default_name: str) -> Path:
        raw = str(summary.get(key) or manifest.get(key) or _run_file(effective_run_dir, default_name))
        return Path(raw).expanduser().resolve()

    return {
        "run_dir": effective_run_dir,
        "summary": summary,
        "manifest": manifest,
        "route_input_file": path_for("route_input_file", "route_input.json"),
        "route_plan_file": path_for("route_plan_file", "route_plan.json"),
        "reviewers_file": path_for("reviewers_file", "reviewers.json"),
        "static_analysis_file": path_for("static_analysis_file", "static_analysis.json"),
        "diff_file": path_for("diff_file", "review_artifact.txt"),
        "requirements_file": path_for("requirements_file", "requirements.txt"),
        "verified_findings_file": path_for("verified_findings_file", "verified_findings.json"),
        "posting_approval_file": path_for("posting_approval_file", "posting_approval.json"),
        "posting_results_file": path_for("posting_results_file", "posting_results.json"),
        "posting_manifest_file": path_for("posting_manifest_file", "posting_manifest.json"),
    }


def _reviewer_results_from_run(run_context: dict[str, Any]) -> list[dict[str, Any]]:
    reviewers_payload = load_json_file(Path(run_context["reviewers_file"]), default={})
    if not isinstance(reviewers_payload, dict):
        raise ValueError(f"invalid reviewers manifest: {run_context['reviewers_file']}")
    passes = reviewers_payload.get("passes") if isinstance(reviewers_payload.get("passes"), list) else []
    replay_results: list[dict[str, Any]] = []
    for item in passes:
        if not isinstance(item, dict):
            continue
        output_file = str(item.get("output_file") or "").strip()
        if not output_file:
            raise ValueError(f"reviewer pass is missing output_file in {run_context['reviewers_file']}")
        result_payload = load_json_file(Path(output_file).expanduser().resolve(), default={})
        if not isinstance(result_payload, dict):
            raise ValueError(f"invalid reviewer result payload: {output_file}")
        replay_results.append(
            {
                "pass_name": str(item.get("pass_name") or ""),
                "persona": str(item.get("persona") or "logic"),
                "provider": str(item.get("provider") or "unknown"),
                "result": result_payload,
            }
        )
    if not replay_results:
        raise ValueError(f"no reviewer outputs found in {run_context['reviewers_file']}")
    return replay_results


def _portable_project_dir(summary: dict[str, Any]) -> Path | None:
    project_dir = str(summary.get("project_dir") or "").strip()
    if not project_dir:
        return None
    project_path = Path(project_dir).expanduser().resolve()
    try:
        project_path.relative_to(_REPO_ROOT.resolve())
    except ValueError:
        return None
    return project_path


def _rebuild_consolidation_expected(run_context: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    reviewer_results = _reviewer_results_from_run(run_context)
    route_plan = load_json_file(Path(run_context["route_plan_file"]), default={})
    static_analysis_payload = load_json_file(Path(run_context["static_analysis_file"]), default={})
    if not isinstance(route_plan, dict):
        raise ValueError(f"invalid route plan: {run_context['route_plan_file']}")
    if not isinstance(static_analysis_payload, dict):
        static_analysis_payload = {}
    candidates_manifest = build_candidates_manifest(
        reviewer_results,
        route_plan=route_plan,
        static_analysis_payload=static_analysis_payload,
    )
    return reviewer_results, route_plan, candidates_manifest


def _write_case_metadata(case_dir: Path, payload: dict[str, Any]) -> None:
    write_text(case_dir / "scaffold.json", _json_dump(payload))


def _scaffold_routing_case(run_context: dict[str, Any], case_name: str, scaffold_root: Path, *, overwrite: bool) -> dict[str, Any]:
    case_dir = _scaffold_case_dir(scaffold_root, "routing", case_name, overwrite=overwrite)
    route_input = load_json_file(Path(run_context["route_input_file"]))
    route_plan = load_json_file(Path(run_context["route_plan_file"]))
    write_text(case_dir / "route_input.json", _json_dump(route_input))
    write_text(case_dir / "expected.json", _json_dump(route_plan))
    write_text(case_dir / "case.json", _json_dump({"input_file": "route_input.json", "expected_file": "expected.json"}))
    _write_case_metadata(
        case_dir,
        {
            "source_run_dir": str(run_context["run_dir"]),
            "suite": "routing",
            "case": case_name,
            "generated_at": utc_now(),
        },
    )
    return {
        "suite": "routing",
        "case": case_name,
        "case_dir": _repo_display_path(case_dir),
        "status": "created",
    }


def _scaffold_consolidation_case(run_context: dict[str, Any], case_name: str, scaffold_root: Path, *, overwrite: bool) -> dict[str, Any]:
    case_dir = _scaffold_case_dir(scaffold_root, "consolidation", case_name, overwrite=overwrite)
    reviewer_results, route_plan, candidates_manifest = _rebuild_consolidation_expected(run_context)
    static_analysis_payload = load_json_file(Path(run_context["static_analysis_file"]), default={})
    write_text(case_dir / "reviewer_results.json", _json_dump(reviewer_results))
    write_text(case_dir / "route_plan.json", _json_dump(route_plan))
    write_text(case_dir / "static_analysis.json", _json_dump(static_analysis_payload))
    write_text(case_dir / "expected.json", _json_dump(candidates_manifest))
    write_text(
        case_dir / "case.json",
        _json_dump(
            {
                "reviewer_results_file": "reviewer_results.json",
                "route_plan_file": "route_plan.json",
                "static_analysis_file": "static_analysis.json",
                "expected_file": "expected.json",
            }
        ),
    )
    _write_case_metadata(
        case_dir,
        {
            "source_run_dir": str(run_context["run_dir"]),
            "suite": "consolidation",
            "case": case_name,
            "generated_at": utc_now(),
        },
    )
    return {
        "suite": "consolidation",
        "case": case_name,
        "case_dir": _repo_display_path(case_dir),
        "status": "created",
    }


def _scaffold_verification_prepare_case(run_context: dict[str, Any], case_name: str, scaffold_root: Path, *, overwrite: bool) -> dict[str, Any]:
    case_dir = _scaffold_case_dir(scaffold_root, "verification_prepare", case_name, overwrite=overwrite)
    _reviewer_results, _route_plan, candidates_manifest = _rebuild_consolidation_expected(run_context)
    write_text(case_dir / "candidates.json", _json_dump(candidates_manifest))
    write_text(case_dir / "artifact.txt", read_text(Path(run_context["diff_file"])))
    requirements_source = Path(run_context["requirements_file"])
    requirements_text = read_text(requirements_source) if requirements_source.is_file() else ""
    write_text(case_dir / "requirements.txt", requirements_text)

    summary = run_context.get("summary") if isinstance(run_context.get("summary"), dict) else {}
    project_dir = _portable_project_dir(summary)
    case_config: dict[str, Any] = {
        "candidates_file": "candidates.json",
        "artifact_file": "artifact.txt",
        "requirements_file": "requirements.txt",
        "expected_file": "expected.json",
    }
    notes: list[str] = []
    if project_dir is not None:
        case_config["project_dir"] = _repo_display_path(project_dir)
    else:
        notes.append("project_dir omitted so the scaffold remains portable across machines")

    candidates, _summary = load_candidates_manifest(case_dir / "candidates.json")
    with tempfile.TemporaryDirectory() as tmp:
        prepared = prepare_verification_artifacts(
            candidates,
            artifact_text=read_text(case_dir / "artifact.txt"),
            project_dir=project_dir,
            requirements_text=requirements_text,
            verify_batch_dir=Path(tmp) / "verify_batches",
            output_file=Path(tmp) / "verification_prepare.json",
        )
    expected_payload = _normalize_verification_prepare(prepared["payload"])
    write_text(case_dir / "expected.json", _json_dump(expected_payload))
    write_text(case_dir / "case.json", _json_dump(case_config))
    _write_case_metadata(
        case_dir,
        {
            "source_run_dir": str(run_context["run_dir"]),
            "suite": "verification_prepare",
            "case": case_name,
            "generated_at": utc_now(),
            "notes": notes,
        },
    )
    return {
        "suite": "verification_prepare",
        "case": case_name,
        "case_dir": _repo_display_path(case_dir),
        "status": "created",
        "notes": notes,
    }


def _scaffold_posting_case(run_context: dict[str, Any], case_name: str, scaffold_root: Path, *, overwrite: bool) -> dict[str, Any] | None:
    summary = run_context.get("summary") if isinstance(run_context.get("summary"), dict) else {}
    if str(summary.get("mode") or "") != "mr":
        return None
    target = str(summary.get("target") or "").strip()
    parsed = _parse_mr_target(target)
    if parsed is None:
        return None
    project, mr_iid = parsed

    case_dir = _scaffold_case_dir(scaffold_root, "posting", case_name, overwrite=overwrite)
    write_text(case_dir / "diff.txt", read_text(Path(run_context["diff_file"])))
    verified_payload = load_json_file(Path(run_context["verified_findings_file"]), default={})
    if not isinstance(verified_payload, dict):
        raise ValueError(f"invalid verified findings file: {run_context['verified_findings_file']}")
    write_text(case_dir / "verified_findings.json", _json_dump(verified_payload))

    approval_payload = load_json_file(Path(run_context["posting_approval_file"]), default={})
    approved_numbers: list[int] = []
    approved_all = False
    if isinstance(approval_payload, dict) and approval_payload:
        approved_numbers = [int(value) for value in (approval_payload.get("approved_finding_numbers") or []) if str(value).strip()]
        approved_all = bool(approval_payload.get("approved_all", False))
    if not approved_numbers and not approved_all:
        findings = verified_payload.get("verified_findings") if isinstance(verified_payload.get("verified_findings"), list) else []
        approved_numbers = [int(item.get("finding_number") or 0) for item in findings if isinstance(item, dict) and int(item.get("finding_number") or 0) > 0]
        approved_all = bool(findings)

    case_config = {
        "mode": "prepare",
        "target": target,
        "project": project,
        "mr_iid": mr_iid,
        "diff_file": "diff.txt",
        "verified_findings_file": "verified_findings.json",
        "approved_finding_numbers": approved_numbers,
        "approved_all": approved_all,
        "expected_file": "expected.json",
    }

    with tempfile.TemporaryDirectory() as tmp:
        manifest = build_manifest(Path(tmp), f"scaffold-{case_name}")
        manifest_file = Path(manifest["manifest_file"])
        write_json(manifest_file, manifest)
        write_json(
            Path(manifest["summary_file"]),
            {
                "contract_version": "ccr.run_summary.v1",
                "run_id": manifest["run_id"],
                "mode": "mr",
                "target": target,
            },
        )
        write_json(
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
        write_json(Path(manifest["verified_findings_file"]), verified_payload)
        write_json(
            Path(manifest["posting_approval_file"]),
            {
                "contract_version": "ccr.posting_approval.v1",
                "run_id": manifest["run_id"],
                "project": project,
                "mr_iid": mr_iid,
                "approved_finding_numbers": approved_numbers,
                "approved_all": approved_all,
                "approved_at": "2026-04-15T00:00:00Z",
                "source": "eval_scaffold",
            },
        )
        write_text(Path(manifest["diff_file"]), read_text(case_dir / "diff.txt"))
        expected_payload = _normalize_posting_payload(prepare_posting_manifest(manifest_file))

    write_text(case_dir / "expected.json", _json_dump(expected_payload))
    write_text(case_dir / "case.json", _json_dump(case_config))
    _write_case_metadata(
        case_dir,
        {
            "source_run_dir": str(run_context["run_dir"]),
            "suite": "posting",
            "case": case_name,
            "generated_at": utc_now(),
            "notes": ["prepare-mode posting fixture scaffolded from run artifacts"],
        },
    )
    return {
        "suite": "posting",
        "case": case_name,
        "case_dir": _repo_display_path(case_dir),
        "status": "created",
        "notes": ["prepare-mode posting fixture scaffolded from run artifacts"],
    }


def scaffold_from_run(
    run_dir: Path,
    *,
    suite: str,
    case_name: str,
    scaffold_root: Path,
    overwrite: bool,
) -> dict[str, Any]:
    run_context = _load_run_context(run_dir)
    selected_suites = list(_CASE_ROOTS.keys()) if suite == "all" else [suite]
    results: list[dict[str, Any]] = []
    for suite_name in selected_suites:
        if suite_name == "routing":
            results.append(_scaffold_routing_case(run_context, case_name, scaffold_root, overwrite=overwrite))
        elif suite_name == "consolidation":
            results.append(_scaffold_consolidation_case(run_context, case_name, scaffold_root, overwrite=overwrite))
        elif suite_name == "verification_prepare":
            results.append(_scaffold_verification_prepare_case(run_context, case_name, scaffold_root, overwrite=overwrite))
        elif suite_name == "posting":
            posting_result = _scaffold_posting_case(run_context, case_name, scaffold_root, overwrite=overwrite)
            if posting_result is not None:
                results.append(posting_result)
        else:
            raise ValueError(f"unsupported scaffold suite: {suite_name}")

    if not results:
        raise ValueError("no eval fixtures were scaffolded from this run")

    summary = {
        "contract_version": _SCAFFOLD_CONTRACT,
        "generated_at": utc_now(),
        "source_run_dir": _repo_display_path(run_context["run_dir"]),
        "suite": suite,
        "case_name": case_name,
        "scaffold_root": _repo_display_path(scaffold_root),
        "case_count": len(results),
        "cases": results,
    }
    return summary


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccr-eval",
        description="Run deterministic local CCR eval suites or scaffold cases from a run directory.",
    )
    parser.add_argument("--suite", choices=["routing", "consolidation", "verification_prepare", "posting", "all"], default="all")
    parser.add_argument("--case", default=None, help="Optional single case name to run.")
    parser.add_argument("--output-dir", default=None, help="Optional directory where eval results should be written.")
    parser.add_argument("--from-run", default=None, help="Optional CCR run directory to scaffold into eval fixtures instead of executing suites.")
    parser.add_argument("--case-name", default=None, help="Case name to use when scaffolding from a run.")
    parser.add_argument("--scaffold-dir", default=None, help="Optional eval root where scaffolded cases should be written (defaults to evals/ccr).")
    parser.add_argument("--overwrite", action="store_true", help="Allow overwriting an existing scaffolded case directory.")
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    try:
        if args.from_run:
            case_name = _normalize_case_name(args.case_name or Path(args.from_run).expanduser().resolve().name)
            scaffold_root = Path(args.scaffold_dir).expanduser().resolve() if args.scaffold_dir else _EVAL_ROOT
            summary = scaffold_from_run(
                Path(args.from_run).expanduser().resolve(),
                suite=args.suite,
                case_name=case_name,
                scaffold_root=scaffold_root,
                overwrite=args.overwrite,
            )
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            sys.exit(0)

        output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else _default_output_dir()
        summary = run_eval(args.suite, case_name=args.case, output_dir=output_dir)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    sys.exit(0 if summary.get("failed_count") == 0 else 1)


if __name__ == "__main__":
    main()
