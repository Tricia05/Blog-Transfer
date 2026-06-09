"""On-disk storage for scan history and exports."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pandas as pd


HISTORY_DIR = Path("data/history")
EXPORTS_DIR = Path("data/exports")


def _ensure_dirs() -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# History (one record per scan)
# ---------------------------------------------------------------------------
@dataclass
class HistoryEntry:
    id: str
    url: str
    scanned_at: str           # ISO timestamp
    post_count: int
    statuses: list[str]
    csv_path: str             # relative path to saved CSV

    @property
    def display_time(self) -> str:
        try:
            dt = datetime.fromisoformat(self.scanned_at)
            return dt.strftime("%b %d, %Y  %I:%M %p").replace(" 0", " ")
        except ValueError:
            return self.scanned_at


def save_history(url: str, df: pd.DataFrame, statuses: list[str]) -> HistoryEntry:
    """Persist a scan: write CSV + JSON metadata. Returns the entry."""
    _ensure_dirs()
    eid = uuid4().hex[:10]
    csv_path = HISTORY_DIR / f"{eid}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    entry = HistoryEntry(
        id=eid,
        url=url,
        scanned_at=datetime.now().isoformat(timespec="seconds"),
        post_count=len(df),
        statuses=list(statuses),
        csv_path=str(csv_path),
    )
    with open(HISTORY_DIR / f"{eid}.json", "w", encoding="utf-8") as f:
        json.dump(asdict(entry), f, indent=2)
    return entry


def list_history() -> list[HistoryEntry]:
    _ensure_dirs()
    entries: list[HistoryEntry] = []
    for j in HISTORY_DIR.glob("*.json"):
        try:
            with open(j, "r", encoding="utf-8") as f:
                entries.append(HistoryEntry(**json.load(f)))
        except (json.JSONDecodeError, TypeError):
            continue
    entries.sort(key=lambda e: e.scanned_at, reverse=True)
    return entries


def load_history(entry_id: str) -> pd.DataFrame | None:
    j = HISTORY_DIR / f"{entry_id}.json"
    if not j.exists():
        return None
    with open(j, "r", encoding="utf-8") as f:
        meta = json.load(f)
    csv = Path(meta["csv_path"])
    if not csv.exists():
        return None
    return pd.read_csv(csv, dtype=object).fillna("")


def delete_history(entry_id: str) -> bool:
    j = HISTORY_DIR / f"{entry_id}.json"
    c = HISTORY_DIR / f"{entry_id}.csv"
    ok = False
    if j.exists():
        j.unlink(); ok = True
    if c.exists():
        c.unlink()
    return ok


# ---------------------------------------------------------------------------
# Exports (files the user has downloaded)
# ---------------------------------------------------------------------------
@dataclass
class ExportEntry:
    filename: str
    size_bytes: int
    saved_at: str
    fmt: str  # "csv" | "xlsx"

    @property
    def size_kb(self) -> str:
        return f"{self.size_bytes / 1024:.1f} KB"

    @property
    def display_time(self) -> str:
        try:
            dt = datetime.fromisoformat(self.saved_at)
            return dt.strftime("%b %d, %Y  %I:%M %p").replace(" 0", " ")
        except ValueError:
            return self.saved_at


def save_export(name_hint: str, content: bytes, fmt: str) -> ExportEntry:
    """Save an exported file to disk and return its entry."""
    _ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{ts}_{name_hint}.{fmt}"
    path = EXPORTS_DIR / base
    path.write_bytes(content)
    return ExportEntry(
        filename=base,
        size_bytes=len(content),
        saved_at=datetime.now().isoformat(timespec="seconds"),
        fmt=fmt,
    )


def list_exports() -> list[ExportEntry]:
    _ensure_dirs()
    entries: list[ExportEntry] = []
    for f in EXPORTS_DIR.iterdir():
        if not f.is_file() or f.suffix.lstrip(".") not in ("csv", "xlsx"):
            continue
        stat = f.stat()
        entries.append(ExportEntry(
            filename=f.name,
            size_bytes=stat.st_size,
            saved_at=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            fmt=f.suffix.lstrip("."),
        ))
    entries.sort(key=lambda e: e.saved_at, reverse=True)
    return entries


def export_path(filename: str) -> Path:
    return EXPORTS_DIR / filename


def delete_export(filename: str) -> bool:
    p = EXPORTS_DIR / filename
    if p.exists():
        p.unlink()
        return True
    return False
