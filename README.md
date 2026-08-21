# hls_mcp

[HLS](https://hls.ch) (Historisches Lexikon der Schweiz | Dizionario Storico della Svizzera |
Dictionnaire Historique de la Suisse) — MCP server using the [MCP 2.0](https://modelcontextprotocol.io)
`MCPServer` API with streamable HTTP transport.

## Corpus

- **33,506 articles** across four categories: `bio` (biographies), `fam` (families),
  `geo` (places), `tem` (topics/institutions)
- **24,277 bio articles** with birth/death dates and family names
- Full-text content in German, French, Italian

## Building the database

The `hls.db` is built from the HLS CSV export on first run (or on demand):

```bash
docker compose run --rm hls-mcp python build_db.py
```

Or outside Docker:

```bash
HLS_SRC_CSV=/path/to/hls_articles.csv HLS_OUT_DB=/path/to/hls.db python build_db.py
```

## Running

```bash
docker compose up -d
```

The endpoint is `http://localhost:8004/mcp` by default, and whatever `--http-path`
says otherwise — see [Transport](#transport).

Connect Claude Code with — note the name and URL are positional, there is no `--url`
flag:

```bash
claude mcp add --transport http hls http://<server-ip>:8004/mcp -s user
```

`-s user` makes the server available in every project; `-s project` writes it to
`.mcp.json` to share with a repository. In Claude Desktop, Cowork or claude.ai, use
Customize → Connectors → **+** → *Add custom connector* with the same URL; those
clients connect from Anthropic's cloud, so the server must be reachable over the public
internet. In a `.mcp.json` the entry is `{"type": "http", "url": "…"}` — a `url`
without a `type` is read as a stdio server and skipped.

## Tools

| Tool | Description |
|------|-------------|
| `corpus_stats` | Corpus summary: article/person counts, text size, categories |
| `search_semantic` | **Meaning-based** passage search — answers questions across languages |
| `semantic_index_stats` | Coverage and provenance of the semantic index |
| `search_articles` | FTS5 full-text search across title, text, lexical class |
| `get_article` | Full article record by HLS id (e.g. `001398`) |
| `list_articles_by_category` | Browse articles by type: bio, fam, geo, tem |
| `list_articles_by_year` | Articles whose time-span overlaps a given year range |
| `list_persons` | Paginated person authority list |
| `search_persons` | Search persons by family name or forename |
| `get_person` | Person record by HLS person id (e.g. `per-001398`) |

## Semantic search

`search_articles` finds articles containing the words you typed. `search_semantic`
finds passages that *mean* what you asked — which is a different, and for this corpus
a more useful, thing:

- **It works across languages.** HLS text is overwhelmingly German. Asking
  *"Que sait-on du Pacte fédéral de 1291 ?"* through `search_articles` returns noise,
  because the French words are not in the German text. Through `search_semantic` it
  returns *Bundesvertrag* and *Schweizerische Eidgenossenschaft*.
- **It does not need the right word.** Asking about the monastery at Königsfelden by
  keyword returned *Franz Ludwig Haller von Königsfelden* and passing mentions; by
  meaning, the *Königsfelden* article ranks first at 0.77.
- **It returns passages, not articles.** A hit is one ~1000-character window with its
  offsets into the article, so an answer can quote and cite the paragraph rather than
  a 20,000-character biography.

Use `search_articles` when the exact string matters (a name, a spelling, a phrase) and
`search_semantic` when the question matters.

### Building the index

```bash
GPUSTACK_API_KEY=... python embed_db.py            # whole corpus
GPUSTACK_API_KEY=... python embed_db.py --limit 500 # trial run on a sample
GPUSTACK_API_KEY=... python embed_db.py --recompute # after changing the model
```

Articles are windowed into ~1000-character passages (150 overlap), each prefixed with
its article title so a passage lifted from mid-article still carries its subject, and
embedded with `qwen3-embedding-0.6b` on GPUStack (1024 dimensions). Vectors are stored
L2-normalised as float32 BLOBs.

Runs are **resumable** — chunks already embedded with the same model are skipped, and
each batch is committed — so an interrupted run continues rather than restarting.
Every run is recorded in `embedding_runs` with its model, dimensions and window
settings.

Measured on the full corpus (2026-08-21): **57,538 passages over all 33,506 articles,
183 seconds** at ~320 passages/s. The database grows from 202 MB to 511 MB; the server
holds 225 MB of vectors resident and loads them at startup, so no user query pays for
the load. A search is one matrix multiply — exact, no approximate index, nothing to
tune — and takes ~100 ms including the round trip to embed the query.

### Query-time requirements

The server embeds the incoming query, so it needs `GPUSTACK_API_KEY` at runtime even
though the article vectors are already in the database. GPUStack is reachable only
from inside the UniBE network; from outside it returns **403 before checking the key**,
so a 403 means the wrong network, not a bad credential. Without a key the other tools
work normally and `search_semantic` returns an explanatory error.

## Transport

<a id="transport"></a>

**Streamable HTTP** — one endpoint answering `POST` (requests), `GET` (the
server→client stream), and `DELETE` (session teardown).

This replaces the SSE transport this server used previously. SSE is deprecated, and
its handshake hands the client an absolute `/messages/` path computed from the app's
own mount point — a path the client cannot reach when the server sits behind a
reverse-proxy sub-path. **Clients pointed at `/sse` must be repointed.**

### Behind a reverse proxy

Set `--http-path` (or `HLS_HTTP_PATH`) to the *public* path, and give nginx a
`location` with the same string. Then nginx forwards the path unchanged:

```nginx
location /mcp/hls/mcp {
    proxy_pass         http://127.0.0.1:8004;   # no trailing slash
    proxy_http_version 1.1;
    proxy_set_header   Connection '';
    proxy_buffering    off;
    proxy_read_timeout 3600s;
    chunked_transfer_encoding on;
}
```

The app's path and the nginx `location` must agree exactly or every request 404s.
The startup line prints what is actually being served:

```
Starting HLS MCP server on 0.0.0.0:8004/mcp/hls/mcp
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HLS_DB` | `/data/hls.db` | Path to the SQLite database |
| `HLS_HOST` | `0.0.0.0` | Bind address |
| `HLS_PORT` | `8004` | TCP port |
| `HLS_HTTP_PATH` | `/mcp` | Path the MCP endpoint is served at |
| `HLS_BM25_WEIGHTS` | `0,10,1,0.5,0.5,3,3` | bm25 column weights for `search_articles` — see [Ranking](#ranking) |
| `GPUSTACK_BASE_URL` | `https://gpustack.unibe.ch/v1` | Embedding endpoint |
| `GPUSTACK_API_KEY` | — | Required to embed queries for `search_semantic` |
| `HLS_EMBED_MODEL` | `qwen3-embedding-0.6b` | Embedding model (1024 dimensions) |
| `HLS_EMBED_BATCH` | `64` | Passages per embedding request |
| `HLS_CHUNK_CHARS` / `HLS_CHUNK_OVERLAP` | `1000` / `150` | Passage windowing |
| `HLS_EMBED_QUERY_PREFIX` | *(Qwen instruction)* | Instruction prefix for query embedding |
| `HLS_DATA_DIR` | `/home/dh/hls_data` | Host directory mounted at `/data` — must be writable by the user running compose, since `embed_db.py` writes the index into the same database |

## Query behaviour

**Limits.** Every `limit` is clamped to at most 500; a negative, zero, or non-numeric
value falls back to that tool's own default rather than returning the whole table.

**Name search.** SQL wildcards in a query are escaped, so searching for `100%` finds a
literal "100%" rather than matching every record.

**Full-text search.** `search_articles` passes the query to FTS5, so operators work —
`Bern OR Brugg`, `Zwing*`, `NEAR(...)`. An invalid FTS5 query falls back to quoted
phrases and then to a literal title search instead of raising.

**Snippets.** Each hit's `snippet` is drawn from the article body, with the matched
terms wrapped in `<b>`. Before this was fixed the snippet was taken from the title
column, so every hit's snippet was simply its own title — a RAG client had no way to
judge relevance without fetching each article in full.

### Ranking

`search_articles` orders by `bm25()` with per-column weights rather than the
unweighted default, because a title match is the strongest signal that an article is
*about* the query, while an unweighted score lets any long article that mentions the
term often outrank it.

| Column | Weight | Why |
|---|---|---|
| `id` | 0 | unindexed |
| `title` | 10 | the headword is what the article is about |
| `content_text` | 1 | baseline |
| `category`, `lexical_class` | 0.5 | classification, not content |
| `family_name`, `first_name` | 3 | a person search should reach the person |

Override with `HLS_BM25_WEIGHTS` (seven comma-separated numbers, in the column order
above). A malformed value is ignored in favour of the defaults, so it can never reach
the SQL.

> These weights are reasoned, not yet tuned against the full corpus. Searching
> `Königsfelden` on the live corpus returned `Franz Ludwig Haller von Königsfelden`
> and several passing mentions ahead of the place itself; re-check that query after
> deploying and adjust the title weight if it still does.

**Year ranges.** `list_articles_by_year` reads the free-text `time_span` field, so the
overlap test runs in Python — over the candidate set *before* paging. It examines at
most `YEAR_SCAN_CAP` (5000) rows.

**Result size.** Claude.ai and Claude Desktop truncate a tool or resource result at
roughly 150,000 characters; the 500-row ceiling keeps every tool under it.

## Deployment

This server runs on `tei.dh.unibe.ch` at
**`https://tei.dh.unibe.ch/mcp/hls/mcp`**, alongside four sibling MCP servers:
[Königsfelden](https://github.com/thodel/kf_mcp), [SSRQ](https://github.com/thodel/ssrq_mcp), [HBLS](https://github.com/thodel/hbls_mcp), [EOS / HGB Basel](https://github.com/thodel/eos_mcp).

What they share — the nginx routing, the landing pages, and the deploy sequence —
lives in **[tei_mcp_ops](https://github.com/thodel/tei_mcp_ops)**. Start there for
anything that spans the fleet; in particular, the app's `--http-path` and the nginx
`location` have to be the same string, which is the rule a sub-path deployment turns
on.

## Tests

```bash
pip install pytest
pytest test_hls_mcp.py
```

Unit tests build their own throwaway database and need no setup. DB and server tests
skip unless pointed at them:

```bash
HLS_DB=/data/hls.db HLS_SERVER=http://localhost:8004 pytest test_hls_mcp.py
```

Requires Python 3.10+ (`X | None` annotations); the container image is
`python:3.12-slim`.
