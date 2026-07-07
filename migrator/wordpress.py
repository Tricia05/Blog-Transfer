"""Thin WordPress REST API client for post migration."""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin

import requests
from requests.auth import HTTPBasicAuth


class WordPressError(RuntimeError):
    pass


class WordPressClient:
    def __init__(
        self,
        site_url: str,
        username: str,
        app_password: str,
        timeout: int = 30,
        request_delay: float = 0.4,
    ) -> None:
        # Strip common path suffixes users sometimes paste in by mistake
        url = site_url.strip().rstrip("/")
        for junk in (
            "/wp-login.php", "/wp-admin", "/wp-admin/",
            "/wp-json", "/wp-json/wp/v2", "/wp-json/wp/v2/",
        ):
            if url.endswith(junk):
                url = url[: -len(junk)].rstrip("/")
        self.base = url + "/wp-json/wp/v2/"
        self.auth = HTTPBasicAuth(username, app_password.replace(" ", ""))
        self.timeout = timeout
        self.request_delay = request_delay
        self._cat_cache: dict[str, int] = {}
        self._tag_cache: dict[str, int] = {}

    # ---- core HTTP ----------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = urljoin(self.base, path.lstrip("/"))
        last_err = None
        for attempt in range(3):
            try:
                r = requests.request(
                    method, url, auth=self.auth, timeout=self.timeout, **kwargs
                )
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if not r.ok:
                    raise WordPressError(
                        f"{method} {url} -> {r.status_code}: {r.text[:300]}"
                    )
                time.sleep(self.request_delay)
                return r.json() if r.content else None
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
                time.sleep(2 ** attempt)
        raise WordPressError(f"Network failure after retries: {last_err}")

    # ---- auth check ---------------------------------------------------

    def verify(self) -> dict:
        return self._request("GET", "users/me")

    # ---- terms (categories & tags) ------------------------------------

    def _get_or_create_term(self, taxonomy: str, name: str, cache: dict) -> int:
        if name in cache:
            return cache[name]
        existing = self._request("GET", f"{taxonomy}?search={name}&per_page=100")
        for term in existing or []:
            if term["name"].lower() == name.lower():
                cache[name] = term["id"]
                return term["id"]
        created = self._request("POST", taxonomy, json={"name": name})
        cache[name] = created["id"]
        return created["id"]

    def resolve_categories(self, names: list[str]) -> list[int]:
        return [self._get_or_create_term("categories", n, self._cat_cache) for n in names]

    def resolve_tags(self, names: list[str]) -> list[int]:
        return [self._get_or_create_term("tags", n, self._tag_cache) for n in names]

    # ---- media --------------------------------------------------------

    def sideload_image(self, image_url: str) -> tuple[int | None, str]:
        """Download an image from a URL and upload it to WP media.

        Returns (media_id, message). On success message is empty; on failure
        media_id is None and message describes why.
        """
        ua = {"User-Agent": "Mozilla/5.0 BlogTransfer/1.0"}
        try:
            resp = requests.get(image_url, timeout=self.timeout, headers=ua)
            resp.raise_for_status()
            content = resp.content
            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
        except requests.RequestException as e:
            return None, f"download failed: {e}"

        filename = image_url.rsplit("/", 1)[-1].split("?")[0] or "image.jpg"
        url = urljoin(self.base, "media")
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": content_type,
        }
        try:
            r = requests.post(
                url, auth=self.auth, headers=headers, data=content,
                timeout=self.timeout,
            )
            if not r.ok:
                return None, f"upload {r.status_code}: {r.text[:160]}"
            time.sleep(self.request_delay)
            return r.json().get("id"), ""
        except requests.RequestException as e:
            return None, f"upload failed: {e}"

    # ---- posts --------------------------------------------------------

    def find_existing_post(self, slug: str) -> dict | None:
        results = self._request("GET", f"posts?slug={slug}&status=any")
        return results[0] if results else None

    def create_post(self, payload: dict) -> dict:
        return self._request("POST", "posts", json=payload)

    def update_post(self, post_id: int, payload: dict) -> dict:
        return self._request("POST", f"posts/{post_id}", json=payload)

    # ---- high-level migration -----------------------------------------

    def sideload_content_images(self, html: str) -> tuple[str, int, int]:
        """Find every <img src="..."> in HTML, upload each to WP media, and
        rewrite the src to the hosted URL.

        Returns (rewritten_html, count_ok, count_failed).
        """
        if not html or "<img" not in html:
            return html, 0, 0
        from bs4 import BeautifulSoup  # local import to keep top of file clean
        soup = BeautifulSoup(html, "html.parser")
        ok = fail = 0
        seen: dict[str, str] = {}   # source_url -> new_url (avoid re-uploading)
        for img in soup.find_all("img"):
            src = (img.get("src") or "").strip()
            if not src or src.startswith("data:"):
                continue
            if src in seen:
                img["src"] = seen[src]
                continue
            media_id, err = self.sideload_image(src)
            if not media_id:
                fail += 1
                continue
            # Fetch the new URL for the uploaded media
            try:
                media = self._request("GET", f"media/{media_id}")
                new_url = media.get("source_url", "")
            except WordPressError:
                new_url = ""
            if new_url:
                img["src"] = new_url
                seen[src] = new_url
                ok += 1
            else:
                fail += 1
        return str(soup), ok, fail

    def upload_post(self, payload: dict, on_duplicate: str = "skip") -> dict:
        """Upload one mapped payload.

        on_duplicate: 'skip' | 'update' | 'create' (default 'skip')
        Returns dict with action + WP response (plus diagnostic keys).
        """
        cats = payload.pop("_categories", [])
        tags = payload.pop("_tags", [])
        featured_url = payload.pop("_featured_image_url", None)
        sideload_content = payload.pop("_sideload_content_images", True)

        if cats:
            payload["categories"] = self.resolve_categories(cats)
        if tags:
            payload["tags"] = self.resolve_tags(tags)

        # Sideload inline images inside the content so nothing is hotlinked
        content_note = ""
        if sideload_content and payload.get("content"):
            new_html, ok, fail = self.sideload_content_images(payload["content"])
            payload["content"] = new_html
            if ok or fail:
                content_note = f"content images: {ok} ok, {fail} failed"

        featured_note = ""
        if featured_url:
            media_id, err = self.sideload_image(featured_url)
            if media_id:
                payload["featured_media"] = media_id
            else:
                featured_note = f"featured image skipped ({err})"

        # Combine both notes into the returned note
        note_bits = [n for n in (featured_note, content_note) if n]
        featured_note = " | ".join(note_bits)

        slug = payload.get("slug")
        if slug:
            existing = self.find_existing_post(slug)
            if existing:
                if on_duplicate == "skip":
                    return {"action": "skipped", "id": existing["id"], "slug": slug, "note": featured_note}
                if on_duplicate == "update":
                    updated = self.update_post(existing["id"], payload)
                    return {"action": "updated", "id": updated["id"], "slug": slug, "note": featured_note}

        created = self.create_post(payload)
        return {"action": "created", "id": created["id"], "slug": created.get("slug"), "note": featured_note}
