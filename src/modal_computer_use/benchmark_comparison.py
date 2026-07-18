"""Compatibility facade for provider comparison benchmarks.

Implementation lives with the benchmark behaviors under
``benchmarks.provider_comparison``. Private aliases below exist only for the
characterization tests that predate that package split.
"""

from __future__ import annotations

from typing import Any

from .benchmarks.provider_comparison.comparison import (
    run_provider_comparison,
)
from .benchmarks.provider_comparison.live import (
    cleanup_provider_sandbox as _cleanup_provider_sandbox,  # noqa: F401
)
from .benchmarks.provider_comparison.live import run_product_provider_cases
from .benchmarks.provider_comparison.payloads import (
    describe_screenshot_payload as _provider_payload_metadata,  # noqa: F401
)
from .benchmarks.provider_comparison.results import (
    build_provider_result as _provider_result,  # noqa: F401
)
from .benchmarks.provider_comparison.results import (
    record_provider_cleanup_errors as add_provider_cleanup_errors,
)
from .benchmarks.provider_comparison.results import (
    record_provider_runtime as finalize_provider_runtime,
)
from .benchmarks.provider_comparison.sdk_support import (
    sanitize_provider_observation as _safe_provider_observation,  # noqa: F401
)


def _run_live_provider_cases(
    *,
    provider: str,
    benchmark: Any,
    cold_cases: tuple[str, ...],
    warm_cases: tuple[str, ...],
    iterations: int,
    warmup_iterations: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return run_product_provider_cases(
        provider=provider,
        driver=benchmark,
        cold_cases=cold_cases,
        warm_cases=warm_cases,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        metadata=metadata,
    )


__all__ = [
    "add_provider_cleanup_errors",
    "finalize_provider_runtime",
    "run_provider_comparison",
]
