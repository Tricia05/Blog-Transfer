import pandas as pd
df = pd.read_excel(r"C:\Users\ROBB\Downloads\blog_posts (2).xlsx", dtype=object)
print("Total rows:", len(df))
print("Columns:", list(df.columns))
print()

# Content length distribution
df["_clen"] = df["blog_content"].fillna("").astype(str).str.len()
print("Content length: min={}, median={}, max={}".format(
    df["_clen"].min(), int(df["_clen"].median()), df["_clen"].max()))
print()

# Suspiciously short content (possible non-posts)
short = df[df["_clen"] < 500].sort_values("_clen")
print(f"=== {len(short)} rows with < 500 chars content (possible non-posts) ===")
for _, r in short.iterrows():
    print(f"  [{r['_clen']:5} ch] {str(r['Title'])[:50]:52} {str(r['Permalink'])[:55]}")
print()

# Generic / suspicious titles
import re
susp = df[df["Title"].fillna("").astype(str).str.strip().str.lower().isin(
    ["blog","home","homepage","about","contact","services","category","uncategorized",""])]
print(f"=== {len(susp)} rows with generic titles ===")
for _, r in susp.iterrows():
    print(f"  {str(r['Title'])[:40]:42} {str(r['Permalink'])[:60]}")
print()

# Duplicate slugs
dup = df[df["blog_slug"].duplicated(keep=False) & df["blog_slug"].notna()]
print(f"=== {len(dup)} rows with duplicate slugs ===")
for _, r in dup.sort_values("blog_slug").head(20).iterrows():
    print(f"  {str(r['blog_slug'])[:45]:47} {str(r['Title'])[:40]}")

# Missing dates
nodate = df[df["blog_dates"].fillna("").astype(str).str.strip()==""]
print(f"\n=== {len(nodate)} rows with no date ===")
for _, r in nodate.head(10).iterrows():
    print(f"  {str(r['Title'])[:50]:52} {str(r['Permalink'])[:55]}")
