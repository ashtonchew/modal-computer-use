from __future__ import annotations

import hashlib
import json
import uuid
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


def default_tags(config: ComputerConfig, *, owner: str | None = None) -> dict[str, str]:
    tags = {
        "computer-use": "true",
        "computer-use.version": __version__,
        "computer-use.config_hash": compute_config_hash(config),
        "computer-use.window_manager": config.desktop.window_manager,
    }
    if config.run_id:
        tags["computer-use.run_id"] = config.run_id
    if owner:
        tags["computer-use.owner"] = owner
    return tags


def sandbox_ref_from_values(**values: Any) -> SandboxRef:
    return SandboxRef.model_validate(values)
