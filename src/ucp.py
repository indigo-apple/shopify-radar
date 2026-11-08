"""Shopify UCP (Universal Commerce Protocol) MCP client.

Every Shopify store that implements UCP publishes a machine-readable profile at
``https://<store>/.well-known/ucp``. That profile advertises an MCP endpoint
(usually ``https://<shop>.myshopify.com/api/ucp/mcp``) exposing tools such as
``search_catalog``, ``get_product``, ``lookup_catalog`` and ``ask_business``.

Why this beats ``/products.json``:

  * it is an official, versioned protocol — no scraping, no CSS selectors;
  * it exposes real variant-level availability (``products.json`` only gives a
    boolean) and price ranges plus list (compare-at) price ranges;
  * ``/products.json`` rate limits globally per IP (429 for minutes), while the
    UCP endpoint does not.

Protocol quirk worth knowing: every ``tools/call`` must carry
``meta.ucp-agent.profile`` — a publicly reachable URL that the store fetches and
validates before answering. The store demands the referenced document contain
``ucp.version``, ``ucp.services``, ``ucp.capabilities`` and
``ucp.payment_handlers``, otherwise it answers 422 ``profile_malformed``.
This actor therefore ships its own hosted agent profile (see
``ucp-agent-profile.json`` in the repo) instead of borrowing the store's.
"""
import json
import time
import urllib.error
import urllib.request

UA = "shopify-radar/1.0 (UCP agent; +https://github.com/indigo-apple/shopify-radar)"

# Publicly hosted agent profile. Required by the protocol on every tools/call.
#
# Hosting matters: the store fetches this URL and rejects anything it does not
# read as JSON. ``raw.githubusercontent.com`` serves ``.json`` as ``text/plain``
# and is rejected with ``profile_malformed: Invalid content type`` — so the
# profile is served through a CDN that sets ``application/json``.
# The list is ordered: the first entry is used, later ones are fallbacks if a
# CDN is unreachable from the store's side.
DEFAULT_AGENT_PROFILES = (
    "https://cdn.jsdelivr.net/gh/indigo-apple/shopify-radar@main/ucp-agent-profile.json",
    "https://raw.githack.com/indigo-apple/shopify-radar/main/ucp-agent-profile.json",
)


class UCPError(RuntimeError):
    pass


class RateLimited(UCPError):
    """The store throttled us. ``retry_after`` is in seconds when advertised."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class NotUCP(UCPError):
    """The store does not implement UCP (or has it disabled)."""


def normalize_domain(raw):
    """Turn user input into a bare domain: no scheme, no path, no www."""
    d = (raw or "").strip().lower()
    for p in ("https://", "http://"):
        if d.startswith(p):
            d = d[len(p):]
    d = d.split("/")[0].split("?")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def _retry_after(e, detail):
    """Pull the cooldown out of a 429, which Shopify reports inconsistently."""
    hdr = e.headers.get("Retry-After") if e.headers else None
    if hdr:
        try:
            return int(float(hdr))
        except (TypeError, ValueError):
            pass
    import re

    m = re.search(r"retry (?:after|in) (\d+)", detail or "", re.I)
    if m:
        return int(m.group(1))
    return None


def _post(url, payload, timeout=90, retries=2):
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": UA,
    }
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:400]
            except Exception:  # noqa: BLE001
                pass
            if e.code == 429:
                wait = _retry_after(e, detail)
                # A cooldown of minutes cannot be slept off inside a run — hand
                # it back to the caller so it can report and move to the next store.
                if wait is None or wait > 60:
                    raise RateLimited(
                        f"rate limited by store (retry after {wait or 'unknown'}s)",
                        retry_after=wait,
                    ) from e
                last = RateLimited(detail, wait)
                time.sleep(wait)
                continue
            if 500 <= e.code < 600:
                last = UCPError(f"HTTP {e.code}: {detail}")
                time.sleep(2 * (attempt + 1))
                continue
            raise UCPError(f"HTTP {e.code}: {detail}") from e
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise UCPError(f"POST {url} failed after {retries} attempts: {last}")


def _get_json(url, timeout=45):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def discover(domain):
    """Return ``(mcp_endpoint, profile_url, ucp_version)``. Raises NotUCP."""
    domain = normalize_domain(domain)
    profile_url = f"https://{domain}/.well-known/ucp"
    try:
        prof = _get_json(profile_url)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise NotUCP(f"{domain}: no UCP profile (HTTP {e.code})") from e
        raise NotUCP(f"{domain}: UCP profile unavailable (HTTP {e.code})") from e
    except Exception as e:  # noqa: BLE001
        raise NotUCP(f"{domain}: UCP profile unreachable ({type(e).__name__})") from e

    ucp = prof.get("ucp") or {}
    endpoint = None
    for svc_list in (ucp.get("services") or {}).values():
        for svc in svc_list:
            if svc.get("transport") == "mcp" and svc.get("endpoint"):
                endpoint = svc["endpoint"]
                break
        if endpoint:
            break
    if not endpoint:
        raise NotUCP(f"{domain}: UCP profile advertises no MCP endpoint")
    return endpoint, profile_url, ucp.get("version")


class UCPClient:
    def __init__(self, domain, agent_profile=None):
        self.domain = normalize_domain(domain)
        self.endpoint, self.shop_profile_url, self.version = discover(self.domain)
        self.agent_profiles = (
            [agent_profile] if agent_profile else list(DEFAULT_AGENT_PROFILES)
        )
        self.agent_profile = self.agent_profiles[0]
        self._id = 0

    def _rpc(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        out = _post(self.endpoint, msg)
        if "error" in out:
            raise UCPError(
                f"{method} failed: {json.dumps(out['error'], ensure_ascii=False)[:300]}"
            )
        return out.get("result", {})

    def initialize(self):
        return self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "shopify-radar", "version": "1.0"},
            },
        )

    def list_tools(self):
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, name, arguments):
        res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if res.get("isError"):
            text = " ".join(c.get("text", "") for c in (res.get("content") or []))
            raise UCPError(f"{name} isError: {text[:300]}")
        return res.get("structuredContent") or {}

    def _search_with_fallback(self, args):
        """Run one search page, rotating to a backup profile on profile errors.

        Stores fetch the referenced profile themselves, so a CDN that is
        unreachable *from the store's network* surfaces as a profile error even
        though the URL works fine from ours. Trying the next mirror is cheaper
        than failing the whole store.
        """
        last = None
        for prof in self.agent_profiles:
            args["meta"] = {"ucp-agent": {"profile": prof}}
            try:
                sc = self.call("search_catalog", args)
                self.agent_profile = prof
                return sc
            except UCPError as e:
                msg = str(e)
                if "agent profile" not in msg and "profile_" not in msg:
                    raise
                last = e
        raise last

    def search_catalog(self, query="", max_products=250, max_pages=50, log=None,
                       page_delay=1.0):
        """Page through the catalog. Returns a list of raw UCP product dicts.

        ``page_delay`` matters: stores throttle bursty paging. A 1s gap between
        pages proved safe across ~45 consecutive requests in testing, while
        hammering a very large store at full speed earned a ~1h cool-down.
        """
        items, cursor, seen_cursors = [], None, set()
        for page in range(max_pages):
            if page and page_delay:
                time.sleep(page_delay)
            args = {
                "meta": {"ucp-agent": {"profile": self.agent_profile}},
                "catalog": {"query": query},
            }
            if cursor:
                args["catalog"]["pagination"] = {"cursor": cursor}
            sc = self._search_with_fallback(args)
            products = sc.get("products") or []
            items.extend(products)
            if log:
                log(f"    page {page + 1}: +{len(products)} -> {len(items)}")
            pg = sc.get("pagination") or {}
            cursor = pg.get("cursor")
            if not pg.get("has_next_page") or not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            if len(items) >= max_products:
                break
        return items[:max_products]


def _amount(money):
    """UCP money is an integer in minor units (cents). Return a float."""
    if not isinstance(money, dict):
        return None
    a = money.get("amount")
    if a is None:
        return None
    try:
        return round(float(a) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def _currency(money):
    if not isinstance(money, dict):
        return None
    return money.get("currency")


def to_rows(products):
    """UCP product dicts -> flat dataset rows."""
    rows = []
    for p in products:
        variants = p.get("variants") or []
        prices = [_amount(v.get("price")) for v in variants]
        prices = [x for x in prices if x is not None]

        pr = p.get("price_range") or {}
        pmin = _amount(pr.get("min")) if pr else None
        pmax = _amount(pr.get("max")) if pr else None
        if pmin is None and prices:
            pmin, pmax = min(prices), max(prices)

        lr = p.get("list_price_range") or {}
        lmin = _amount(lr.get("min")) if lr else None
        lmax = _amount(lr.get("max")) if lr else None
        # compare-at is the "was" price; take the highest so a partially
        # discounted range still reports a sensible strike-through.
        compare_at = max([x for x in (lmin, lmax) if x] or [0]) or None

        currency = _currency(pr.get("min")) if pr else None
        if not currency:
            for v in variants:
                currency = _currency(v.get("price"))
                if currency:
                    break

        available = any(
            (v.get("availability") or {}).get("available") for v in variants
        )

        media = p.get("media") or []
        image = media[0].get("url") if media else None

        pid = (p.get("id") or "").rstrip("/").split("/")[-1] or p.get("handle")

        cols = p.get("collections") or []
        product_type = None
        if cols:
            product_type = cols[0].get("title") or cols[0].get("handle")
        elif p.get("categories"):
            product_type = (p["categories"][0] or {}).get("value")

        tags = p.get("tags")
        if isinstance(tags, list):
            tags = ",".join(t for t in tags if isinstance(t, str))

        discount_pct = None
        if compare_at and pmin and compare_at > pmin:
            discount_pct = round((compare_at - pmin) / compare_at * 100, 1)

        rows.append(
            {
                "product_id": str(pid),
                "handle": p.get("handle"),
                "title": p.get("title"),
                "product_type": product_type,
                "vendor": p.get("vendor"),
                "tags": tags,
                "price_min": pmin,
                "price_max": pmax,
                "compare_at": compare_at,
                "discount_pct": discount_pct,
                "currency": currency,
                "available": bool(available),
                "variant_count": len(variants),
                "variants": [
                    {
                        "sku": v.get("sku"),
                        "title": v.get("title"),
                        "price": _amount(v.get("price")),
                        "available": (v.get("availability") or {}).get("available"),
                    }
                    for v in variants
                ],
                "image": image,
            }
        )
    return rows
