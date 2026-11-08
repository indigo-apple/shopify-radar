# Shopify Store Radar

Track any Shopify store's full catalog — prices, discounts, variant-level stock — and get told **exactly what changed** since the last run.

Built on **UCP** (Universal Commerce Protocol), the machine-readable product feed Shopify publishes for AI agents. That means no HTML scraping and no CSS selectors that break every other week — plus variant-level stock that `/products.json` does not expose.

## What you get

One row per product:

| Field | Meaning |
|---|---|
| `store` | Domain you asked for |
| `productId`, `handle`, `url` | Identity and link |
| `title`, `vendor`, `productType`, `tags` | Basic merchandising data |
| `priceMin` / `priceMax` | Price range across variants |
| `compareAtPrice` | The "was" price (strike-through) |
| `discountPct` | Effective discount, e.g. `58.6` |
| `currency` | Currency of the prices |
| `available` | `true` if **any** variant is in stock |
| `variantCount`, `variants[]` | Per-variant SKU, price and stock |
| `imageUrl` | Main product image |
| `sitemapUpdatedAt` | Last time the merchant touched the product page |
| `changeType` | `new` / `delisted` / `price_drop` / `price_rise` / `sold_out` / `back_in_stock` |
| `previousPrice`, `changePct` | For price changes |
| `firstSeenAt`, `lastSeenAt` | When this actor first/last saw the product |

## Why this actor and not a scraper

Shopify exposes `/products.json` on most stores, and plenty of scrapers use it. Two problems with that:

1. **It rate limits globally per IP.** Hit a few stores in a row and every request returns `429` for minutes.
2. **It only tells you in-stock true/false**, with no real stock data.

The UCP endpoint is an official protocol endpoint, it exposes variant-level availability and price ranges, and it is versioned — so it does not break silently.

### About rate limits (measured, not assumed)

UCP is not a free-for-all. In testing:

| Test | Result |
|---|---|
| 45 requests to one store, 1s apart | all OK |
| 40 pages of a very large catalog, no delay | `429`, ~1h cool-down |
| Hitting store A until throttled, then store B | store B unaffected |

So throttling is **per store and burst-sensitive**, not a global IP ban. That is why this actor spaces page requests (`pageDelaySec`, default 1s) and, when a store hands back a long cool-down, reports it in `storesFailed` instead of stalling the run. If a big store throttles you, raise `pageDelaySec` to 2–3s or lower `maxProductsPerStore`.

## Input

```json
{
  "stores": ["allbirds.com", "fashionnova.com"],
  "maxProductsPerStore": 250,
  "detectChanges": true,
  "onlyChanges": false,
  "includeSitemap": true
}
```

| Input | Default | Notes |
|---|---|---|
| `stores` | — | Required. Domains or full URLs, up to 50. |
| `maxProductsPerStore` | `250` | `0` = whole catalog. One product = one billed item. |
| `detectChanges` | `true` | Snapshot is kept per store between runs. |
| `onlyChanges` | `false` | Return only changed products — cheap for daily schedules. |
| `includeSitemap` | `true` | Adds `sitemapUpdatedAt`. A few seconds per store. |
| `requestTimeoutSec` | `45` | Per-request timeout. |

## Use cases

- **Competitor monitoring** — schedule daily runs and read the `changes` view to see who cut prices, what sold out, what they launched.
- **Assortment research** — pull a whole category from 10 brands and compare price points and discount depth.
- **Restock alerts** — run hourly on a few SKUs you care about, filter for `back_in_stock`.
- **Discount hunting** — sort by `discountPct` to find real markdowns instead of the perennial "was" prices.
- **Market entry** — count products per type and median price to size a niche before you build for it.

### Running this continuously

Apify can schedule this actor (Apache-style cron) — for example every day at 08:00 UTC with `onlyChanges: true`. Each run compares against the snapshot left by the previous one, so you accumulate a price history without re-reading everything you already know.

## Coverage

UCP is rolling out across Shopify. Most large stores already publish it. A store that does not will be reported in the run log and in the `SUMMARY` record (`storesFailed`) rather than failing the whole run.

## Limitations

- **No publish date.** UCP does not expose `published_at`. `sitemapUpdatedAt` is the closest signal, and it changes on *any* edit, so treat it as "recently modified", not "new product". True new-product detection comes from comparing snapshots over time — that is what `changeType: "new"` does.
- **Stock is a boolean per variant**, not a quantity. Shopify does not expose exact on-hand counts publicly.
- **Prices are variant price ranges**, not per-market pricing. Currency comes from the store's default market.

## Notes for developers

The protocol requires every request to include `meta.ucp-agent.profile` — a publicly reachable JSON document that the store fetches and validates. This actor ships its own at `ucp-agent-profile.json`; override it via the `agentProfileUrl` input if you host your own.

Source: https://github.com/indigo-apple/shopify-radar
