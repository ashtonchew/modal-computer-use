"""Compatibility entry point for the application-owned Modal run gateway.

Behavior lives in :mod:`examples.run_gateway`; this module preserves the
original example's imports and Modal executable surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

_examples_dir = str(Path(__file__).resolve().parent)
if _examples_dir not in sys.path:
    sys.path.insert(0, _examples_dir)

from run_gateway import *  # noqa: E402,F403
from run_gateway import __all__ as _package_exports  # noqa: E402
from run_gateway.modal_adapter import (  # noqa: E402
    ModalTrajectoryDispatcher as _ModalTrajectoryDispatcher,
)
from run_gateway.modal_adapter import app, build_default_service, modal  # noqa: E402
from run_gateway.service import admission_fingerprint as _admission_fingerprint  # noqa: E402

if modal is not None:
    from run_gateway.modal_adapter import RunGateway as _RunGateway

    RunGateway = _RunGateway


class ModalTrajectoryDispatcher(_ModalTrajectoryDispatcher):
    """Compatibility shim that keeps this module's injectable Modal runtime."""

    def __init__(self, function: object) -> None:
        super().__init__(function, modal_runtime=modal)

    @classmethod
    def from_name(cls, app_name: str, function_name: str):
        if modal is None:
            raise ImportError("Modal is required for hosted trajectory dispatch")
        return cls(modal.Function.from_name(app_name, function_name))

__all__ = [
    *_package_exports,
    "_admission_fingerprint",
    "app",
    "build_default_service",
]
if modal is not None:
    __all__.append("RunGateway")


if __name__ == "__main__":
    raise SystemExit(
        "Adapt build_default_service() with application-owned durable dependencies, "
        "then deploy with Modal."
    )
