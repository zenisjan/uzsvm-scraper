# Nabídka Majetku Scraper (ÚZSVM)

An Apify Actor for scraping state-property offers from nabidkamajetku.gov.cz — the Czech government's ÚZSVM (Úřad pro zastupování státu ve věcech majetkových) property disposal portal.

## Features

- **Auctions**: the site's JSON API (`POST /api/Property/AuctionList`), paged 100 at a time
- **Other forms of sale and lease** ("Jiné formy prodeje a pronájmu": sale, gift, lease): `POST /api/Property/List` with `ListType: "alltenders"`, paged the same way. Their ids are stored as `T{id}` because property ids and auction ids are separate sequences
- **Detail pages**: every item's page (`/Home/AuctionDetail/{id}` for auctions, `/Home/Detail/{id}` for tenders) is fetched 4 at a time, request starts at least 0.25 s apart, for exact GPS, the contact person, phone and e-mail, all photos and the auction facts the list lacks (minimum bid, deposit, registration deadline)
- **Coordinates**: exact GPS from the detail page when it has one (real-estate auctions); otherwise the district centroid (movables and tenders carry `[0, 0]`)
- **Auction status filtering**: Filter by active, closed, prepared, or canceled auctions
- **Price & location filters**: Filter by price range and district name
- **Database integration**: PostgreSQL storage with actor run tracking
- **Dual storage**: Data saved to both Apify dataset and PostgreSQL database
- **Rate limiting**: 0.3 s between list pages; detail pages as above

## Input Configuration

All parameters are optional.

- **maxListings** (integer): Maximum listings to scrape (default: 0 = all)
- **auctionStatus** (string): Filter by status — `all`, `active`, `closed`, `prepared`, `canceled` (default: `active`)
- **priceMin** (integer): Minimum price in CZK (applied after fetching)
- **priceMax** (integer): Maximum price in CZK (applied after fetching)
- **location** (string): District name filter
- **searchQuery** (string): Search term for titles/descriptions
- **categoryId** (integer): Category filter (0 = all)
- **includeTenders** (boolean): Also scrape the other forms of sale and lease (default: true). `maxListings` counts auctions first, then tenders
- **includeDetailedData** (boolean): Fetch every item's detail page (default: true)

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
- **id**: Auction id, or `T{id}` for a tender (other form of sale and lease)
- **title**: Auction title
- **url**: Direct link to the detail page
- **category**: Property category (e.g. Pozemky, Stavby), cut at a word boundary to fit 50 characters
- **price**: Numeric price in CZK (auctions: the current price, i.e. the minimum bid until someone bids)
- **price_text**: Original price text with currency
- **description**: Short description
- **full_description**: The description, then `Stav: …` (status with the auction/offer period), `Organizace: …`, and one `Label: value` line per known fact:
  - auctions: `Nejnižší podání`, `Aktuální cena`, `Minimální příhoz`, `Počet příhozů`, `Kauce`, `Začátek aukce`, `Konec aukce`, `Konec přihlašování`, `Forma`, `E-mail`, `Adresa kontaktu`
  - tenders: `Typ nabídky` (Prodej / Nabídky daru / Pronájem a pacht), `Cena`, `Začátek nabídky`, `Konec příjmu nabídek`, `Forma`, `E-mail`, `Adresa kontaktu`
  - amounts read `123 000 Kč`; times are ISO 8601 in Europe/Prague with the offset
- **location**: District name
- **coordinates_lat / coordinates_lng**: Exact GPS from the detail page, else the district centroid (`gps_source`: `detail` / `district`, dataset only)
- **date**: Auction start (tenders: offer start), ISO 8601 in Europe/Prague with the offset, e.g. `2026-11-24T10:00:00+01:00`
- **date_from / date_to**: Auction or offer period, same format (dataset only)
- **is_top**: Whether listing is a top property
- **is_knocked_down**: Whether the auction is closed/sold
- **image_url**: Main image URL
- **images**: All photo URLs from the detail page (a list; the database stores it as a JSON array)
- **contact_name**: The contact person from the detail page (the organization if there is none)
- **phone**: The contact person's phone from the detail page
- **contact_email**, **offer_kind** (`auction` / `tender`): dataset only
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
  "date": "2026-01-15T10:00:00+01:00",
  "date_from": "2026-01-15T10:00:00+01:00",
  "date_to": "2026-03-15T10:00:00+01:00",
  "is_knocked_down": false,
  "scraped_at": "2026-03-11T10:30:00.000000"
}
```

## Technical Details

- **Language**: Python 3.13+
- **HTTP Client**: HTTPX (async)
- **Geocoding**: Built-in Czech city lookup (`czech_cities.py`)
- **Database**: PostgreSQL with connection pooling and retry logic
- **API**: POST-based JSON API with pagination (100 items/page), plus HTML detail pages parsed with BeautifulSoup
- **Time budget**: detail pages stop 2 minutes before the run's Apify timeout; the rest keep their list data

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

The scraper stores data in a shared PostgreSQL database alongside other scrapers (bazos, gfr). Only active (non-closed) auctions and the tenders are saved to the database; everything goes to the Apify dataset.

- **actor_runs**: Tracks each scraping session
- **listings**: Stores scraped auction data
- **No database on the platform fails the run**: the `DB_*` variables are set per Actor version (they live on version 0.0, which `.actor/actor.json` targets). If they are missing or the database is unreachable, a platform run fails instead of quietly saving to the dataset only. Local runs still continue with dataset-only storage.
- **A run that may have missed listings ends `failed`**, so sold-detection ignores it: a failed, malformed or empty auction or tender list page before `PropertyTotalCount`, a page that only repeats earlier rows, a row that cannot be converted, no auctions at all, or a failed insert. The reasons are logged (`Run incomplete — …`) and set as the run's status message. A failed detail page is not one of them: the item keeps its list data (district centroid, organization as contact) and is counted in the log.
