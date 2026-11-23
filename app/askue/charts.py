"""Графики-картинки (PNG) для Telegram. Цвет каждого объекта задан в config.yaml (objects[].color)."""
import datetime as dt
import io
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from .config import objects as cfg_objects  # noqa: E402

FALLBACK = ["#1f6fd1", "#ff8c00", "#9b4dca", "#2ca02c", "#e0202a", "#a86a32", "#555555"]
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.3})


def _color(o, n):
    return o.get("color") or FALLBACK[n % len(FALLBACK)]


def _series(conn, oid, hours, fast):
    view = "object_power_fast" if fast else "object_power_1m"
    with conn.cursor() as cur:
        cur.execute("SELECT ts, p FROM %s WHERE object_id = %%s AND ts > now() - make_interval(hours => %%s) "
                    "ORDER BY ts" % view, (oid, hours))
        rows = cur.fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def _kw(x):
    return ("%.1f" % x).replace(".", ",")


def _finish(fig, ax, cfg, hours, title):
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(dt.timezone.utc)
    ax.set_xlim(now - dt.timedelta(hours=hours), now)   # ось всегда на весь запрошенный период
    if hours <= 3:
        ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0, 30], tz=tz))
    elif hours <= 12:
        ax.xaxis.set_major_locator(mdates.HourLocator(tz=tz))
    else:
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=3, tz=tz))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    ax.set_ylabel("кВт")
    ax.set_ylim(bottom=0)
    ax.set_title(title, loc="left", fontsize=11, fontweight="bold")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    return buf.getvalue()


def chart_objects(conn, cfg, hours=3):
    """Мощность всех объектов: до 3 ч — линии по 2-секундным данным, дольше — слои по минутным средним."""
    fast = hours <= 3
    objs = cfg_objects(cfg, with_total=False)
    fig, ax = plt.subplots(figsize=(8, 4.6))
    if fast:
        for n, o in enumerate(objs):
            t, p = _series(conn, o["id"], hours, True)
            if t:
                ax.plot(t, p, lw=1.4, color=_color(o, n), label="%s  %s кВт" % (o["name"], _kw(p[-1])))
        title = "Мощность объектов за %d ч (каждые 2 с)" % hours
    else:
        series = [(o, *_series(conn, o["id"], hours, False)) for o in objs]
        times = sorted({x for _, t, _ in series for x in t})
        stacks = []
        for o, t, p in series:
            d = dict(zip(t, p))
            stacks.append([d.get(x, 0) or 0 for x in times])
        if times:
            ax.stackplot(times, stacks, colors=[_color(o, n) for n, o in enumerate(objs)], alpha=0.9,
                         labels=["%s  %s кВт" % (o["name"], _kw(s[-1])) for (o, _, _), s in zip(series, stacks)])
        title = "Мощность объектов за %d ч, слоями (сумма = вся база)" % hours
    ax.legend(loc="upper left", fontsize=8, ncol=3, frameon=True, framealpha=0.85, edgecolor="none")
    return _finish(fig, ax, cfg, hours, title)


def chart_object(conn, cfg, oid, hours=24, night_kw=None):
    o = next(x for x in cfg["objects"] if x["id"] == oid)
    fast = hours <= 3
    t, p = _series(conn, oid, hours, fast)
    col = _color(o, 0)
    fig, ax = plt.subplots(figsize=(8, 4.2))
    if t:
        ax.plot(t, p, lw=1.4, color=col)
        ax.fill_between(t, p, alpha=0.18, color=col)
    if o.get("night_max_kw") is not None:
        ax.axhline(o["night_max_kw"], color="#e0202a", ls="--", lw=1.2, label="ночной порог")
    if night_kw is not None:
        ax.axhline(night_kw, color="#555555", ls=":", lw=1.2, label="обычно ночью")
    if o.get("night_max_kw") is not None or night_kw is not None:
        ax.legend(loc="upper left", fontsize=8, frameon=False)
    now_txt = (" — сейчас %s кВт" % _kw(p[-1])) if p else ""
    return _finish(fig, ax, cfg, hours, "%s за %d ч%s" % (o["name"], hours, now_txt))
