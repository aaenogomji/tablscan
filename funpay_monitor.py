# -*- coding: utf-8 -*-
"""
FunPay stock monitor: Path of Exile offers of selected sellers.
Каждый запуск:
  1) качает страницы профилей,
  2) парсит офферы в разделах Path of Exile (наличие + цена),
  3) сравнивает с прошлым снимком (state.json),
  4) события пишет в CSV-статистику по каждому профилю:
     - полный журнал событий (funpay_stats_*.csv)
     - журнал продаж (funpay_sales_*.csv): каждая продажа = дельта стока x цена, сумма ₽
     - дневные итоги (funpay_daily_*.csv): по дням, сколько шт и на сколько ₽ продано
     - лог проверок (funpay_checks_log.csv) со стоимостью текущего стока (кол-во x цена)
  5) перевыставление (оффер исчез + в этом же скане появился новый с тем же
     кол-вом — смена лиги/цены) фиксируется в журнале со старой и новой ценой,
     но НЕ считается продажей и в выручку не попадает;
     смена цены при том же оффере пишется как событие "смена цены X -> Y",
     дальнейшие продажи считаются по новой цене.
"""
import os
import re
import csv
import json
import html as htmllib
import sys
import time
import fcntl
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import urllib.request

TZ = ZoneInfo("Europe/Moscow")
_lock_fh = None
# Пути можно переопределить переменными окружения (нужно для запуска
# в GitHub Actions: FUNPAY_DATA_DIR=funpay_data FUNPAY_STATS_DIR=funpay_stats)
DATA_DIR = Path(os.environ.get("FUNPAY_DATA_DIR",
                               "/home/z/my-project/scripts/funpay_data"))
STATS_DIR = Path(os.environ.get("FUNPAY_STATS_DIR",
                                "/home/z/my-project/download/funpay_stats"))
STATE_FILE = DATA_DIR / "state.json"
CHECKS_LOG = STATS_DIR / "funpay_checks_log.csv"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

PROFILES = {
    "2615704": {"name": "ComradeD", "url": "https://funpay.com/users/2615704/"},
    "173106": {"name": "Skullxbasher", "url": "https://funpay.com/users/173106/"},
}

H3_RE = re.compile(r'<h3><a href="https://funpay\.com/[^"]+">([^<]+)</a></h3>')
ITEM_RE = re.compile(
    r'<a href="https://funpay\.com/[^"]*offer\?id=([^"]+)" class="tc-item">(.*?)</a>', re.S)
SERVER_RE = re.compile(r'<div class="tc-server[^"]*"[^>]*>(.*?)</div>', re.S)
AMOUNT_RE = re.compile(r'<div class="tc-amount"[^>]*>([^<]*)</div>')
AMOUNT_DS_RE = re.compile(r'<div class="tc-amount"[^>]*data-s="([^"]*)"')
PRICE_DIV_RE = re.compile(r'<div class="tc-price"[^>]*>\s*<div>(.*?)</div>', re.S)
UNIT_RE = re.compile(r'class="unit">([^<]*)<')

STATS_HEADER = ["Время события", "Событие", "Раздел", "Лига/сервер", "Цена за ед.",
                "Валюта", "Сток до", "Сток после", "Изменение шт.", "Сумма, ₽",
                "Выставлен в (МСК)", "Активность, мин", "ID оффера"]
SALES_HEADER = ["Дата", "Время", "Лига/сервер", "Продано шт.", "Цена за ед., ₽",
                "Сумма, ₽", "Тип продажи", "ID оффера"]
DAILY_HEADER = ["Дата", "Продано шт.", "Выручка, ₽"]
CHECKS_HEADER = ["Время", "Профиль", "Офферов PoE", "Всего шт. в стоке",
                 "Стоимость стока, ₽", "Ошибка"]


def now_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def fetch(url, tries=3):
    last_err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                # Accept-Language НЕ отправлять: FunPay при любом его значении
                # отдаёт цены в USD, без него — в рублях (проверено эмпирически)
                "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < tries - 1:
                time.sleep(5)
    raise RuntimeError(f"fetch failed: {url}: {last_err}")


def parse_num(s):
    if s is None:
        return 0.0
    s = re.sub(r"[^\d.,]", "", s.replace("\u00a0", "").replace(" ", ""))
    if not s:
        return 0.0
    return float(s.replace(",", "."))


def parse_profile(html):
    """-> list of dicts: {id, section, server, price, currency, qty}"""
    h3s = list(H3_RE.finditer(html))
    offers = []
    for idx, m in enumerate(h3s):
        title = htmllib.unescape(m.group(1)).strip()
        if "path of exile" not in title.lower():
            continue
        chunk_end = h3s[idx + 1].start() if idx + 1 < len(h3s) else len(html)
        chunk = html[m.end():chunk_end]
        for it in ITEM_RE.finditer(chunk):
            oid, body = it.group(1), it.group(2)
            srv = SERVER_RE.search(body)
            server = htmllib.unescape(srv.group(1)).strip() if srv else ""
            server = re.sub(r"\s+", " ", server)
            amt = AMOUNT_DS_RE.search(body)
            qty = parse_num(amt.group(1)) if amt else 0.0
            if qty == 0.0:
                amt2 = AMOUNT_RE.search(body)
                qty = parse_num(amt2.group(1)) if amt2 else 0.0
            pr = PRICE_DIV_RE.search(body)
            price, cur = 0.0, ""
            if pr:
                inner = pr.group(1)
                u = UNIT_RE.search(inner)
                cur = u.group(1).strip() if u else "₽"
                price = parse_num(inner)
            offers.append({"id": oid, "section": title, "server": server,
                           "price": price, "currency": cur, "qty": qty})
    return offers


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def ensure_header(path, header):
    """Актуализирует заголовок CSV: переносит данные старых строк в новые колонки по именам."""
    if not path.exists():
        return
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f, delimiter=";"))
    if not rows or rows[0] == header:
        return
    old_header, body = rows[0], rows[1:]
    idx = {name: i for i, name in enumerate(header)}
    out = []
    for r in body:
        nr = [""] * len(header)
        for j, name in enumerate(old_header):
            if name in idx and j < len(r):
                nr[idx[name]] = r[j]
        out.append(nr)
    rewrite_csv(path, header, out)


def append_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        rewrite_csv(path, header, rows)
        return
    ensure_header(path, header)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f, delimiter=";").writerows(rows)


def rewrite_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header)
        w.writerows(rows)


def fmt_qty(x):
    return f"{x:g}"


def fmt_money(x):
    return f"{round(x + 1e-9, 2):g}"


def minutes_listed(o):
    try:
        f0 = datetime.strptime(o.get("first_seen", ""), "%Y-%m-%d %H:%M:%S")
        return str(round((datetime.now(TZ) - f0).total_seconds() / 60))
    except Exception:  # noqa: BLE001
        return ""


def record_sale(ts, date_str, o, sold_qty, old_qty, new_qty, full_sale, daily, csv_base):
    """Одна продажа: строка в stats CSV + sales CSV + дневной итог в state. Сумма = шт x цена."""
    amount = round(sold_qty * o["price"], 2)
    stype = "СТОК ПРОПАЛ (продан/снят)" if full_sale else "продажа (частичная)"
    kind = "полный выкуп или снятие" if full_sale else "частичный выкуп"
    append_csv(csv_base["stats"], STATS_HEADER, [
        [ts, stype, o.get("section", ""), o.get("server", ""), o["price"], o.get("currency", "₽"),
         fmt_qty(old_qty), fmt_qty(new_qty), f"-{fmt_qty(sold_qty)}", fmt_money(amount),
         o.get("first_seen", ""), minutes_listed(o), o["id"]]])
    append_csv(csv_base["sales"], SALES_HEADER, [
        [date_str, ts.split(" ")[1], o.get("server", ""), fmt_qty(sold_qty), o["price"],
         fmt_money(amount), kind, o["id"]]])
    day = daily.setdefault(date_str, {"qty": 0.0, "rub": 0.0})
    day["qty"] += sold_qty
    day["rub"] = round(day["rub"] + amount, 2)
    return amount


def write_daily_csv(pid, meta, daily):
    rows = [[d, fmt_qty(v["qty"]), fmt_money(v["rub"])]
            for d, v in sorted(daily.items())]
    rewrite_csv(STATS_DIR / f"funpay_daily_{pid}_{meta['name']}.csv", DAILY_HEADER, rows)


def acquire_lock():
    """Защита от параллельных запусков (крон-сессии иногда накладываются):
    второй экземпляр молча завершается, не создавая дублей в логах."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(DATA_DIR / "monitor.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Другой запуск уже выполняется — пропуск.")
        sys.exit(0)
    return fh


def main():
    global _lock_fh
    _lock_fh = acquire_lock()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    prev_check = (state.get("2615704") or {}).get("last_check", "")
    ts = now_str()
    date_str = ts.split(" ")[0]
    print(f"Проверка FunPay — {ts} (МСК)")
    failures = []

    for pid, meta in PROFILES.items():
        csv_base = {
            "stats": STATS_DIR / f"funpay_stats_{pid}_{meta['name']}.csv",
            "sales": STATS_DIR / f"funpay_sales_{pid}_{meta['name']}.csv",
        }
        try:
            html = fetch(meta["url"])
        except Exception as e:  # noqa: BLE001
            failures.append(f"{meta['name']} ({pid}): {e}")
            append_csv(CHECKS_LOG, CHECKS_HEADER,
                       [[ts, f"{meta['name']} ({pid})", "", "", "", str(e)[:120]]])
            continue

        offers = parse_profile(html)
        cur = {o["id"]: dict(o) for o in offers}
        pid_state = state.get(pid) or {}
        prev = pid_state.get("offers", {})
        daily = pid_state.get("daily", {})

        appeared, sales, price_changes = [], [], []
        for oid, o in cur.items():
            qty, price = o["qty"], o["price"]
            if oid in prev:
                p = prev[oid]
                o["first_seen"] = p["first_seen"]
                o["last_seen"] = ts
                if qty > 0 and p["qty"] > 0 and qty < p["qty"]:
                    sales.append((p["qty"] - qty, o, False))
                if qty > 0 and p["qty"] > 0 and qty > p["qty"]:
                    appeared.append((qty - p["qty"], o, True))  # добавка
                if price != p["price"] and price > 0:
                    price_changes.append((p["price"], o))
            else:
                o["first_seen"] = ts
                o["last_seen"] = ts
                if qty > 0:
                    appeared.append((qty, o, False))
        # полное исчезновение: оффер пропал со страницы либо количество стало 0
        for oid, p in prev.items():
            if oid not in cur and p["qty"] > 0:
                sales.append((p["qty"], p, True))
            elif oid in cur and cur[oid]["qty"] == 0 and p["qty"] > 0:
                sales.append((p["qty"], p, True))

        # перевыставление: оффер исчез и в этом же скане появился новый с тем же
        # кол-вом — продавец снял и перевыставил (смена лиги/цены). Это НЕ продажа:
        # в продажи и дневной итог не включаем, но фиксируем в журнале со старой
        # и новой ценой. Если это был всё же реальный выкуп + новое выставление,
        # поправить можно скриптом funpay_correct.py.
        relist_events = []
        matched_new = set()
        for gi in range(len(sales) - 1, -1, -1):
            sold_qty, g, full = sales[gi]
            if not full:
                continue
            for ni, (q, n, is_add) in enumerate(appeared):
                if is_add or ni in matched_new or q != sold_qty:
                    continue
                matched_new.add(ni)
                relist_events.append((g, n))
                sales.pop(gi)
                break
        for ni in sorted(matched_new, reverse=True):
            appeared.pop(ni)

        # продажи: сумма = продано шт x цена за ед. -> stats CSV + sales CSV + дневной итог
        sold_events = []
        for sold_qty, o, full_sale in sales:
            amount = record_sale(ts, date_str, o, sold_qty,
                                 sold_qty if full_sale else o["qty"] + sold_qty,
                                 0 if full_sale else o["qty"],
                                 full_sale, daily, csv_base)
            sold_events.append((sold_qty, o, full_sale, amount))

        # прочие события в полный журнал
        relist_rows = []
        for g, n in relist_events:
            cur_sym = n.get("currency", "₽")
            srv = (f"{g.get('server', '')} -> {n.get('server', '')}"
                   if g.get("server") != n.get("server") else n.get("server", ""))
            relist_rows.append(
                [ts, f"перевыставление стока (НЕ продажа); цена {g['price']:g} -> {n['price']:g}{cur_sym}",
                 n.get("section", ""), srv, n["price"], cur_sym,
                 fmt_qty(n["qty"]), fmt_qty(n["qty"]), "0", "",
                 g.get("first_seen", ""), minutes_listed(g), n["id"]])
        other_rows = relist_rows
        for delta, o, is_add in appeared:
            ev = "выставлен сток" if not is_add else "добавка в сток"
            other_rows.append([ts, ev, o["section"], o["server"], o["price"], o["currency"],
                               "" if not is_add else fmt_qty(o["qty"] - delta), fmt_qty(o["qty"]),
                               f"+{fmt_qty(delta)}", "", o["first_seen"], "", o["id"]])
        for old, o in price_changes:
            other_rows.append([ts, f"смена цены {old:g} -> {o['price']:g}", o["section"],
                               o["server"], o["price"], o["currency"], fmt_qty(o["qty"]),
                               fmt_qty(o["qty"]), "", "", o.get("first_seen", ""), "", o["id"]])
        if other_rows:
            append_csv(csv_base["stats"], STATS_HEADER, other_rows)

        # лог проверок: стоимость стока = сумма (кол-во x цена) по всем офферам
        stock_value = round(sum(o["qty"] * o["price"] for o in cur.values()), 2)
        total_qty = sum(o["qty"] for o in cur.values())
        append_csv(CHECKS_LOG, CHECKS_HEADER,
                   [[ts, f"{meta['name']} ({pid})", len(cur), fmt_qty(total_qty),
                     fmt_money(stock_value), ""]])

        state[pid] = {"name": meta["name"], "last_check": ts,
                      "offers": cur, "daily": daily}
        write_daily_csv(pid, meta, daily)

        # текстовый отчёт
        if not prev and not cur:
            print(f"[{meta['name']}] офферов PoE нет.")
            continue
        print(f"[{meta['name']}] офферов PoE: {len(cur)}, всего в стоке: {fmt_qty(total_qty)} шт "
              f"на {fmt_money(stock_value)} ₽")
        for delta, o, is_add in appeared:
            if is_add:
                print(f"  ДОБАВКА: {o['section']} | {o['server']} | +{fmt_qty(delta)} шт "
                      f"(теперь {fmt_qty(o['qty'])}) | {o['price']:g}{o['currency']}")
            else:
                print(f"  ВЫСТАВЛЕН СТОК: {o['section']} | {o['server']} | {fmt_qty(o['qty'])} шт "
                      f"| {o['price']:g}{o['currency']} | сток на "
                      f"{fmt_money(o['qty'] * o['price'])} ₽")
        for g, n in relist_events:
            print(f"  ПЕРЕВЫСТАВЛЕН (не продажа): {g.get('server', '')} -> {n.get('server', '')} | "
                  f"{fmt_qty(n['qty'])} шт | цена {g['price']:g} -> {n['price']:g}{n.get('currency', '₽')}")
        for sold_qty, o, full_sale, amount in sold_events:
            tag = "СТОК ПРОПАЛ" if full_sale else "ПРОДАЖА"
            print(f"  {tag}: {o.get('section', '')} | {o.get('server', '')} | "
                  f"-{fmt_qty(sold_qty)} шт x {o['price']:g}₽ = {fmt_money(amount)} ₽")
        for old, o in price_changes:
            print(f"  ЦЕНА: {o['section']} | {o['server']} | {old:g} -> {o['price']:g}{o['currency']}")
        day = daily.get(date_str)
        if day:
            print(f"  Итог за {date_str}: продано {fmt_qty(day['qty'])} шт "
                  f"на {fmt_money(day['rub'])} ₽")

    save_state(state)
    # Heartbeat дублируем прямо здесь: крон-сессии иногда обходят обёртку
    # funpay_task.sh и вызывают монитор напрямую — журнал живости не должен слепнуть.
    try:
        with open(DATA_DIR / "heartbeat.log", "a", encoding="utf-8") as hb_fh:
            hb_fh.write(f"{ts}|0|{prev_check or 'none'}\n")
    except OSError:
        pass
    if failures:
        print("ОШИБКИ ПРОВЕРКИ: " + "; ".join(failures))
        sys.exit(2)
    print("OK")


if __name__ == "__main__":
    main()
