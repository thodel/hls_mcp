#!/usr/bin/env python3
"""
corpus.py — what makes this repo the HLS rather than one of the others.

embeddings.py, vectorstore.py and embed_db.py are vendored byte-identical
across the fleet (see ch-h-bot docs/MCP_PROVIDER_PATTERN.md); whatever is
particular to the HLS lives here instead, so that the shared files can stay a
checksum.

What the shared embed_db.py reads from here:
    DB_ENV, DEFAULT_DB, PARENT_TABLE   what and where the corpus is
    iter_documents(conn, limit)        -> (article_id, head, text)
    clean(text)                        the byline off, blank runs collapsed

The head is the article title, verbatim, on the first line of every passage;
the offsets of a passage count in clean(text). That is exactly what this
repo's own embed_db.py wrote until #14, so the 57,538 passages of the live
index keep their ids, offsets and text, and no vector is embedded again
(measured over every article in thodel/ch-h-bot#555).
"""

from __future__ import annotations

import os
import re

# The shared embeddings.py reads HLS_EMBED_MODEL, HLS_CHUNK_CHARS, ... only if it
# knows the prefix. Set before it is imported; the Dockerfile sets it as well.
os.environ.setdefault("MCP_ENV_PREFIX", "HLS")

import embeddings as emb  # noqa: E402

from article_metadata import split_author_byline  # noqa: E402

DB_ENV = "HLS_DB"
DEFAULT_DB = "/data/hls.db"
PARENT_TABLE = "articles"

# Qwen3-Embedding is instruction-aware: queries carry a task instruction and
# documents do not. This one names the encyclopaedia; the shared default only
# says "Swiss history".
QUERY_PREFIX = os.environ.get(
    "HLS_EMBED_QUERY_PREFIX",
    "Instruct: Given a question about Swiss history, retrieve passages from an "
    "encyclopaedia that answer it\nQuery: ",
)


def clean(text: str) -> str:
    """Article body with the byline stripped and whitespace normalised."""
    _, body = split_author_byline(text)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def head_line(title) -> str:
    """The head of every passage of an article: its title, verbatim.

    Verbatim, not stripped: eight live titles carry a space at the edge, and a
    .strip() here would change the stored text of their eleven passages and
    cost them their vectors (thodel/ch-h-bot#555). One function builds the
    head and db.passage_body cuts it, so the two sides cannot disagree.
    """
    return title or ""


def iter_documents(conn, limit=None):
    """Yield (article_id, head, text) for every article with a body."""
    sql = ("SELECT id, title, content_text FROM articles "
           "WHERE content_text IS NOT NULL AND content_text <> '' ORDER BY id")
    rows = conn.execute(sql + (" LIMIT ?" if limit else ""),
                        (limit,) if limit else ()).fetchall()
    for article_id, title, text in rows:
        yield article_id, head_line(title), text


def embed_query(query: str, model: str = emb.DEFAULT_MODEL, client=None) -> list[float]:
    """Embed one search query with the HLS instruction prefix."""
    vectors = emb.embed_texts([QUERY_PREFIX + query], model=model, client=client)
    if not vectors:
        raise emb.EmbeddingError("the embedding service returned no vector for the query")
    return vectors[0]
