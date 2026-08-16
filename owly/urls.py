"""URL canonicalization for seen-item tracking and hydration."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_TRACKING_KEYS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid"}
_TWITTER_HOSTS = {"twitter.com", "www.twitter.com", "mobile.twitter.com", "x.com", "www.x.com"}


def canonicalize_url(url: str) -> str:
    """Lowercase host, strip tracking params / fragments / trailing slash, map Twitter to x.com."""
    text = (url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    scheme = (parsed.scheme or "https").lower()
    if scheme not in ("http", "https"):
        return text.rstrip("/")
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc in _TWITTER_HOSTS:
        netloc = "x.com"
    path = parsed.path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]
    query = urlencode(query_pairs)
    return urlunparse((scheme, netloc, path, "", query, ""))
