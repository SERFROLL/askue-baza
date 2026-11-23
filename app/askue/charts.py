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


FAST_MAX_MIN = 30   # до 30 минут — 2-секундные данные, дольше — минутные средние


def _series(conn, oid, minutes, fast):
    view = "object_power_fast" if fast else "object_power_1m"
    with conn.cursor() as cur:
        cur.execute("SELECT ts, p FROM %s WHERE object_id = %%s AND ts > now() - make_interval(mins => %%s) "
                    "ORDER BY ts" % view, (oid, int(minutes)))
        rows = cur.fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def _kw(x):
    return ("%.1f" % x).replace(".", ",")


def _span(minutes):
    return "%d мин" % minutes if minutes < 60 else "%d ч" % (minutes // 60)


def _finish(fig, ax, cfg, minutes, title, t_first=None):
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(minutes=minutes)
    if t_first is not None and t_first > start:   # данных меньше, чем запрошено, — ось по данным
        start = t_first
    ax.set_xlim(start, now)
    loc = mdates.AutoDateLocator(tz=tz, minticks=4, maxticks=9)
    ax.xaxis.set_major_locator(loc)
    fmt = mdates.ConciseDateFormatter(loc, tz=tz, show_offset=False)
    fmt.formats = ["%Y", "%d.%m", "%d.%m", "%H:%M", "%H:%M", "%H:%M:%S"]
    fmt.zero_formats = ["", "%Y", "%d.%m", "%d.%m", "%H:%M", "%H:%M"]
    ax.xaxis.set_major_formatter(fmt)
    ax.set_ylabel("кВт")
    ax.set_ylim(bottom=0)
    ax.set_title(title, loc="left", fontsize=11, fontweight="bold")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    return buf.getvalue()


def chart_objects(conn, cfg, minutes=180):
    """Мощность всех объектов: до 6 ч — линии (до 30 мин по 2-секундным данным, дольше по минутным
    средним), больше 6 ч — слоями по минутным средним."""
    fast = minutes <= FAST_MAX_MIN
    objs = cfg_objects(cfg, with_total=False)
    fig, ax = plt.subplots(figsize=(8, 4.6))
    t_first = None
    if minutes <= 360:
        for n, o in enumerate(objs):
            t, p = _series(conn, o["id"], minutes, fast)
            if t:
                t_first = min(t_first or t[0], t[0])
                ax.plot(t, p, lw=1.4, color=_color(o, n), label="%s  %s кВт" % (o["name"], _kw(p[-1])))
        title = "Мощность объектов за %s (%s)" % (_span(minutes), "каждые 2 с" if fast else "средние за минуту")
    else:
        series = [(o, *_series(conn, o["id"], minutes, False)) for o in objs]
        times = sorted({x for _, t, _ in series for x in t})
        stacks = []
        for o, t, p in series:
            d = dict(zip(t, p))
            stacks.append([d.get(x, 0) or 0 for x in times])
        if times:
            t_first = times[0]
            ax.stackplot(times, stacks, colors=[_color(o, n) for n, o in enumerate(objs)], alpha=0.9,
                         labels=["%s  %s кВт" % (o["name"], _kw(s[-1])) for (o, _, _), s in zip(series, stacks)])
        title = "Мощность объектов за %s, слоями (сумма = вся база)" % _span(minutes)
    ax.legend(loc="upper left", fontsize=8, ncol=3, frameon=True, framealpha=0.85, edgecolor="none")
    return _finish(fig, ax, cfg, minutes, title, t_first)


def chart_object(conn, cfg, oid, minutes=1440, night_kw=None):
    o = next(x for x in cfg["objects"] if x["id"] == oid)
    t, p = _series(conn, oid, minutes, minutes <= FAST_MAX_MIN)
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
    return _finish(fig, ax, cfg, minutes, "%s за %s%s" % (o["name"], _span(minutes), now_txt), t[0] if t else None)
