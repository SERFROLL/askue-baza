"""База показаний (PostgreSQL). Схема создаётся сама при запуске опросчика."""
import time

import psycopg

from .config import db_dsn

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

CREATE TABLE IF NOT EXISTS points (
    id   text PRIMARY KEY,
    name text NOT NULL,
    sort int  NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_errors (
    ts    timestamptz NOT NULL DEFAULT now(),
    point text,
    error text
);
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
    with conn.cursor() as cur:
        for n, p in enumerate(cfg["points"]):
            cur.execute(
                "INSERT INTO points (id, name, sort) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, sort = EXCLUDED.sort",
                (p["id"], p["name"], n),
            )


def insert_reading(conn, ts, point, r):
    conn.execute(
        "INSERT INTO readings (ts, point, %s) VALUES (%%s, %%s, %s) ON CONFLICT DO NOTHING"
        % (", ".join(COLS), ", ".join(["%s"] * len(COLS))),
        [ts, point] + [r.get(c) for c in COLS],
    )


def insert_error(conn, point, error):
    conn.execute("INSERT INTO poll_errors (point, error) VALUES (%s, %s)", (point, error[:500]))
