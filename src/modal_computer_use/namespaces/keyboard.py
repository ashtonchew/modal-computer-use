from __future__ import annotations

from typing import Literal

from modal_computer_use.actions import normalize_key_combo
from modal_computer_use.models import ActionResult

from .base import AsyncNamespace, Namespace


class KeyboardNamespace(Namespace):
    def type(
        self,
        text: str,
        delay_ms: int = 10,
        method: Literal["auto", "keystrokes", "xdotool", "clipboard"] = "auto",
    ) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json(
                "/v1/keyboard/type",
                json={"text": text, "delay_ms": delay_ms, "method": method},
                _mutation=True,
            )
        )

    def press(
        self,
        key: str,
        modifiers: list[str] | None = None,
        duration_ms: int = 0,
    ) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json(
                "/v1/keyboard/press",
                json={"key": key, "modifiers": modifiers or [], "duration_ms": duration_ms},
                _mutation=True,
            )
        )

    def hotkey(self, *keys: str, duration_ms: int = 0) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json(
                "/v1/keyboard/hotkey",
                json={"keys": normalize_key_combo(keys), "duration_ms": duration_ms},
                _mutation=True,
            )
        )

    def hold(
        self,
        key: str,
        duration_ms: int | None = None,
    ) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json(
                "/v1/keyboard/hold",
                json={"key": key, "duration_ms": duration_ms},
                _mutation=True,
            )
        )

    def supported_keys(self) -> dict[str, str]:
        return dict(self._client.get_json("/v1/keyboard/keys"))


class AsyncKeyboardNamespace(AsyncNamespace):
    async def type(
        self,
        text: str,
        delay_ms: int = 10,
        method: Literal["auto", "keystrokes", "xdotool", "clipboard"] = "auto",
    ) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json(
                "/v1/keyboard/type",
                json={"text": text, "delay_ms": delay_ms, "method": method},
                _mutation=True,
            )
        )

    async def press(
        self,
        key: str,
        modifiers: list[str] | None = None,
        duration_ms: int = 0,
    ) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json(
                "/v1/keyboard/press",
                json={"key": key, "modifiers": modifiers or [], "duration_ms": duration_ms},
                _mutation=True,
            )
        )

    async def hotkey(self, *keys: str, duration_ms: int = 0) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json(
                "/v1/keyboard/hotkey",
                json={"keys": normalize_key_combo(keys), "duration_ms": duration_ms},
                _mutation=True,
            )
        )

    async def hold(self, key: str, duration_ms: int | None = None) -> ActionResult:
        return ActionResult.model_validate(
            await self._client.post_json(
                "/v1/keyboard/hold",
                json={"key": key, "duration_ms": duration_ms},
                _mutation=True,
            )
        )

    async def supported_keys(self) -> dict[str, str]:
        return dict(await self._client.get_json("/v1/keyboard/keys"))
