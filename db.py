"""
db.py — SQLite query helpers for HLS MCP server.
"""
import sqlite3
from contextlib import contextmanager
from typing import Any

_DB_PATH = "/data/hls.db"


def set_db_path(path: str):
    global _DB_PATH
    _DB_PATH = path


@contextmanager
def conn():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    try:
        yield con
    finally:
        con.close()


def _row(r) -> dict | None:
    return dict(r) if r else None


def _rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


# ── Stats ─────────────────────────────────────────────────────────────────────

def stats() -> dict[str, Any]:
    with conn() as c:
        n_art    = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        n_per    = c.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        txt_chars= c.execute("SELECT SUM(LENGTH(content_text)) FROM articles").fetchone()[0] or 0
        cats     = dict(c.execute(
            "SELECT category, COUNT(*) FROM articles GROUP BY category").fetchall())
        lex_cnt  = c.execute("SELECT COUNT(DISTINCT lexical_class) FROM articles WHERE lexical_class IS NOT NULL").fetchone()[0]
        return {
            "n_articles":    n_art,
            "n_persons":     n_per,
            "text_chars":    txt_chars,
            "text_mb":       round(txt_chars / 1e6, 1),
            "categories":    cats,
            "n_lexical_classes": lex_cnt,
        }


# ── Article queries ───────────────────────────────────────────────────────────

def search_articles(query: str, limit: int = 20) -> list[dict]:
    with conn() as c:
        try:
            rows = c.execute("""
                SELECT a.id, a.title, a.category, a.lexical_class,
                       snippet(articles_fts, 1, '<b>', '</b>', '…', 32) AS snippet,
                       a.lat, a.lon, a.time_span, a.family_name, a.first_name
                FROM articles_fts f
                JOIN articles a ON a.rowid = f.rowid
                WHERE articles_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (query, limit)).fetchall()
        except Exception:
            # Fallback: LIKE on title
            rows = c.execute("""
                SELECT id, title, category, lexical_class,
                       SUBSTR(content_text, 1, 120) AS snippet,
                       lat, lon, time_span, family_name, first_name
                FROM articles
                WHERE title LIKE ?
                LIMIT ?
            """, (f"%{query}%", limit)).fetchall()
    return _rows(rows)


def get_article(article_id: str) -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        return _row(row)


def list_articles_by_category(
    category: str, limit: int = 100, offset: int = 0
) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT id, title, category, lexical_class, time_span,
                   lat, lon, family_name, first_name
            FROM articles
            WHERE category = ?
            ORDER BY title
            LIMIT ? OFFSET ?
        """, (category, limit, offset)).fetchall()
    return _rows(rows)


def list_articles_by_lexical_class(
    lc: str, limit: int = 100, offset: int = 0
) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT id, title, category, lexical_class, time_span,
                   lat, lon, family_name, first_name
            FROM articles
            WHERE lexical_class LIKE ?
            ORDER BY title
            LIMIT ? OFFSET ?
        """, (f"%{lc}%", limit, offset)).fetchall()
    return _rows(rows)


def list_articles_by_year(
    year_from: int, year_to: int, limit: int = 100, offset: int = 0
) -> list[dict]:
    """
    List articles whose time_span overlaps [year_from, year_to].
    time_span is stored as a string like '1848–1920' or 'um 1520'.
    """
    with conn() as c:
        rows = c.execute("""
            SELECT id, title, category, lexical_class, time_span,
                   lat, lon, family_name, first_name
            FROM articles
            WHERE time_span IS NOT NULL AND time_span != ''
            ORDER BY time_span
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()

    # Simple overlap filter: extract first 4-digit year from time_span
    import re
    filtered = []
    year_pat = re.compile(r"\b(\d{4})\b")
    for row in rows:
        ts = row["time_span"] or ""
        years = year_pat.findall(ts)
        if not years:
            continue
        try:
            y_start = int(years[0])
            y_end   = int(years[-1])
            if y_start <= year_to and y_end >= year_from:
                filtered.append(row)
        except ValueError:
            continue
    return filtered


# ── Person queries ────────────────────────────────────────────────────────────

def list_persons(limit: int = 50, offset: int = 0) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT id, article_id, family_name, first_name,
                   additional, birth_date, death_date, gender, category
            FROM persons
            ORDER BY family_name, first_name
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    return _rows(rows)


def search_persons(query: str, limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT p.id, p.article_id, p.family_name, p.first_name,
                   p.additional, p.birth_date, p.death_date, p.gender,
                   a.title, a.category, a.lexical_class, a.lat, a.lon
            FROM persons p
            JOIN articles a ON a.id = p.article_id
            WHERE p.family_name LIKE ? OR p.first_name LIKE ?
            ORDER BY p.family_name, p.first_name
            LIMIT ?
        """, (f"%{query}%", f"%{query}%", limit)).fetchall()
    return _rows(rows)


def get_person(pid: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone()
        return _row(row)


def get_persons_by_article(article_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM persons WHERE article_id=?", (article_id,)).fetchall()
        return _rows(rows)
