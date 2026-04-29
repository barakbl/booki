#!/usr/bin/env python3
"""
ingest.py — Index bookmark Markdown files into a vector database.

One file = one bookmark. Frontmatter is the source of truth.
Safe to re-run — upserts by URL hash, so no duplicates.

Usage:
    booki ingest                 Index (or update) all bookmarks
    booki ingest --reset         Wipe collection and re-index from scratch
    booki ingest --config other.toml
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path

log = logging.getLogger("booki.ingest")

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        sys.exit("Install tomli: pip install tomli  (or upgrade to Python 3.11+)")

import chromadb
from chromadb.utils import embedding_functions


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
BATCH_SIZE = 100


# ─── YAML parsing (matches sync.py) ───────────────────────────────────────────

def _parse_yaml_block(block: str) -> dict:
    result: dict = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()

        if raw.startswith("[") and raw.endswith("]"):
            try:
                val = json.loads(raw)
                result[key] = val if isinstance(val, list) else [val]
            except json.JSONDecodeError:
                inner = raw[1:-1].strip()
                result[key] = (
                    [i.strip().strip("\"'") for i in inner.split(",") if i.strip()]
                    if inner else []
                )
            continue

        if raw.lower() == "true":
            result[key] = True; continue
        if raw.lower() == "false":
            result[key] = False; continue

        if raw.lstrip("-").isdigit():
            result[key] = int(raw); continue

        if raw.startswith('"') and raw.endswith('"'):
            try:
                result[key] = json.loads(raw); continue
            except json.JSONDecodeError:
                result[key] = raw[1:-1]; continue

        if raw.startswith("'") and raw.endswith("'"):
            result[key] = raw[1:-1]; continue

        result[key] = raw
    return result


# ─── Bookmark file → document ─────────────────────────────────────────────────

def parse_bookmark_file(path: Path) -> dict | None:
    """Return frontmatter dict, or None if the file lacks a frontmatter block."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = FRONTMATTER_RE.match(content)
    if not m:
        return None
    return _parse_yaml_block(m.group(1))


def build_document(fm: dict) -> str:
    """
    The text that gets embedded.

    Core signals (present on every item):
      - title        (user's label)
      - browser_path (user's own organization)
      - tags, notes  (user's own annotations)
      - keywords     (LLM-extracted searchable terms)
      - summary      (LLM-extracted description)

    Source-specific signals (added when present):
      - channel      (YouTube)
      - description  (YouTube — first N chars of video/channel description)
      - youtube_tags (YouTube's own tags)

    URL itself is NOT embedded — it's mostly noise (query strings, hashes).
    The domain / channel is surfaced via the structured fields above instead.
    """
    parts = [f"Title: {fm.get('title', '').strip()}"]

    if kind := str(fm.get("kind", "")).strip():
        if kind and kind != "bookmark":
            parts.append(f"Kind: {kind}")

    if bp := str(fm.get("browser_path", "")).strip():
        parts.append(f"Path: {bp}")

    if channel := str(fm.get("channel", "")).strip():
        parts.append(f"Channel: {channel}")

    tags = fm.get("tags", []) or []
    if tags:
        parts.append(f"Tags: {', '.join(str(t) for t in tags)}")

    yt_tags = fm.get("youtube_tags", []) or []
    if yt_tags:
        parts.append(f"YouTube tags: {', '.join(str(t) for t in yt_tags)}")

    keywords = fm.get("keywords", []) or []
    if keywords:
        parts.append(f"Keywords: {', '.join(str(k) for k in keywords)}")

    if notes := str(fm.get("notes", "")).strip():
        parts.append(f"Notes: {notes}")

    if summary := str(fm.get("summary", "")).strip():
        parts.append(f"Summary: {summary}")

    if desc := str(fm.get("description", "")).strip():
        parts.append(f"Description: {desc}")

    return "\n".join(parts)


def bm_id(fm: dict) -> str:
    url = str(fm.get("url", "")).rstrip("/").lower()
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def bm_metadata(fm: dict) -> dict:
    """ChromaDB metadata — scalars only (str/int/float/bool)."""
    return {
        "url":          str(fm.get("url", "")),
        "title":        str(fm.get("title", "")),
        "kind":         str(fm.get("kind", "bookmark")),
        "importance":   int(fm.get("importance", 0)),
        "tags":         ", ".join(str(t) for t in (fm.get("tags", []) or [])),
        "keywords":     ", ".join(str(k) for k in (fm.get("keywords", []) or [])),
        "notes":        str(fm.get("notes", "")),
        "summary":      str(fm.get("summary", "")),
        "status":       str(fm.get("status", "unchecked")),
        "source":       str(fm.get("source", "")),
        "sources":      ", ".join(str(s) for s in (fm.get("sources", []) or [])),
        "browser_path": str(fm.get("browser_path", "")),
        "folder_path":  str(fm.get("folder_path", "")),
        "archive_url":  str(fm.get("archive_url", "")),
        "enriched":     bool(fm.get("last_enriched")),
        # YouTube / source-specific scalars — harmless for other kinds
        # (default to "" / 0 / False), but let users filter by channel,
        # watched-state, etc. from chat.py once the corpus is mixed.
        "channel":               str(fm.get("channel", "")),
        "channel_id":            str(fm.get("channel_id", "")),
        "video_id":              str(fm.get("video_id", "")),
        "duration":              str(fm.get("duration", "")),
        "published_at":          str(fm.get("published_at", "")),
        "view_count":            int(fm.get("view_count", 0) or 0),
        "liked":                 bool(fm.get("liked", False)),
        "watched":               bool(fm.get("watched", False)),
        "subscribed":            bool(fm.get("subscribed", False)),
        "subscribed_to_channel": bool(fm.get("subscribed_to_channel", False)),
    }


# ─── Loading ──────────────────────────────────────────────────────────────────

def load_all_bookmarks(bookmarks_dir: Path, min_importance: int) -> list[dict]:
    # Dedupe by URL — keep the copy with the highest importance (rare, but e.g.
    # same URL in multiple browsers or folders).
    seen: dict[str, dict] = {}
    for md_file in sorted(bookmarks_dir.rglob("*.md")):
        fm = parse_bookmark_file(md_file)
        if not fm:
            continue
        if fm.get("removed_from_browser") or fm.get("removed_from_source"):
            continue
        if not fm.get("url"):
            continue
        if int(fm.get("importance", 0)) < min_importance:
            continue
        key = str(fm.get("url", "")).rstrip("/").lower()
        if key not in seen or int(fm.get("importance", 0)) > int(seen[key].get("importance", 0)):
            seen[key] = fm
    return list(seen.values())


# ─── Embeddings ───────────────────────────────────────────────────────────────

def get_embedding_fn(cfg: dict):
    provider = cfg.get("provider", "local")

    if provider == "local":
        model = cfg.get("local_model", "all-MiniLM-L6-v2")
        print(f"  Embeddings : local  ({model})")
        return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model)

    if provider == "openai":
        import os
        model = cfg.get("openai_model", "text-embedding-3-small")
        api_key = cfg.get("openai_api_key") or os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            sys.exit("openai_api_key missing — set it in config.toml or OPENAI_API_KEY env var")
        print(f"  Embeddings : openai ({model})")
        return embedding_functions.OpenAIEmbeddingFunction(api_key=api_key, model_name=model)

    sys.exit(f"Unknown embeddings.provider '{provider}' — use 'local' or 'openai'")


# ─── Main ─────────────────────────────────────────────────────────────────────

def ingest(cfg: dict, reset: bool = False) -> None:
    bk_cfg = cfg["bookmarks"]
    db_cfg = cfg["vector_db"]
    em_cfg = cfg["embeddings"]

    bookmarks_dir   = Path(bk_cfg["dir"])
    min_importance  = int(bk_cfg.get("min_importance", 0))
    persist_dir     = Path(db_cfg["persist_dir"])
    collection_name = db_cfg.get("collection", "bookmarks")

    print(f"Bookmarks dir : {bookmarks_dir}")
    print(f"Vector DB     : {persist_dir}")
    print(f"Min importance: {min_importance}")

    log.info("ingest_started", extra={
        "bookmarks_dir":  str(bookmarks_dir),
        "persist_dir":    str(persist_dir),
        "collection":     collection_name,
        "min_importance": min_importance,
        "reset":          bool(reset),
    })
    t0 = time.monotonic()

    bookmarks = load_all_bookmarks(bookmarks_dir, min_importance)
    enriched_count = sum(1 for b in bookmarks if b.get("last_enriched"))
    print(f"  Loaded {len(bookmarks)} bookmark(s) — {enriched_count} enriched")
    if not bookmarks:
        print("Nothing to index. Run `booki sync` first.")
        log.info("ingest_finished", extra={
            "loaded": 0, "indexed": 0, "enriched": 0, "reset": bool(reset),
            "duration_s": round(time.monotonic() - t0, 3),
        })
        return

    ef = get_embedding_fn(em_cfg)

    client = chromadb.PersistentClient(path=str(persist_dir))

    if reset:
        try:
            client.delete_collection(collection_name)
            print(f"  Reset: deleted collection '{collection_name}'")
        except Exception:
            pass

    collection = client.get_or_create_collection(
        name=collection_name,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )

    ids       = [bm_id(b)          for b in bookmarks]
    documents = [build_document(b) for b in bookmarks]
    metadatas = [bm_metadata(b)    for b in bookmarks]

    print(f"\nIndexing into '{collection_name}'...")
    for i in range(0, len(ids), BATCH_SIZE):
        collection.upsert(
            ids=ids[i:i+BATCH_SIZE],
            documents=documents[i:i+BATCH_SIZE],
            metadatas=metadatas[i:i+BATCH_SIZE],
        )
        done = min(i + BATCH_SIZE, len(ids))
        print(f"  [{done}/{len(ids)}]")

    final_count = collection.count()
    print(f"\nDone — {final_count} document(s) in '{collection_name}'.")
    log.info("ingest_finished", extra={
        "loaded":         len(bookmarks),
        "indexed":        len(ids),
        "enriched":       enriched_count,
        "collection_size": final_count,
        "reset":          bool(reset),
        "duration_s":    round(time.monotonic() - t0, 3),
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Index bookmarks into ChromaDB.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"Config file (default: {DEFAULT_CONFIG})")
    parser.add_argument("--reset", action="store_true",
                        help="Delete the collection and re-index from scratch.")
    args = parser.parse_args()

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)

    ingest(cfg, reset=args.reset)


if __name__ == "__main__":
    main()
