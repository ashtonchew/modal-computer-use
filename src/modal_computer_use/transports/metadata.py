from __future__ import annotations

from collections.abc import Callable, Mapping

type MetadataHeaders = Mapping[str, str] | Callable[[], Mapping[str, str]]


def resolve_metadata_headers(provider: MetadataHeaders | None) -> dict[str, str]:
    if provider is None:
        return {}
    headers = provider() if callable(provider) else provider
    return dict(headers)
