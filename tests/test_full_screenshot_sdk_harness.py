from __future__ import annotations

import asyncio
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
from scripts.benchmarks.x11_shm_screenshot_runner import (
    _build_repeated_bounded_x_server_diagnostic,
    _is_modal_daemon_cmdline,
    _run_repeated_bounded_x_server_diagnostic,
    _validate_bounded_x_server_sample_count,
)

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


def test_promotion_runner_allows_the_complete_fixed_campaign_to_finish() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "PROMOTION_RUN_TIMEOUT_SECONDS = 7_200" in runner
    assert "timeout=PROMOTION_RUN_TIMEOUT_SECONDS" in runner


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


def test_promotion_runner_exposes_repeated_bounded_x_server_diagnostic() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLES = 10" in runner
    assert "BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLE_COUNTS = frozenset((10, 30))" in runner
    assert "def run_repeated_bounded_x_server_probe(" in runner
    assert "sample_count: int = BOUNDED_X_SERVER_DIAGNOSTIC_SAMPLES" in runner
    assert "provenance: dict[str, str | bool] | None = None" in runner
    assert '"sample_count": sample_count' in runner
    assert '"observations": observations' in runner
    assert '"terminal_cleanup": cleanup' in runner
    assert '"retries": 0' in runner
    assert '"replacement_samples": 0' in runner


def test_repeated_bounded_x_server_diagnostic_persists_the_remote_result() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "def repeated_bounded_x_server_main(" in runner
    assert "run_repeated_bounded_x_server_probe.remote(" in runner
    assert 'f"benchmark-data/x11-shm-bounded-x-diagnostic-{sample_count}.json"' in runner
    assert "path.write_text(json.dumps(result, indent=2, sort_keys=True) + \"\\n\")" in runner
    assert "print(json.dumps(result, indent=2, sort_keys=True))" in runner
    assert "provenance=_local_provenance()" in runner


def test_repeated_bounded_x_server_diagnostic_retains_safe_iterations_and_cleanup() -> None:
    observations = [
        {
            "passed": True,
            "public_error_code": "internal_error",
            "public_error_detail_type": "ScreenshotCaptureTimedOut",
            "constructor_elapsed_ms": 2006.9,
        },
        {
            "passed": False,
            "failure_type": "RuntimeError",
            "failure_phase": "capture_after_restart",
        },
    ]
    cleanup = {"succeeded": True, "remaining_sandboxes": 0}
    provenance = {
        "source_revision": "a" * 40,
        "worktree_clean": True,
        "x11_shm_source_sha256": "b" * 64,
        "cargo_lock_sha256": "c" * 64,
        "image_identity": "inline:browser-chromium-x11-shm",
    }

    payload = _build_repeated_bounded_x_server_diagnostic(
        observations, cleanup, provenance
    )

    assert payload["sample_count"] == 2
    assert payload["failure_count"] == 1
    assert payload["passed"] is False
    assert payload["observations"] == observations
    assert payload["terminal_cleanup"] == cleanup
    assert payload["retries"] == 0
    assert payload["replacement_samples"] == 0
    assert payload["schema_version"] == "x11-shm-bounded-x-diagnostic.v1"
    assert payload["provenance"] == provenance


def test_repeated_bounded_x_server_diagnostic_requires_exactly_ten_samples() -> None:
    with pytest.raises(
        ValueError,
        match="bounded X server diagnostic requires exactly 10 or 30 samples",
    ):
        asyncio.run(_run_repeated_bounded_x_server_diagnostic(sample_count=9))


def test_repeated_bounded_x_server_diagnostic_requires_provenance() -> None:
    with pytest.raises(
        ValueError, match="clean local benchmark provenance is required"
    ):
        asyncio.run(_run_repeated_bounded_x_server_diagnostic(sample_count=10))


def test_repeated_bounded_x_server_diagnostic_allows_only_ten_or_thirty_samples() -> None:
    _validate_bounded_x_server_sample_count(10)
    _validate_bounded_x_server_sample_count(30)
    with pytest.raises(
        ValueError,
        match="bounded X server diagnostic requires exactly 10 or 30 samples",
    ):
        _validate_bounded_x_server_sample_count(20)


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
    assert 'observation["public_capture_elapsed_ms"]' in runner
    assert '"position": position' in runner
    assert "continue_on_failure=True" in runner
    assert '"failure_count": failure_count' in runner
    assert '"terminal_cleanup": cleanup' in runner
    assert "def readiness_main(" in runner


def test_promotion_runner_exposes_candidate_timeout_origin_diagnostic() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )

    assert "def run_x11_shm_timeout_origin_probe(" in runner
    assert 'if samples != 100:' in runner
    assert '"sample_count": sample_count' in runner
    assert '"timeout_origin_counts": timeout_origin_counts' in runner
    assert '"retries": 0' in runner
    assert '"terminal_cleanup": cleanup' in runner
    assert "def timeout_origin_main(" in runner


def test_promotion_soak_matches_daemon_argv_token_not_helper_text() -> None:
    daemon_argv = b"python\0-m\0modal_computer_use.daemon\0--display\0:99\0"
    helper_argv = (
        b"python\0-c\0"
        b"modal_computer_use.daemon\0"
    )

    assert _is_modal_daemon_cmdline(daemon_argv) is True
    assert _is_modal_daemon_cmdline(helper_argv) is False

    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )
    soak = runner.split("async def _run_x11_shm_soak", maxsplit=1)[1].split(
        "async def _measure", maxsplit=1
    )[0]
    assert 'argv[index : index + 2] == [b"-m", b"modal_computer_use.daemon"]' in runner
    assert "_is_modal_daemon_cmdline(command)" in soak


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
    assert 'details.get("timeout_origin")' in runner
    assert 'result["failure_timeout_origin"]' in runner
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


def test_promotion_restart_retains_lifecycle_restart_elapsed_time() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )
    restart_probe = runner.split(
        "async def _run_x_server_timeout_probe", maxsplit=1
    )[1].split("async def _run_region_parity_probe", maxsplit=1)[0]

    assert "restart_started = time.perf_counter()" in restart_probe
    assert '"lifecycle_restart_elapsed_ms": lifecycle_restart_elapsed_ms' in restart_probe
    assert "finally:\n            lifecycle_restart_elapsed_ms = round(" in restart_probe


def test_promotion_restart_retains_allowlisted_readiness_categories() -> None:
    runner = Path("scripts/benchmarks/x11_shm_screenshot_runner.py").read_text(
        encoding="utf-8"
    )
    restart_probe = runner.split(
        "async def _run_x_server_timeout_probe", maxsplit=1
    )[1].split("async def _run_region_parity_probe", maxsplit=1)[0]

    assert "_safe_daemon_failure(exc)" in restart_probe
    assert '"failure_readiness_categories"' in restart_probe


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
