"""База показаний (PostgreSQL). Схема и представления создаются сами при запуске опросчика.

Таблицы:
  readings     — раз в минуту полный набор по точке: U, I, P, cos, энергия приём/отдача (счётчики энергии).
  power_fast   — каждые fast_interval_s: P total и по фазам; хранится fast_retention_days.
  power_1m     — минутные средняя / мин / макс мощности из power_fast; хранится всегда.
  objects, object_terms — объекты и из каких точек они складываются (из config.yaml).
Представления по объектам: object_power_fast, object_power_1m, object_energy_day, object_night.
"""
import time

import psycopg

from .config import db_dsn, parse_terms

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts        timestamptz NOT NULL,
    point     text        NOT NULL,
    u1 real, u2 real, u3 real,
    i1 real, i2 real, i3 real,
    p_total real, p1 real, p2 real, p3 real,
    pf real,
    e_import double precision,
    e_export double precision,
    PRIMARY KEY (point, ts)
);
CREATE INDEX IF NOT EXISTS readings_ts ON readings (ts);

CREATE TABLE IF NOT EXISTS power_fast (
    ts timestamptz NOT NULL, point text NOT NULL,
    p real, p1 real, p2 real, p3 real,
    PRIMARY KEY (point, ts)
);
CREATE INDEX IF NOT EXISTS power_fast_ts ON power_fast (ts);

CREATE TABLE IF NOT EXISTS power_1m (
    ts timestamptz NOT NULL, point text NOT NULL,
    p_avg real, p_min real, p_max real, n int,
    PRIMARY KEY (point, ts)
);
CREATE INDEX IF NOT EXISTS power_1m_ts ON power_1m (ts);

CREATE TABLE IF NOT EXISTS points (id text PRIMARY KEY, name text NOT NULL, sort int NOT NULL);
CREATE TABLE IF NOT EXISTS objects (
    id text PRIMARY KEY, name text NOT NULL, sort int NOT NULL,
    night_max_kw real, is_total boolean NOT NULL DEFAULT false, n_terms int NOT NULL DEFAULT 1
);
ALTER TABLE objects ADD COLUMN IF NOT EXISTS parent text;
CREATE TABLE IF NOT EXISTS object_terms (
    object_id text NOT NULL, point text NOT NULL, sign smallint NOT NULL,
    PRIMARY KEY (object_id, point)
);
CREATE TABLE IF NOT EXISTS poll_errors (
    ts    timestamptz NOT NULL DEFAULT now(),
    point text,
    error text
);
"""

# {tz} подставляется из config.yaml (проверено регулярным выражением при загрузке)
VIEWS = """
CREATE OR REPLACE VIEW object_power_fast AS
SELECT f.ts, t.object_id, GREATEST(sum(t.sign * f.p), 0) AS p
FROM power_fast f JOIN object_terms t ON t.point = f.point JOIN objects o ON o.id = t.object_id
GROUP BY f.ts, t.object_id HAVING count(*) = max(o.n_terms);

CREATE OR REPLACE VIEW object_power_1m AS
SELECT m.ts, t.object_id, GREATEST(sum(t.sign * m.p_avg), 0) AS p
FROM power_1m m JOIN object_terms t ON t.point = m.point JOIN objects o ON o.id = t.object_id
GROUP BY m.ts, t.object_id HAVING count(*) = max(o.n_terms);

CREATE OR REPLACE VIEW point_energy_day AS
SELECT (ts AT TIME ZONE '{tz}')::date AS day, point,
       max(e_import) - min(e_import) AS kwh, min(e_import) AS e_start, max(e_import) AS e_end,
       min(ts) AS t_first, max(ts) AS t_last
FROM readings GROUP BY 1, 2;

CREATE OR REPLACE VIEW object_energy_day AS
SELECT d.day, t.object_id, GREATEST(sum(t.sign * d.kwh), 0) AS kwh, min(d.t_first) AS t_first
FROM point_energy_day d JOIN object_terms t ON t.point = d.point JOIN objects o ON o.id = t.object_id
GROUP BY d.day, t.object_id HAVING count(*) = max(o.n_terms);

CREATE OR REPLACE VIEW object_night AS
SELECT (ts AT TIME ZONE '{tz}')::date AS night, object_id, avg(p) AS p_avg, max(p) AS p_max
FROM object_power_1m
WHERE extract(hour FROM ts AT TIME ZONE '{tz}') >= {h1} AND extract(hour FROM ts AT TIME ZONE '{tz}') < {h2}
GROUP BY 1, 2;
"""

COLS = ["u1", "u2", "u3", "i1", "i2", "i3", "p_total", "p1", "p2", "p3", "pf", "e_import", "e_export"]


def connect(retries=30):
    last = None
    for _ in range(retries):
        try:
            return psycopg.connect(db_dsn(), autocommit=True)
        except psycopg.OperationalError as e:
            last = e
            time.sleep(2)
    raise last


def init(conn, cfg):
    conn.execute(SCHEMA)
    h1, h2 = cfg["reports"]["night_hours"]
    # представления пересоздаются: их состав мог поменяться
    for v in ("object_night", "object_energy_day", "point_energy_day", "object_power_1m", "object_power_fast"):
        conn.execute("DROP VIEW IF EXISTS %s" % v)
    conn.execute(VIEWS.format(tz=cfg["timezone"], h1=int(h1), h2=int(h2)))
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM points")
            for n, p in enumerate(cfg["points"]):
                cur.execute("INSERT INTO points (id, name, sort) VALUES (%s, %s, %s)", (p["id"], p["name"], n))
            cur.execute("DELETE FROM object_terms")
            cur.execute("DELETE FROM objects")
            for n, o in enumerate(cfg["objects"]):
                terms = parse_terms(o)
                cur.execute("INSERT INTO objects (id, name, sort, night_max_kw, is_total, n_terms, parent) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            (o["id"], o["name"], n, o.get("night_max_kw"), bool(o.get("total")), len(terms), o.get("parent")))
                for pid, sign in terms:
                    cur.execute("INSERT INTO object_terms (object_id, point, sign) VALUES (%s, %s, %s)",
                                (o["id"], pid, sign))
    # минуты до запуска быстрого опроса: минутная запись = мгновенный снимок из readings
    conn.execute("""
        INSERT INTO power_1m (ts, point, p_avg, p_min, p_max, n)
        SELECT ts, point, p_total, p_total, p_total, 1 FROM readings
        WHERE ts < COALESCE((SELECT min(ts) FROM power_fast), now() - interval '2 minutes')
        ON CONFLICT DO NOTHING""")


def insert_reading(conn, ts, point, r):
    conn.execute(
        "INSERT INTO readings (ts, point, %s) VALUES (%%s, %%s, %s) ON CONFLICT DO NOTHING"
        % (", ".join(COLS), ", ".join(["%s"] * len(COLS))),
        [ts, point] + [r.get(c) for c in COLS],
    )


def insert_fast(conn, rows):
    """rows: [(ts, point, p, p1, p2, p3), ...]"""
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO power_fast (ts, point, p, p1, p2, p3) VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING", rows)


def rollup_minute(conn, minute_start):
    """Минутные средняя/мин/макс за минуту [minute_start, +1 мин) из power_fast."""
    conn.execute("""
        INSERT INTO power_1m (ts, point, p_avg, p_min, p_max, n)
        SELECT %s, point, avg(p), min(p), max(p), count(*) FROM power_fast
        WHERE ts >= %s AND ts < %s + interval '1 minute' GROUP BY point
        ON CONFLICT (point, ts) DO UPDATE SET p_avg = EXCLUDED.p_avg, p_min = EXCLUDED.p_min,
            p_max = EXCLUDED.p_max, n = EXCLUDED.n""", (minute_start, minute_start, minute_start))


def purge_fast(conn, days):
    conn.execute("DELETE FROM power_fast WHERE ts < now() - make_interval(days => %s)", (int(days),))


def insert_error(conn, point, error):
    conn.execute("INSERT INTO poll_errors (point, error) VALUES (%s, %s)", (point, error[:500]))
