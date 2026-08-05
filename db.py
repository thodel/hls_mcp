"""
db.py — SQLite query helpers for HLS MCP server.
"""
import re
import sqlite3
from contextlib import contextmanager
from typing import Any

_DB_PATH = "/data/hls.db"

MAX_LIMIT = 500      # ceiling for any caller-supplied limit
YEAR_SCAN_CAP = 5000 # rows examined by the time_span filter, see list_articles_by_year

# The schema, owned here so build_db.py and the tests cannot drift apart.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS articles (
    id            TEXT    PRIMARY KEY,
    version       TEXT,
    title         TEXT    NOT NULL,
    content_html  TEXT,
    content_text  TEXT,
    time_span     TEXT,
    orig_time     TEXT,
    category      TEXT,
    lexical_class TEXT,
    orig_lexical  TEXT,
    place_class   TEXT,
    orig_place    TEXT,
    lat           REAL,
    lon           REAL,
    birth_date    TEXT,
    death_date    TEXT,
    family_name   TEXT,
    additional    TEXT,
    first_name    TEXT,
    gender        TEXT
);
CREATE TABLE IF NOT EXISTS persons (
    id          TEXT PRIMARY KEY,
    article_id  TEXT REFERENCES articles(id),
    family_name TEXT,
    first_name  TEXT,
    additional  TEXT,
    birth_date  TEXT,
    death_date  TEXT,
    gender      TEXT,
    category    TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    id UNINDEXED, title, content_text, category,
    lexical_class, family_name, first_name,
    content=articles, content_rowid=rowid
);
CREATE INDEX IF NOT EXISTS ix_articles_category ON articles(category);
CREATE INDEX IF NOT EXISTS ix_articles_lexical  ON articles(lexical_class);
CREATE INDEX IF NOT EXISTS ix_articles_family   ON articles(family_name);
CREATE INDEX IF NOT EXISTS ix_persons_article   ON persons(article_id);
CREATE INDEX IF NOT EXISTS ix_persons_family    ON persons(family_name);
"""

ARTICLE_BRIEF = ("id, title, category, lexical_class, time_span, "
                 "lat, lon, family_name, first_name")


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


def clamp(limit, default, cap=MAX_LIMIT) -> int:
    """Constrain a caller-supplied limit. SQLite reads LIMIT -1 as unbounded, so an
    unchecked negative value would return the whole table; anything invalid or out
    of range falls back to the tool's own default."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return min(n, cap) if n >= 1 else default


def clamp_offset(offset) -> int:
    try:
        return max(int(offset), 0)
    except (TypeError, ValueError):
        return 0


def like_pattern(query) -> str:
    """Substring pattern for LIKE, with the wildcards escaped so a query of '%' or
    '_' matches those characters literally instead of the whole table. Pairs with
    ESCAPE '\\' in the SQL."""
    escaped = (query or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def quote_fts(query: str) -> str:
    """Rewrite a query as quoted FTS5 phrases, one per word (implicit AND).
    Strips the characters FTS5 treats as syntax so no input can be a syntax error."""
    tokens = [t for t in re.split(r'\s+', re.sub(r'["\*\(\):^-]', ' ', query or "")) if t]
    return ' '.join(f'"{t}"' for t in tokens)


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

_FTS_SQL = """
    SELECT a.id, a.title, a.category, a.lexical_class,
           snippet(articles_fts, 1, '<b>', '</b>', '…', 32) AS snippet,
           a.lat, a.lon, a.time_span, a.family_name, a.first_name
    FROM articles_fts f
    JOIN articles a ON a.rowid = f.rowid
    WHERE articles_fts MATCH ?
    ORDER BY rank
    LIMIT ?
"""

_LIKE_SQL = """
    SELECT id, title, category, lexical_class,
           SUBSTR(content_text, 1, 120) AS snippet,
           lat, lon, time_span, family_name, first_name
    FROM articles
    WHERE title LIKE ? ESCAPE '\\'
    LIMIT ?
"""


def search_articles(query: str, limit: int = 20) -> list[dict]:
    """Full-text search. Honours FTS5 operators (OR, NEAR, prefix*) when the query is
    well formed, falls back to quoted phrases, and finally to a literal title search,
    rather than raising at the caller."""
    limit = clamp(limit, 20)
    if not query or not query.strip():
        return [{"error": "Empty query."}]
    with conn() as c:
        for q in (query, quote_fts(query)):
            if not q:
                continue
            try:
                return _rows(c.execute(_FTS_SQL, (q, limit)).fetchall())
            except sqlite3.OperationalError:
                continue
        return _rows(c.execute(_LIKE_SQL, (like_pattern(query), limit)).fetchall())


def get_article(article_id: str) -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        return _row(row)


def list_articles_by_category(
    category: str, limit: int = 100, offset: int = 0
) -> list[dict]:
    with conn() as c:
        rows = c.execute(f"""
            SELECT {ARTICLE_BRIEF}
            FROM articles
            WHERE category = ?
            ORDER BY title
            LIMIT ? OFFSET ?
        """, (category, clamp(limit, 100), clamp_offset(offset))).fetchall()
    return _rows(rows)


def list_articles_by_lexical_class(
    lc: str, limit: int = 100, offset: int = 0
) -> list[dict]:
    with conn() as c:
        rows = c.execute(f"""
            SELECT {ARTICLE_BRIEF}
            FROM articles
            WHERE lexical_class LIKE ? ESCAPE '\\'
            ORDER BY title
            LIMIT ? OFFSET ?
        """, (like_pattern(lc), clamp(limit, 100), clamp_offset(offset))).fetchall()
    return _rows(rows)


_YEAR_RE = re.compile(r"\b(\d{3,4})\b")


def list_articles_by_year(
    year_from: int, year_to: int, limit: int = 100, offset: int = 0
) -> list[dict]:
    """
    List articles whose time_span overlaps [year_from, year_to].

    time_span is free text ('1848–1920', 'um 1520'), so the overlap test has to run
    in Python. It is applied to the id/time_span pairs *before* paging — an earlier
    version paged first and filtered second, which meant the year range only ever
    saw whichever rows happened to sort first and silently returned the wrong set.
    """
    limit, offset = clamp(limit, 100), clamp_offset(offset)
    with conn() as c:
        candidates = c.execute("""
            SELECT id, time_span FROM articles
            WHERE time_span IS NOT NULL AND time_span != ''
            ORDER BY time_span
            LIMIT ?
        """, (YEAR_SCAN_CAP,)).fetchall()

        ids = []
        for row in candidates:
            years = _YEAR_RE.findall(row["time_span"] or "")
            if not years:
                continue
            if int(years[0]) <= year_to and int(years[-1]) >= year_from:
                ids.append(row["id"])

        page = ids[offset:offset + limit]
        if not page:
            return []
        placeholders = ",".join("?" * len(page))
        rows = c.execute(
            f"SELECT {ARTICLE_BRIEF} FROM articles WHERE id IN ({placeholders})", page
        ).fetchall()

    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[i] for i in page if i in by_id]


# ── Person queries ────────────────────────────────────────────────────────────

def list_persons(limit: int = 50, offset: int = 0) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT id, article_id, family_name, first_name,
                   additional, birth_date, death_date, gender, category
            FROM persons
            ORDER BY family_name, first_name
            LIMIT ? OFFSET ?
        """, (clamp(limit, 50), clamp_offset(offset))).fetchall()
    return _rows(rows)


def search_persons(query: str, limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT p.id, p.article_id, p.family_name, p.first_name,
                   p.additional, p.birth_date, p.death_date, p.gender,
                   a.title, a.category, a.lexical_class, a.lat, a.lon
            FROM persons p
            JOIN articles a ON a.id = p.article_id
            WHERE p.family_name LIKE ?1 ESCAPE '\\' OR p.first_name LIKE ?1 ESCAPE '\\'
            ORDER BY p.family_name, p.first_name
            LIMIT ?2
        """, (like_pattern(query), clamp(limit, 50))).fetchall()
    return _rows(rows)


def get_person(pid: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone()
        return _row(row)


def get_persons_by_article(article_id: str, limit: int = MAX_LIMIT) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM persons WHERE article_id=? LIMIT ?",
            (article_id, clamp(limit, MAX_LIMIT))).fetchall()
        return _rows(rows)
