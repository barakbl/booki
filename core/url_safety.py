"""
core.url_safety — central allowlist for URL schemes Booki will persist
and surface as live links.

Why an allowlist:
  - `javascript:` and `data:` URLs become clickable XSS sinks the moment
    they reach an HTML export or the web UI's `<a href>` rendering. A
    hostile (or compromised) source plugin can produce them just by
    yielding an Item with such a `url`. The attribute escaper escapes
    `&<>"'` but does NOT validate the scheme.
  - `vbscript:` is the IE-era equivalent; modern browsers ignore it but
    we still don't want it persisted into archived exports.
  - `_fetch_page_title` issues `requests.get(url)` against user-supplied
    URLs from `/api/link`. Without a scheme guard, that's an SSRF lever
    against `http://localhost`, internal services, AWS IMDS, etc.

What we allow:
  - `http`, `https`             — the bulk of bookmarks.
  - `file`                      — items from `[[sources.directory.dirs]]`.
                                  Containment of the actual file path is
                                  enforced separately by `core.local_files`.

Anything else is a "dead" URL: it should be persisted only through paths
that explicitly opt in, and never followed by `requests.get`.
"""

from __future__ import annotations


# Schemes the ingest pipeline + web UI treat as "live" links.
SAFE_URL_SCHEMES: tuple[str, ...] = ("http", "https", "file")

# Schemes we follow with `requests.get` (title fetcher, dead-link probe).
# `file` URLs are never followed — they go through the local-file proxy.
FETCHABLE_URL_SCHEMES: tuple[str, ...] = ("http", "https")


def _scheme(url: str) -> str:
    s = (url or "").strip().lower()
    if not s:
        return ""
    # Don't use urlparse — it accepts garbage like `javascript://example.com`
    # by treating `example.com` as netloc. We only need the prefix.
    idx = s.find(":")
    return s[:idx] if idx > 0 else ""


def is_safe_url(url: str) -> bool:
    """True if this URL is one Booki will store and render as a live link.

    Used at write time (sync, manual link API) so non-allowlisted schemes
    never become clickable bookmarks. Existing items with bad schemes
    already on disk are not retroactively rewritten — but new writes
    from any source go through this gate.
    """
    return _scheme(url) in SAFE_URL_SCHEMES


def is_fetchable_url(url: str) -> bool:
    """True if Booki may issue an outbound HTTP request against this URL.

    Stricter than `is_safe_url`: even `file://` (which we *display* via
    the proxy) is not fetched directly. Used by `_fetch_page_title` to
    block the SSRF lever opened by accepting arbitrary schemes through
    `/api/link`.
    """
    return _scheme(url) in FETCHABLE_URL_SCHEMES
