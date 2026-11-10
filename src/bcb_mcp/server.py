"""FastMCP server entrypoint for bcb-mcp (STDIO, Streamable HTTP and SSE)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp import FastMCP

from .cache import ParquetCache
from .config import APP_NAME, __version__, get_cache_dir
from .resources import register_resources_and_prompts
from .service import SeriesService
from .tools import register_tools

logger = logging.getLogger("bcb_mcp")

INSTRUCTIONS = (
    "Brazilian macro and financial time series from the Central Bank of Brazil (BCB-SGS). "
    "Start with search_series() to translate a concept into an SGS id (the bundled "
    "dictionary understands Portuguese and English, and can also query the live BCB "
    "catalog). Then call get_series() for the observations or get_series_stats() for a "
    "compact summary. Long histories are fetched in chunks, cached as parquet files and "
    "served from disk on later calls; query_cache() runs read-only DuckDB SQL over "
    "everything cached. Units matter: rates are percentages, credit and fiscal series "
    "are usually R$ million, external sector series are US$ million. Always report the "
    "unit and the last observation date."
)


def create_mcp_server(
    cache_dir: str | Path | None = None,
) -> tuple[FastMCP, SeriesService]:
    """Build a configured FastMCP instance and its service."""
    cache = ParquetCache(cache_dir)
    service = SeriesService(cache=cache)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await service.close()

    mcp = FastMCP(
        APP_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        website_url="https://github.com/msperlin/bcb-mcp",
        lifespan=lifespan,
    )
    register_tools(mcp, service)
    register_resources_and_prompts(mcp, service)
    return mcp, service


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="MCP server for Brazilian Central Bank (BCB-SGS) time series.",
    )

    transport = parser.add_mutually_exclusive_group()
    transport.add_argument(
        "--stdio",
        action="store_true",
        help="Run over STDIO transport (default when launched by an MCP client).",
    )
    transport.add_argument(
        "--http",
        action="store_true",
        help="Run over Streamable HTTP transport.",
    )
    transport.add_argument(
        "--sse",
        action="store_true",
        help="Run over legacy SSE transport.",
    )

    parser.add_argument(
        "--host",
        default=os.getenv("MCP_HOST", "127.0.0.1"),
        help="HTTP/SSE bind host (default: 127.0.0.1 or $MCP_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MCP_PORT", "8000")),
        help="HTTP/SSE bind port (default: 8000 or $MCP_PORT).",
    )
    parser.add_argument(
        "--path",
        default=os.getenv("MCP_PATH", "/mcp"),
        help="Streamable HTTP endpoint path (default: /mcp or $MCP_PATH).",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Override the cache directory (default: $BCB_MCP_CACHE_DIR or ~/.cache/bcb-mcp).",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level written to stderr (default: INFO).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args, unknown = parser.parse_known_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )

    if unknown:
        logger.warning("Ignoring unrecognised CLI args: %s", unknown)

    if args.cache_dir:
        os.environ["BCB_MCP_CACHE_DIR"] = args.cache_dir
    cache_dir = get_cache_dir(args.cache_dir)

    mcp, service = create_mcp_server(cache_dir)
    logger.info(
        "bcb-mcp %s | dictionary: %d series | cache: %s",
        __version__,
        len(service.dictionary),
        cache_dir,
    )

    run_http = bool(args.http)
    run_sse = bool(args.sse)

    try:
        if run_http:
            logger.info(
                "Starting %s (Streamable HTTP) on http://%s:%d%s",
                APP_NAME,
                args.host,
                args.port,
                args.path,
            )
            mcp.run(
                transport="streamable-http",
                host=args.host,
                port=args.port,
                path=args.path,
                show_banner=False,
            )
        elif run_sse:
            logger.info("Starting %s (SSE) on http://%s:%d", APP_NAME, args.host, args.port)
            mcp.run(transport="sse", host=args.host, port=args.port, show_banner=False)
        else:
            logger.info("Starting %s (STDIO)", APP_NAME)
            mcp.run(transport="stdio", show_banner=False)
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown requested.")
        return 0
    except Exception:  # pragma: no cover - defensive
        logger.exception("Fatal error in %s", APP_NAME)
        return 1


def cli_main() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli_main()
