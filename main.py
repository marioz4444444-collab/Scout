# -*- coding: utf-8 -*-
# СКАУТ — Модуль 1. Мониторит Property Finder через актёр Apify memo23,
# ловит ТОЛЬКО новые листинги и шлёт их Юрию в личку на проверку.
# Дедуп самостоятельный (по ссылке листинга), хранится в KV-store Apify.

import os
import re
import csv
import json
import time
import html
import hashlib
import requests
from datetime import datetime, timedelta, timezone

BUILD = "SCOUT-7"

# Дубай = UTC+4 круглый год (перевода часов нет)
DUBAI = timezone(timedelta(hours=4))

# ---------- настройки по умолчанию (можно переопределить через env) ----------
ACTOR_ID       = os.environ.get("ACTOR_ID", "gAeFCToTd2UbRqBF9").strip()
MAX_ITEMS      = int(os.environ.get("MAX_ITEMS", "20"))
KV_STORE_NAME  = os.environ.get("KV_STORE_NAME", "laretz-scout-v2").strip()
MAX_CARDS      = 25  # защита от лавины, если не распознано поле ссылки

# Расписание прогонов по времени Дубая. Меняется переменной RUN_TIMES в Railway,
# например: 10:30,14:00,18:00
RUN_TIMES_RAW  = os.environ.get("RUN_TIMES", "10:30,14:00,18:00")
# Сделать прогон сразу при старте, не дожидаясь расписания: RUN_ON_START=yes
RUN_ON_START   = os.environ.get("RUN_ON_START", "no").strip().lower() in ("yes", "true", "1")


def _clean_secret(s: str) -> str:
    # выкидываем невидимые символы и пробелы из токенов/ключей (урок Ларца)
    if not s:
        return ""
    s = s.replace("\u200b", "").replace("\ufeff", "").replace("\u00a0", " ")
    return s.strip()


def clean_text(s: str) -> str:
    # чистим юникод в исходящем тексте
    if s is None:
        return ""
    s = str(s)
    for bad in ("\u2028", "\u2029", "\u200b", "\ufeff"):
        s = s.replace(bad, "")
    return s.strip()


TELEGRAM_TOKEN   = _clean_secret(os.environ.get("TELEGRAM_TOKEN", ""))
APIFY_TOKEN      = _clean_secret(os.environ.get("APIFY_TOKEN", ""))
ALLOWED_USER_ID  = _clean_secret(os.environ.get("ALLOWED_USER_ID", ""))
SHEET_ID         = _clean_secret(os.environ.get("SHEET_ID", ""))
SCOUT_FILTERS_GID = _clean_secret(os.environ.get("SCOUT_FILTERS_GID", "0"))
INBOX_WEBHOOK    = _clean_secret(os.environ.get("INBOX_WEBHOOK", ""))
BACKFILL_ONCE    = os.environ.get("BACKFILL_ONCE", "").strip()

APIFY_HEADERS = {
    "Authorization": "Bearer " + APIFY_TOKEN,
    "Content-Type": "application/json",
}

# ---------- имена полей актёра memo23 (реальные — первыми) ----------
URL_KEYS      = ["shareUrl", "url", "link", "propertyUrl", "listingUrl"]
PERMIT_KEYS   = ["reraNumber", "DLD Permit Number", "dldPermitNumber", "permit"]
PRICE_KEYS    = ["propertyPrice", "price", "Price", "priceValue"]
CURRENCY_KEYS = ["currency", "Currency"]
BED_KEYS      = ["propertyBedrooms", "bedrooms", "Bedrooms", "beds"]
SIZE_KEYS     = ["propertySizeSqft", "size", "area", "Size", "builtUpArea"]
PROJECT_KEYS  = ["ListingTitle", "listingTitle", "title", "Title", "propertyTitle"]
REF_KEYS      = ["reference", "Reference", "ref", "referenceNumber"]


# ============================ Telegram ============================
def tg_send(text: str):
    text = clean_text(text)
    if len(text) > 3800:
        text = text[:3800] + "…"
    try:
        requests.post(
            "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_TOKEN,
            json={"chat_id": ALLOWED_USER_ID, "text": text,
                  "disable_web_page_preview": True},
            timeout=30,
        )
    except Exception as e:
        print("tg_send error:", e)


# ============================ Apify: запуск актёра ============================
def apify_run(start_url: str):
    payload = {
        "startUrls": [start_url],
        "maxItems": MAX_ITEMS,
        "monitoringMode": False,
        "enrichEmails": False,
        "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
    }
    url = ("https://api.apify.com/v2/acts/%s/run-sync-get-dataset-items"
           % ACTOR_ID)
    r = requests.post(url, headers=APIFY_HEADERS, json=payload, timeout=300)
    if r.status_code not in (200, 201):
        raise RuntimeError("Apify %s: %s" % (r.status_code, r.text[:400]))
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("Apify вернул не список: %s" % str(data)[:300])
    return data


# ============================ Apify: KV-store (память) ============================
_KV_ID = None


def kv_store_id():
    global _KV_ID
    if _KV_ID:
        return _KV_ID
    r = requests.post(
        "https://api.apify.com/v2/key-value-stores",
        headers=APIFY_HEADERS,
        params={"name": KV_STORE_NAME},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError("KV-store create %s: %s" % (r.status_code, r.text[:300]))
    _KV_ID = r.json()["data"]["id"]
    return _KV_ID


def kv_get(key: str):
    url = ("https://api.apify.com/v2/key-value-stores/%s/records/%s"
           % (kv_store_id(), key))
    r = requests.get(url, headers={"Authorization": "Bearer " + APIFY_TOKEN},
                     timeout=30)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError("KV get %s: %s" % (r.status_code, r.text[:300]))
    try:
        return r.json()
    except Exception:
        return None


def kv_put(key: str, obj):
    url = ("https://api.apify.com/v2/key-value-stores/%s/records/%s"
           % (kv_store_id(), key))
    r = requests.put(url, headers=APIFY_HEADERS,
                     data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                     timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError("KV put %s: %s" % (r.status_code, r.text[:300]))


# ============================ Фильтры из Google-таблицы ============================
def read_filters():
    csv_url = ("https://docs.google.com/spreadsheets/d/%s/export?format=csv&gid=%s"
               % (SHEET_ID, SCOUT_FILTERS_GID))
    r = requests.get(csv_url, timeout=30)
    r.encoding = "utf-8"
    rows = list(csv.reader(r.text.splitlines()))
    filters = []
    negative = {"no", "нет", "false", "0", "off", "-", "disabled", "выкл"}
    for i, row in enumerate(rows):
        if i == 0:                      # первая строка — заголовок
            continue
        if len(row) < 2:
            continue
        name = clean_text(row[0])
        link = clean_text(row[1])
        active = clean_text(row[2]).lower() if len(row) > 2 else ""
        if not link.startswith("http"):
            continue
        if active in negative:
            continue
        if not name:
            name = link
        filters.append((name, link))
    return filters


# ============================ Разбор листинга ============================
def pick(item, keys):
    for k in keys:
        if k in item:
            v = item[k]
            if v is None or isinstance(v, (dict, list)):
                continue
            s = str(v).strip()
            if s:
                return s
    return ""


def listing_key(item):
    u = pick(item, URL_KEYS)
    if u:
        return u
    ref = pick(item, REF_KEYS)
    if ref:
        return "ref:" + ref
    blob = json.dumps(item, sort_keys=True, ensure_ascii=False)
    return "h:" + hashlib.md5(blob.encode("utf-8")).hexdigest()


def format_card(name, item):
    project  = pick(item, PROJECT_KEYS)
    beds     = pick(item, BED_KEYS)
    size     = pick(item, SIZE_KEYS)
    price    = pick(item, PRICE_KEYS)
    currency = pick(item, CURRENCY_KEYS)
    permit   = pick(item, PERMIT_KEYS)
    url      = pick(item, URL_KEYS)

    lines = ["🆕 Новый листинг — %s" % name, ""]
    if project:
        lines.append("🏢 %s" % project)
    bs = []
    if beds:
        bs.append("🛏 %s" % beds)
    if size:
        bs.append("📐 %s sqft" % size)
    if bs:
        lines.append("   ".join(bs))
    if price:
        lines.append("💰 %s %s" % (price, currency) if currency else "💰 %s" % price)
    if permit:
        lines.append("📋 RERA: %s" % permit)
    if url:
        lines.append("🔗 %s" % url)
    return "\n".join(lines)


def seen_key(name):
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower())
    return ("seen_" + safe)[:200]


def backfill_key(name):
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower())
    return ("backfilled_" + safe)[:200]


def should_backfill(name):
    # разовая заливка существующих листингов в scout_inbox
    if not BACKFILL_ONCE:
        return False
    targets = [t.strip().lower() for t in BACKFILL_ONCE.split(",")]
    want = ("all" in targets or "yes" in targets or
            name.strip().lower() in targets)
    if not want:
        return False
    return kv_get(backfill_key(name)) is None  # ещё не заливали


def backfill_filter(name, url):
    items = apify_run(url)
    rows = []
    keys = []
    for it in items:
        if isinstance(it, dict):
            rows.append(build_row(name, it))
            keys.append(listing_key(it))
    if rows:
        write_to_inbox(rows)
    # помечаем эти листинги виденными, чтобы process_filter их не продублировал
    stored = kv_get(seen_key(name)) or []
    kv_put(seen_key(name), list(set(stored) | set(keys)))
    # ставим метку «заливка сделана» — повтор не сработает даже при рестарте
    kv_put(backfill_key(name), {"done": True, "count": len(rows)})
    tg_send("📥 Скаут: разово залил %d существующих листингов по фильтру "
            "«%s» в таблицу. Дальше — только новое." % (len(rows), name))


def build_row(name, item):
    # строка для scout_inbox: Скаут заполняет часть колонок, остальное — пробивка
    now = (datetime.utcnow() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")
    return {
        "date":      now,
        "filter":    name,
        "title":     pick(item, PROJECT_KEYS),
        "bedrooms":  pick(item, BED_KEYS),
        "size_sqft": pick(item, SIZE_KEYS),
        "price":     pick(item, PRICE_KEYS),
        "currency":  pick(item, CURRENCY_KEYS),
        "rera":      pick(item, PERMIT_KEYS),
        "link":      pick(item, URL_KEYS),
    }


def write_to_inbox(rows):
    # пишет строки в scout_inbox. Возвращает True при успехе, False при провале.
    # делает до 3 попыток; редирект/HTML больше НЕ считается автоуспехом.
    if not INBOX_WEBHOOK or not rows:
        return True
    last_err = ""
    for attempt in range(3):
        try:
            r = requests.post(INBOX_WEBHOOK,
                              json={"action": "add", "rows": rows}, timeout=60)
            if r.status_code in (200, 201):
                try:
                    data = r.json()
                    if data.get("ok"):
                        return True
                    last_err = "script: %s" % str(data)[:200]
                except ValueError:
                    last_err = "не-JSON ответ: %s" % (r.text or "")[:120]
            else:
                last_err = "HTTP %s: %s" % (r.status_code, (r.text or "")[:120])
        except Exception as e:
            last_err = str(e)[:200]
        time.sleep(5)  # пауза перед повтором
    print("write_to_inbox FAILED:", last_err)
    return False


# ============================ Обработка одного фильтра ============================
def process_filter(name, url):
    items = apify_run(url)
    current = {}
    for it in items:
        if isinstance(it, dict):
            k = listing_key(it)
            if k:
                current[k] = it

    stored = kv_get(seen_key(name))

    # --- ПЕРВЫЙ ПРОГОН: молча пишем baseline, шлём одну сводку ---
    if stored is None:
        kv_put(seen_key(name), list(current.keys()))
        tg_send("✅ Скаут запущен. Фильтр «%s»: в базе %d листингов. "
                "Дальше шлю только новое." % (name, len(current)))
        if items and isinstance(items[0], dict):
            fields = ", ".join(list(items[0].keys())[:40])
            tg_send("🔧 Поля листинга (перешли это Клоду для настройки "
                    "карточки):\n%s" % fields)
        return

    # --- ОБЫЧНЫЙ ПРОГОН: только новые ---
    seen_set = set(stored)
    new_keys = [k for k in current if k not in seen_set]

    if not new_keys:
        return

    if len(new_keys) > MAX_CARDS:
        tg_send("⚠️ Скаут: фильтр «%s» дал %d «новых» сразу — похоже, не "
                "распознано поле ссылки. Шлю первые 5, проверь карточку."
                % (name, len(new_keys)))
        new_keys = new_keys[:5]

    for k in new_keys:
        tg_send(format_card(name, current[k]))
        time.sleep(0.5)

    # пишем новые листинги в scout_inbox
    rows = [build_row(name, current[k]) for k in new_keys]
    ok = write_to_inbox(rows)

    if ok:
        # записалось — отмечаем ВСЁ виденным (и новое, и всё текущее)
        updated = list(seen_set | set(current.keys()))
        kv_put(seen_key(name), updated)
    else:
        # запись провалилась — НЕ отмечаем новые виденными, попробуем в след. прогон.
        # но старые (что уже были виденными) оставляем виденными.
        tg_send("⚠️ Скаут: %d новых карточек ушло, но в таблицу НЕ записалось "
                "(фильтр «%s»). Повторю на следующем прогоне — листинги не "
                "потеряются." % (len(new_keys), name))
        already = set(current.keys()) - set(new_keys)  # виденные, что ещё в выдаче
        kv_put(seen_key(name), list(seen_set | already))


# ============================ Цикл ============================
def cycle():
    filters = read_filters()
    if not filters:
        tg_send("⚠️ Скаут: во вкладке scout_filters нет активных фильтров.")
        return
    for name, url in filters:
        try:
            if should_backfill(name):
                backfill_filter(name, url)
            process_filter(name, url)
        except Exception as e:
            tg_send("⚠️ Скаут: ошибка по фильтру «%s»\n%s" % (name, str(e)[:500]))


# ============================ Расписание ============================
def parse_times(raw):
    # "10:30,14:00,18:00" -> [(10,30),(14,0),(18,0)]
    out = []
    for chunk in _clean_secret(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hh, mm = chunk.split(":")
            hh, mm = int(hh), int(mm)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                out.append((hh, mm))
        except Exception:
            pass
    return sorted(set(out))


RUN_TIMES = parse_times(RUN_TIMES_RAW) or [(10, 30), (14, 0), (18, 0)]


def now_dubai():
    return datetime.now(DUBAI)


def next_run(after):
    # ближайшее время запуска строго ПОСЛЕ момента after (по Дубаю)
    for hh, mm in RUN_TIMES:
        candidate = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate > after:
            return candidate
    hh, mm = RUN_TIMES[0]          # сегодняшние прошли — берём первое завтрашнее
    tomorrow = after + timedelta(days=1)
    return tomorrow.replace(hour=hh, minute=mm, second=0, microsecond=0)


def times_str():
    return ", ".join("%02d:%02d" % (h, m) for h, m in RUN_TIMES)


def safe_cycle(tag):
    try:
        cycle()
    except Exception as e:
        tg_send("⚠️ Скаут: критическая ошибка цикла (%s)\n%s" % (tag, str(e)[:500]))


def main():
    missing = [n for n, v in [
        ("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("APIFY_TOKEN", APIFY_TOKEN),
        ("ALLOWED_USER_ID", ALLOWED_USER_ID), ("SHEET_ID", SHEET_ID),
    ] if not v]
    if missing:
        print("НЕТ переменных:", missing)
        # пытаемся хотя бы сообщить, если есть телеграм-токен
        if TELEGRAM_TOKEN and ALLOWED_USER_ID:
            tg_send("⚠️ Скаут не запущен: нет переменных %s" % ", ".join(missing))
        return

    inbox = "✅ запись в таблицу вкл" if INBOX_WEBHOOK else "⚠️ запись в таблицу ВЫКЛ (нет INBOX_WEBHOOK)"
    nxt = next_run(now_dubai())
    tg_send("🚀 Скаут запущен. BUILD = %s\n"
            "🕒 Расписание (Дубай): %s\n"
            "maxItems: %d\n%s\n"
            "Ближайший прогон: %s"
            % (BUILD, times_str(), MAX_ITEMS, inbox, nxt.strftime("%d.%m %H:%M")))

    if RUN_ON_START:
        tg_send("▶️ Прогон при старте (RUN_ON_START=yes).")
        safe_cycle("старт")
        nxt = next_run(now_dubai())
        tg_send("😴 Готово. Следующий прогон: %s" % nxt.strftime("%d.%m %H:%M"))

    while True:
        now = now_dubai()
        if now >= nxt:
            tg_send("⏰ Скаут: прогон по расписанию %s" % nxt.strftime("%H:%M"))
            safe_cycle(nxt.strftime("%H:%M"))
            nxt = next_run(now_dubai())
            tg_send("😴 Готово. Следующий прогон: %s" % nxt.strftime("%d.%m %H:%M"))
        # спим короткими отрезками — расписание не уедет и рестарт его не собьёт
        left = (nxt - now_dubai()).total_seconds()
        time.sleep(max(5, min(60, left)))


if __name__ == "__main__":
    main()
