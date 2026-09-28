from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass

    Observer = None

from utils import is_video_file


class WatchFolderHandler(FileSystemEventHandler):
    def __init__(self, extensions: list[str], stable_wait_seconds: float, on_stable: Callable[[str], None]):
        self.extensions = extensions
        self.stable_wait_seconds = stable_wait_seconds
        self.on_stable = on_stable
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_created(self, event):
        if not event.is_directory and is_video_file(event.src_path, self.extensions):
            self._reset_timer(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and is_video_file(event.src_path, self.extensions):
            self._reset_timer(event.src_path)

    def _reset_timer(self, path: str):
        with self._lock:
            if path in self._timers:
                self._timers[path].cancel()
            timer = threading.Timer(self.stable_wait_seconds, self._fire, [path])
            timer.daemon = True
            timer.start()
            self._timers[path] = timer

    def _fire(self, path: str):
        with self._lock:
            self._timers.pop(path, None)
        if Path(path).exists():
            self.on_stable(path)


def start_watcher(watch_folder: str, extensions: list[str], stable_wait_seconds: float, on_stable: Callable[[str], None]) -> Observer:
    Path(watch_folder).mkdir(parents=True, exist_ok=True)
    handler = WatchFolderHandler(extensions, stable_wait_seconds, on_stable)
    observer = Observer()
    observer.schedule(handler, watch_folder, recursive=False)
    observer.start()
    return observer
