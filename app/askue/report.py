"""Тексты отчётов для Telegram: вечерний отчёт, состояние связи, предупреждения."""
import datetime as dt
from zoneinfo import ZoneInfo


def _q(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def current_power(conn, minutes=5):
    """Средняя мощность за последние minutes минут и время последнего показания по каждой точке."""
    rows = _q(conn, """
        SELECT point, avg(p_total), max(ts) FROM readings
        WHERE ts > now() - make_interval(mins => %s) GROUP BY point""", (minutes,))
    return {r[0]: (r[1], r[2]) for r in rows}


def last_seen(conn):
    return {r[0]: r[1] for r in _q(conn, "SELECT point, max(ts) FROM readings GROUP BY point")}


def typical_night(conn, cfg):
    """Медиана часовых средних мощностей в ночные часы за прошлые дни."""
    rep = cfg["reports"]
    h1, h2 = rep["night_hours"]
    rows = _q(conn, """
        SELECT point, percentile_cont(0.5) WITHIN GROUP (ORDER BY hp) FROM (
            SELECT point, date_trunc('hour', ts) AS hh, avg(p_total) AS hp FROM readings
            WHERE ts > now() - make_interval(days => %s)
              AND extract(hour FROM ts AT TIME ZONE %s) >= %s
              AND extract(hour FROM ts AT TIME ZONE %s) < %s
            GROUP BY point, hh) x
        GROUP BY point""", (rep["typical_days"], cfg["timezone"], h1, cfg["timezone"], h2))
    return {r[0]: r[1] for r in rows}


def energy_today(conn, cfg):
    tz = ZoneInfo(cfg["timezone"])
    midnight = dt.datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = _q(conn, """
        SELECT point, max(e_import) - min(e_import) FROM readings
        WHERE ts >= %s GROUP BY point""", (midnight,))
    return {r[0]: r[1] for r in rows}


def _fmt(x, nd=1, unit=""):
    """Число с запятой и единицей; «нет данных», если значения нет."""
    if x is None:
        return "нет данных"
    s = (("%." + str(nd) + "f") % x).replace(".", ",")
    return s + (" " + unit if unit else "")


def assess(p, now_kw, night_kw, rep):
    """Оценка точки: (значок, пояснение)."""
    thr = p.get("night_max_kw")
    if now_kw is None:
        return "⚪", "нет данных"
    if thr is not None:
        if now_kw > thr:
            return "🔴", "Выше порога %s — не выключен?" % _fmt(thr, 1, "кВт")
        return "🟢", "В норме (порог %s)." % _fmt(thr, 1, "кВт")
    if night_kw is not None and now_kw > max(night_kw * rep["suspicious_factor"], rep["suspicious_min_kw"]):
        return "🟠", "Больше обычного ночного — проверить."
    if night_kw is None:
        return "⚪", "Порог не задан, статистики пока нет."
    return "🟢", "Как обычно ночью."


def evening_report(conn, cfg, title="Вечерний отчёт"):
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(tz)
    cur = current_power(conn)
    night = typical_night(conn, cfg)
    energy = energy_today(conn, cfg)
    lines = ["<b>%s — %s</b>" % (title, now.strftime("%d.%m.%Y %H:%M")), cfg["site"], ""]
    flagged = 0
    for p in cfg["points"]:
        now_kw = cur.get(p["id"], (None, None))[0]
        icon, note = assess(p, now_kw, night.get(p["id"]), cfg["reports"])
        if icon in ("🔴", "🟠"):
            flagged += 1
        lines.append("%s <b>%s</b>: сейчас %s, обычно ночью %s, за сегодня %s. %s" % (
            icon, p["name"], _fmt(now_kw, 1, "кВт"), _fmt(night.get(p["id"]), 1, "кВт"),
            _fmt(energy.get(p["id"]), 0, "кВт·ч"), note))
    lines.append("")
    lines.append("Требуют проверки: %d." % flagged if flagged else "Все объекты в норме.")
    return "\n".join(lines)


def status_report(conn, cfg):
    tz = ZoneInfo(cfg["timezone"])
    seen = last_seen(conn)
    now = dt.datetime.now(dt.timezone.utc)
    lim = cfg["alerts"]["offline_min"]
    lines = ["<b>Связь со счётчиками</b>"]
    for p in cfg["points"]:
        t = seen.get(p["id"])
        if t is None:
            lines.append("⚪ %s: данных нет" % p["name"])
            continue
        age = (now - t).total_seconds() / 60
        icon = "🟢" if age <= lim else "🔴"
        lines.append("%s %s: последнее показание %s" % (icon, p["name"], t.astimezone(tz).strftime("%d.%m %H:%M")))
    return "\n".join(lines)
