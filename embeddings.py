#!/usr/bin/env python3
"""
embeddings.py — the embedding layer shared by the pipeline and the server.

The MCP server is where an institution's embeddings live: a client asks a
question in natural language and the server answers with the passages that mean
the same thing, rather than the ones that happen to share keywords. Keyword
search cannot do that across languages — a French question does not retrieve
German articles — and cross-language retrieval is a requirement of this corpus.

Vectors come from GPUStack (`qwen3-embedding-0.6b`, 1024 dimensions), are
L2-normalised on the way in, and are stored as float32 BLOBs in SQLite. At
33,506 articles the whole matrix is well under 250 MB, so similarity is an
honest brute-force dot product over a numpy array — no index, nothing to tune,
and exact results.
"""

from __future__ import annotations

import os
import re
import struct
from typing import Iterable, Optional

from article_metadata import split_author_byline

DEFAULT_MODEL = os.environ.get("HLS_EMBED_MODEL", "qwen3-embedding-0.6b")
DEFAULT_BASE_URL = os.environ.get("GPUSTACK_BASE_URL", "https://gpustack.unibe.ch/v1")
DEFAULT_BATCH = int(os.environ.get("HLS_EMBED_BATCH", "64"))

# Passage windowing. 1000 characters is roughly a paragraph of an HLS article —
# small enough that a hit points at the relevant passage, large enough to carry
# its own context into an answer.
CHUNK_CHARS = int(os.environ.get("HLS_CHUNK_CHARS", "1000"))
CHUNK_OVERLAP = int(os.environ.get("HLS_CHUNK_OVERLAP", "150"))

# Qwen3-Embedding is instruction-aware: queries carry a task instruction and
# documents do not. Skipping this costs real retrieval quality, so it is applied
# by default and can be overridden for a differently-trained model.
QUERY_PREFIX = os.environ.get(
    "HLS_EMBED_QUERY_PREFIX",
    "Instruct: Given a question about Swiss history, retrieve passages from an "
    "encyclopaedia that answer it\nQuery: ",
)


class EmbeddingError(RuntimeError):
    """Raised when embeddings are requested but cannot be produced."""


# ── Chunking ─────────────────────────────────────────────────────────────────

def clean_article_text(text: str) -> str:
    """Article body with the byline stripped and whitespace normalised."""
    _, body = split_author_byline(text)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def chunk_article(text: str, size: int = CHUNK_CHARS,
                  overlap: int = CHUNK_OVERLAP) -> list[tuple[int, int, str]]:
    """Window an article into ``(char_start, char_end, text)`` passages.

    Offsets are into the *cleaned* text, so a hit can be located in the article
    it came from. Windows break at a paragraph or sentence boundary when one
    falls in the back half, so passages do not end mid-clause.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [(0, len(text), text)]

    out: list[tuple[int, int, str]] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            for marker in ("\n\n", ". ", "\n"):
                cut = window.rfind(marker)
                if cut > size * 0.5:
                    end = start + cut + len(marker)
                    break
        piece = text[start:end].strip()
        if piece:
            out.append((start, end, piece))
        if end >= len(text) or end <= start:
            break
        nxt = end - overlap
        start = nxt if nxt > start else end
    return out


# ── Vector storage ───────────────────────────────────────────────────────────

def pack(vector: Iterable[float]) -> bytes:
    """L2-normalise and pack a vector as little-endian float32.

    Normalising once at write time makes every later similarity a plain dot
    product, which is what keeps brute-force search cheap.
    """
    values = [float(v) for v in vector]
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return struct.pack(f"<{len(values)}f", *(v / norm for v in values))


def unpack(blob: bytes) -> list[float]:
    """Unpack a stored vector (mostly for tests and inspection)."""
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


# ── GPUStack client ──────────────────────────────────────────────────────────

_client = None


def get_client(base_url: Optional[str] = None, api_key: Optional[str] = None):
    """OpenAI-compatible GPUStack client, created once.

    GPUStack is reachable only from inside the UniBE network; from anywhere else
    it answers 403 *before* checking the key, so a 403 means the wrong network
    rather than a bad credential. The message says so, because the alternative
    is an afternoon spent rotating a key that was never the problem.
    """
    global _client
    if _client is not None and base_url is None and api_key is None:
        return _client
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise EmbeddingError(
            "the `openai` package is required for embeddings — pip install openai"
        ) from exc
    key = api_key or os.environ.get("GPUSTACK_API_KEY")
    if not key:
        raise EmbeddingError(
            "GPUSTACK_API_KEY is not set; semantic search needs it to embed the query")
    client = OpenAI(base_url=base_url or DEFAULT_BASE_URL, api_key=key)
    if base_url is None and api_key is None:
        _client = client
    return client


def embed_texts(texts: list[str], model: str = DEFAULT_MODEL,
                client=None) -> list[list[float]]:
    """Embed a batch of texts. Returns raw (un-normalised) vectors."""
    if not texts:
        return []
    client = client or get_client()
    try:
        response = client.embeddings.create(model=model, input=texts)
    except Exception as exc:
        detail = str(exc)
        if "403" in detail:
            detail += ("  — gpustack.unibe.ch denies requests from outside the "
                       "UniBE network before checking the API key; connect the VPN.")
        raise EmbeddingError(f"embedding request failed: {detail}") from exc
    return [item.embedding for item in response.data]


def embed_query(query: str, model: str = DEFAULT_MODEL, client=None) -> list[float]:
    """Embed one search query, with the instruction prefix applied."""
    vectors = embed_texts([QUERY_PREFIX + query], model=model, client=client)
    if not vectors:
        raise EmbeddingError("the embedding service returned no vector for the query")
    return vectors[0]
