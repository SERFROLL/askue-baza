import os
import re

import yaml

CONFIG_PATH = os.environ.get("ASKUE_CONFIG", "/config/config.yaml")
TERM = re.compile(r"^([+-])(.+)$")


def parse_terms(obj):
    """['+M1.4', '-M3'] -> [('M1.4', 1), ('M3', -1)]"""
    out = []
    for t in obj["terms"]:
        m = TERM.match(t.strip())
        if not m:
            raise ValueError("объект %s: неверный член %r (нужно +ID или -ID)" % (obj["id"], t))
        out.append((m.group(2), 1 if m.group(1) == "+" else -1))
    return out


def load(path=None):
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    ids = [p["id"] for p in cfg["points"]]
    if len(ids) != len(set(ids)):
        raise ValueError("в config.yaml повторяются id точек учёта")
    for p in cfg["points"]:
        if p["gateway"] not in cfg["gateways"]:
            raise ValueError("точка %s: нет шлюза %s" % (p["id"], p["gateway"]))
    oids = [o["id"] for o in cfg["objects"]]
    if len(oids) != len(set(oids)):
        raise ValueError("в config.yaml повторяются id объектов")
    for o in cfg["objects"]:
        for pid, _ in parse_terms(o):
            if pid not in ids:
                raise ValueError("объект %s: нет точки %s" % (o["id"], pid))
    if not re.match(r"^[A-Za-z_/+-]+$", cfg["timezone"]):
        raise ValueError("странное имя часового пояса")
    return cfg


def objects(cfg, with_total=True):
    return [o for o in cfg["objects"] if with_total or not o.get("total")]


def db_dsn():
    return "host=%s dbname=%s user=%s password=%s" % (
        os.environ.get("POSTGRES_HOST", "db"),
        os.environ.get("POSTGRES_DB", "askue"),
        os.environ.get("POSTGRES_USER", "askue"),
        os.environ["POSTGRES_PASSWORD"],
    )
