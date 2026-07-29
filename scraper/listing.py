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


# Nested sitemaps we should NOT follow — they hold pages, taxonomy archives,
# media, and other non-post content that would pollute the results.
NON_POST_SITEMAP = re.compile(
    r"(page|categor|tag|author|product|attachment|media|"
    r"local|team|portfolio|project|service)[-_]?sitemap",
    re.I,
)
# Nested sitemaps that clearly hold posts.
POST_SITEMAP = re.compile(r"(post|article|blog)[-_]?sitemap|sitemap[-_]?post", re.I)


def from_sitemap(base_url: str, fetcher: Fetcher) -> list[str]:
    """Collect post URLs from the site's sitemap(s).

    Only follows nested sitemaps that look like POST sitemaps; skips
    page/category/tag/author/media sitemaps so service pages and archive
    pages don't get scraped as if they were blog posts.
    """
    found: set[str] = set()
    base_root = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"

    queue = [urljoin(base_root, p) for p in SITEMAP_CANDIDATES]
    seen_sitemaps: set[str] = set()
    followed_child = False  # did we follow at least one post-specific child?

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

        children = [s.text for s in root.findall(f"{ns}sitemap/{ns}loc") if s.text]
        if children:
            # This is a sitemap index. Follow post sitemaps; skip the rest.
            for child in children:
                if NON_POST_SITEMAP.search(child):
                    continue
                if POST_SITEMAP.search(child):
                    queue.append(child)
                    followed_child = True
            # If nothing matched a post sitemap, fall back to following any
            # child that isn't an obvious non-post one (best effort).
            if not followed_child:
                for child in children:
                    if not NON_POST_SITEMAP.search(child):
                        queue.append(child)

        # URL set (actual post URLs)
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


def _apply_prefix(posts: list[str], parsed) -> list[str]:
    """Keep only URLs under the listing URL's path prefix (e.g. /blog/)."""
    prefix = parsed.path.rstrip("/")
    if not prefix:
        return posts
    prefix_full = f"{parsed.scheme}://{parsed.netloc}{prefix}/".rstrip("/")
    return [p for p in posts if p.startswith(prefix_full)]


def discover_posts(start_url: str, limit: int | None, fetcher: Fetcher) -> list[str]:
    """Discover post URLs starting from a blog or site URL.

    Strategy: try the sitemap first (fast + comprehensive), fall back to
    crawling the listing page and its pagination.

    The listing URL's path prefix (e.g. '/blog/') is used as a *soft* filter:
    it's applied only if it still leaves posts. Many WordPress sites list
    posts at '/blog/' but give the posts themselves root-level permalinks
    ('/post-name/'), so a hard prefix filter would wrongly discard everything.
    """
    parsed = urlparse(start_url)
    base_root = _normalize(f"{parsed.scheme}://{parsed.netloc}")
    listing_root = _normalize(start_url)

    def cleanup(urls: list[str]) -> list[str]:
        out = []
        for p in urls:
            if NON_POST_HINTS.search(p):
                continue
            if re.search(r"/\d{4}(/\d{2})?/?$", p):      # date archives /2024/ /2024/07/
                continue
            if p in (base_root, listing_root):            # site root / listing root
                continue
            if re.search(r"\.(php|html?|aspx)$", p, re.I): # static pages
                continue
            out.append(p)
        return out

    # 1. Sitemap (fast + comprehensive)
    posts = cleanup(from_sitemap(start_url, fetcher))

    # 2. Fall back to crawling the listing page if the sitemap had nothing
    if not posts:
        max_pages = 500 if not limit else max(20, (limit // 5) + 5)
        posts = cleanup(from_listing(start_url, fetcher, max_pages=max_pages))

    # 3. Soft path-prefix filter: apply only if it still leaves posts.
    #    (Many sites list at /blog/ but give posts root-level permalinks, so a
    #    hard filter would wrongly discard everything.)
    filtered = _apply_prefix(posts, parsed)
    if filtered:
        posts = filtered

    posts = sorted(set(posts))
    if limit:
        posts = posts[:limit]
    return posts
