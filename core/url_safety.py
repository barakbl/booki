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

import ipaddress
import socket
from urllib.parse import urlparse


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

    Note: this only checks the scheme. Use `is_externally_fetchable_url`
    when an arbitrary user-supplied URL is about to be dereferenced — it
    additionally rejects loopback / private-network / link-local / IMDS
    targets so the request can't be turned into an SSRF probe.
    """
    return _scheme(url) in FETCHABLE_URL_SCHEMES


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    """True if this IP address must NOT be the target of an outbound fetch.

    Covers: loopback (127/8, ::1), private networks (RFC1918 / fc00::/7),
    link-local (169.254/16, fe80::/10 — includes AWS/GCP IMDS at
    169.254.169.254), multicast, unspecified (0.0.0.0, ::), and
    reserved/site-local. Public-routable IPs pass.

    The set is intentionally broad: any of these targets crossed from
    user-supplied input would be an SSRF lever (LAN scans, IMDS creds,
    internal admin panels). Users who genuinely want to bookmark an
    intranet page do so by hand-editing the .md, not via /api/link.
    """
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def is_externally_fetchable_url(url: str) -> bool:
    """True if Booki may dereference this URL as an *external* HTTP request.

    Stricter than `is_fetchable_url`: also resolves the host and refuses
    addresses that point back at this machine, the LAN, or the cloud
    metadata service. Returns False on any DNS / parse failure (fail
    closed — a name we can't resolve isn't a name we'll fetch).

    Callers must additionally re-check the resolved IP after each
    redirect; this function only validates the URL it was handed.
    """
    if not is_fetchable_url(url):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").strip()
    if not host:
        return False
    # Literal IP — skip DNS, just classify it.
    try:
        ip = ipaddress.ip_address(host)
        return not _is_blocked_ip(ip)
    except ValueError:
        pass
    # Hostname — resolve every A/AAAA. If *any* of them lands on a
    # blocked range, refuse: a multi-A record like
    # `evil.example. A 1.2.3.4 A 127.0.0.1` would otherwise let an
    # attacker who controls DNS punch through to loopback on retry.
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for fam, _t, _p, _c, sockaddr in infos:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])  # strip IPv6 zone id
        except ValueError:
            return False
        if _is_blocked_ip(ip):
            return False
    return True
