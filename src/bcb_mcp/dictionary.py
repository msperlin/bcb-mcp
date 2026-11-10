"""Curated dictionary of BCB-SGS series, with accent-insensitive search.

The dictionary ships as a JSON data file so that it can be regenerated from the
official BCB catalog without touching the code. It exists to help AI clients
(and humans) translate natural language into SGS series ids.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

DATA_FILE = "series_dictionary.json"


def normalize(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace."""
    if text is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    ascii_only = ascii_only.lower()
    ascii_only = re.sub(r"[^a-z0-9]+", " ", ascii_only)
    return " ".join(ascii_only.split())


@dataclass(frozen=True)
class SeriesInfo:
    """One dictionary entry."""

    id: int
    name: str
    name_pt: str
    category: str
    unit: str
    frequency: str
    source: str
    description: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "name_pt": self.name_pt,
            "category": self.category,
            "unit": self.unit,
            "frequency": self.frequency,
            "source": self.source,
            "description": self.description,
            "aliases": list(self.aliases),
        }

    @property
    def search_blob(self) -> str:
        parts = [self.name, self.name_pt, self.category, self.description, " ".join(self.aliases)]
        return normalize(" ".join(parts))


class SeriesDictionary:
    """In-memory index over the bundled SGS series dictionary."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        payload = payload or _load_payload()
        self.version: str = payload.get("version", "0")
        self.source: str = payload.get("source", "")
        self.source_url: str = payload.get("source_url", "")
        self.generated_at: str = payload.get("generated_at", "")
        self.categories: dict[str, dict[str, str]] = payload.get("categories", {})
        self._series: dict[int, SeriesInfo] = {}
        for raw in payload.get("series", []):
            info = SeriesInfo(
                id=int(raw["id"]),
                name=raw["name"],
                name_pt=raw.get("name_pt", raw["name"]),
                category=raw.get("category", "other"),
                unit=raw.get("unit", ""),
                frequency=raw.get("frequency", ""),
                source=raw.get("source", ""),
                description=raw.get("description", ""),
                aliases=tuple(raw.get("aliases", ())),
            )
            self._series[info.id] = info
        self._alias_index: dict[str, list[SeriesInfo]] = {}
        for info in self._series.values():
            for key in {normalize(a) for a in (info.name, info.name_pt, *info.aliases) if a}:
                self._alias_index.setdefault(key, []).append(info)

    # -- basic access --------------------------------------------------------
    def __len__(self) -> int:
        return len(self._series)

    def __contains__(self, series_id: object) -> bool:
        return _as_int(series_id) in self._series

    def get(self, series_id: int | str) -> SeriesInfo | None:
        return self._series.get(_as_int(series_id))

    def all_series(self) -> list[SeriesInfo]:
        return sorted(self._series.values(), key=lambda s: s.id)

    # -- categories ----------------------------------------------------------
    def list_categories(self) -> list[dict[str, Any]]:
        out = []
        for key, meta in self.categories.items():
            members = [s for s in self._series.values() if s.category == key]
            if not members:
                continue
            out.append(
                {
                    "category": key,
                    "label": meta.get("label", key),
                    "label_pt": meta.get("label_pt", ""),
                    "description": meta.get("description", ""),
                    "series_count": len(members),
                    "example_ids": [s.id for s in sorted(members, key=lambda s: s.id)[:5]],
                }
            )
        return sorted(out, key=lambda c: c["category"])

    def by_category(self, category: str) -> list[SeriesInfo]:
        needle = normalize(category).replace(" ", "_")
        matches = [
            s
            for s in self._series.values()
            if normalize(s.category).replace(" ", "_") == needle
            or needle in normalize(s.category).replace(" ", "_")
            or needle in normalize(self.categories.get(s.category, {}).get("label", ""))
        ]
        return sorted(matches, key=lambda s: s.id)

    # -- search --------------------------------------------------------------
    def search(
        self, query: str, limit: int = 25, category: str | None = None
    ) -> list[tuple[SeriesInfo, float]]:
        """Rank dictionary entries against a free-text query.

        Scoring is deliberately simple and explainable: exact alias matches beat
        prefix matches, which beat substring matches. All query tokens must be
        present somewhere in the entry (AND semantics).
        """
        needle = normalize(query)
        if not needle:
            pool = self.by_category(category) if category else self.all_series()
            return [(info, 1.0) for info in pool[:limit]]

        tokens = needle.split()
        cat_needle = normalize(category).replace(" ", "_") if category else None
        scored: list[tuple[SeriesInfo, float]] = []

        for info in self._series.values():
            if cat_needle and cat_needle not in normalize(info.category).replace(" ", "_"):
                continue
            blob = info.search_blob
            if not all(tok in blob for tok in tokens):
                continue

            score = 1.0
            names = {normalize(info.name), normalize(info.name_pt)}
            aliases = {normalize(a) for a in info.aliases}
            if needle in names:
                score += 10.0
            elif needle in aliases:
                score += 8.0
            elif any(name.startswith(needle) for name in names):
                score += 6.0
            elif any(alias.startswith(needle) for alias in aliases):
                score += 5.0
            elif any(needle in name for name in names):
                score += 4.0
            else:
                score += 2.0

            # reward entries whose whole alias set covers the query tokens early
            if all(any(tok in alias for alias in aliases) for tok in tokens):
                score += 1.5
            # prefer canonical (shorter, headline) series over long sub-components
            score -= min(len(info.name) / 400.0, 1.0)
            scored.append((info, round(score, 3)))

        scored.sort(key=lambda item: (-item[1], item[0].id))
        return scored[:limit]

    def resolve(self, value: int | str) -> SeriesInfo | None:
        """Resolve an id, a numeric string, or a (unique) alias to an entry."""
        as_int = _as_int(value)
        if as_int is not None and as_int in self._series:
            return self._series[as_int]
        if isinstance(value, str):
            needle = normalize(value)
            exact = self._alias_index.get(needle)
            if exact:
                return sorted(exact, key=lambda s: s.id)[0]
            hits = self.search(value, limit=1)
            if hits:
                return hits[0][0]
        return None

    def suggest(self, query: str, limit: int = 5) -> list[int]:
        return [info.id for info, _ in self.search(query, limit=limit)]


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _load_payload() -> dict[str, Any]:
    data = resources.files("bcb_mcp").joinpath(f"data/{DATA_FILE}").read_text(encoding="utf-8")
    return json.loads(data)


@lru_cache(maxsize=1)
def get_dictionary() -> SeriesDictionary:
    """Return the process-wide dictionary instance."""
    return SeriesDictionary()
