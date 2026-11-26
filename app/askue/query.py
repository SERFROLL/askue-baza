"""Выборки для диалога бота: мощность и энергия по выбранным объектам за выбранный период."""
import datetime as dt
from zoneinfo import ZoneInfo

from .config import parse_terms

UTC = dt.timezone.utc

# период мощности (минуты) -> шаг усреднения графика (минуты; 0 — без усреднения, точки через 2 с)
POWER_STEP = {15: 0, 180: 1, 720: 5, 1440: 10}


def _q(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def terms_of(objs):
    return {o["id"]: parse_terms(o) for o in objs}


# ---------------- мощность ----------------
def power_now(conn, cfg, objs, sec=10):
    """Для каждого объекта: P и P по фазам (среднее за sec с), мин/макс за последнюю минуту."""
    pts = {r[0]: r[1:] for r in _q(conn, """
        SELECT point, avg(p), avg(p1), avg(p2), avg(p3) FROM power_fast
        WHERE ts > now() - make_interval(secs => %s) GROUP BY point""", (sec,))}
    mm = {r[0]: (r[1], r[2]) for r in _q(conn, """
        SELECT object_id, min(p), max(p) FROM object_power_fast
        WHERE ts > now() - interval '60 seconds' GROUP BY object_id""")}
    last = _q(conn, "SELECT max(ts) FROM power_fast")[0][0]
    out = {}
    for oid, terms in terms_of(objs).items():
        if all(pid in pts for pid, _ in terms):
            vals = [sum(s * (pts[pid][k] or 0) for pid, s in terms) for k in range(4)]
            vals[0] = max(0.0, vals[0])
            out[oid] = {"p": vals[0], "ph": vals[1:], "min": mm.get(oid, (None, None))[0], "max": mm.get(oid, (None, None))[1]}
    return out, last


def power_series(conn, oid, minutes):
    """(время, среднее, мин, макс) с шагом по POWER_STEP. Мин/макс — по 2-секундным данным (если они есть)."""
    step = POWER_STEP.get(minutes, max(1, minutes // 150))
    if step == 0:
        rows = _q(conn, """SELECT ts, p FROM object_power_fast WHERE object_id = %s
                           AND ts > now() - make_interval(mins => %s) ORDER BY ts""", (oid, minutes))
        return [(r[0], r[1], None, None) for r in rows]
    origin = "2000-01-01 00:00:00+00"
    avg = _q(conn, """SELECT date_bin(make_interval(mins => %s), ts, %s::timestamptz) AS b, avg(p) FROM object_power_1m
                      WHERE object_id = %s AND ts > now() - make_interval(mins => %s) GROUP BY b ORDER BY b""",
             (step, origin, oid, minutes))
    band = {r[0]: (r[1], r[2]) for r in _q(conn, """
        SELECT date_bin(make_interval(mins => %s), ts, %s::timestamptz) AS b, min(p), max(p) FROM object_power_fast
        WHERE object_id = %s AND ts > now() - make_interval(mins => %s) GROUP BY b""", (step, origin, oid, minutes))}
    return [(b, p, band.get(b, (None, None))[0], band.get(b, (None, None))[1]) for b, p in avg]


def power_peak(conn, oid, minutes):
    """Максимум по 2-секундным данным за период: (кВт, время) или None."""
    r = _q(conn, """SELECT ts, p FROM object_power_fast WHERE object_id = %s
                    AND ts > now() - make_interval(mins => %s) ORDER BY p DESC LIMIT 1""", (oid, minutes))
    return (r[0][1], r[0][0]) if r else None


# ---------------- энергия ----------------
def energy_period(cfg, code):
    """Код периода -> (начало, конец, подпись). Время местное, конец не включается."""
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(tz)
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = mid.replace(day=1)
    prev_month = (month - dt.timedelta(days=1)).replace(day=1)
    if code == "today":
        return mid, now, "сегодня"
    if code == "yday":
        return mid - dt.timedelta(days=1), mid, "вчера (%s)" % (mid - dt.timedelta(days=1)).strftime("%d.%m")
    if code == "7d":
        return mid - dt.timedelta(days=6), now, "7 дней"
    if code == "mon":
        return month, now, "этот месяц"
    if code == "pmon":
        return prev_month, month, "прошлый месяц (%s)" % prev_month.strftime("%m.%Y")
    if code.startswith("c"):  # c20261101-20261115
        a, b = code[1:].split("-")
        d1 = dt.datetime.strptime(a, "%Y%m%d").replace(tzinfo=tz)
        d2 = dt.datetime.strptime(b, "%Y%m%d").replace(tzinfo=tz) + dt.timedelta(days=1)
        return d1, min(d2, now), "%s–%s" % (d1.strftime("%d.%m.%Y"), (d2 - dt.timedelta(days=1)).strftime("%d.%m.%Y"))
    raise ValueError(code)


def point_readings(conn, pids, start, end):
    """{точка: (показание на начало, время, показание на конец, время)} по счётчику энергии приёма."""
    out = {}
    for pid in pids:
        a = _q(conn, "SELECT e_import, ts FROM readings WHERE point = %s AND ts >= %s AND ts < %s ORDER BY ts LIMIT 1",
               (pid, start, end))
        b = _q(conn, "SELECT e_import, ts FROM readings WHERE point = %s AND ts >= %s AND ts < %s ORDER BY ts DESC LIMIT 1",
               (pid, start, end))
        if a and b:
            out[pid] = (a[0][0], a[0][1], b[0][0], b[0][1])
    return out


def object_periods(conn, oids):
    """{объект: [(действует_с | None, действует_до | None, [(точка, знак)])]} из object_terms.
    Периоды, когда объекта не было (пустой состав), в таблице отсутствуют — их энергия считается 0."""
    out = {}
    rows = _q(conn, """SELECT object_id, period, valid_from, valid_to, point, sign FROM object_terms
                       WHERE object_id = ANY(%s) ORDER BY object_id, period, point""", (list(oids),))
    for oid, k, vf, vt, pid, s in rows:
        per = out.setdefault(oid, {}).setdefault(k, (vf, vt, []))
        per[2].append((pid, s))
    return {oid: [ps[k] for k in sorted(ps)] for oid, ps in out.items()}


def _clip(vf, vt, start, end):
    """Пересечение периода [vf, vt) с [start, end); end=None — до сейчас. None, если не пересекаются."""
    a = start if vf is None or (start is not None and start > vf) else vf
    if vt is None:
        b = end
    elif end is None:
        b = vt
    else:
        b = min(vt, end)
    if b is not None and a is not None and a >= b:
        return None
    return a, b


def _first_last(conn, pid, a, b, cache):
    """Показания счётчика энергии приёма: (первое, последнее) в [a, b) или None, если показаний нет."""
    key = (pid, a, b)
    if key not in cache:
        cond = "AND ts < %s" if b is not None else ""
        par = (pid, a) + ((b,) if b is not None else ())
        f = _q(conn, "SELECT e_import FROM readings WHERE point = %%s AND ts >= %%s %s ORDER BY ts LIMIT 1" % cond, par)
        l = _q(conn, "SELECT e_import FROM readings WHERE point = %%s AND ts >= %%s %s ORDER BY ts DESC LIMIT 1" % cond, par)
        cache[key] = (f[0][0], l[0][0]) if f and l and f[0][0] is not None and l[0][0] is not None else None
    return cache[key]


def object_kwh(conn, oids, start, end=None):
    """{объект: кВт·ч или None (нет показаний)} за [start, end) с учётом смены состава объектов во времени:
    каждый период состава считается по своим точкам, результаты складываются."""
    per = object_periods(conn, oids)
    cache, out = {}, {}
    for oid in oids:
        if oid not in per:
            out[oid] = None
            continue
        total, ok = 0.0, True
        for vf, vt, terms in per[oid]:
            seg = _clip(vf, vt, start, end)
            if seg is None:
                continue
            for pid, s in terms:
                fl = _first_last(conn, pid, seg[0], seg[1], cache)
                if fl is None:
                    ok = False
                    break
                total += s * (fl[1] - fl[0])
            if not ok:
                break
        out[oid] = max(0.0, total) if ok else None
    return out


def energy_totals(conn, objs, start, end):
    """{объект: кВт·ч} за период по разности показаний счётчиков энергии (с учётом смены состава объектов);
    плюс показания по точкам текущего состава."""
    terms = terms_of(objs)
    pids = sorted({pid for t in terms.values() for pid, _ in t})
    rd = point_readings(conn, pids, start, end)
    tot = {oid: v for oid, v in object_kwh(conn, list(terms), start, end).items() if v is not None}
    return tot, rd


def _step_td(step):
    n, unit = step.split()
    unit = unit.rstrip("s")
    return dt.timedelta(**{{"minute": "minutes", "hour": "hours", "day": "days"}[unit]: int(n)})


def energy_series(conn, cfg, objs, start, end, step):
    """Нарастающий итог по объектам: (времена начал интервалов, {объект: [кВт·ч от начала периода]}).
    step — интервал PostgreSQL ('10 minutes', '1 hour', '1 day'); интервалы выровнены по местной полуночи.
    Учитывается смена состава объектов во времени (history в config.yaml)."""
    oids = [o["id"] for o in objs]
    per = object_periods(conn, oids)
    pids = sorted({pid for ps in per.values() for _, _, t in ps for pid, _ in t})
    origin = start.replace(hour=0, minute=0, second=0, microsecond=0)
    td = _step_td(step)
    per_point = {}
    for pid in pids:
        rows = _q(conn, """SELECT date_bin(%s::interval, ts, %s) AS b, max(e_import) FROM readings
                           WHERE point = %s AND ts >= %s AND ts < %s GROUP BY b""", (step, origin, pid, start, end))
        if rows:
            per_point[pid] = {r[0]: r[1] for r in rows}
    buckets = sorted(set().union(*[set(v) for v in per_point.values()])) if per_point else []
    cum = {}  # последнее известное показание точки на конец каждого интервала
    for pid, vals in per_point.items():
        last, cum[pid] = None, {}
        for b in buckets:
            if b in vals:
                last = vals[b]
            cum[pid][b] = last
    cache, series = {}, {}
    for oid in oids:
        if oid not in per:
            continue
        segs, ok = [], True
        for vf, vt, terms in per[oid]:
            seg = _clip(vf, vt, start, end)
            if seg is None:
                continue
            e0, e1 = {}, {}
            for pid, _ in terms:
                fl = _first_last(conn, pid, seg[0], seg[1], cache)
                if fl is None or pid not in cum:
                    ok = False
                    break
                e0[pid], e1[pid] = fl
            if not ok:
                break
            segs.append((seg[0], seg[1], terms, e0, e1))
        if not ok:
            continue
        vals = []
        for b in buckets:
            be, v = b + td, 0.0
            for a, bnd, terms, e0, e1 in segs:
                if a >= be:
                    continue  # период начинается позже этого интервала
                for pid, s in terms:
                    if bnd is not None and bnd <= be:
                        x = e1[pid]  # период уже закончился
                    else:
                        x = cum[pid][b] if cum[pid][b] is not None else e0[pid]
                    v += s * (max(x, e0[pid]) - e0[pid])
            vals.append(max(0.0, v))
        series[oid] = vals
    return buckets, series
