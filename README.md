# Nabídka Majetku Scraper (ÚZSVM)

An Apify Actor for scraping property auction listings from nabidkamajetku.gov.cz — the Czech government's ÚZSVM (Úřad pro zastupování státu ve věcech majetkových) property disposal portal.

## Features

- **Direct API access**: Uses the site's JSON API (`/api/Property/AuctionList`) — no HTML parsing needed
- **Auction status filtering**: Filter by active, closed, prepared, or canceled auctions
- **Price & location filters**: Filter by price range and district name
- **Geocoding**: Automatic Czech city/district to GPS coordinate mapping
- **Database integration**: PostgreSQL storage with actor run tracking
- **Dual storage**: Data saved to both Apify dataset and PostgreSQL database
- **Rate limiting**: Respectful 0.3s delay between API pages

## Input Configuration

All parameters are optional.

- **maxListings** (integer): Maximum listings to scrape (default: 0 = all)
- **auctionStatus** (string): Filter by status — `all`, `active`, `closed`, `prepared`, `canceled` (default: `active`)
- **priceMin** (integer): Minimum price in CZK (applied after fetching)
- **priceMax** (integer): Maximum price in CZK (applied after fetching)
- **location** (string): District name filter
- **searchQuery** (string): Search term for titles/descriptions
- **categoryId** (integer): Category filter (0 = all)

## Example Input

```json
{
  "maxListings": 100,
  "auctionStatus": "active",
  "priceMin": 50000,
  "priceMax": 5000000,
  "location": "Praha"
}
```

## Output Data

Each listing includes:
- **id**: Unique auction identifier
- **title**: Auction title
- **url**: Direct link to auction detail page
- **category**: Property category (e.g. Pozemky, Stavby)
- **price**: Numeric price in CZK
- **price_text**: Original price text with currency
- **description**: Short description
- **full_description**: Full description with status and organization info
- **location**: District name
- **coordinates_lat / coordinates_lng**: GPS coordinates (geocoded from district)
- **date**: Auction start date
- **date_from / date_to**: Auction period
- **is_top**: Whether listing is a top property
- **is_knocked_down**: Whether the auction is closed/sold
- **image_url**: Main image URL
- **images**: JSON array of image URLs
- **contact_name**: Organization name
- **scraped_at**: Timestamp when scraped

## Example Output

```json
{
  "id": "12345",
  "title": "Pozemek p.č. 1234/5 v k.ú. Praha",
  "url": "https://www.nabidkamajetku.gov.cz/Home/AuctionDetail/12345",
  "category": "Pozemky",
  "price": 150000,
  "price_text": "150 000,00 Kč",
  "location": "Praha",
  "coordinates_lat": 50.0755,
  "coordinates_lng": 14.4378,
  "date_from": "2026-01-15T00:00:00",
  "date_to": "2026-03-15T00:00:00",
  "is_knocked_down": false,
  "scraped_at": "2026-03-11T10:30:00.000000"
}
```

## Technical Details

- **Language**: Python 3.13+
- **HTTP Client**: HTTPX (async)
- **Geocoding**: Built-in Czech city lookup (`czech_cities.py`)
- **Database**: PostgreSQL with connection pooling and retry logic
- **API**: POST-based JSON API with pagination (100 items/page)

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `DB_HOST` | PostgreSQL host | localhost |
| `DB_PORT` | PostgreSQL port | 25432 |
| `DB_NAME` | Database name | bazos_scraper |
| `DB_USER` | Database user | postgres |
| `DB_PASSWORD` | Database password | — |
| `DB_SSL_MODE` | SSL mode | prefer |
| `SCRAPER_NAME` | Override scraper name | nabidka_majetku |
| `APIFY_ACTOR_RUN_ID` | Apify run ID | local-run |

## Usage

### Apify Platform
```bash
apify push
```

### Local Development
```bash
apify run --input='{"auctionStatus": "active", "maxListings": 50}'
```

## Database Integration

The scraper stores data in a shared PostgreSQL database alongside other scrapers (bazos, gfr). Only active (non-closed) auctions are saved to the database; all auctions go to the Apify dataset.

- **actor_runs**: Tracks each scraping session
- **listings**: Stores scraped auction data
- **No database on the platform fails the run**: the `DB_*` variables are set per Actor version (they live on version 0.0, which `.actor/actor.json` targets). If they are missing or the database is unreachable, a platform run fails instead of quietly saving to the dataset only. Local runs still continue with dataset-only storage.
