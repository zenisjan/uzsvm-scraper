#!/usr/bin/env python3
"""
ÚZSVM Property Auction Scraper (nabidkamajetku.gov.cz)
======================================================

Scrapes property auction listings from the Czech government's ÚZSVM
(Úřad pro zastupování státu ve věcech majetkových) property portal.

Uses the site's JSON API directly:
- POST /api/Property/AuctionList — paginated auction listings
- Images via /api/Property/Attachment/{imageId}
- Detail pages at /Home/Detail/{id}
"""

import asyncio
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from apify import Actor

try:
    from .czech_cities import geocode_czech_city
except ImportError:
    from czech_cities import geocode_czech_city


BASE_URL = "https://www.nabidkamajetku.gov.cz"
API_AUCTION_LIST = f"{BASE_URL}/api/Property/AuctionList"
API_ATTACHMENT = f"{BASE_URL}/api/Property/Attachment"
DETAIL_URL = f"{BASE_URL}/Home/AuctionDetail"


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
    """Scraper for nabidkamajetku.gov.cz using the site's JSON API."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def scrape_all_auctions(
        self,
        max_listings: int = 0,
        auction_status: str = "active",
        price_min: int = 0,
        price_max: int = 0,
        location: Optional[str] = None,
        search_query: Optional[str] = None,
        category_id: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Scrape auctions from the paginated API and apply filters.

        Args:
            max_listings: Maximum number of listings to return (0 = all)
            auction_status: Filter by status ("all", "active", "closed", "prepared", "canceled")
            price_min: Minimum price filter in CZK
            price_max: Maximum price filter in CZK
            location: Location filter (district name)
            search_query: Search term to filter titles/descriptions
            category_id: Category ID filter (0 = all)

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

            # Apply local filters (price, location)
            filtered = self._apply_filters(
                all_auctions, price_min, price_max, location, search_query
            )

            Actor.log.info(f"After filtering: {len(filtered)} auctions match criteria")

            # Limit results if requested
            if max_listings > 0 and len(filtered) > max_listings:
                filtered = filtered[:max_listings]
                Actor.log.info(f"Limited to {max_listings} auctions as requested")

            # Convert to standardized format
            Actor.log.info("Converting to standardized format...")
            standardized = []
            for auction_data in filtered:
                converted = self._convert_to_standard_format(auction_data)
                if converted:
                    standardized.append(converted)

            Actor.log.info(f"Final result: {len(standardized)} auctions ready for storage")
            return standardized

        except Exception as e:
            Actor.log.error(f"Error in scrape_all_auctions: {e}")
            return []

    async def _fetch_all_pages(
        self,
        list_type: str,
        fulltext: Optional[str],
        category_id: int,
        max_listings: int,
    ) -> List[Dict[str, Any]]:
        """Fetch all auction pages from the API."""
        all_auctions: List[Dict[str, Any]] = []
        page = 1
        page_size = 100  # max reasonable page size

        while True:
            body = {
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

            try:
                response = await _request_with_retry(
                    self.client,
                    "POST",
                    API_AUCTION_LIST,
                    json=body,
                    timeout=60.0,
                )

                if response.status_code != 200:
                    Actor.log.error(
                        f"API returned {response.status_code}: {response.text[:500]}"
                    )
                    break

                data = response.json()
                auctions = data.get("Auctions", [])
                total_count = data.get("PropertyTotalCount", 0)

                if not auctions:
                    break

                all_auctions.extend(auctions)
                Actor.log.info(
                    f"Page {page}: fetched {len(auctions)} auctions "
                    f"({len(all_auctions)}/{total_count} total)"
                )

                # Stop if we have enough
                if max_listings > 0 and len(all_auctions) >= max_listings:
                    break

                # Stop if we've fetched all pages
                if len(all_auctions) >= total_count:
                    break

                page += 1

                # Small delay to be polite
                await asyncio.sleep(0.3)

            except Exception as e:
                Actor.log.error(f"Failed to fetch page {page}: {e}")
                break

        return all_auctions

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
        try:
            # Remove spaces, replace comma with dot
            cleaned = price_str.replace(" ", "").replace("\xa0", "").replace(",", ".")
            return float(cleaned)
        except (ValueError, AttributeError):
            return None

    def _geocode_location(self, location: str) -> Optional[float]:
        """Return latitude for a Czech city/district name, or None."""
        if not location:
            return None
        coords = geocode_czech_city(location.strip())
        return coords[0] if coords else None

    def _geocode_location_lng(self, location: str) -> Optional[float]:
        """Return longitude for a Czech city/district name, or None."""
        if not location:
            return None
        coords = geocode_czech_city(location.strip())
        return coords[1] if coords else None

    def _convert_to_standard_format(
        self, api_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Convert API data to standardized listing format matching the DB schema."""
        try:
            auction_id = str(api_data.get("Id", ""))
            if not auction_id:
                return None

            # Basic fields
            title = api_data.get("Name", "") or ""
            description = api_data.get("Description", "") or ""
            category = api_data.get("CategoryName", "") or ""
            district = api_data.get("DistrictName", "") or ""
            status_name = api_data.get("StatusName", "") or ""

            # Price
            price = self._parse_price(api_data.get("Price"))
            price_text = api_data.get("Price", "") or ""
            if price_text:
                price_text = f"{price_text} Kč"

            # Dates
            start_date = api_data.get("StartDate", "")
            end_date = api_data.get("EndDate", "")
            updated_date = api_data.get("UpdatedDate", "")

            # Organization
            org = api_data.get("Organization", {}) or {}
            org_name = org.get("u04Name", "")

            # Image
            image_id = api_data.get("ImageId", "")
            image_url = f"{API_ATTACHMENT}/{image_id}" if image_id else ""

            # Build URL
            auction_url = f"{DETAIL_URL}/{auction_id}"

            # Build full description
            full_description = description
            if status_name:
                full_description += f"\n\nStav: {status_name}"
            if org_name:
                full_description += f"\n\nOrganizace: {org_name}"

            # Auction status
            auction_status = api_data.get("AuctionStatus", 0)
            # 0 = announced, 1 = active/running, 2 = closed
            is_knocked_down = auction_status == 2

            # Images array
            images = [image_url] if image_url else []

            return {
                "id": auction_id,
                "title": title,
                "url": auction_url,
                "category": category[:50],
                "price": price if price else 0.0,
                "price_text": price_text,
                "description": title[:500] if title else "",
                "full_description": full_description[:5000],
                "location": district,
                "views": 0,
                "date": start_date or updated_date,
                "is_top": api_data.get("TopProperty", False),
                "image_url": image_url,
                "contact_name": org_name,
                "phone": "",
                "coordinates_lat": self._geocode_location(district),
                "coordinates_lng": self._geocode_location_lng(district),
                "images": json.dumps(images),
                "similar_listings": json.dumps([]),
                "scraped_at": datetime.now().isoformat(),
                "is_knocked_down": is_knocked_down,
                "date_from": start_date,
                "date_to": end_date,
            }

        except Exception as e:
            Actor.log.warning(f"Error converting auction data: {e}")
            return None


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

        Actor.log.info(f"Configuration:")
        Actor.log.info(f"  Max listings: {max_listings if max_listings > 0 else 'unlimited'}")
        Actor.log.info(f"  Auction status: {auction_status}")
        Actor.log.info(f"  Price range: {price_min}-{price_max if price_max > 0 else 'unlimited'} CZK")
        Actor.log.info(f"  Location filter: {location if location else 'none'}")
        Actor.log.info(f"  Search query: {search_query if search_query else 'none'}")
        Actor.log.info(f"  Category ID: {category_id if category_id else 'all'}")

        # Initialize database connection
        db_manager_available = False
        try:
            from .database import db_manager

            scraper_name = os.environ.get("SCRAPER_NAME", "nabidka_majetku")
            db_manager.scraper_name = scraper_name

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

            all_auctions = await scraper.scrape_all_auctions(
                max_listings=max_listings,
                auction_status=auction_status,
                price_min=price_min,
                price_max=price_max,
                location=location,
                search_query=search_query,
                category_id=category_id,
            )

            # Save data to both Apify dataset and database
            if all_auctions:
                await Actor.push_data(all_auctions)
                Actor.log.info(f"Saved {len(all_auctions)} auctions to Apify dataset")

                if db_manager_available:
                    try:
                        # Only save active (non-closed) auctions to database
                        db_auctions = [
                            a for a in all_auctions if not a.get("is_knocked_down", True)
                        ]
                        Actor.log.info(
                            f"Filtering for database: {len(db_auctions)} active auctions "
                            f"out of {len(all_auctions)} total"
                        )

                        if db_auctions:
                            Actor.log.info("Saving active auctions to database...")
                            db_manager.insert_listings(db_auctions)
                            Actor.log.info(
                                f"Saved {len(db_auctions)} active listings to database"
                            )
                        else:
                            Actor.log.info("No active auctions to save to database")
                    except Exception as e:
                        Actor.log.error(f"Failed to save listings to database: {e}")
                        try:
                            Actor.log.info(
                                "Attempting to refresh connection pool and retry"
                            )
                            db_manager.refresh_pool()
                            db_auctions = [
                                a
                                for a in all_auctions
                                if not a.get("is_knocked_down", True)
                            ]
                            if db_auctions:
                                db_manager.insert_listings(db_auctions)
                                Actor.log.info(
                                    f"Saved {len(db_auctions)} active listings after retry"
                                )
                        except Exception as retry_e:
                            Actor.log.error(
                                f"Failed to save listings even after retry: {retry_e}"
                            )
            else:
                Actor.log.warning("No auctions found matching the specified criteria")

            # Zero auctions almost always means upstream breakage (API change,
            # auth/session failure) rather than an empty market — mark the run
            # 'failed' so sold-detection ignores it instead of flagging every
            # previously-seen listing as sold.
            run_status = "completed" if all_auctions else "failed"

            # Update actor run status
            if db_manager_available:
                try:
                    db_manager.update_actor_run_status(run_status, len(all_auctions))
                    Actor.log.info("Updated actor run status in database")
                except Exception as e:
                    Actor.log.error(f"Failed to update actor run status: {e}")

            # Final summary
            Actor.log.info("=" * 60)
            Actor.log.info("SCRAPING COMPLETED")
            Actor.log.info(f"Total auctions scraped: {len(all_auctions)}")
            if all_auctions and db_manager_available:
                db_count = len(
                    [a for a in all_auctions if not a.get("is_knocked_down", True)]
                )
                Actor.log.info(
                    f"Data saved to: Apify dataset ({len(all_auctions)}) "
                    f"and database ({db_count} active)"
                )
            Actor.log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
