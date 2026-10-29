"""NiceGUI element around the vendored FullCalendar standard bundle (v6)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from nicegui import ui
from nicegui.events import GenericEventArguments


class FullCalendar(ui.element, component="fullcalendar.js"):
    def __init__(
        self,
        options: dict[str, Any],
        *,
        on_select: Callable[[GenericEventArguments], Any] | None = None,
        on_date_click: Callable[[GenericEventArguments], Any] | None = None,
        on_event_click: Callable[[GenericEventArguments], Any] | None = None,
        on_event_change: Callable[[GenericEventArguments], Any] | None = None,
    ) -> None:
        super().__init__()
        self.add_resource(Path(__file__).parent / "lib")
        self._props["options"] = options
        self._props["events"] = []
        self._props["day-info"] = {}
        for name, handler in (
            ("tc_select", on_select),
            ("tc_date_click", on_date_click),
            ("tc_event_click", on_event_click),
            ("tc_event_change", on_event_change),
        ):
            if handler is not None:
                self.on(name, handler)

    def set_data(self, events: list[dict[str, Any]], day_info: dict[str, dict[str, str]] | None = None) -> None:
        self._props["events"] = events
        if day_info is not None:
            self._props["day-info"] = day_info
        self.update()

    def goto_date(self, day: str) -> None:
        self.run_method("gotoDate", day)

    def unselect(self) -> None:
        self.run_method("unselect")

    def scroll_to_time(self, time: str) -> None:
        self.run_method("scrollToTime", time)
