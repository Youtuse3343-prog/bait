from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import requests

from bot.config import settings
from bot.store import store


class KeepAliveService:
    """Small self-ping worker controlled from the owner dashboard.

    The destination is deliberately fixed to this dashboard's /health route so
    the feature cannot be repurposed as an arbitrary request/SSRF tool.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = {
            "running": False,
            "last_attempt": None,
            "last_success": None,
            "last_status": None,
            "last_error": None,
        }

    @property
    def target_url(self) -> str:
        return settings.dashboard_base_url.rstrip("/") + "/health"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="self-keep-alive")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """Apply changed dashboard settings without waiting for the old interval."""
        self._wake.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {**self._state, "target_url": self.target_url}

    def ping_once(self) -> tuple[bool, str]:
        attempted = datetime.now(timezone.utc).isoformat()
        try:
            response = requests.get(
                self.target_url,
                timeout=10,
                allow_redirects=False,
                headers={"User-Agent": "ProfessionalDiscordBot-KeepAlive/1.0"},
            )
            ok = 200 <= response.status_code < 300
            with self._lock:
                self._state["last_attempt"] = attempted
                self._state["last_status"] = response.status_code
                self._state["last_error"] = None if ok else f"HTTP {response.status_code}"
                if ok:
                    self._state["last_success"] = attempted
            return ok, f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            # Keep the dashboard useful without exposing a potentially long or
            # sensitive requests exception in the UI.
            message = exc.__class__.__name__
            with self._lock:
                self._state["last_attempt"] = attempted
                self._state["last_status"] = None
                self._state["last_error"] = message
            return False, message

    def _run(self) -> None:
        with self._lock:
            self._state["running"] = True
        try:
            while not self._stop.is_set():
                cfg = store.get_global().get("keep_alive", {})
                enabled = bool(cfg.get("enabled", False))
                interval = max(60, min(3600, int(cfg.get("interval_seconds", 300))))

                if enabled:
                    self.ping_once()
                    wait_for = interval
                else:
                    # Recheck fairly quickly while disabled so enabling from the
                    # dashboard takes effect without restarting the process.
                    wait_for = 15

                self._wake.wait(wait_for)
                self._wake.clear()
        finally:
            with self._lock:
                self._state["running"] = False
