"""Snapshot persistence and change detection.

An Apify actor is stateless: every run gets a fresh container and a fresh
run-scoped key-value store. To detect "what changed since last time" we need a
store that survives between runs, so we use a *named* key-value store
(``shopify-radar-baseline``) and keep one record per domain.

Comparison is a plain diff of ``{price, available}`` per product id, which is
enough for the five events that actually matter to a merchant watching a
competitor:

    new            product shows up that we have never seen in this store
    delisted       product vanished from the catalog
    price_drop     price_min fell
    price_rise     price_min rose
    sold_out       was available, now fully out of stock
    back_in_stock  was out of stock, now available
"""
import json

BASELINE_STORE = "shopify-radar-baseline"
SCHEMA_VERSION = 1


def _key(domain):
    return f"baseline-{domain}"


def diff(old, rows, skip_delisted=False):
    """Compare a previous snapshot with the current rows.

    ``old`` is ``{product_id: {"p": price, "a": available, "t": title}}``.
    Returns a list of event dicts (empty when nothing meaningful changed).
    """
    events = []
    seen = set()
    for r in rows:
        pid = r["product_id"]
        seen.add(pid)
        prev = old.get(pid)
        price = r.get("price_min")
        avail = 1 if r.get("available") else 0

        if prev is None:
            events.append(
                {"type": "new", "product_id": pid, "title": r.get("title"),
                 "to": price}
            )
            continue

        old_price = prev.get("p")
        if price is not None and old_price is not None and abs(price - old_price) > 0.009:
            kind = "price_drop" if price < old_price else "price_rise"
            pct = (price - old_price) / old_price * 100 if old_price else None
            events.append(
                {"type": kind, "product_id": pid, "title": r.get("title"),
                 "from": old_price, "to": price,
                 "pct": round(pct, 2) if pct is not None else None}
            )

        old_avail = prev.get("a")
        if old_avail == 1 and avail == 0:
            events.append({"type": "sold_out", "product_id": pid,
                           "title": r.get("title")})
        elif old_avail == 0 and avail == 1:
            events.append({"type": "back_in_stock", "product_id": pid,
                           "title": r.get("title")})

    for pid, prev in old.items():
        if pid not in seen and not skip_delisted:
            events.append({"type": "delisted", "product_id": pid,
                           "title": prev.get("t"), "from": prev.get("p")})
    return events


def compact(rows):
    """Rows -> the minimal shape we need to remember for the next run."""
    return {
        r["product_id"]: {
            "p": r.get("price_min"),
            "a": 1 if r.get("available") else 0,
            "t": (r.get("title") or "")[:140],
        }
        for r in rows
    }


async def load(store, domain):
    """Read the previous snapshot for ``domain``. Returns (products, scanned_at)."""
    raw = await store.get_value(_key(domain))
    if not raw:
        return {}, None
    try:
        data = raw if isinstance(raw, dict) else json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}, None
    if data.get("v") != SCHEMA_VERSION:
        return {}, None
    return data.get("products") or {}, data.get("scannedAt")


async def save(store, domain, rows, scanned_at, previous=None, keep_missing=False):
    """Write the current snapshot.

    ``previous`` lets us carry forward ``firstSeenAt`` so a product keeps its
    original discovery date across runs. Delisted products are intentionally
    dropped: the ``delisted`` event is emitted in the run that observes it, and
    keeping ghosts around would re-report them forever.
    """
    products = compact(rows)
    for pid, cur in products.items():
        old = (previous or {}).get(pid)
        cur["f"] = (old.get("f") if old else None) or scanned_at
    if keep_missing and previous:
        # Truncated scan: keep products we did not reach this time, otherwise
        # they would silently vanish from the baseline and reappear as "new".
        for pid, prev in previous.items():
            products.setdefault(pid, prev)
    await store.set_value(
        _key(domain),
        {"v": SCHEMA_VERSION, "scannedAt": scanned_at, "products": products},
    )
    return len(products)
