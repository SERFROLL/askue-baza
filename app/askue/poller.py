"""Опросчик: раз в poll_interval_s читает все точки учёта и пишет показания в базу.

Запуск в контейнере:   python -m askue.poller
Проверка без базы:     python -m askue.poller --once --dry-run --config config/config.yaml
"""
import argparse
import datetime as dt
import logging
import os
import time

from . import config as config_mod
from .modbus import Link, ModbusError
from .wbmap import read_channel, read_voltages

log = logging.getLogger("poller")


def poll_once(cfg, links):
    """Один проход по всем точкам. Возвращает (ts, {point_id: reading}, {point_id: error})."""
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    results, errors = {}, {}
    voltages = {}
    for p in cfg["points"]:
        link = links[p["gateway"]]
        key = (p["gateway"], p["unit"])
        try:
            if key not in voltages:
                voltages[key] = read_voltages(link, p["unit"])
            r = read_channel(link, p["unit"], p["channel"])
            r["u1"], r["u2"], r["u3"] = voltages[key]
            results[p["id"]] = r
        except (OSError, ModbusError) as e:
            errors[p["id"]] = "%s: %s" % (type(e).__name__, e)
    return ts, results, errors


def make_links(cfg):
    src = os.environ.get("MODBUS_SOURCE_IP") or None
    return {
        name: Link(g["host"], int(g["port"]), float(cfg.get("modbus_timeout_s", 3)), src)
        for name, g in cfg["gateways"].items()
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    ap.add_argument("--dry-run", action="store_true", help="не писать в базу, только напечатать")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = config_mod.load(args.config)
    links = make_links(cfg)
    conn = None
    if not args.dry_run:
        from . import db
        conn = db.connect()
        db.init(conn, cfg)

    interval = int(cfg.get("poll_interval_s", 60))
    last_err = {}
    while True:
        ts, results, errors = poll_once(cfg, links)
        if args.dry_run:
            for pid, r in results.items():
                print("%s %-6s P=%7.3f кВт  I=%5.1f/%5.1f/%5.1f А  cos=%.2f  E+=%.2f E-=%.2f кВт·ч" % (
                    ts.isoformat(), pid, r["p_total"], r["i1"], r["i2"], r["i3"], r["pf"], r["e_import"], r["e_export"]))
            for pid, e in errors.items():
                print("%s %-6s ОШИБКА %s" % (ts.isoformat(), pid, e))
        else:
            from . import db
            for pid, r in results.items():
                db.insert_reading(conn, ts, pid, r)
            for pid, e in errors.items():
                # в журнал ошибок пишем только смену состояния, чтобы не засорять базу
                if last_err.get(pid) != e:
                    db.insert_error(conn, pid, e)
                    log.warning("%s: %s", pid, e)
            for pid in results:
                if pid in last_err:
                    log.info("%s: связь восстановлена", pid)
        last_err = errors
        if args.once:
            break
        time.sleep(max(1.0, interval - (time.time() % interval)))


if __name__ == "__main__":
    main()
