"""Extract structured fields from a single blog post HTML page."""
from __future__ import annotations

import re
from urllib.parse import urljoin
from dateutil import parser as dateparser
from bs4 import BeautifulSoup, Tag, Comment, NavigableString

from .http import Fetcher


# Common selectors for the main article body, in priority order.
# More specific selectors come first; broad fallbacks last.
CONTENT_SELECTORS = [
    "article .entry-content",
    "article .post-content",
    ".entry-content",
    ".post-content",
    ".article-content",
    ".post-body",
    ".blog-post-content",
    ".the_content_wrapper",          # Betheme / Muffin Builder
    "[itemprop='articleBody']",
    # WordPress universal hooks (these appear on the post wrapper itself):
    "div.hentry",
    "div[id^='post-']",
    "main article",
    "article",
    "#Content",
    "#content",
    "main",
]

# Selectors for WordPress / theme junk inside the article body
JUNK_SELECTORS = [
    ".comments", ".comments-area", ".comment-respond",
    ".share", ".sharedaddy", ".jp-relatedposts", ".social",
    ".social-share", ".share-buttons", ".addtoany_share_save_container",
    ".post-navigation", ".nav-links", ".pagination",
    ".post-meta", ".entry-meta", ".byline", ".author-bio", ".about-author",
    ".breadcrumb", ".breadcrumbs",
    ".wp-block-buttons", ".wp-block-button",
    ".tags-links", ".cat-links", ".post-tags", ".post-categories",
    ".post-header", ".entry-header", ".article-header",
    ".post-title", ".entry-title", ".article-title",
    ".post-thumbnail", ".featured-image", ".post-featured-image",
    ".the_champ_sharing_container",
    "#respond",
]

# Attributes to drop from every tag — they're theme/plugin-specific noise
ATTR_BLACKLIST = {
    "style", "class", "id",
    "loading", "decoding", "sizes", "srcset",
    "width", "height",
    "data-id", "data-src", "data-srcset", "data-sizes", "data-recalc-dims",
    "data-orig-file", "data-large-file", "data-medium-file", "data-image-meta",
    "data-image-title", "data-image-description", "data-image-caption",
    "data-permalink", "data-lazy-src", "data-lazy-srcset", "data-lazy-sizes",
    "rel",
}


def _meta(soup: BeautifulSoup, **attrs) -> str:
    tag = soup.find("meta", attrs=attrs)
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""


GENERIC_H1 = {
    "blog", "blogs", "articles", "article", "news", "posts", "post",
    "home", "homepage", "category",
}


def _extract_title(soup: BeautifulSoup) -> str:
    """Pick the most specific post title available.

    Priority:
      1. og:title (set by SEO plugins; almost always the real post title)
      2. <title> tag, with any " | Site Name" trailing portion stripped
      3. <h1>, unless it looks generic (e.g. just "Blog")
    """
    og = _meta(soup, property="og:title")
    if og:
        return og

    if soup.title and soup.title.string:
        t = soup.title.string.strip()
        # Strip common " | Site Name" / " - Site Name" trailers
        for sep in (" | ", " - ", " — ", " – "):
            if sep in t:
                t = t.split(sep)[0].strip()
                break
        if t:
            return t

    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(strip=True)
        if text and text.lower() not in GENERIC_H1:
            return text

    return ""


def _extract_meta_title(soup: BeautifulSoup) -> str:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return _meta(soup, property="og:title")


def _extract_meta_description(soup: BeautifulSoup) -> str:
    return (
        _meta(soup, attrs={"name": "description"})
        or _meta(soup, property="og:description")
    )


def _extract_permalink(soup: BeautifulSoup, fallback: str) -> str:
    """Resolve the post's public URL.

    Priority:
      1. The actual URL after the browser followed redirects (most reliable).
      2. Canonical link, if present and not an "ugly" ?p= / ?page_id= URL.
      3. og:url, with the same restriction.
      4. Whatever fallback we were given.

    Some WordPress sites have misconfigured canonical/og tags that always
    return the post-ID form even when pretty permalinks are enabled, so the
    resolved URL wins over the meta tags.
    """
    def is_ugly(u: str) -> bool:
        return ("?p=" in u) or ("?page_id=" in u) or ("&p=" in u)

    if fallback and not is_ugly(fallback):
        return fallback.strip()

    link = soup.find("link", rel="canonical")
    if link and link.get("href") and not is_ugly(link["href"]):
        return link["href"].strip()

    og = _meta(soup, property="og:url")
    if og and not is_ugly(og):
        return og.strip()

    return fallback.strip() if fallback else ""


def _extract_published(soup: BeautifulSoup) -> str:
    """Return a single string like 'July 21, 2025 4:19 PM' (or '' if unknown)."""
    candidates = [
        _meta(soup, property="article:published_time"),
        _meta(soup, attrs={"name": "article:published_time"}),
        _meta(soup, attrs={"itemprop": "datePublished"}),
    ]
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        candidates.append(time_tag.get("datetime", ""))
    if time_tag and time_tag.get_text(strip=True):
        candidates.append(time_tag.get_text(strip=True))

    for raw in candidates:
        if not raw:
            continue
        try:
            dt = dateparser.parse(raw)
            date_part = f"{dt.strftime('%B')} {dt.day}, {dt.year}"
            time_part = dt.strftime("%I:%M %p").lstrip("0")
            return f"{date_part} {time_part}"
        except (ValueError, TypeError, OverflowError):
            continue
    return ""


def _extract_terms(soup: BeautifulSoup, kind: str) -> str:
    """kind = 'category' or 'tag'.

    Disambiguates the WordPress quirk where category links use
    rel='category tag' by checking the URL path first: /category/ wins
    over rel='tag', and /tag/ is unambiguous.
    """
    names: list[str] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if not text:
            continue
        href = a.get("href", "")
        rel_tokens = a.get("rel", []) or []

        # Path-based detection (authoritative)
        if "/category/" in href:
            url_kind = "category"
        elif "/tag/" in href:
            url_kind = "tag"
        elif "category" in rel_tokens:
            url_kind = "category"
        elif "tag" in rel_tokens and "category" not in rel_tokens:
            url_kind = "tag"
        else:
            continue

        if url_kind == kind and text not in seen:
            seen.add(text); names.append(text)

    # Fallback selectors if nothing found
    if not names:
        cls = "post-categories" if kind == "category" else "post-tags"
        for el in soup.select(f".{cls} a, .entry-{kind}s a, .{kind}s a"):
            text = el.get_text(strip=True)
            if text and text not in seen:
                seen.add(text); names.append(text)

    return ", ".join(names)


def _extract_featured_image(soup: BeautifulSoup, base_url: str) -> str:
    og = _meta(soup, property="og:image")
    if og:
        return urljoin(base_url, og)
    twitter = _meta(soup, attrs={"name": "twitter:image"})
    if twitter:
        return urljoin(base_url, twitter)
    # Look inside the article for a likely hero image
    article = soup.find("article") or soup
    img = article.find("img")
    if img and img.get("src"):
        return urljoin(base_url, img["src"])
    return ""


def _clean_article(el: Tag, base_url: str) -> None:
    """Aggressively strip WordPress/theme junk from an article element."""
    # 1. Remove non-content tags entirely
    for tag in el.find_all(["script", "style", "noscript", "form", "iframe", "svg", "button"]):
        tag.decompose()

    # 2. Remove HTML comments (often Yoast / WP block markers)
    for c in el.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()

    # 3. Remove known junk regions (related posts, share buttons, etc.)
    for sel in JUNK_SELECTORS:
        for j in el.select(sel):
            j.decompose()

    # 4. Resolve relative URLs and recover lazy-loaded image src BEFORE we
    #    strip data-* attributes.
    for img in el.find_all("img"):
        for attr in ("data-lazy-src", "data-src", "data-original"):
            if (not img.get("src") or "data:image" in img.get("src", "")) and img.get(attr):
                img["src"] = img[attr]
                break
        if img.get("src"):
            img["src"] = urljoin(base_url, img["src"])
        # Drop pure tracking pixels
        if img.get("src", "").endswith(("1x1.gif", "spacer.gif")):
            img.decompose()
    for a in el.find_all("a", href=True):
        a["href"] = urljoin(base_url, a["href"])

    # 5. Strip noise attributes from every tag
    for tag in el.find_all(True):
        for attr in list(tag.attrs.keys()):
            if attr in ATTR_BLACKLIST or attr.startswith("data-") or attr.startswith("aria-"):
                del tag.attrs[attr]

    # 6. Unwrap empty / pointless wrappers (div, span with no attributes left)
    for tag in el.find_all(["div", "span", "section", "figure"]):
        if not tag.attrs and not tag.find(True) and not (tag.string or "").strip():
            tag.decompose()
            continue
        # Unwrap plain spans entirely — they almost always wrap text for styling
        if tag.name == "span" and not tag.attrs:
            tag.unwrap()

    # 7. Drop empty paragraphs / headings
    for tag in el.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li"]):
        if not tag.find(True) and not (tag.get_text(strip=True)):
            tag.decompose()


def _extract_content(soup: BeautifulSoup, base_url: str) -> str:
    """Return cleaned HTML of the post body.

    Tries CONTENT_SELECTORS in order and accepts the first match that yields
    at least ~150 characters of visible text after cleanup. This avoids
    selecting an empty wrapper while still being permissive about themes.
    """
    best_html = ""
    best_text_len = 0
    for sel in CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if not el or not isinstance(el, Tag):
            continue
        # Operate on a fresh copy so we don't damage `soup` for other extractors
        clone = BeautifulSoup(str(el), "lxml").find()
        if not clone:
            continue
        _clean_article(clone, base_url)
        text_len = len(clone.get_text(strip=True))
        if text_len < 150:
            # Probably an empty wrapper or breadcrumbs-only div
            if text_len > best_text_len:
                best_text_len = text_len
                best_html = clone.decode_contents().strip()
            continue
        html = clone.decode_contents().strip()
        html = re.sub(r"\n\s*\n+", "\n", html)
        html = re.sub(r"[ \t]{2,}", " ", html)
        return html
    # No selector hit the threshold — return the best we found (may be empty)
    return best_html


def extract_post(url: str, fetcher: Fetcher) -> dict | None:
    """Fetch a post URL and return a dict of extracted fields, or None on failure."""
    r = fetcher.get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "lxml")

    return {
        "Title": _extract_title(soup),
        "Permalink": _extract_permalink(soup, fallback=str(r.url)),
        "blog_dates": _extract_published(soup),
        "blog_Category": _extract_terms(soup, "category"),
        "blog_Tag": _extract_terms(soup, "tag"),
        "blog_featured_image": _extract_featured_image(soup, str(r.url)),
        "blog_content": _extract_content(soup, str(r.url)),
        "blog_metadesc": _extract_meta_description(soup),
        "blog_metatitle": _extract_meta_title(soup),
    }
