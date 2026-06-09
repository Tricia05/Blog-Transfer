"""Small wrapper for polite HTTP fetching."""
from __future__ import annotations

import time
import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 BlogTransfer/1.0"
)


class Fetcher:
    def __init__(self, delay: float = 0.7, timeout: int = 20) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.delay = delay
        self.timeout = timeout
        self._last = 0.0

    def get(self, url: str) -> requests.Response | None:
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            self._last = time.time()
            if r.status_code == 200:
                return r
        except requests.RequestException:
            return None
        return None
