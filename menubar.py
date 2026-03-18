#!/usr/bin/env python3
"""Menu bar app for Plex Playlist Generator.

Usage:
    python3 menubar.py

To auto-start on login, add a Login Item in System Settings > General > Login Items.
"""
import subprocess
import threading
import webbrowser
from pathlib import Path

import rumps

APP_URL = "http://localhost:8484"
PROJECT_DIR = str(Path(__file__).parent)
CHECK_INTERVAL = 10  # seconds


def _app_icon_path() -> str | None:
    """Return the path to the bundled app icon, if it exists."""
    icon = Path(__file__).parent / "app" / "static" / "icon.png"
    return str(icon) if icon.exists() else None


def _docker_daemon_ready() -> bool:
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return True
    except Exception:
        return False


def _wait_for_docker(timeout: int = 60) -> bool:
    """Wait up to `timeout` seconds for Docker daemon to be ready."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _docker_daemon_ready():
            return True
        time.sleep(2)
    return False


def _docker_running() -> bool:
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--status", "running", "-q"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


class PlexPlaylistApp(rumps.App):
    def __init__(self):
        icon_path = _app_icon_path()
        super().__init__("", icon=icon_path, quit_button=None)
        self._has_icon = icon_path is not None
        if not self._has_icon:
            self.title = "♩"  # text fallback if icon file missing
        self._open_item = rumps.MenuItem("Open Plex Playlist", callback=self._open)
        self._start_item = rumps.MenuItem("Start", callback=self._start)
        self._stop_item = rumps.MenuItem("Stop", callback=self._stop)
        self.menu = [
            self._open_item,
            None,
            self._start_item,
            self._stop_item,
            None,
            rumps.MenuItem("Quit", callback=lambda _: rumps.quit_application()),
        ]
        self._refresh_status()
        self._timer = rumps.Timer(self._tick, CHECK_INTERVAL)
        self._timer.start()

    def _tick(self, _):
        self._refresh_status()

    def _refresh_status(self):
        running = _docker_running()
        if not self._has_icon:
            self.title = "♫" if running else "♩"
        else:
            self.title = "" if running else "⏸"
        self._start_item.set_callback(None if running else self._start)
        self._stop_item.set_callback(self._stop if running else None)

    def _open(self, _):
        if not _docker_running():
            threading.Thread(target=self._start_then_open, daemon=True).start()
        else:
            webbrowser.open(APP_URL)

    def _start_then_open(self):
        if not _wait_for_docker():
            return
        subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=PROJECT_DIR,
            capture_output=True,
        )
        self._refresh_status()
        import time
        # Give the app a moment to bind its port
        time.sleep(3)
        webbrowser.open(APP_URL)

    def _start(self, _):
        self.title = "…"
        threading.Thread(target=self._run_compose, args=(["up", "-d"],), daemon=True).start()

    def _stop(self, _):
        self.title = "…"
        threading.Thread(target=self._run_compose, args=(["down"],), daemon=True).start()

    def _run_compose(self, args: list[str]):
        if not _wait_for_docker():
            self._refresh_status()
            return
        subprocess.run(
            ["docker", "compose"] + args,
            cwd=PROJECT_DIR,
            capture_output=True,
        )
        self._refresh_status()


if __name__ == "__main__":
    PlexPlaylistApp().run()
