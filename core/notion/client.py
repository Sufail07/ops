"""Minimal Notion REST client: pacing (~3 req/s), retry on 429/5xx, pagination, page text."""
from __future__ import annotations

import time
from typing import Any

import httpx

BASE = "https://api.notion.com/v1"
TEXT_BLOCKS = {"paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item",
               "to_do", "quote", "callout", "toggle"}


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, token: str, version: str = "2025-09-03", min_interval: float = 0.35,
                 http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=30)
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": version,
                        "Content-Type": "application/json"}
        self.min_interval = min_interval
        self._last = 0.0

    def _request(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> dict[str, Any]:
        for attempt in range(5):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.http.request(method, BASE + path, headers=self.headers, json=json, params=params)
            except httpx.TransportError:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code >= 400:
                raise NotionError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise NotionError(f"{method} {path}: gave up after retries")

    def query_all(self, data_source_id: str, filter: dict | None = None) -> list[dict]:
        pages: list[dict] = []
        cursor = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if filter:
                body["filter"] = filter
            if cursor:
                body["start_cursor"] = cursor
            res = self._request("POST", f"/data_sources/{data_source_id}/query", body)
            pages += res["results"]
            if not res.get("has_more"):
                return pages
            cursor = res["next_cursor"]

    def create_page(self, data_source_id: str, properties: dict, children: list[dict] | None = None) -> dict:
        body: dict[str, Any] = {"parent": {"type": "data_source_id", "data_source_id": data_source_id},
                                "properties": properties}
        if children:
            body["children"] = children
        return self._request("POST", "/pages", body)

    def archive_page(self, page_id: str) -> dict:
        return self._request("PATCH", f"/pages/{page_id}", {"in_trash": True})

    def get_data_source(self, data_source_id: str) -> dict:
        return self._request("GET", f"/data_sources/{data_source_id}")

    def update_data_source(self, data_source_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/data_sources/{data_source_id}", {"properties": properties})

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def list_users(self) -> list[dict]:
        users: list[dict] = []
        cursor = None
        while True:
            res = self._request("GET", "/users", params={"page_size": 100, **({"start_cursor": cursor} if cursor else {})})
            users += res["results"]
            if not res.get("has_more"):
                return users
            cursor = res["next_cursor"]

    def page_text(self, page_id: str, depth: int = 0) -> str:
        lines: list[str] = []
        cursor = None
        while True:
            res = self._request("GET", f"/blocks/{page_id}/children",
                                params={"page_size": 100, **({"start_cursor": cursor} if cursor else {})})
            for b in res["results"]:
                body = b.get(b["type"], {})
                if b["type"] in TEXT_BLOCKS:
                    text = "".join(x.get("plain_text", "") for x in body.get("rich_text", []))
                    if text.strip():
                        lines.append(("  " * depth) + text)
                if b.get("has_children") and depth < 3:
                    sub = self.page_text(b["id"], depth + 1)
                    if sub:
                        lines.append(sub)
            if not res.get("has_more"):
                return "\n".join(lines)
            cursor = res["next_cursor"]
