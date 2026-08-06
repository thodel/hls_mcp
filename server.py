"""server.py — HLS MCP server (mcp 2.0 MCPServer, streamable HTTP)."""
import argparse, json, logging, os
from mcp.server.mcpserver import MCPServer
import db as db_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_DB   = os.environ.get("HLS_DB",   "/data/hls.db")
DEFAULT_HOST = os.environ.get("HLS_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("HLS_PORT", "8004"))


def normalise_path(path):
    """A single leading slash, no trailing slash — the form the ASGI route wants.

    Behind a reverse proxy the server must answer on its *public* path: the
    streamable-HTTP transport builds no URLs of its own, but the route only
    matches what it was mounted at. Setting this to the public path (e.g.
    /mcp/hls/mcp) lets nginx proxy_pass without rewriting, which is the mismatch
    that makes a sub-path deployment 404."""
    cleaned = (path or "").strip().strip("/")
    return f"/{cleaned}" if cleaned else "/mcp"


DEFAULT_HTTP_PATH = normalise_path(os.environ.get("HLS_HTTP_PATH", "/mcp"))

db_module.set_db_path(DEFAULT_DB)

mcp = MCPServer(
    name="HLS",
    version="1.0.0",
    instructions=(
        "HLS (Historisches Lexikon der Schweiz | Dizionario Storico della Svizzera | "
        "Dictionnaire Historique de la Suisse), Switzerland's national historical encyclopedia. "
        "Articles cover biographies, families, places, institutions, and events from all periods. "
        "Articles use HLS identifiers (e.g. 001398). Persons have per-XXXXXX ids, places locXXXXXX. "
        "Use search_articles for full-text search; search_persons for biographical lookups; "
        "get_article for a full article record; list_articles_by_category to browse by type."
    ),
)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def corpus_stats() -> dict:
    """High-level counts for the HLS corpus."""
    return db_module.stats()


@mcp.tool()
def search_articles(query: str, limit: int = 20) -> list[dict]:
    """Full-text search across all HLS articles (title, text, lexical class)."""
    return db_module.search_articles(query, limit)


@mcp.tool()
def get_article(article_id: str) -> dict:
    """Full article by HLS id (e.g. '001398')."""
    result = db_module.get_article(article_id)
    if not result:
        return {"error": f"Article '{article_id}' not found."}
    return result


@mcp.tool()
def list_articles_by_category(category: str, limit: int = 100, offset: int = 0) -> list[dict]:
    """
    List articles by category.
    category: one of 'bio', 'fam', 'geo', 'tem'.
    """
    if category not in ("bio", "fam", "geo", "tem"):
        return [{"error": "category must be one of: bio, fam, geo, tem"}]
    return db_module.list_articles_by_category(category, limit, offset)


@mcp.tool()
def list_articles_by_year(year_from: int, year_to: int, limit: int = 100, offset: int = 0) -> list[dict]:
    """
    List articles whose time-span overlaps [year_from, year_to].
    """
    if year_to < year_from:
        return [{"error": "year_to must be >= year_from"}]
    return db_module.list_articles_by_year(year_from, year_to, limit, offset)


@mcp.tool()
def list_persons(limit: int = 50, offset: int = 0) -> list[dict]:
    """Paginated list of all person records."""
    return db_module.list_persons(limit, offset)


@mcp.tool()
def search_persons(query: str, limit: int = 50) -> list[dict]:
    """Search persons by family name or forename. Returns biographical and location data."""
    return db_module.search_persons(query, limit)


@mcp.tool()
def get_person(pid: str) -> dict:
    """Person record by HLS person id (e.g. 'per-001398')."""
    result = db_module.get_person(pid)
    if not result:
        return {"error": f"Person '{pid}' not found."}
    return result


# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("hls://stats")
def resource_stats() -> str:
    return json.dumps(db_module.stats(), indent=2)


@mcp.resource("hls://article/{article_id}")
def resource_article(article_id: str) -> str:
    result = db_module.get_article(article_id)
    if not result:
        return json.dumps({"error": f"Article '{article_id}' not found."})
    return json.dumps(result, indent=2, ensure_ascii=False)


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="HLS MCP server")
    ap.add_argument("--db",   default=DEFAULT_DB,   help="Path to hls.db (env HLS_DB)")
    ap.add_argument("--host", default=DEFAULT_HOST, help="Bind address (env HLS_HOST)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port (env HLS_PORT)")
    ap.add_argument("--http-path", default=DEFAULT_HTTP_PATH,
                    help="Path the MCP endpoint is served at; set it to the public "
                         "path when behind a reverse proxy (env HLS_HTTP_PATH)")
    args = ap.parse_args(argv)
    args.http_path = normalise_path(args.http_path)
    return args


def main(argv=None):
    args = parse_args(argv)
    db_module.set_db_path(args.db)

    logger.info(f"Database: {args.db}")
    try:
        s = db_module.stats()
        logger.info(f"Corpus: {s['n_articles']:,} articles, {s['n_persons']:,} persons, "
                    f"{s['text_mb']} MB text, categories: {s['categories']}")
    except Exception as e:
        logger.warning(f"Could not read DB stats: {e}")
    logger.info(f"Starting HLS MCP server on {args.host}:{args.port}{args.http_path}")
    mcp.run(transport="streamable-http", host=args.host, port=args.port,
            streamable_http_path=args.http_path)


if __name__ == "__main__":
    main()
