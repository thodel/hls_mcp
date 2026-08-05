#!/usr/bin/env python3
"""
test_hls_mcp.py — test suite for the HLS MCP server.

Runs two ways. Under pytest, failures fail the run; the CLI keeps the grouped
output and sets the exit code.

    pytest test_hls_mcp.py                                     # unit tests only
    HLS_DB=/data/hls.db pytest test_hls_mcp.py                 # + DB tests
    HLS_SERVER=http://localhost:8004 pytest test_hls_mcp.py    # + server tests

    python test_hls_mcp.py --unit
    python test_hls_mcp.py --db /data/hls.db --server http://localhost:8004

Unit tests build their own throwaway database, so they need no setup. Tests
needing the real DB or a live server skip when it isn't configured.
"""
import argparse, json, os, sqlite3, sys, tempfile
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Helpers ───────────────────────────────────────────────────────────────────

RED   = "\033[91m"
GREEN = "\033[92m"
YELLOW= "\033[93m"
RESET = "\033[0m"

def ok(msg):   print(f"{GREEN}✅ {msg}{RESET}")
def fail(msg): print(f"{RED}❌ {msg}{RESET}")
def warn(msg): print(f"{YELLOW}⚠️  {msg}{RESET}")
def info(msg): print(f"   {msg}")

class Checks:
    """Collects several checks so one run reports them all, then fails as a unit.

    Not named Test* — pytest would try to collect it as a test class.
    """
    def __init__(self): self.passed = self.failed = 0; self.failures = []
    def check(self, cond, msg):
        if cond:
            self.passed += 1
            ok(msg)
        else:
            self.failed += 1
            self.failures.append(msg)
            fail(msg)
    def assert_ok(self):
        total = self.passed + self.failed
        print(f"\n{'─'*50}")
        print(f"Ran {total} checks: {GREEN}{self.passed} passed{RESET}", end="")
        if self.failed: print(f", {RED}{self.failed} failed{RESET}", end="")
        print()
        if self.failed:
            raise AssertionError(
                f"{self.failed} of {total} checks failed:\n  - " + "\n  - ".join(self.failures))


@pytest.fixture
def db_path():
    return os.environ.get("HLS_DB", "")

@pytest.fixture
def base_url():
    return os.environ.get("HLS_SERVER", "")


# ── Fixture database ──────────────────────────────────────────────────────────

# id, version, title, content_html, content_text, time_span, orig_time, category,
# lexical_class, orig_lexical, place_class, orig_place, lat, lon, birth_date,
# death_date, family_name, additional, first_name, gender
ARTICLES = [
    ("001398", "1", "Zwingli, Ulrich", "", "Reformator in Zürich", "1484-1531", "",
     "bio", "Reformator", "", "", "", None, None, "1484", "1531", "Zwingli", "", "Ulrich", "m"),
    ("001399", "1", "Brugg", "", "Stadt im Kanton Aargau", "1284-", "",
     "geo", "Stadt", "", "", "", 47.48, 8.21, "", "", "", "", "", ""),
    ("001400", "1", "Anna 100% Sicher", "", "Testartikel mit Prozentzeichen", "1600-1650", "",
     "bio", "Test", "", "", "", None, None, "1600", "1650", "Sicher", "", "Anna", "f"),
    ("001401", "1", "Hans_Meier", "", "Testartikel mit Unterstrich", "1700-1750", "",
     "bio", "Test", "", "", "", None, None, "1700", "1750", "Meier", "", "Hans", "m"),
]

# The literal '%' and '_' live in the person names on purpose: they are what the
# wildcard-escaping test searches for.
PERSONS = [
    ("per-001398", "001398", "Zwingli", "Ulrich", "", "1484", "1531", "m", "bio"),
    ("per-001400", "001400", "Sicher", "Anna 100%", "", "1600", "1650", "f", "bio"),
    ("per-001401", "001401", "Hans_Meier", "Hans", "", "1700", "1750", "m", "bio"),
]


def make_fixture_db(path):
    import db as db_module
    con = sqlite3.connect(path)
    con.executescript(db_module.SCHEMA_SQL)
    con.executemany(f"INSERT INTO articles VALUES ({','.join('?' * 20)})", ARTICLES)
    con.executemany(f"INSERT INTO persons VALUES ({','.join('?' * 9)})", PERSONS)
    con.execute(
        "INSERT INTO articles_fts(rowid,id,title,content_text,category,"
        "lexical_class,family_name,first_name) "
        "SELECT rowid,id,title,content_text,category,lexical_class,family_name,"
        "first_name FROM articles")
    con.commit(); con.close()
    db_module.set_db_path(path)
    return db_module


# ── 1. Unit tests ─────────────────────────────────────────────────────────────

def test_limits_are_clamped():
    """LIMIT -1 is unbounded in SQLite, so a negative limit must fall back."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/hls.db")
        tr.check(db.clamp(-1, 50) == 50, "negative limit falls back to the default")
        tr.check(db.clamp(0, 50) == 50, "zero limit falls back to the default")
        tr.check(db.clamp("many", 50) == 50, "non-numeric limit falls back to the default")
        tr.check(db.clamp(10**9, 50) == db.MAX_LIMIT, f"huge limit capped at {db.MAX_LIMIT}")
        tr.check(db.clamp_offset(-3) == 0, "negative offset clamps to 0")

        tr.check(len(db.list_persons(-1)) == 3, "list_persons(-1) is bounded, not unbounded")
        tr.check(len(db.list_articles_by_category("bio", -1)) == 3,
                 "list_articles_by_category(-1) is bounded")
        tr.check(len(db.search_persons("e", limit=-1)) <= db.MAX_LIMIT,
                 "search_persons(-1) is bounded")
        first, second = db.list_persons(2, 0), db.list_persons(2, 2)
        tr.check({r["id"] for r in first}.isdisjoint({r["id"] for r in second}),
                 "list_persons pages without overlap")
    tr.assert_ok()


def test_like_wildcards_are_escaped():
    """A '%' or '_' in a search query must match itself, not act as a wildcard."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/hls.db")
        names = lambda rows: sorted(r["family_name"] for r in rows)

        tr.check(names(db.search_persons("%")) == ["Sicher"],
                 "'%' matches a literal percent, not every row")
        tr.check(names(db.search_persons("_")) == ["Hans_Meier"],
                 "'_' matches a literal underscore, not any character")
        tr.check(db.search_persons("%Zwingli%") == [],
                 "caller-supplied wildcards do not expand")
        tr.check(names(db.search_persons("Zwingli")) == ["Zwingli"],
                 "ordinary substring search still works")
        tr.check(len(db.list_articles_by_lexical_class("%")) == 0,
                 "list_articles_by_lexical_class escapes wildcards too")
        tr.check(db.like_pattern("a%b_c") == "%a\\%b\\_c%", "like_pattern escapes both wildcards")
    tr.assert_ok()


def test_fulltext_survives_hostile_queries():
    """FTS5 syntax errors must degrade to a literal search, not raise."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/hls.db")
        hits = db.search_articles("Reformator")
        tr.check(hits and hits[0]["id"] == "001398", f"plain query finds the article ({hits[:1]})")
        for q in ['Zwingli"', "Brugg AND", "(unbalanced", "Refor*", "%"]:
            try:
                res = db.search_articles(q, 5)
                tr.check(isinstance(res, list), f"search_articles({q!r}) returned a list")
            except Exception as e:
                tr.check(False, f"search_articles({q!r}) raised {type(e).__name__}: {e}")
        tr.check("error" in db.search_articles("")[0], "an empty query is reported as an error")
    tr.assert_ok()


def test_year_filter_applies_before_paging():
    """The overlap filter must decide the result set, not the page that happens to
    sort first — the old implementation paged first and filtered second."""
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/hls.db")
        ids = lambda rows: sorted(r["id"] for r in rows)

        tr.check(ids(db.list_articles_by_year(1480, 1490)) == ["001398"],
                 "a narrow range returns only the overlapping article")
        tr.check(ids(db.list_articles_by_year(1600, 1800)) == ["001400", "001401"],
                 "a wider range returns every overlapping article")
        tr.check(db.list_articles_by_year(1000, 1100) == [],
                 "a range outside every span returns nothing")
        # With limit=1 the filter must still be applied to the whole candidate set:
        # paging first would return the first row by time_span order ('1284-'), which
        # does not overlap 1480–1490 at all.
        page = db.list_articles_by_year(1480, 1490, limit=1)
        tr.check(len(page) == 1 and page[0]["id"] == "001398",
                 f"limit=1 still returns a matching article (got {page})")
        tr.check(db.list_articles_by_year(1600, 1800, limit=1, offset=1)[0]["id"] == "001401",
                 "offset pages within the filtered set")
    tr.assert_ok()


def test_connection_is_read_only():
    tr = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        db = make_fixture_db(f"{tmp}/hls.db")
        with db.conn() as c:
            try:
                c.execute("DELETE FROM articles")
                tr.check(False, "a write through db.conn() was accepted")
            except sqlite3.OperationalError as e:
                tr.check(True, f"writes are rejected ({e})")
        tr.check(db.stats()["n_articles"] == 4, "corpus is intact after the attempted write")
    tr.assert_ok()


def test_server_module_registers_tools():
    """server.py must import without reading sys.argv, and expose every tool."""
    pytest.importorskip("mcp", reason="mcp SDK not installed")
    import anyio
    import server as server_module

    tr = Checks()
    expected = {"corpus_stats", "search_articles", "get_article", "list_articles_by_category",
                "list_articles_by_year", "list_persons", "search_persons", "get_person"}
    names = {t.name for t in anyio.run(server_module.mcp.list_tools)}
    tr.check(not expected - names, f"all tools registered (missing: {sorted(expected - names)})")

    n = server_module.normalise_path
    tr.check(n("/mcp/hls/mcp") == "/mcp/hls/mcp", "an already-correct path is unchanged")
    tr.check(n("mcp/hls/mcp/") == "/mcp/hls/mcp", "slashes are normalised")
    tr.check(n("") == "/mcp" and n(None) == "/mcp", "an empty path falls back to /mcp")
    tr.check(server_module.parse_args([]).http_path == "/mcp", "default endpoint path is /mcp")
    tr.check(server_module.parse_args(["--http-path", "mcp/hls/mcp/"]).http_path
             == "/mcp/hls/mcp", "--http-path is normalised on the way in")
    tr.assert_ok()


# ── 2. DB tests — against the real corpus ─────────────────────────────────────

def test_db_layer_against_real_db(db_path):
    if not db_path or not os.path.exists(db_path):
        pytest.skip(f"hls.db not found at {db_path!r} — set HLS_DB or pass --db")

    import db as db_module
    db_module.set_db_path(db_path)
    tr = Checks()

    s = db_module.stats()
    info(f"articles={s['n_articles']} persons={s['n_persons']} text={s['text_mb']} MB")
    tr.check(s["n_articles"] > 1000, f"articles: >1000 (got {s['n_articles']})")
    tr.check(len(db_module.list_persons(-1)) <= db_module.MAX_LIMIT, "list_persons(-1) is bounded")

    for q in ["Zwingli", "%", "_", 'quote"mark', "Bern AND", "Brugg OR Bern"]:
        try:
            tr.check(isinstance(db_module.search_articles(q, 5), list),
                     f"search_articles({q!r}) returned a list")
            tr.check(isinstance(db_module.search_persons(q, 5), list),
                     f"search_persons({q!r}) returned a list")
        except Exception as e:
            tr.check(False, f"{q!r} raised {type(e).__name__}: {e}")

    years = db_module.list_articles_by_year(1500, 1600, limit=5)
    tr.check(isinstance(years, list), f"list_articles_by_year returned {len(years)} rows")
    tr.assert_ok()


# ── 3. Server integration test ────────────────────────────────────────────────

def _tool_payload(result):
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured.get("result", structured)
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            try: return json.loads(text)
            except json.JSONDecodeError: return text
    return None


def test_server(base_url):
    """Drive the running server over streamable HTTP using the official client."""
    if not base_url:
        pytest.skip("no server URL — set HLS_SERVER or pass --server")
    try:
        import anyio
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as e:
        pytest.skip(f"mcp client library not available: {e}")

    tr = Checks()
    url = base_url.rstrip("/")
    if url.rsplit("/", 1)[-1] != "mcp":
        url += "/mcp"

    async def exercise():
        async with streamable_http_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                names = {t.name for t in (await session.list_tools()).tools}
                tr.check("corpus_stats" in names, f"tools/list returned {len(names)} tools")

                stats = _tool_payload(await session.call_tool("corpus_stats", {}))
                tr.check(isinstance(stats, dict) and stats.get("n_articles", 0) > 0,
                         f"corpus_stats returns articles (got {stats})")

                for tool, args in [
                    ("search_articles", {"query": "Zwingli", "limit": 3}),
                    ("search_persons",  {"query": "Anna", "limit": 3}),
                    ("list_persons",    {"limit": 3}),
                    ("list_articles_by_year", {"year_from": 1500, "year_to": 1600, "limit": 3}),
                ]:
                    res = await session.call_tool(tool, args)
                    tr.check(not res.is_error, f"{tool} call succeeded")

                res = await session.call_tool("search_articles", {"query": 'Zwingli"', "limit": 3})
                tr.check(not res.is_error, "search_articles survives an unbalanced quote")

                payload = _tool_payload(await session.call_tool("get_article",
                                                               {"article_id": "definitely_not_an_id"}))
                tr.check(isinstance(payload, dict) and "error" in payload,
                         f"unknown article id returns an error object (got {payload})")

                resources = {str(r.uri) for r in (await session.list_resources()).resources}
                tr.check("hls://stats" in resources, f"hls://stats listed (got {sorted(resources)})")

    anyio.run(exercise)
    tr.assert_ok()


# ── CLI ───────────────────────────────────────────────────────────────────────

def cli_run(label, fn, *fn_args):
    print(f"\n{label}")
    try:
        fn(*fn_args)
        return True
    except pytest.skip.Exception as e:
        warn(f"skipped: {e}")
        return True
    except AssertionError as e:
        fail(str(e))
        return False
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HLS MCP test suite")
    ap.add_argument("--unit", action="store_true", help="Run unit tests")
    ap.add_argument("--db", default=os.environ.get("HLS_DB", ""), help="Path to hls.db")
    ap.add_argument("--server", default=os.environ.get("HLS_SERVER", ""), help="Server URL")
    args = ap.parse_args()

    if not args.unit and not args.db and not args.server:
        ap.print_help(); sys.exit(0)

    print(f"{'═'*50}\nHLS MCP test suite\n{'═'*50}")
    ok_all = True

    if args.unit:
        ok_all &= cli_run("[1] Unit: limits are clamped", test_limits_are_clamped)
        ok_all &= cli_run("[2] Unit: LIKE wildcards are escaped", test_like_wildcards_are_escaped)
        ok_all &= cli_run("[3] Unit: full-text survives hostile queries",
                          test_fulltext_survives_hostile_queries)
        ok_all &= cli_run("[4] Unit: year filter applies before paging",
                          test_year_filter_applies_before_paging)
        ok_all &= cli_run("[5] Unit: connection is read-only", test_connection_is_read_only)
        ok_all &= cli_run("[6] Unit: server registers its tools", test_server_module_registers_tools)

    if args.db:
        ok_all &= cli_run(f"[7] DB: query layer ({args.db})", test_db_layer_against_real_db, args.db)

    if args.server:
        ok_all &= cli_run(f"[8] Server: MCP integration ({args.server})", test_server, args.server)

    print(f"\n{'═'*50}")
    print(f"{GREEN}ALL PASSED{RESET}" if ok_all else f"{RED}FAILURES{RESET}")
    sys.exit(0 if ok_all else 1)
