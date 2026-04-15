from __future__ import annotations

from typing import Any

from ccr_runtime.common import ratio


def normalize_llm_invocation(
    payload: Any,
    *,
    provider: str | None,
    duration_ms: int | None = None,
    exit_code: int | None = None,
    timed_out: bool | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}

    normalized_provider = data.get("provider")
    if not isinstance(normalized_provider, str) or not normalized_provider:
        normalized_provider = provider

    thread_id = data.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        thread_id = None

    tokens = data.get("tokens")
    if not isinstance(tokens, int) or tokens < 0:
        tokens = 0

    normalized_duration_ms = data.get("duration_ms")
    if not isinstance(normalized_duration_ms, int) or normalized_duration_ms < 0:
        normalized_duration_ms = max(0, int(duration_ms or 0))

    normalized_exit_code = data.get("exit_code")
    if not isinstance(normalized_exit_code, int):
        normalized_exit_code = int(exit_code or 0)

    normalized_error = data.get("error")
    if normalized_error is not None:
        normalized_error = str(normalized_error)
    elif error:
        normalized_error = str(error)

    normalized_timed_out = data.get("timed_out")
    if not isinstance(normalized_timed_out, bool):
        normalized_timed_out = bool(timed_out)

    schema_valid = data.get("schema_valid")
    if not isinstance(schema_valid, bool):
        schema_valid = normalized_exit_code == 0 and not normalized_timed_out

    schema_retries = data.get("schema_retries")
    if not isinstance(schema_retries, int) or schema_retries < 0:
        schema_retries = 0

    schema_violations = data.get("schema_violations")
    if not isinstance(schema_violations, list):
        schema_violations = []
    else:
        schema_violations = [str(item) for item in schema_violations]

    return {
        "provider": normalized_provider,
        "thread_id": thread_id,
        "tokens": tokens,
        "duration_ms": normalized_duration_ms,
        "exit_code": normalized_exit_code,
        "error": normalized_error,
        "timed_out": normalized_timed_out,
        "schema_valid": schema_valid,
        "schema_retries": schema_retries,
        "schema_violations": schema_violations,
    }


def invocation_event_fields(invocation: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(invocation, dict):
        return {}
    payload: dict[str, Any] = {"llm_invocation": invocation}
    tokens = int(invocation.get("tokens") or 0)
    if tokens > 0:
        payload["tokens"] = tokens
    schema_retries = int(invocation.get("schema_retries") or 0)
    if schema_retries > 0:
        payload["schema_retries"] = schema_retries
    if invocation.get("schema_valid") is False:
        payload["schema_valid"] = False
    exit_code = int(invocation.get("exit_code") or 0)
    if exit_code != 0:
        payload["exit_code"] = exit_code
    if invocation.get("timed_out") is True:
        payload["timed_out"] = True
    schema_violations = invocation.get("schema_violations") if isinstance(invocation.get("schema_violations"), list) else []
    if schema_violations:
        payload["schema_violation_count"] = len(schema_violations)
        payload["schema_violations"] = schema_violations
    return payload


def collect_llm_invocations(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    invocations: list[dict[str, Any]] = []
    for item in results:
        invocation = item.get("llm_invocation")
        if isinstance(invocation, dict):
            invocations.append(invocation)
    return invocations


def empty_llm_metrics() -> dict[str, Any]:
    return {
        "call_count": 0,
        "total_tokens": 0,
        "total_duration_ms": 0,
        "schema_retry_count": 0,
        "schema_retry_rate": None,
        "schema_violation_count": 0,
        "timed_out_calls": 0,
        "failed_calls": 0,
        "provider_breakdown": {},
    }


def aggregate_llm_metrics(invocations: list[dict[str, Any]]) -> dict[str, Any]:
    if not invocations:
        return empty_llm_metrics()

    totals = empty_llm_metrics()
    provider_breakdown: dict[str, dict[str, Any]] = {}
    for invocation in invocations:
        provider = str(invocation.get("provider") or "unknown")
        tokens = max(0, int(invocation.get("tokens") or 0))
        duration_value = max(0, int(invocation.get("duration_ms") or 0))
        schema_retries = max(0, int(invocation.get("schema_retries") or 0))
        schema_violations = invocation.get("schema_violations") if isinstance(invocation.get("schema_violations"), list) else []
        schema_violation_count = len(schema_violations)
        exit_code_value = int(invocation.get("exit_code") or 0)
        timed_out_value = bool(invocation.get("timed_out", False))
        failed = exit_code_value != 0 or timed_out_value or bool(invocation.get("error"))

        bucket = provider_breakdown.setdefault(
            provider,
            {
                "call_count": 0,
                "total_tokens": 0,
                "total_duration_ms": 0,
                "schema_retry_count": 0,
                "schema_violation_count": 0,
                "timed_out_calls": 0,
                "failed_calls": 0,
            },
        )

        totals["call_count"] += 1
        totals["total_tokens"] += tokens
        totals["total_duration_ms"] += duration_value
        totals["schema_retry_count"] += schema_retries
        totals["schema_violation_count"] += schema_violation_count
        if timed_out_value:
            totals["timed_out_calls"] += 1
        if failed:
            totals["failed_calls"] += 1

        bucket["call_count"] += 1
        bucket["total_tokens"] += tokens
        bucket["total_duration_ms"] += duration_value
        bucket["schema_retry_count"] += schema_retries
        bucket["schema_violation_count"] += schema_violation_count
        if timed_out_value:
            bucket["timed_out_calls"] += 1
        if failed:
            bucket["failed_calls"] += 1

    totals["schema_retry_rate"] = ratio(totals["schema_retry_count"], totals["call_count"])
    totals["provider_breakdown"] = {key: provider_breakdown[key] for key in sorted(provider_breakdown)}
    return totals


def llm_summary_fields(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "llm_call_count": int(metrics.get("call_count") or 0),
        "total_tokens": int(metrics.get("total_tokens") or 0),
        "llm_total_duration_ms": int(metrics.get("total_duration_ms") or 0),
        "schema_retry_count": int(metrics.get("schema_retry_count") or 0),
        "schema_retry_rate": metrics.get("schema_retry_rate"),
        "schema_violation_count": int(metrics.get("schema_violation_count") or 0),
        "timed_out_calls": int(metrics.get("timed_out_calls") or 0),
        "failed_calls": int(metrics.get("failed_calls") or 0),
        "provider_breakdown": dict(metrics.get("provider_breakdown") or {}),
    }


def llm_metrics_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    provider_breakdown = summary.get("provider_breakdown") if isinstance(summary.get("provider_breakdown"), dict) else {}
    return {
        "call_count": int(summary.get("llm_call_count") or 0),
        "total_tokens": int(summary.get("total_tokens") or 0),
        "total_duration_ms": int(summary.get("llm_total_duration_ms") or 0),
        "schema_retry_count": int(summary.get("schema_retry_count") or 0),
        "schema_retry_rate": summary.get("schema_retry_rate"),
        "schema_violation_count": int(summary.get("schema_violation_count") or 0),
        "timed_out_calls": int(summary.get("timed_out_calls") or 0),
        "failed_calls": int(summary.get("failed_calls") or 0),
        "provider_breakdown": dict(provider_breakdown),
    }


def merge_llm_metrics(*metrics_items: dict[str, Any]) -> dict[str, Any]:
    combined = empty_llm_metrics()
    provider_breakdown: dict[str, dict[str, Any]] = {}
    for metrics in metrics_items:
        if not isinstance(metrics, dict):
            continue
        combined["call_count"] += int(metrics.get("call_count") or 0)
        combined["total_tokens"] += int(metrics.get("total_tokens") or 0)
        combined["total_duration_ms"] += int(metrics.get("total_duration_ms") or 0)
        combined["schema_retry_count"] += int(metrics.get("schema_retry_count") or 0)
        combined["schema_violation_count"] += int(metrics.get("schema_violation_count") or 0)
        combined["timed_out_calls"] += int(metrics.get("timed_out_calls") or 0)
        combined["failed_calls"] += int(metrics.get("failed_calls") or 0)

        nested = metrics.get("provider_breakdown") if isinstance(metrics.get("provider_breakdown"), dict) else {}
        for provider, bucket in nested.items():
            if not isinstance(bucket, dict):
                continue
            merged_bucket = provider_breakdown.setdefault(
                provider,
                {
                    "call_count": 0,
                    "total_tokens": 0,
                    "total_duration_ms": 0,
                    "schema_retry_count": 0,
                    "schema_violation_count": 0,
                    "timed_out_calls": 0,
                    "failed_calls": 0,
                },
            )
            merged_bucket["call_count"] += int(bucket.get("call_count") or 0)
            merged_bucket["total_tokens"] += int(bucket.get("total_tokens") or 0)
            merged_bucket["total_duration_ms"] += int(bucket.get("total_duration_ms") or 0)
            merged_bucket["schema_retry_count"] += int(bucket.get("schema_retry_count") or 0)
            merged_bucket["schema_violation_count"] += int(bucket.get("schema_violation_count") or 0)
            merged_bucket["timed_out_calls"] += int(bucket.get("timed_out_calls") or 0)
            merged_bucket["failed_calls"] += int(bucket.get("failed_calls") or 0)

    combined["schema_retry_rate"] = ratio(combined["schema_retry_count"], combined["call_count"])
    combined["provider_breakdown"] = {key: provider_breakdown[key] for key in sorted(provider_breakdown)}
    return combined
