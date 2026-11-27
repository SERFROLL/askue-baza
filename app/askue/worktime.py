"""Энергия в рабочее и нерабочее время.

Рабочее время — рабочие дни производственного календаря РФ (calendar_ru в config.yaml) с work_time[0] до work_time[1]
(08:00–19:00). Нерабочее — всё остальное: вечер и ночь 19:00–08:00, выходные и праздники.

Счёт по показаниям счётчиков энергии (readings, раз в минуту): каждый прирост показания между соседними записями
относится к рабочему или нерабочему времени по середине этого промежутка. Поэтому рабочее + нерабочее = общий расход
по показаниям, а погрешность на каждой границе 08:00/19:00 — не больше прироста за одну минуту.
Состав объектов во времени (history) учитывается через object_terms.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from .report import _fmt, _head, kwh_rub, tariff_note

SQL = """
WITH r AS (
  SELECT point, ts, e_import - lag(e_import) OVER w AS de, ts - lag(ts) OVER w AS gap
  FROM readings WHERE ts >= %(a)s::timestamptz - interval '15 minutes' AND ts < %(b)s
  WINDOW w AS (PARTITION BY point ORDER BY ts)),
c AS (
  SELECT point, ts, GREATEST(de, 0) AS de, gap, (ts - gap / 2) AT TIME ZONE %(tz)s AS mid FROM r
  WHERE ts >= %(a)s AND de IS NOT NULL),
k AS (
  SELECT point, ts, de, gap,
         CASE WHEN mid::date = ANY(%(off)s::date[]) OR mid::time < %(h1)s::time OR mid::time >= %(h2)s::time
              THEN 'off' ELSE 'work' END AS cls FROM c)
SELECT 'e' AS kind, t.object_id AS id, k.cls, sum(t.sign * k.de) AS v, min(k.ts) AS t0, max(k.ts) AS t1
FROM k JOIN object_terms t ON t.point = k.point
     AND (t.valid_from IS NULL OR k.ts >= t.valid_from) AND (t.valid_to IS NULL OR k.ts < t.valid_to)
GROUP BY t.object_id, k.cls
UNION ALL
SELECT 'h', point, cls, extract(epoch FROM sum(gap)) / 3600, NULL, NULL FROM k
WHERE gap < interval '5 minutes' GROUP BY point, cls
"""


def off_days(cfg, d1, d2):
    """Нерабочие даты в [d1, d2] по календарю calendar_ru и список лет, которых в календаре нет."""
    cal = cfg.get("calendar_ru") or {}
    out, missing = [], set()
    d = d1
    while d <= d2:
        y = cal.get(d.year)
        if y is None:
            missing.add(d.year)
            y = {}
        hol = set(y.get("holidays") or [])
        wrk = set(y.get("workdays") or [])
        if d in hol or (d.weekday() >= 5 and d not in wrk):
            out.append(d)
        d += dt.timedelta(days=1)
    return out, sorted(missing)


def split(conn, cfg, start, end):
    """({объект: {"work": кВт·ч, "off": кВт·ч}}, {"work": ч, "off": ч}, t0, t1, годы без календаря)."""
    tz = cfg["timezone"]
    h1, h2 = cfg["reports"].get("work_time", ["08:00", "19:00"])
    ltz = ZoneInfo(tz)
    off, missing = off_days(cfg, start.astimezone(ltz).date(), end.astimezone(ltz).date())
    with conn.cursor() as cur:
        cur.execute(SQL, {"a": start, "b": end, "tz": tz, "off": off, "h1": h1, "h2": h2})
        rows = cur.fetchall()
    energy, hours, t0, t1 = {}, {}, None, None
    for kind, oid, cls, v, a, b in rows:
        if kind == "e":
            energy.setdefault(oid, {"work": 0.0, "off": 0.0})[cls] = max(float(v or 0), 0.0)
            t0 = a if t0 is None or a < t0 else t0
            t1 = b if t1 is None or b > t1 else t1
        else:  # часы с данными: по самой полной точке
            hours[cls] = max(hours.get(cls, 0.0), float(v or 0))
    return energy, hours, t0, t1, missing


def _avg(kwh, h):
    return kwh / h if h and h > 0.05 else None


def text(conn, cfg, start, end, label):
    tz = ZoneInfo(cfg["timezone"])
    h1, h2 = cfg["reports"].get("work_time", ["08:00", "19:00"])
    energy, hours, t0, t1, missing = split(conn, cfg, start, end)
    lines = _head(cfg, "Рабочее и нерабочее время, %s" % label)
    if t0 is None:
        lines.append("За этот период данных нет (сбор идёт с 23.11.2026 15:09).")
        return "\n".join(lines)
    hw, ho = hours.get("work", 0.0), hours.get("off", 0.0)
    lines[2] = ("Данные с %s по %s. Часов с данными: рабочих %s, нерабочих %s." % (
        t0.astimezone(tz).strftime("%d.%m %H:%M"), t1.astimezone(tz).strftime("%d.%m %H:%M"),
        _fmt(hw, 0), _fmt(ho, 0)))  # вместо пустой строки после заголовка: перед каждым объектом своя пустая
    for o in cfg["objects"]:
        e = energy.get(o["id"], {"work": 0.0, "off": 0.0})
        w, f = e["work"], e["off"]
        s = w + f
        lines.append("")
        lines.append("<b>%s</b>: всего %s" % (o["name"], kwh_rub(s, cfg)))
        for icon, name, v, h in (("☀️", "рабочее", w, hw), ("🌙", "нерабочее", f, ho)):
            share = " — %d %%" % round(v / s * 100) if s > 0.05 else ""
            avg = _avg(v, h)
            lines.append("%s %s: %s%s%s" % (icon, name, kwh_rub(v, cfg), share,
                                            ", в среднем %s" % _fmt(avg, 1, "кВт") if avg is not None else ""))
    lines.append("")
    lines.append("Рабочее время — рабочие дни по производственному календарю РФ с %s до %s. Нерабочее — с %s до %s, "
                 "выходные и праздничные дни. «В среднем» — средняя мощность за эти часы." % (h1, h2, h2, h1))
    if missing:
        lines.append("⚠️ Для %s г. праздники в календаре не заданы: выходными считаются только суббота и воскресенье."
                     % ", ".join(map(str, missing)))
    if tariff_note(cfg):
        lines.append(tariff_note(cfg))
    return "\n".join(lines)
