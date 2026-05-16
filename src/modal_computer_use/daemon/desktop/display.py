from __future__ import annotations

from modal_computer_use.models import DisplayGeometry, DisplayInfo


class StaticDisplayController:
    def __init__(self, *, width: int, height: int, display: str = ":99") -> None:
        self.width = width
        self.height = height
        self.display = display

    async def info(self) -> DisplayInfo:
        display = DisplayGeometry(
            id=f"{self.display}.0",
            x=0,
            y=0,
            width=self.width,
            height=self.height,
        )
        return DisplayInfo(primary_display=display, total_displays=1, displays=[display])
