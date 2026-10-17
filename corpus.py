#!/usr/bin/env python3
"""
corpus.py — what makes this repo the HLS rather than one of the others.

embeddings.py is vendored byte-identical across the fleet (see ch-h-bot
docs/MCP_PROVIDER_PATTERN.md); whatever is particular to the HLS lives here
instead, so that the shared file can stay a checksum.

This repo is on the older layout: embed_db.py and the semantic search in db.py
are its own, not the shared ones. Its index stores `chunks.article_id` (the
shared schema says `parent_id`) and puts the article title on the first line of
every passage, which the shared embed_db.py cannot do. Moving to the shared
files is a schema migration plus a full re-embed, so it is a deployment, not a
sync.
"""

from __future__ import annotations

import os
import re

# The shared embeddings.py reads HLS_EMBED_MODEL, HLS_CHUNK_CHARS, ... only if it
# knows the prefix. Set before it is imported; the Dockerfile sets it as well.
os.environ.setdefault("MCP_ENV_PREFIX", "HLS")

import embeddings as emb  # noqa: E402

from article_metadata import split_author_byline  # noqa: E402

# Qwen3-Embedding is instruction-aware: queries carry a task instruction and
# documents do not. This one names the encyclopaedia; the shared default only
# says "Swiss history".
QUERY_PREFIX = os.environ.get(
    "HLS_EMBED_QUERY_PREFIX",
    "Instruct: Given a question about Swiss history, retrieve passages from an "
    "encyclopaedia that answer it\nQuery: ",
)


def clean_article_text(text: str) -> str:
    """Article body with the byline stripped and whitespace normalised."""
    _, body = split_author_byline(text)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def embed_query(query: str, model: str = emb.DEFAULT_MODEL, client=None) -> list[float]:
    """Embed one search query with the HLS instruction prefix."""
    vectors = emb.embed_texts([QUERY_PREFIX + query], model=model, client=client)
    if not vectors:
        raise emb.EmbeddingError("the embedding service returned no vector for the query")
    return vectors[0]
