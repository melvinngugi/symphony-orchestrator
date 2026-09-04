from html.parser import HTMLParser
import logging
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin

import requests
from requests.auth import HTTPBasicAuth

from app.core.config import settings


logger = logging.getLogger("symphony.confluence")


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        clean = data.strip()
        if clean:
            self.parts.append(clean)


class ConfluenceClient:
    """Read-only Confluence document adapter with space-scoped title search."""

    def __init__(
        self,
        space_keys: list[str],
        fail_on_missing_documents: bool = True,
    ):
        settings.validate_confluence()
        if not isinstance(space_keys, list) or any(
            not isinstance(key, str) or not key.strip() for key in space_keys
        ):
            raise ValueError("Confluence space_keys must be a list of non-empty strings")
        if not isinstance(fail_on_missing_documents, bool):
            raise ValueError("fail_on_missing_documents must be a boolean")
        self.space_keys = self._deduplicate(value.strip() for value in space_keys)
        self.fail_on_missing_documents = fail_on_missing_documents
        self._space_ids: list[str] | None = None
        self.base_url = settings.CONFLUENCE_HOST.rstrip("/")
        self.auth = HTTPBasicAuth(
            settings.CONFLUENCE_USER_EMAIL,
            settings.CONFLUENCE_API_TOKEN,
        )
        self.headers = {"Accept": "application/json"}
        self.request_timeout = (
            settings.HTTP_CONNECT_TIMEOUT_SECONDS,
            settings.HTTP_READ_TIMEOUT_SECONDS,
        )

    def fetch_documents_by_id(self, document_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch complete pages by ID, preserving first-occurrence input order."""
        ids = self._validated_values(document_ids, "document_ids")
        return [self._fetch_page(page_id) for page_id in ids]

    def fetch_documents_by_name(self, document_names: list[str]) -> list[dict[str, Any]]:
        """Fetch every exact-title page match within the configured spaces."""
        names = self._validated_values(document_names, "document_names")
        if not names:
            return []
        if not self.space_keys:
            raise ValueError("Confluence document-name lookup requires at least one space key")

        space_ids = self._resolve_space_ids()
        matched_ids: list[str] = []
        matched_id_set: set[str] = set()
        missing_names: list[str] = []
        for name in names:
            page_ids = self._search_page_ids(name, space_ids)
            if not page_ids:
                missing_names.append(name)
                continue
            for page_id in page_ids:
                if page_id not in matched_id_set:
                    matched_ids.append(page_id)
                    matched_id_set.add(page_id)

        if missing_names:
            message = "Confluence pages not found: " + ", ".join(
                repr(name) for name in missing_names
            )
            if self.fail_on_missing_documents:
                raise ValueError(message)
            logger.warning(message)
        return self.fetch_documents_by_id(matched_ids)

    def _fetch_page(self, page_id: str) -> dict[str, Any]:
        payload = self._get_json(
            f"{self.base_url}/wiki/api/v2/pages/{page_id}",
            params={"body-format": "storage"},
        )
        if not isinstance(payload, dict):
            raise ValueError(f"Confluence page {page_id} response must be an object")
        return self._normalize_page(payload)

    def _resolve_space_ids(self) -> list[str]:
        if self._space_ids is not None:
            return list(self._space_ids)
        space_ids: list[str] = []
        for space_key in self.space_keys:
            payload = self._get_json(
                f"{self.base_url}/wiki/api/v2/spaces",
                params={"keys": space_key, "limit": 25},
            )
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list) or len(results) != 1:
                raise ValueError(
                    f"Confluence space key '{space_key}' was not uniquely resolved"
                )
            space_id = results[0].get("id") if isinstance(results[0], dict) else None
            if not isinstance(space_id, str) or not space_id:
                raise ValueError(f"Confluence space '{space_key}' is missing an id")
            if space_id not in space_ids:
                space_ids.append(space_id)
        self._space_ids = space_ids
        return list(space_ids)

    def _search_page_ids(self, title: str, space_ids: list[str]) -> list[str]:
        url = f"{self.base_url}/wiki/api/v2/pages"
        params: dict[str, Any] | None = {
            "title": title,
            "space-id": space_ids,
            "limit": 100,
        }
        page_ids: list[str] = []
        while url:
            page_payload = self._get_json(url, params=params)
            params = None
            page_results = page_payload.get("results") if isinstance(page_payload, dict) else None
            if not isinstance(page_results, list):
                raise ValueError(f"Confluence page search for '{title}' must return an array")
            for page in page_results:
                if not isinstance(page, dict):
                    raise ValueError("Confluence page search result must be an object")
                if page.get("title") != title:
                    continue
                page_id = page.get("id")
                if not isinstance(page_id, str) or not page_id:
                    raise ValueError(
                        f"Confluence page search result for '{title}' is missing an id"
                    )
                if page_id not in page_ids:
                    page_ids.append(page_id)
            links = page_payload.get("_links", {})
            next_link = links.get("next") if isinstance(links, dict) else None
            url = urljoin(self.base_url, next_link) if isinstance(next_link, str) and next_link else ""
        return page_ids

    @classmethod
    def _validated_values(cls, configured: object, field_name: str) -> list[str]:
        if not isinstance(configured, list) or any(
            not isinstance(value, str) or not value.strip() for value in configured
        ):
            raise ValueError(f"Confluence {field_name} must be a list of non-empty strings")
        return cls._deduplicate(value.strip() for value in configured)

    @staticmethod
    def _deduplicate(values: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value not in seen:
                result.append(value)
                seen.add(value)
        return result

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        try:
            response = requests.get(
                url,
                headers=self.headers,
                auth=self.auth,
                params=params,
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Confluence request failed: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"Confluence request failed ({response.status_code}): {response.text}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ValueError("Confluence response is not valid JSON") from exc

    @staticmethod
    def _normalize_page(page: Any) -> dict[str, Any]:
        if not isinstance(page, dict):
            raise ValueError("Confluence page response item must be an object")
        page_id = page.get("id")
        title = page.get("title")
        body = page.get("body", {}).get("storage", {}).get("value")
        if not isinstance(page_id, str) or not page_id:
            raise ValueError("Confluence page is missing an id")
        if not isinstance(title, str) or not title:
            raise ValueError(f"Confluence page {page_id} is missing a title")
        if not isinstance(body, str):
            raise ValueError(f"Confluence page {page_id} is missing storage body content")
        extractor = _TextExtractor()
        extractor.feed(body)
        version = page.get("version") if isinstance(page.get("version"), dict) else {}
        return {
            "id": page_id,
            "title": title,
            "version": version.get("number"),
            "updatedAt": version.get("createdAt"),
            "text": "\n".join(extractor.parts),
            "source": "confluence",
            "trust": "untrusted-reference-data",
        }
