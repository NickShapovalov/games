#!/usr/bin/env python3
"""Охота за сиянием: проверка формулы на прошлых сезонах (бэктест).

Для каждой ночи сезона (18:00–03:00 МСК, только тёмные часы) сравниваем:
  ПРОГНОЗ, каким он был накануне:
    - облачность на день вперёд по ECMWF, ICON, GFS (Open-Meteo Previous Runs, *_previous_day1)
    - прогноз Kp NOAA, выпущенный накануне (архив NCEI, 3-day forecast)
  ФАКТ:
    - облачность по реанализу ERA5 (Open-Meteo Historical Weather)
    - наблюдённый Kp (GFZ Potsdam)
Считаем шанс той же формулой, что и страница, и смотрим, как часто ночи с
высоким шансом действительно были ясными и активными.

Запуск: python3 .github/scripts/aurora_backtest.py aurora/backtest
"""
import json, math, os, re, sys, time, datetime as dt, urllib.request, urllib.parse, html

LAT, LON = 69.288, 32.800
UA = "aurora-hunt-backtest/1.0 github.com/NickShapovalov/games"
SEASONS = [("2024/25", "2024-09-01", "2025-03-31"), ("2025/26", "2025-09-01", "2026-03-31")]
MODELS = ["ecmwf_ifs025", "icon_seamless", "gfs_seamless"]
NCEI = "https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/daily_reports/3day_forecast/"
D2R = math.pi / 180


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # повторяем при сетевых сбоях
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


# ---------- астрономия (как на странице) ----------
def _days(ts):
    return ts / 86400 - 10957.5


def _alt(ra, dec, d):
    h = (280.16 + 360.9856235 * d + LON) * D2R - ra
    f = LAT * D2R
    return math.asin(math.sin(f) * math.sin(dec) + math.cos(f) * math.cos(dec) * math.cos(h)) / D2R


def sun_alt(ts):
    d = _days(ts)
    g = (357.529 + 0.98560028 * d) * D2R
    q = 280.459 + 0.98564736 * d
    lo = (q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) * D2R
    ep = (23.439 - 0.00000036 * d) * D2R
    ra = math.atan2(math.cos(ep) * math.sin(lo), math.cos(lo))
    dec = math.asin(math.sin(ep) * math.sin(lo))
    return _alt(ra, dec, d)


def moon(ts):
    d = _days(ts)
    L, M, F = 218.316 + 13.176396 * d, 134.963 + 13.064993 * d, 93.272 + 13.22935 * d
    lo = (L + 6.289 * math.sin(M * D2R)) * D2R
    la = 5.128 * math.sin(F * D2R) * D2R
    ep = 23.439 * D2R
    ra = math.atan2(math.sin(lo) * math.cos(ep) - math.tan(la) * math.sin(ep), math.cos(lo))
    dec = math.asin(math.sin(la) * math.cos(ep) + math.cos(la) * math.sin(ep) * math.sin(lo))
    age = ((ts - dt.datetime(2000, 1, 6, 18, 14, tzinfo=dt.timezone.utc).timestamp()) / 86400) % 29.530588853
    ill = (1 - math.cos(2 * math.pi * age / 29.530588853)) / 2
    return _alt(ra, dec, d), ill


def act(kp):
    pts = [(0, .15), (1, .35), (2, .6), (3, .8), (4, .92), (5, 1)]
    if kp >= 5:
        return 1.0
    for (a, b), (c, e) in zip(pts, pts[1:]):
        if kp <= c:
            return b + (kp - a) / (c - a) * (e - b)
    return 1.0


# ---------- загрузка данных ----------
def om(base, start, end, hourly, models=None):
    q = {"latitude": LAT, "longitude": LON, "start_date": start, "end_date": end,
         "hourly": hourly, "timezone": "GMT"}
    if models:
        q["models"] = models
    return json.loads(get(base + "?" + urllib.parse.urlencode(q)))["hourly"]


def forecast_clouds(start, end):
    """{"2025-01-01T18": {"ecmwf": 10, ...}} — прогноз, сделанный за сутки."""
    out, src = {}, None
    for base in ("https://previous-runs-api.open-meteo.com/v1/forecast",
                 "https://historical-forecast-api.open-meteo.com/v1/forecast"):
        try:
            h = om(base, start, end, "cloud_cover_previous_day1", ",".join(MODELS))
            src = base
            break
        except Exception as e:
            print("clouds forecast:", base, e)
    if src is None:
        raise RuntimeError("нет архива прогнозов облачности")
    for key, vals in h.items():
        if not key.startswith("cloud_cover"):
            continue
        name = "ecmwf" if "ecmwf" in key else "icon" if "icon" in key else "gfs" if "gfs" in key else "best"
        for t, v in zip(h["time"], vals):
            if v is not None:
                out.setdefault(t[:13], {})[name] = v
    return out, src


def actual_clouds(start, end):
    h = om("https://archive-api.open-meteo.com/v1/archive", start, end, "cloud_cover")
    return {t[:13]: v for t, v in zip(h["time"], h["cloud_cover"]) if v is not None}


def observed_kp(start, end):
    url = "https://kp.gfz.de/app/json/?" + urllib.parse.urlencode(
        {"start": start + "T00:00:00Z", "end": end + "T23:59:59Z", "index": "Kp"})
    d = json.loads(get(url))
    return {t[:13]: k for t, k in zip(d["datetime"], d["Kp"]) if k is not None}


def ncei_index():
    """Находит все файлы 3-day forecast в архиве NCEI (структура папок может быть любой)."""
    files, seen, queue = {}, set(), [NCEI]
    while queue and len(seen) < 400:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            page = get(url)
        except Exception as e:
            print("ncei:", url, e)
            continue
        for href in re.findall(r'href="([^"?#]+)"', page):
            href = html.unescape(href)
            full = urllib.parse.urljoin(url, href)
            m = re.search(r"(\d{8})(\d{4})three_day_forecast\.txt$", href)
            if m:
                files[(m.group(1), m.group(2))] = full
            elif href.endswith("/") and full.startswith(url) and full != url and re.search(r"/\d{4}(/\d{2})?/$", full):
                yr = re.search(r"/(\d{4})/", full[len(NCEI) - 1:])
                if yr and 2024 <= int(yr.group(1)) <= 2026:
                    queue.append(full)
    return files


MON = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def parse_3day(txt):
    """{"2025-01-02T21": 3.67, ...} из таблицы Kp index breakdown."""
    m = re.search(r"Kp index breakdown ([A-Z][a-z]{2}) (\d{1,2})-([A-Z][a-z]{2}) (\d{1,2}) (\d{4})", txt)
    if not m:
        return {}
    y = int(m.group(5))
    lines = txt[m.end():].splitlines()
    days = None
    out = {}
    for ln in lines:
        hdr = re.findall(r"([A-Z][a-z]{2}) (\d{1,2})", ln)
        if days is None and len(hdr) == 3:
            days = []
            for mon, dd in hdr:
                yy = y - 1 if (mon == "Dec" and m.group(3) == "Jan") else y
                days.append(dt.date(yy, MON[mon], int(dd)))
            continue
        r = re.match(r"\s*(\d{2})-\d{2}UT\s+(.*)", ln)
        if days and r:
            vals = re.findall(r"(\d+(?:\.\d+)?)", re.sub(r"\(G\d\)", "", r.group(2)))
            for day, v in zip(days, vals[:3]):
                out[f"{day.isoformat()}T{r.group(1)}"] = float(v)
        if days and out and not r and ln.strip() == "":
            break
    return out


def kp_forecasts(nights):
    """Для ночи D берём прогноз, выпущенный накануне в 12:30 UTC (запасной — 00:30)."""
    idx = ncei_index()
    print("ncei files:", len(idx))
    res, cache = {}, {}
    for D in nights:
        prev = (D - dt.timedelta(days=1)).strftime("%Y%m%d")
        for issue in ("1230", "0030"):
            url = idx.get((prev, issue))
            if not url:
                continue
            if url not in cache:
                try:
                    cache[url] = parse_3day(get(url))
                    time.sleep(0.2)
                except Exception as e:
                    print("kp fc:", url, e)
                    cache[url] = {}
            if cache[url]:
                res[D] = cache[url]
                break
    return res


# ---------- оценка ночей ----------
def night_hours(D):
    t0 = dt.datetime(D.year, D.month, D.day, 15, tzinfo=dt.timezone.utc)
    return [t0 + dt.timedelta(hours=i) for i in range(10)]


def kp_in_night(kpmap, hours):
    vals = []
    for h in hours:
        key = h.replace(hour=h.hour - h.hour % 3).strftime("%Y-%m-%dT%H")
        if key in kpmap:
            vals.append(kpmap[key])
    return max(vals) if vals else None


def evaluate(D, fc_cl, ac_cl, kp_obs, kp_fc):
    hours = [h for h in night_hours(D) if sun_alt(h.timestamp()) < -10]
    if len(hours) < 3:
        return None
    keys = [h.strftime("%Y-%m-%dT%H") for h in hours]
    fc = [sum(fc_cl[k].values()) / len(fc_cl[k]) for k in keys if fc_cl.get(k)]
    ac = [ac_cl[k] for k in keys if k in ac_cl]
    if len(fc) < len(keys) * 0.7 or len(ac) < len(keys) * 0.7:
        return None
    per = {}
    for m in ("ecmwf", "icon", "gfs"):
        v = [fc_cl[k][m] for k in keys if fc_cl.get(k, {}).get(m) is not None]
        if len(v) >= len(keys) * 0.7:
            per[m] = sum(1 - x / 100 for x in v) / len(v)
    spread = max(per.values()) - min(per.values()) if len(per) > 1 else 0
    sky_fc = sum(1 - c / 100 for c in fc) / len(fc)
    moon_up = [moon(h.timestamp()) for h in hours]
    ill = moon_up[len(moon_up) // 2][1]
    up_frac = sum(1 for a, _ in moon_up if a > 0) / len(hours)
    moon_f = 1 - .15 * ill * up_frac
    kpf = kp_in_night(kp_fc.get(D, {}), hours)
    kpo = kp_in_night(kp_obs, hours)
    if kpo is None:
        return None
    chance_fc = round(100 * sky_fc * act(kpf) * moon_f) if kpf is not None else None
    clear_hours_obs = sum(1 for c in ac if c <= 30)
    sky_obs = sum(1 - c / 100 for c in ac) / len(ac)
    good = clear_hours_obs >= 3 and kpo >= 2.67
    return {"night": D.isoformat(), "dark_h": len(hours), "chance": chance_fc,
            "chance_perfect_kp": round(100 * sky_fc * act(kpo) * moon_f),
            "sky_fc": round(sky_fc, 2), "spread": round(spread, 2), "kp_fc": kpf,
            "sky_obs": round(sky_obs, 2), "clear_h_obs": clear_hours_obs, "kp_obs": kpo,
            "moon": round(ill, 2), "moon_up": round(up_frac, 2), "good": good}


def table(rows, key):
    out = []
    for th in (30, 40, 50, 55, 60, 65, 70, 75):
        go = [r for r in rows if r[key] is not None and r[key] >= th]
        hit = sum(r["good"] for r in go)
        allgood = sum(r["good"] for r in rows if r[key] is not None)
        out.append({"threshold": th, "go_nights": len(go), "good": hit,
                    "precision": round(hit / len(go), 2) if go else None,
                    "recall": round(hit / allgood, 2) if allgood else None})
    return out


def bins(rows, key):
    out = []
    for lo in range(0, 100, 10):
        b = [r for r in rows if r[key] is not None and lo <= r[key] < lo + 10]
        if b:
            out.append({"range": f"{lo}–{lo + 9}", "nights": len(b), "good_share": round(sum(r["good"] for r in b) / len(b), 2)})
    return out


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    all_rows, notes = [], []
    for name, start, end in SEASONS:
        end_d = min(dt.date.fromisoformat(end), dt.date.today() - dt.timedelta(days=6))
        end = end_d.isoformat()
        end1 = (end_d + dt.timedelta(days=1)).isoformat()
        fc_cl, src = forecast_clouds(start, end1)
        notes.append(f"{name}: прогноз облаков из {src}")
        ac_cl = actual_clouds(start, end1)
        kp_obs = observed_kp(start, end1)
        nights = [dt.date.fromisoformat(start) + dt.timedelta(days=i)
                  for i in range((end_d - dt.date.fromisoformat(start)).days + 1)]
        kp_fc = kp_forecasts(nights)
        notes.append(f"{name}: прогнозов Kp NOAA найдено для {len(kp_fc)} из {len(nights)} ночей")
        for D in nights:
            r = evaluate(D, fc_cl, ac_cl, kp_obs, kp_fc)
            if r:
                r["season"] = name
                all_rows.append(r)
    good = sum(r["good"] for r in all_rows)
    summary = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "definition": "Хорошая ночь по факту: не меньше 3 тёмных часов с облачностью ≤30 % (ERA5) и наблюдённый Kp ≥ 2.67 (GFZ).",
        "nights": len(all_rows), "good_nights": good,
        "base_rate": round(good / len(all_rows), 2) if all_rows else None,
        "by_threshold": table(all_rows, "chance"),
        "by_threshold_if_kp_known": table(all_rows, "chance_perfect_kp"),
        "calibration": bins(all_rows, "chance"),
        "by_month": [],
        "notes": notes,
    }
    months = {}
    for r in all_rows:
        months.setdefault(r["night"][5:7], []).append(r)
    for m in sorted(months, key=lambda x: (int(x) < 9, int(x))):
        rs = months[m]
        summary["by_month"].append({"month": m, "nights": len(rs), "good_share": round(sum(x["good"] for x in rs) / len(rs), 2)})
    json.dump(summary, open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(all_rows, open(os.path.join(outdir, "nights.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "aurora/backtest")
