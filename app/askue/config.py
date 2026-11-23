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
        if o.get("parent") and o["parent"] not in oids:
            raise ValueError("объект %s: нет родителя %s" % (o["id"], o["parent"]))
    if not re.match(r"^[A-Za-z_/+-]+$", cfg["timezone"]):
        raise ValueError("странное имя часового пояса")
    return cfg


def objects(cfg, with_total=True):
    return [o for o in cfg["objects"] if with_total or not o.get("total")]


def top_objects(cfg):
    """Объекты верхнего уровня без итога (Склад 1 ... Сторожка + компрессор) — их сумма равна всей базе."""
    return [o for o in cfg["objects"] if not o.get("total") and not o.get("parent")]


def parents(cfg):
    return {o["parent"] for o in cfg["objects"] if o.get("parent")}


def for_stack(objs, cfg):
    """Объекты для графиков «слоями»/столбиков без двойного счёта: без итога, а если выбраны родитель
    и его нитки — родитель заменяется нитками."""
    sel = {o["id"] for o in objs}
    kids = {o["parent"] for o in cfg["objects"] if o.get("parent") and o["id"] in sel}
    return [o for o in objs if not o.get("total") and o["id"] not in kids]


def db_dsn():
    return "host=%s dbname=%s user=%s password=%s" % (
        os.environ.get("POSTGRES_HOST", "db"),
        os.environ.get("POSTGRES_DB", "askue"),
        os.environ.get("POSTGRES_USER", "askue"),
        os.environ["POSTGRES_PASSWORD"],
    )
