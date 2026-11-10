"""MCP tool registrations for bcb-mcp."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from .config import DEFAULT_MAX_ROWS, DEFAULT_SEARCH_RESULTS
from .service import SeriesService, parse_date

SeriesIdArg = Annotated[
    list[int | str],
    Field(
        description=(
            "SGS series identifiers. Each item may be a numeric SGS id (11) or a "
            "dictionary alias in English/Portuguese ('selic', 'ipca', 'desemprego', 'dolar'). "
            "Use search_series() when unsure."
        ),
        min_length=1,
        max_length=50,
    ),
]


def register_tools(mcp: FastMCP, service: SeriesService) -> None:
    """Attach every bcb-mcp tool to the FastMCP instance."""

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    async def search_series(
        query: Annotated[
            str,
            Field(
                description=(
                    "Free-text search, e.g. 'unemployment', 'selic', 'desemprego', "
                    "'consumer prices', 'public debt', 'credito'."
                )
            ),
        ],
        max_results: Annotated[
            int,
            Field(description="Maximum number of matches to return (1..500).", ge=1, le=500),
        ] = DEFAULT_SEARCH_RESULTS,
        include_online: Annotated[
            bool,
            Field(
                description=(
                    "Also query the live BCB-SGS catalog (English names). "
                    "Set to false for an offline, dictionary-only search."
                )
            ),
        ] = True,
        category: Annotated[
            str | None,
            Field(
                description=(
                    "Optional dictionary category filter, e.g. 'inflation', 'labor', "
                    "'external_sector', 'fiscal', 'credit', 'activity'."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Find BCB-SGS series ids from natural language.

        Searches the bundled curated dictionary (151 key Brazilian macro series,
        with Portuguese and English aliases) and, optionally, the official BCB
        catalog. Always call this before get_series() when you do not know the id.
        """
        return await service.search_series(
            query, max_results=max_results, include_online=include_online, category=category
        )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def list_categories() -> dict[str, Any]:
        """List the dictionary categories with descriptions and example series ids."""
        return service.list_categories()

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def list_series(
        category: Annotated[
            str | None,
            Field(description="Optional category key, e.g. 'inflation'. Omit to list everything."),
        ] = None,
        limit: Annotated[
            int,
            Field(description="Maximum number of series to list (1..500).", ge=1, le=500),
        ] = 200,
    ) -> dict[str, Any]:
        """Browse the curated dictionary, optionally restricted to one category."""
        return service.list_series(category=category, limit=limit)

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def describe_series(series_ids: SeriesIdArg) -> dict[str, Any]:
        """Show dictionary metadata (name, unit, frequency, source, description) and cache state."""
        return service.describe_series(series_ids)

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    async def get_series(
        series_ids: SeriesIdArg,
        first_date: Annotated[
            str | None,
            Field(
                description=(
                    "Start date (YYYY-MM-DD or DD/MM/YYYY). Defaults to five years before last_date."
                )
            ),
        ] = None,
        last_date: Annotated[
            str | None,
            Field(description="End date (YYYY-MM-DD or DD/MM/YYYY). Defaults to today."),
        ] = None,
        format_data: Annotated[
            Literal["long", "wide"],
            Field(
                description=(
                    "'long' (default) returns one row per observation with series_id/"
                    "series_name columns; 'wide' pivots to one column per series."
                )
            ),
        ] = "long",
        use_cache: Annotated[
            bool,
            Field(
                description=(
                    "Reuse locally cached parquet data and download only missing date ranges. "
                    "Set to false to force a fresh download."
                )
            ),
        ] = True,
        max_rows: Annotated[
            int,
            Field(
                description=(
                    "Maximum observations kept per series (most recent are kept). "
                    "Defaults to 1000; raise it only when you really need the full history."
                ),
                ge=1,
                le=100_000,
            ),
        ] = DEFAULT_MAX_ROWS,
        series_names: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional display names, positionally aligned with series_ids "
                    "(like naming a vector in R's GetBCBData)."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Download BCB-SGS time series (the analogue of GetBCBData::gbcbd_get_series).

        Long spans are split into API-friendly chunks automatically, results are
        cached as parquet files, and only missing date ranges are downloaded on
        subsequent calls. Failed ids are reported in `failures` instead of aborting
        the whole request, exactly like the R package.

        Rows are compact: each one has ref_date, value and series_id. The name,
        unit, frequency and source of every series are in the `series` array, so
        pair rows with `series` before writing prose (units differ across ids).
        """
        return await service.get_series(
            series_ids,
            first_date=parse_date(first_date),
            last_date=parse_date(last_date),
            format_data=format_data,
            use_cache=use_cache,
            max_rows=max_rows,
            series_names=series_names,
        )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    async def get_series_stats(
        series_ids: SeriesIdArg,
        first_date: Annotated[
            str | None,
            Field(description="Start date (YYYY-MM-DD). Defaults to five years before last_date."),
        ] = None,
        last_date: Annotated[
            str | None,
            Field(description="End date (YYYY-MM-DD). Defaults to today."),
        ] = None,
        use_cache: Annotated[
            bool,
            Field(description="Reuse cached parquet data (default) or force a refresh."),
        ] = True,
    ) -> dict[str, Any]:
        """Compact summary of each series (last value, min, max, mean, change) without the raw rows.

        Use this when you need the numbers for a narrative answer and do not need
        the full observation history.
        """
        return await service.get_series_stats(
            series_ids,
            first_date=parse_date(first_date),
            last_date=parse_date(last_date),
            use_cache=use_cache,
        )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def query_cache(
        sql: Annotated[
            str,
            Field(
                description=(
                    "Read-only DuckDB SQL over the local parquet cache. Available views: "
                    "cached_series(ref_date, value, series_id, series_name, unit, frequency, "
                    "source, fetched_at), cached_queries (same columns), series_metadata("
                    "series_id, series_name, coverage, rows, updated_at)."
                )
            ),
        ],
        limit: Annotated[
            int,
            Field(description="Maximum rows returned (1..100000).", ge=1, le=100_000),
        ] = 1000,
    ) -> dict[str, Any]:
        """Run read-only SQL across every cached series (DuckDB over parquet files).

        Example: SELECT series_name, avg(value) FROM cached_series
        WHERE ref_date >= DATE '2020-01-01' GROUP BY 1 ORDER BY 2 DESC.
        """
        return service.query_cache(sql, limit=limit)

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def cache_status() -> dict[str, Any]:
        """Report what is cached locally: series, coverage ranges, row counts and disk usage."""
        return service.cache_status()

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": False, "destructiveHint": True})
    def clear_cache(
        series_ids: Annotated[
            list[int | str] | None,
            Field(
                description=(
                    "Series to evict (ids or aliases). Omit to wipe the whole cache "
                    "(parquet series, query files, catalog searches and metadata)."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Delete cached parquet files, either for selected series or everything."""
        return service.clear_cache(series_ids)

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def today() -> dict[str, str]:
        """Current date on the server, useful for building relative date ranges."""
        now = dt.date.today()
        return {
            "today": now.isoformat(),
            "one_year_ago": (now - dt.timedelta(days=365)).isoformat(),
            "five_years_ago": (now - dt.timedelta(days=365 * 5)).isoformat(),
        }
