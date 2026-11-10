"""Tests for the BCB HTTP client (parsing, chunking, retries) without real network."""

from __future__ import annotations

import datetime as dt
import itertools

import httpx
import pytest
import respx

from bcb_mcp.bcb_client import (
    BCBClient,
    BCBError,
    SeriesNotFound,
    parse_bcb_date,
    parse_search_page,
    split_date_range,
)

DATA_URL_RE = r"https://api\.bcb\.gov\.br/dados/serie/bcdata\.sgs\.(\d+)/dados"

SEARCH_HTML = """
<span class="textoPequeno">
    Amount of series found:&nbsp;
    <b>2</b>
</span>
<table class="tabela" id='tabelaSeries' border=0 width='100%'>
<tr class='fundoPadraoAClaro3' style="font: 8pt;">
 <td align="center"><input type="checkbox" name="cbxSelecionaSerie" value="11" /></td>
 <td align="center">11</td>
 <td align="left">Interest rate - <font color=red>Selic</font></td>
 <td align="center">% p.d.</td>
 <td align="center"><span class="txtMenor1" title="Daily">D</span></td>
 <td align="center">04/06/1986</td>
 <td align="center">09/11/2026</td>
 <td>BCB-Demab</td>
 <td align="center">N</td>
</tr>
<tr class='fundoPadraoAClaro3' style="font: 8pt;">
 <td align="center"><input type="checkbox" name="cbxSelecionaSerie" value="432" /></td>
 <td align="center">432</td>
 <td align="left">Interest rate - Selic target</td>
 <td align="center">% p.y.</td>
 <td align="center"><span class="txtMenor1" title="Daily">D</span></td>
 <td align="center">05/03/1999</td>
 <td align="center">09/12/2026</td>
 <td>Copom</td>
 <td align="center">N</td>
</tr>
</table>
"""


def test_parse_bcb_date() -> None:
    assert parse_bcb_date("09/11/2026") == dt.date(2026, 11, 9)


def test_split_date_range_short_span() -> None:
    chunks = split_date_range(dt.date(2024, 1, 1), dt.date(2024, 12, 31))
    assert chunks == [(dt.date(2024, 1, 1), dt.date(2024, 12, 31))]


def test_split_date_range_long_span() -> None:
    chunks = split_date_range(dt.date(1986, 1, 1), dt.date(2026, 1, 1))
    assert len(chunks) >= 4
    assert chunks[0][0] == dt.date(1986, 1, 1)
    assert chunks[-1][1] == dt.date(2026, 1, 1)
    for (_, end), (next_start, _) in itertools.pairwise(chunks):
        assert next_start == end + dt.timedelta(days=1)
        assert (end - next_start).days < 0


def test_split_date_range_rejects_inverted_range() -> None:
    with pytest.raises(ValueError):
        split_date_range(dt.date(2024, 1, 2), dt.date(2024, 1, 1))


def test_parse_search_page() -> None:
    rows, total = parse_search_page(SEARCH_HTML)
    assert total == 2
    assert [row["id"] for row in rows] == [11, 432]
    assert rows[0]["name"] == "Interest rate - Selic"
    assert rows[0]["unit"] == "% p.d."
    assert rows[0]["frequency"] == "Daily"
    assert rows[1]["source"] == "Copom"


def test_parse_search_page_without_table() -> None:
    rows, total = parse_search_page("<html>nothing here</html>")
    assert rows == []
    assert total == 0


@respx.mock
async def test_fetch_series_parses_values() -> None:
    respx.get(url__regex=DATA_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"data": "02/01/2024", "valor": "0.043739"},
                {"data": "03/01/2024", "valor": ""},
            ],
        )
    )
    client = BCBClient(min_interval=0)
    try:
        rows = await client.fetch_series_range(11, dt.date(2024, 1, 1), dt.date(2024, 1, 5))
    finally:
        await client.close()
    assert rows == [(dt.date(2024, 1, 2), 0.043739), (dt.date(2024, 1, 3), None)]


@respx.mock
async def test_fetch_series_chunks_long_ranges() -> None:
    route = respx.get(url__regex=DATA_URL_RE).mock(
        return_value=httpx.Response(200, json=[{"data": "02/01/2024", "valor": "1.0"}])
    )
    client = BCBClient(min_interval=0)
    try:
        await client.fetch_series(11, dt.date(2000, 1, 1), dt.date(2024, 1, 1))
    finally:
        await client.close()
    assert route.call_count >= 3


@respx.mock
async def test_fetch_series_raises_on_api_error() -> None:
    respx.get(url__regex=DATA_URL_RE).mock(
        return_value=httpx.Response(200, json={"error": "endpoint not found!"})
    )
    client = BCBClient(min_interval=0, max_retries=1)
    try:
        with pytest.raises(BCBError, match="endpoint not found"):
            await client.fetch_series_range(999999, dt.date(2024, 1, 1), dt.date(2024, 2, 1))
    finally:
        await client.close()


@respx.mock
async def test_fetch_series_404_raises_series_not_found() -> None:
    respx.get(url__regex=DATA_URL_RE).mock(return_value=httpx.Response(404))
    client = BCBClient(min_interval=0, max_retries=1)
    try:
        with pytest.raises(SeriesNotFound):
            await client.fetch_series_range(123, dt.date(2024, 1, 1), dt.date(2024, 2, 1))
    finally:
        await client.close()


@respx.mock
async def test_fetch_series_retries_server_errors() -> None:
    route = respx.get(url__regex=DATA_URL_RE)
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(200, json=[{"data": "02/01/2024", "valor": "2.0"}]),
    ]
    client = BCBClient(min_interval=0, max_retries=2)
    try:
        rows = await client.fetch_series_range(11, dt.date(2024, 1, 1), dt.date(2024, 1, 5))
    finally:
        await client.close()
    assert rows == [(dt.date(2024, 1, 2), 2.0)]
    assert route.call_count == 2


@respx.mock
async def test_search_catalog_parses_pages() -> None:
    respx.get("https://www3.bcb.gov.br/sgspub/").mock(return_value=httpx.Response(200, text=""))
    respx.post(
        url__regex=r"https://www3\.bcb\.gov\.br/sgspub/localizarseries/localizarSeries\.do.*"
    ).mock(return_value=httpx.Response(200, content=SEARCH_HTML.encode("iso-8859-1")))
    client = BCBClient(min_interval=0)
    try:
        rows = await client.search_catalog("selic", max_results=10)
    finally:
        await client.close()
    assert [row["id"] for row in rows] == [11, 432]
