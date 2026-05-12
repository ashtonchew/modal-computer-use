from __future__ import annotations

from .config import ComputerConfig
from .models import SandboxRef
from .registry import SandboxRegistry
from .sandbox import ComputerSandbox, ConfigMismatchPolicy, ReusePolicy


class ComputerSandboxManager:
    """Small orchestration facade for create/list/attach flows.

    This is intentionally thin. It does not own a provider loop or a policy engine.
    """

    def __init__(self, app_name: str = "modal-computer-use") -> None:
        self.app_name = app_name
        self.registry = SandboxRegistry(app_name=app_name)

    def create(self, *, config: ComputerConfig | None = None, **kwargs: object) -> ComputerSandbox:
        return ComputerSandbox.create(app_name=self.app_name, config=config, **kwargs)

    def attach(self, sandbox_id: str, **kwargs: object) -> ComputerSandbox:
        return ComputerSandbox.attach(sandbox_id=sandbox_id, app_name=self.app_name, **kwargs)

    def attach_or_create(
        self,
        *,
        config: ComputerConfig | None = None,
        run_id: str | None = None,
        name: str | None = None,
        reuse: bool | ReusePolicy = "by_run_id",
        on_config_mismatch: ConfigMismatchPolicy = "raise",
        **kwargs: object,
    ) -> ComputerSandbox:
        return ComputerSandbox.attach_or_create(
            app_name=self.app_name,
            config=config,
            run_id=run_id,
            name=name,
            reuse=reuse,
            on_config_mismatch=on_config_mismatch,
            **kwargs,
        )

    def list(self) -> list[SandboxRef]:
        return self.registry.list()


SandboxManager = ComputerSandboxManager
