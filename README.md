# hls_mcp

[HLS](https://hls.ch) (Historisches Lexikon der Schweiz | Dizionario Storico della Svizzera |
Dictionnaire Historique de la Suisse) — MCP server using the [MCP 2.0](https://modelcontextprotocol.io)
`MCPServer` API with SSE transport.

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
# Server: http://localhost:8004/sse  (SSE transport)
```

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

## MCP 2.0 transport

Uses **SSE** (Server-Sent Events):

1. `GET /sse` — opens SSE stream, receives `data: /messages/?session_id=…`
2. `POST /messages/?session_id=…` — send JSON-RPC requests

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HLS_DB` | `/data/hls.db` | Path to the SQLite database |
| `HLS_HOST` | `0.0.0.0` | Bind address |
| `HLS_PORT` | `8004` | TCP port |
