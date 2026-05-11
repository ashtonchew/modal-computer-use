from __future__ import annotations

from modal_computer_use.client import DaemonClient


class Namespace:
    def __init__(self, client: DaemonClient) -> None:
        self._client = client
