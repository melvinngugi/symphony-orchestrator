import logging
from types import SimpleNamespace

import pytest

from app.services import confluence as confluence_module
from app.services.confluence import ConfluenceClient


@pytest.fixture(autouse=True)
def confluence_settings(monkeypatch):
    monkeypatch.setattr(
        type(confluence_module.settings), "validate_confluence", lambda self: None
    )
    monkeypatch.setattr(
        confluence_module.settings,
        "CONFLUENCE_HOST",
        "https://example.atlassian.net",
    )
    monkeypatch.setattr(
        confluence_module.settings, "CONFLUENCE_USER_EMAIL", "bot@example.com"
    )
    monkeypatch.setattr(confluence_module.settings, "CONFLUENCE_API_TOKEN", "token")


def _response(payload, status_code=200):
    return SimpleNamespace(
        status_code=status_code,
        text="error" if status_code != 200 else "",
        json=lambda: payload,
    )


def _page(page_id, title="Strategy"):
    return {
        "id": page_id,
        "title": title,
        "body": {"storage": {"value": f"<h1>Goal {page_id}</h1><p>Grow retention.</p>"}},
        "version": {"number": 3, "createdAt": "2026-09-01"},
    }


def test_fetch_documents_by_id_preserves_order_deduplicates_and_normalizes(monkeypatch):
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        return _response(_page(url.rsplit("/", 1)[-1]))

    monkeypatch.setattr(confluence_module.requests, "get", fake_get)
    documents = ConfluenceClient([]).fetch_documents_by_id(["42", "7", "42"])

    assert [document["id"] for document in documents] == ["42", "7"]
    assert documents[0] == {
        "id": "42",
        "title": "Strategy",
        "version": 3,
        "updatedAt": "2026-09-01",
        "text": "Goal 42\nGrow retention.",
        "source": "confluence",
        "trust": "untrusted-reference-data",
    }
    assert [url.rsplit("/", 1)[-1] for url in calls] == ["42", "7"]


def test_fetch_documents_by_id_propagates_missing_page(monkeypatch):
    monkeypatch.setattr(
        confluence_module.requests,
        "get",
        lambda *_args, **_kwargs: _response({}, status_code=404),
    )
    with pytest.raises(RuntimeError, match=r"Confluence request failed \(404\)"):
        ConfluenceClient([]).fetch_documents_by_id(["missing"])


def test_fetch_documents_by_name_resolves_spaces_paginates_and_fetches_full_pages(
    monkeypatch,
):
    calls = []

    def fake_get(url, **kwargs):
        params = kwargs.get("params")
        calls.append((url, params))
        if url.endswith("/wiki/api/v2/spaces"):
            return _response({"results": [{"id": f"space-{params['keys']}"}]})
        if url.endswith("/wiki/api/v2/pages"):
            assert params == {
                "title": "Product Strategy",
                "space-id": ["space-STRATEGY", "space-PRODUCT"],
                "limit": 100,
            }
            return _response(
                {
                    "results": [
                        {"id": "42", "title": "Product Strategy"},
                        {"id": "ignored", "title": "product strategy"},
                    ],
                    "_links": {"next": "/wiki/api/v2/pages?cursor=next"},
                }
            )
        if "cursor=next" in url:
            assert params is None
            return _response(
                {"results": [{"id": "43", "title": "Product Strategy"}], "_links": {}}
            )
        page_id = url.rsplit("/", 1)[-1]
        return _response(_page(page_id, "Product Strategy"))

    monkeypatch.setattr(confluence_module.requests, "get", fake_get)
    documents = ConfluenceClient(["STRATEGY", "PRODUCT"]).fetch_documents_by_name(
        ["Product Strategy"]
    )

    assert [document["id"] for document in documents] == ["42", "43"]
    assert any("cursor=next" in url for url, _params in calls)


def test_duplicate_exact_titles_across_spaces_are_returned_once_by_page_id(monkeypatch):
    def fake_get(url, **kwargs):
        params = kwargs.get("params")
        if url.endswith("/spaces"):
            return _response({"results": [{"id": f"space-{params['keys']}"}]})
        if url.endswith("/pages"):
            return _response(
                {
                    "results": [
                        {"id": "42", "title": "Strategy"},
                        {"id": "43", "title": "Strategy"},
                        {"id": "42", "title": "Strategy"},
                    ]
                }
            )
        page_id = url.rsplit("/", 1)[-1]
        return _response(_page(page_id))

    monkeypatch.setattr(confluence_module.requests, "get", fake_get)
    documents = ConfluenceClient(["A", "B"]).fetch_documents_by_name(["Strategy"])

    assert [document["id"] for document in documents] == ["42", "43"]


def test_missing_titles_fail_by_default(monkeypatch):
    def fake_get(url, **_kwargs):
        if url.endswith("/spaces"):
            return _response({"results": [{"id": "space-1"}]})
        return _response({"results": []})

    monkeypatch.setattr(confluence_module.requests, "get", fake_get)
    with pytest.raises(ValueError, match="Confluence pages not found"):
        ConfluenceClient(["STRATEGY"]).fetch_documents_by_name(["Missing A", "Missing B"])


def test_missing_titles_warn_once_when_non_strict(monkeypatch, caplog):
    def fake_get(url, **kwargs):
        params = kwargs.get("params")
        if url.endswith("/spaces"):
            return _response({"results": [{"id": "space-1"}]})
        if url.endswith("/pages"):
            results = (
                [{"id": "42", "title": "Strategy"}]
                if params["title"] == "Strategy"
                else []
            )
            return _response({"results": results})
        return _response(_page("42"))

    monkeypatch.setattr(confluence_module.requests, "get", fake_get)
    with caplog.at_level(logging.WARNING, logger="symphony.confluence"):
        documents = ConfluenceClient(
            ["STRATEGY"], fail_on_missing_documents=False
        ).fetch_documents_by_name(["Strategy", "Missing A", "Missing B"])

    assert [document["id"] for document in documents] == ["42"]
    assert [record.message for record in caplog.records] == [
        "Confluence pages not found: 'Missing A', 'Missing B'"
    ]


def test_name_lookup_without_space_keys_is_rejected():
    with pytest.raises(ValueError, match="requires at least one space key"):
        ConfluenceClient([]).fetch_documents_by_name(["Strategy"])


def test_empty_name_lookup_requires_no_spaces_or_requests(monkeypatch):
    monkeypatch.setattr(
        confluence_module.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("Unexpected Confluence request"),
    )
    assert ConfluenceClient([]).fetch_documents_by_name([]) == []
