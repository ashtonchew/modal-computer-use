from __future__ import annotations

import json
import math
import re
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ASSET = ROOT / "docs" / "assets" / "warm-operation-p50-2026-07-30.svg"
REPORT = ROOT / "docs" / "benchmark-results-2026-07-30-warm-paths.md"
OPTIMIZED = ROOT / "benchmark-data" / "modal-optimized-provider-2026-07-30.json"
DEFAULTS = ROOT / "benchmark-data" / "provider-compare-coordinate-command-2026-07-30.json"

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
    ("daytona", "Daytona"),
    ("e2b", "E2B"),
    ("modal-daemon", "Modal simple"),
    ("tzafon", "Tzafon"),
)


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
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


def _case_values(case: dict[str, Any]) -> tuple[float, float]:
    assert case["status"] == "ok"
    assert case["iterations"] == 30
    assert case["successful_iterations"] == 30
    samples = [float(value) for value in case["samples_ms"]]
    assert len(samples) == 30
    p50 = statistics.median(samples)
    p95 = _p95(samples)
    assert math.isclose(p50, float(case["summary_ms"]["p50"]), abs_tol=1e-9)
    assert math.isclose(p95, float(case["summary_ms"]["p95"]), abs_tol=1e-9)
    return p50, p95


def _all_values() -> dict[tuple[str, str], tuple[float, float]]:
    optimized = _load(OPTIMIZED)
    defaults = _load(DEFAULTS)
    assert set(defaults["providers"]) == {"modal-daemon", "daytona", "e2b", "tzafon"}
    values: dict[tuple[str, str], tuple[float, float]] = {}
    for case_key, _label in CASE_SPECS:
        values[("modal-optimized", case_key)] = _case_values(optimized["cases"][case_key])
        for provider_key in defaults["providers"]:
            values[(provider_key, case_key)] = _case_values(
                defaults["providers"][provider_key]["cases"][case_key]
            )
    return values


def test_readme_benchmark_figure_is_current() -> None:
    subprocess.run(
        [sys.executable, "scripts/render_readme_benchmark_figure.py", "--check"],
        cwd=ROOT,
        check=True,
    )


def test_readme_benchmark_figure_is_accessible_and_data_bound() -> None:
    namespace = "{http://www.w3.org/2000/svg}"
    root = ET.fromstring(ASSET.read_text(encoding="utf-8"))  # noqa: S314 - trusted generated file
    assert root.attrib["role"] == "img"
    assert root.attrib["aria-labelledby"] == "warm-title warm-desc"
    assert root.attrib["viewBox"] == "0 0 1152 560"

    title = root.find(f"{namespace}title")
    description = root.find(f"{namespace}desc")
    assert title is not None and title.attrib["id"] == "warm-title"
    assert description is not None and description.attrib["id"] == "warm-desc"
    assert title.text == "Warm-operation latency p50, July 30, 2026"
    assert description.text is not None
    assert "lowest p50 in all six displayed rows" in description.text
    assert "different configurations and caller topologies" in description.text

    texts = root.findall(f".//{namespace}text")
    text_values = ["".join(node.itertext()) for node in texts]
    classes = [node.attrib.get("class") for node in texts]
    assert classes.count("head") == 6
    assert classes.count("task") == 6
    assert classes.count("num-highlight") == 6
    assert classes.count("num") == 24
    assert classes.count("caption") == 1
    assert text_values[:6] == ["Task", *(label for _key, label in COLUMN_SPECS)]

    for _key, label in (*COLUMN_SPECS, *CASE_SPECS):
        assert label in text_values
    for (column_key, case_key), (p50, _p95_value) in _all_values().items():
        assert f"{p50:,.1f} ms" in text_values, (column_key, case_key)

    rendered_text = " ".join(root.itertext()).lower()
    for forbidden in ("ratio", "speedup", "faster"):
        assert re.search(rf"\b{forbidden}\b", rendered_text) is None
    assert re.search(r"\b\d+(?:\.\d+)?x\b", rendered_text) is None


def test_readme_benchmark_figure_keeps_readable_type() -> None:
    svg = ASSET.read_text(encoding="utf-8")
    assert ".head { fill: #F1F4EF; font-size: 18px;" in svg
    assert ".task { fill: #F1F4EF; font-size: 18px;" in svg
    assert ".num { fill: #E8EDE6; font-size: 19px;" in svg
    assert ".num-highlight { fill: #F1F4EF; font-size: 19px;" in svg
    assert ".caption { fill: #B9C2B8; font-size: 15px;" in svg


def test_warm_path_report_matches_samples() -> None:
    report = REPORT.read_text(encoding="utf-8")
    normalized_report = re.sub(r"\s+", " ", report)
    assert (
        "| Case | Modal optimized p50 / p95 | Daytona default p50 / p95 / ratio | "
        "E2B default p50 / p95 / ratio | Modal simple p50 / p95 / ratio | "
        "Tzafon default p50 / p95 / ratio |"
    ) in report
    values = _all_values()
    for case_key, _label in CASE_SPECS:
        modal_p50, modal_p95 = values[("modal-optimized", case_key)]
        assert f"{modal_p50:,.2f} / {modal_p95:,.2f} ms" in report
        for provider_key, _column_label in COLUMN_SPECS[1:]:
            p50, p95 = values[(provider_key, case_key)]
            ratio = p50 / modal_p50
            assert f"{p50:,.2f} / {p95:,.2f} ms / {ratio:.2f}x" in report

    assert "used synchronous `ComputerSandbox` and `DaemonClient`" in report
    assert "A separate benchmark must compare async and sync" in normalized_report
