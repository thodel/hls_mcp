"""
db.py — SQLite query helpers for HLS MCP server.
"""
import os
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

-- ── Semantic search (embed_db.py) ────────────────────────────────────────────
-- Articles are windowed into passages and each passage is embedded, so a query
-- can match the paragraph that answers it rather than a whole biography. The
-- vector is stored L2-normalised, which makes cosine similarity a dot product.
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT    PRIMARY KEY,   -- "<article_id>#<chunk_index>"
    article_id  TEXT    NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    UNIQUE (article_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id TEXT    PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    model    TEXT    NOT NULL,
    dims     INTEGER NOT NULL,
    vector   BLOB    NOT NULL          -- float32, little-endian, L2-normalised
);
-- One row per pipeline run: which model produced which vectors, and when.
-- Provenance is a project requirement, not bookkeeping — an answer's evidence
-- has to be traceable to the process that indexed it.
CREATE TABLE IF NOT EXISTS embedding_runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    model       TEXT NOT NULL,
    dims        INTEGER,
    base_url    TEXT,
    chunk_chars INTEGER,
    chunk_overlap INTEGER,
    n_articles  INTEGER,
    n_chunks    INTEGER,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS ix_chunks_article ON chunks(article_id);
CREATE INDEX IF NOT EXISTS ix_embeddings_model ON embeddings(model);
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
           a.lat, a.lon, a.time_span, a.family_name, a.first_name
    FROM articles_fts f
    JOIN articles a ON a.rowid = f.rowid
    WHERE articles_fts MATCH ?
    ORDER BY bm25(articles_fts, {_BM25_WEIGHTS})
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


# ── Semantic search ───────────────────────────────────────────────────────────
#
# Vectors are stored L2-normalised, so cosine similarity is a dot product and
# the whole search is one matrix multiply. At ~100k chunks × 1024 dimensions the
# matrix is a few hundred megabytes and the multiply takes milliseconds, so
# there is no approximate index here: results are exact, and there is no recall
# parameter to get wrong.
#
# The matrix is built once per (database, model) on first use and cached. Build
# it eagerly at startup with `warm_semantic_index()` so the first user query is
# not the one that pays for it.

_VECTOR_CACHE: dict[tuple[str, str], Any] = {}


def _load_matrix(model: str):
    """(chunk_ids, matrix) for a model, loaded once and cached."""
    key = (_DB_PATH, model)
    cached = _VECTOR_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "numpy is required for semantic search — pip install numpy") from exc

    with conn() as c:
        rows = c.execute(
            "SELECT chunk_id, dims, vector FROM embeddings WHERE model = ? "
            "ORDER BY chunk_id", (model,)).fetchall()
    if not rows:
        raise RuntimeError(
            f"no embeddings for model {model!r} in {_DB_PATH}. "
            "Run embed_db.py to build the semantic index.")

    dims = rows[0][1]
    chunk_ids = [r[0] for r in rows]
    # One contiguous buffer rather than a list of arrays: this is the difference
    # between a matrix multiply and a hundred thousand small ones.
    buffer = b"".join(r[2] for r in rows)
    matrix = np.frombuffer(buffer, dtype="<f4").reshape(len(rows), dims)
    _VECTOR_CACHE[key] = (chunk_ids, matrix)
    return chunk_ids, matrix


def warm_semantic_index(model: str) -> dict[str, Any]:
    """Load the vectors now and report what was loaded (or why it could not be)."""
    try:
        chunk_ids, matrix = _load_matrix(model)
    except RuntimeError as exc:
        return {"ready": False, "model": model, "reason": str(exc)}
    return {"ready": True, "model": model,
            "n_chunks": len(chunk_ids), "dims": int(matrix.shape[1]),
            "megabytes": round(matrix.nbytes / 1_048_576, 1)}


def semantic_stats(model: str | None = None) -> dict[str, Any]:
    """Coverage of the semantic index, and the runs that produced it."""
    with conn() as c:
        if not c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                         "AND name='embeddings'").fetchone():
            return {"indexed": False,
                    "reason": "no embeddings table; run embed_db.py"}
        by_model = c.execute(
            "SELECT model, COUNT(*), MAX(dims) FROM embeddings GROUP BY model"
        ).fetchall()
        n_chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_articles_total = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        n_articles_chunked = c.execute(
            "SELECT COUNT(DISTINCT article_id) FROM chunks").fetchone()[0]
        runs = c.execute(
            "SELECT run_id, started_at, finished_at, model, dims, n_chunks, notes "
            "FROM embedding_runs ORDER BY started_at DESC LIMIT 5").fetchall()
    return {
        "indexed": bool(by_model),
        "n_chunks": n_chunks,
        "n_articles_indexed": n_articles_chunked,
        "n_articles_total": n_articles_total,
        "coverage": round(n_articles_chunked / n_articles_total, 4)
        if n_articles_total else 0.0,
        "models": [{"model": m, "n_vectors": n, "dims": d} for m, n, d in by_model],
        "recent_runs": [
            {"run_id": r[0], "started_at": r[1], "finished_at": r[2],
             "model": r[3], "dims": r[4], "n_chunks": r[5], "notes": r[6]}
            for r in runs],
    }


_SEMANTIC_SQL = """
    SELECT c.chunk_id, c.article_id, c.chunk_index, c.char_start, c.char_end,
           c.text, a.title, a.category, a.lexical_class, a.time_span,
           a.family_name, a.first_name, a.lat, a.lon
    FROM chunks c JOIN articles a ON a.id = c.article_id
    WHERE c.chunk_id IN ({placeholders})
"""


def search_semantic(query_vector, limit: int = 20, model: str | None = None,
                    category: str | None = None,
                    per_article: int = 2) -> list[dict]:
    """Passages closest in meaning to an already-embedded query.

    ``per_article`` caps how many passages one article may contribute, so a
    single long biography cannot fill the whole result set and crowd out the
    other articles that answer the question.
    """
    import numpy as np

    limit = clamp(limit, 20)
    model = model or os.environ.get("HLS_EMBED_MODEL", "qwen3-embedding-0.6b")
    chunk_ids, matrix = _load_matrix(model)

    query = np.asarray(query_vector, dtype="float32")
    if query.shape[0] != matrix.shape[1]:
        raise ValueError(
            f"query has {query.shape[0]} dimensions, index has {matrix.shape[1]}")
    norm = float(np.linalg.norm(query)) or 1.0
    scores = matrix @ (query / norm)

    # Take a generous slice before filtering: the category filter and the
    # per-article cap both discard candidates, so topping up here avoids a
    # second pass over the matrix.
    fetch = min(len(chunk_ids), max(limit * 8, limit + 50))
    candidates = np.argpartition(-scores, fetch - 1)[:fetch]
    candidates = candidates[np.argsort(-scores[candidates])]

    picked = [(chunk_ids[i], float(scores[i])) for i in candidates]
    by_id = {}
    with conn() as c:
        for start in range(0, len(picked), 400):
            window = picked[start:start + 400]
            sql = _SEMANTIC_SQL.format(placeholders=",".join("?" * len(window)))
            for row in c.execute(sql, [cid for cid, _ in window]).fetchall():
                by_id[row[0]] = row

    out: list[dict] = []
    seen_per_article: dict[str, int] = {}
    for chunk_id, score in picked:
        row = by_id.get(chunk_id)
        if row is None:
            continue                       # vector outlived its chunk
        if category and row[7] != category:
            continue
        if seen_per_article.get(row[1], 0) >= per_article:
            continue
        seen_per_article[row[1]] = seen_per_article.get(row[1], 0) + 1
        out.append({
            "id": row[1],
            "chunk_id": chunk_id,
            "title": row[6],
            "category": row[7],
            "lexical_class": row[8],
            "snippet": row[5],
            "score": round(score, 4),
            "chunk_index": row[2],
            "char_start": row[3],
            "char_end": row[4],
            "time_span": row[9],
            "family_name": row[10],
            "first_name": row[11],
            "lat": row[12],
            "lon": row[13],
        })
        if len(out) >= limit:
            break
    return out
