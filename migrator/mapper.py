"""Map cleaned Excel rows -> WordPress post payloads."""
from __future__ import annotations

import re
from typing import Any
import pandas as pd

from . import cleaner


def map_row(row: pd.Series, config: dict) -> dict[str, Any]:
    """Apply column_map + cleaning rules to one row.

    Returns a dict with WP-shaped fields plus _categories/_tags as name lists
    (resolved to IDs later by the WordPress client).
    """
    cmap = config.get("column_map", {})
    defaults = config.get("defaults", {})
    tag_sep = config.get("tag_separator", ",")
    cat_sep = config.get("category_separator", ",")

    def get(field: str) -> Any:
        col = cmap.get(field)
        if col is None or col not in row.index:
            return None
        return row[col]

    payload: dict[str, Any] = {}

    title = cleaner.clean_text(get("title"))
    if title:
        payload["title"] = title

    excerpt = cleaner.clean_text(get("excerpt"))
    if excerpt:
        payload["excerpt"] = excerpt

    raw_content = get("content")
    if raw_content and re.search(r"<\w+[^>]*>", str(raw_content)):
        content = cleaner.clean_blog_content(
            raw_content, drop_trailing=[excerpt or "", title or ""]
        )
    else:
        content = cleaner.text_to_html(raw_content)
    if content:
        payload["content"] = content

    iso = cleaner.clean_date(get("date"))
    if iso:
        payload["date"] = iso

    payload["status"] = cleaner.clean_status(
        get("status"), default=defaults.get("status", "draft")
    )

    slug = cleaner.clean_slug(get("slug")) or cleaner.clean_slug(title)
    if slug:
        payload["slug"] = slug

    if "author_id" in defaults:
        payload["author"] = defaults["author_id"]

    payload["_categories"] = cleaner.clean_list(get("categories"), cat_sep)
    payload["_tags"] = cleaner.clean_list(get("tags"), tag_sep)

    featured = cleaner.clean_text(get("featured_image"))
    if featured:
        payload["_featured_image_url"] = featured

    return payload


def validate(payload: dict) -> list[str]:
    """Return a list of problems with this payload (empty = OK)."""
    problems = []
    if not payload.get("title"):
        problems.append("missing title")
    if not payload.get("content"):
        problems.append("missing content")
    return problems
