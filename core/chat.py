#!/usr/bin/env python3
"""
chat.py — Search your bookmarks with natural language.

Usage:
    booki chat "what are my AI tools?"
    booki chat "find RAG resources" --no-llm
    booki chat "machine learning" --n 8
    booki chat "devtools" --min-importance 5
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger("booki.chat")

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

import chromadb
from chromadb.utils import embedding_functions


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


# ─── Retrieval ────────────────────────────────────────────────────────────────

def get_embedding_fn(em_cfg: dict):
    provider = em_cfg.get("provider", "local")
    if provider == "local":
        return embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=em_cfg.get("local_model", "all-MiniLM-L6-v2")
        )
    if provider == "openai":
        import os
        return embedding_functions.OpenAIEmbeddingFunction(
            api_key=em_cfg.get("openai_api_key") or os.getenv("OPENAI_API_KEY", ""),
            model_name=em_cfg.get("openai_model", "text-embedding-3-small"),
        )
    sys.exit(f"Unknown embeddings.provider: {provider}")


def search(query: str, cfg: dict, n: int, min_importance: int) -> list[dict]:
    db_cfg = cfg["vector_db"]
    client = chromadb.PersistentClient(path=str(Path(db_cfg["persist_dir"])))

    try:
        collection = client.get_collection(
            db_cfg.get("collection", "bookmarks"),
            embedding_function=get_embedding_fn(cfg["embeddings"]),
        )
    except Exception:
        sys.exit("Collection not found — run:  booki ingest")

    where = {"importance": {"$gte": min_importance}} if min_importance > 0 else None
    results = collection.query(
        query_texts=[query],
        n_results=n,
        where=where,
    )

    bookmarks = []
    for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
        bookmarks.append({**meta, "_score": round(1 - dist, 3)})
    return bookmarks


# ─── LLM providers ────────────────────────────────────────────────────────────

def call_ollama(prompt: str, model: str, base_url: str) -> str:
    import requests
    r = requests.post(
        f"{base_url.rstrip('/')}/api/chat",
        json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["message"]["content"]


def call_claude(prompt: str, model: str) -> str:
    import os
    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    msg = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def call_openai(prompt: str, model: str) -> str:
    import os
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
    )
    return resp.choices[0].message.content


def ask_llm(prompt: str, llm_cfg: dict) -> str:
    provider = llm_cfg.get("provider", "ollama")
    model    = llm_cfg.get("model", "llama3.2")
    t0 = time.monotonic()
    try:
        if provider == "ollama":
            answer = call_ollama(prompt, model, llm_cfg.get("base_url", "http://localhost:11434"))
        elif provider == "claude":
            answer = call_claude(prompt, model)
        elif provider == "openai":
            answer = call_openai(prompt, model)
        else:
            sys.exit(f"Unknown llm.provider '{provider}' — use 'ollama', 'claude', or 'openai'")
    except Exception:
        log.exception("llm_call_failed", extra={"provider": provider, "model": model,
                                                "prompt_chars": len(prompt)})
        raise
    log.info("llm_call", extra={
        "provider": provider, "model": model,
        "prompt_chars": len(prompt), "answer_chars": len(answer),
        "duration_s": round(time.monotonic() - t0, 3),
    })
    return answer


# ─── Prompt ───────────────────────────────────────────────────────────────────

def build_prompt(query: str, bookmarks: list[dict]) -> str:
    lines = []
    for i, bm in enumerate(bookmarks, 1):
        kind = bm.get("kind", "bookmark")
        header = f"{i}. [{kind}] {bm.get('title', '—')}  (★{bm.get('importance', 0)})"
        parts = [header, f"   URL: {bm.get('url', '')}"]
        if channel := bm.get("channel", ""):
            parts.append(f"   Channel: {channel}")
        flags = [k for k in ("liked", "watched", "subscribed", "subscribed_to_channel") if bm.get(k)]
        if flags:
            parts.append(f"   Flags: {', '.join(flags)}")
        if tags := bm.get("tags", ""):
            parts.append(f"   Tags: {tags}")
        if summary := bm.get("summary", ""):
            parts.append(f"   Summary: {summary}")
        if notes := bm.get("notes", ""):
            parts.append(f"   Notes: {notes}")
        lines.append("\n".join(parts))

    bm_block = "\n\n".join(lines)
    return (
        f'You are a helpful assistant for navigating a personal library of '
        f'bookmarks, videos, channels, and other indexed items.\n\n'
        f'User query: "{query}"\n\n'
        f'Relevant items retrieved:\n\n{bm_block}\n\n'
        f'Answer concisely based on these items. '
        f"If none are relevant, say so. Don't invent URLs or titles."
    )


# ─── Display ──────────────────────────────────────────────────────────────────

def print_results(bookmarks: list[dict]) -> None:
    if not bookmarks:
        print("No items found.")
        return
    print(f"\nFound {len(bookmarks)} item(s):\n")
    for i, bm in enumerate(bookmarks, 1):
        status = bm.get("status", "")
        status_tag = f"  [{status}]" if status not in ("alive", "unchecked", "") else ""
        kind = bm.get("kind", "bookmark")
        kind_tag = f"[{kind}] " if kind and kind != "bookmark" else ""
        print(f"  {i}. [score:{bm['_score']:.2f}] [★{bm.get('importance',0)}] "
              f"{kind_tag}{bm.get('title','—')}{status_tag}")
        print(f"        {bm.get('url','')}")
        # Source-specific context line — shown only when populated.
        extras = []
        if channel := bm.get("channel", ""):
            extras.append(f"Channel: {channel}")
        if duration := bm.get("duration", ""):
            extras.append(f"Dur: {duration}")
        flags = []
        if bm.get("liked"):                 flags.append("liked")
        if bm.get("watched"):               flags.append("watched")
        if bm.get("subscribed_to_channel"): flags.append("sub-ch")
        if bm.get("subscribed"):            flags.append("subscribed")
        if flags:
            extras.append("Flags: " + ", ".join(flags))
        if extras:
            print("        " + "  ·  ".join(extras))
        if tags := bm.get("tags", ""):
            print(f"        Tags: {tags}")
        if notes := bm.get("notes", ""):
            print(f"        Notes: {notes}")
    print()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search bookmarks with natural language.",
    )
    parser.add_argument("query", help='Search query, e.g. "AI tools"')
    parser.add_argument("--no-llm", action="store_true",
                        help="Show matching bookmarks only, skip the LLM answer")
    parser.add_argument("--n", type=int, default=None,
                        help="Number of results (overrides config llm.n_results)")
    parser.add_argument("--min-importance", type=int, default=0,
                        help="Filter results below this importance score")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)

    n = args.n or int(cfg.get("llm", {}).get("n_results", 5))

    bookmarks = search(args.query, cfg, n, args.min_importance)
    print_results(bookmarks)

    if args.no_llm:
        return

    llm_cfg = cfg.get("llm", {})
    provider = llm_cfg.get("provider", "ollama")
    model    = llm_cfg.get("model", "?")
    print(f"─── {provider} / {model} " + "─" * 30)

    try:
        answer = ask_llm(build_prompt(args.query, bookmarks), llm_cfg)
        print(f"\n{answer}\n")
    except Exception as e:
        sys.exit(f"\nLLM call failed: {e}")


if __name__ == "__main__":
    main()
