"""Der Umzug auf das geteilte embed_db.py + vectorstore.py (#14, ch-h-bot#555).

Drei Dinge müssen zusammen stimmen, sonst verliert der Live-Index Vektoren:

1. migrate_parent_id.py benennt article_id -> parent_id (und n_articles ->
   n_parents) um, tauscht den Index und lässt sonst nichts anbrennen —
   idempotent, in einer Transaktion, der FK bleibt stehen.
2. Das geteilte embed_db.py baut mit corpus.iter_documents + corpus.clean
   GENAU die Passagen, die das alte embed_db.py gebaut hat: Kennung, Offsets,
   Text. Die Erwartungen unten sind am 22.10.2026 mit dem alten Code auf
   dieser Fixture eingefroren (commit f8bd7e7), vor dem Löschen — mit einem
   Titel «Brugg » (Leerzeichen am Ende) und « Habsburg» (am Anfang), weil ein
   .strip() am Kopf im Live-Index 11 Passagen gekostet hätte.
3. Ein Treffer zeigt die Passage ohne ihre Kopfzeile (Flottenregel aus
   eperiodica_mcp#8); Offsets und Kategorie-Filter wie bisher.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import corpus  # noqa: E402
import db  # noqa: E402
import embed_db  # noqa: E402
import embeddings as emb  # noqa: E402
import migrate_parent_id  # noqa: E402
import vectorstore  # noqa: E402

# ── Die Fixture, auf der das alte embed_db.py lief ───────────────────────────

PARA = ("Das Kloster wurde 1309 von Königin Elisabeth und ihrer Tochter Agnes "
        "an der Stelle gestiftet, an der König Albrecht I. ermordet worden war. ")
LONG = ("Autorin/Autor:\nHistorikerin A\n" + PARA * 6 + "\n\n\n\n" + PARA * 6
        + "\n\n" + PARA * 5)
ZWINGLI = ("Autorin/Autor:   Peter Blickle\n"
           + "Reformator in Zürich, geboren in Wildhaus. " * 30)

# id, title, category, content_text, time_span (repr einer Liste, wie der
# CSV-Export sie schreibt, #10)
ARTICLES = [
    ("000001", "Königsfelden", "geo", LONG, "['1301–1350', '1351–1400']"),
    ("000002", "Brugg ", "geo", "Stadt im Kanton Aargau.", None),
    ("000003", " Habsburg", "fam", "Adelsgeschlecht.", None),
    ("000004", "Leer", "tem", "", None),
    ("000005", "Zwingli, Ulrich", "bio", ZWINGLI, "['1451–1500', '1501–1550']"),
]

# Was das ALTE embed_db.py (f8bd7e7) aus dieser Fixture machte, mit den
# Standardfenstern 1000/150: chunk_id, article_id, chunk_index, char_start,
# char_end, len(text), sha256(text)[:16]. Eingefroren am 22.10.2026.
FROZEN = [
    ("000001#0", "000001", 0, 0, 842, 852, "9f24e537d3da9c37"),
    ("000001#1", "000001", 1, 692, 1684, 1002, "e162eea5349dac15"),
    ("000001#2", "000001", 2, 1534, 2383, 862, "f2b6e1a3938e4892"),
    ("000002#0", "000002", 0, 0, 23, 30, "15b3d8712ed947b2"),
    ("000003#0", "000003", 0, 0, 16, 26, "6839903684127b9d"),
    ("000005#0", "000005", 0, 0, 989, 1004, "0f1a62191c63a316"),
    ("000005#1", "000005", 1, 839, 1289, 466, "3c8713989884eab9"),
]
# Die beiden kurzen Passagen wörtlich: der Kopf ist der Titel, unverändert.
FROZEN_TEXT = {
    "000002#0": "Brugg \nStadt im Kanton Aargau.",
    "000003#0": " Habsburg\nAdelsgeschlecht.",
}
# Und was corpus.clean aus den Texten machte: Byline weg, \n{3,} -> \n\n.
FROZEN_CLEAN = {
    "000001": (2383, "Das Kloster wurde 1309 von Königin Elisa"),
    "000005": (1289, "Reformator in Zürich, geboren in Wildhau"),
}

# Das Index-Schema, wie db.SCHEMA_SQL es bis #14 anlegte (FK und alter Index).
OLD_INDEX_SQL = """
CREATE TABLE chunks (
    chunk_id    TEXT    PRIMARY KEY,
    article_id  TEXT    NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    UNIQUE (article_id, chunk_index)
);
CREATE TABLE embeddings (
    chunk_id TEXT    PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    model    TEXT    NOT NULL,
    dims     INTEGER NOT NULL,
    vector   BLOB    NOT NULL
);
CREATE TABLE embedding_runs (
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
CREATE INDEX ix_chunks_article ON chunks(article_id);
CREATE INDEX ix_embeddings_model ON embeddings(model);
"""

MODEL = "test-model"
# Ein Vektor je Passage, von Hand gesetzt: 000002 (Brugg) liegt genau auf der
# Anfrage [1, 0, 0], die übrigen dahinter.
VECTORS = {
    "000001#0": [0.6, 0.8, 0.0], "000001#1": [0.0, 1.0, 0.0], "000001#2": [0.0, 0.8, 0.6],
    "000002#0": [1.0, 0.0, 0.0],
    "000003#0": [0.8, 0.6, 0.0],
    "000005#0": [0.0, 0.0, 1.0], "000005#1": [0.6, 0.0, 0.8],
}


def _insert_articles(conn):
    conn.executemany(
        "INSERT INTO articles (id, title, category, content_text, time_span) "
        "VALUES (?,?,?,?,?)", ARTICLES)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _chunk_rows(conn, parent_col: str):
    return [(r[0], r[1], r[2], r[3], r[4], len(r[5]), _digest(r[5])) for r in conn.execute(
        f"SELECT chunk_id, {parent_col}, chunk_index, char_start, char_end, text "
        f"FROM chunks ORDER BY {parent_col}, chunk_index")]


def old_schema_db(path) -> str:
    """hls.db wie vor #14: articles wie heute, der Index im alten Schema, die
    eingefrorenen Passagen samt Vektor und einem Lauf mit n_articles."""
    path = str(path)
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_SQL)
    conn.executescript("DROP TABLE embeddings; DROP TABLE chunks; DROP TABLE embedding_runs;")
    conn.executescript(OLD_INDEX_SQL)
    _insert_articles(conn)
    # Dieselben Zeilen wie FROZEN, vom alten Code gebaut, aber hier mit den
    # vollen Texten, die der neue Code gleich noch einmal bauen muss.
    new = sqlite3.connect(":memory:")
    new.executescript(db.SCHEMA_SQL)
    _insert_articles(new)
    embed_db.build_chunks(new, None, emb.CHUNK_CHARS, emb.CHUNK_OVERLAP, quiet=True)
    for row in new.execute("SELECT * FROM chunks"):
        conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)", row)    # positional
        conn.execute("INSERT INTO embeddings VALUES (?,?,?,?)",
                     (row[0], MODEL, 3, emb.pack(VECTORS[row[0]])))
    conn.execute(
        "INSERT INTO embedding_runs (run_id, started_at, finished_at, model, dims, "
        "n_articles, n_chunks) VALUES ('r1', '2026-08-21T10:00:00Z', "
        "'2026-08-21T10:03:03Z', ?, 3, 4, 7)", (MODEL,))
    conn.commit()
    conn.close()
    return path


# ── 2. Der neue Code baut die alten Passagen ─────────────────────────────────

def test_the_shared_embed_db_builds_exactly_the_passages_the_old_one_built():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA_SQL)
    _insert_articles(conn)
    embed_db.build_chunks(conn, None, emb.CHUNK_CHARS, emb.CHUNK_OVERLAP, quiet=True)
    assert _chunk_rows(conn, "parent_id") == FROZEN
    texts = dict(conn.execute("SELECT chunk_id, text FROM chunks"))
    assert {k: texts[k] for k in FROZEN_TEXT} == FROZEN_TEXT
    assert "000004#0" not in texts, "an article without a body has no passage"
    # Jede Passage beginnt mit dem Titel, wörtlich — auch «Brugg » und « Habsburg».
    titles = dict((a[0], a[1]) for a in ARTICLES)
    for cid, text in texts.items():
        assert text.startswith(titles[cid.split("#")[0]] + "\n"), cid


def test_the_head_is_the_title_verbatim():
    assert corpus.head_line("Brugg ") == "Brugg "
    assert corpus.head_line(" Habsburg") == " Habsburg"
    assert corpus.head_line(None) == ""
    docs = list(corpus.iter_documents(_memory_with_articles()))
    assert [(d[0], d[1]) for d in docs] == [
        ("000001", "Königsfelden"), ("000002", "Brugg "), ("000003", " Habsburg"),
        ("000005", "Zwingli, Ulrich")]
    assert docs[0][2] == LONG, "the text is handed over raw; clean runs in the chunker"


def _memory_with_articles():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA_SQL)
    _insert_articles(conn)
    return conn


def test_iter_documents_honours_the_limit():
    assert [d[0] for d in corpus.iter_documents(_memory_with_articles(), 2)] == [
        "000001", "000002"]


def test_clean_is_the_old_clean_article_text():
    for aid, title, cat, text, _ in ARTICLES:
        if aid in FROZEN_CLEAN:
            cleaned = corpus.clean(text)
            assert (len(cleaned), cleaned[:40]) == FROZEN_CLEAN[aid], aid
            assert "Autorin/Autor" not in cleaned and "\n\n\n" not in cleaned
    assert corpus.clean("Politische Gemeinde.") == "Politische Gemeinde."
    assert corpus.clean("Autorin/Autor:\nBeat Bühler\nPolitische Gemeinde.") == \
        "Politische Gemeinde."


# ── 1. Die Migration ─────────────────────────────────────────────────────────

def _indexes(conn, table="chunks"):
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")
            if not r[1].startswith("sqlite_autoindex")}


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_migration_renames_swaps_the_index_and_keeps_every_row(tmp_path, capsys):
    path = old_schema_db(tmp_path / "hls.db")
    conn = sqlite3.connect(path)
    before = _chunk_rows(conn, "article_id")
    assert before == FROZEN and _indexes(conn) == {"ix_chunks_article"}
    conn.close()

    assert migrate_parent_id.main(["--db", path]) == 0
    out = capsys.readouterr().out
    assert "chunks.article_id -> parent_id" in out
    assert "embedding_runs.n_articles -> n_parents" in out
    assert "DROP INDEX ix_chunks_article" in out
    assert "integrity_check: ok" in out
    assert "before: chunks=7, parents=4, embeddings=7, embedding_runs=1" in out
    assert "after:  chunks=7, parents=4, embeddings=7, embedding_runs=1" in out

    conn = sqlite3.connect(path)
    assert "parent_id" in _columns(conn, "chunks") and "article_id" not in _columns(conn, "chunks")
    assert "n_parents" in _columns(conn, "embedding_runs")
    assert "n_articles" not in _columns(conn, "embedding_runs")
    assert _indexes(conn) == {"ix_chunks_parent"}, "the old index is gone, exactly one new"
    assert _chunk_rows(conn, "parent_id") == FROZEN
    assert conn.execute("SELECT n_parents FROM embedding_runs").fetchone() == (4,)
    # Der FK folgt der Umbenennung und bleibt (ch-h-bot#555: nicht neu bauen).
    fks = conn.execute("PRAGMA foreign_key_list(chunks)").fetchall()
    assert [(fk[2], fk[3], fk[4]) for fk in fks] == [("articles", "parent_id", "id")]
    # UNIQUE (parent_id, chunk_index) ist noch da: ein Duplikat wird abgewiesen.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO chunks VALUES ('x', '000002', 0, 0, 1, 'x')")
    conn.close()


def test_migration_is_idempotent_and_the_shared_schema_adds_no_second_index(tmp_path, capsys):
    path = old_schema_db(tmp_path / "hls.db")
    assert migrate_parent_id.main(["--db", path]) == 0
    capsys.readouterr()
    assert migrate_parent_id.main(["--db", path]) == 0
    assert "nothing to do" in capsys.readouterr().out
    conn = sqlite3.connect(path)
    assert migrate_parent_id.migrate(conn) == []
    # Was embed_db.main beim Start tut:
    conn.executescript(vectorstore.SCHEMA_SQL)
    assert _indexes(conn) == {"ix_chunks_parent"}
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()


def test_migration_refuses_a_missing_database(tmp_path):
    assert migrate_parent_id.main(["--db", str(tmp_path / "nope.db")]) == 1


# ── 1 + 2 + 3: nach der Migration findet der geteilte Code alles ─────────────

def test_after_the_migration_nothing_is_pending_and_search_works(tmp_path, capsys):
    path = old_schema_db(tmp_path / "hls.db")
    assert migrate_parent_id.main(["--db", path]) == 0

    conn = sqlite3.connect(path)
    conn.executescript(vectorstore.SCHEMA_SQL)
    # Ein neuer Lauf chunkt alles noch einmal (INSERT OR IGNORE) und ändert nichts…
    embed_db.build_chunks(conn, None, emb.CHUNK_CHARS, emb.CHUNK_OVERLAP, quiet=True)
    assert _chunk_rows(conn, "parent_id") == FROZEN
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone() == (7,)
    # …und jede Passage hat ihren Vektor: nichts steht zum Einbetten an. (Ohne
    # Pending kehrt embed_pending zurück, bevor es einen Client braucht.)
    assert embed_db.embed_pending(conn, MODEL, 64, quiet=True) == (0, 0)
    conn.close()

    # embed_db.py --status, wie im Einspielplan
    assert embed_db.main(["--db", path, "--status"]) == 0
    status = capsys.readouterr().out
    assert "n_chunks: 7" in status and "n_indexed: 4" in status and "n_total: 5" in status

    db.set_db_path(path)
    stats = db.semantic_stats()
    assert stats["n_chunks"] == 7 and stats["coverage"] == 0.8
    assert (stats["n_indexed"], stats["n_total"]) == (4, 5)
    assert "n_articles_indexed" not in stats
    assert db.warm_semantic_index(MODEL)["n_chunks"] == 7

    hits = db.search_semantic([1.0, 0.0, 0.0], limit=3, model=MODEL)
    assert [h["id"] for h in hits] == ["000002", "000003", "000001"]
    brugg = hits[0]
    assert brugg["title"] == "Brugg " and brugg["chunk_id"] == "000002#0"
    assert brugg["snippet"] == "Stadt im Kanton Aargau.", "the passage without its head"
    assert (brugg["char_start"], brugg["char_end"]) == (0, 23)
    assert brugg["category"] == "geo" and brugg["score"] == 1.0
    assert hits[2]["time_span"] == ["1301–1350", "1351–1400"]      # Listen (#13)
    assert hits[2]["lexical_class"] == []

    # Der Kategorie-Filter sitzt jetzt in row_to_hit.
    bio = db.search_semantic([1.0, 0.0, 0.0], limit=3, model=MODEL, category="bio")
    assert [h["id"] for h in bio] == ["000005", "000005"]      # beide Passagen, Kappe 2
    assert bio[0]["chunk_id"] == "000005#1" and bio[0]["snippet"].startswith("geboren")
    assert db.search_semantic([1.0, 0.0, 0.0], limit=3, model=MODEL, category="xyz") == []

    # per_parent: Königsfelden hat drei Passagen, mit Kappe 1 kommt eine.
    kf = db.search_semantic([0.0, 1.0, 0.0], limit=5, model=MODEL, per_parent=1)
    assert [h["id"] for h in kf][:1] == ["000001"]
    assert sum(1 for h in kf if h["id"] == "000001") == 1


# ── 3. Das Snippet ohne Kopfzeile ────────────────────────────────────────────

def _row(text, title="Brugg ", category="geo"):
    # Die Spalten von db._JOIN_SQL, in dieser Reihenfolge.
    return ("000002#0", "000002", 0, 0, 23, text, title, category,
            None, None, None, None, 47.48, 8.21, None)


def test_the_snippet_starts_with_the_text_not_the_head():
    hit = db._row_to_hit(_row("Brugg \nStadt im Kanton Aargau."), "000002#0", 0.9)
    assert hit["snippet"] == "Stadt im Kanton Aargau."
    assert hit["title"] == "Brugg "                          # der Titel bleibt ein Feld
    assert (hit["char_start"], hit["char_end"]) == (0, 23)   # unverändert: im Artikeltext
    assert hit["lat"] == 47.48 and hit["author"] is None


def test_a_passage_without_its_own_head_is_left_alone():
    """Ein Index ohne Kopf, oder eine Passage, die mit irgendeiner Zeile beginnt —
    beides bleibt, wie es ist. Geschnitten wird nur der Kopf DIESES Artikels."""
    assert db.passage_body("Stadt im Kanton Aargau.", "Brugg ") == "Stadt im Kanton Aargau."
    other = "Brugg\nStadt im Kanton Aargau."            # ohne das Leerzeichen: ein anderer Kopf
    assert db.passage_body(other, "Brugg ") == other
    assert db.passage_body("Brugg \nStadt", None) == "Brugg \nStadt"
    assert db.passage_body("Brugg Stadt", "Brugg ") == "Brugg Stadt", "only a whole line"


def test_the_head_is_the_one_embed_db_writes():
    """Dieselbe Funktion baut den Kopf, der auf die Passage kommt — sonst
    schneidet die eine Seite, was die andere nicht schreibt."""
    for _, title, _, text, _ in ARTICLES:
        if not text:
            continue
        piece = emb.chunk_document(text, clean=corpus.clean)[0][2]
        stored = f"{corpus.head_line(title)}\n{piece}"
        assert db.passage_body(stored, title) == piece, title


def test_the_category_filter_drops_the_row():
    assert db._row_to_hit(_row("x", category="geo"), "c", 0.5, "bio") is None
    assert db._row_to_hit(_row("x", category="geo"), "c", 0.5, "geo")["category"] == "geo"
    assert db._row_to_hit(_row("x", category="geo"), "c", 0.5, None)["id"] == "000002"
