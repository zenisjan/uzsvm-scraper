#!/usr/bin/env python3
"""
ÚZSVM Property Auction Scraper (nabidkamajetku.gov.cz)
======================================================

Scrapes state-property offers from the Czech government's ÚZSVM
(Úřad pro zastupování státu ve věcech majetkových) property portal.

Uses the site's JSON API plus each item's HTML detail page:
- POST /api/Property/AuctionList — paginated auctions; detail /Home/AuctionDetail/{id}
- POST /api/Property/List with ListType "alltenders" — paginated "Jiné formy prodeje
  a pronájmu" offers (sale, gift, lease); detail /Home/Detail/{id}; stored with
  ids prefixed "T" because property ids and auction ids are separate sequences
- Detail pages give exact GPS (real-estate auctions), the contact person, phone and
  e-mail, the whole photo gallery and the auction facts that the list lacks
- Images via /api/Property/Attachment/{imageId}
"""

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import httpx
from apify import Actor
from bs4 import BeautifulSoup

try:
    from .czech_cities import geocode_czech_city
except ImportError:
    from czech_cities import geocode_czech_city

try:
    import lxml  # noqa: F401  (faster parser; beautifulsoup4[lxml] is in requirements)

    _HTML_PARSER = "lxml"
except ImportError:
    _HTML_PARSER = "html.parser"

try:
    from zoneinfo import ZoneInfo

    _PRAGUE: Optional[Any] = ZoneInfo("Europe/Prague")
except Exception:  # no tz database in the image: _to_prague() uses the EU rule
    _PRAGUE = None


BASE_URL = "https://www.nabidkamajetku.gov.cz"
API_AUCTION_LIST = f"{BASE_URL}/api/Property/AuctionList"
API_PROPERTY_LIST = f"{BASE_URL}/api/Property/List"
API_ATTACHMENT = f"{BASE_URL}/api/Property/Attachment"
DETAIL_URL = f"{BASE_URL}/Home/AuctionDetail"
TENDER_DETAIL_URL = f"{BASE_URL}/Home/Detail"

# Tenders come from the property list, auctions from the auction list: the ids
# are separate sequences that can overlap, while listings are keyed by
# (id, actor_run_id, scraper_name). Tender ids are therefore stored as "T{id}".
TENDER_ID_PREFIX = "T"

LIST_PAGE_SIZE = 100

# Detail pages are fetched DETAIL_CONCURRENCY at a time, with request starts at
# least DETAIL_MIN_INTERVAL_SECS apart (the idnes-reality pattern): ~700 pages
# take a few minutes instead of half an hour one by one.
DETAIL_CONCURRENCY = 4
DETAIL_MIN_INTERVAL_SECS = 0.25
_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

# Column limits in the shared listings table.
CATEGORY_MAX_LEN = 50  # varchar(50)
CONTACT_NAME_MAX_LEN = 255  # varchar(255)
PHONE_MAX_LEN = 100  # varchar(100)
FULL_DESCRIPTION_MAX_LEN = 5000

# Stop fetching detail pages this long before the Apify hard timeout, so the run
# can still save what it has and write its final status.
_DEADLINE_MARGIN_SECS = 120

# Generous bounding box of Czechia. Detail pages of movables (and of tenders)
# carry [0, 0]; anything else outside the box is a data-entry error.
_CZ_LAT = (48.4, 51.2)
_CZ_LNG = (11.9, 19.0)

_CENTER_RE = re.compile(
    r"var\s+center\s*=\s*\[\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*\]"
)
_LOCAL_DT_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s+(\d{1,2}):(\d{2})")
_PERIOD_RE = re.compile(
    r"(\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2})\s*-\s*(\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2})"
)


def _run_deadline() -> Optional[datetime]:
    """UTC instant to stop fetching detail pages by (Apify timeout minus a margin).

    None for local runs, where Apify sets no timeout variable.
    """
    iso = os.environ.get("APIFY_TIMEOUT_AT") or os.environ.get("ACTOR_TIMEOUT_AT")
    if not iso:
        return None
    try:
        deadline = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline - timedelta(seconds=_DEADLINE_MARGIN_SECS)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _eu_summer_time(utc_dt: datetime) -> bool:
    """CEST runs from the last Sunday of March to the last Sunday of October, 01:00 UTC."""

    def last_sunday(month: int) -> datetime:
        day = datetime(utc_dt.year, month, 31, 1, 0, tzinfo=timezone.utc)
        return day - timedelta(days=(day.weekday() + 1) % 7)

    return last_sunday(3) <= utc_dt < last_sunday(10)


def _to_prague(utc_dt: datetime) -> datetime:
    if _PRAGUE is not None:
        return utc_dt.astimezone(_PRAGUE)
    hours = 2 if _eu_summer_time(utc_dt) else 1
    return utc_dt.astimezone(timezone(timedelta(hours=hours)))


def _iso_from_utc(value: Any) -> str:
    """API StartDate/EndDate (UTC without an offset) -> Prague ISO 8601 with offset.

    '2027-01-26T10:00:00' -> '2027-01-26T11:00:00+01:00'. '' if unparseable.
    """
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return _to_prague(moment).isoformat(timespec="seconds")


def _iso_from_local(text: Any) -> str:
    """'26.01.2027 11:00' (Prague wall-clock time, as the site prints it) -> ISO with offset."""
    match = _LOCAL_DT_RE.search(str(text or ""))
    if not match:
        return ""
    day, month, year, hour, minute = (int(g) for g in match.groups())
    try:
        naive = datetime(year, month, day, hour, minute)
    except ValueError:
        return ""
    if _PRAGUE is not None:
        return naive.replace(tzinfo=_PRAGUE).isoformat(timespec="seconds")
    for hours in (2, 1):  # EU rule: CEST if that instant really is summer time
        moment = (naive - timedelta(hours=hours)).replace(tzinfo=timezone.utc)
        if (hours == 2) == _eu_summer_time(moment):
            return moment.astimezone(timezone(timedelta(hours=hours))).isoformat(
                timespec="seconds"
            )
    return naive.replace(tzinfo=timezone(timedelta(hours=1))).isoformat(timespec="seconds")


def _period_from_text(text: Any) -> Tuple[str, str]:
    """'Aukce (14.10.2026 10:00 - 02.12.2026 10:00)' -> (start ISO, end ISO); last range wins."""
    found = None
    for found in _PERIOD_RE.finditer(str(text or "")):
        pass
    if found is None:
        return "", ""
    return _iso_from_local(found.group(1)), _iso_from_local(found.group(2))


def _tender_kind(status_name: str) -> str:
    """'Jiné formy PP (Prodej) (23.10.2026 13:00 - …)' -> 'Prodej' (or 'Nabídky daru', …)."""
    for match in re.finditer(r"\(([^()]*)\)", status_name or ""):
        inner = " ".join(match.group(1).split())
        if inner and not _PERIOD_RE.search(inner):
            return inner
    return ""


def _parse_amount(value: Any) -> Optional[float]:
    """Czech-formatted amount ('39 990 000,00', '1 000,00 Kč') -> float, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d,.\-]", "", str(value)).replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _format_czk(amount: Optional[float]) -> str:
    """123000.0 -> '123 000 Kč'; 937.5 -> '937,50 Kč'; None -> ''."""
    if amount is None:
        return ""
    if abs(amount - round(amount)) < 0.005:
        return f"{int(round(amount)):,} Kč".replace(",", " ")
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",") + " Kč"


def _clean(text: Any) -> str:
    """Collapse all whitespace (incl. NBSP and newlines) to single spaces."""
    return " ".join(("" if text is None else str(text)).split())


def _cut_at_word(text: str, limit: int) -> str:
    """Normalise whitespace and shorten to `limit` chars at a word boundary."""
    text = _clean(text)
    if len(text) <= limit:
        return text
    window = text[: limit + 1]  # one extra char: a space there ends a whole word
    cut = window.rsplit(" ", 1)[0] if " " in window else ""
    cut = cut.rstrip(" ,;:-/")
    return cut or text[:limit]


def _compose_full_description(
    description: str,
    status_name: str,
    org_name: str,
    facts: List[Tuple[str, Any]],
) -> str:
    """Description, then the 'Stav'/'Organizace' lines, then one 'Label: value' line per fact.

    The labelled lines are what the web app's normalizer reads (it splits on
    newlines/';' and takes the text before the first ':' as the label). Only the
    free-text description is shortened to fit, so the labelled lines always survive.
    """
    tail = ""
    if status_name:
        tail += f"\n\nStav: {status_name}"
    if org_name:
        tail += f"\n\nOrganizace: {org_name}"
    lines = [
        f"{label}: {_clean(value).replace(';', ',')}"
        for label, value in facts
        if value is not None and _clean(value) != ""
    ]
    if lines:
        tail += "\n\n" + "\n".join(lines)
    head = (description or "")[: max(0, FULL_DESCRIPTION_MAX_LEN - len(tail))]
    return (head + tail)[:FULL_DESCRIPTION_MAX_LEN].lstrip()


def _parse_contact(column) -> Dict[str, str]:
    """The 'Kontaktní údaje' block: name, tel: link, mailto: link, …, office address."""
    contact: Dict[str, str] = {}
    tel = column.select_one('a[href^="tel:"]')
    if tel is not None:
        contact["phone"] = _clean(tel.get_text(" ", strip=True)) or _clean(
            tel.get("href", "")[4:]
        )
    mail = column.select_one('a[href^="mailto:"]')
    if mail is not None:
        contact["email"] = _clean(mail.get_text(" ", strip=True)) or _clean(
            mail.get("href", "")[7:]
        )
    paragraphs = column.find_all("p")
    if paragraphs and paragraphs[0].find("a") is None:
        contact["name"] = _clean(paragraphs[0].get_text(" ", strip=True))
    if len(paragraphs) > 1 and paragraphs[-1].find("a") is None:
        contact["address"] = _clean(paragraphs[-1].get_text(" ", strip=True))
    return {key: value for key, value in contact.items() if value}


# Bounded exponential backoff for transient fetch failures (timeouts, resets,
# 429/5xx). A single flaky response used to abort an entire category scrape.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


async def _request_with_retry(client, method, url, attempts=3, **kwargs):
    """Issue a request, retrying transient failures with exponential backoff.

    Returns the final response without raising for status (callers keep their
    own raise_for_status()); re-raises the last transport error if the
    connection itself keeps failing.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError as e:
            if attempt == attempts:
                raise
            reason = repr(e)
        else:
            if response.status_code not in _RETRYABLE_STATUS or attempt == attempts:
                return response
            reason = f"HTTP {response.status_code}"
        delay = 2.0 * (2 ** (attempt - 1))
        Actor.log.warning(
            f"Transient fetch failure ({reason}); retry {attempt}/{attempts - 1} "
            f"in {delay:.0f}s: {url}"
        )
        await asyncio.sleep(delay)


class _FailRunOnCrash:
    """Marks the actor_runs row 'failed' if the run dies with an unhandled error.

    Historically a terminal status was written only on the happy path, so a crash
    (or an Apify hard-timeout kill) left the row 'running' forever and the web app
    could not tell a dead run from a live one — sold-detection then mistreated the
    run's listings. Used as `async with Actor, _FailRunOnCrash():` so it exits
    before Actor; the exception still propagates and fails the platform run too.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            try:
                # db_manager is only imported inside main() in this scraper, so
                # resolve it lazily here as well.
                try:
                    from .database import db_manager
                except ImportError:
                    from database import db_manager
                Actor.log.error(f"Run failed with unhandled error: {exc!r}")
                db_manager.update_actor_run_status("failed", 0)
                db_manager.close_pool()
            except Exception as finalize_error:
                Actor.log.error(f"Could not mark run failed: {finalize_error}")
        return False


class NabidkaMajetkuScraper:
    """Scraper for nabidkamajetku.gov.cz using the site's JSON API and detail pages."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        # Why this run is incomplete (a page or the conversion failed); main() marks
        # such a run 'failed' instead of 'completed'.
        self.incomplete_reasons: List[str] = []
        # UTC instant to stop fetching detail pages by (set from the Apify timeout).
        self.deadline: Optional[datetime] = None
        # Detail-page outcomes. A failed or skipped page keeps the list data, so
        # it is counted, not an early stop.
        self.detail_ok = 0
        self.detail_failures = 0
        self.detail_skipped = 0
        self.gps_rejected = 0
        # Event-loop time the next detail request may start (see DETAIL_*).
        self._next_detail_start = 0.0

    def _past_deadline(self) -> bool:
        return self.deadline is not None and _now_utc() >= self.deadline

    async def scrape_all_auctions(
        self,
        max_listings: int = 0,
        auction_status: str = "active",
        price_min: int = 0,
        price_max: int = 0,
        location: Optional[str] = None,
        search_query: Optional[str] = None,
        category_id: int = 0,
        include_details: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Scrape auctions from the paginated API, apply filters, fetch detail pages.

        Args:
            max_listings: Maximum number of listings to return (0 = all)
            auction_status: Filter by status ("all", "active", "closed", "prepared", "canceled")
            price_min: Minimum price filter in CZK
            price_max: Maximum price filter in CZK
            location: Location filter (district name)
            search_query: Search term to filter titles/descriptions
            category_id: Category ID filter (0 = all)
            include_details: Fetch each auction's detail page

        Returns:
            List of auction dictionaries in standardized format
        """
        try:
            # Fetch all pages from the API
            Actor.log.info("Fetching auction data from API...")
            all_auctions = await self._fetch_all_pages(
                auction_status, search_query, category_id, max_listings
            )

            if not all_auctions:
                Actor.log.warning("No auction data received from API")
                return []

            Actor.log.info(f"Fetched {len(all_auctions)} total auctions from API")
            return await self._filter_detail_convert(
                all_auctions, "auction", self._convert_to_standard_format,
                lambda item: f"{DETAIL_URL}/{item.get('Id')}",
                max_listings, price_min, price_max, location, search_query,
                include_details,
            )

        except Exception as e:
            Actor.log.error(f"Error in scrape_all_auctions: {e}")
            self.incomplete_reasons.append(f"scrape_all_auctions: {e!r}")
            return []

    async def scrape_all_tenders(
        self,
        max_listings: int = 0,
        price_min: int = 0,
        price_max: int = 0,
        location: Optional[str] = None,
        search_query: Optional[str] = None,
        category_id: int = 0,
        include_details: bool = True,
    ) -> List[Dict[str, Any]]:
        """Scrape the "Jiné formy prodeje a pronájmu" offers (sale, gift, lease).

        Same paging, failure handling, filters and detail pages as the auctions;
        ids are stored as "T{id}".
        """
        try:
            Actor.log.info("Fetching tender data (other forms of sale and lease) from API...")
            all_tenders = await self._fetch_list_pages(
                API_PROPERTY_LIST,
                lambda page, size: self._tender_body(page, size, search_query, category_id),
                "Properties",
                "tender list",
                max_listings,
            )
            if not all_tenders:
                Actor.log.warning("No tender data received from API")
                return []

            Actor.log.info(f"Fetched {len(all_tenders)} total tenders from API")
            return await self._filter_detail_convert(
                all_tenders, "tender", self._convert_tender,
                lambda item: f"{TENDER_DETAIL_URL}/{item.get('Id')}",
                max_listings, price_min, price_max, location, search_query,
                include_details,
            )

        except Exception as e:
            Actor.log.error(f"Error in scrape_all_tenders: {e}")
            self.incomplete_reasons.append(f"scrape_all_tenders: {e!r}")
            return []

    async def _filter_detail_convert(
        self,
        items: List[Dict[str, Any]],
        what: str,
        convert: Callable[[Dict[str, Any], Optional[Dict[str, Any]]], Optional[Dict[str, Any]]],
        detail_url: Callable[[Dict[str, Any]], str],
        max_listings: int,
        price_min: int,
        price_max: int,
        location: Optional[str],
        search_query: Optional[str],
        include_details: bool,
    ) -> List[Dict[str, Any]]:
        """Filter, cap, fetch detail pages and convert one list's raw items."""
        # Apply local filters (price, location)
        filtered = self._apply_filters(items, price_min, price_max, location, search_query)
        Actor.log.info(f"After filtering: {len(filtered)} {what}s match criteria")

        # Limit results if requested
        if max_listings > 0 and len(filtered) > max_listings:
            filtered = filtered[:max_listings]
            Actor.log.info(f"Limited to {max_listings} {what}s as requested")

        if include_details and filtered:
            Actor.log.info(f"Fetching {len(filtered)} {what} detail pages...")
            details = await self._fetch_details([detail_url(item) for item in filtered])
        else:
            details = [None] * len(filtered)

        # Convert to standardized format
        Actor.log.info("Converting to standardized format...")
        standardized = []
        for item, detail in zip(filtered, details):
            try:
                converted = convert(item, detail)
            except Exception as e:
                # A dropped row would read as "sold" on the map: fail the run.
                Actor.log.error(f"Could not convert {what} {item.get('Id')!r}: {e!r}")
                self.incomplete_reasons.append(
                    f"{what} {item.get('Id')!r} could not be converted: {e!r}"
                )
                continue
            if converted:
                standardized.append(converted)

        Actor.log.info(f"Final result: {len(standardized)} {what}s ready for storage")
        return standardized

    async def _fetch_all_pages(
        self,
        list_type: str,
        fulltext: Optional[str],
        category_id: int,
        max_listings: int,
    ) -> List[Dict[str, Any]]:
        """Fetch all auction pages from the API."""

        def body(page: int, page_size: int) -> Dict[str, Any]:
            return {
                "Page": page,
                "PageSize": page_size,
                "ListType": list_type,
                "Order": "Default",
                "OrderDesc": True,
                "CategoryId": category_id,
                "LocalityId": 0,
                "MunicipialityId": 0,
                "CadastreId": 0,
                "AuctionModeId": 0,
                "Fulltext": fulltext or "",
                "OrgId": "",
                "OrganizationType": 0,
                "OrganizationId": 0,
                "ContactZipCode": "",
            }

        return await self._fetch_list_pages(
            API_AUCTION_LIST, body, "Auctions", "auction list", max_listings
        )

    @staticmethod
    def _tender_body(
        page: int, page_size: int, fulltext: Optional[str], category_id: int
    ) -> Dict[str, Any]:
        # Mirrors the site's own propertyListOptions for "Jiné formy prodeje a pronájmu".
        return {
            "ListType": "alltenders",
            "Page": page,
            "PageSize": page_size,
            "Order": "Default",
            "OrderDesc": "true",
            "CategoryId": category_id,
            "Fulltext": fulltext or "",
            "OrgId": "",
            "OrganizationType": 0,
            "OrganizationId": 0,
            "LocalityId": 0,
            "MunicipialityId": 0,
            "CadastreId": 0,
            "ContactZipCode": "",
            "PropertyAuthor": "",
            "ShowEndedProperties": False,
            "InterestId": 0,
            "MyinterestId": 0,
            "State": 0,
        }

    async def _fetch_list_pages(
        self,
        url: str,
        make_body: Callable[[int, int], Dict[str, Any]],
        items_key: str,
        what: str,
        max_listings: int,
    ) -> List[Dict[str, Any]]:
        """Page through one list endpoint until PropertyTotalCount (or the cap).

        A failed, malformed or empty page before PropertyTotalCount is recorded in
        incomplete_reasons: the run then ends 'failed' instead of letting
        sold-detection flag every row it never reached as sold.
        """
        rows: List[Dict[str, Any]] = []
        seen_ids = set()
        page = 1

        while True:
            try:
                response = await _request_with_retry(
                    self.client,
                    "POST",
                    url,
                    json=make_body(page, LIST_PAGE_SIZE),
                    timeout=60.0,
                )

                if response.status_code != 200:
                    Actor.log.error(
                        f"API returned {response.status_code}: {response.text[:500]}"
                    )
                    self.incomplete_reasons.append(
                        f"{what} page {page}: HTTP {response.status_code}"
                    )
                    break

                data = response.json()
                items = data.get(items_key) if isinstance(data, dict) else None
                total_count = data.get("PropertyTotalCount") if isinstance(data, dict) else None
                if not isinstance(items, list) or not isinstance(total_count, int):
                    self.incomplete_reasons.append(
                        f"{what} page {page}: unexpected response "
                        f"(no '{items_key}' list or PropertyTotalCount)"
                    )
                    break

                if not items:
                    if len(rows) < total_count:
                        self.incomplete_reasons.append(
                            f"{what} page {page} came back empty at "
                            f"{len(rows)}/{total_count}"
                        )
                    break

                page_ids = [item.get("Id") for item in items]
                if page > 1 and all(item_id in seen_ids for item_id in page_ids):
                    # The API ignored Page: paging further would never reach the total.
                    self.incomplete_reasons.append(
                        f"{what} page {page} repeated earlier rows at "
                        f"{len(rows)}/{total_count}"
                    )
                    break
                seen_ids.update(page_ids)

                rows.extend(items)
                Actor.log.info(
                    f"{what.capitalize()} page {page}: fetched {len(items)} rows "
                    f"({len(rows)}/{total_count} total)"
                )

                # Stop if we have enough
                if max_listings > 0 and len(rows) >= max_listings:
                    break

                # Stop if we've fetched all pages
                if len(rows) >= total_count:
                    break

                page += 1

                # Small delay to be polite
                await asyncio.sleep(0.3)

            except Exception as e:
                Actor.log.error(f"Failed to fetch {what} page {page}: {e}")
                self.incomplete_reasons.append(f"{what} page {page}: {e!r}")
                break

        # A row that moved across a page boundary while paging comes back twice; two
        # rows with the same key in one INSERT batch would fail the whole insert.
        unique: List[Dict[str, Any]] = []
        kept = set()
        for item in rows:
            item_id = item.get("Id")
            if item_id is not None and item_id in kept:
                continue
            kept.add(item_id)
            unique.append(item)
        if len(unique) < len(rows):
            Actor.log.warning(
                f"{what.capitalize()}: dropped {len(rows) - len(unique)} duplicate rows "
                f"(the list shifted while paging)"
            )
        return unique

    def _apply_filters(
        self,
        auctions: List[Dict[str, Any]],
        price_min: int,
        price_max: int,
        location: Optional[str],
        search_query: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Apply local filters to the auction data."""
        filtered = auctions

        # Filter by price range
        if price_min > 0 or price_max > 0:
            price_filtered = []
            for auction in filtered:
                price = self._parse_price(auction.get("Price"))
                if price is None:
                    continue
                if price_min > 0 and price < price_min:
                    continue
                if price_max > 0 and price > price_max:
                    continue
                price_filtered.append(auction)
            filtered = price_filtered
            Actor.log.info(f"Price filtered: {len(filtered)} remaining")

        # Filter by location (district)
        if location and location.strip():
            location_lower = location.lower().strip()
            filtered = [
                a
                for a in filtered
                if location_lower in (a.get("DistrictName", "") or "").lower()
            ]
            Actor.log.info(f"Location filtered: {len(filtered)} remaining")

        # Filter by search query (client-side, in case API fulltext missed something)
        if search_query and search_query.strip():
            query_lower = search_query.lower().strip()
            filtered = [
                a
                for a in filtered
                if query_lower in (a.get("Name", "") or "").lower()
                or query_lower in (a.get("Description", "") or "").lower()
            ]
            Actor.log.info(f"Search filtered: {len(filtered)} remaining")

        return filtered

    @staticmethod
    def _parse_price(price_str: Optional[str]) -> Optional[float]:
        """Parse Czech-formatted price string like '17 100,00' to float."""
        if not price_str:
            return None
        return _parse_amount(price_str)

    # ----- detail pages -------------------------------------------------------

    async def _fetch_details(self, urls: List[str]) -> List[Optional[Dict[str, Any]]]:
        """Fetch and parse detail pages, DETAIL_CONCURRENCY at a time, in input order.

        A page that still fails after retries yields None (the item keeps its list
        data) and is counted in detail_failures. Once the run's time budget is spent
        the remaining items are skipped the same way (detail_skipped).
        """
        semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
        done = 0

        async def fetch(url: str) -> Optional[Dict[str, Any]]:
            nonlocal done
            async with semaphore:
                if self._past_deadline():
                    self.detail_skipped += 1
                    return None
                await self._pace_detail_request()
                details = await self._fetch_detail(url)
            done += 1
            if done % 100 == 0:
                Actor.log.info(f"Fetched detail page {done}/{len(urls)}")
            return details

        skipped_before = self.detail_skipped
        results = await asyncio.gather(*(fetch(url) for url in urls))
        skipped = self.detail_skipped - skipped_before
        if skipped:
            Actor.log.warning(
                f"Run time budget reached; {skipped} of {len(urls)} items keep their "
                f"list data without a detail page"
            )
        return list(results)

    async def _pace_detail_request(self) -> None:
        """Wait for this detail request's start slot, DETAIL_MIN_INTERVAL_SECS apart."""
        loop = asyncio.get_running_loop()
        start = max(loop.time(), self._next_detail_start)
        self._next_detail_start = start + DETAIL_MIN_INTERVAL_SECS
        await asyncio.sleep(start - loop.time())

    async def _fetch_detail(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            response = await _request_with_retry(
                self.client,
                "GET",
                url,
                headers=_HTML_HEADERS,
                timeout=30.0,
                follow_redirects=True,
            )
            response.raise_for_status()
            details = self._parse_detail_page(response.text)
        except Exception as e:
            self.detail_failures += 1
            Actor.log.warning(f"Detail page failed, keeping the list data: {url}: {e!r}")
            return None
        self.detail_ok += 1
        return details

    def _parse_detail_page(self, text: str) -> Dict[str, Any]:
        """Exact GPS, labelled fields, contact block and photo gallery of a detail page."""
        soup = BeautifulSoup(text, _HTML_PARSER)

        gps = None
        center = _CENTER_RE.search(text)
        if center:
            gps = self._plausible_gps(float(center.group(1)), float(center.group(2)))

        fields: Dict[str, str] = {}
        contact: Dict[str, str] = {}
        for label in soup.find_all("label"):
            key = _clean(label.get_text(" ", strip=True)).rstrip(":").strip()
            holder = label.find_parent("div")
            column = holder.find_next_sibling("div") if holder is not None else None
            if not key or column is None:
                continue
            if key == "Kontaktní údaje":
                contact = _parse_contact(column)
                continue
            for note in column.select(".diff-tz-text"):  # "(SEČ/SELČ)" hint
                note.decompose()
            value = _clean(column.get_text(" ", strip=True))
            if value and key not in fields:
                fields[key] = value

        images: List[str] = []
        for link in soup.select('a[data-lightbox][href*="/api/Property/Attachment/"]'):
            href = urljoin(BASE_URL + "/", link.get("href", ""))
            if href not in images:
                images.append(href)

        if center is None and not contact and "Kategorie" not in fields:
            raise ValueError("not a detail page (no map, contact or category)")
        return {"gps": gps, "fields": fields, "contact": contact, "images": images}

    def _plausible_gps(self, lat: float, lng: float) -> Optional[Tuple[float, float]]:
        """[0, 0] means "no GPS"; a point outside Czechia is ignored (swapped pairs fixed)."""
        if lat == 0 and lng == 0:
            return None
        if _CZ_LAT[0] <= lat <= _CZ_LAT[1] and _CZ_LNG[0] <= lng <= _CZ_LNG[1]:
            return lat, lng
        if _CZ_LAT[0] <= lng <= _CZ_LAT[1] and _CZ_LNG[0] <= lat <= _CZ_LNG[1]:
            return lng, lat
        self.gps_rejected += 1
        Actor.log.warning(f"Ignoring detail-page GPS outside Czechia: [{lat}, {lng}]")
        return None

    # ----- conversion -----------------------------------------------------------

    @staticmethod
    def _coordinates(
        gps: Optional[Tuple[float, float]], district: str
    ) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        """Exact detail-page GPS when there is one, otherwise the district centroid."""
        if gps:
            return gps[0], gps[1], "detail"
        coords = geocode_czech_city(district.strip()) if district else None
        if coords:
            return coords[0], coords[1], "district"
        return None, None, None

    @staticmethod
    def _images(
        api_data: Dict[str, Any], details: Dict[str, Any]
    ) -> Tuple[List[str], str]:
        """(all image URLs, main image URL): the detail gallery, else the list thumbnail."""
        image_id = api_data.get("ImageId", "") or ""
        image_url = f"{API_ATTACHMENT}/{image_id}" if image_id else ""
        images = list(details.get("images") or [])
        if image_url and image_url not in images:
            images.insert(0, image_url)
        if not image_url and images:
            image_url = images[0]
        return images, image_url

    @staticmethod
    def _contact_facts(contact: Dict[str, str]) -> List[Tuple[str, Any]]:
        return [
            ("E-mail", contact.get("email")),
            ("Adresa kontaktu", contact.get("address")),
        ]

    def _convert_to_standard_format(
        self, api_data: Dict[str, Any], details: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Convert an auction (list row + optional detail page) to the DB listing format."""
        auction_id = str(api_data.get("Id", "") or "")
        if not auction_id:
            Actor.log.warning("Skipping an auction row without an Id")
            return None
        details = details or {}
        fields = details.get("fields") or {}
        contact = details.get("contact") or {}

        # Basic fields
        title = api_data.get("Name", "") or ""
        description = api_data.get("Description", "") or ""
        category = api_data.get("CategoryName", "") or ""
        district = api_data.get("DistrictName", "") or ""
        status_name = api_data.get("StatusName", "") or ""

        # Price (the current price: the starting price until someone bids)
        price = self._parse_price(api_data.get("Price"))
        price_text = api_data.get("Price", "") or ""
        if price_text:
            price_text = f"{price_text} Kč"

        # Dates: the API's StartDate/EndDate are UTC without an offset
        start_iso = _iso_from_utc(api_data.get("StartDate"))
        end_iso = _iso_from_utc(api_data.get("EndDate"))
        if not (start_iso and end_iso):
            term_start, term_end = _period_from_text(fields.get("Termín aukce"))
            start_iso = start_iso or term_start
            end_iso = end_iso or term_end
        updated_iso = _iso_from_local(api_data.get("UpdatedDate"))

        # Organization
        org = api_data.get("Organization", {}) or {}
        org_name = org.get("u04Name", "") or fields.get("Instituce", "")

        min_increment = _parse_amount(api_data.get("MinimalIncrement"))
        if min_increment is None:
            min_increment = _parse_amount(fields.get("Minimální navýšení ceny"))
        bids = api_data.get("NbrOfBids")
        if not isinstance(bids, int) or isinstance(bids, bool):
            bids_text = re.search(r"\d+", fields.get("Počet příhozů", ""))
            bids = int(bids_text.group()) if bids_text else None
        current_price = price if price else _parse_amount(fields.get("Aktuální cena"))

        facts: List[Tuple[str, Any]] = [
            ("Nejnižší podání", _format_czk(_parse_amount(fields.get("Nejnižší podání")))),
            ("Aktuální cena", _format_czk(current_price)),
            ("Minimální příhoz", _format_czk(min_increment)),
            ("Počet příhozů", bids),
            ("Kauce", _format_czk(_parse_amount(fields.get("Kauce")))),
            ("Začátek aukce", start_iso),
            ("Konec aukce", end_iso),
            (
                "Konec přihlašování",
                _iso_from_local(fields.get("Termín možnosti přihlášení k aukci")),
            ),
            ("Forma", fields.get("Forma")),
        ] + self._contact_facts(contact)

        # Auction status
        auction_status = api_data.get("AuctionStatus", 0)
        # 0 = announced, 1 = active/running, 2 = closed
        is_knocked_down = auction_status == 2

        lat, lng, gps_source = self._coordinates(details.get("gps"), district)
        images, image_url = self._images(api_data, details)

        return {
            "id": auction_id,
            "title": title,
            "url": f"{DETAIL_URL}/{auction_id}",
            "category": _cut_at_word(category, CATEGORY_MAX_LEN),
            "price": price if price else 0.0,
            "price_text": price_text,
            "description": title[:500] if title else "",
            "full_description": _compose_full_description(
                description, status_name, org_name, facts
            ),
            "location": district,
            "views": 0,
            "date": start_iso or updated_iso,
            "is_top": api_data.get("TopProperty", False),
            "image_url": image_url,
            "contact_name": (contact.get("name") or org_name)[:CONTACT_NAME_MAX_LEN],
            "phone": (contact.get("phone") or "")[:PHONE_MAX_LEN],
            "coordinates_lat": lat,
            "coordinates_lng": lng,
            # Python lists: database.py JSON-encodes them once.
            "images": images,
            "similar_listings": [],
            "scraped_at": datetime.now().isoformat(),
            "is_knocked_down": is_knocked_down,
            "date_from": start_iso,
            "date_to": end_iso,
            "offer_kind": "auction",
            "gps_source": gps_source,
            "contact_email": contact.get("email", ""),
        }

    def _convert_tender(
        self, api_data: Dict[str, Any], details: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Convert a tender (sale / gift / lease offer) to the DB listing format."""
        tender_id = str(api_data.get("Id", "") or "")
        if not tender_id:
            Actor.log.warning("Skipping a tender row without an Id")
            return None
        details = details or {}
        fields = details.get("fields") or {}
        contact = details.get("contact") or {}

        title = api_data.get("Name", "") or ""
        description = api_data.get("Description", "") or ""
        category = api_data.get("CategoryName", "") or fields.get("Kategorie", "")
        district = api_data.get("DistrictName", "") or fields.get("Okres", "")
        status_name = api_data.get("StatusName", "") or fields.get("Stav", "")

        price_raw = api_data.get("Price") or ""
        price = self._parse_price(price_raw)
        price_text = f"{price_raw} Kč" if price_raw else ""
        if price is None and fields.get("Cena"):
            price = _parse_amount(fields["Cena"])
            price_text = fields["Cena"] if price is not None else ""

        # The offer period is only in the status text, in Prague wall-clock time:
        # "Jiné formy PP (Prodej) (23.10.2026 13:00 - 10.11.2026 10:00)".
        start_iso, end_iso = _period_from_text(status_name)
        updated_iso = _iso_from_local(api_data.get("UpdatedDate"))

        org = api_data.get("Organization", {}) or {}
        org_name = org.get("u04Name", "") or fields.get("Instituce", "")

        facts: List[Tuple[str, Any]] = [
            ("Typ nabídky", _tender_kind(status_name)),
            ("Cena", _format_czk(price) if price else ""),
            ("Začátek nabídky", start_iso),
            ("Konec příjmu nabídek", end_iso),
            ("Forma", fields.get("Forma")),
        ] + self._contact_facts(contact)

        lat, lng, gps_source = self._coordinates(details.get("gps"), district)
        images, image_url = self._images(api_data, details)

        return {
            "id": f"{TENDER_ID_PREFIX}{tender_id}",
            "title": title,
            "url": f"{TENDER_DETAIL_URL}/{tender_id}",
            "category": _cut_at_word(category, CATEGORY_MAX_LEN),
            "price": price if price else 0.0,
            "price_text": price_text,
            "description": title[:500] if title else "",
            "full_description": _compose_full_description(
                description, status_name, org_name, facts
            ),
            "location": district,
            "views": 0,
            "date": start_iso or updated_iso,
            "is_top": api_data.get("TopProperty", False),
            "image_url": image_url,
            "contact_name": (contact.get("name") or org_name)[:CONTACT_NAME_MAX_LEN],
            "phone": (contact.get("phone") or "")[:PHONE_MAX_LEN],
            "coordinates_lat": lat,
            "coordinates_lng": lng,
            "images": images,
            "similar_listings": [],
            "scraped_at": datetime.now().isoformat(),
            "is_knocked_down": False,
            "date_from": start_iso,
            "date_to": end_iso,
            "offer_kind": "tender",
            "gps_source": gps_source,
            "contact_email": contact.get("email", ""),
        }


async def main():
    """Main function to run the scraper."""
    async with Actor, _FailRunOnCrash():
        Actor.log.info("Starting Nabidka Majetku Scraper (nabidkamajetku.gov.cz)")
        Actor.log.info("=" * 60)

        # Get input parameters
        input_data = await Actor.get_input() or {}

        max_listings = input_data.get("maxListings", 0)
        auction_status = input_data.get("auctionStatus", "active")
        price_min = input_data.get("priceMin", 0)
        price_max = input_data.get("priceMax", 0)
        location = input_data.get("location", "")
        search_query = input_data.get("searchQuery", "")
        category_id = input_data.get("categoryId", 0)
        include_tenders = input_data.get("includeTenders", True)
        include_details = input_data.get("includeDetailedData", True)
        run_deadline = _run_deadline()

        Actor.log.info(f"Configuration:")
        Actor.log.info(f"  Max listings: {max_listings if max_listings > 0 else 'unlimited'}")
        Actor.log.info(f"  Auction status: {auction_status}")
        Actor.log.info(f"  Price range: {price_min}-{price_max if price_max > 0 else 'unlimited'} CZK")
        Actor.log.info(f"  Location filter: {location if location else 'none'}")
        Actor.log.info(f"  Search query: {search_query if search_query else 'none'}")
        Actor.log.info(f"  Category ID: {category_id if category_id else 'all'}")
        Actor.log.info(f"  Include tenders (other forms of sale and lease): {include_tenders}")
        Actor.log.info(f"  Include detail pages: {include_details}")
        if run_deadline:
            Actor.log.info(f"  Detail pages stop by (UTC): {run_deadline.isoformat()}")

        # Initialize database connection
        db_manager_available = False
        try:
            from .database import db_manager

            scraper_name = os.environ.get("SCRAPER_NAME", "nabidka_majetku")
            db_manager.scraper_name = scraper_name

            # database.py falls back to localhost when DB_HOST is unset, which only
            # ever works on a dev machine. On the platform that means the Actor
            # version has no DB_* environment variables (env vars are per version).
            if Actor.is_at_home() and not os.environ.get("DB_HOST"):
                raise RuntimeError(
                    "DB_HOST is not set: add the DB_* environment variables to the "
                    "Actor version this build belongs to"
                )

            db_manager.initialize_pool()

            # Create actor run record
            actor_run_id = (
                os.environ.get("APIFY_ACTOR_RUN_ID")
                or os.environ.get("ACTOR_RUN_ID", "local-run")
            )
            actor_run_start = datetime.now()
            db_manager.set_actor_run_info(actor_run_id, actor_run_start)

            db_manager.create_actor_run(
                categories=[category_id or "all"],
                max_listings=max_listings,
                search_query=search_query,
                location=location,
                price_min=price_min,
                price_max=price_max,
            )

            Actor.log.info("Database connection established and actor run created")
            db_manager_available = True

        except Exception as e:
            Actor.log.error(f"Failed to initialize database: {e}")
            if Actor.is_at_home():
                # The database is the point of a platform run: without it nothing
                # reaches the map. Carrying on made the Apify run "succeed" while its
                # actor_runs row sat in READY until the janitor swept it 6 h later,
                # which hid a missing-env-vars deploy for three months. Fail loudly.
                raise
            Actor.log.warning(
                "Continuing without database integration - data will be stored in Apify dataset only"
            )
            db_manager_available = False

        # Create HTTP client
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "cs,en;q=0.5",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            scraper = NabidkaMajetkuScraper(client)
            scraper.deadline = run_deadline

            all_auctions = await scraper.scrape_all_auctions(
                max_listings=max_listings,
                auction_status=auction_status,
                price_min=price_min,
                price_max=price_max,
                location=location,
                search_query=search_query,
                category_id=category_id,
                include_details=include_details,
            )

            all_tenders: List[Dict[str, Any]] = []
            if include_tenders:
                # maxListings caps the whole run: auctions first, tenders get the rest.
                remaining = max_listings - len(all_auctions) if max_listings > 0 else 0
                if max_listings > 0 and remaining <= 0:
                    Actor.log.info(f"maxListings ({max_listings}) reached by auctions; skipping tenders")
                else:
                    all_tenders = await scraper.scrape_all_tenders(
                        max_listings=remaining,
                        price_min=price_min,
                        price_max=price_max,
                        location=location,
                        search_query=search_query,
                        category_id=category_id,
                        include_details=include_details,
                    )

            all_listings = all_auctions + all_tenders
            incomplete_reasons = list(scraper.incomplete_reasons)
            if not all_auctions and not incomplete_reasons:
                # Zero auctions used to fail the run on its own (see below); keep that
                # when tenders came back, or completing would flag every auction sold.
                incomplete_reasons.append("no auctions found")
            saved = 0  # rows actually inserted into the database

            # Save data to both Apify dataset and database
            if all_listings:
                await Actor.push_data(all_listings)
                Actor.log.info(
                    f"Saved {len(all_listings)} listings to Apify dataset "
                    f"({len(all_auctions)} auctions, {len(all_tenders)} tenders)"
                )

                if db_manager_available:
                    try:
                        # Only save active (non-closed) auctions to database
                        db_auctions = [
                            a for a in all_listings if not a.get("is_knocked_down", True)
                        ]
                        Actor.log.info(
                            f"Filtering for database: {len(db_auctions)} active listings "
                            f"out of {len(all_listings)} total"
                        )

                        if db_auctions:
                            Actor.log.info("Saving active listings to database...")
                            db_manager.insert_listings(db_auctions)
                            saved = len(db_auctions)
                            Actor.log.info(
                                f"Saved {len(db_auctions)} active listings to database"
                            )
                        else:
                            Actor.log.info("No active listings to save to database")
                    except Exception as e:
                        Actor.log.error(f"Failed to save listings to database: {e}")
                        try:
                            Actor.log.info(
                                "Attempting to refresh connection pool and retry"
                            )
                            db_manager.refresh_pool()
                            db_auctions = [
                                a
                                for a in all_listings
                                if not a.get("is_knocked_down", True)
                            ]
                            if db_auctions:
                                db_manager.insert_listings(db_auctions)
                                saved = len(db_auctions)
                                Actor.log.info(
                                    f"Saved {len(db_auctions)} active listings after retry"
                                )
                        except Exception as retry_e:
                            Actor.log.error(
                                f"Failed to save listings even after retry: {retry_e}"
                            )
                            incomplete_reasons.append(f"database insert failed: {retry_e!r}")
            else:
                Actor.log.warning("No listings found matching the specified criteria")

            # Zero auctions almost always means upstream breakage (API change,
            # auth/session failure) rather than an empty market — mark the run
            # 'failed' so sold-detection ignores it instead of flagging every
            # previously-seen listing as sold. The same goes for a run that stopped
            # early (a page failed, the insert failed): sold-detection would flag
            # everything it didn't reach as sold.
            run_status = "completed" if all_listings and not incomplete_reasons else "failed"
            for reason in incomplete_reasons:
                Actor.log.error(f"Run incomplete — {reason}")

            # Update actor run status
            if db_manager_available:
                try:
                    db_manager.update_actor_run_status(run_status, saved)
                    Actor.log.info("Updated actor run status in database")
                except Exception as e:
                    Actor.log.error(f"Failed to update actor run status: {e}")

            if incomplete_reasons:
                more = f" (+{len(incomplete_reasons) - 1} more)" if len(incomplete_reasons) > 1 else ""
                try:
                    await Actor.set_status_message(f"Stopped early: {incomplete_reasons[0]}{more}")
                except Exception as e:
                    Actor.log.warning(f"Could not set the run's status message: {e}")

            # Final summary
            Actor.log.info("=" * 60)
            Actor.log.info("SCRAPING COMPLETED")
            Actor.log.info(f"Total auctions scraped: {len(all_auctions)}")
            Actor.log.info(f"Total tenders scraped: {len(all_tenders)}")
            if include_details:
                exact = sum(1 for a in all_listings if a.get("gps_source") == "detail")
                Actor.log.info(
                    f"Detail pages: {scraper.detail_ok} ok, {scraper.detail_failures} "
                    f"failed after retries, {scraper.detail_skipped} skipped for the "
                    f"time budget (failed/skipped items keep their list data)"
                )
                Actor.log.info(
                    f"Exact GPS from detail pages: {exact} of {len(all_listings)} "
                    f"(the rest use the district centroid; "
                    f"{scraper.gps_rejected} out-of-country points ignored)"
                )
            if all_listings and db_manager_available:
                db_count = len(
                    [a for a in all_listings if not a.get("is_knocked_down", True)]
                )
                Actor.log.info(
                    f"Data saved to: Apify dataset ({len(all_listings)}) "
                    f"and database ({db_count} active)"
                )
            Actor.log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
