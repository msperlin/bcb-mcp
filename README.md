# bcb-mcp

**MCP server for Brazilian Central Bank (BCB-SGS) time series** — the Python counterpart of the R package [`GetBCBData`](https://github.com/msperlin/GetBCBData).

It exposes the Central Bank of Brazil's SGS database (Sistema Gerenciador de Séries Temporais) to any MCP client (Claude Desktop, Cursor, Zed, VS Code, OpenCode, …) with three things the raw API does not give you:

1. **A curated dictionary** of ~150 key Brazilian macro series (inflation, rates, activity, labor, external sector, fiscal, credit, monetary, energy, commodities) with Portuguese **and** English aliases, so an LLM can go from "unemployment" or "desemprego" to SGS `24369` without guessing.
2. **A parquet cache** — every query is stored as a parquet file and served from disk on the next call, with only the missing date ranges downloaded.
3. **DuckDB SQL** over everything cached, so the model can join, aggregate and compare series locally.

Like `GetBCBData::gbcbd_get_series()`, it accepts vectors of ids, returns long or wide data, splits long histories into API-friendly chunks (the BCB API rejects requests spanning more than ~10 years), retries failed calls, and reports per-series failures instead of aborting.

---

## Installation

```bash
pip install bcb-mcp
# or
uvx bcb-mcp --stdio
```

From source:

```bash
git clone https://github.com/msperlin/bcb-mcp
cd bcb-mcp
uv venv && uv pip install -e .
```

## Quick start

### As an MCP server (STDIO)

```json
{
  "mcpServers": {
    "bcb": {
      "command": "uvx",
      "args": ["bcb-mcp", "--stdio"]
    }
  }
}
```

### As an HTTP server

```bash
bcb-mcp --http --host 127.0.0.1 --port 8000 --path /mcp
```

### Directly in Python

```python
import asyncio
from bcb_mcp import SeriesService


async def main():
    svc = SeriesService()
    df = await svc.get_series(["selic", 433], first_date="2020-01-01", format_data="wide")
    print(df["columns"], df["row_count"])
    await svc.close()


asyncio.run(main())
```

---

## Tools

| Tool | What it does |
| --- | --- |
| `search_series` | Natural language → SGS ids. Searches the bundled dictionary and (optionally) the live BCB catalog. |
| `list_categories` | Dictionary categories with descriptions and example ids. |
| `list_series` | Browse the dictionary, optionally by category. |
| `describe_series` | Name, unit, frequency, source, description, aliases and cache state for given ids. |
| `get_series` | Download observations. Long/wide, chunked, cached, per-series failure reporting. |
| `get_series_stats` | Compact summary (last value, mean, min, max, change) without the raw rows. |
| `query_cache` | Read-only DuckDB SQL over all cached parquet files. |
| `cache_status` | What is cached: series, coverage ranges, rows, disk usage. |
| `clear_cache` | Evict selected series or the whole cache. |
| `today` | Server date plus handy "one/five years ago" dates. |

### Output shape

`get_series` returns one compact row per observation (`ref_date`, `value`,
`series_id`). The name, unit, frequency and source of each series are returned
once in the `series` array, and `failures` lists the ids that could not be
fetched. Payload size matters for LLMs: the default `max_rows` is **1,000 per
series** (about 50k tokens of JSON), so use `get_series_stats` when you only need
the numbers, `format_data="wide"` to align series on dates, or raise `max_rows`
for the full history.

Cache freshness: a range is downloaded once, but requests that reach the present
revalidate the last `BCB_MCP_TAIL_REFRESH_DAYS` days at most every
`BCB_MCP_TAIL_REFRESH_MINUTES` minutes, so new observations and BCB revisions
show up without refetching on every call.

### Resources

- `bcb://dictionary/series` — the full curated dictionary (JSON).
- `bcb://dictionary/categories` — categories and counts.
- `bcb://cache/status` — local cache summary.

### Prompts

- `macro_briefing` — guided workflow for a short Brazilian macro briefing.
- `compare_series` — guided workflow to compare series over time.

---

## Example prompts

> "What is the latest Selic rate and the 12-month IPCA? Compare them since 2015."

> "Puxe a taxa de desemprego (PNAD Contínua) e o IBC-Br dos últimos 3 anos e diga se a atividade está acelerando."

> "Compare credit outstanding as a share of GDP with the average lending rate since 2015. Use SQL if needed."

The model will call `search_series("selic")` → `get_series_stats([432, 13522])` →
`get_series([432, 13522], format_data="wide")`, and can fall back to `query_cache`
for cross-series SQL. Note the units: `432` is the Selic target in `% p.y.`
(comparable to annual inflation), while `11` is the daily Selic in `% p.d.`.

---

## The dictionary

`src/bcb_mcp/data/series_dictionary.json` ships 151 hand-picked series, each with:

```json
{
  "id": 24369,
  "name": "Unemployment rate - PNADC",
  "name_pt": "Taxa de desocupacao - PNAD Continua",
  "category": "labor",
  "unit": "%",
  "frequency": "Monthly",
  "source": "IBGE",
  "description": "Unemployment rate from the Continuous PNAD survey.",
  "aliases": ["desemprego", "taxa de desemprego", "unemployment", "unemployment rate", "pnadc"]
}
```

Search is accent- and case-insensitive (`desemprego`, `Desemprego`, `DESEMPREGO`), matches English and Portuguese, ignores short function words (`da`, `de`, `of`, ...) in multi-word queries, and ranks exact alias hits above substring hits. The catalog includes the headline aggregates an LLM usually asks for first — IPCA in 12 months (`13522`), core IPCA (`11427`/`4466`), gross and net public debt (`13762`/`4513`) — plus Portuguese aliases such as `dívida pública` and `taxa de câmbio`. Anything not in the dictionary can still be found through the live BCB catalog search (English names), and any numeric id can be passed straight to `get_series`.

Regenerate the dictionary metadata from the official catalog:

```bash
python scripts/build_dictionary.py --check
```

---

## Cache layout

```
~/.cache/bcb-mcp/                     # or $BCB_MCP_CACHE_DIR
├── parquet/
│   ├── series/sgs_11.parquet         # consolidated, deduplicated series store
│   ├── queries/sgs_11__2024-01-01__2024-12-31.parquet
│   └── catalog/search_<hash>.parquet # cached catalog searches
├── meta/sgs_11.json                  # coverage ranges, rows, official metadata
└── .write.lock                       # cross-process lock for cache mutations
```

- Every executed query is written as its own parquet file (auditable, reusable).
- The consolidated store tracks **coverage intervals**: asking for 2010–2024 after having 2015–2024 downloads only 2010–2014.
- Writes use a cross-process file lock and unique temporary files, so several
  MCP clients (Claude Desktop, Cursor, OpenCode, …) can share the same cache
  directory safely.
- Requests that reach the present revalidate the last few days periodically
  (see the freshness settings below); future dates are clamped to today so a
  `last_date` in the future cannot mark data as permanently cached.
- All I/O goes through DuckDB; no pandas/pyarrow needed.
- Point the cache elsewhere with `--cache-dir` or `BCB_MCP_CACHE_DIR`.

### Querying the cache

```sql
SELECT series_name,
       date_trunc('year', ref_date) AS year,
       avg(value) AS avg_value
FROM cached_series
WHERE series_id IN (11, 433)
GROUP BY 1, 2
ORDER BY 1, 2;
```

Views available in `query_cache`: `cached_series`, `cached_queries`, `series_metadata`.

---

## Mapping from the R package

| GetBCBData | bcb-mcp |
| --- | --- |
| `gbcbd_get_series(id, first_date, last_date, format_data)` | `get_series(series_ids, first_date, last_date, format_data)` |
| `names(id) <- c("Selic" = 11)` | `series_names=["Selic"]` (or just use aliases) |
| `gbcbd_search_series("selic")` | `search_series("selic")` |
| `use_memoise = TRUE` | `use_cache=True` (default) — parquet instead of memoise |
| `cache_path` | `--cache-dir` / `BCB_MCP_CACHE_DIR` |
| `do_parallel = TRUE` | always concurrent, bounded by `BCB_MCP_MAX_CONCURRENCY` (default 4) |
| 3-year chunking | 10-year chunking (the API limit), same retry/backoff behaviour |
| long / wide data frames | long / wide JSON rows |

---

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BCB_MCP_CACHE_DIR` | `~/.cache/bcb-mcp` | Cache directory. |
| `BCB_MCP_TIMEOUT` | `60` | HTTP timeout (seconds). |
| `BCB_MCP_MAX_RETRIES` | `3` | Retries per request (exponential backoff). |
| `BCB_MCP_MIN_INTERVAL` | `0.2` | Minimum seconds between requests. |
| `BCB_MCP_MAX_CONCURRENCY` | `4` | Concurrent series downloads. |
| `BCB_MCP_MAX_ROWS` | `1000` | Default observations kept per series. |
| `BCB_MCP_TAIL_REFRESH_DAYS` | `3` | Days revalidated when a request reaches the present (0 disables). |
| `BCB_MCP_TAIL_REFRESH_MINUTES` | `60` | How old the cached copy must be before the tail is revalidated. |

---

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest --cov=bcb_mcp --cov-report=term-missing
ruff check . && ruff format --check .
```

Tests mock HTTP with `respx`; no network access is required.

## Releasing to PyPI

The package is ready to publish. Two options:

1. **Trusted publishing (recommended, no token).** Create the project on PyPI as a
   *pending publisher* for `bcb-mcp` with workflow `publish.yml` and environment
   `pypi`, then publish a GitHub release. The workflow builds the sdist/wheel and
   uploads them with OIDC.
2. **Manual.** `uv build && uv publish` (needs `UV_PUBLISH_TOKEN` or a
   `~/.pypirc`).

Before tagging, bump the version in `pyproject.toml` and `src/bcb_mcp/config.py`
and add a `CHANGELOG.md` entry.


## Data source and disclaimer

Data comes from the public BCB SGS endpoints (`api.bcb.gov.br` and `www3.bcb.gov.br`). Series may be revised or discontinued by the Central Bank. This project is not affiliated with the Banco Central do Brasil. Use at your own risk.

## License

MIT — see [LICENSE](LICENSE).
