#!/usr/bin/env python3
"""Generate a small local manifest of first-party metadata for Hood-hub covers.

The manifest stores provider URLs and identifiers, not copied copyrighted artwork.
Open Library and iTunes remain the image/audio hosts; the browser can discover the
first visible URLs immediately without waiting for a search request.
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import requests

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
OUT_JSON = ROOT / "assets" / "cover-manifest.json"
OUT_JS = ROOT / "assets" / "cover-manifest.js"
OUT_CRITICAL_JS = ROOT / "assets" / "critical-cover-manifest.js"
OUT_PRELOADS = ROOT / "assets" / "critical-cover-preloads.html"
TIMEOUT = 7


def js_unquote(value: str) -> str:
    return value.replace('\\"', '"').replace('\\\\', '\\')


def extract_records(name: str) -> list[dict[str, str]]:
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(rf"const {name}=\[(.*?)\n\s*\];", html, re.S)
    if not match:
        raise RuntimeError(f"Could not find {name} in {INDEX}")
    records = []
    for title, creator in re.findall(r'\{t:"((?:\\.|[^"])*)",a:"((?:\\.|[^"])*)"', match.group(1)):
        records.append({"title": js_unquote(title), "creator": js_unquote(creator)})
    if not records:
        raise RuntimeError(f"No records found in {name}")
    return records


def book_key(record: dict[str, str]) -> str:
    return f"{record['title']}::{record['creator']}".lower()


def music_key(record: dict[str, str]) -> str:
    return f"{record['title']}::{record['creator']}".lower()


def fetch_book(record: dict[str, str]) -> tuple[str, dict]:
    params = {
        "q": f"{record['title']} {record['creator']}".strip(),
        "limit": 1,
        "fields": "cover_i",
    }
    try:
        response = requests.get("https://openlibrary.org/search.json", params=params, timeout=TIMEOUT)
        response.raise_for_status()
        docs = response.json().get("docs", [])
        cover_id = docs[0].get("cover_i") if docs else None
        if cover_id:
            return book_key(record), {
                "small": f"https://covers.openlibrary.org/b/id/{cover_id}-S.jpg",
                "medium": f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg",
                "provider": "Open Library",
                "providerUrl": "https://openlibrary.org/",
            }
    except requests.RequestException as exc:
        print(f"book lookup failed for {record['title']}: {exc}", file=sys.stderr)
    return book_key(record), {}


def fetch_music(record: dict[str, str]) -> tuple[str, dict]:
    term = f"{record['title']} {record['creator'] if record['creator'] != '—' else ''}".strip()
    params = {
        "term": term,
        "media": "music",
        "entity": "song",
        "limit": 10,
        "country": "IN",
    }
    try:
        response = requests.get("https://itunes.apple.com/search", params=params, timeout=TIMEOUT)
        response.raise_for_status()
        results = response.json().get("results", [])
        match = next((item for item in results if item.get("artworkUrl100")), None)
        if match:
            artwork = match.get("artworkUrl100", "")
            return music_key(record), {
                "small": artwork,
                "large": artwork.replace("/100x100bb", "/600x600bb"),
                "previewUrl": match.get("previewUrl", "") if str(match.get("previewUrl", "")).startswith("https://") else "",
                "trackName": match.get("trackName", ""),
                "artistName": match.get("artistName", ""),
                "collectionName": match.get("collectionName", ""),
                "trackViewUrl": match.get("trackViewUrl", ""),
                "provider": "iTunes Search",
                "providerUrl": "https://www.apple.com/itunes/",
            }
    except requests.RequestException as exc:
        print(f"music lookup failed for {term}: {exc}", file=sys.stderr)
    return music_key(record), {}


def update_index_preloads(tags: str) -> None:
    html = INDEX.read_text(encoding="utf-8")
    block = "<!-- VIVUHUB_CRITICAL_PRELOADS_START -->\n" + tags.rstrip() + "\n<!-- VIVUHUB_CRITICAL_PRELOADS_END -->"
    pattern = re.compile(r"<!-- VIVUHUB_CRITICAL_PRELOADS_START -->.*?<!-- VIVUHUB_CRITICAL_PRELOADS_END -->", re.S)
    if pattern.search(html):
        html = pattern.sub(block, html, count=1)
    else:
        html = html.replace("</head>", block + "\n</head>", 1)
    INDEX.write_text(html, encoding="utf-8")


def main() -> int:
    books = extract_records("booksData")
    music = extract_records("musicData")
    manifest = {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "books": {},
        "music": {},
    }
    with ThreadPoolExecutor(max_workers=8) as pool:
        book_futures = [pool.submit(fetch_book, record) for record in books]
        music_futures = [pool.submit(fetch_music, record) for record in music]
        for future in book_futures:
            key, value = future.result()
            if value:
                manifest["books"][key] = value
        for future in music_futures:
            key, value = future.result()
            if value:
                manifest["music"][key] = value

    OUT_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OUT_JS.write_text(
        "window.VIVUHUB_COVER_MANIFEST_FULL="
        + json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    critical = {
        "version": manifest["version"],
        "generatedAt": manifest["generatedAt"],
        "books": dict(list(manifest["books"].items())[:4]),
        "music": dict(list(manifest["music"].items())[:4]),
    }
    OUT_CRITICAL_JS.write_text(
        "window.VIVUHUB_COVER_MANIFEST="
        + json.dumps(critical, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    preload_urls = []
    for item in list(critical["books"].values())[:2]:
        preload_urls.append((item.get("small", ""), "92px"))
    for item in list(critical["music"].values())[:2]:
        preload_urls.append((item.get("small", ""), "82px"))
    preload_tags = []
    for url, size in preload_urls:
        if not url:
            continue
        preload_tags.append(f'<link rel="preload" as="image" href="{url}" imagesizes="{size}" fetchpriority="high">')
    preload_text = "\n".join(preload_tags)
    OUT_PRELOADS.write_text(preload_text + "\n", encoding="utf-8")
    update_index_preloads(preload_text)
    print(json.dumps({
        "books_in_catalog": len(books),
        "book_covers_resolved": len(manifest["books"]),
        "music_in_catalog": len(music),
        "music_artwork_resolved": len(manifest["music"]),
        "music_previews_resolved": sum(1 for item in manifest["music"].values() if item.get("previewUrl")),
        "json": str(OUT_JSON),
        "javascript": str(OUT_JS),
        "critical_javascript": str(OUT_CRITICAL_JS),
        "preloads": str(OUT_PRELOADS),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
