from __future__ import annotations

import importlib
import os
from types import TracebackType
from typing import Any, Self


def _env_enabled() -> bool:
    value = os.getenv("COMPUTER_USE_OTEL_ENABLED")
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


class OptionalSpan:
    def __init__(
        self,
        context_manager: Any | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        self._context_manager = context_manager
        self._attributes = attributes or {}
        self._span: Any | None = None

    def __enter__(self) -> Self:
        if self._context_manager is not None:
            self._span = self._context_manager.__enter__()
            for key, value in self._attributes.items():
                self.set_attribute(key, value)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self._context_manager is not None:
            return self._context_manager.__exit__(exc_type, exc, traceback)
        return None

    def set_attribute(self, key: str, value: str | int | float | bool | None) -> None:
        if self._span is not None and value is not None:
            self._span.set_attribute(key, value)

    def record_exception(self, exc: BaseException) -> None:
        if self._span is not None:
            self._span.record_exception(exc)


class OptionalTracer:
    def __init__(self, *, enabled: bool | None = None, name: str = "modal_computer_use") -> None:
        self.enabled = _env_enabled() if enabled is None else enabled
        self.available = False
        self._tracer: Any | None = None
        if not self.enabled:
            return
        try:
            trace = importlib.import_module("opentelemetry.trace")
        except ImportError:
            return
        self._tracer = trace.get_tracer(name)
        self.available = True

    def span(
        self,
        name: str,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> OptionalSpan:
        if self._tracer is None:
            return OptionalSpan()
        manager = self._tracer.start_as_current_span(name)
        return OptionalSpan(manager, attributes)


def get_tracer(*, enabled: bool | None = None, name: str = "modal_computer_use") -> OptionalTracer:
    return OptionalTracer(enabled=enabled, name=name)
