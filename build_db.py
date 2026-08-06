#!/usr/bin/env python3
"""
build_db.py — Build hls.db from the HLS CSV export.
Run once to populate /data/hls.db inside the container.
"""
import csv, os, sys, sqlite3

import db as db_module

SRC_CSV = os.environ.get("HLS_SRC_CSV", "/src/hls_articles.csv")
OUT_DB  = os.environ.get("HLS_OUT_DB",  "/data/hls.db")
BATCH   = 5000

def build_db():
    if not os.path.exists(SRC_CSV):
        print(f"ERROR: source CSV not found at {SRC_CSV}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(OUT_DB), exist_ok=True)
    if os.path.exists(OUT_DB):
        os.remove(OUT_DB)

    conn = sqlite3.connect(OUT_DB)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous  = NORMAL")

    conn.executescript(db_module.SCHEMA_SQL)

    reader = csv.DictReader(open(SRC_CSV, encoding="utf-8", errors="replace"))
    rows_buf, persons_buf = [], []
    total = 0

    for row in reader:
        aid = row["id"].strip()
        if not aid:
            continue
        cat   = row.get("category", "") or ""
        fam   = row.get("bio.family_name", "") or ""
        first = row.get("bio.first_name", "") or ""
        addl  = row.get("bio.additional_name", "") or ""
        bdate = row.get("bio.birth_date", "") or ""
        ddate = row.get("bio.death_date", "") or ""
        gender= row.get("bio.gender", "") or ""

        rows_buf.append((
            aid, row.get("version",""), row.get("title",""),
            row.get("content_html",""), row.get("content_text",""),
            row.get("time_spanes",""), row.get("origin_time_spanes",""),
            cat, row.get("lexical_class",""), row.get("origin_lexical_class",""),
            row.get("place_class",""), row.get("origin_place_class",""),
            float(row["geo.lat"])   if row.get("geo.lat")   else None,
            float(row["geo.lon"])   if row.get("geo.lon")   else None,
            bdate, ddate, fam, addl, first, gender,
        ))

        if cat == "bio" and fam:
            persons_buf.append((f"per-{aid}", aid, fam, first, addl, bdate, ddate, gender, cat))

        if len(rows_buf) >= BATCH:
            conn.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_buf)
            if persons_buf:
                conn.executemany("INSERT OR IGNORE INTO persons VALUES (?,?,?,?,?,?,?,?,?)", persons_buf)
            total += len(rows_buf)
            print(f"  inserted {total:,} …", file=sys.stderr)
            rows_buf.clear(); persons_buf.clear()

    if rows_buf:
        conn.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_buf)
        total += len(rows_buf)
    if persons_buf:
        conn.executemany("INSERT OR IGNORE INTO persons VALUES (?,?,?,?,?,?,?,?,?)", persons_buf)
    conn.commit()
    print(f"  committed {total:,} articles", file=sys.stderr)

    print("  building FTS …", file=sys.stderr)
    conn.execute(
        "INSERT INTO articles_fts(rowid,id,title,content_text,category,lexical_class,family_name,first_name) "
        "SELECT rowid,id,title,content_text,category,lexical_class,family_name,first_name FROM articles"
    )
    conn.commit()

    n_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    n_per = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    print(f"Done: {n_art:,} articles, {n_per:,} persons → {OUT_DB}")

if __name__ == "__main__":
    build_db()
