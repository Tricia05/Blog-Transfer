"""Excel -> DataFrame loader."""
from __future__ import annotations

from pathlib import Path
import pandas as pd


def load_excel(path: str | Path, sheet: str | int = 0) -> pd.DataFrame:
    """Load an Excel file into a DataFrame.

    Reads all columns as object dtype so messy values (mixed dates, numeric
    strings, etc.) survive intact for the cleaning pass.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Excel file not found: {path}")

    df = pd.read_excel(path, sheet_name=sheet, dtype=object, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all").reset_index(drop=True)
    return df


def describe(df: pd.DataFrame) -> dict:
    """Return a quick summary of the loaded data."""
    return {
        "rows": len(df),
        "columns": list(df.columns),
        "non_null_counts": {c: int(df[c].notna().sum()) for c in df.columns},
    }
