"""Run the authorized external-provider action-to-frame benchmark.

The command wraps the provider comparison seam. It uses one warm resource for each
provider and measures the fixed public action-to-immediate-frame case. Modal's
``computer.step`` result is intentionally outside this runner; the separate Step
promotion artifact supplies that arm.

No provider call is made unless ``--authorize`` is present. Provider identifiers
are used only for private inventory accounting. Tracked output contains counts and
statuses, never identifiers, credentials, URLs, screenshot bytes, or raw errors.

Pilot (candidate evidence)::

    uv run python scripts/run_external_action_frame_benchmark.py \
      --env-file .env --iterations 3 --authorize \
      --output benchmark-results/candidates/external-action-frame-pilot.json

Full run (publication candidate)::

    uv run python scripts/run_external_action_frame_benchmark.py \
      --env-file .env --iterations 100 --authorize \
      --output benchmark-results/candidates/external-action-frame-<date>.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_PROVIDERS = ("daytona", "e2b", "tzafon")
ACTION_FRAME_CASE = "action_to_immediate_frame"
ACTION_FRAME_CASE_ID = "ordered-actions-to-immediate-frame-v1"
ACTION_FRAME_SEMANTICS = "one-left-click-at-512-384-then-immediate-full-frame"
ACTION_FRAME_ACTION_PAYLOAD_SHA256 = (
    "83599900ae670680c7d84271000b03114940c492d935c26b5f0999a281958296"
)
ACTION_FRAME_TIMER_BOUNDARY = (
    "caller_before_ordered_action_dispatch_to_validated_immediate_full_frame_bytes"
)
DEFAULT_WARMUP_ITERATIONS = 1
DEFAULT_ITERATIONS = 100
DEFAULT_COMPARE_TIMEOUT_SECONDS = 3600
ENV_KEYS = frozenset(
    {
        "DAYTONA_API_KEY",
        "DAYTONA_API_URL",
        "DAYTONA_TARGET",
        "DAYTONA_SNAPSHOT",
        "E2B_API_KEY",
        "E2B_TEMPLATE",
        "TZAFON_API_KEY",
        "LIGHTCONE_BASE_URL",
    }
)
PROVIDER_CREDENTIALS = {
    "daytona": "DAYTONA_API_KEY",
    "e2b": "E2B_API_KEY",
    "tzafon": "TZAFON_API_KEY",
}
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class InventoryError(RuntimeError):
    """Raised when a provider resource inventory cannot be verified."""


@dataclass(frozen=True)
class StaticInventory:
    """Test seam for deterministic before/after inventory snapshots."""

    provider: str
    before: set[str]
    after: set[str]

    def snapshot_before(self) -> set[str]:
        return set(self.before)

    def snapshot_after(self) -> set[str]:
        return set(self.after)


class ProviderInventory:
    """Adapter around one provider's list API."""

    def __init__(self, provider: str, target: Any, list_method: Callable[[], Any]) -> None:
        self.provider = provider
        self._target = target
        self._list_method = list_method

    def snapshot(self) -> set[str]:
        try:
            listing = self._list_method()
            if inspect.isawaitable(listing):
                import asyncio

                listing = asyncio.run(listing)
        except Exception as exc:
            raise InventoryError(f"{self.provider} resource inventory failed") from exc
        return extract_resource_ids(_flatten_listing(listing))

    def snapshot_before(self) -> set[str]:
        return self.snapshot()

    def snapshot_after(self) -> set[str]:
        return self.snapshot()


def require_live_authorization(
    authorized: bool,
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Reject accidental live execution before provider imports or network I/O."""

    if not authorized:
        raise PermissionError(
            "external action-frame benchmark is live and billable; pass --authorize explicitly"
        )
    values = environment if environment is not None else os.environ
    missing = [name for name in PROVIDER_CREDENTIALS.values() if not values.get(name)]
    if missing:
        names = ", ".join(missing)
        raise PermissionError(f"external action-frame benchmark credentials are missing: {names}")


def load_benchmark_environment(path: Path | None) -> dict[str, str]:
    """Load allowlisted dotenv keys without printing their values.

    Existing process values win. The return value reports only whether a key was
    loaded or preserved, so callers can log it without exposing credentials.
    """

    if path is None:
        return {}
    if not path.is_file():
        raise FileNotFoundError(f"benchmark env file does not exist: {path}")
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in ENV_KEYS:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not value:
            continue
        if os.environ.get(key):
            result[key] = "preserved"
            continue
        os.environ[key] = value
        result[key] = "loaded"
    return result


def extract_resource_ids(values: Iterable[Any]) -> set[str]:
    """Extract provider resource IDs from list responses without retaining objects."""

    result: set[str] = set()
    for value in values:
        identifier = _resource_id(value)
        if identifier:
            result.add(identifier)
    return result


def _resource_id(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key in ("id", "sandbox_id", "computer_id", "uuid"):
            raw = value.get(key)
            if isinstance(raw, str) and raw:
                return raw
            if isinstance(raw, int) and not isinstance(raw, bool):
                return str(raw)
        return None
    for name in ("id", "sandbox_id", "computer_id", "uuid"):
        raw = getattr(value, name, None)
        if isinstance(raw, str) and raw:
            return raw
        if isinstance(raw, int) and not isinstance(raw, bool):
            return str(raw)
    return None


def _flatten_listing(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        if _resource_id(value):
            return [value]
        for key in ("items", "data", "sandboxes", "computers", "results"):
            if key in value:
                return _flatten_listing(value[key])
        return []
    if isinstance(value, (str, bytes, bytearray)):
        return []
    # Daytona returns an object with an ``items`` attribute.  E2B returns a
    # cursor paginator whose pages are exposed through ``next_items``.  Treat
    # both as list responses instead of silently converting them to an empty
    # inventory (which would make leaked resources look clean).
    next_items = getattr(value, "next_items", None)
    has_next = getattr(value, "has_next", None)
    if callable(next_items) and isinstance(has_next, bool):
        items: list[Any] = []
        page_count = 0
        while bool(getattr(value, "has_next", False)):
            page_count += 1
            if page_count > 1000:
                raise InventoryError("provider resource inventory pagination exceeded its bound")
            page = next_items()
            if page is value:
                raise InventoryError("provider resource inventory returned its paginator")
            items.extend(_flatten_listing(page))
        return items
    for key in ("items", "data", "sandboxes", "computers", "results"):
        child = getattr(value, key, None)
        if child is not None and child is not value:
            return _flatten_listing(child)
    if isinstance(value, Iterable):
        return list(value)
    if _resource_id(value):
        return [value]
    return []


def verify_cleanup(
    before: set[str] | None,
    after: set[str] | None,
) -> dict[str, int | str | None]:
    """Compare provider inventories and fail closed when either side is unknown."""

    if before is None or after is None:
        return {"status": "unverifiable", "survivors": None}
    survivors = len(after - before)
    return {
        "status": "clean" if survivors == 0 else "survivors",
        "survivors": survivors,
    }


def hash_resource_ids(values: Iterable[str]) -> list[str]:
    """Return deterministic hashes for private diagnostics without retaining IDs."""

    return sorted(hashlib.sha256(value.encode("utf-8")).hexdigest() for value in values)


def build_provider_inventories() -> dict[str, ProviderInventory]:
    """Build list-only adapters for Daytona, E2B Desktop, and Tzafon.

    The adapters use documented or SDK-exposed list methods and never create a
    resource. An absent list method makes the run unverifiable and therefore fails
    closed before the benchmark command is started.
    """

    return {
        "daytona": _daytona_inventory(),
        "e2b": _e2b_inventory(),
        "tzafon": _tzafon_inventory(),
    }


def _daytona_inventory() -> ProviderInventory:
    try:
        module = importlib.import_module("daytona")
        config_cls = getattr(module, "DaytonaConfig", None)
        client_cls = module.Daytona
        kwargs: dict[str, Any] = {"api_key": os.environ["DAYTONA_API_KEY"]}
        for env_name, option in (("DAYTONA_API_URL", "api_url"), ("DAYTONA_TARGET", "target")):
            if os.environ.get(env_name):
                kwargs[option] = os.environ[env_name]
        client = client_cls(config_cls(**kwargs)) if config_cls is not None else client_cls()
    except Exception as exc:
        raise InventoryError("Daytona inventory client is unavailable") from exc
    method = _find_list_method(client, ("list", "list_sandboxes", "list_all"))
    return ProviderInventory("daytona", client, method)


def _e2b_inventory() -> ProviderInventory:
    try:
        module = importlib.import_module("e2b_desktop")
        target = module.Sandbox
    except Exception as exc:
        raise InventoryError("E2B inventory client is unavailable") from exc
    method = _find_list_method(target, ("list", "list_sandboxes", "list_all"))
    return ProviderInventory("e2b", target, method)


def _tzafon_inventory() -> ProviderInventory:
    try:
        module = importlib.import_module("tzafon")
        lightcone = module.Lightcone
        kwargs: dict[str, Any] = {"api_key": os.environ["TZAFON_API_KEY"]}
        if os.environ.get("LIGHTCONE_BASE_URL"):
            kwargs["base_url"] = os.environ["LIGHTCONE_BASE_URL"]
        client = lightcone(**kwargs)
        target = client.computers
    except Exception as exc:
        raise InventoryError("Tzafon inventory client is unavailable") from exc
    method = _find_list_method(target, ("list", "list_all"))
    return ProviderInventory("tzafon", target, method)


def _find_list_method(target: Any, names: tuple[str, ...]) -> Callable[[], Any]:
    for name in names:
        method = getattr(target, name, None)
        if callable(method):
            return method
    raise InventoryError("provider resource list API is unavailable")


def execute_compare(command: list[str]) -> tuple[int, str, str]:
    """Execute the fixed provider comparison command without shell interpolation."""

    child_environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    existing_pythonpath = child_environment.get("PYTHONPATH")
    child_environment["PYTHONPATH"] = (
        source_path
        if not existing_pythonpath
        else os.pathsep.join((source_path, existing_pythonpath))
    )
    try:
        result = subprocess.run(  # noqa: S603 - command is assembled from fixed allowlisted flags.
            command,
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            env=child_environment,
            timeout=DEFAULT_COMPARE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"compare command timed out after {DEFAULT_COMPARE_TIMEOUT_SECONDS}s"
    return result.returncode, result.stdout, result.stderr


def build_compare_command(
    *,
    env_file: Path | None,
    iterations: int,
) -> list[str]:
    # Invoke the module with the current interpreter.  Resolving a console script
    # from PATH can select another checkout's editable install and make the
    # recorded source SHA describe code that did not run.
    command = [sys.executable, "-m", "modal_computer_use.cli"]
    command.extend(
        [
            "benchmark",
            "compare",
            "--providers",
            ",".join(EXTERNAL_PROVIDERS),
            "--case",
            "action-to-immediate-frame",
            "--iterations",
            str(iterations),
        ]
    )
    if env_file is not None:
        command.extend(("--env-file", str(env_file)))
    return command


def _parse_compare_output(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return {
            "ok": False,
            "providers": {},
            "failures": [{"phase": "runner", "category": "invalid_json_output"}],
        }
    return (
        payload
        if isinstance(payload, dict)
        else {
            "ok": False,
            "providers": {},
            "failures": [{"phase": "runner", "category": "invalid_json_output"}],
        }
    )


def build_tracked_payload(
    compare: Mapping[str, Any],
    *,
    source_sha: str,
    evidence_date: str,
    cleanup: Mapping[str, Mapping[str, int | str | None]],
    inventories: Mapping[str, tuple[set[str] | None, set[str] | None]] | None = None,
    iterations: int,
    warmup_iterations: int = DEFAULT_WARMUP_ITERATIONS,
) -> dict[str, Any]:
    """Project the compare output into a secret-free tracked orchestration artifact."""

    providers: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    compare_providers = compare.get("providers")
    compare_map = compare_providers if isinstance(compare_providers, Mapping) else {}
    for provider in EXTERNAL_PROVIDERS:
        raw_provider = compare_map.get(provider)
        provider_payload = raw_provider if isinstance(raw_provider, Mapping) else {}
        raw_cases = provider_payload.get("cases")
        cases = raw_cases if isinstance(raw_cases, Mapping) else {}
        raw_case = cases.get(ACTION_FRAME_CASE)
        case = _safe_case(raw_case)
        if case:
            case["source_sha"] = source_sha
        provider_failures = _safe_failures(
            provider_payload.get("failures"),
            raw_case.get("failures") if isinstance(raw_case, Mapping) else None,
        )
        failures.extend({"provider": provider, **failure} for failure in provider_failures)
        cleanup_value = cleanup.get(
            provider,
            {"status": "unverifiable", "survivors": None},
        )
        before_after = (inventories or {}).get(provider)
        providers[provider] = {
            "status": provider_payload.get("status", "failed"),
            "source_sha": source_sha,
            "metadata": _safe_provider_metadata(provider_payload.get("metadata")),
            "case": case,
            "failures": provider_failures,
            "cleanup": {
                "status": cleanup_value.get("status"),
                "survivors": cleanup_value.get("survivors"),
            },
            "inventory": {
                "before_count": (
                    len(before_after[0]) if before_after and before_after[0] is not None else None
                ),
                "after_count": (
                    len(before_after[1]) if before_after and before_after[1] is not None else None
                ),
            },
        }
    raw_failures = compare.get("failures")
    failures.extend(_safe_failures(raw_failures, None))
    # A clean status is meaningful only when both inventories were observed.  A
    # missing or failed list call must remain rejected, even if a caller supplied
    # a stale cleanup map that says clean.
    all_clean = True
    for provider, value in providers.items():
        before_after = (inventories or {}).get(provider)
        if (
            before_after is None
            or before_after[0] is None
            or before_after[1] is None
            or value["cleanup"] != {"status": "clean", "survivors": 0}
        ):
            all_clean = False
            break
    all_measured = all(
        value["status"] == "ok"
        and isinstance(value["case"], Mapping)
        and _case_matches_contract(value["case"], iterations)
        and not value["failures"]
        for value in providers.values()
    )
    status = "eligible" if all_clean and all_measured and iterations >= 30 else "candidate"
    if failures or not all_clean or not all_measured:
        status = "rejected"
    return {
        "schema_version": 1,
        "benchmark": "external-provider-action-frame-run",
        "status": status,
        "evidence_date": evidence_date,
        "source_sha": source_sha,
        "case_id": ACTION_FRAME_CASE_ID,
        "action_semantics": ACTION_FRAME_SEMANTICS,
        "timer_boundary": ACTION_FRAME_TIMER_BOUNDARY,
        "warmup_iterations": warmup_iterations,
        "measured_iterations": iterations,
        "providers": providers,
        "cleanup": {
            "source_sha": source_sha,
            "providers": {
                provider: {
                    "status": value["cleanup"]["status"],
                    "survivors": value["cleanup"]["survivors"],
                }
                for provider, value in providers.items()
            },
        },
        "failures": failures,
    }


def _safe_case(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"status": "not_measured", "failures": []}
    allowed = {
        "status",
        "iterations",
        "successful_iterations",
        "samples_ms",
        "summary_ms",
        "case_id",
        "provider",
        "path",
        "timer_boundary",
        "action_semantics",
        "action_payload_sha256",
        "action_count",
        "screenshot",
        "request_shape",
        "harness_retries",
        "replacement_samples",
    }
    return {key: value[key] for key in allowed if key in value}


def _case_matches_contract(case: Mapping[str, Any], iterations: int) -> bool:
    """Gate publication on the fixed action, timer, and validated frame shape."""

    screenshot = case.get("screenshot")
    samples = case.get("samples_ms")
    if not isinstance(screenshot, Mapping) or not isinstance(samples, list):
        return False
    width = screenshot.get("width")
    height = screenshot.get("height")
    image_format = screenshot.get("format")
    return (
        case.get("status") == "ok"
        and case.get("case_id") == ACTION_FRAME_CASE_ID
        and case.get("action_semantics") == ACTION_FRAME_SEMANTICS
        and case.get("action_payload_sha256") == ACTION_FRAME_ACTION_PAYLOAD_SHA256
        and case.get("timer_boundary") == ACTION_FRAME_TIMER_BOUNDARY
        and case.get("successful_iterations") == iterations
        and len(samples) == iterations
        and all(
            isinstance(sample, int | float)
            and not isinstance(sample, bool)
            and math.isfinite(float(sample))
            and sample >= 0
            for sample in samples
        )
        and isinstance(width, int)
        and width > 0
        and isinstance(height, int)
        and height > 0
        and isinstance(image_format, str)
        and bool(image_format.strip())
        and case.get("harness_retries") == 0
        and case.get("replacement_samples") == 0
    )


def _safe_provider_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "sdk_package",
        "sdk_version",
        "sdk_retry_policy",
        "sdk_max_retries",
        "target_kind",
        "startup_model",
        "uses_snapshot_or_template",
        "readiness_contract",
        "setup_included",
        "ingress_included",
        "first_observation_api",
        "resolution",
        "dpi",
        "display",
        "cpu_count",
        "memory_gib",
        "storage_gib",
        "cpu_count_source",
        "memory_gib_source",
        "storage_gib_source",
        "persistent",
        "computer_kind",
        "topology",
    }
    return {key: value[key] for key in allowed if key in value}


def _safe_failures(*values: Any) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, Mapping):
                failures.append({"phase": "unknown", "category": "benchmark_failure"})
                continue
            safe: dict[str, Any] = {
                "phase": item.get("phase", "unknown"),
                "category": item.get("type") or item.get("category") or "benchmark_failure",
            }
            if isinstance(item.get("iteration"), int):
                safe["iteration"] = item["iteration"]
            failures.append(safe)
    return failures


def _git_source_sha() -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to record the benchmark source")
    result = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
        [git, "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    source_sha = result.stdout.strip()
    if SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise RuntimeError("git HEAD is not a full lowercase SHA")
    return source_sha


def verify_clean_source_revision(source_sha: str) -> None:
    if SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise ValueError("source_sha must be one full lowercase Git SHA")
    if _git_source_sha() != source_sha:
        raise ValueError("source_sha does not match git HEAD")
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to verify the benchmark source")
    result = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
        [git, "status", "--porcelain=v1", "--untracked-files=normal"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise ValueError("worktree must be clean before a live external benchmark")


def _write_json_new(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"benchmark output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _ensure_outputs_available(output_path: Path, private_output: Path | None) -> None:
    """Reject output collisions before any provider resource is touched."""

    paths = [output_path, private_output] if private_output is not None else [output_path]
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("tracked and private benchmark outputs must be different paths")
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError(f"benchmark output already exists: {', '.join(existing)}")


def _write_private(
    path: Path,
    *,
    compare: Mapping[str, Any],
    inventories: Mapping[str, Any],
) -> None:
    private = {
        "compare": compare,
        "inventory_hashes": {
            provider: {
                "before": hash_resource_ids(before or set()),
                "after": hash_resource_ids(after or set()),
            }
            for provider, (before, after) in inventories.items()
        },
    }
    _write_json_new(path, private)


def run_benchmark(
    *,
    authorize: bool,
    env_file: Path | None,
    output_path: Path,
    iterations: int,
    warmup_iterations: int = DEFAULT_WARMUP_ITERATIONS,
    source_sha: str | None = None,
    private_output: Path | None = None,
    inventory: Mapping[str, Any] | None = None,
    source_verifier: Callable[[str], None] | None = None,
    executor: Callable[[list[str]], tuple[int, str, str]] | None = None,
) -> dict[str, Any]:
    """Run one external action-frame campaign and write its sanitized result."""

    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    if warmup_iterations != DEFAULT_WARMUP_ITERATIONS:
        raise ValueError("the compare seam uses exactly one warmup iteration")
    if not authorize:
        require_live_authorization(False)
    _ensure_outputs_available(output_path, private_output)
    load_benchmark_environment(env_file)
    require_live_authorization(True)
    source = source_sha or _git_source_sha()
    (source_verifier or verify_clean_source_revision)(source)
    inventories = dict(inventory or build_provider_inventories())
    if set(inventories) != set(EXTERNAL_PROVIDERS):
        raise InventoryError("inventory adapters must cover exactly Daytona, E2B, and Tzafon")
    before: dict[str, set[str]] = {}
    for provider in EXTERNAL_PROVIDERS:
        try:
            before[provider] = set(inventories[provider].snapshot_before())
        except Exception as exc:
            raise InventoryError(f"{provider} pre-run inventory is unverifiable") from exc
    command = build_compare_command(env_file=env_file, iterations=iterations)
    return_code, stdout, _stderr = (executor or execute_compare)(command)
    compare = _parse_compare_output(stdout)
    after: dict[str, set[str] | None] = {}
    cleanup: dict[str, dict[str, int | str | None]] = {}
    for provider in EXTERNAL_PROVIDERS:
        try:
            after[provider] = set(inventories[provider].snapshot_after())
        except Exception:
            after[provider] = None
        cleanup[provider] = verify_cleanup(before.get(provider), after[provider])
    payload = build_tracked_payload(
        compare,
        source_sha=source,
        evidence_date=date.today().isoformat(),
        cleanup=cleanup,
        inventories={
            provider: (before.get(provider), after.get(provider)) for provider in EXTERNAL_PROVIDERS
        },
        iterations=iterations,
        warmup_iterations=warmup_iterations,
    )
    if return_code != 0 and not payload["failures"]:
        payload["failures"] = [{"phase": "runner", "category": "compare_command_failed"}]
        payload["status"] = "rejected"
    _write_json_new(output_path, payload)
    if private_output is not None:
        _write_private(
            private_output,
            compare=compare,
            inventories={
                provider: (before.get(provider), after.get(provider))
                for provider in EXTERNAL_PROVIDERS
            },
        )
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--warmup-iterations", type=int, default=DEFAULT_WARMUP_ITERATIONS)
    parser.add_argument("--source-sha")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmark-results/candidates")
        / f"external-action-frame-{date.today().isoformat()}.json",
    )
    parser.add_argument("--private-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = run_benchmark(
            authorize=args.authorize,
            env_file=args.env_file,
            output_path=args.output,
            iterations=args.iterations,
            warmup_iterations=args.warmup_iterations,
            source_sha=args.source_sha,
            private_output=args.private_output,
        )
    except (FileNotFoundError, InventoryError, PermissionError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "benchmark": payload["benchmark"],
                "status": payload["status"],
                "source_sha": payload["source_sha"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0 if payload["status"] in {"eligible", "candidate"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
