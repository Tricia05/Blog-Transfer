"""Background scan runner: thread-based with stop / pause / progress."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .http import Fetcher
from .listing import discover_posts
from .post import extract_post


COLUMNS = [
    "ID", "Title", "Permalink",
    "blog_dates",
    "blog_Category", "blog_Tag",
    "blog_featured_image", "blog_content",
    "blog_metadesc", "blog_metatitle",
    "blog_status",
]


@dataclass
class ScanState:
    """Mutable, thread-safe-enough state for one scan run."""
    stop_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    progress: dict = field(default_factory=lambda: {
        "phase": "idle",     # idle | discovering | extracting | done | stopped
        "found": 0,
        "done": 0,
        "current_url": "",
        "message": "",
    })
    result_df: pd.DataFrame | None = None
    thread: threading.Thread | None = None

    # ------- control commands -------
    def request_stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()  # so paused thread can wake and exit

    def toggle_pause(self) -> bool:
        """Returns the new paused state."""
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.progress["message"] = "Resumed."
            return False
        self.pause_event.set()
        self.progress["message"] = "Paused."
        return True

    def is_paused(self) -> bool:
        return self.pause_event.is_set()

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def is_done(self) -> bool:
        return self.progress["phase"] in ("done", "stopped")


def _worker(
    state: ScanState,
    start_url: str,
    limit: int,
    statuses: list[str],
) -> None:
    fetcher = Fetcher()
    state.progress["phase"] = "discovering"
    state.progress["message"] = "Discovering and gathering post URLs..."

    try:
        post_urls = discover_posts(start_url, limit or None, fetcher)
    except Exception as e:
        state.progress["phase"] = "stopped"
        state.progress["message"] = f"Discovery failed: {e}"
        return

    if not post_urls:
        state.progress["phase"] = "stopped"
        state.progress["message"] = "No posts discovered at that URL."
        return

    state.progress["phase"] = "extracting"
    state.progress["found"] = len(post_urls)
    rows: list[dict] = []
    total = len(post_urls)

    for i, u in enumerate(post_urls, 1):
        # Stop check
        if state.stop_event.is_set():
            state.progress["message"] = f"Stopped after {len(rows)} posts."
            break
        # Pause loop
        while state.pause_event.is_set() and not state.stop_event.is_set():
            threading.Event().wait(0.2)
        if state.stop_event.is_set():
            break

        data = extract_post(u, fetcher)
        if data:
            data["ID"] = i
            data["blog_status"] = statuses[(i - 1) % len(statuses)]
            rows.append(data)

        state.progress["done"] = i
        state.progress["current_url"] = u

    df = pd.DataFrame(rows, columns=COLUMNS)
    if not df.empty:
        sort_dt = pd.to_datetime(df["blog_dates"].fillna(""), errors="coerce")
        df = (
            df.assign(_s=sort_dt)
              .sort_values("_s", ascending=False, na_position="last")
              .drop(columns="_s").reset_index(drop=True)
        )
    df["ID"] = range(1, len(df) + 1)
    state.result_df = df

    if state.stop_event.is_set():
        state.progress["phase"] = "stopped"
    else:
        state.progress["phase"] = "done"
        state.progress["message"] = "Done."


def start_scan(start_url: str, limit: int, statuses: list[str]) -> ScanState:
    state = ScanState()
    t = threading.Thread(
        target=_worker, args=(state, start_url, limit, statuses), daemon=True,
    )
    state.thread = t
    t.start()
    return state
