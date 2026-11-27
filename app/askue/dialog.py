"""Пошаговый диалог в Telegram: Мощность и Энергия — объект(ы) → период → ответ с кнопками уточнения.

Состояние хранится прямо в callback_data (до 64 байт), объекты кодируются битовой маской
по порядку objects в config.yaml. Экраны возвращаются как View; бот сам решает, править сообщение или
прислать новое (текст нельзя превратить в картинку правкой).

Коды callback_data:
  m                      главное меню
  S / C                  сводка сейчас / связь со счётчиками
  P:<mask>               мощность: выбор объектов;  Pt:<mask>:<i> — отметить i;  Pa:<mask> — все объекты
  Pd:<mask>              мощность: выбор периода
  Pp:<mask>:<период>     мощность: ответ (период: now | 15 | 180 | 720 | 1440 минут)
  E:<mask>, Et, Ea, Ed   энергия: то же для объектов и периода
  Ep:<mask>:<код>        энергия: итог числом (код: today | yday | 7d | mon | pmon | cYYYYMMDD-YYYYMMDD | cust)
  Ev:<mask>:<код>:<вид>  энергия: график (вид: c — нарастающим, b — по часам/дням)
  W, Wp:<код>            рабочее / нерабочее время: выбор периода и отчёт по всем объектам (коды как у Ep)
"""
import datetime as dt
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from . import charts, query, report, worktime
from . import config as config_mod
from .report import _fmt, kwh_rub, tariff_note

POWER_PERIODS = [("now", "Сейчас"), ("15", "15 мин"), ("180", "3 ч"), ("720", "12 ч"), ("1440", "24 ч")]
ENERGY_PERIODS = [("today", "Сегодня"), ("yday", "Вчера"), ("7d", "7 дней"),
                  ("mon", "Этот месяц"), ("pmon", "Прошлый месяц"), ("cust", "Свой период")]
PERIOD_RE = re.compile(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s*[-–—]\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?")


@dataclass
class View:
    kind: str                      # "text" | "photo"
    text: str = ""                 # текст или подпись к картинке
    png: bytes = b""
    kb: dict = field(default_factory=dict)


def btn(text, data):
    return {"text": text, "callback_data": data}


def menu_kb(admin, subscribed=False):
    rows = [[btn("⚡ Мощность", "P:0"), btn("🔋 Энергия", "E:0")] if admin else [btn("⚡ Мощность", "P:0")],
            *([[btn("🌗 Рабочее / нерабочее время", "W")]] if admin else []),
            [btn("🟢 Сводка сейчас", "S")] + ([btn("🔌 Связь", "C")] if admin else []),
            [btn("🔕 Не получать вечерний отчёт" if subscribed else "🔔 Получать вечерний отчёт", "Sub")],
            [btn("ℹ️ О системе", "I")]]
    return {"inline_keyboard": rows}


SUB_SQL = "CREATE TABLE IF NOT EXISTS subscribers (chat_id text PRIMARY KEY, title text, since timestamptz NOT NULL DEFAULT now())"


def is_subscribed(conn, chat_id):
    conn.execute(SUB_SQL)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM subscribers WHERE chat_id = %s", (str(chat_id),))
        return cur.fetchone() is not None


def subscribers(conn):
    conn.execute(SUB_SQL)
    with conn.cursor() as cur:
        cur.execute("SELECT chat_id FROM subscribers ORDER BY since")
        return [r[0] for r in cur.fetchall()]


def toggle_subscription(conn, chat_id, title=""):
    """Подписать/отписать чат. Возвращает True, если теперь подписан."""
    if is_subscribed(conn, chat_id):
        conn.execute("DELETE FROM subscribers WHERE chat_id = %s", (str(chat_id),))
        return False
    conn.execute("INSERT INTO subscribers (chat_id, title) VALUES (%s, %s) ON CONFLICT DO NOTHING", (str(chat_id), title))
    return True


def info_text(cfg):
    """Краткая справка о системе — закрепляется в чате и показывается по кнопке «О системе»."""
    url = cfg.get("dashboard_url", "")
    names = []
    for o in cfg["objects"]:
        if o.get("total") or o.get("parent"):
            continue
        kids = [k["name"] for k in cfg["objects"] if k.get("parent") == o["id"]]
        names.append("%s (%s)" % (o["name"], ", ".join(kids)) if kids else o["name"])
    return ("<b>ℹ️ АСКУЭ базы — %s</b>\n"
            "Электроучёт: мощность и расход энергии по объектам — %s.\n\n"
            "📊 <b>Полная панель отчётности</b>: %s\n"
            "Открывается только из локальной сети базы и офиса: графики, история, ночное потребление, "
            "сверка с энергосбытом.\n\n"
            "🤖 <b>В боте</b>: «🏠 Меню» или /menu → ⚡ Мощность или 🔋 Энергия → объект → период.\n"
            "Мощность обновляется каждые 2 с. Вечерний отчёт — %s, утренняя сводка — %s."
            % (cfg["site"].split(", ", 1)[-1], ", ".join(names), url,
               cfg["reports"]["evening_time"], cfg["reports"].get("morning_time", "08:00")))


def home_kb():
    return {"inline_keyboard": [[btn("🏠 Меню", "m")]]}


class Dialog:
    def __init__(self, cfg):
        self.cfg = cfg
        self.objs = cfg["objects"]
        # «Все объекты» — верхний уровень без итога (нитки складов выбираются отдельно)
        self.all_mask = sum(1 << i for i, o in enumerate(self.objs) if not o.get("total") and not o.get("parent"))
        self.pending = {}  # chat_id -> mask (энергия) или "W" (рабочее/нерабочее): ждём текст «свой период»

    # ---------- общее ----------
    def selected(self, mask):
        return [o for i, o in enumerate(self.objs) if mask >> i & 1]

    def menu(self, admin, subscribed=False):
        return View("text", "<b>АСКУЭ базы</b>\nЧто показать?", kb=menu_kb(admin, subscribed))

    def pick(self, k, mask, title):
        rows, row = [], []
        for i, o in enumerate(self.objs):
            row.append(btn(("✅ " if mask >> i & 1 else "") + o.get("short", o["name"]), "%st:%d:%d" % (k, mask, i)))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        all_on = mask & self.all_mask == self.all_mask
        rows.append([btn(("✅ " if all_on else "") + "Все объекты", "%sa:%d" % (k, mask))])
        rows.append([btn("🏠 Меню", "m")] + ([btn("Далее ▶", "%sd:%d" % (k, mask))] if mask else []))
        names = ", ".join(o["name"] for o in self.selected(mask)) or "не выбраны"
        return View("text", "<b>%s</b>\nВыберите один или несколько объектов и нажмите «Далее».\n\nВыбрано: %s" % (title, names),
                    kb={"inline_keyboard": rows})

    def periods(self, k, mask, title, items):
        btns = [btn(label, "%sp:%d:%s" % (k, mask, code)) for code, label in items]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        rows.append([btn("◀ Объекты", "%s:%d" % (k, mask)), btn("🏠 Меню", "m")])
        names = ", ".join(o["name"] for o in self.selected(mask))
        return View("text", "<b>%s</b>: %s\nЗа какой период?" % (title, names), kb={"inline_keyboard": rows})

    # ---------- мощность ----------
    def power_result(self, mask, period):
        objs = self.selected(mask)
        back = [btn("◀ Объекты", "P:%d" % mask), btn("◀ Период", "Pd:%d" % mask)]
        refresh = btn("🔄 Обновить", "Pp:%d:%s" % (mask, period))
        kb = {"inline_keyboard": [back, [refresh, btn("🏠 Меню", "m")]]}
        tz = ZoneInfo(self.cfg["timezone"])
        if period == "now":
            data, last = query.power_now(self.conn, self.cfg, objs)
            spark = report.sparklines(self.conn)
            lines = ["<b>⚡ Мощность сейчас</b> — данные на %s" % (last.astimezone(tz).strftime("%H:%M:%S") if last else "—"), ""]
            for o in objs:
                d = data.get(o["id"])
                if not d:
                    lines.append("<b>%s</b>: нет данных" % o["name"])
                    continue
                ph = " · ".join("L%d %s" % (k + 1, _fmt(v, 1)) for k, v in enumerate(d["ph"]))
                lines.append("<b>%s</b>: %s  (%s)" % (o["name"], _fmt(d["p"], 1, "кВт"), ph))
                if d["min"] is not None:
                    lines.append("за минуту: от %s до %s" % (_fmt(d["min"], 1), _fmt(d["max"], 1, "кВт")))
                if spark.get(o["id"]):
                    lines.append("<code>%s</code>" % spark[o["id"]])
            lines.append("")
            lines.append("Мощность — среднее за 10 с; тренд — 2 часа, шкала у каждого объекта своя.")
            return View("text", "\n".join(lines), kb=kb)
        minutes = int(period)
        png, stats = charts.chart_power(self.conn, self.cfg, objs, minutes)
        now = query.power_now(self.conn, self.cfg, objs)[0]
        cap = []
        for o in objs:
            if o["id"] not in stats:
                cap.append("%s: нет данных" % o["name"])
                continue
            avg, peak = stats[o["id"]]
            s = "%s: сейчас %s, среднее %s" % (o["name"], _fmt(now.get(o["id"], {}).get("p"), 1), _fmt(avg, 1, "кВт"))
            if peak:
                s += ", макс %s в %s" % (_fmt(peak[0], 1, "кВт"), peak[1].astimezone(tz).strftime("%H:%M:%S"))
            cap.append(s)
        return View("photo", "\n".join(cap)[:1000], png=png, kb=kb)

    # ---------- энергия ----------
    def energy_result(self, mask, code, view=None):
        objs = self.selected(mask)
        start, end, label = query.energy_period(self.cfg, code)
        tz = ZoneInfo(self.cfg["timezone"])
        kb = {"inline_keyboard": [
            [btn("📈 Нарастающим", "Ev:%d:%s:c" % (mask, code)), btn("📊 По часам/дням", "Ev:%d:%s:b" % (mask, code))],
            [btn("🔢 Итог числом", "Ep:%d:%s" % (mask, code))],
            [btn("◀ Объекты", "E:%d" % mask), btn("◀ Период", "Ed:%d" % mask), btn("🏠 Меню", "m")]]}
        if view is None:
            tot, rd = query.energy_totals(self.conn, objs, start, end)
            if not rd:
                return View("text", "<b>🔋 Энергия, %s</b>\nЗа этот период данных нет (сбор идёт с 23.11.2026 15:09)." % label, kb=kb)
            t0 = min(v[1] for v in rd.values())
            t1 = max(v[3] for v in rd.values())
            lines = ["<b>🔋 Энергия, %s</b>" % label,
                     "Данные с %s по %s" % (t0.astimezone(tz).strftime("%d.%m %H:%M"), t1.astimezone(tz).strftime("%d.%m %H:%M")), ""]
            for o in objs:
                lines.append(("<b>%s</b>: %s" if o.get("total") else "%s: %s") % (o["name"], kwh_rub(tot.get(o["id"]), self.cfg)))
            parts = [o for o in config_mod.for_stack(objs, self.cfg) if o["id"] in tot]  # без двойного счёта ниток
            if len(parts) > 1:
                lines.append("Сумма выбранных: %s" % kwh_rub(sum(tot[o["id"]] for o in parts), self.cfg))
            lines.append("")
            lines.append("<b>Показания счётчиков энергии, кВт·ч</b> (начало → конец):")
            names = {p["id"]: p["name"] for p in self.cfg["points"]}
            for pid, (e0, _, e1, _) in rd.items():
                lines.append("%s: %s → %s" % (names.get(pid, pid), _fmt(e0, 1), _fmt(e1, 1)))
            if tariff_note(self.cfg):
                lines += ["", tariff_note(self.cfg)]
            return View("text", "\n".join(lines), kb=kb)
        span_days = (end - start).total_seconds() / 86400
        if view == "c":
            step, step_min = ("10 minutes", 10) if span_days <= 2 else ("1 hour", 60)
        else:
            step, step_min = ("1 hour", 60) if span_days <= 2 else ("1 day", 1440)
        buckets, series = query.energy_series(self.conn, self.cfg, objs, start, end, step)
        if not buckets:
            return View("text", "<b>🔋 Энергия, %s</b>\nЗа этот период данных нет." % label, kb=kb)
        what = "нарастающим итогом" if view == "c" else ("по часам" if step_min == 60 else "по дням")
        png = charts.chart_energy(self.cfg, objs, buckets, series, step_min, "cum" if view == "c" else "bars",
                                  "Энергия, %s — %s" % (label, what))
        cap = "; ".join("%s %s" % (o["name"], kwh_rub(series[o["id"]][-1], self.cfg)) for o in objs if o["id"] in series)
        return View("photo", ("Итого за период: " + cap)[:1000], png=png, kb=kb)

    def work_result(self, code):
        start, end, label = query.energy_period(self.cfg, code)
        kb = {"inline_keyboard": [[btn("🔄 Обновить", "Wp:" + code), btn("◀ Период", "W"), btn("🏠 Меню", "m")]]}
        return View("text", worktime.text(self.conn, self.cfg, start, end, label), kb=kb)

    def custom_period(self, chat_id, text):
        """Текст «01.11–15.11» после кнопки «Свой период». Возвращает View или None, если ничего не ждали."""
        mask = self.pending.pop(chat_id, None)
        if mask is None:
            return None
        m = PERIOD_RE.search(text)
        if not m:
            self.pending[chat_id] = mask
            return View("text", "Не понял период. Напишите, например: <code>01.11–15.11</code> или "
                                "<code>01.11.2026–30.11.2026</code>.", kb=home_kb())
        year = dt.datetime.now(ZoneInfo(self.cfg["timezone"])).year
        def mk(d, mo, y):
            y = int(y) if y else year
            return dt.date(y + 2000 if y < 100 else y, int(mo), int(d))
        try:
            d1, d2 = mk(*m.group(1, 2, 3)), mk(*m.group(4, 5, 6))
        except ValueError:
            self.pending[chat_id] = mask
            return View("text", "Такой даты нет. Напишите период ещё раз, например: <code>01.11–15.11</code>.", kb=home_kb())
        if d2 < d1:
            d1, d2 = d2, d1
        if mask == "W":
            return self.work_result("c%s-%s" % (d1.strftime("%Y%m%d"), d2.strftime("%Y%m%d")))
        return self.energy_result(mask, "c%s-%s" % (d1.strftime("%Y%m%d"), d2.strftime("%Y%m%d")))

    # ---------- разбор нажатия ----------
    def handle(self, conn, chat_id, data, admin, title=""):
        """Возвращает View для нажатой кнопки (или None, если кнопка недоступна этому чату)."""
        self.conn = conn
        parts = data.split(":")
        k = parts[0]
        mask = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if k == "m":
            return self.menu(admin, is_subscribed(conn, chat_id))
        if k == "Sub":
            on = toggle_subscription(conn, chat_id, title)
            ev = self.cfg["reports"]["evening_time"]
            text = ("🔔 <b>Подписка включена.</b>\nКаждый день в %s сюда придёт вечерний отчёт по объектам, а ночью — "
                    "предупреждения, если объект не выключен. Отказаться можно этой же кнопкой в меню." % ev
                    if on else "🔕 <b>Подписка выключена.</b>\nВечерний отчёт и ночные предупреждения больше приходить не будут. "
                               "Данные по-прежнему доступны по кнопкам меню.")
            return View("text", text + "\n\nЧто показать?", kb=menu_kb(admin, on))
        if k == "S":
            return View("text", report.now_report(conn, self.cfg), kb=home_kb())
        if k == "I":
            return View("text", info_text(self.cfg), kb=home_kb())
        if k == "C" and admin:
            return View("text", report.status_report(conn, self.cfg), kb=home_kb())
        if k[0] in ("E", "W") and not admin:
            return None
        if k in ("P", "E"):
            return self.pick(k, mask, "⚡ Мощность" if k == "P" else "🔋 Энергия")
        if k in ("Pt", "Et"):
            return self.pick(k[0], mask ^ (1 << int(parts[2])), "⚡ Мощность" if k[0] == "P" else "🔋 Энергия")
        if k in ("Pa", "Ea"):
            new = mask & ~self.all_mask if mask & self.all_mask == self.all_mask else mask | self.all_mask
            return self.pick(k[0], new, "⚡ Мощность" if k[0] == "P" else "🔋 Энергия")
        if k == "Pd":
            return self.periods("P", mask, "⚡ Мощность", POWER_PERIODS)
        if k == "Ed":
            return self.periods("E", mask, "🔋 Энергия", ENERGY_PERIODS)
        if k == "Pp":
            return self.power_result(mask, parts[2])
        if k == "Ep":
            if parts[2] == "cust":
                self.pending[chat_id] = mask
                return View("text", "<b>🔋 Энергия — свой период</b>\nНапишите даты сообщением, например: "
                                    "<code>01.11–15.11</code> или <code>01.11.2026–30.11.2026</code>.", kb=home_kb())
            return self.energy_result(mask, parts[2])
        if k == "Ev":
            return self.energy_result(mask, parts[2], parts[3])
        if k == "W":
            rows = [[btn(label, "Wp:" + code) for code, label in ENERGY_PERIODS[i:i + 3]] for i in (0, 3)]
            rows.append([btn("🏠 Меню", "m")])
            return View("text", "<b>🌗 Рабочее и нерабочее время</b>\nЭнергия по базе и по каждому объекту: рабочее время — "
                                "будни по производственному календарю РФ с 08:00 до 19:00, нерабочее — с 19:00 до 08:00, "
                                "выходные и праздники.\n\nЗа какой период?", kb={"inline_keyboard": rows})
        if k == "Wp":
            if parts[1] == "cust":
                self.pending[chat_id] = "W"
                return View("text", "<b>🌗 Рабочее и нерабочее — свой период</b>\nНапишите даты сообщением, например: "
                                    "<code>01.11–15.11</code> или <code>01.11.2026–30.11.2026</code>.", kb=home_kb())
            return self.work_result(parts[1])
        # старые кнопки из прежних версий
        if k == "now":
            return View("text", report.now_report(conn, self.cfg), kb=home_kb())
        if k in ("chart", "objs"):
            return self.pick("P", self.all_mask, "⚡ Мощность")
        if k == "energy" and admin:
            return self.pick("E", 0, "🔋 Энергия")
        if k == "status" and admin:
            return View("text", report.status_report(conn, self.cfg), kb=home_kb())
        return self.menu(admin)
