"""Apify Actor: Shopify Store Radar.

Give it a list of Shopify store domains. For each one it reads the store's
public UCP (Universal Commerce Protocol) MCP endpoint — the official product
feed Shopify ships for AI agents — and returns every product with price,
compare-at price, discount, variant-level stock, and the change events since
the previous run (new, delisted, price up/down, sold out, back in stock).

Typical uses: competitor price monitoring, assortment tracking, restock
watchlists, discount hunting, market research on any Shopify brand.
"""
import asyncio
import time
from datetime import datetime, timezone

from apify import Actor

from . import baseline as bl
from . import sitemap, ucp

MAX_CONCURRENT_STORES = 4


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def scan_store(domain, opts):
    """Scan one store. Returns a result dict; never raises."""
    t0 = time.time()
    domain = ucp.normalize_domain(domain)
    res = {"store": domain, "ok": False, "products": 0, "events": 0,
           "elapsedSec": 0.0, "error": None, "ucpEndpoint": None}
    try:
        client = ucp.UCPClient(domain, agent_profile=opts["agent_profile"])
        res["ucpEndpoint"] = client.endpoint

        products = await asyncio.to_thread(
            client.search_catalog,
            "",
            opts["max_products"],
            opts["max_pages"],
            lambda m: Actor.log.debug(f"[{domain}] {m}"),
            opts["page_delay"],
        )
        rows = ucp.to_rows(products)
        if not rows:
            res["error"] = "catalog returned no products"
            return res

        # If we stopped before the catalog ended, the product set is a prefix of
        # the real one, and every product past the cut would look "delisted".
        # Suppress delisted detection in that case instead of crying wolf.
        truncated = len(products) >= opts["max_products"] and opts["max_products"] < 1_000_000
        res["truncated"] = truncated

        if opts["include_sitemap"]:
            mods = await asyncio.to_thread(
                sitemap.fetch_lastmod, domain, 5, opts["timeout"]
            )
            for r in rows:
                r["sitemap_updated_at"] = mods.get(r.get("handle"))

        old, old_scanned = ({}, None)
        if opts["detect_changes"]:
            old, old_scanned = await bl.load(opts["baseline"], domain)

        events = bl.diff(old, rows, skip_delisted=truncated) if opts["detect_changes"] else []
        by_pid = {}
        for e in events:
            by_pid.setdefault(e["product_id"], []).append(e)

        now = _now_iso()
        items = []
        for r in rows:
            evs = by_pid.get(r["product_id"]) or []
            primary = _primary_event(evs)
            if opts["only_changes"] and old and not evs:
                continue
            prev_price = None
            change_pct = None
            if primary and primary["type"] in ("price_drop", "price_rise"):
                prev_price = primary.get("from")
                change_pct = primary.get("pct")
            handle = r.get("handle")
            items.append({
                "store": domain,
                "shopifyShop": client.endpoint.split("/")[2],
                "productId": r["product_id"],
                "handle": handle,
                "title": r.get("title"),
                "url": f"https://{domain}/products/{handle}" if handle else None,
                "priceMin": r.get("price_min"),
                "priceMax": r.get("price_max"),
                "compareAtPrice": r.get("compare_at"),
                "discountPct": r.get("discount_pct"),
                "currency": r.get("currency"),
                "available": r.get("available"),
                "variantCount": r.get("variant_count"),
                "variants": r.get("variants"),
                "vendor": r.get("vendor"),
                "productType": r.get("product_type"),
                "tags": r.get("tags"),
                "imageUrl": r.get("image"),
                "sitemapUpdatedAt": r.get("sitemap_updated_at"),
                "changeType": primary["type"] if primary else None,
                "previousPrice": prev_price,
                "changePct": change_pct,
                "firstSeenAt": _first_seen(old, r["product_id"], now),
                "lastSeenAt": now,
                "baselineAvailable": bool(old),
            })

        # Persist before pushing so a crash mid-push does not lose the baseline.
        if opts["detect_changes"]:
            await bl.save(opts["baseline"], domain, rows, now, previous=old,
                          keep_missing=truncated)

        # Delisted products no longer exist in the catalog, so they cannot come
        # from `rows` — append them from the diff so the user can see what the
        # store dropped.
        if opts["include_delisted"]:
            for e in events:
                if e["type"] != "delisted":
                    continue
                items.append({
                    "store": domain,
                    "shopifyShop": client.endpoint.split("/")[2],
                    "productId": e["product_id"],
                    "handle": None,
                    "title": e.get("title"),
                    "url": None,
                    "priceMin": None,
                    "priceMax": None,
                    "compareAtPrice": None,
                    "discountPct": None,
                    "currency": None,
                    "available": False,
                    "variantCount": None,
                    "variants": None,
                    "vendor": None,
                    "productType": None,
                    "tags": None,
                    "imageUrl": None,
                    "sitemapUpdatedAt": None,
                    "changeType": "delisted",
                    "previousPrice": e.get("from"),
                    "changePct": None,
                    "firstSeenAt": _first_seen(old, e["product_id"], now),
                    "lastSeenAt": now,
                    "baselineAvailable": True,
                })

        if items:
            await Actor.push_data(items)

        res.update(ok=True, products=len(rows), events=len(events),
                   elapsedSec=round(time.time() - t0, 1))
        Actor.log.info(
            f"[{domain}] {len(rows)} products, {len(events)} changes, "
            f"{len(items)} rows pushed in {res['elapsedSec']}s"
        )
        return res
    except ucp.RateLimited as e:
        res["error"] = f"rate limited by store (cooldown {e.retry_after or '?'}s)"
        Actor.log.warning(
            f"[{domain}] throttled by the store — {e}. "
            f"Raise pageDelaySec or lower maxProductsPerStore for this store."
        )
    except ucp.NotUCP as e:
        res["error"] = f"not UCP: {e}"
        Actor.log.warning(f"[{domain}] skipped — {e}")
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        Actor.log.error(f"[{domain}] failed — {res['error']}")
    res["elapsedSec"] = round(time.time() - t0, 1)
    return res


def _first_seen(old, pid, now):
    prev = old.get(pid) or {}
    return prev.get("f") or now


def _primary_event(events):
    """Pick the event worth surfacing when a product changed in several ways.

    Price is what people watch, so price events win over stock events.
    """
    if not events:
        return None
    order = {"price_drop": 0, "price_rise": 1, "new": 2,
             "back_in_stock": 3, "sold_out": 4, "delisted": 5}
    return sorted(events, key=lambda e: order.get(e["type"], 9))[0]


async def main():
    async with Actor:
        inp = await Actor.get_input() or {}

        stores = inp.get("stores") or []
        if isinstance(stores, str):
            stores = [s.strip() for s in stores.replace(",", "\n").split("\n")]
        stores = [ucp.normalize_domain(s) for s in stores if s and s.strip()]
        # de-duplicate but keep the user's order
        seen, ordered = set(), []
        for s in stores:
            if s not in seen:
                seen.add(s)
                ordered.append(s)
        stores = ordered[:50]

        if not stores:
            Actor.log.error("No stores given. Add at least one domain, e.g. allbirds.com")
            await Actor.set_value("SUMMARY", {"stores": 0, "error": "no input"})
            return

        opts = {
            "max_products": int(inp.get("maxProductsPerStore", 250)) or 1_000_000,
            "max_pages": int(inp.get("maxProductPages", 25)),
            "detect_changes": bool(inp.get("detectChanges", True)),
            "only_changes": bool(inp.get("onlyChanges", False)),
            "include_delisted": bool(inp.get("includeDelisted", True)),
            "include_sitemap": bool(inp.get("includeSitemap", True)),
            "page_delay": float(inp.get("pageDelaySec", 1.0)),
            "timeout": int(inp.get("requestTimeoutSec", 45)),
            "agent_profile": (inp.get("agentProfileUrl") or "").strip()
            or ucp.DEFAULT_AGENT_PROFILE,
        }
        if opts["only_changes"] and not opts["detect_changes"]:
            Actor.log.warning("onlyChanges needs detectChanges — enabling it")
            opts["detect_changes"] = True

        kv = await Actor.open_key_value_store(name=bl.BASELINE_STORE)
        opts["baseline"] = kv

        Actor.log.info(
            f"Scanning {len(stores)} store(s): max {opts['max_products']} products each, "
            f"change detection {'on' if opts['detect_changes'] else 'off'}"
        )

        sem = asyncio.Semaphore(MAX_CONCURRENT_STORES)

        async def guarded(d):
            async with sem:
                return await scan_store(d, opts)

        results = await asyncio.gather(*(guarded(d) for d in stores))

        ok = [r for r in results if r["ok"]]
        summary = {
            "storesRequested": len(stores),
            "storesScanned": len(ok),
            "storesFailed": [r["store"] for r in results if not r["ok"]],
            "products": sum(r["products"] for r in ok),
            "changes": sum(r["events"] for r in ok),
            "elapsedSec": round(sum(r["elapsedSec"] for r in ok), 1),
            "perStore": results,
        }
        await Actor.set_value("SUMMARY", summary)
        Actor.log.info(
            f"Done: {summary['storesScanned']}/{summary['storesRequested']} stores, "
            f"{summary['products']} products, {summary['changes']} change events"
        )
