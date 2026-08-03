from __future__ import annotations

import argparse
import html
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modal_computer_use.benchmarks.artifacts import validate_sanitized_provider_benchmark
from modal_computer_use.benchmarks.provider_results import (
    validate_sanitized_modal_optimized_input,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OPTIMIZED_INPUT = REPO_ROOT / "benchmark-data" / "modal-optimized-provider-2026-07-30.json"
DEFAULT_INPUT = (
    REPO_ROOT / "benchmark-data" / "provider-compare-coordinate-command-2026-07-30.json"
)
OUTPUT_PATH = REPO_ROOT / "docs" / "assets" / "warm-operation-p50-2026-07-30.svg"

MEASURED_ITERATIONS = 30
CASE_SPECS = (
    ("screenshot_full", "Full screenshot"),
    ("coordinate_click", "One click"),
    ("coordinate_click_sequence", "Four ordered clicks"),
    ("type_100_chars", "Type 100 characters"),
    ("type_1000_chars", "Type 1,000 characters"),
    ("command_nonlogin_shell_echo", "Shell command"),
)
COLUMN_SPECS = (
    ("modal-optimized", "Modal optimized"),
    ("modal-daemon", "Modal default"),
    ("daytona", "Daytona default"),
    ("e2b", "E2B default"),
    ("tzafon", "Tzafon default"),
)


@dataclass(frozen=True)
class BenchmarkCell:
    p50: float
    p95: float


@dataclass(frozen=True)
class BenchmarkRow:
    key: str
    label: str
    cells: dict[str, BenchmarkCell]


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.relative_to(REPO_ROOT)} must contain a JSON object")
    return payload


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    rank = 0.95 * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _cell(case: Any, *, context: str) -> BenchmarkCell:
    if not isinstance(case, dict):
        raise ValueError(f"{context} must be an object")
    if case.get("status") != "ok":
        raise ValueError(f"{context} must have status=ok")
    if case.get("iterations") != MEASURED_ITERATIONS:
        raise ValueError(f"{context} must have {MEASURED_ITERATIONS} iterations")
    if case.get("successful_iterations") != MEASURED_ITERATIONS:
        raise ValueError(f"{context} must have {MEASURED_ITERATIONS} successful iterations")

    raw_samples = case.get("samples_ms")
    if not isinstance(raw_samples, list) or len(raw_samples) != MEASURED_ITERATIONS:
        raise ValueError(f"{context} must contain {MEASURED_ITERATIONS} samples")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_samples):
        raise ValueError(f"{context} samples must be numeric")
    samples = [float(value) for value in raw_samples]

    summary = case.get("summary_ms")
    if not isinstance(summary, dict):
        raise ValueError(f"{context} must contain summary_ms")
    p50 = statistics.median(samples)
    p95 = _p95(samples)
    if not math.isclose(float(summary.get("p50", -1.0)), p50, abs_tol=1e-9):
        raise ValueError(f"{context} stored p50 does not match samples")
    if not math.isclose(float(summary.get("p95", -1.0)), p95, abs_tol=1e-9):
        raise ValueError(f"{context} stored p95 does not match samples")
    return BenchmarkCell(p50=p50, p95=p95)


def load_benchmark_rows() -> tuple[BenchmarkRow, ...]:
    optimized = _load_json(OPTIMIZED_INPUT)
    defaults = _load_json(DEFAULT_INPUT)
    validate_sanitized_modal_optimized_input(optimized)
    validate_sanitized_provider_benchmark(defaults)

    if defaults.get("ok") is not True:
        raise ValueError("provider-default artifact must have ok=true")
    if defaults.get("iterations") != MEASURED_ITERATIONS:
        raise ValueError("provider-default artifact must have 30 iterations")
    if defaults.get("warmup_iterations") != 1:
        raise ValueError("provider-default artifact must have one warmup iteration")

    providers = defaults.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("provider-default artifact must contain providers")
    expected_providers = {key for key, _label in COLUMN_SPECS if key != "modal-optimized"}
    if set(providers) != expected_providers:
        raise ValueError("provider-default artifact has an unexpected provider set")

    optimized_cases = optimized.get("cases")
    if not isinstance(optimized_cases, dict):
        raise ValueError("optimized artifact must contain cases")

    rows: list[BenchmarkRow] = []
    for case_key, label in CASE_SPECS:
        cells = {
            "modal-optimized": _cell(
                optimized_cases.get(case_key),
                context=f"Modal optimized {case_key}",
            )
        }
        for provider_key in expected_providers:
            provider = providers.get(provider_key)
            if not isinstance(provider, dict) or not isinstance(provider.get("cases"), dict):
                raise ValueError(f"provider {provider_key} must contain cases")
            cells[provider_key] = _cell(
                provider["cases"].get(case_key),
                context=f"{provider_key} {case_key}",
            )
        rows.append(BenchmarkRow(key=case_key, label=label, cells=cells))
    return tuple(rows)


def _format_latency(value: float) -> str:
    return f"{value:,.1f} ms"


def render_svg(rows: tuple[BenchmarkRow, ...]) -> str:
    if len(rows) != len(CASE_SPECS):
        raise ValueError("benchmark figure requires the exact six-case row set")

    width = 1152
    height = 560
    left = 32
    task_right = 260
    column_width = 172
    table_top = 32
    header_bottom = 88
    row_height = 68
    table_bottom = header_bottom + row_height * len(rows)
    column_centers = {
        key: task_right + column_width * index + column_width // 2
        for index, (key, _label) in enumerate(COLUMN_SPECS)
    }

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" '
            'aria-labelledby="warm-title warm-desc">'
        ),
        '  <title id="warm-title">Warm-operation latency p50, July 30, 2026</title>',
        (
            '  <desc id="warm-desc">Modal optimized recorded the lowest p50 in all six '
            'displayed rows. The comparison measures five public paths with different '
            'configurations and caller topologies.</desc>'
        ),
        "  <defs>",
        "    <style>",
        "      .bg { fill: #101411; }",
        "      .highlight { fill: #233528; stroke: #91B89A; stroke-width: 2; }",
        "      .rule { stroke: #4E584F; stroke-width: 1.2; }",
        (
            '      .head, .task, .num, .num-highlight, .caption { font-family: '
            '-apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; }'
        ),
        "      .head { fill: #F1F4EF; font-size: 14px; font-weight: 600; }",
        "      .task { fill: #F1F4EF; font-size: 15px; font-weight: 500; }",
        "      .num { fill: #E8EDE6; font-size: 16px; font-weight: 400; }",
        "      .num-highlight { fill: #F1F4EF; font-size: 16px; font-weight: 700; }",
        "      .caption { fill: #B9C2B8; font-size: 12px; }",
        "    </style>",
        "  </defs>",
        f'  <rect class="bg" width="{width}" height="{height}"/>',
        (
            f'  <rect class="highlight" x="{task_right}" y="{table_top}" '
            f'width="{column_width}" height="{table_bottom - table_top}" rx="8"/>'
        ),
    ]

    horizontal_positions = [
        table_top,
        header_bottom,
        *(header_bottom + row_height * index for index in range(1, len(rows) + 1)),
    ]
    for y in horizontal_positions:
        lines.append(f'  <line class="rule" x1="{left}" y1="{y}" x2="1120" y2="{y}"/>')
    vertical_positions = [left, task_right, *(task_right + column_width * i for i in range(1, 6))]
    for x in vertical_positions:
        lines.append(
            f'  <line class="rule" x1="{x}" y1="{table_top}" x2="{x}" y2="{table_bottom}"/>'
        )

    header_y = 66
    lines.append(f'  <text class="head" x="48" y="{header_y}">Task</text>')
    for key, label in COLUMN_SPECS:
        lines.append(
            f'  <text class="head" x="{column_centers[key]}" y="{header_y}" '
            f'text-anchor="middle">{html.escape(label)}</text>'
        )

    for row_index, row in enumerate(rows):
        text_y = header_bottom + row_height * row_index + 41
        lines.append(
            f'  <text class="task" x="48" y="{text_y}">{html.escape(row.label)}</text>'
        )
        for key, _label in COLUMN_SPECS:
            css_class = "num-highlight" if key == "modal-optimized" else "num"
            value = html.escape(_format_latency(row.cells[key].p50))
            lines.append(
                f'  <text class="{css_class}" x="{column_centers[key]}" y="{text_y}" '
                f'text-anchor="middle">{value}</text>'
            )

    lines.extend(
        [
            (
                '  <text class="caption" x="32" y="536">Warm-operation p50 in '
                'milliseconds. Lower is better.</text>'
            ),
            "</svg>",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the README warm-operation benchmark SVG.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the generated benchmark SVG is missing or stale",
    )
    args = parser.parse_args()

    generated = render_svg(load_benchmark_rows())
    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        if current != generated:
            print(
                "docs/assets/warm-operation-p50-2026-07-30.svg is stale; "
                "run `uv run python scripts/render_readme_benchmark_figure.py`"
            )
            return 1
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(generated, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
