"""Smoke tests for the MCP server wiring (tools, resources, prompts)."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from bcb_mcp.server import build_arg_parser, create_mcp_server


@pytest.fixture
def server(tmp_path):
    mcp, service = create_mcp_server(tmp_path / "cache")
    return mcp, service


async def test_tools_are_registered(server) -> None:
    mcp, _ = server
    async with Client(mcp) as client:
        tools = {tool.name for tool in await client.list_tools()}
    expected = {
        "search_series",
        "list_categories",
        "list_series",
        "describe_series",
        "get_series",
        "get_series_stats",
        "query_cache",
        "cache_status",
        "clear_cache",
        "today",
    }
    assert expected <= tools


async def test_resources_and_prompts(server) -> None:
    mcp, _ = server
    async with Client(mcp) as client:
        resources = {str(resource.uri) for resource in await client.list_resources()}
        prompts = {prompt.name for prompt in await client.list_prompts()}
    assert {
        "bcb://dictionary/series",
        "bcb://dictionary/categories",
        "bcb://cache/status",
    } <= resources
    assert {"macro_briefing", "compare_series"} <= prompts


async def test_call_list_categories(server) -> None:
    mcp, _ = server
    async with Client(mcp) as client:
        result = await client.call_tool("list_categories", {})
    payload = json.loads(result.content[0].text)
    assert payload["total_series"] > 100
    assert payload["categories"]


async def test_call_describe_series(server) -> None:
    mcp, _ = server
    async with Client(mcp) as client:
        result = await client.call_tool("describe_series", {"series_ids": ["selic"]})
    payload = json.loads(result.content[0].text)
    assert payload["series"][0]["id"] == 11


async def test_call_search_series_offline(server) -> None:
    mcp, _ = server
    async with Client(mcp) as client:
        result = await client.call_tool(
            "search_series", {"query": "desemprego", "include_online": False}
        )
    payload = json.loads(result.content[0].text)
    assert payload["results"][0]["id"] == 24369


async def test_call_today(server) -> None:
    mcp, _ = server
    async with Client(mcp) as client:
        result = await client.call_tool("today", {})
    payload = json.loads(result.content[0].text)
    assert "today" in payload


def test_cli_parser_defaults() -> None:
    args = build_arg_parser().parse_args([])
    assert args.http is False
    assert args.stdio is False
    assert args.host == "127.0.0.1"
    assert args.path == "/mcp"


def test_cli_parser_transports() -> None:
    assert build_arg_parser().parse_args(["--http"]).http is True
    assert build_arg_parser().parse_args(["--sse"]).sse is True
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--http", "--sse"])
