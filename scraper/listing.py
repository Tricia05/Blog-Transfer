"""Discover individual blog post URLs from a website / listing URL."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, urldefrag
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from .http import Fetcher


SITEMAP_CANDIDATES = [
    "/post-sitemap.xml",
    "/wp-sitemap-posts-post-1.xml",
    "/sitemap_index.xml",
    "/sitemap.xml",
]

ARTICLE_HINTS = re.compile(
    r"(article|post|entry|blog-post|hentry)", re.I,
)
NON_POST_HINTS = re.compile(
    r"(/category/|/categories/|/tag/|/tags/|/author/|/page/|/feed|"
    r"/wp-content/|/wp-admin/|/wp-includes/|/comments/|#)",
    re.I,
)


def _same_host(base: str, url: str) -> bool:
    return urlparse(base).netloc == urlparse(url).netloc


def _normalize(url: str) -> str:
    url, _ = urldefrag(url)
    return url.rstrip("/")


def from_sitemap(base_url: str, fetcher: Fetcher) -> list[str]:
    """Try common WordPress sitemap locations and collect post URLs."""
    found: set[str] = set()
    base_root = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"

    queue = [urljoin(base_root, p) for p in SITEMAP_CANDIDATES]
    seen_sitemaps: set[str] = set()

    while queue:
        sm = queue.pop(0)
        if sm in seen_sitemaps:
            continue
        seen_sitemaps.add(sm)
        r = fetcher.get(sm)
        if not r:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        # Sitemap index -> nested sitemaps
        for s in root.findall(f"{ns}sitemap/{ns}loc"):
            if s.text and "post" in s.text.lower():
                queue.append(s.text)
            elif s.text and "sitemap" in s.text.lower():
                queue.append(s.text)
        # URL set
        for u in root.findall(f"{ns}url/{ns}loc"):
            if u.text and _same_host(base_url, u.text):
                found.add(_normalize(u.text))

    return sorted(found)


def from_listing(base_url: str, fetcher: Fetcher, max_pages: int = 500) -> list[str]:
    """Walk a blog listing page and its pagination, collecting article links."""
    found: set[str] = set()
    visited: set[str] = set()
    queue = [base_url]

    while queue and len(visited) < max_pages:
        page = queue.pop(0)
        if page in visited:
            continue
        visited.add(page)
        r = fetcher.get(page)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "lxml")

        # 1) <article> tags + their first link
        for art in soup.find_all("article"):
            a = art.find("a", href=True)
            if a:
                u = urljoin(page, a["href"])
                if _same_host(base_url, u) and not NON_POST_HINTS.search(u):
                    found.add(_normalize(u))

        # 2) class-based hints
        for el in soup.select('[class*="post"] a[href], [class*="entry"] a[href]'):
            u = urljoin(page, el["href"])
            if _same_host(base_url, u) and not NON_POST_HINTS.search(u):
                if ARTICLE_HINTS.search(" ".join(el.get("class", []))) or el.find("h1") or el.find("h2"):
                    found.add(_normalize(u))

        # 3) Pagination: follow rel=next or "/page/N" links on same listing
        for a in soup.find_all("a", href=True):
            href = urljoin(page, a["href"])
            if not _same_host(base_url, href):
                continue
            if a.get("rel") and "next" in a.get("rel"):
                queue.append(href)
            elif re.search(r"/page/\d+/?$", href):
                queue.append(href)

    return sorted(found)


def discover_posts(start_url: str, limit: int | None, fetcher: Fetcher) -> list[str]:
    """Discover post URLs starting from a blog or site URL.

    Strategy: try sitemap first (fast + comprehensive), fall back to crawling
    the listing page and its pagination. If the input URL has a path prefix
    (e.g. '/blog/'), only keep URLs under that prefix.
    """
    posts = from_sitemap(start_url, fetcher)

    # If the user pointed at a sub-path like /blog/, restrict to that prefix
    parsed = urlparse(start_url)
    prefix = parsed.path.rstrip("/")
    if prefix and prefix != "":
        prefix_full = f"{parsed.scheme}://{parsed.netloc}{prefix}/"
        posts = [p for p in posts if p.startswith(prefix_full.rstrip("/"))]

    # Fall back to crawling the listing page if sitemap had nothing useful.
    # Estimate enough pagination to cover the requested limit (most blogs
    # show ~10 posts per listing page; we add headroom).
    if not posts:
        max_pages = 500 if not limit else max(20, (limit // 5) + 5)
        posts = from_listing(start_url, fetcher, max_pages=max_pages)
        if prefix:
            prefix_full = f"{parsed.scheme}://{parsed.netloc}{prefix}/"
            posts = [p for p in posts if p.startswith(prefix_full.rstrip("/"))]

    # Filter out obvious non-post URLs
    posts = [p for p in posts if not NON_POST_HINTS.search(p)]

    # Drop the bare site root and listing root
    base_root = _normalize(f"{parsed.scheme}://{parsed.netloc}")
    listing_root = _normalize(start_url)
    posts = [p for p in posts if p not in (base_root, listing_root)]

    # Drop static page extensions (.php/.html/.aspx) — likely site pages, not posts
    posts = [p for p in posts if not re.search(r"\.(php|html?|aspx)$", p, re.I)]

    if limit:
        posts = posts[:limit]
    return posts
