from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from ..errors import ModalNotInstalledError

MODAL_BILLING_SOURCE = "modal.billing.workspace_billing_report"
MODAL_BILLING_DOC_URL = "https://modal.com/docs/reference/modal.billing"
MODAL_BILLING_DEFAULT_RESOLUTION = "h"
MODAL_BILLING_TAG_NAMES = ("benchmark", "benchmark_run_id", "surface")

BillingReportLoader = Callable[
    [datetime, datetime, str, list[str] | None],
    Iterable[Any],
]


def new_benchmark_run_id() -> str:
    return f"sdk_surface_{uuid4().hex[:16]}"


def modal_surface_benchmark_tags(benchmark_run_id: str) -> dict[str, str]:
    return {
        "benchmark": "sdk-surfaces",
        "benchmark_run_id": benchmark_run_id,
        "surface": "daemon-http",
    }


def modal_billing_reconciliation_request(
    *,
    start: datetime,
    end: datetime | None,
    required_tags: dict[str, str],
    tag_names: list[str] | None = None,
    resolution: str = MODAL_BILLING_DEFAULT_RESOLUTION,
    buffer_seconds: int = 0,
) -> dict[str, Any]:
    safe_required_tags = _safe_tags(required_tags)
    safe_tag_names = _safe_tag_names(tag_names or list(MODAL_BILLING_TAG_NAMES))
    return {
        "surface": "daemon-http",
        "start": _utc_iso(start),
        "end": _utc_iso(end) if end is not None else None,
        "resolution": resolution,
        "tag_names": safe_tag_names,
        "required_tags": safe_required_tags,
        "buffer_seconds": max(0, int(buffer_seconds)),
    }


def reconcile_modal_billing_from_metadata(
    metadata: dict[str, Any] | None,
    *,
    report_loader: BillingReportLoader | None = None,
) -> dict[str, Any] | None:
    if not metadata:
        return None
    request = metadata.get("modal_billing_reconciliation")
    if not isinstance(request, dict):
        return None
    return reconcile_modal_billing(request, report_loader=report_loader)


def reconcile_modal_billing(
    request: dict[str, Any],
    *,
    report_loader: BillingReportLoader | None = None,
) -> dict[str, Any]:
    start = _parse_datetime(request.get("start"))
    end = _parse_datetime(request.get("end")) or datetime.now(UTC)
    buffer_seconds = _int_or_default(request.get("buffer_seconds"), 0)
    if buffer_seconds > 0:
        end = end - timedelta(seconds=buffer_seconds)
    resolution = _safe_resolution(request.get("resolution"))
    tag_names = _safe_tag_names(request.get("tag_names"))
    required_tags = _safe_tags(request.get("required_tags"))

    base = {
        "source": MODAL_BILLING_SOURCE,
        "source_url": MODAL_BILLING_DOC_URL,
        "currency": "USD",
        "start": _utc_iso(start) if start is not None else None,
        "end": _utc_iso(end),
        "resolution": resolution,
        "tag_names": tag_names,
        "required_tags": required_tags,
        "matched_row_count": 0,
        "total": None,
        "notes": [
            "Modal billing reports are delayed and bucketed by full intervals; "
            "short benchmark runs may not be available immediately."
        ],
    }
    if start is None or not required_tags:
        return {
            **base,
            "status": "not_measured",
            "reason": "billing reconciliation requires start time and required tags",
        }
    if end <= start:
        return {
            **base,
            "status": "not_measured",
            "reason": "billing reconciliation end must be after start",
        }

    loader = report_loader or _load_modal_billing_report
    try:
        rows = list(loader(start, end, resolution, tag_names or None))
    except (ImportError, ModalNotInstalledError):
        return {
            **base,
            "status": "unavailable",
            "reason": "modal billing API is unavailable; install the modal extra",
        }
    except Exception as exc:
        return {
            **base,
            "status": "failed",
            "reason": _safe_error_message(exc),
        }

    matched_rows = [_row_summary(row) for row in rows if _row_matches(row, required_tags)]
    if not matched_rows:
        if rows:
            return {
                **base,
                "status": "no_matching_tags",
                "reason": "billing rows were available but none matched the required tags",
                "row_count": len(rows),
            }
        return {
            **base,
            "status": "not_available_yet",
            "reason": "no billing rows were available for the requested window",
            "row_count": len(rows),
        }

    total = sum((row["cost"] for row in matched_rows), Decimal("0"))
    intervals = sorted({row["interval_start"] for row in matched_rows if row["interval_start"]})
    return {
        **base,
        "status": "matched",
        "matched_row_count": len(matched_rows),
        "row_count": len(rows),
        "total": {"amount": float(total), "unit": "report_window"},
        "interval_starts": intervals,
    }


def _load_modal_billing_report(
    start: datetime,
    end: datetime,
    resolution: str,
    tag_names: list[str] | None,
) -> Iterable[Any]:
    from ..sandbox import modal_workspace_billing_report

    return modal_workspace_billing_report(
        start=start,
        end=end,
        resolution=resolution,
        tag_names=tag_names,
    )


def _row_matches(row: Any, required_tags: dict[str, str]) -> bool:
    tags = _row_tags(row)
    return all(tags.get(key) == value for key, value in required_tags.items())


def _row_summary(row: Any) -> dict[str, Any]:
    return {
        "cost": _decimal_or_zero(_row_value(row, "cost")),
        "interval_start": _utc_iso(_row_value(row, "interval_start")),
    }


def _row_tags(row: Any) -> dict[str, str]:
    return _safe_tags(_row_value(row, "tags"))


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _decimal_or_zero(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float | str):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return Decimal("0")
    return Decimal("0")


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _utc_iso(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_tags(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, str] = {}
    for key, raw_value in value.items():
        safe_key = str(key).strip()
        if not safe_key:
            continue
        safe_value = str(raw_value).strip()
        if safe_value:
            safe[safe_key] = safe_value
    return safe


def _safe_tag_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list | tuple):
        values = list(value)
    else:
        return []
    names: list[str] = []
    for item in values:
        name = str(item).strip()
        if name and name not in names:
            names.append(name)
    return names


def _safe_resolution(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return MODAL_BILLING_DEFAULT_RESOLUTION


def _int_or_default(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _safe_error_message(exc: Exception) -> str:
    message = str(exc)
    for marker in ("Bearer ", "token=", "password=", "secret="):
        if marker in message:
            return "modal billing reconciliation failed"
    return message[:240]
