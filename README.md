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
| `search_articles` | FTS5 full-text search across title, text, lexical class |
| `get_article` | Full article record by HLS id (e.g. `001398`) |
| `list_articles_by_category` | Browse articles by type: bio, fam, geo, tem |
| `list_articles_by_year` | Articles whose time-span overlaps a given year range |
| `list_persons` | Paginated person authority list |
| `search_persons` | Search persons by family name or forename |
| `get_person` | Person record by HLS person id (e.g. `per-001398`) |

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

## Query behaviour

**Limits.** Every `limit` is clamped to at most 500; a negative, zero, or non-numeric
value falls back to that tool's own default rather than returning the whole table.

**Name search.** SQL wildcards in a query are escaped, so searching for `100%` finds a
literal "100%" rather than matching every record.

**Full-text search.** `search_articles` passes the query to FTS5, so operators work —
`Bern OR Brugg`, `Zwing*`, `NEAR(...)`. An invalid FTS5 query falls back to quoted
phrases and then to a literal title search instead of raising.

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
