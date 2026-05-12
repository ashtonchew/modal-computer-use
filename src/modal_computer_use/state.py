from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from ._version import __version__
from .config import ComputerConfig
from .models import SandboxRef


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def compute_config_hash(config: ComputerConfig) -> str:
    payload = config.model_dump(mode="json", exclude={"vnc_password", "request_id"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def created_at_tag(now: datetime | None = None) -> str:
    timestamp = now or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_tags(
    config: ComputerConfig,
    *,
    owner: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, str]:
    tags = {
        "computer-use": "true",
        "computer-use.version": __version__,
        "computer-use.created_at": created_at_tag(created_at),
        "computer-use.config_hash": compute_config_hash(config),
        "computer-use.window_manager": config.desktop.window_manager,
        "computer-use.artifacts_dir": config.storage.artifacts_dir,
    }
    if config.run_id:
        tags["computer-use.run_id"] = config.run_id
    if owner:
        tags["computer-use.owner"] = owner
    return tags


def sandbox_ref_from_values(**values: Any) -> SandboxRef:
    return SandboxRef.model_validate(values)
