from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
SEEN_PATH = ROOT / "seen.json"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
PRICE_RE = re.compile(r"(?<!\d)(\d{1,3}(?:[ .]\d{3})?|\d{3,5})(?!\d)\s*(?:dh|dhs|mad|درهم)", re.I)


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def parse_price(text: str) -> int | None:
    match = PRICE_RE.search(text.replace("\u202f", " ").replace("\xa0", " "))
    if not match:
        return None
    return int(re.sub(r"\D", "", match.group(1)))


def listing_id(url: str, title: str, price: int) -> str:
    stable = f"{url.split('?')[0]}|{norm(title)}|{price}"
    return hashlib.sha256(stable.encode()).hexdigest()[:20]


def accepted(title: str, text: str, price: int | None) -> bool:
    joined = norm(f"{title} {text}")
    if price is None or price > int(CONFIG["max_price_mad"]):
        return False
    if not any(norm(word) in joined for word in CONFIG["include_words"]):
        return False
    if any(norm(word) in joined for word in CONFIG["exclude_words"]):
        return False
    return any(norm(city) in joined for city in CONFIG["cities"])


def jsonld_items(soup: BeautifulSoup, base_url: str) -> list[dict]:
    found: list[dict] = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
                title = str(node.get("name") or "")
                url = str(node.get("url") or "")
                offers = node.get("offers") if isinstance(node.get("offers"), dict) else {}
                raw_price = str(offers.get("price") or node.get("price") or "")
                if title and url and raw_price:
                    digits = re.sub(r"\D", "", raw_price)
                    if digits:
                        found.append({"title": title, "url": urljoin(base_url, url), "price": int(digits), "text": json.dumps(node, ensure_ascii=False)})
    return found


def anchor_items(soup: BeautifulSoup, base_url: str) -> list[dict]:
    found: list[dict] = []
    host = urlparse(base_url).netloc
    for anchor in soup.select("a[href]"):
        block = anchor.find_parent(["article", "li", "div"]) or anchor
        text = norm(block.get_text(" ", strip=True))
        if not any(norm(word) in text for word in CONFIG["include_words"]):
            continue
        price = parse_price(text)
        url = urljoin(base_url, anchor.get("href", ""))
        if price and urlparse(url).netloc == host:
            found.append({"title": anchor.get_text(" ", strip=True) or text[:100], "url": url, "price": price, "text": text})
    return found


def fetch(url: str) -> list[dict]:
    response = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "fr-MA,ar-MA;q=0.9"}, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items = jsonld_items(soup, url)
    items.extend(anchor_items(soup, url))
    unique = {}
    for item in items:
        unique[item["url"].split("?")[0]] = item
    return list(unique.values())


def telegram(item: dict) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    message = (
        "🔥 <b>همزة PS5 جديدة</b>\n\n"
        f"🎮 {html.escape(item['title'][:180])}\n"
        f"💰 <b>{item['price']} درهم</b>\n"
        f"🔗 <a href=\"{html.escape(item['url'], quote=True)}\">فتح الإعلان</a>"
    )
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=30,
    )
    response.raise_for_status()


def main() -> int:
    seen = set(json.loads(SEEN_PATH.read_text(encoding="utf-8"))) if SEEN_PATH.exists() else set()
    new_ids: list[str] = []
    errors = []
    matches = []
    for url in CONFIG["search_urls"]:
        try:
            for item in fetch(url):
                if accepted(item["title"], item["text"], item["price"]):
                    matches.append(item)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    for item in sorted(matches, key=lambda x: x["price"]):
        item_id = listing_id(item["url"], item["title"], item["price"])
        if item_id not in seen:
            telegram(item)
            seen.add(item_id)
            new_ids.append(item_id)
    SEEN_PATH.write_text(json.dumps(sorted(seen)[-3000:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"matches={len(matches)} new={len(new_ids)} errors={len(errors)}")
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors and not matches else 0


if __name__ == "__main__":
    raise SystemExit(main())

