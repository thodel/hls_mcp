"""
db.py — SQLite query helpers for HLS MCP server.
"""
from __future__ import annotations  # db.py already uses 3.10 syntax

import ast
import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Any

import corpus
import vectorstore

_DB_PATH = os.environ.get(corpus.DB_ENV, corpus.DEFAULT_DB)

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
    gender        TEXT,
    author        TEXT
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

# The semantic index (chunks, embeddings, embedding_runs) is the shared schema
# from vectorstore.py: articles are windowed into passages and each passage is
# embedded, so a query can match the paragraph that answers it rather than a
# whole biography. The parent of a chunk is `parent_id`; a database built
# before #14 says `article_id` and is renamed by migrate_parent_id.py.
SCHEMA_SQL += vectorstore.SCHEMA_SQL

ARTICLE_BRIEF = ("id, title, category, lexical_class, time_span, "
                 "lat, lon, family_name, first_name, author")


def set_db_path(path: str):
    global _DB_PATH
    _DB_PATH = path
    vectorstore.clear_cache()


def migrate(path: str | None = None) -> list[str]:
    """Bring an existing database up to the current schema.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists,
    so a column added to SCHEMA_SQL never reaches a deployed database. The
    queries select it regardless, and every search then fails with "no such
    column" — at the user, on the first request after a deploy, not at build
    time. This runs at startup so the failure surfaces in the server log
    instead.

    Returns the migrations applied, so a caller can log what it changed.
    """
    from article_metadata import split_author_byline

    applied: list[str] = []
    con = sqlite3.connect(path or _DB_PATH)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(articles)")}
        if not cols:
            return applied  # No database yet; build_db.py creates it.

        if "author" not in cols:
            con.execute("ALTER TABLE articles ADD COLUMN author TEXT")
            # Backfill from the stored body rather than re-importing the CSV:
            # the byline is still in content_text, so this needs neither the
            # source files nor a re-embedding run.
            rows = con.execute(
                "SELECT id, content_text FROM articles "
                "WHERE content_text LIKE 'Autorin/Autor:%'"
            ).fetchall()
            updates = []
            for aid, text in rows:
                author, _ = split_author_byline(text or "")
                if author:
                    updates.append((author, aid))
            con.executemany("UPDATE articles SET author = ? WHERE id = ?", updates)
            con.commit()
            applied.append(f"articles.author added, {len(updates)} backfilled")
    finally:
        con.close()
    return applied


@contextmanager
def conn():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    try:
        yield con
    finally:
        con.close()


# The classification columns hold several values per article: an article on a
# monastery sits in two lexical classes and spans fifteen half-centuries. The
# CSV export writes each of them as Python's repr() of a list —
#
#     "['1701–1750', '1751–1800']"
#
# — and build_db.py copies that text into the TEXT column unchanged, so the
# database holds the repr, not JSON (measured against the live server on
# 19.10.2026, issue #10). A client reading JSON got a string it had to parse
# with ast.literal_eval or a regex, and for lexical_class the list separator
# ", " is not distinguishable from a comma inside a term. The columns are
# parsed here, at read time, so no rebuild or migration is needed and every
# tool returns a real list.
LIST_COLUMNS = ("time_span", "orig_time", "lexical_class", "orig_lexical",
                "place_class", "orig_place")


def as_list(value) -> list[str]:
    """A multi-valued column as a list of strings, whatever the stored form.

    repr() of a list parses to its items; a plain value becomes a one-item
    list; NULL and '' become []. The output type is always list, so a client
    never has to branch on the shape.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            parsed = None
        if isinstance(parsed, (list, tuple)):
            return [str(v) for v in parsed]
    return [text]


def _listify(d: dict) -> dict:
    for col in LIST_COLUMNS:
        if col in d:
            d[col] = as_list(d[col])
    return d


def _row(r) -> dict | None:
    return _listify(dict(r)) if r else None


def _rows(rs) -> list[dict]:
    return [_listify(dict(r)) for r in rs]


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

# Column order in articles_fts: 0 id (UNINDEXED), 1 title, 2 content_text,
# 3 category, 4 lexical_class, 5 family_name, 6 first_name.
#
# The snippet must come from column 2 (content_text). Taking it from column 1
# returned the article's own title as its "snippet" for every hit, which told a
# caller nothing it did not already have — and left RAG clients no choice but to
# fetch every hit in full before they could judge relevance.
#
# bm25 weights, rather than the unweighted default: a title match is the
# strongest possible signal that an article is *about* the query, but with equal
# weights it loses to any long article that happens to mention the term often.
# Searching "Königsfelden" ranked "Franz Ludwig Haller von Königsfelden" above
# the place itself. Negative bm25 scores sort ascending — lowest is best.
# Tunable without a rebuild: HLS_BM25_WEIGHTS="0.0,10.0,1.0,0.5,0.5,3.0,3.0".
# The defaults have been reasoned about but not yet tuned against the full
# 33,506-article corpus — see README §Ranking.
_DEFAULT_BM25_WEIGHTS = "0.0, 10.0, 1.0, 0.5, 0.5, 3.0, 3.0"


def _bm25_weights() -> str:
    """Validated bm25 column weights: seven finite numbers, one per column."""
    raw = os.environ.get("HLS_BM25_WEIGHTS", _DEFAULT_BM25_WEIGHTS)
    try:
        values = [float(part) for part in raw.split(",")]
    except ValueError:
        values = []
    if len(values) != 7 or any(v != v or v in (float("inf"), float("-inf")) for v in values):
        # A malformed override must not be interpolated into SQL, and must not
        # take the server down — fall back to the vetted defaults.
        return _DEFAULT_BM25_WEIGHTS
    return ", ".join(repr(v) for v in values)


_BM25_WEIGHTS = _bm25_weights()

_FTS_SQL = f"""
    SELECT a.id, a.title, a.category, a.lexical_class,
           snippet(articles_fts, 2, '<b>', '</b>', '…', 32) AS snippet,
           a.lat, a.lon, a.time_span, a.family_name, a.first_name, a.author
    FROM articles_fts f
    JOIN articles a ON a.rowid = f.rowid
    WHERE articles_fts MATCH ?
    ORDER BY bm25(articles_fts, {_BM25_WEIGHTS})
    LIMIT ?
"""

_LIKE_SQL = """
    SELECT id, title, category, lexical_class,
           SUBSTR(content_text, 1, 120) AS snippet,
           lat, lon, time_span, family_name, first_name, author
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

    The stored value is a list of half-century slots in no particular order
    ("['1701–1750', …, '1401–1450']" on the live Uzwil record), so the span is
    the envelope — the smallest and the largest year anywhere in it — not the
    first and the last year as written. First/last missed every article whose
    list happened to end on an early slot.
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
            years = [int(y) for y in _YEAR_RE.findall(" ".join(as_list(row["time_span"])))]
            if not years:
                continue
            if min(years) <= year_to and max(years) >= year_from:
                ids.append(row["id"])

        page = ids[offset:offset + limit]
        if not page:
            return []
        placeholders = ",".join("?" * len(page))
        rows = c.execute(
            f"SELECT {ARTICLE_BRIEF} FROM articles WHERE id IN ({placeholders})", page
        ).fetchall()

    by_id = {r["id"]: _listify(dict(r)) for r in rows}
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


# ── Semantic search ───────────────────────────────────────────────────────────
#
# The matrix, the search and the per-parent cap are vectorstore.py, shared
# across the fleet. What is HLS here: which columns a hit carries (JOIN_SQL),
# how a row becomes a hit and which rows the category filter drops
# (_row_to_hit), and that the snippet starts with the text, not the head.

_JOIN_SQL = """
    SELECT c.chunk_id, c.parent_id, c.chunk_index, c.char_start, c.char_end, c.text,
           a.title, a.category, a.lexical_class, a.time_span,
           a.family_name, a.first_name, a.lat, a.lon, a.author
    FROM chunks c JOIN articles a ON a.id = c.parent_id
    WHERE c.chunk_id IN ({placeholders})
"""


def passage_body(text: str, title) -> str:
    """The passage without its head line (fleet rule, ch-h-bot#555).

    Every passage carries the article title on its first line — for the
    vector, which otherwise has no idea what a window from the middle of a
    long biography is about. The reader does not need it there: the hit
    carries `title` as a field, and a snippet that starts with it repeats the
    citation line above it, puts the title instead of the text into the
    link's `#:~:text=` fragment and hides the half-word a window may start
    with from the bot's trimming (thodel/eperiodica_mcp#10). Only a text that
    starts with exactly this article's head, as corpus.head_line builds it,
    is cut; a passage without one is left as it is.
    """
    head = corpus.head_line(title)
    if head and text.startswith(head + "\n"):
        return text[len(head) + 1:]
    return text


def _row_to_hit(row, chunk_id: str, score: float, category: str | None = None):
    if category and row[7] != category:
        return None
    return {
        "id": row[1],
        "chunk_id": chunk_id,
        "title": row[6],
        "category": row[7],
        "lexical_class": as_list(row[8]),
        "snippet": passage_body(row[5], row[6]),
        "score": score,
        "chunk_index": row[2],
        "char_start": row[3],
        "char_end": row[4],
        "time_span": as_list(row[9]),
        "family_name": row[10],
        "first_name": row[11],
        "lat": row[12],
        "lon": row[13],
        "author": row[14],
    }


def search_semantic(query_vector, limit: int = 20, model: str | None = None,
                    category: str | None = None,
                    per_parent: int = 2) -> list[dict]:
    """Passages closest in meaning to an already-embedded query.

    ``per_parent`` caps how many passages one article may contribute, so a
    single long biography cannot fill the whole result set and crowd out the
    other articles that answer the question.
    """
    return vectorstore.search(
        conn, _DB_PATH, query_vector,
        join_sql=_JOIN_SQL,
        row_to_hit=lambda row, cid, sc: _row_to_hit(row, cid, sc, category),
        limit=clamp(limit, 20), model=model, per_parent=per_parent)


def warm_semantic_index(model: str) -> dict[str, Any]:
    """Load the vectors now and report what was loaded (or why it could not be)."""
    return vectorstore.warm(conn, _DB_PATH, model)


def semantic_stats() -> dict[str, Any]:
    """Coverage of the semantic index, and the runs that produced it."""
    return vectorstore.stats(conn, corpus.PARENT_TABLE)
