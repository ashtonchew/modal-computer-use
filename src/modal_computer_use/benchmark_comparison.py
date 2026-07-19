"""Public facade for the branch-only provider comparison benchmark."""

from __future__ import annotations

from .benchmarks.provider_comparison.comparison import (
    run_provider_comparison,
)
from .benchmarks.provider_comparison.results import (
    record_provider_cleanup_errors as add_provider_cleanup_errors,
)
from .benchmarks.provider_comparison.results import (
    record_provider_runtime as finalize_provider_runtime,
)

__all__ = [
    "add_provider_cleanup_errors",
    "finalize_provider_runtime",
    "run_provider_comparison",
]
