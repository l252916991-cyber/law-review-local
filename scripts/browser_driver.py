"""Drive a real Chromium-family browser through the DevTools protocol.

The repository has no browser-automation dependency: the page flows are verified
with the browser already installed on the host, speaking CDP over a small
RFC 6455 WebSocket client written here. When no supported browser is present the
caller must skip rather than claim a page verification.

The harness is deliberately thin — launch, navigate, evaluate one JavaScript
expression that returns JSON, screenshot — because the assertions belong in the
flow script, not in the driver.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

# Local DevTools traffic must never be routed through an HTTP proxy; the
# environment's proxy answers 502 for loopback and would look like a crash.
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CANDIDATES: tuple[str, ...] = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


def find_browser(explicit: str | None = None) -> str | None:
    """Return the first usable browser executable, or ``None``.

    ``LAW_REVIEW_BROWSER`` wins so a host can point at a non-standard install;
    the fallback list covers the desktop and CI locations we know about.
    """
    for candidate in (explicit, os.getenv("LAW_REVIEW_BROWSER"), *CANDIDATES):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class WebSocket:
    """Minimal client: text frames only, masked client-to-server payloads."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise ValueError("only ws:// URLs are supported")
        self.timeout = timeout
        self._socket = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=timeout)
        self._buffer = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port or 80}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise ConnectionError("devtools closed the handshake")
            header += chunk
        head, _, remainder = header.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"devtools refused the upgrade: {head[:120]!r}")
        self._buffer = remainder

    def _read(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise ConnectionError("devtools closed the connection")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        fragments: list[bytes] = []
        while True:
            first, second = self._read(2)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else b""
            payload = self._read(length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("devtools sent a close frame")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            fragments.append(payload)
            if first & 0x80:
                return b"".join(fragments).decode("utf-8")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        finally:
            self._socket.close()


class BrowserSession:
    """Headless browser page with a JSON-returning ``evaluate``."""

    def __init__(self, url: str, *, browser: str | None = None, width: int = 1440, height: int = 1000,
                 timeout: float = 45.0) -> None:
        self.executable = browser or find_browser()
        if not self.executable:
            raise RuntimeError("no supported browser installed")
        self.url = url
        self.width = width
        self.height = height
        self.timeout = timeout
        self.port = _free_port()
        self._profile = tempfile.TemporaryDirectory(prefix="lexvault-browser-")
        self._log = tempfile.TemporaryFile()
        self.process: subprocess.Popen[bytes] | None = None
        self.ws: WebSocket | None = None
        self.target_id = ""
        self._message_id = 0

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "BrowserSession":
        assert self.executable is not None
        self.process = subprocess.Popen(
            [
                self.executable, "--headless=new", "--disable-gpu", "--no-sandbox",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--disable-background-networking", "--disable-sync",
                f"--user-data-dir={self._profile.name}",
                f"--remote-debugging-port={self.port}",
                f"--window-size={self.width},{self.height}",
                self.url,
            ],
            stdout=self._log, stderr=self._log,
        )
        try:
            self._wait_for_devtools()
            self._open_target()
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *exc_info: Any) -> Literal[False]:
        self.close()
        return False

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self._profile.cleanup()
        self._log.close()

    def _log_tail(self) -> str:
        try:
            self._log.flush()
            self._log.seek(0)
            return self._log.read()[-1500:].decode("utf-8", "replace")
        except (OSError, ValueError):
            return ""

    def _wait_for_devtools(self) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                with _NO_PROXY.open(f"http://127.0.0.1:{self.port}/json/version", timeout=2) as response:
                    json.loads(response.read())
                return
            except OSError:
                if self.process is not None and self.process.poll() is not None:
                    break
                time.sleep(0.2)
        raise TimeoutError(f"the browser did not open a DevTools port: {self._log_tail()}")

    def _list_targets(self) -> list[dict[str, Any]]:
        with _NO_PROXY.open(f"http://127.0.0.1:{self.port}/json/list", timeout=5) as response:
            return list(json.loads(response.read()))

    def _open_target(self) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            pages = [item for item in self._list_targets() if item.get("type") == "page"]
            if pages:
                self.target_id = str(pages[0]["id"])
                self.ws = WebSocket(str(pages[0]["webSocketDebuggerUrl"]), timeout=self.timeout)
                self.send("Page.enable")
                self.send("Runtime.enable")
                return
            time.sleep(0.2)
        raise TimeoutError("the browser opened no page target")

    # -- protocol ----------------------------------------------------------
    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.ws is None:
            raise RuntimeError("browser session is not connected")
        self._message_id += 1
        message_id = self._message_id
        self.ws.send_text(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv_text())
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method} failed: {message['error']}")
            return dict(message.get("result") or {})

    def evaluate(self, expression: str, *, await_promise: bool = True) -> Any:
        result = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        })
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            raise RuntimeError(f"page script failed: {details.get('text')} {details.get('exception', {}).get('description', '')}")
        return result.get("result", {}).get("value")

    def wait_until(self, expression: str, *, timeout: float | None = None, interval: float = 0.2) -> Any:
        """Poll a JS expression until it returns a truthy value."""
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while time.monotonic() < deadline:
            value = self.evaluate(f"Boolean({expression})")
            if value:
                return self.evaluate(expression)
            time.sleep(interval)
        raise TimeoutError(f"timed out waiting for: {expression}")

    def screenshot(self, path: Path, *, selector: str | None = None) -> Path:
        """Capture the viewport, or one element after scrolling it into view.

        The application scrolls inside a panel rather than the window, so the
        element is brought into view with ``scrollIntoView`` and the clip is
        expressed in viewport coordinates.
        """
        params: dict[str, Any] = {"format": "png"}
        if selector:
            box = self.evaluate(
                "(() => { const node = document.querySelector(%s);"
                " if (!node) return null;"
                " node.scrollIntoView({block: 'center', inline: 'nearest'});"
                " const rect = node.getBoundingClientRect();"
                " return {x: rect.x, y: rect.y, width: rect.width, height: rect.height}; })()"
                % json.dumps(selector)
            )
            if not box or not box.get("width") or not box.get("height"):
                raise RuntimeError(f"cannot screenshot the element: {selector}")
            params["clip"] = {
                "x": max(0.0, float(box["x"])),
                "y": max(0.0, float(box["y"])),
                "width": float(box["width"]),
                "height": float(box["height"]),
                "scale": 1,
            }
        result = self.send("Page.captureScreenshot", params)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(result["data"]))
        return path
