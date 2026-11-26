"""Video categories: list_categories plus validation used by upload_video and update_metadata.

videoCategories.list (YouTube Data API v3): part=snippet, filtered by regionCode (ISO 3166-1
alpha-2) and localized with hl. Quota: 1 unit. snippet.assignable says whether videos can be
associated with the category. Results are cached per (region, language) for the process.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import TYPE_CHECKING, Any

from .errors import YouTubeMCPError
from .youtube import invalid, validate_category_id

if TYPE_CHECKING:
    from .service import YouTubeMCPService

log = logging.getLogger(__name__)
_REGION_RE = re.compile(r"^[A-Za-z]{2}$")


def validate_region(region_code: str) -> str:
    region = (region_code or "").strip().upper()
    if not _REGION_RE.match(region):
        raise invalid(f"'{region_code}' is not an ISO 3166-1 alpha-2 region code.", "Use codes like IN, US or GB.")
    return region


class CategoryCatalog:
    def __init__(self, svc: "YouTubeMCPService") -> None:
        self.svc = svc
        self._cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def _fetch(self, region: str, hl: str) -> list[dict[str, Any]]:
        key = (region, hl)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        items = self.svc.client.list_video_categories(region, hl)
        cats = sorted(({"category_id": str(i.get("id")), "title": i.get("snippet", {}).get("title"),
                        "assignable": bool(i.get("snippet", {}).get("assignable"))} for i in items),
                      key=lambda c: int(c["category_id"]) if c["category_id"].isdigit() else 0)
        with self._lock:
            self._cache[key] = cats
        return cats

    def list_categories(self, region_code: str | None = None, hl: str = "en_US",
                        assignable_only: bool = False) -> dict[str, Any]:
        region = validate_region(region_code or self.svc.settings.default_region)
        hl = (hl or "en_US").strip()
        cats = self._fetch(region, hl)
        if assignable_only:
            cats = [c for c in cats if c["assignable"]]
        default = self.svc.settings.default_category_id
        return {"ok": True, "region_code": region, "hl": hl, "default_category_id": default,
                "categories": [{**c, "is_default": c["category_id"] == default} for c in cats],
                "note": "Only categories with assignable=true can be used for uploads and metadata updates."}

    def ensure_assignable(self, category_id: str, region_code: str | None = None) -> str:
        """Raise invalid_input if the category is unknown or not assignable in the region.

        The configured default category is trusted without a lookup (keeps Quick Action uploads
        fast and offline-tolerant). If the lookup itself fails (e.g. network), YouTube will
        still validate the ID server-side, so the call is allowed to proceed.
        """
        category_id = validate_category_id(category_id)
        if category_id == self.svc.settings.default_category_id:
            return category_id
        region = validate_region(region_code or self.svc.settings.default_region)
        try:
            cats = {c["category_id"]: c for c in self._fetch(region, "en_US")}
        except YouTubeMCPError as err:
            log.warning("Category lookup failed (%s); letting YouTube validate category %s", err.code, category_id)
            return category_id
        cat = cats.get(category_id)
        if cat is None:
            raise invalid(f"Category {category_id} does not exist in region {region}.",
                          "Call list_categories to see valid category IDs.", category_id=category_id)
        if not cat["assignable"]:
            raise invalid(f"Category {category_id} ({cat['title']}) cannot be assigned to videos.",
                          "Choose a category with assignable=true from list_categories.", category_id=category_id)
        return category_id
