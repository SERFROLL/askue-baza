"""Тексты для Telegram по объектам (Склад 1, Склад 2 расчётом, Склад 3, ...) и по точкам (техника)."""
import datetime as dt
import statistics
from zoneinfo import ZoneInfo

from .config import objects as cfg_objects
from .query import object_kwh

SPARK = "▁▂▃▄▅▆▇█"


def _q(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _fmt(x, nd=1, unit=""):
    """Число с запятой и единицей; «нет данных», если значения нет."""
    if x is None:
        return "нет данных"
    s = (("%." + str(nd) + "f") % x).replace(".", ",")
    return s + (" " + unit if unit else "")


def tariff(cfg):
    return cfg.get("reports", {}).get("tariff_rub_per_kwh")


def kwh_rub(x, cfg, nd=1):
    """«123,4 кВт·ч (1 049 ₽)» — энергия и её стоимость по тарифу из config.yaml."""
    s = _fmt(x, nd, "кВт·ч")
    t = tariff(cfg)
    if x is None or not t:
        return s
    return "%s (%s ₽)" % (s, "{:,.0f}".format(x * t).replace(",", " "))


def tariff_note(cfg):
    t = tariff(cfg)
    return "Рубли — по тарифу %s ₽ за кВт·ч." % _fmt(t, 2).rstrip("0").rstrip(",") if t else None


# ---------- данные по объектам ----------
def now_power(conn, cfg):
    """Мощность «сейчас» по объектам: среднее за now_avg_s по 2-секундным данным, кВт. Плюс время последних данных."""
    sec = int(cfg["reports"].get("now_avg_s", 10))
    rows = _q(conn, """SELECT object_id, avg(p), max(ts) FROM object_power_fast
                       WHERE ts > now() - make_interval(secs => %s) GROUP BY object_id""", (sec,))
    if not rows:  # быстрых данных нет — берём последние минутные
        rows = _q(conn, """SELECT object_id, avg(p), max(ts) FROM object_power_1m
                           WHERE ts > now() - interval '3 minutes' GROUP BY object_id""")
    return {r[0]: (r[1], r[2]) for r in rows}


def avg_power(conn, minutes):
    rows = _q(conn, """SELECT object_id, avg(p) FROM object_power_1m
                       WHERE ts > now() - make_interval(mins => %s) GROUP BY object_id""", (minutes,))
    return {r[0]: r[1] for r in rows}


def typical_night(conn, cfg):
    """Медиана часовых средних мощностей в ночные часы за прошлые дни, кВт."""
    rep = cfg["reports"]
    h1, h2 = rep["night_hours"]
    tz = cfg["timezone"]
    rows = _q(conn, """
        SELECT object_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY hp) FROM (
            SELECT object_id, date_trunc('hour', ts) AS hh, avg(p) AS hp FROM object_power_1m
            WHERE ts > now() - make_interval(days => %s)
              AND extract(hour FROM ts AT TIME ZONE %s) >= %s AND extract(hour FROM ts AT TIME ZONE %s) < %s
            GROUP BY object_id, hh) x
        GROUP BY object_id""", (rep["typical_days"], tz, h1, tz, h2))
    return {r[0]: r[1] for r in rows}


def energy_day(conn, day):
    """{object_id: (кВт·ч, время первых данных за этот день)}"""
    return {r[0]: (r[1], r[2]) for r in _q(conn, "SELECT object_id, kwh, t_first FROM object_energy_day WHERE day = %s", (day,))}


def sparklines(conn, minutes=120, buckets=24):
    """Мини-тренд за minutes минут из символов ▁▂▃▄▅▆▇█ (шкала своя у каждого объекта, от 0 до максимума)."""
    step = max(1, minutes // buckets)
    rows = _q(conn, """
        SELECT object_id, floor(extract(epoch FROM now() - ts) / 60 / %s)::int AS b, avg(p)
        FROM object_power_1m WHERE ts > now() - make_interval(mins => %s)
        GROUP BY 1, 2""", (step, minutes))
    data = {}
    for oid, b, p in rows:
        data.setdefault(oid, {})[b] = p
    out = {}
    for oid, d in data.items():
        series = [d.get(b) for b in range(buckets - 1, -1, -1)]
        top = max([v for v in series if v is not None] + [0.1])
        out[oid] = "".join(" " if v is None else SPARK[min(7, int(v / top * 7.999))] for v in series)
    return out


def assess(o, now_kw, night_kw, rep):
    """Оценка объекта: (значок, пояснение)."""
    thr = o.get("night_max_kw")
    if now_kw is None:
        return "⚪", "Нет данных."
    if thr is not None:
        if now_kw > thr:
            return "🔴", "Выше порога %s — не выключен?" % _fmt(thr, 1, "кВт")
        return "🟢", "В норме (порог %s)." % _fmt(thr, 1, "кВт")
    if night_kw is not None and now_kw > max(night_kw * rep["suspicious_factor"], rep["suspicious_min_kw"]):
        return "🟠", "Больше обычного ночного — проверить."
    if night_kw is None:
        return "⚪", "Порог не задан, статистики пока нет."
    return "🟢", "Как обычно ночью."


# ---------- отчёты ----------
def _label(o, bold=True):
    """Имя объекта для текста; нитка склада — с отступом под своим складом."""
    if o.get("parent"):
        return "   ↳ " + o["name"]  # Склад 3/1 — под строкой «Склад 3»
    return "<b>%s</b>" % o["name"] if bold else o["name"]


def _head(cfg, title):
    now = dt.datetime.now(ZoneInfo(cfg["timezone"]))
    return ["<b>%s — %s</b>" % (title, now.strftime("%d.%m.%Y %H:%M")), cfg["site"], ""]


def evening_report(conn, cfg, title="Вечерний отчёт", full=True):
    """full=False — для охраны: только объекты, которые требуют проверки."""
    tz = ZoneInfo(cfg["timezone"])
    today = dt.datetime.now(tz).date()
    midnight = dt.datetime.combine(today, dt.time(0), tz)
    cur = avg_power(conn, 5)  # за 5 минут: короткие пуски не дают ложных тревог
    night = typical_night(conn, cfg)
    energy = energy_day(conn, today)
    lines = _head(cfg, title)
    flagged, unknown, objs = [], 0, cfg_objects(cfg, with_total=False)
    for o in objs:
        now_kw = cur.get(o["id"])
        icon, note = assess(o, now_kw, night.get(o["id"]), cfg["reports"])
        kwh, since = energy.get(o["id"], (None, None))
        period = "с %s" % since.astimezone(tz).strftime("%H:%M") if since and since - midnight > dt.timedelta(minutes=15) else "за сегодня"
        line = "%s %s: сейчас %s, обычно ночью %s, %s %s. %s" % (
            icon, _label(o), _fmt(now_kw, 1, "кВт"), _fmt(night.get(o["id"]), 1, "кВт"), period, kwh_rub(kwh, cfg, 0), note)
        if icon in ("🔴", "🟠"):
            flagged.append(line)
        elif icon == "⚪":
            unknown += 1
        if full:
            lines.append(line)
    if not full:
        if flagged:
            lines += flagged
        else:
            lines.pop()  # без пустой строки перед итогом
    if full:
        tot = [o for o in cfg["objects"] if o.get("total")]
        for o in tot:
            kwh, _ = energy.get(o["id"], (None, None))
            lines.append("")
            lines.append("<b>%s</b>: сейчас %s, за сегодня %s." % (o["name"], _fmt(cur.get(o["id"]), 1, "кВт"), kwh_rub(kwh, cfg, 0)))
    lines.append("")
    if flagged:
        lines.append("Требуют проверки: %d." % len(flagged))
    elif unknown == len(objs):
        lines.append("Оценить пока нельзя: ночные пороги не заданы, а статистика ночного потребления "
                     "накопится примерно за неделю.")
    elif unknown:
        lines.append("Объекты, которые можно оценить, в норме. Без оценки: %d." % unknown)
    else:
        lines.append("Все объекты в норме.")
    if full and tariff_note(cfg):
        lines.append(tariff_note(cfg))
    return "\n".join(lines)


def now_report(conn, cfg):
    """«Сейчас»: мощность каждого объекта (среднее за несколько секунд) и мини-тренд за 2 часа."""
    tz = ZoneInfo(cfg["timezone"])
    cur = now_power(conn, cfg)
    night = typical_night(conn, cfg)
    spark = sparklines(conn)
    lines = _head(cfg, "Сейчас")
    for o in cfg["objects"]:
        kw, ts = cur.get(o["id"], (None, None))
        if o.get("total"):
            lines.append("")
            lines.append("<b>%s</b>: %s" % (o["name"], _fmt(kw, 1, "кВт")))
            lines.append("<code>%s</code>" % spark.get(o["id"], ""))
            continue
        icon, _ = assess(o, kw, night.get(o["id"]), cfg["reports"])
        lines.append("%s %s: %s" % (icon, _label(o), _fmt(kw, 1, "кВт")))
        lines.append("<code>%s</code>" % spark.get(o["id"], ""))
    newest = max([v[1] for v in cur.values() if v[1]], default=None)
    lines.append("")
    lines.append("Мощность — среднее за %s с; тренд — последние 2 часа, шкала у каждого объекта своя. Данные на %s." % (
        cfg["reports"].get("now_avg_s", 10), newest.astimezone(tz).strftime("%H:%M:%S") if newest else "—"))
    return "\n".join(lines)


def morning_report(conn, cfg):
    """Расход за прошлые сутки по объектам и сравнение с медианой предыдущих 7 суток."""
    tz = ZoneInfo(cfg["timezone"])
    y = dt.datetime.now(tz).date() - dt.timedelta(days=1)
    rows = _q(conn, "SELECT day, object_id, kwh FROM object_energy_day WHERE day BETWEEN %s AND %s",
              (y - dt.timedelta(days=7), y))
    by = {}
    for day, oid, kwh in rows:
        by.setdefault(oid, {})[day] = kwh
    lines = _head(cfg, "Утренняя сводка за %s" % y.strftime("%d.%m.%Y"))
    for o in cfg["objects"]:
        d = by.get(o["id"], {})
        prev = [v for k, v in d.items() if k != y and v is not None]
        cmp = ""
        if prev and d.get(y) is not None:
            med = statistics.median(prev)
            if med > 0.5:
                cmp = ", обычно %s, %+.0f%%" % (_fmt(med, 0, "кВт·ч"), (d[y] - med) / med * 100)
        name = "<b>%s</b>" % o["name"] if o.get("total") else _label(o, bold=False)
        lines.append("%s: %s%s" % (name, kwh_rub(d.get(y), cfg, 0), cmp))
    lines.append("")
    lines.append(period_energy_text(conn, cfg, month_start(cfg), None, "С начала месяца"))
    if tariff_note(cfg):
        lines.append(tariff_note(cfg))
    return "\n".join(lines)


def month_start(cfg, months_back=0):
    tz = ZoneInfo(cfg["timezone"])
    d = dt.datetime.now(tz).date().replace(day=1)
    for _ in range(months_back):
        d = (d - dt.timedelta(days=1)).replace(day=1)
    return dt.datetime.combine(d, dt.time(0), tz)


def period_energy(conn, start, end=None):
    """Энергия за период по показаниям счётчиков энергии (как у энергосбыта): по точкам — показания
    на начало и конец периода; по объектам — разность с учётом знаков. Возвращает (points, objects, t0, t1)."""
    end_sql = "AND ts < %s" if end else ""
    params = (start,) + ((end,) if end else ())
    pts = _q(conn, """
        SELECT p.id, p.name, a.e0, b.e1, a.t0, b.t1 FROM points p
        LEFT JOIN LATERAL (SELECT e_import AS e0, ts AS t0 FROM readings r
                           WHERE r.point = p.id AND ts >= %%s %s ORDER BY ts LIMIT 1) a ON true
        LEFT JOIN LATERAL (SELECT e_import AS e1, ts AS t1 FROM readings r
                           WHERE r.point = p.id AND ts >= %%s %s ORDER BY ts DESC LIMIT 1) b ON true
        ORDER BY p.sort""" % (end_sql, end_sql), params + params)
    rows = _q(conn, "SELECT id, name, is_total FROM objects ORDER BY sort")
    kwh = object_kwh(conn, [r[0] for r in rows], start, end)  # с учётом смены состава объектов во времени
    objs = [(oid, name, is_total, kwh.get(oid)) for oid, name, is_total in rows]
    t0 = min([r[4] for r in pts if r[4]], default=None)
    t1 = max([r[5] for r in pts if r[5]], default=None)
    return pts, objs, t0, t1


def period_energy_text(conn, cfg, start, end, title):
    tz = ZoneInfo(cfg["timezone"])
    pts, objs, t0, t1 = period_energy(conn, start, end)
    if t0 is None:
        return "%s: данных нет." % title
    lines = ["<b>%s</b>: данные с %s по %s" % (title, t0.astimezone(tz).strftime("%d.%m %H:%M"), t1.astimezone(tz).strftime("%d.%m %H:%M"))]
    for oid, name, is_total, kwh in objs:
        lines.append(("<b>%s</b>: %s" if is_total else "%s: %s") % (name, kwh_rub(kwh, cfg)))
    return "\n".join(lines)


def energy_report(conn, cfg):
    """Расход за текущий и прошлый месяц + показания счётчиков энергии — для сверки с энергосбытом."""
    tz = ZoneInfo(cfg["timezone"])
    cur, prev = month_start(cfg), month_start(cfg, 1)
    lines = _head(cfg, "Расход энергии")
    lines.append(period_energy_text(conn, cfg, cur, None, "Текущий месяц"))
    lines.append("")
    lines.append(period_energy_text(conn, cfg, prev, cur, "Прошлый месяц"))
    pts, _, _, _ = period_energy(conn, cur, None)
    lines.append("")
    lines.append("<b>Показания счётчиков энергии (приём), кВт·ч</b>")
    for pid, name, e0, e1, t0, t1 in pts:
        lines.append("%s: %s → %s" % (name, _fmt(e0, 1), _fmt(e1, 1)))
    lines.append("")
    lines.append("Для сверки с энергосбытом сравнивайте «Вся база» с коммерческим счётчиком за те же даты. "
                 "Данные собираются с 23.11.2026 15:09.")
    if tariff_note(cfg):
        lines.append(tariff_note(cfg))
    return "\n".join(lines)


def status_report(conn, cfg):
    """Техника: связь по каждому каналу счётчиков и рост энергии отдачи."""
    tz = ZoneInfo(cfg["timezone"])
    seen = {r[0]: r[1] for r in _q(conn, "SELECT point, max(ts) FROM power_fast WHERE ts > now() - interval '1 day' GROUP BY point")}
    full = {r[0]: r[1] for r in _q(conn, "SELECT point, max(ts) FROM readings GROUP BY point")}
    exp = export_growth(conn, 60)
    now = dt.datetime.now(dt.timezone.utc)
    lim = cfg["alerts"]["offline_min"]
    lines = ["<b>Связь со счётчиками</b>"]
    for p in cfg["points"]:
        t = seen.get(p["id"]) or full.get(p["id"])
        if t is None:
            lines.append("⚪ %s: данных нет" % p["name"])
            continue
        icon = "🟢" if (now - t).total_seconds() <= lim * 60 else "🔴"
        extra = ""
        if exp.get(p["id"], 0) > cfg["alerts"]["export_kwh_per_hour"]:
            icon, extra = "🟠", ", растёт энергия отдачи (+%s за час) — проверить ТТ" % _fmt(exp[p["id"]], 2, "кВт·ч")
        lines.append("%s %s: %s%s" % (icon, p["name"], t.astimezone(tz).strftime("%d.%m %H:%M:%S"), extra))
    return "\n".join(lines)


def export_growth(conn, minutes):
    rows = _q(conn, """SELECT point, max(e_export) - min(e_export) FROM readings
                       WHERE ts > now() - make_interval(mins => %s) GROUP BY point""", (minutes,))
    return {r[0]: r[1] or 0 for r in rows}


def last_seen(conn):
    rows = _q(conn, """SELECT point, max(ts) FROM (
                           SELECT point, ts FROM power_fast WHERE ts > now() - interval '1 day'
                           UNION ALL SELECT point, max(ts) FROM readings GROUP BY point) x GROUP BY point""")
    return {r[0]: r[1] for r in rows}
