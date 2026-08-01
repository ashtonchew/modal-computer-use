from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import struct
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Literal
from urllib.request import urlopen

from modal_computer_use.daemon.process_environment import desktop_process_environment
from modal_computer_use.models import ActionResult, X11Window

BrowserGpuMode = Literal["auto", "off", "chromium-vulkan"]

DEFAULT_BROWSER_PROFILE_DIR = "/home/desktop/.cache/modal-computer-use/browser-profile"
CHROMIUM_VULKAN_ARGS = [
    "--enable-gpu",
    "--use-angle=vulkan",
    "--enable-features=Vulkan",
    "--disable-vulkan-surface",
]


class X11BrowserController:
    def __init__(
        self,
        *,
        browser: str | None,
        launch: Callable[[str, Sequence[str]], Awaitable[ActionResult]],
        windows: Callable[[], Awaitable[list[X11Window]]],
        profile_dir: str | None = None,
        launch_args: Sequence[str] = (),
        gpu_mode: str = "auto",
    ) -> None:
        self.browser = browser
        self.profile_dir = profile_dir
        self.launch_args = list(launch_args)
        self.gpu_mode = gpu_mode
        self._launch = launch
        self._windows = windows

    async def open_url(self, url: str, wait_for_window: bool = True) -> ActionResult:
        command = browser_command(self.browser)
        profile_dir = ensure_browser_profile(self.profile_dir)
        args = browser_launch_args(
            self.browser,
            url,
            profile_dir=profile_dir,
            extra_args=self.launch_args,
            gpu_mode=self.gpu_mode,
        )
        before = len(await self._windows()) if wait_for_window else None
        result = await self._launch(command, args)
        output = dict(result.output or {})
        output.update(
            {
                "browser": self.browser or command,
                "gpu_mode": self.gpu_mode,
                "launch_args": list(self.launch_args),
                "profile_dir": profile_dir,
            }
        )
        result.output = output
        if not result.ok or not wait_for_window:
            return result
        waited_ms = await self._wait_for_browser_window(previous_window_count=before or 0)
        result.output["waited_ms"] = waited_ms
        return result

    async def render_metrics(
        self,
        url: str,
        *,
        display: str,
        timeout_seconds: float = 30.0,
    ) -> dict[str, object]:
        if self.browser != "chromium":
            return {
                "ok": False,
                "message": "browser render metrics currently require chromium",
                "browser": self.browser,
            }
        command = browser_command(self.browser)
        executable = command
        profile_dir = ensure_browser_profile(self.profile_dir)
        try:
            return await asyncio.to_thread(
                measure_chromium_render_metrics,
                command=executable,
                url=url,
                display=display,
                profile_dir=profile_dir,
                launch_args=self.launch_args,
                gpu_mode=self.gpu_mode,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            return {
                "ok": False,
                "message": "browser render metrics failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "browser": self.browser,
                "gpu_mode": self.gpu_mode,
                "url": url,
            }

    async def prewarm(self) -> ActionResult:
        if not self.browser:
            return ActionResult(ok=True, message="browser prewarm skipped: no browser configured")
        return await self.open_url("about:blank", wait_for_window=True)

    async def _wait_for_browser_window(self, *, previous_window_count: int) -> int:
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            windows = await self._windows()
            if len(windows) > previous_window_count or any(
                _browser_title_match(window.title) for window in windows
            ):
                return int((10 - (deadline - asyncio.get_running_loop().time())) * 1000)
            await asyncio.sleep(0.2)
        return 10_000


def browser_command(browser: str | None) -> str:
    if browser == "firefox":
        return "firefox"
    if browser == "chromium":
        return "chromium"
    return browser or "xdg-open"


def browser_launch_args(
    browser: str | None,
    url: str,
    *,
    profile_dir: str | None = None,
    extra_args: Sequence[str] | None = None,
    gpu_mode: str = "auto",
) -> list[str]:
    resolved_profile_dir = profile_dir or DEFAULT_BROWSER_PROFILE_DIR
    browser_args = list(extra_args or [])
    if browser == "firefox":
        return ["--profile", resolved_profile_dir, *browser_args, "--new-tab", url]
    if browser == "chromium":
        return [
            f"--user-data-dir={resolved_profile_dir}",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            *_chromium_gpu_args(gpu_mode),
            *browser_args,
            url,
        ]
    return [url]


def ensure_browser_profile(profile_dir: str | None) -> str:
    resolved = profile_dir or DEFAULT_BROWSER_PROFILE_DIR
    Path(resolved).mkdir(parents=True, exist_ok=True)
    return resolved


def measure_chromium_render_metrics(
    *,
    command: str,
    url: str,
    display: str,
    profile_dir: str,
    launch_args: Sequence[str],
    gpu_mode: str,
    timeout_seconds: float,
) -> dict[str, object]:
    port = _free_local_port()
    probe_profile = f"{profile_dir}-render-probe-{port}"
    Path(probe_profile).mkdir(parents=True, exist_ok=True)
    args = [
        command,
        *browser_launch_args(
            "chromium",
            url,
            profile_dir=probe_profile,
            extra_args=launch_args,
            gpu_mode=gpu_mode,
        ),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    env = desktop_process_environment(display=display)
    started = time.perf_counter()
    process = subprocess.Popen(  # noqa: S603
        args,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        target = _wait_for_cdp_target(port, timeout_seconds=timeout_seconds)
        metrics = _wait_for_navigation_metrics(
            str(target["webSocketDebuggerUrl"]),
            timeout_seconds=timeout_seconds,
        )
        wall_ms = (time.perf_counter() - started) * 1000
        return {
            "ok": True,
            "url": url,
            "command": command,
            "args": args[1:],
            "pid": process.pid,
            "gpu_mode": gpu_mode,
            "profile_dir": probe_profile,
            "wall_ms": wall_ms,
            "metrics": metrics,
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _chromium_gpu_args(gpu_mode: str) -> list[str]:
    if gpu_mode == "off":
        return ["--disable-gpu"]
    if gpu_mode == "chromium-vulkan":
        return list(CHROMIUM_VULKAN_ARGS)
    return ["--enable-gpu"]


def _browser_title_match(title: str) -> bool:
    lowered = title.lower()
    return any(token in lowered for token in ("firefox", "mozilla", "chromium", "chrome"))


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_cdp_target(port: int, *, timeout_seconds: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(  # nosemgrep: dynamic-urllib-use-detected -- fixed loopback HTTP URL.
                f"http://127.0.0.1:{port}/json/list", timeout=1
            ) as response:
                targets = json.loads(response.read().decode("utf-8"))
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return dict(target)
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for Chromium CDP target: {last_error}")


def _wait_for_navigation_metrics(
    websocket_url: str,
    *,
    timeout_seconds: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    with _LocalWebSocket(websocket_url, timeout=timeout_seconds) as websocket:
        request_id = 1
        while time.monotonic() < deadline:
            metrics = _cdp_evaluate(websocket, request_id)
            request_id += 1
            navigation = metrics.get("navigation")
            if (
                isinstance(navigation, dict)
                and float(navigation.get("loadEventEnd") or 0) > 0
                and metrics.get("readyState") == "complete"
            ):
                return metrics
            time.sleep(0.1)
    raise TimeoutError("timed out waiting for browser navigation metrics")


def _cdp_evaluate(websocket: _LocalWebSocket, request_id: int) -> dict[str, object]:
    expression = """
(() => {
  const nav = performance.getEntriesByType("navigation")[0];
  const paints = performance.getEntriesByType("paint").map((entry) => ({
    name: entry.name,
    startTime: entry.startTime
  }));
  const canvas = document.createElement("canvas");
  const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
  let webgl = null;
  if (gl) {
    const debug = gl.getExtension("WEBGL_debug_renderer_info");
    webgl = {
      vendor: debug ? gl.getParameter(debug.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
      renderer: debug
        ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL)
        : gl.getParameter(gl.RENDERER)
    };
  }
  return {
    url: location.href,
    title: document.title,
    readyState: document.readyState,
    bodyTextLength: document.body ? document.body.innerText.length : 0,
    navigation: nav ? nav.toJSON() : null,
    paint: paints,
    webgl
  };
})()
"""
    websocket.send_json(
        {
            "id": request_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        }
    )
    while True:
        message = websocket.recv_json()
        if message.get("id") != request_id:
            continue
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"CDP evaluation failed: {message}")
        value = result.get("result")
        if not isinstance(value, dict) or "value" not in value:
            raise RuntimeError(f"CDP evaluation did not return a value: {message}")
        evaluated = value["value"]
        if not isinstance(evaluated, dict):
            raise RuntimeError(f"CDP evaluation returned unexpected value: {evaluated!r}")
        return evaluated


class _LocalWebSocket:
    def __init__(self, url: str, *, timeout: float) -> None:
        if not url.startswith("ws://127.0.0.1:"):
            raise ValueError("only local ws://127.0.0.1 CDP targets are supported")
        rest = url.removeprefix("ws://")
        host_port, _, path = rest.partition("/")
        host, _, port = host_port.partition(":")
        self.host = host
        self.port = int(port)
        self.path = "/" + path
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def __enter__(self) -> _LocalWebSocket:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self.sock.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"CDP websocket handshake failed: {response[:120]!r}")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def send_json(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self._send_frame(data)

    def recv_json(self) -> dict[str, object]:
        payload = self._recv_frame()
        decoded = json.loads(payload.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError(f"unexpected websocket payload: {decoded!r}")
        return decoded

    def _send_frame(self, payload: bytes) -> None:
        if self.sock is None:
            raise RuntimeError("websocket is not connected")
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.extend([0x80 | 126, *struct.pack("!H", length)])
        else:
            header.extend([0x80 | 127, *struct.pack("!Q", length)])
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_frame(self) -> bytes:
        if self.sock is None:
            raise RuntimeError("websocket is not connected")
        first = self._recv_exact(2)
        opcode = first[0] & 0x0F
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if first[1] & 0x80:
            mask = self._recv_exact(4)
            payload = self._recv_exact(length)
            return bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        payload = self._recv_exact(length)
        if opcode == 0x8:
            raise RuntimeError("websocket closed")
        if opcode != 0x1:
            return self._recv_frame()
        return payload

    def _recv_exact(self, length: int) -> bytes:
        if self.sock is None:
            raise RuntimeError("websocket is not connected")
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.sock.recv(length - len(chunks))
            if not chunk:
                raise RuntimeError("websocket closed while reading")
            chunks.extend(chunk)
        return bytes(chunks)


__all__ = [
    "DEFAULT_BROWSER_PROFILE_DIR",
    "BrowserGpuMode",
    "X11BrowserController",
    "browser_command",
    "browser_launch_args",
    "ensure_browser_profile",
    "measure_chromium_render_metrics",
]
