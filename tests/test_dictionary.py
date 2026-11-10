"""Tests for the bundled series dictionary."""

from __future__ import annotations

import pytest

from bcb_mcp.dictionary import SeriesDictionary, get_dictionary, normalize


@pytest.fixture(scope="module")
def dictionary() -> SeriesDictionary:
    return get_dictionary()


def test_dictionary_is_not_empty(dictionary: SeriesDictionary) -> None:
    assert len(dictionary) >= 100
    assert dictionary.version
    assert dictionary.source


def test_every_entry_has_required_fields(dictionary: SeriesDictionary) -> None:
    for info in dictionary.all_series():
        assert isinstance(info.id, int) and info.id > 0
        assert info.name
        assert info.name_pt
        assert info.unit
        assert info.frequency
        assert info.category in dictionary.categories
        assert info.description


def test_ids_are_unique(dictionary: SeriesDictionary) -> None:
    ids = [info.id for info in dictionary.all_series()]
    assert len(ids) == len(set(ids))


def test_normalize_strips_accents_and_case() -> None:
    assert normalize("Desemprego") == "desemprego"
    assert normalize("  Inflação  ") == "inflacao"
    assert normalize("Dívida Bruta (%)") == "divida bruta"


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("selic", 11),
        ("taxa selic", 11),
        ("ipca", 433),
        ("inflacao", 433),
        ("desemprego", 24369),
        ("unemployment", 24369),
        ("dolar", 1),
        ("cambio", 1),
        ("pib", 22099),
        ("divida bruta", 13762),
        ("credito", 20539),
        ("ipca 12 meses", 13522),
        ("inflacao acumulada", 13522),
        ("nucleo", 11427),
        ("media aparada", 4466),
        ("divida publica", 4513),
        ("taxa de cambio", 1),
    ],
)
def test_search_finds_headline_series(
    dictionary: SeriesDictionary, query: str, expected_id: int
) -> None:
    hits = [info.id for info, _ in dictionary.search(query, limit=5)]
    assert expected_id in hits


def test_search_is_accent_insensitive(dictionary: SeriesDictionary) -> None:
    assert dictionary.search("inflacao")[0][0].id == dictionary.search("inflação")[0][0].id


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("núcleo da inflação", 11427),
        ("taxa de câmbio", 1),
        ("dívida pública", 4513),
        ("variação do ipca em 12 meses", 13522),
    ],
)
def test_search_handles_natural_portuguese(
    dictionary: SeriesDictionary, query: str, expected_id: int
) -> None:
    hits = [info.id for info, _ in dictionary.search(query, limit=5)]
    assert expected_id in hits


def test_resolve_natural_aliases(dictionary: SeriesDictionary) -> None:
    assert dictionary.resolve("taxa de câmbio").id == 1
    assert dictionary.resolve("dívida pública").id == 4513
    assert dictionary.resolve("ipca 12 meses").id == 13522


def test_search_respects_category(dictionary: SeriesDictionary) -> None:
    results = dictionary.search("precos", limit=50, category="inflation")
    assert results
    assert all(info.category == "inflation" for info, _ in results)


def test_search_empty_query_returns_all(dictionary: SeriesDictionary) -> None:
    results = dictionary.search("", limit=10)
    assert len(results) == 10


def test_resolve_accepts_ids_and_aliases(dictionary: SeriesDictionary) -> None:
    assert dictionary.resolve(11).id == 11
    assert dictionary.resolve("11").id == 11
    assert dictionary.resolve("selic").id == 11
    assert dictionary.resolve("SELIC").id == 11
    assert dictionary.resolve("not-a-series") is None


def test_categories_have_members(dictionary: SeriesDictionary) -> None:
    categories = dictionary.list_categories()
    assert len(categories) == len(dictionary.categories)
    for category in categories:
        assert category["series_count"] > 0
        assert category["example_ids"]
        assert category["description"]


def test_by_category(dictionary: SeriesDictionary) -> None:
    inflation = dictionary.by_category("inflation")
    assert inflation
    assert all(info.category == "inflation" for info in inflation)
    assert dictionary.by_category("does-not-exist") == []


def test_suggest_returns_ids(dictionary: SeriesDictionary) -> None:
    suggestions = dictionary.suggest("desemprego")
    assert suggestions and all(isinstance(item, int) for item in suggestions)
