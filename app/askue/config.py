import os
import yaml

CONFIG_PATH = os.environ.get("ASKUE_CONFIG", "/config/config.yaml")


def load(path=None):
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    ids = [p["id"] for p in cfg["points"]]
    if len(ids) != len(set(ids)):
        raise ValueError("в config.yaml повторяются id точек учёта")
    for p in cfg["points"]:
        if p["gateway"] not in cfg["gateways"]:
            raise ValueError("точка %s: нет шлюза %s" % (p["id"], p["gateway"]))
    return cfg


def db_dsn():
    return "host=%s dbname=%s user=%s password=%s" % (
        os.environ.get("POSTGRES_HOST", "db"),
        os.environ.get("POSTGRES_DB", "askue"),
        os.environ.get("POSTGRES_USER", "askue"),
        os.environ["POSTGRES_PASSWORD"],
    )
