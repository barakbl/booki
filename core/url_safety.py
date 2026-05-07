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

import contextlib
import ipaddress
import socket
import threading
from typing import Iterator, Optional
from urllib.parse import urljoin, urlparse


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


def resolve_safe_ip(host: str) -> Optional[str]:
    """Resolve `host` and return one routable IP, or None.

    Returns the first address in the getaddrinfo result that is NOT in a
    blocked range. If *any* address is in a blocked range, we return None
    (fail-closed). Used by `pinned_dns` so the request that follows
    cannot land on a different IP than the one we validated.

    (P2-01 — defeats DNS rebinding by ensuring the connect() goes to the
    same IP getaddrinfo returned at validation time.)
    """
    try:
        ip = ipaddress.ip_address(host)
        return None if _is_blocked_ip(ip) else str(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    chosen: Optional[str] = None
    for fam, _t, _p, _c, sockaddr in infos:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            return None
        if _is_blocked_ip(ip):
            return None
        if chosen is None:
            chosen = str(ip)
    return chosen


_DNS_PIN_LOCK = threading.Lock()


@contextlib.contextmanager
def pinned_dns(host: str, ip: str) -> Iterator[None]:
    """Pin DNS resolution of `host` to `ip` for the duration of the block.

    Why: `is_externally_fetchable_url(url)` resolves the host once and
    classifies the IP, but `requests.get(url, …)` immediately re-resolves.
    A DNS server controlled by an attacker can return a public IP to the
    first lookup (passes the gate) and a loopback IP to the second
    (the actual fetch). This context manager monkey-patches
    `socket.getaddrinfo` so any resolution of `host` returns only the
    pre-validated `ip` — the second lookup can't land elsewhere.

    Thread-safe: serialises through `_DNS_PIN_LOCK` because monkey-patching
    `socket.getaddrinfo` is process-global and FastAPI runs sync handlers
    on a thread pool. The patched window is short (one HTTP request).
    """
    target_host = host.lower()
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET

    with _DNS_PIN_LOCK:
        original = socket.getaddrinfo

        def patched(host_, *args, **kwargs):
            if isinstance(host_, str) and host_.lower() == target_host:
                # Match getaddrinfo's return shape: a list of 5-tuples
                # (family, type, proto, canonname, sockaddr).
                port = args[0] if args else kwargs.get("port") or 0
                if isinstance(port, str):
                    try:
                        port = int(port)
                    except ValueError:
                        port = 0
                if fam == socket.AF_INET6:
                    sockaddr = (ip, port, 0, 0)
                else:
                    sockaddr = (ip, port)
                return [(fam, socket.SOCK_STREAM, 0, "", sockaddr)]
            return original(host_, *args, **kwargs)

        socket.getaddrinfo = patched
        try:
            yield
        finally:
            socket.getaddrinfo = original


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


# ─── Outbound GET ──────────────────────────────────────────────────────────

# Process-wide cap on concurrent outbound fetches initiated by booki — across
# /api/link, the favicon proxy, and the offline-archive exporter. Without
# this, a flood of concurrent requests multiplies into a matching number of
# socket + buffer holders. (P2-05)
_FETCH_CONCURRENCY = threading.Semaphore(4)
_MAX_FETCH_REDIRECTS = 5


def safe_get(url: str, *, timeout: int, headers: dict,
             max_bytes: int):
    """Outbound GET with a re-validated SSRF gate at every redirect hop.

    Returns the final `requests.Response` (with `.content` capped at
    `max_bytes` and the connection already closed) or None on any guard
    failure. We intentionally do NOT use `allow_redirects=True`: urllib3's
    follow re-runs DNS a second time and can land on a blocked IP without us
    noticing. Manual redirects let us re-run `is_externally_fetchable_url`
    on every hop, and the IP is pinned across the validation/connect pair so
    a DNS-rebinding race is impossible. (P2-01)
    """
    import requests

    if not _FETCH_CONCURRENCY.acquire(timeout=timeout):
        return None
    try:
        current = url
        for _ in range(_MAX_FETCH_REDIRECTS + 1):
            if not is_externally_fetchable_url(current):
                return None
            host = (urlparse(current).hostname or "").lower()
            ip = resolve_safe_ip(host) if host else None
            if ip is None:
                return None
            try:
                with pinned_dns(host, ip):
                    r = requests.get(current, timeout=timeout,
                                     allow_redirects=False, stream=True,
                                     headers=headers)
            except requests.RequestException:
                return None
            if r.is_redirect or r.is_permanent_redirect:
                loc = r.headers.get("Location") or ""
                r.close()
                if not loc:
                    return None
                current = urljoin(current, loc)
                continue
            try:
                chunks: list[bytes] = []
                total = 0
                for chunk in r.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break
                r._content = b"".join(chunks)[:max_bytes]
            except requests.RequestException:
                r.close()
                return None
            finally:
                r.close()
            return r
        return None
    finally:
        _FETCH_CONCURRENCY.release()
