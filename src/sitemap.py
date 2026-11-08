"""Read product ``lastmod`` timestamps from a store's sitemap.

UCP deliberately does not expose a publish/creation date, so the sitemap is the
only cheap signal for "the merchant touched this product recently". It is a
hint, not a guarantee: ``lastmod`` moves on any edit (description tweak, image
swap, price change), so treat it as "recently modified", never as "new product".
"""
import re
import urllib.request

UA = "Mozilla/5.0 (compatible; shopify-radar/1.0)"


def _get(url, timeout=30, opener=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if opener is not None:
        with opener.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def fetch_lastmod(domain, max_submaps=5, timeout=30, opener=None):
    """Return ``{handle: lastmod}`` for products listed in the sitemap index.

    Never raises: stores without a sitemap, or with an unusual one, simply get
    an empty mapping.
    """
    out = {}
    try:
        idx = _get(f"https://{domain}/sitemap.xml", timeout=timeout, opener=opener)
    except Exception:  # noqa: BLE001
        return out

    subs = [
        s.replace("&amp;", "&")
        for s in re.findall(r"<loc>([^<]+)</loc>", idx)
        if "sitemap_products" in s
    ]
    for sm in subs[:max_submaps]:
        try:
            xml = _get(sm, timeout=timeout, opener=opener)
        except Exception:  # noqa: BLE001
            continue
        for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
            loc = re.search(r"<loc>([^<]+)</loc>", block)
            if not loc:
                continue
            handle = loc.group(1).rstrip("/").split("/")[-1].split("?")[0]
            mod = re.search(r"<lastmod>([^<]+)</lastmod>", block)
            if mod:
                out[handle] = mod.group(1)
    return out
