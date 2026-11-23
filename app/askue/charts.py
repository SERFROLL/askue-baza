"""Графики-картинки (PNG) для Telegram."""
import io
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from .config import objects as cfg_objects  # noqa: E402

COLORS = ["#2a78d6", "#e8a33d", "#8e5cd9", "#2fa35b", "#d6453d", "#4fb8c9", "#9c7a4f"]
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.3})


def _series(conn, oid, hours, fast):
    view = "object_power_fast" if fast else "object_power_1m"
    with conn.cursor() as cur:
        cur.execute("SELECT ts, p FROM %s WHERE object_id = %%s AND ts > now() - make_interval(hours => %%s) "
                    "ORDER BY ts" % view, (oid, hours))
        rows = cur.fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def _finish(fig, ax, cfg, hours, title):
    tz = ZoneInfo(cfg["timezone"])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    ax.set_ylabel("кВт")
    ax.set_ylim(bottom=0)
    ax.set_title(title, loc="left", fontsize=11, fontweight="bold")
    fig.autofmt_xdate(rotation=0, ha="center")
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
                ax.plot(t, p, lw=1.2, color=COLORS[n % len(COLORS)], label="%s  %s кВт" % (o["name"], ("%.1f" % p[-1]).replace(".", ",")))
        title = "Мощность объектов за %d ч (каждые 2 с)" % hours
    else:
        series = [(o, *_series(conn, o["id"], hours, False)) for o in objs]
        times = sorted({x for _, t, _ in series for x in t})
        stacks = []
        for o, t, p in series:
            d = dict(zip(t, p))
            stacks.append([d.get(x, 0) or 0 for x in times])
        if times:
            ax.stackplot(times, stacks, colors=COLORS[:len(objs)], alpha=0.85,
                         labels=[o["name"] for o, _, _ in series])
        title = "Мощность объектов за %d ч, слоями (сумма = вся база)" % hours
    ax.legend(loc="upper left", fontsize=8, ncol=2, frameon=False)
    return _finish(fig, ax, cfg, hours, title)


def chart_object(conn, cfg, oid, hours=24, night_kw=None):
    o = next(x for x in cfg["objects"] if x["id"] == oid)
    fast = hours <= 3
    t, p = _series(conn, oid, hours, fast)
    fig, ax = plt.subplots(figsize=(8, 4.2))
    if t:
        ax.plot(t, p, lw=1.3, color=COLORS[0])
        ax.fill_between(t, p, alpha=0.15, color=COLORS[0])
    if o.get("night_max_kw") is not None:
        ax.axhline(o["night_max_kw"], color="#d6453d", ls="--", lw=1.2, label="ночной порог")
    if night_kw is not None:
        ax.axhline(night_kw, color="#888888", ls=":", lw=1.2, label="обычно ночью")
    if o.get("night_max_kw") is not None or night_kw is not None:
        ax.legend(loc="upper left", fontsize=8, frameon=False)
    now_txt = (" — сейчас %s кВт" % ("%.1f" % p[-1]).replace(".", ",")) if p else ""
    return _finish(fig, ax, cfg, hours, "%s за %d ч%s" % (o["name"], hours, now_txt))
