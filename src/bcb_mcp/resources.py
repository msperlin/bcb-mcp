"""MCP resources and prompts for bcb-mcp."""

from __future__ import annotations

import json

from fastmcp import FastMCP

from .service import SeriesService


def register_resources_and_prompts(mcp: FastMCP, service: SeriesService) -> None:
    """Expose the dictionary and cache state as MCP resources, plus usage prompts."""

    @mcp.resource(
        "bcb://dictionary/categories",
        name="SGS dictionary categories",
        description="Categories of the bundled BCB-SGS series dictionary.",
        mime_type="application/json",
    )
    def dictionary_categories() -> str:
        return json.dumps(service.list_categories(), ensure_ascii=False, indent=1)

    @mcp.resource(
        "bcb://dictionary/series",
        name="SGS series dictionary",
        description=(
            "Full curated dictionary of Brazilian macro series (id, name, unit, frequency, "
            "source, description, aliases)."
        ),
        mime_type="application/json",
    )
    def dictionary_series() -> str:
        return json.dumps(
            {
                "version": service.dictionary.version,
                "generated_at": service.dictionary.generated_at,
                "source": service.dictionary.source,
                "series": [info.to_dict() for info in service.dictionary.all_series()],
            },
            ensure_ascii=False,
            indent=1,
        )

    @mcp.resource(
        "bcb://cache/status",
        name="Local parquet cache status",
        description="What is cached locally: series, coverage, rows and disk usage.",
        mime_type="application/json",
    )
    def cache_status() -> str:
        status = service.cache_status()
        status.pop("series", None)  # keep the resource small
        return json.dumps(status, ensure_ascii=False, indent=1)

    @mcp.prompt(
        name="macro_briefing",
        description="Build a short Brazilian macro briefing from a set of SGS series.",
    )
    def macro_briefing(
        theme: str = "inflacao, juros e atividade",
        period_years: int = 2,
    ) -> str:
        return (
            f"You are a macroeconomist writing a briefing about: {theme}.\n\n"
            "Workflow:\n"
            "1. Call search_series() for each concept you need (Portuguese or English).\n"
            "2. Call get_series_stats() with the last "
            f"{period_years} years of data to get the key numbers cheaply.\n"
            "3. Call get_series() only for the series you want to chart or inspect monthly.\n"
            "4. Cite the SGS id, the unit and the reference period for every number.\n\n"
            "Rules:\n"
            "- Never invent values: every figure must come from a tool result.\n"
            "- Mention the frequency (daily/monthly/quarterly) and the last observation date.\n"
            "- If a series is missing from the dictionary, use the live BCB catalog search.\n"
        )

    @mcp.prompt(
        name="compare_series",
        description="Compare two or more SGS series over time using the local cache.",
    )
    def compare_series(series: str = "selic, ipca", start: str = "2015-01-01") -> str:
        return (
            f"Compare these SGS series since {start}: {series}.\n\n"
            "1. search_series() to confirm the ids.\n"
            "2. get_series(..., format_data='wide') to align them on the same dates.\n"
            "3. If you need cross-series SQL (correlations, yearly averages), call "
            "query_cache() with the cached_series view.\n"
            "4. Report units explicitly and note that daily and monthly series are "
            "not directly comparable without aggregation.\n"
        )
