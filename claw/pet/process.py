"""Lifecycle manager for the out-of-process desktop pet window."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from claw.paths import is_frozen


class PetProcessManager:
    def __init__(self, gateway_url: str, data_dir: Path):
        self.gateway_url = gateway_url.rstrip("/")
        self.data_dir = Path(data_dir)
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def start(self) -> bool:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return False
            if is_frozen():
                command = [
                    sys.executable,
                    "--pet",
                    "--gateway-url",
                    self.gateway_url,
                    "--data-dir",
                    str(self.data_dir),
                ]
            else:
                command = [
                    sys.executable,
                    "-m",
                    "claw.pet",
                    "--gateway-url",
                    self.gateway_url,
                    "--data-dir",
                    str(self.data_dir),
                ]
            kwargs: dict = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "cwd": str(Path.cwd()),
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self._process = subprocess.Popen(command, **kwargs)
            return True

    def stop(self, timeout: float = 3.0) -> bool:
        with self._lock:
            process = self._process
            self._process = None
        if process is None or process.poll() is not None:
            return False
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
        return True
