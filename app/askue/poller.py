"""Опросчик счётчиков.

На каждый шлюз — свой поток и одно TCP-подключение (шлюз держит только одно):
  * каждые fast_interval_s — мощность P (total и по фазам) по всем точкам шлюза → power_fast;
  * в начале каждой минуты — полный набор (U, I, P, cos, энергия) → readings.
Время тиков выровнено по сетке, поэтому точки разных шлюзов получают одинаковые метки времени
(это нужно для объектов-разностей, например Склад 2 = линия на ВРУ − M3 − M4).
Главный поток пишет в базу, раз в минуту считает минутные средние и раз в час чистит старые быстрые данные.

Запуск в контейнере:   python -m askue.poller
Проверка без базы:     python -m askue.poller --once --dry-run --config config/config.yaml
"""
import argparse
import datetime as dt
import logging
import os
import queue
import threading
import time

from . import config as config_mod
from .modbus import Link, ModbusError
from .wbmap import read_channel, read_power, read_voltages

log = logging.getLogger("poller")
UTC = dt.timezone.utc


def _retry(fn, link, *args):
    """Одна повторная попытка, если шлюз оборвал соединение (например, к нему подключился кто-то ещё)."""
    try:
        return fn(link, *args)
    except OSError:
        link.close()
        return fn(link, *args)


def read_full(link, points):
    results, errors, volts = {}, {}, {}
    for p in points:
        try:
            if p["unit"] not in volts:
                volts[p["unit"]] = _retry(read_voltages, link, p["unit"])
            r = _retry(read_channel, link, p["unit"], p["channel"])
            r["u1"], r["u2"], r["u3"] = volts[p["unit"]]
            results[p["id"]] = r
        except (OSError, ModbusError) as e:
            errors[p["id"]] = "%s: %s" % (type(e).__name__, e)
    return results, errors


def gateway_worker(name, link, points, fast, q, stop):
    last_minute = None
    while not stop.is_set():
        tick = (int(time.time() // fast) + 1) * fast
        stop.wait(max(0.0, tick - time.time()))
        if stop.is_set():
            break
        ts = dt.datetime.fromtimestamp(tick, UTC)
        rows, errors = [], {}
        for p in points:
            try:
                pw = _retry(read_power, link, p["unit"], p["channel"])
                rows.append((ts, p["id"], pw[0], pw[1], pw[2], pw[3]))
            except (OSError, ModbusError) as e:
                errors[p["id"]] = "%s: %s" % (type(e).__name__, e)
        q.put(("fast", rows, errors))
        minute = int(tick // 60)
        if minute != last_minute:
            last_minute = minute
            mts = dt.datetime.fromtimestamp(minute * 60, UTC)
            results, ferr = read_full(link, points)
            q.put(("full", mts, results, ferr))


def make_links(cfg):
    src = os.environ.get("MODBUS_SOURCE_IP") or None
    return {
        name: Link(g["host"], int(g["port"]), float(cfg.get("modbus_timeout_s", 1.5)), src)
        for name, g in cfg["gateways"].items()
    }


def dry_run(cfg):
    links = make_links(cfg)
    ts = dt.datetime.now(UTC).replace(microsecond=0)
    for gw, link in links.items():
        pts = [p for p in cfg["points"] if p["gateway"] == gw]
        t0 = time.time()
        results, errors = read_full(link, pts)
        for pid, r in results.items():
            print("%s %-6s P=%7.3f кВт  I=%5.1f/%5.1f/%5.1f А  cos=%.2f  E+=%.2f E-=%.2f кВт·ч" % (
                ts.isoformat(), pid, r["p_total"], r["i1"], r["i2"], r["i3"], r["pf"], r["e_import"], r["e_export"]))
        for pid, e in errors.items():
            print("%s %-6s ОШИБКА %s" % (ts.isoformat(), pid, e))
        print("%s: полный набор за %.2f с" % (gw, time.time() - t0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--once", action="store_true", help="один полный проход и выход")
    ap.add_argument("--dry-run", action="store_true", help="не писать в базу, только напечатать")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config_mod.load(args.config)
    if args.dry_run or args.once:
        dry_run(cfg)
        return

    from . import db
    conn = db.connect()
    db.init(conn, cfg)
    fast = float(cfg.get("fast_interval_s", 2))
    q = queue.Queue(maxsize=10000)
    stop = threading.Event()
    for gw, link in make_links(cfg).items():
        pts = [p for p in cfg["points"] if p["gateway"] == gw]
        threading.Thread(target=gateway_worker, args=(gw, link, pts, fast, q, stop),
                         name="gw-" + gw, daemon=True).start()
    log.info("опрос запущен: мощность каждые %.0f с, полный набор раз в минуту", fast)

    last_err = {}
    rolled_minute = int(time.time() // 60)
    next_purge = time.time() + 60
    while True:
        try:
            item = q.get(timeout=1)
        except queue.Empty:
            item = None
        try:
            if item and item[0] == "fast":
                _, rows, errors = item
                if rows:
                    db.insert_fast(conn, rows)
                ok = {r[1] for r in rows}
            elif item and item[0] == "full":
                _, mts, results, errors = item
                for pid, r in results.items():
                    db.insert_reading(conn, mts, pid, r)
                ok = set(results)
            else:
                errors, ok = {}, set()
            # журнал ошибок: только смена состояния точки
            for pid, e in errors.items():
                if last_err.get(pid) != e:
                    db.insert_error(conn, pid, e)
                    log.warning("%s: %s", pid, e)
                    last_err[pid] = e
            for pid in ok:
                if pid in last_err:
                    log.info("%s: связь восстановлена", pid)
                    del last_err[pid]
            # минутные средние — через 5 с после окончания минуты
            m = int((time.time() - 5) // 60)
            while rolled_minute < m:
                db.rollup_minute(conn, dt.datetime.fromtimestamp(rolled_minute * 60, UTC))
                rolled_minute += 1
            if time.time() > next_purge:
                db.purge_fast(conn, cfg.get("fast_retention_days", 14))
                next_purge = time.time() + 3600
        except Exception as e:  # noqa: BLE001 — сбой базы не должен ронять опрос
            log.error("ошибка записи в базу: %s", e)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
            conn = db.connect()


if __name__ == "__main__":
    main()
