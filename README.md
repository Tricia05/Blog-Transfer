# Blog Transfer Project

Migrate blog posts from a messy Excel file into a WordPress site, with
automatic data cleaning (dates, smart quotes, status values) and column
mapping.

## Setup

```bash
pip install -r requirements.txt
```

In WordPress, create an **Application Password**:
WP Admin → Users → Profile → Application Passwords → "Add New".

Edit `mapping.yaml`:
- Set `wordpress_url`, `username`, `app_password`.
- Update `column_map` to match the column headers in your Excel file.

## Usage

**1. Inspect** the Excel file (see columns and sample rows):

```bash
python migrate.py inspect "posts.xlsx"
```

**2. Dry run** (no upload — writes `output/dry_run_preview.json`):

```bash
python migrate.py run "posts.xlsx" --config mapping.yaml --dry-run
```

**3. Real run** (uploads to WordPress):

```bash
python migrate.py run "posts.xlsx" --config mapping.yaml
```

### Options

| Flag | Purpose |
|---|---|
| `--limit N` | Process only the first N rows (good for testing) |
| `--sheet NAME` | Read a specific sheet (default: first sheet) |
| `--on-duplicate {skip,update,create}` | What to do if a post with the same slug exists (default: `skip`) |
| `--output-dir DIR` | Where to write reports (default: `output/`) |

## What gets cleaned automatically

- **Dates** — Excel serial numbers, `"6/5/21"`, `"June 5, 2021"`, etc. → ISO 8601
- **Smart quotes** and HTML entities normalized
- **Status** values like `live` / `yes` / `published` → `publish`
- **Slugs** auto-generated from title if missing
- **Plain text** content with blank-line paragraphs → wrapped in `<p>` tags
- **Categories / tags** auto-created in WordPress if they don't exist

## Output

After a run, `output/` contains:
- `migration_report.csv` — every row that was created/updated/skipped
- `errors.csv` — rows that failed with the API error message
- `invalid_rows.csv` — rows missing required fields (title or content)

## Safety

- Default post status is `draft` — review in WP Admin before publishing.
- Re-running by default skips posts whose slug already exists. Use
  `--on-duplicate update` to overwrite, or `create` to allow duplicates.
- Always start with `--limit 5 --dry-run` on a new dataset.

## Project layout

```
migrate.py           # CLI entry point
mapping.yaml         # column map + WP credentials
migrator/
  loader.py          # Excel -> DataFrame
  cleaner.py         # date / text / status normalization
  mapper.py          # row -> WP payload
  wordpress.py       # REST API client
output/              # reports written here
```

## Limitations / not yet implemented

- Featured image upload from URL (the field is mapped but not pushed)
- Author lookup by name (use numeric `author_id` in defaults for now)
- Custom post types — currently always creates standard `posts`
- Inline image rewriting (images embedded in content stay as external URLs)
