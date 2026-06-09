"""CLI entry point for the blog migration tool.

Usage:
    python migrate.py inspect posts.xlsx
    python migrate.py run posts.xlsx --config mapping.yaml --dry-run
    python migrate.py run posts.xlsx --config mapping.yaml
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import click
import pandas as pd
import yaml
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from migrator import loader, mapper
from migrator.wordpress import WordPressClient, WordPressError

console = Console()


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@click.group()
def cli():
    """Excel -> WordPress blog migration tool."""


@cli.command()
@click.argument("excel_file", type=click.Path(exists=True))
@click.option("--sheet", default=0, help="Sheet name or index (default: 0)")
def inspect(excel_file, sheet):
    """Show column names, row count, and sample data from an Excel file."""
    df = loader.load_excel(excel_file, sheet=sheet)
    info = loader.describe(df)

    console.print(f"[bold green]Loaded[/] {info['rows']} rows from {excel_file}")
    table = Table(title="Columns")
    table.add_column("Column")
    table.add_column("Non-null", justify="right")
    for col, n in info["non_null_counts"].items():
        table.add_row(col, str(n))
    console.print(table)

    console.print("\n[bold]First 3 rows:[/]")
    console.print(df.head(3).to_string())


@cli.command()
@click.argument("excel_file", type=click.Path(exists=True))
@click.option("--config", "-c", required=True, type=click.Path(exists=True))
@click.option("--sheet", default=0, help="Sheet name or index")
@click.option(
    "--output", "-o", default="output/cleaned_preview.xlsx",
    type=click.Path(), help="Where to write the cleaned Excel file",
)
def preview(excel_file, config, sheet, output):
    """Run cleaning + mapping and write the result to a new Excel file for review."""
    cfg = load_config(config)
    df = loader.load_excel(excel_file, sheet=sheet)

    rows = []
    for i, row in df.iterrows():
        p = mapper.map_row(row, cfg)
        problems = mapper.validate(p)
        rows.append({
            "row": i,
            "valid": "yes" if not problems else "NO",
            "problems": "; ".join(problems),
            "title": p.get("title", ""),
            "slug": p.get("slug", ""),
            "date": p.get("date", ""),
            "status": p.get("status", ""),
            "categories": ", ".join(p.get("_categories", [])),
            "tags": ", ".join(p.get("_tags", [])),
            "featured_image_url": p.get("_featured_image_url", ""),
            "excerpt": p.get("excerpt", ""),
            "content_chars": len(p.get("content", "") or ""),
            "content_preview": (p.get("content", "") or "")[:300],
        })

    out_df = pd.DataFrame(rows)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    out_df.to_excel(output, index=False, engine="openpyxl")
    console.print(f"[green]Wrote[/] {output}  ({len(out_df)} rows)")


@cli.command()
@click.argument("excel_file", type=click.Path(exists=True))
@click.option("--config", "-c", required=True, type=click.Path(exists=True))
@click.option("--sheet", default=0, help="Sheet name or index")
@click.option("--dry-run", is_flag=True, help="Preview only; do not upload")
@click.option("--limit", type=int, default=None, help="Process only first N rows")
@click.option(
    "--on-duplicate",
    type=click.Choice(["skip", "update", "create"]),
    default="skip",
)
@click.option("--output-dir", default="output", type=click.Path())
def run(excel_file, config, sheet, dry_run, limit, on_duplicate, output_dir):
    """Run the full migration pipeline."""
    cfg = load_config(config)
    output = Path(output_dir)
    output.mkdir(exist_ok=True)

    df = loader.load_excel(excel_file, sheet=sheet)
    if limit:
        df = df.head(limit)
    console.print(f"[bold]Loaded[/] {len(df)} rows")

    # ---- Map all rows ----
    mapped = []
    invalid = []
    for i, row in df.iterrows():
        payload = mapper.map_row(row, cfg)
        problems = mapper.validate(payload)
        if problems:
            invalid.append({"row": i, "problems": "; ".join(problems), "title": payload.get("title", "")})
        else:
            mapped.append((i, payload))

    console.print(f"[green]Valid:[/] {len(mapped)}   [red]Invalid:[/] {len(invalid)}")
    if invalid:
        with open(output / "invalid_rows.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["row", "title", "problems"])
            w.writeheader()
            w.writerows(invalid)
        console.print(f"  Wrote {output / 'invalid_rows.csv'}")

    # ---- Dry-run preview ----
    if dry_run:
        preview = output / "dry_run_preview.json"
        with open(preview, "w", encoding="utf-8") as f:
            json.dump([p for _, p in mapped], f, indent=2, ensure_ascii=False)
        console.print(f"[yellow]Dry run[/] - wrote {preview}")
        if mapped:
            console.print("\n[bold]Sample payload (first row):[/]")
            console.print(json.dumps(mapped[0][1], indent=2, ensure_ascii=False))
        return

    # ---- Upload ----
    try:
        client = WordPressClient(
            site_url=cfg["wordpress_url"],
            username=cfg["username"],
            app_password=cfg["app_password"],
        )
        me = client.verify()
        console.print(f"[green]Authenticated as[/] {me.get('name')} (id={me.get('id')})")
    except (WordPressError, KeyError) as e:
        console.print(f"[red]Auth failed:[/] {e}")
        sys.exit(1)

    report_path = output / "migration_report.csv"
    error_path = output / "errors.csv"
    with open(report_path, "w", newline="", encoding="utf-8") as rf, \
         open(error_path, "w", newline="", encoding="utf-8") as ef:
        report = csv.DictWriter(rf, fieldnames=["row", "action", "id", "slug", "title"])
        errors = csv.DictWriter(ef, fieldnames=["row", "title", "error"])
        report.writeheader()
        errors.writeheader()

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Uploading", total=len(mapped))
            for idx, payload in mapped:
                title = payload.get("title", "")
                try:
                    result = client.upload_post(payload, on_duplicate=on_duplicate)
                    report.writerow({
                        "row": idx, "action": result["action"],
                        "id": result.get("id"), "slug": result.get("slug"),
                        "title": title,
                    })
                except WordPressError as e:
                    errors.writerow({"row": idx, "title": title, "error": str(e)})
                progress.advance(task)

    console.print(f"\n[green]Done.[/] Report: {report_path}   Errors: {error_path}")


if __name__ == "__main__":
    cli()
