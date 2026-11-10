"""Async clients for the two BCB endpoints used by bcb-mcp.

1. Data API (JSON):  https://api.bcb.gov.br/dados/serie/bcdata.sgs.{id}/dados
2. SGS catalog search (HTML form): https://www3.bcb.gov.br/sgspub/...

Both are public and unauthenticated, but the data API caps a single request at
about ten years of daily data, so long spans are fetched in chunks.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html as html_lib
import logging
import re
import time
from typing import Any

import httpx

from .config import (
    MAX_DAYS_PER_REQUEST,
    MAX_RETRIES,
    MIN_REQUEST_INTERVAL,
    SGS_DATA_URL,
    SGS_SEARCH_HOME,
    SGS_SEARCH_URL,
    USER_AGENT,
)

logger = logging.getLogger("bcb_mcp.client")

DATE_FMT_BCB = "%d/%m/%Y"


class BCBError(RuntimeError):
    """Raised when the BCB API returns an unrecoverable error."""


class SeriesNotFound(BCBError):
    """Raised when the SGS id does not exist or has no data in the range."""


def parse_bcb_date(text: str) -> dt.date:
    """Parse the dd/mm/yyyy dates returned by the data API."""
    return dt.datetime.strptime(text.strip(), DATE_FMT_BCB).date()


def split_date_range(
    first_date: dt.date, last_date: dt.date, max_days: int = MAX_DAYS_PER_REQUEST
) -> list[tuple[dt.date, dt.date]]:
    """Split a range into chunks that the BCB API accepts."""
    if last_date < first_date:
        raise ValueError("last_date must be >= first_date")
    chunks: list[tuple[dt.date, dt.date]] = []
    start = first_date
    while start <= last_date:
        end = min(start + dt.timedelta(days=max_days), last_date)
        chunks.append((start, end))
        start = end + dt.timedelta(days=1)
    return chunks


class _RateLimiter:
    """Serialises requests so we never hammer the BCB servers."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, min_interval)
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def wait(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            elapsed = time.monotonic() - self._last_call
            delay = self._min_interval - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_call = time.monotonic()


class BCBClient:
    """Thin async wrapper around the BCB SGS endpoints."""

    def __init__(
        self,
        *,
        timeout: float = 60.0,
        max_retries: int = MAX_RETRIES,
        min_interval: float = MIN_REQUEST_INTERVAL,
        max_concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.max_retries = max(1, max_retries)
        self._limiter = _RateLimiter(min_interval)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html"},
            limits=httpx.Limits(max_connections=max_concurrency + 2),
        )

    async def close(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> BCBClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # -- data API ------------------------------------------------------------
    async def fetch_series_range(
        self, series_id: int, first_date: dt.date, last_date: dt.date
    ) -> list[tuple[dt.date, float | None]]:
        """Download one chunk of a series (no chunking logic here)."""
        url = SGS_DATA_URL.format(series_id=series_id)
        params = {
            "formato": "json",
            "dataInicial": first_date.strftime(DATE_FMT_BCB),
            "dataFinal": last_date.strftime(DATE_FMT_BCB),
        }
        payload = await self._get_json(url, params)
        if isinstance(payload, dict):
            message = payload.get("error") or payload.get("message") or str(payload)
            raise BCBError(f"SGS {series_id}: {message}")
        if not isinstance(payload, list):
            raise BCBError(f"SGS {series_id}: unexpected response {type(payload).__name__}")

        rows: list[tuple[dt.date, float | None]] = []
        for item in payload:
            if not isinstance(item, dict) or "data" not in item:
                continue
            raw_value = str(item.get("valor", "")).strip()
            value: float | None
            try:
                value = float(raw_value.replace(",", ".")) if raw_value not in ("", "-") else None
            except ValueError:
                value = None
            rows.append((parse_bcb_date(str(item["data"])), value))
        return rows

    async def fetch_series(
        self, series_id: int, first_date: dt.date, last_date: dt.date
    ) -> list[tuple[dt.date, float | None]]:
        """Download a series, splitting long spans into API-friendly chunks."""
        chunks = split_date_range(first_date, last_date)
        collected: dict[dt.date, float | None] = {}
        for start, end in chunks:
            for ref_date, value in await self.fetch_series_range(series_id, start, end):
                collected[ref_date] = value
        return sorted(collected.items())

    async def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                await self._limiter.wait()
                async with self._semaphore:
                    response = await self._client.get(url, params=params)
                if response.status_code == 404:
                    raise SeriesNotFound(f"BCB returned 404 for {url}")
                if response.status_code == 429 or response.status_code >= 500:
                    raise BCBError(f"HTTP {response.status_code} from BCB")
                response.raise_for_status()
                try:
                    return response.json()
                except ValueError:
                    text = response.text[:200]
                    raise BCBError(f"BCB returned non-JSON payload: {text!r}") from None
            except SeriesNotFound:
                raise
            except (httpx.HTTPError, BCBError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                wait = 2.0**attempt
                logger.warning("request failed (%s); retrying in %.1fs", exc, wait)
                await asyncio.sleep(wait)
        raise BCBError(f"failed to fetch {url}: {last_error}")

    # -- catalog search ------------------------------------------------------
    async def search_catalog(self, text: str, max_results: int = 50) -> list[dict[str, Any]]:
        """Search the official SGS catalog by text (English catalog names)."""
        await self._ensure_session()
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        page = 1
        total = 0
        while len(rows) < max_results:
            method = "localizarSeriesPorTexto" if page == 1 else "getPagina"
            html_text = await self._post_search(method, text, page)
            page_rows, total = parse_search_page(html_text)
            fresh = [r for r in page_rows if r["id"] not in seen]
            for row in fresh:
                seen.add(row["id"])
                rows.append(row)
            if not fresh or not total or len(rows) >= total or page >= 40:
                break
            page += 1
        return rows[:max_results]

    async def _ensure_session(self) -> None:
        """The legacy search service needs a session cookie."""
        try:
            await self._limiter.wait()
            async with self._semaphore:
                await self._client.get(SGS_SEARCH_HOME)
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            raise BCBError(f"could not start SGS search session: {exc}") from exc

    async def _post_search(self, method: str, text: str, page: int) -> str:
        data = {
            "texto": text,
            "hdTipoPesquisa": "6",
            "hdTipoOrdenacao": "0",
            "hdNumPagina": str(page),
            "bilQtde": "1",
            "periodicidade": "0",
            "fonte": "341",
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                await self._limiter.wait()
                async with self._semaphore:
                    response = await self._client.post(
                        SGS_SEARCH_URL, params={"method": method}, data=data
                    )
                response.raise_for_status()
                text_body = response.content.decode("iso-8859-1", errors="replace").replace(
                    "\x00", ""
                )
                if "Session expired" in text_body:
                    await self._ensure_session()
                    raise BCBError("SGS search session expired")
                return text_body
            except (httpx.HTTPError, BCBError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(2.0**attempt)
        raise BCBError(f"SGS catalog search failed: {last_error}")


# --- HTML parsing -----------------------------------------------------------

_TOTAL_RE = re.compile(r"Amount of series found:.*?<b>(\d+)</b>", re.S)
_TABLE_RE = re.compile(r"id=.tabelaSeries.", re.S)


def _clean(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", "", fragment)
    return " ".join(html_lib.unescape(text).split())


def parse_search_page(page_html: str) -> tuple[list[dict[str, Any]], int]:
    """Parse one HTML page of the SGS catalog search results."""
    total_match = _TOTAL_RE.search(page_html)
    total = int(total_match.group(1)) if total_match else 0

    parts = _TABLE_RE.split(page_html, maxsplit=1)
    if len(parts) < 2:
        return [], total

    rows: list[dict[str, Any]] = []
    for chunk in parts[1].split("<tr")[1:]:
        if "cbxSelecionaSerie" not in chunk:
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", chunk, re.S)
        if len(cells) < 8:
            continue
        id_match = re.search(r'value="(\d+)"', cells[0])
        freq_match = re.search(r'title="([^"]+)"', cells[4])
        rows.append(
            {
                "id": int(id_match.group(1)) if id_match else None,
                "name": _clean(cells[2]),
                "unit": _clean(cells[3]),
                "frequency": _clean(freq_match.group(1)) if freq_match else _clean(cells[4]),
                "first_date": _clean(cells[5]),
                "last_date": _clean(cells[6]),
                "source": _clean(cells[7]),
            }
        )
    return [row for row in rows if row["id"] is not None], total
