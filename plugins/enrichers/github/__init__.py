"""
plugins.enrichers.github — tag and enrich GitHub repository links.

Detects URLs of the form `github.com/<owner>/<repo>` (excluding non-repo
paths like /features, /pricing, /search), hits the GitHub REST API, and
writes back stars / forks / languages / topics / top contributors / license
/ default branch / archived / fork / last-pushed.

Also adds `"github"` to the item's `sources` list so any item — regardless
of which source originally produced it — is searchable as a github repo.

Config (all optional):

    [enrichers.github]
    # Falls back to GITHUB_TOKEN env var. Without one you get the
    # 60 req/h public rate limit — fine for small libraries, painful
    # past ~20 repos. With one you get 5,000 req/h.
    token              = ""
    max_contributors   = 5      # how many top contributors to record
    timeout            = 10     # seconds, per HTTP request
    cooldown_days      = 7      # skip items enriched within the last N days

Run with:
    booki sync --enrich-meta            # default: skip recently-enriched items
    booki sync --enrich-meta --all      # re-enrich everything
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime
from typing import Optional

import requests

from ...base import Enricher, register_enricher

log = logging.getLogger("booki.enrichers.github")


# `github.com/<owner>/<repo>` — repo root only. We deliberately reject deeper
# paths (issues, files, releases, wikis) since those don't map to one repo
# cleanly and the user's bookmark of a sub-page is still about the same repo.
GH_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/?#]+)/([^/?#]+?)(?:\.git)?/?(?:[?#]|$)",
    re.IGNORECASE,
)
GH_API = "https://api.github.com"

# Reserved github.com paths that look like /<owner>/<repo> but aren't repos.
RESERVED_TOPLEVEL = {
    "about", "features", "pricing", "marketplace", "search", "settings",
    "login", "join", "logout", "explore", "topics", "trending", "collections",
    "events", "sponsors", "security", "enterprise", "team", "customer-stories",
    "readme", "site", "contact", "site-map", "apps", "personal", "business",
    "premium-support", "status", "404", "organizations", "new", "notifications",
}


def _today_iso() -> str:
    return date.today().isoformat()


def _days_since_iso(iso: str) -> Optional[int]:
    if not iso:
        return None
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (date.today() - d).days


@register_enricher
class GitHubEnricher(Enricher):
    name = "github"

    # Allow `--all` to override the cooldown without rewriting `is_applicable`.
    # The sync engine sets this on the instance before calling enrich/is_applicable.
    force_all: bool = False

    def configure(self, cfg: dict) -> None:
        super().configure(cfg)
        self.token = (cfg.get("token") or os.environ.get("GITHUB_TOKEN") or "").strip()
        self.max_contributors = int(cfg.get("max_contributors", 5) or 5)
        self.timeout = int(cfg.get("timeout", 10) or 10)
        self.cooldown_days = int(cfg.get("cooldown_days", 7) or 7)

    # — gating —

    def is_applicable(self, fm: dict) -> bool:
        url = str(fm.get("url", "") or "").strip()
        if not url:
            return False
        m = GH_URL_RE.match(url)
        if not m:
            return False
        owner = m.group(1).lower()
        if owner in RESERVED_TOPLEVEL:
            return False
        if self.force_all:
            return True
        # Cooldown: skip items we touched recently.
        last = str(fm.get("github_last_enriched", "") or "")
        days = _days_since_iso(last)
        if days is not None and days < self.cooldown_days:
            return False
        return True

    # — work —

    def enrich(self, fm: dict) -> Optional[dict]:
        url = str(fm.get("url", "") or "").strip()
        m = GH_URL_RE.match(url)
        if not m:
            return None
        owner, repo = m.group(1), m.group(2)

        try:
            repo_data = self._get(f"/repos/{owner}/{repo}")
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 404:
                # Repo is gone — record the fact so we don't keep retrying.
                log.info("github_repo_gone", extra={"url": url, "owner": owner, "repo": repo})
                return {
                    "github_status":        "gone",
                    "github_last_enriched": _today_iso(),
                }
            log.warning("github_http_error",
                        extra={"url": url, "status": status, "error": str(e)})
            return None
        except (requests.RequestException, ValueError) as e:
            log.warning("github_fetch_failed", extra={"url": url, "error": str(e)})
            return None

        if not repo_data:
            return None

        # Languages and contributors are best-effort — failures are logged
        # but don't sink the whole enrichment.
        languages = self._safe_get(f"/repos/{owner}/{repo}/languages") or {}
        contributors = self._safe_get(
            f"/repos/{owner}/{repo}/contributors",
            params={"per_page": str(self.max_contributors), "anon": "false"},
        ) or []

        # Tag the item as a github repo via the multi-source `sources` list.
        existing_sources = [str(s) for s in (fm.get("sources") or []) if str(s).strip()]
        if "github" not in existing_sources:
            existing_sources.append("github")

        license_obj = repo_data.get("license") or {}
        owner_obj = repo_data.get("owner") or {}

        return {
            # tagging
            "sources": existing_sources,

            # core metadata
            "github_owner":        str(owner_obj.get("login") or owner),
            "github_owner_type":   str(owner_obj.get("type") or ""),       # "User" | "Organization"
            "github_repo":         str(repo_data.get("name") or repo),
            "github_full_name":    str(repo_data.get("full_name") or f"{owner}/{repo}"),
            "github_description":  str(repo_data.get("description") or ""),
            "github_homepage":     str(repo_data.get("homepage") or ""),
            "github_default_branch": str(repo_data.get("default_branch") or ""),
            "github_license":      str(license_obj.get("spdx_id") or license_obj.get("name") or ""),

            # signals
            "github_stars":        int(repo_data.get("stargazers_count") or 0),
            "github_forks":        int(repo_data.get("forks_count") or 0),
            "github_watchers":     int(repo_data.get("subscribers_count") or 0),
            "github_open_issues":  int(repo_data.get("open_issues_count") or 0),
            "github_size_kb":      int(repo_data.get("size") or 0),

            # status flags
            "github_archived":     bool(repo_data.get("archived")),
            "github_fork":         bool(repo_data.get("fork")),
            "github_disabled":     bool(repo_data.get("disabled")),
            "github_private":      bool(repo_data.get("private")),

            # listy fields
            "github_topics":       [str(t) for t in (repo_data.get("topics") or [])],
            "github_languages":    list(languages.keys()),
            "github_top_contributors": [
                str(c.get("login") or "") for c in contributors
                if isinstance(c, dict) and c.get("login")
            ][: self.max_contributors],

            # timestamps
            "github_pushed_at":    str(repo_data.get("pushed_at") or "")[:10],
            "github_created_at":   str(repo_data.get("created_at") or "")[:10],
            "github_status":       "ok",
            "github_last_enriched": _today_iso(),
        }

    # — http —

    def _get(self, path: str, *, params: Optional[dict] = None) -> Optional[dict]:
        headers = {
            "Accept":      "application/vnd.github+json",
            "User-Agent":  "booki-github-enricher",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        r = requests.get(GH_API + path, headers=headers, params=params, timeout=self.timeout)
        # Surface rate-limit hits with enough info to debug.
        if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
            reset = r.headers.get("X-RateLimit-Reset", "")
            log.error("github_rate_limited",
                      extra={"path": path, "reset_unix": reset,
                             "authenticated": bool(self.token)})
            r.raise_for_status()
        r.raise_for_status()
        return r.json() if r.content else None

    def _safe_get(self, path: str, *, params: Optional[dict] = None):
        """Like _get but returns None on any error — used for accessory endpoints
        where partial enrichment is better than no enrichment."""
        try:
            return self._get(path, params=params)
        except (requests.RequestException, ValueError) as e:
            log.debug("github_aux_fetch_failed", extra={"path": path, "error": str(e)})
            return None

    @classmethod
    def field_specs(cls) -> list[dict]:
        g = "GitHub"
        return [
            {"name": "github_full_name",        "label": "Repo",            "group": g, "format": "text"},
            {"name": "github_description",      "label": "Description",     "group": g, "format": "text"},
            {"name": "github_owner",            "label": "Owner",           "group": g, "format": "text"},
            {"name": "github_owner_type",       "label": "Owner type",      "group": g, "format": "text"},
            {"name": "github_homepage",         "label": "Homepage",        "group": g, "format": "url"},
            {"name": "github_stars",            "label": "Stars",           "group": g, "format": "number"},
            {"name": "github_forks",            "label": "Forks",           "group": g, "format": "number"},
            {"name": "github_watchers",         "label": "Watchers",        "group": g, "format": "number"},
            {"name": "github_open_issues",      "label": "Open issues",     "group": g, "format": "number"},
            {"name": "github_size_kb",          "label": "Size (KB)",       "group": g, "format": "number"},
            {"name": "github_languages",        "label": "Languages",       "group": g, "format": "list"},
            {"name": "github_topics",           "label": "Topics",          "group": g, "format": "tags"},
            {"name": "github_top_contributors", "label": "Top contributors","group": g, "format": "list"},
            {"name": "github_license",          "label": "License",         "group": g, "format": "text"},
            {"name": "github_default_branch",   "label": "Default branch",  "group": g, "format": "text"},
            {"name": "github_archived",         "label": "Archived",        "group": g, "format": "bool"},
            {"name": "github_fork",             "label": "Fork",            "group": g, "format": "bool"},
            {"name": "github_disabled",         "label": "Disabled",        "group": g, "format": "bool"},
            {"name": "github_private",          "label": "Private",         "group": g, "format": "bool"},
            {"name": "github_pushed_at",        "label": "Last push",       "group": g, "format": "date"},
            {"name": "github_created_at",       "label": "Created",         "group": g, "format": "date"},
            {"name": "github_status",           "label": "Status",          "group": g, "format": "text"},
            {"name": "github_last_enriched",    "label": "Enriched on",     "group": g, "format": "date"},
        ]
