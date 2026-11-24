#!/usr/bin/env python3
"""
Bluesky collector: search public posts by keyword and save them to JSONL.

Output (one JSON object per line):
    {"id": <post uri>, "created_at": ..., "lang": ..., "query": ..., "author_hash": ..., "text": ...}
The classifier can read this directly:  --input posts.jsonl --id-col id --text-col text

Usage:
    python bluesky_collector.py --keywords keywords.txt --since 2026-11-01 --until 2026-11-23 \
        --lang en --max-per-query 500 --output /data/posts.jsonl

Auth is OPTIONAL. If Bluesky returns 403 for anonymous search, set:
    BSKY_HANDLE=yourbot.bsky.social   BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
(use a dedicated account and an APP PASSWORD, never your main password)
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

PUBLIC = "https://public.api.bsky.app/xrpc"
AUTHED = "https://bsky.social/xrpc"
UA = "research-collector/0.1 (contact: set-your-email@example.org)"


class Bsky:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = os.environ.get("BSKY_USER_AGENT", UA)
        self.base = PUBLIC

    def login(self):
        h, p = os.environ.get("BSKY_HANDLE"), os.environ.get("BSKY_APP_PASSWORD")
        if not (h and p):
            sys.exit("Got 403 from the public API. Set BSKY_HANDLE and BSKY_APP_PASSWORD to authenticate.")
        r = self.s.post(f"{AUTHED}/com.atproto.server.createSession",
                        json={"identifier": h, "password": p}, timeout=30)
        r.raise_for_status()
        self.s.headers["Authorization"] = "Bearer " + r.json()["accessJwt"]
        self.base = AUTHED
        print("Authenticated.", file=sys.stderr)

    def get(self, method, params, _tried_login=False):
        for attempt in range(6):
            try:
                r = self.s.get(f"{self.base}/{method}", params=params, timeout=30)
            except requests.RequestException as e:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 403 and not _tried_login and self.base == PUBLIC:
                self.login()
                return self.get(method, params, _tried_login=True)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 2 ** (attempt + 2)))
                print(f"Rate limited; sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("Too many retries")


def hash_author(did: str, salt: str) -> str:
    return hashlib.sha256((salt + did).encode()).hexdigest()[:16]


def load_seen(path: Path) -> set:
    seen = set()
    if path.exists():
        for line in path.open(encoding="utf-8"):
            try:
                seen.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                pass
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keywords", default=str(Path(__file__).with_name("keywords.txt")),
                    help="Text file, one search query per line (default: keywords.txt next to this script)")
    ap.add_argument("--output", default="/data/posts.jsonl")
    ap.add_argument("--since", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--until", help="YYYY-MM-DD (exclusive)")
    ap.add_argument("--lang", help="e.g. en")
    ap.add_argument("--max-per-query", type=int, default=500)
    ap.add_argument("--sort", choices=["latest", "top"], default="latest")
    ap.add_argument("--keep-handles", action="store_true", help="Store the author handle (off by default)")
    ap.add_argument("--sleep", type=float, default=0.5, help="Seconds between requests")
    args = ap.parse_args()

    salt = os.environ.get("HASH_SALT", "")
    if not salt and not args.keep_handles:
        print("Warning: HASH_SALT not set; author hashes can be reversed by guessing. "
              "Set HASH_SALT to a random secret.", file=sys.stderr)

    if not Path(args.keywords).is_file():
        sys.exit(f"Keywords file not found: {args.keywords}\n"
                 "Mount your own with: -v $(pwd)/keywords.txt:/app/keywords.txt")
    queries = [l.strip() for l in Path(args.keywords).read_text(encoding="utf-8").splitlines()
               if l.strip() and not l.startswith("#")]
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = load_seen(out)  # resume-safe: never write the same post twice
    api = Bsky()
    total_new = 0

    with out.open("a", encoding="utf-8") as f:
        for q in queries:
            got, cursor = 0, None
            while got < args.max_per_query:
                params = {"q": q, "limit": min(100, args.max_per_query - got), "sort": args.sort}
                if args.since: params["since"] = args.since + "T00:00:00Z"
                if args.until: params["until"] = args.until + "T00:00:00Z"
                if args.lang: params["lang"] = args.lang
                if cursor: params["cursor"] = cursor
                data = api.get("app.bsky.feed.searchPosts", params)
                posts = data.get("posts", [])
                if not posts:
                    break
                for p in posts:
                    got += 1
                    uri = p.get("uri")
                    rec = p.get("record", {})
                    text = (rec.get("text") or "").strip()
                    if not uri or not text or uri in seen:
                        continue
                    seen.add(uri)
                    author = p.get("author", {})
                    row = {
                        "id": uri,
                        "created_at": rec.get("createdAt"),
                        "lang": (rec.get("langs") or [None])[0],
                        "query": q,
                        "author_hash": hash_author(author.get("did", ""), salt),
                        "text": text,
                    }
                    if args.keep_handles:
                        row["author_handle"] = author.get("handle")
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    total_new += 1
                f.flush()
                cursor = data.get("cursor")
                if not cursor:
                    break
                time.sleep(args.sleep)
            print(f"[{q}] fetched {got} results", file=sys.stderr)
    print(f"Done. {total_new} new posts written to {out} ({len(seen)} total).")


if __name__ == "__main__":
    print (os.name)
    main()
