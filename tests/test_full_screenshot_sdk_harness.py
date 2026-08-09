from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from modal_computer_use.benchmarks.full_screenshot_sdk_harness import (
    _EXPECTED_PAYLOAD,
    _validate_sample,
    build_paired_random_schedule,
    measure_full_screenshot_arms,
)
from modal_computer_use.errors import DaemonHTTPError

ROOT = Path(__file__).resolve().parents[1]
DATA = b"png-body-for-contract-test"
SHA = hashlib.sha256(DATA).hexdigest()


def test_promotion_runner_uses_the_mounted_chromium_fixture_path() -> None:
    runner = (ROOT / "scripts" / "benchmarks" / "x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "/opt/mcu-scripts/benchmarks/fixtures/x11_shm_chromium_fixture.html" in runner
    assert "/opt/modal-computer-use/native/x11_shm" in runner
    assert 'tags={"benchmark_run": BENCHMARK_RUN_TAG}' in runner
    assert "cpu=(CPU, CPU)" in runner
    assert "memory=(MEMORY_MIB, MEMORY_MIB)" in runner


def test_promotion_runner_exposes_the_bounded_x_server_probe() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "def run_bounded_x_server_probe(" in runner
    assert '_ArmContext("auto")' in runner
    assert 'print(json.dumps(result, sort_keys=True))' in runner
    assert '"failure_phase": failure_phase' in runner
    assert '"failure_code": failure_code' in runner
    assert "computer.screenshots.full(), timeout=10.0" in runner
    assert '"python", "-c", constructor_probe, timeout=10' in runner
    assert "and elapsed_ms < 2_500.0" in runner


def test_promotion_runner_exposes_the_x_server_restart_probe() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "def run_x_server_restart_probe(" in runner
    assert 'return await _run_x_server_restart_probe(lambda: _ArmContext("auto"))' in runner
    assert 'raise RuntimeError("X server restart probe cleanup found live Sandboxes")' in runner
    assert 'print(json.dumps(result, sort_keys=True))' in runner


def test_promotion_runner_exposes_a_retained_100_pair_readiness_replication() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "def run_readiness_replication(" in runner
    assert 'if samples != 100:' in runner
    assert (
        'raise ValueError("readiness replication requires exactly 100 samples per arm")'
        in runner
    )
    assert '"sample_count_per_arm": samples' in runner
    assert 'observation["startup_total_ms"] = round(elapsed_ms, 4)' in runner
    assert '"position": position' in runner
    assert '"terminal_cleanup": cleanup' in runner
    assert "def readiness_main(" in runner


def test_promotion_readiness_retains_sdk_startup_stages() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "SessionStartupTiming" in runner
    assert "timing=self.startup_timing" in runner
    assert 'observation["startup_timing"] = timing.as_dict()' in runner
    assert '"startup_timings": startup_timings[arm]' in runner
    assert 'observation["failure_phase"] = (' in runner
    assert "_startup_failure_phase(context.startup_timing)" in runner
    assert 'if context.enter_phase == "create_sandbox"' in runner
    assert "else context.enter_phase" in runner
    assert '"connection_parameters_ready": "daemon_readiness"' in runner
    assert '"attestation_ready": "attested_tunnel_readiness"' in runner
    assert 'observation.update(_safe_daemon_failure(exc))' in runner
    assert 'observation["status"] = "failed"' in runner
    assert 'observation["failure_phase"] = "cleanup"' in runner


def test_promotion_restart_retains_safe_failure_attribution() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert 'phase = "context_enter"' in runner
    assert 'phase = "capture_before_restart"' in runner
    assert 'phase = "lifecycle_restart"' in runner
    assert 'phase = "capture_after_restart"' in runner
    assert '"failure_phase": phase' in runner
    assert '{"failure_phase": "cleanup"}' in runner
    assert "**_safe_daemon_failure(exc)" in runner


def test_promotion_failure_attribution_never_retains_daemon_error_text() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    helper = runner.split("def _safe_daemon_failure", maxsplit=1)[1].split(
        "\n\nasync def _completed_process_stdout_text",
        maxsplit=1,
    )[0]
    assert 'details.get("type")' in helper
    assert 'details.get("errors")' in helper
    assert 'result["failure_readiness_categories"]' in helper
    assert 'details.get("error")' not in helper


class FakeScreenshot:
    format = "png"
    width = 1024
    height = 768
    size_bytes = len(DATA)
    sha256 = SHA
    cursor_visible = False
    cursor_position = SimpleNamespace(x=17, y=23)
    coordinate_space = SimpleNamespace(model_dump=lambda mode=None: {})

    def as_bytes(self) -> bytes:
        return DATA


def _trace(*, backend: str = "mss") -> dict[str, object]:
    return {
        "path": "/v1/screenshots/full/raw",
        "request_json": dict(_EXPECTED_PAYLOAD),
        "response_headers": {
            "content-type": "image/png",
            "x-computer-use-width": "1024",
            "x-computer-use-height": "768",
            "x-computer-use-size-bytes": str(len(DATA)),
            "x-computer-use-sha256": SHA,
            "x-computer-use-capture-backend": backend,
            "x-computer-use-cursor-position": '{"x":17,"y":23}',
            "x-computer-use-timing-ms": json.dumps(
                {
                    "capture_ms": 1.0,
                    "encode_ms": 0.2,
                    "hash_ms": 0.25,
                    "total_ms": 1.5,
                }
            ),
        },
    }


def test_schedule_is_reproducible_paired_and_randomized() -> None:
    first = build_paired_random_schedule(
        ("mss", "x11-shm"), sample_count=100, seed=20260808
    )
    second = build_paired_random_schedule(
        ("mss", "x11-shm"), sample_count=100, seed=20260808
    )

    assert first == second
    assert len(first) == 200
    assert all(
        {entry["arm"] for entry in first[index : index + 2]} == {"mss", "x11-shm"}
        for index in range(0, len(first), 2)
    )
    assert {entry["position"] for entry in first} == {0, 1}
    assert any(
        first[index]["arm"] != first[index + 2]["arm"]
        for index in range(0, len(first) - 2, 2)
    )


def test_validate_sample_accepts_canonical_contract() -> None:
    _validate_sample(FakeScreenshot(), _trace())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "/v1/screenshots/full"),
        ("request_json", {"format": "jpeg"}),
    ],
)
def test_validate_sample_rejects_route_or_payload(field: str, value: object) -> None:
    trace = _trace()
    trace[field] = value
    with pytest.raises(AssertionError):
        _validate_sample(FakeScreenshot(), trace)


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("content-type", "image/jpeg"),
        ("x-computer-use-width", "800"),
        ("x-computer-use-sha256", "0" * 64),
        ("x-computer-use-cursor-position", '{"x":1024,"y":0}'),
        ("x-computer-use-timing-ms", '{"total_ms":-1}'),
        ("x-computer-use-timing-ms", '{"total_ms":1}'),
    ],
)
def test_validate_sample_rejects_response_contract(header: str, value: str) -> None:
    trace = _trace()
    headers = trace["response_headers"]
    assert isinstance(headers, dict)
    headers[header] = value
    with pytest.raises(AssertionError):
        _validate_sample(FakeScreenshot(), trace)


class FakeClient:
    def __init__(self, backend: str, *, fail_on_call: int | None = None) -> None:
        self.backend = backend
        self.fail_on_call = fail_on_call
        self.calls = 0

    async def post_bytes_with_headers(self, path: str, *, json: dict[str, object]):
        assert path == "/v1/screenshots/full/raw"
        assert json == _EXPECTED_PAYLOAD
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise DaemonHTTPError(
                "internal server error",
                status_code=500,
                code="internal_error",
                details={
                    "error_type": "ScreenshotCaptureTimedOut",
                    "token": "must-not-appear",
                    "errors": [
                        "screenshot capture failed",
                        "https://private.invalid/must-not-appear",
                    ],
                },
            )
        return DATA, _trace(backend=self.backend)["response_headers"]


class FakeScreenshots:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    async def full(self) -> FakeScreenshot:
        await self._client.post_bytes_with_headers(
            "/v1/screenshots/full/raw", json=dict(_EXPECTED_PAYLOAD)
        )
        return FakeScreenshot()


class FakeComputer:
    def __init__(self, backend: str, *, fail_on_call: int | None = None) -> None:
        self.client = FakeClient(backend, fail_on_call=fail_on_call)
        self.screenshots = FakeScreenshots(self.client)


class FakeContext:
    def __init__(
        self,
        backend: str,
        *,
        fail_cleanup: bool = False,
        fail_on_call: int | None = None,
    ) -> None:
        self.computer = FakeComputer(backend, fail_on_call=fail_on_call)
        self.fail_cleanup = fail_cleanup

    async def __aenter__(self) -> FakeComputer:
        return self.computer

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.fail_cleanup:
            raise RuntimeError("cleanup failed")


@pytest.mark.asyncio
async def test_measure_full_screenshot_records_public_and_daemon_boundaries() -> None:
    result = await measure_full_screenshot_arms(
        {"mss": lambda: FakeContext("mss"), "x11-shm": lambda: FakeContext("x11-shm")},
        sample_count=100,
        warmup_iterations=10,
        decode_parity=lambda _data: True,
        expected_capture_backends={"mss": "mss", "x11-shm": "x11-shm"},
        schedule_seed=20260808,
    )

    assert result["public_call"] == "await computer.screenshots.full()"
    assert len(result["schedule"]) == 200
    for arm in ("mss", "x11-shm"):
        observations = result["arms"][arm]["observations"]
        assert len(observations) == 100
        assert observations[0]["daemon_total_ms"] == 1.5
        assert observations[0]["hash_ms"] == 0.25
        assert observations[0]["payload_bytes"] == len(DATA)
        assert observations[0]["metadata_parity"] is True


@pytest.mark.asyncio
async def test_measure_full_screenshot_requires_promotion_sample_and_warmup_counts() -> None:
    with pytest.raises(ValueError, match="100"):
        await measure_full_screenshot_arms(
            {"mss": lambda: FakeContext("mss"), "x11-shm": lambda: FakeContext("x11-shm")},
            sample_count=30,
            warmup_iterations=10,
            decode_parity=lambda _data: True,
        )
    with pytest.raises(ValueError, match="10 warmup"):
        await measure_full_screenshot_arms(
            {"mss": lambda: FakeContext("mss"), "x11-shm": lambda: FakeContext("x11-shm")},
            sample_count=100,
            warmup_iterations=3,
            decode_parity=lambda _data: True,
        )


@pytest.mark.asyncio
async def test_measure_full_screenshot_requires_pixel_parity_callback() -> None:
    with pytest.raises(ValueError, match="pixel parity"):
        await measure_full_screenshot_arms(
            {"mss": lambda: FakeContext("mss"), "x11-shm": lambda: FakeContext("x11-shm")},
            sample_count=100,
            warmup_iterations=10,
        )


@pytest.mark.asyncio
async def test_measure_full_screenshot_fails_cleanup() -> None:
    with pytest.raises(RuntimeError, match="cleanup"):
        await measure_full_screenshot_arms(
            {
                "mss": lambda: FakeContext("mss", fail_cleanup=True),
                "x11-shm": lambda: FakeContext("x11-shm"),
            },
            sample_count=100,
            warmup_iterations=10,
            decode_parity=lambda _data: True,
        )


@pytest.mark.asyncio
async def test_measure_full_screenshot_annotates_daemon_failure_without_secrets() -> None:
    with pytest.raises(DaemonHTTPError) as caught:
        await measure_full_screenshot_arms(
            {
                "mss": lambda: FakeContext("mss"),
                "x11-shm": lambda: FakeContext("x11-shm", fail_on_call=11),
            },
            sample_count=100,
            warmup_iterations=10,
            decode_parity=lambda _data: True,
            schedule_seed=20260808,
        )

    notes = "\n".join(caught.value.__notes__)
    assert "arm=x11-shm phase=sample sample_index=0" in notes
    assert "status_code=500 code=internal_error" in notes
    assert "error_type=ScreenshotCaptureTimedOut" in notes
    assert "readiness_errors=screenshot,unknown" in notes
    assert "must-not-appear" not in notes
