#!/usr/bin/env python3
"""İndirim sitelerini tarar, seçtiğin kategorilerdeki fırsatları Telegram'a gönderir.

Fırsatları fotoğrafıyla gönderir; gönderilenleri seen_deals.json'a yazar (GitHub önbelleği taşır, depoya yazılmaz).
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TR = ZoneInfo("Europe/Istanbul")

# Kaynaklar: ana sayfa ya da doğrudan RSS adresi yazabilirsin.
# Bot ana sayfadan RSS adresini kendisi bulmaya çalışır.
SITES = {
    "Taze Fırsat": "https://tazefirsat.com",
    "FırsatZ": "https://firsatz.com",
    "İndirim Bakanlığı": "https://indirimbakanligi.com",
}

# Başlıkta kelime BAŞI eşleşmesi aranır ("kulaklık" -> kulaklığı, kulaklıklar...)
CATEGORIES = {
    "📱 Elektronik": [
        "iphone", "samsung galaxy", "akıllı telefon", "telefon", "laptop",
        "notebook", "macbook", "bilgisayar", "kulaklık", "airpods", "tablet",
        "ipad", "akıllı saat", "monitör",
    ],
    "👟 Ayakkabı": ["ayakkabı", "sneaker", "krampon", "terlik", "çizme"],
    "👕 Giyim": [
        "giyim", "mont", "kaban", "tişört", "t-shirt", "sweatshirt", "pantolon",
        "kot", "jean", "elbise", "gömlek", "ceket", "hırka", "eşofman", "kazak",
    ],
    "🏠 Ev aletleri": [
        "buzdolabı", "çamaşır makinesi", "bulaşık makinesi", "kurutma makinesi",
        "robot süpürge", "süpürge", "airfryer", "air fryer", "kahve makinesi",
        "fırın", "ütü", "blender", "mikser", "klima",
    ],
}

# Başlıkta % indirim yazıyorsa ve bu orandan düşükse atlanır. 0 = kapalı.
# Başlıkta yüzde yazmıyorsa haber her zaman geçer.
MIN_DISCOUNT = 0

# Sadece bu mağazalardan gelen fırsatlar gönderilir (başlık, açıklama, bağlantı
# ve etiketlere bakılır). Tüm mağazalar için REQUIRE_STORE = False yap.
REQUIRE_STORE = True
STORES = {
    "Amazon": ["amazon"],
    "Trendyol": ["trendyol"],
    "Hepsiburada": ["hepsiburada"],
    "MediaMarkt": ["mediamarkt", "media markt"],
    "Teknosa": ["teknosa"],
    "n11": ["n11"],
}

WINDOW_HOURS = 10     # 3 saatte bir çalışır; zamanlayıcı kayarsa kaçmasın
MAX_PER_FEED = 10
MAX_TOTAL = 15        # tek turda en fazla kaç fırsat (Telegram'ı boğmasın)
MAX_SEEN = 3000
CHUNK_LIMIT = 3800
STATE_FILE = Path(__file__).with_name("seen_deals.json")
UA = "Mozilla/5.0 (compatible; FirsatBot/1.0)"


def norm(text):
    table = str.maketrans("İIıŞşĞğÜüÖöÇç", "iiissgguuoocc")
    return text.translate(table).lower()


def category_of(title):
    t = norm(title)
    for cat, words in CATEGORIES.items():
        for w in words:
            k = norm(w)
            pos = t.find(k)
            while pos != -1:
                if pos == 0 or not t[pos - 1].isalnum():
                    return cat
                pos = t.find(k, pos + 1)
    return None


def word_start_find(hay, word):
    k = norm(word)
    pos = hay.find(k)
    while pos != -1:
        if pos == 0 or not hay[pos - 1].isalnum():
            return True
        pos = hay.find(k, pos + 1)
    return False


def store_of(text):
    t = norm(text)
    for name, words in STORES.items():
        if any(word_start_find(t, w) for w in words):
            return name
    return None


def discount_ok(title):
    if MIN_DISCOUNT <= 0:
        return True
    nums = [int(n) for n in re.findall(r"%\s*(\d{1,2})", title)]
    nums += [int(n) for n in re.findall(r"(\d{1,2})\s*%", title)]
    return not nums or max(nums) >= MIN_DISCOUNT


def title_key(title):
    return "t:" + re.sub(r"[^a-z0-9]+", "", norm(title))[:80]


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    dt = None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TR)
    return dt


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read()


def looks_like_feed(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return False
    return root.tag.rsplit("}", 1)[-1].lower() in ("rss", "feed", "rdf")


def get_feed(site):
    """(feed_adresi, veri) döndürür. Ana sayfa verilirse RSS'i bulur."""
    data = fetch(site)
    if looks_like_feed(data):
        return site, data
    text = data.decode("utf-8", "replace")
    candidates = []
    for m in re.finditer(r"<link[^>]+>", text, re.I):
        tag = m.group(0)
        if re.search(r'type=["\']application/(rss|atom)\+xml', tag, re.I):
            href = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if href and "comment" not in href.group(1).lower():
                candidates.append(urllib.parse.urljoin(site, html.unescape(href.group(1))))
    base = site.rstrip("/") + "/"
    for suffix in ("feed/", "rss", "feed", "rss.xml"):
        candidates.append(urllib.parse.urljoin(base, suffix))
    for url in candidates:
        try:
            d = fetch(url)
        except Exception:
            continue
        if looks_like_feed(d):
            return url, d
    raise ValueError("RSS adresi bulunamadı")


IMG_RE = re.compile(r'<img[^>]+?(?:data-src|src)=["\']([^"\']+)', re.I)


def parse_feed(xml_bytes):
    """Her kayıt için sözlük döndürür: title, link, dt, image, hay."""
    root = ET.fromstring(xml_bytes)
    items = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        title = link = image = ""
        dates, extra = {}, []
        for ch in node:
            name = ch.tag.rsplit("}", 1)[-1]
            if name == "title":
                title = " ".join(html.unescape(ch.text or "").split())
            elif name == "link":
                link = (ch.text or "").strip() or ch.get("href", "").strip()
            elif name in ("pubDate", "published", "updated", "date"):
                dates[name] = ch.text
            elif name in ("enclosure", "content", "thumbnail"):
                url = ch.get("url", "")
                kind = (ch.get("type") or ch.get("medium") or "image").lower()
                if url and "image" in kind and not image:
                    image = url
            elif name in ("description", "encoded", "summary", "category"):
                extra.append(ch.text or "")
        body = " ".join(extra)
        if not image:
            m = IMG_RE.search(html.unescape(body))
            if m:
                image = m.group(1)
        image = html.unescape(image).strip()
        if not image.startswith("http"):
            image = ""
        raw = (dates.get("pubDate") or dates.get("published")
               or dates.get("date") or dates.get("updated"))
        if title and link:
            text = re.sub(r"<[^>]+>", " ", html.unescape(body))
            items.append({
                "title": title, "link": link, "dt": parse_date(raw),
                "image": image, "hay": f"{title} {text} {link}",
            })
    return items


def load_seen():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(seen[-MAX_SEEN:], ensure_ascii=False), encoding="utf-8")


def collect(seen):
    seen_set = set(seen)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    deals, failed = [], 0
    for source, site in SITES.items():
        try:
            feed_url, data = get_feed(site)
            items = parse_feed(data)
        except Exception as exc:
            failed += 1
            print(f"[uyarı] {source} alınamadı: {exc}", file=sys.stderr)
            continue
        picked = 0
        newest_first = sorted(
            items, key=lambda x: x["dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True
        )
        for it in newest_first:
            if picked >= MAX_PER_FEED or len(deals) >= MAX_TOTAL:
                break
            if it["dt"] is None or it["dt"] < cutoff:
                continue
            cat = category_of(it["title"])
            store = store_of(it["hay"])
            tk = title_key(it["title"])
            if not cat or not discount_ok(it["title"]):
                continue
            if REQUIRE_STORE and not store:
                continue
            if it["link"] in seen_set or tk in seen_set:
                continue
            seen_set.update((it["link"], tk))
            deals.append({**it, "cat": cat, "store": store, "source": source, "keys": (it["link"], tk)})
            picked += 1
        print(f"{source} ({feed_url}): {len(items)} kayıt tarandı, {picked} fırsat.")
    deals.sort(key=lambda d: d["dt"])  # en yeni en altta
    return deals, failed


def caption_of(d):
    shop = f"{d['store']} · " if d["store"] else ""
    text = (
        f"{html.escape(d['cat'], quote=False)}\n"
        f'<a href="{html.escape(d["link"], quote=True)}">{html.escape(d["title"], quote=False)}</a>\n'
        f"🏬 <i>{html.escape(shop + d['source'], quote=False)}</i>"
    )
    return text[:1000]


def tg_call(token, method, params):
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        urllib.request.urlopen(url, data=data, timeout=25).read()
    except urllib.error.HTTPError as exc:
        print(f"Telegram {method} hatası:", exc.read().decode("utf-8", "replace"), file=sys.stderr)
        raise


def send_telegram(token, chat_id, text):
    tg_call(token, "sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })


def send_deal(token, chat_id, d):
    cap = caption_of(d)
    if d["image"]:
        try:
            tg_call(token, "sendPhoto", {
                "chat_id": chat_id, "photo": d["image"], "caption": cap, "parse_mode": "HTML",
            })
            return
        except urllib.error.HTTPError:
            pass  # görsel alınamadıysa düz metin olarak gönder
    send_telegram(token, chat_id, cap)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    can_send = bool(token and chat_id)

    if os.environ.get("TEST") == "true":
        if not can_send:
            sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanımlı değil.")
        send_telegram(token, chat_id, "✅ Fırsat botu çalışıyor, Telegram bağlantısı tamam.")
        print("Test mesajı gönderildi.")
        return

    deals, failed = collect(load_seen())
    if failed == len(SITES):
        sys.exit("Hiçbir kaynağa ulaşılamadı.")
    if not deals:
        print("Yeni fırsat yok, mesaj gönderilmedi.")
        return
    if not can_send:
        print("[deneme modu]")
        for d in deals:
            print(d["image"] or "(görsel yok)", "\n", caption_of(d), "\n---")
        return

    sent = []
    try:
        for d in deals:
            send_deal(token, chat_id, d)
            sent.extend(d["keys"])
            time.sleep(1.2)  # Telegram hız sınırı
    finally:
        if sent:
            save_seen(load_seen() + sent)
    print(f"{len(sent) // 2} fırsat gönderildi.")


if __name__ == "__main__":
    main()
