import json
import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from app.models.usage import RateLimitWindow, UsageSnapshot


logger = logging.getLogger("symphony.usage")


class CodexProtocolError(RuntimeError):
    pass


def parse_usage_snapshot(
    account_result: dict,
    limits_result: dict,
    *,
    now: Optional[float] = None,
) -> UsageSnapshot:
    account = account_result.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise CodexProtocolError("Codex is not signed in with a ChatGPT account")

    rate_limits = limits_result.get("rateLimits")
    if not isinstance(rate_limits, dict):
        rate_limits = {}

    windows = []
    for window_name in ("primary", "secondary"):
        window = rate_limits.get(window_name)
        if not isinstance(window, dict):
            continue

        used_percent = window.get("usedPercent")
        duration = window.get("windowDurationMins")
        if not isinstance(used_percent, (int, float)) or not isinstance(duration, int):
            continue

        resets_at = window.get("resetsAt")
        windows.append(
            RateLimitWindow(
                used_percent=float(used_percent),
                window_duration_minutes=duration,
                resets_at=resets_at if isinstance(resets_at, int) else None,
            )
        )

    reset_credits = limits_result.get("rateLimitResetCredits")
    available_count = None
    if isinstance(reset_credits, dict) and isinstance(reset_credits.get("availableCount"), int):
        available_count = reset_credits["availableCount"]

    plan_type = account.get("planType")
    reached_type = rate_limits.get("rateLimitReachedType")
    return UsageSnapshot(
        status="available",
        plan_type=plan_type if isinstance(plan_type, str) else None,
        windows=tuple(windows),
        rate_limit_reached_type=reached_type if isinstance(reached_type, str) else None,
        reset_credits_available=available_count,
        updated_at=time.time() if now is None else now,
    )


class CodexAppServerClient:
    def __init__(
        self,
        *,
        command: tuple[str, ...] = ("codex", "app-server", "--stdio"),
        request_timeout_seconds: float = 15.0,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ):
        self.command = command
        self.request_timeout_seconds = request_timeout_seconds
        self.process_factory = process_factory
        self.process: Optional[subprocess.Popen] = None
        self._messages: queue.Queue[Optional[str]] = queue.Queue()
        self._request_id = 0

    def connect(self) -> None:
        self.process = self.process_factory(
            list(self.command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        if self.process.stdout is None or self.process.stdin is None:
            raise CodexProtocolError("Codex app-server did not provide stdio pipes")

        stdout = self.process.stdout
        threading.Thread(target=self._read_stdout, args=(stdout,), daemon=True).start()
        if self.process.stderr is not None:
            stderr = self.process.stderr
            threading.Thread(target=self._drain_stderr, args=(stderr,), daemon=True).start()

        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "symphony_orchestrator",
                    "title": "Symphony Orchestrator",
                    "version": "1.0",
                }
            },
        )
        self._send({"method": "initialized"})

    def fetch_snapshot(self) -> UsageSnapshot:
        account_result = self._request("account/read", {"refreshToken": False})
        limits_result = self._request("account/rateLimits/read")
        return parse_usage_snapshot(account_result, limits_result)

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def _request(self, method: str, params: Optional[dict] = None) -> dict:
        self._request_id += 1
        request_id = self._request_id
        payload = {"method": method, "id": request_id}
        if params is not None:
            payload["params"] = params
        self._send(payload)

        deadline = time.monotonic() + self.request_timeout_seconds
        while True:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise CodexProtocolError(f"Timed out waiting for Codex response to {method}")
            try:
                line = self._messages.get(timeout=timeout)
            except queue.Empty as exc:
                raise CodexProtocolError(f"Timed out waiting for Codex response to {method}") from exc
            if line is None:
                raise CodexProtocolError("Codex app-server exited unexpectedly")

            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Ignoring invalid JSON from Codex app-server")
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                detail = error.get("message") if isinstance(error, dict) else str(error)
                raise CodexProtocolError(f"Codex request {method} failed: {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexProtocolError(f"Codex request {method} returned an invalid result")
            return result

    def _send(self, payload: dict) -> None:
        if self.process is None or self.process.stdin is None:
            raise CodexProtocolError("Codex app-server is not connected")
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def _read_stdout(self, stdout) -> None:
        for line in stdout:
            self._messages.put(line)
        self._messages.put(None)

    def _drain_stderr(self, stderr) -> None:
        for line in stderr:
            logger.debug("Codex app-server: %s", line.rstrip())


class CodexUsageCollector:
    def __init__(
        self,
        *,
        poll_interval_seconds: float = 60.0,
        stale_after_seconds: float = 180.0,
        retry_interval_seconds: float = 15.0,
        client_factory: Callable[[], CodexAppServerClient] = CodexAppServerClient,
    ):
        self.poll_interval_seconds = poll_interval_seconds
        self.stale_after_seconds = stale_after_seconds
        self.retry_interval_seconds = retry_interval_seconds
        self.client_factory = client_factory
        self._snapshot = UsageSnapshot()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._client: Optional[CodexAppServerClient] = None

    def stop(self) -> None:
        self._stop_event.set()
        client = self._client
        if client is not None:
            client.close()

    def snapshot(self) -> UsageSnapshot:
        with self._lock:
            snapshot = self._snapshot
        if (
            snapshot.updated_at is not None
            and time.time() - snapshot.updated_at > self.stale_after_seconds
        ):
            return snapshot.with_status("stale")
        return snapshot

    def run(self) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            client = self.client_factory()
            self._client = client
            try:
                client.connect()
                auth_marker = self._auth_state_marker()
                while not self._stop_event.is_set():
                    snapshot = client.fetch_snapshot()
                    with self._lock:
                        self._snapshot = snapshot

                    if self._stop_event.wait(self.poll_interval_seconds):
                        break
                    current_marker = self._auth_state_marker()
                    if current_marker != auth_marker:
                        logger.info("Codex authentication changed; reconnecting usage collector")
                        break
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.warning("Unable to refresh Codex usage: %s", exc)
                    with self._lock:
                        self._snapshot = replace(self._snapshot, error=str(exc))
            finally:
                client.close()
                self._client = None

            if not self._stop_event.is_set():
                self._stop_event.wait(self.retry_interval_seconds)

    def _auth_state_marker(self) -> Optional[tuple[int, int]]:
        codex_home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
        try:
            stat = (codex_home / "auth.json").stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size
