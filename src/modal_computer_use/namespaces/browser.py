from __future__ import annotations

from modal_computer_use.models import ActionResult

from .base import Namespace


class BrowserNamespace(Namespace):
    def open_url(self, url: str, wait_for_window: bool = True) -> ActionResult:
        return ActionResult.model_validate(
            self._client.post_json(
                "/v1/browser/open-url",
                json={"url": url, "wait_for_window": wait_for_window},
            )
        )

    def status(self) -> dict:
        return dict(self._client.get_json("/v1/browser/status"))
