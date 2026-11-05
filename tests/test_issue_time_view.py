"""Texts and the bar shown for the time on an issue (tooltip and worklog dialog)."""

import re

from timecheat.app import time_panel_html, time_tooltip
from timecheat.model import IssueTime

H = 3600


def widths(panel: str) -> dict[str, float]:
    return {name: float(value) for name, value in re.findall(r'class="tc-bar-(\w+)" style="width:([\d.]+)%"', panel)}


def test_tooltip_with_estimate_and_pending():
    summary = IssueTime(mine=10 * H, others=4 * H, original=16 * H, remaining=2 * H).with_pending(H)
    assert time_tooltip("A-1", summary) == [
        "⏱ Your time on A-1: 11h (+1h not uploaded)",
        "📊 Everyone: 15h of 16h estimate (94%) · remaining 2h (≈ 1h after upload)",
    ]


def test_tooltip_without_estimate_and_over_estimate():
    assert time_tooltip("A-1", IssueTime(mine=5400).with_pending(0)) == [
        "⏱ Your time on A-1: 1h 30m",
        "📊 Everyone: 1h 30m · no estimate",
    ]
    over = IssueTime(mine=10 * H, others=2 * H, original=8 * H, remaining=0).with_pending(-1800)
    assert time_tooltip("A-1", over)[1] == (
        "📊 Everyone: 11h 30m of 8h estimate (144%), 3h 30m over · remaining 0m (≈ 30m after upload)")
    assert "−30m not uploaded" in time_tooltip("A-1", over)[0]


def test_panel_bar_is_scaled_to_the_estimate():
    panel = time_panel_html("A-1", IssueTime(mine=4 * H, others=2 * H, original=16 * H, remaining=10 * H)
                            .with_pending(2 * H))
    assert widths(panel) == {"mine": 25.0, "pending": 12.5, "others": 12.5}
    assert "Estimate 16h</b> · 50% used" in panel and "Remaining 10h (≈ 8h after upload)" in panel
    assert "tc-over-text" not in panel


def test_panel_marks_the_part_over_the_estimate():
    panel = time_panel_html("A-1", IssueTime(mine=6 * H, others=4 * H, original=8 * H).with_pending(0))
    assert widths(panel) == {"mine": 60.0, "pending": 0.0, "others": 40.0, "over": 20.0}
    assert '<span class="tc-over-text">2h over</span>' in panel


def test_panel_without_estimate_has_no_bar_and_escapes_the_key():
    panel = time_panel_html("<A-1>", IssueTime(mine=H).with_pending(0))
    assert "tc-bar" not in panel and "No estimate" in panel and "&lt;A-1&gt;" in panel
