#!/usr/bin/env python3
"""Охота за сиянием: собирает прогноз для aurora/data.json.

Источники:
  - NOAA SWPC: прогноз Kp по 3-часовым окнам
  - MET Norway: почасовые облачность, температура и ветер над Ура-Губой
  - Travelpayouts (Aviasales Data API): самые дешёвые найденные билеты LED ⇄ MMK
    Нужен секрет репозитория TRAVELPAYOUTS_TOKEN. Без него цены просто не обновятся.

Запуск: python3 .github/scripts/aurora_update.py aurora/data.json
"""
import json, os, sys, re, time, html, datetime as dt, urllib.request, urllib.parse

LAT, LON = 69.288, 32.800
UA = "aurora-hunt/1.0 github.com/NickShapovalov/games"
MSK = dt.timezone(dt.timedelta(hours=3))


def get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def kp_forecast():
    """Kp по 3-часовым окнам UT: [["2026-11-26T00", 3.0], ...]."""
    raw = json.loads(get("https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"))
    rows = []
    if raw and isinstance(raw[0], list):  # старый формат: первая строка — заголовки
        head = raw[0]
        raw = [dict(zip(head, r)) for r in raw[1:]]
    for r in raw:
        t = str(r.get("time_tag", ""))[:13].replace(" ", "T")
        try:
            rows.append([t, round(float(r.get("kp")), 2)])
        except (TypeError, ValueError):
            pass
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    return [r for r in rows if r[0][:10] >= today]


def kp_text_meta():
    """Время выпуска и пояснение из текстового прогноза на 3 дня."""
    txt = get("https://services.swpc.noaa.gov/text/3-day-forecast.txt")
    issued, note = None, ""
    m = re.search(r":Issued:\s*(\d{4}) (\w{3}) (\d{2}) (\d{2})(\d{2}) UTC", txt)
    if m:
        y, mon, d, hh, mm = m.groups()
        t = dt.datetime.strptime(f"{y} {mon} {d} {hh}{mm}", "%Y %b %d %H%M")
        issued = t.strftime("%Y-%m-%dT%H:%M:00Z")
    m = re.search(r"Rationale:\s*(.+?)(?:\n\s*\n|$)", txt, re.S)
    if m:
        note = " ".join(m.group(1).split())
    return issued, note


def weather():
    """Почасовые [время UTC, температура, облачность %, ветер м/с]."""
    url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={LAT}&lon={LON}"
    data = json.loads(get(url))
    out = []
    for ts in data["properties"]["timeseries"]:
        d = ts["data"]["instant"]["details"]
        out.append([ts["time"][:13], round(d["air_temperature"], 1),
                    round(d["cloud_area_fraction"]), round(d["wind_speed"], 1)])
    return out[:72]


def cloud_models():
    """Облачность по трём моделям (ECMWF, ICON, GFS) через Open-Meteo: {"2026-11-27T18": {"ecmwf": 0, ...}}."""
    url = ("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON, "hourly": "cloud_cover",
        "models": "ecmwf_ifs025,icon_seamless,gfs_seamless", "timezone": "GMT", "forecast_days": 4}))
    h = json.loads(get(url))["hourly"]
    out = {}
    for key, vals in h.items():
        if not key.startswith("cloud_cover"):
            continue
        name = "ecmwf" if "ecmwf" in key else "icon" if "icon" in key else "gfs" if "gfs" in key else "best"
        for t, v in zip(h["time"], vals):
            if v is not None:
                out.setdefault(t[:13], {})[name] = round(v)
    if not out:
        raise ValueError("нет данных облачности")
    return out


AIRLINES = {"5N": "Smartavia", "SU": "Аэрофлот", "FV": "Россия", "DP": "Победа", "A4": "Азимут",
            "WZ": "Red Wings", "UT": "ЮТэйр", "N4": "Nordwind", "S7": "S7", "YC": "Ямал"}


def rasp_schedule(from_id, to_id, date, key):
    """Прямые рейсы на дату через официальное API Яндекс Расписаний."""
    url = "https://api.rasp.yandex-net.ru/v3.0/search/?" + urllib.parse.urlencode({
        "apikey": key, "format": "json", "from": from_id, "to": to_id, "lang": "ru_RU",
        "date": date, "transport_types": "plane", "limit": 100})
    data = json.loads(get(url))
    found = {}
    for seg in data.get("segments", []):
        th = seg.get("thread") or {}
        num = (th.get("number") or "").replace("\u00a0", " ").strip()
        code = num.split(" ")[0] if num else ""
        if code not in AIRLINES or seg.get("has_transfers"):
            continue
        dep, arr = seg.get("departure") or "", seg.get("arrival") or ""
        found[num] = {"dep": dep[11:16], "arr": arr[11:16],
                      "airline": AIRLINES.get(code) or (th.get("carrier") or {}).get("title", code)}
    out = []
    for f, t in sorted(found.items(), key=lambda x: x[1]["dep"]):
        code, n = f.split(" ", 1)
        if code == "SU" and f"FV {n}" in found:  # код-шеринг: тот же самолёт
            continue
        out.append({"flight": f, **t})
    return out


def schedules(days):
    key = os.environ.get("YANDEX_RASP_KEY")
    if not key:
        return None
    res = {}
    for d in days:
        back = (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()
        res[d] = {"out": rasp_schedule("c2", "c23", d, key), "back": rasp_schedule("c23", "c2", back, key)}
        time.sleep(1)
    return res


def tp(token, **params):
    q = urllib.parse.urlencode({"currency": "rub", "sorting": "price", "limit": 3, "market": "ru", "direct": "true", **params})
    res = json.loads(get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates?" + q,
                         {"X-Access-Token": token}))
    return res.get("data") or []


def offer(o):
    link = o.get("link") or ""
    return {
        "price": o.get("price"),
        "flight": f'{o.get("airline", "")} {o.get("flight_number", "")}'.strip(),
        "dep": o.get("departure_at"),
        "ret": o.get("return_at"),
        "transfers": o.get("transfers"),
        "rtransfers": o.get("return_transfers"),
        "link": ("https://www.aviasales.ru" + link) if link.startswith("/") else None,
    }


def tickets(days):
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        return None
    res = {}
    for d in days:
        back = (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()
        entry = {}
        rt = tp(token, origin="LED", destination="MMK", departure_at=d, return_at=back, one_way="false", limit=30)
        if rt:
            entry["rt"] = offer(rt[0])
            best = {}
            for o in rt:  # самый дешёвый вариант у каждого перевозчика
                a = o.get("airline")
                if a and (a not in best or o.get("price", 1e9) < best[a].get("price", 1e9)):
                    best[a] = o
            entry["rt_options"] = [dict(offer(o), airline=a) for a, o in sorted(best.items(), key=lambda x: x[1].get("price", 1e9))]
        out = tp(token, origin="LED", destination="MMK", departure_at=d, one_way="true")
        if out:
            entry["out"] = offer(out[0])
        bk = tp(token, origin="MMK", destination="LED", departure_at=back, one_way="true")
        if bk:
            entry["back"] = offer(bk[0])
        res[d] = entry
    res["checked"] = dt.datetime.now(MSK).strftime("%Y-%m-%d %H:%M")
    res["note"] = ("Только прямые рейсы: самые дешёвые билеты, которые пользователи Авиасейлса "
                   "находили за последние 2–7 дней. При покупке цена может отличаться.")
    return res


def main(path):
    try:
        old = json.load(open(path, encoding="utf-8"))
    except Exception:
        old = {}
    data = dict(old)
    errors = []
    for name, fn in (("kp", kp_forecast), ("hourly", weather), ("clouds", cloud_models)):
        try:
            data[name] = fn()
        except Exception as e:  # оставляем прошлые данные, но отмечаем ошибку
            errors.append(f"{name}: {e}")
    src = dict(old.get("sources", {}))
    try:
        issued, note = kp_text_meta()
        if issued:
            src["kpIssued"] = issued
        if note:
            src["kpNote"] = "NOAA SWPC: " + note
    except Exception as e:
        errors.append(f"kp-text: {e}")
    src["weather"] = "MET Norway (yr.no) и модели ECMWF, ICON, GFS (Open-Meteo), прогноз для Ура-Губы"
    data["sources"] = src
    today = dt.datetime.now(MSK).date()
    days = [(today + dt.timedelta(days=i)).isoformat() for i in (1, 2, 3)]
    try:
        sch = schedules(days)
        if sch is not None:
            data["schedule"] = sch
    except Exception as e:
        errors.append(f"schedule: {e}")
    try:
        t = tickets(days)
        if t is not None:
            data["tickets"] = t
    except Exception as e:
        errors.append(f"tickets: {e}")
    data["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")
    data["errors"] = errors
    data.setdefault("place", {"name": "Ура-Губа", "lat": LAT, "lon": LON, "fromAirportKm": 55, "fromAirportMin": 60})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("ok" if not errors else "partial: " + "; ".join(errors))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "aurora/data.json")
