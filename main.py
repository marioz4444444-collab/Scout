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

BUILD = "SCOUT-1"

# ---------- настройки по умолчанию (можно переопределить через env) ----------
ACTOR_ID       = os.environ.get("ACTOR_ID", "gAeFCToTd2UbRqBF9").strip()
MAX_ITEMS      = int(os.environ.get("MAX_ITEMS", "20"))
INTERVAL_HOURS = float(os.environ.get("INTERVAL_HOURS", "3"))
KV_STORE_NAME  = os.environ.get("KV_STORE_NAME", "laretz-scout-seen").strip()
MAX_CARDS      = 25  # защита от лавины, если не распознано поле ссылки


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

APIFY_HEADERS = {
    "Authorization": "Bearer " + APIFY_TOKEN,
    "Content-Type": "application/json",
}

# ---------- варианты имён полей (актёр может называть их по-разному) ----------
URL_KEYS     = ["url", "link", "propertyUrl", "detailUrl", "listingUrl",
                "href", "Link", "URL", "property_url"]
PERMIT_KEYS  = ["DLD Permit Number", "dldPermitNumber", "DLD Permit",
                "permit", "permitNumber", "dld_permit_number", "permitNo"]
PRICE_KEYS   = ["price", "Price", "priceValue", "amount", "salePrice", "price_value"]
BED_KEYS     = ["bedrooms", "Bedrooms", "beds", "bedroom", "bed"]
SIZE_KEYS    = ["size", "area", "Size", "Area", "builtUpArea", "sizeMin",
                "area_sqft", "size_sqft"]
PROJECT_KEYS = ["project", "Project", "building", "Building", "title", "Title",
                "name", "propertyTitle", "tower", "development"]
AGENCY_KEYS  = ["agency", "Agency name", "agencyName", "Agency",
                "agency_name", "brokerage"]
REF_KEYS     = ["reference", "Reference", "ref", "referenceNumber"]


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
    project = pick(item, PROJECT_KEYS)
    beds    = pick(item, BED_KEYS)
    size    = pick(item, SIZE_KEYS)
    price   = pick(item, PRICE_KEYS)
    permit  = pick(item, PERMIT_KEYS)
    agency  = pick(item, AGENCY_KEYS)
    url     = pick(item, URL_KEYS)

    lines = ["🆕 Новый листинг — %s" % name, ""]
    if project:
        lines.append("🏢 %s" % project)
    bs = []
    if beds:
        bs.append("🛏 %s" % beds)
    if size:
        bs.append("📐 %s" % size)
    if bs:
        lines.append("   ".join(bs))
    if price:
        lines.append("💰 %s" % price)
    if permit:
        lines.append("📋 DLD: %s" % permit)
    if agency:
        lines.append("🏬 %s" % agency)
    if url:
        lines.append("🔗 %s" % url)
    return "\n".join(lines)


def seen_key(name):
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower())
    return ("seen_" + safe)[:200]


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

    updated = list(seen_set | set(current.keys()))
    kv_put(seen_key(name), updated)


# ============================ Цикл ============================
def cycle():
    filters = read_filters()
    if not filters:
        tg_send("⚠️ Скаут: во вкладке scout_filters нет активных фильтров.")
        return
    for name, url in filters:
        try:
            process_filter(name, url)
        except Exception as e:
            tg_send("⚠️ Скаут: ошибка по фильтру «%s»\n%s" % (name, str(e)[:500]))


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

    tg_send("🚀 Скаут запущен. BUILD = %s. Интервал: %g ч, maxItems: %d."
            % (BUILD, INTERVAL_HOURS, MAX_ITEMS))

    while True:
        try:
            cycle()
        except Exception as e:
            tg_send("⚠️ Скаут: критическая ошибка цикла\n%s" % str(e)[:500])
        time.sleep(INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    main()
