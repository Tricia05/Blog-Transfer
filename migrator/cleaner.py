"""Data cleaning and normalization."""
from __future__ import annotations

import html
import re
from datetime import datetime, date
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup
from dateutil import parser as dateparser


STATUS_MAP = {
    "publish": "publish", "published": "publish", "live": "publish",
    "public": "publish", "yes": "publish", "y": "publish", "1": "publish",
    "true": "publish", "active": "publish",
    "draft": "draft", "drafts": "draft", "unpublished": "draft",
    "no": "draft", "n": "draft", "0": "draft", "false": "draft",
    "pending": "pending", "review": "pending",
    "private": "private",
    "future": "future", "scheduled": "future",
    "trash": "trash", "deleted": "trash",
}

SMART_QUOTES = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "--", "…": "...", " ": " ",
}


def clean_text(value: Any) -> str | None:
    """Strip whitespace, decode HTML entities, normalize smart quotes."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if not s:
        return None
    s = html.unescape(s)
    for bad, good in SMART_QUOTES.items():
        s = s.replace(bad, good)
    return s


def clean_date(value: Any) -> str | None:
    """Parse a messy date value into ISO 8601 (YYYY-MM-DDTHH:MM:SS)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None

    if isinstance(value, (datetime, pd.Timestamp)):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).strftime("%Y-%m-%dT%H:%M:%S")

    if isinstance(value, (int, float)):
        # Excel serial date (days since 1899-12-30)
        try:
            ts = pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(value))
            return ts.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None

    s = str(value).strip()
    if not s:
        return None
    try:
        dt = dateparser.parse(s, dayfirst=False, fuzzy=True)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError, OverflowError):
        return None


def clean_status(value: Any, default: str = "draft") -> str:
    s = clean_text(value)
    if not s:
        return default
    return STATUS_MAP.get(s.lower(), default)


def clean_slug(value: Any) -> str | None:
    s = clean_text(value)
    if not s:
        return None
    s = s.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return s or None


def clean_list(value: Any, separator: str = ",") -> list[str]:
    """Split a delimited string into a clean list."""
    s = clean_text(value)
    if not s:
        return []
    return [p.strip() for p in s.split(separator) if p.strip()]


def text_to_html(value: Any) -> str | None:
    """Convert plain text with line breaks into basic HTML paragraphs.

    If the input already contains HTML tags, return as-is (just cleaned).
    """
    s = clean_text(value)
    if not s:
        return None
    if re.search(r"<\w+[^>]*>", s):
        return s
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", s) if p.strip()]
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)


def clean_blog_content(value: Any, drop_trailing: list[str] | None = None) -> str | None:
    """Clean scraped WordPress HTML.

    - Trims wrapper whitespace and tabs from scraping
    - Normalizes smart quotes
    - Drops trailing plain-text junk (metadesc/metatitle accidentally
      concatenated by a scraper) by re-rendering only block-level HTML
      and discarding loose text after the final closing tag.
    """
    s = clean_text(value)
    if not s:
        return None

    soup = BeautifulSoup(s, "html.parser")
    parts: list[str] = []
    for node in soup.contents:
        name = getattr(node, "name", None)
        if name:  # any HTML tag
            parts.append(str(node))
        # ignore bare NavigableString (scraper junk text)

    cleaned = "\n".join(p.strip() for p in parts if p.strip())

    if drop_trailing:
        for tail in drop_trailing:
            tail = (tail or "").strip()
            if tail and cleaned.rstrip().endswith(tail):
                cleaned = cleaned.rstrip()[: -len(tail)].rstrip()

    return cleaned or None
