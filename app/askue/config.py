import datetime as dt
import os
import re
from zoneinfo import ZoneInfo

import yaml

CONFIG_PATH = os.environ.get("ASKUE_CONFIG", "/config/config.yaml")
TERM = re.compile(r"^([+-])(.+)$")


def parse_terms(obj):
    """['+M1.4', '-M3'] -> [('M1.4', 1), ('M3', -1)]. Это ТЕКУЩИЙ состав объекта (без истории)."""
    out = []
    for t in obj.get("terms") or []:
        m = TERM.match(t.strip())
        if not m:
            raise ValueError("объект %s: неверный член %r (нужно +ID или -ID)" % (obj["id"], t))
        out.append((m.group(2), 1 if m.group(1) == "+" else -1))
    return out


def periods(obj, tzname):
    """Состав объекта по периодам: [(действует_с | None, действует_до | None, [(точка, знак)])] по времени.
    В config.yaml: history — составы, которые действовали ДО указанного местного времени, по возрастанию:
      history: [{until: "2026-11-25 12:05", terms: ["+M3", "+M4"]}]
    terms самого объекта действуют с последнего until и дальше. Пустой terms — объекта в этот период не было (0)."""
    tz = ZoneInfo(tzname)
    out, prev = [], None
    for h in obj.get("history") or []:
        until = dt.datetime.strptime(str(h["until"])[:16], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        if prev is not None and until <= prev:
            raise ValueError("объект %s: history должен идти по возрастанию until" % obj["id"])
        out.append((prev, until, parse_terms({"id": obj["id"], "terms": h.get("terms")})))
        prev = until
    out.append((prev, None, parse_terms(obj)))
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
    if not re.match(r"^[A-Za-z_/+-]+$", cfg["timezone"]):
        raise ValueError("странное имя часового пояса")
    for o in cfg["objects"]:
        for _, _, terms in periods(o, cfg["timezone"]):
            for pid, _ in terms:
                if pid not in ids:
                    raise ValueError("объект %s: нет точки %s" % (o["id"], pid))
        if not parse_terms(o):
            raise ValueError("объект %s: пустой текущий состав terms" % o["id"])
        if o.get("parent") and o["parent"] not in oids:
            raise ValueError("объект %s: нет родителя %s" % (o["id"], o["parent"]))
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
