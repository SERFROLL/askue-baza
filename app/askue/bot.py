"""Telegram-бот АСКУЭ.

Переменные окружения:
  TELEGRAM_BOT_TOKEN   токен от @BotFather (обязательно)
  TELEGRAM_CHAT_IDS    id чатов администраторов через запятую: полные отчёты, утренняя сводка, техника
  TELEGRAM_GUARD_IDS   id чатов охраны через запятую: вечером только «не выключенные», ночные предупреждения
  TELEGRAM_PROXY       необязательно: прокси для api.telegram.org
  TELEGRAM_API_BASE    необязательно: свой адрес-ретранслятор вместо https://api.telegram.org

Кнопки под сообщениями: «Сейчас», «График 3 ч», «Сутки», «По объекту», «Расход» (админы), «Связь» (админы).
Бот пишет в журнал id каждого чата, который ему пишет, — так подключаются новые чаты.
"""
import datetime as dt
import json
import logging
import os
import time
from zoneinfo import ZoneInfo

import requests

from . import charts, report
from . import config as config_mod
from . import db

log = logging.getLogger("bot")

HELP = ("Кнопки под сообщениями:\n«Сейчас» — мощность объектов и тренд за 2 часа\n«График 3 ч», «Сутки» — картинка\n"
        "«По объекту» — график одного объекта за сутки\n«Расход» — энергия за месяц для сверки с энергосбытом\n"
        "«Связь» — состояние счётчиков\n\nКоманды: /now, /report, /chart, /energy, /status, /id")


class Telegram:
    def __init__(self):
        # docker compose передаёт незаданную переменную как пустую строку
        self.base = (os.environ.get("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/")
        self.token = os.environ["TELEGRAM_BOT_TOKEN"]
        proxy = os.environ.get("TELEGRAM_PROXY")
        self.proxies = {"https": proxy, "http": proxy} if proxy else None

    def _url(self, method):
        return "%s/bot%s/%s" % (self.base, self.token, method)

    def call(self, method, http_timeout=30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        r = requests.post(self._url(method), json=params, timeout=http_timeout, proxies=self.proxies)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram %s: %s" % (method, data.get("description")))
        return data["result"]

    def send(self, chat_id, text, keyboard=None):
        parts = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
        for n, part in enumerate(parts):
            last = n == len(parts) - 1
            self.call("sendMessage", chat_id=chat_id, text=part, parse_mode="HTML", disable_web_page_preview=True,
                      reply_markup=keyboard if last and keyboard else None)

    def photo(self, chat_id, png, caption=None, keyboard=None):
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if keyboard:
            data["reply_markup"] = json.dumps(keyboard)
        r = requests.post(self._url("sendPhoto"), data=data, files={"photo": ("chart.png", png, "image/png")},
                          timeout=60, proxies=self.proxies)
        if not r.json().get("ok"):
            raise RuntimeError("Telegram sendPhoto: %s" % r.json().get("description"))


def keyboard(admin):
    rows = [[{"text": "Сейчас", "callback_data": "now"}, {"text": "График 3 ч", "callback_data": "chart:3"},
             {"text": "Сутки", "callback_data": "chart:24"}],
            [{"text": "По объекту", "callback_data": "objs"}]]
    if admin:
        rows[1] += [{"text": "Расход", "callback_data": "energy"}, {"text": "Связь", "callback_data": "status"}]
    return {"inline_keyboard": rows}


def objects_keyboard(cfg):
    btns = [{"text": o["name"], "callback_data": "obj:" + o["id"]} for o in cfg["objects"]]
    return {"inline_keyboard": [btns[i:i + 3] for i in range(0, len(btns), 3)]}


def _ids(var):
    return [c.strip() for c in os.environ.get(var, "").split(",") if c.strip()]


def _state_get(conn, key):
    conn.execute("CREATE TABLE IF NOT EXISTS bot_state (key text PRIMARY KEY, value text)")
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM bot_state WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None


def _state_set(conn, key, value):
    conn.execute("INSERT INTO bot_state (key, value) VALUES (%s, %s) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))


def _in_window(now, window):
    start = dt.time.fromisoformat(window[0])
    end = dt.time.fromisoformat(window[1])
    t = now.time()
    return (start <= t or t < end) if start > end else (start <= t < end)


class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tg = Telegram()
        self.admins = _ids("TELEGRAM_CHAT_IDS")
        self.guards = _ids("TELEGRAM_GUARD_IDS")
        self.conn = db.connect()
        self.alert_sent = {}

    def broadcast(self, text, to, kb=None):
        for c in to:
            try:
                self.tg.send(c, text, kb)
            except Exception as e:  # noqa: BLE001 — один недоступный чат не должен ронять бота
                log.error("не удалось отправить в %s: %s", c, e)

    # ----- действия -----
    def act(self, chat_id, action):
        admin = str(chat_id) in self.admins
        c, cfg = self.conn, self.cfg
        if action in ("now", "/now", "/start", "сейчас"):
            self.tg.send(chat_id, report.now_report(c, cfg), keyboard(admin))
        elif action in ("report", "/report", "отчёт", "отчет"):
            self.tg.send(chat_id, report.evening_report(c, cfg, title="Отчёт по запросу"), keyboard(admin))
        elif action.startswith("chart:") or action == "/chart":
            hours = int(action.split(":")[1]) if ":" in action else 3
            self.tg.photo(chat_id, charts.chart_objects(c, cfg, hours), keyboard=keyboard(admin))
        elif action == "objs":
            self.tg.send(chat_id, "Выберите объект:", objects_keyboard(cfg))
        elif action.startswith("obj:"):
            oid = action[4:]
            if any(o["id"] == oid for o in cfg["objects"]):
                night = report.typical_night(c, cfg).get(oid)
                self.tg.photo(chat_id, charts.chart_object(c, cfg, oid, 24, night), keyboard=keyboard(admin))
        elif action in ("energy", "/energy") and admin:
            self.tg.send(chat_id, report.energy_report(c, cfg), keyboard(admin))
        elif action in ("status", "/status") and admin:
            self.tg.send(chat_id, report.status_report(c, cfg), keyboard(admin))
        elif action in ("/help", "help"):
            self.tg.send(chat_id, HELP, keyboard(admin))

    def handle(self, upd):
        if "callback_query" in upd:
            cq = upd["callback_query"]
            chat = cq["message"]["chat"]
            try:
                self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:  # noqa: BLE001
                pass
            if str(chat["id"]) in self.admins + self.guards:
                self.act(chat["id"], cq.get("data", ""))
            return
        msg = upd.get("message") or upd.get("channel_post")
        if not msg or "text" not in msg:
            return
        chat = msg["chat"]
        chat_id = chat["id"]
        text = msg["text"].strip().lower().split("@")[0]
        log.info("сообщение из чата id=%s (%s %s): %s", chat_id, chat.get("type"),
                 chat.get("title") or chat.get("username") or chat.get("first_name") or "", text[:40])
        if text in ("/id", "id"):
            self.tg.send(chat_id, "id этого чата: <code>%s</code>" % chat_id)
            return
        if str(chat_id) not in self.admins + self.guards:
            if text == "/start":
                self.tg.send(chat_id, "Бот АСКУЭ базы. Этот чат пока не подключён. id чата: <code>%s</code>" % chat_id)
            return
        self.act(chat_id, text)

    # ----- расписание -----
    def scheduled(self):
        cfg, c = self.cfg, self.conn
        tz = ZoneInfo(cfg["timezone"])
        now = dt.datetime.now(tz)
        today = now.date().isoformat()
        evening = dt.time.fromisoformat(cfg["reports"]["evening_time"])
        morning = dt.time.fromisoformat(cfg["reports"].get("morning_time", "08:00"))
        if now.time() >= evening and _state_get(c, "evening_sent") != today:
            self.broadcast(report.evening_report(c, cfg), self.admins, keyboard(True))
            if self.guards:
                self.broadcast(report.evening_report(c, cfg, full=False), self.guards, keyboard(False))
            _state_set(c, "evening_sent", today)
        if now.time() >= morning and _state_get(c, "morning_sent") != today:
            self.broadcast(report.morning_report(c, cfg), self.admins, keyboard(True))
            _state_set(c, "morning_sent", today)
        self.alerts(now)

    def alerts(self, now):
        cfg, c, al = self.cfg, self.conn, self.cfg["alerts"]
        if not al.get("enabled"):
            return
        names = {p["id"]: p["name"] for p in cfg["points"]}
        # 1) нет связи — админам, в любое время, при смене состояния
        seen = report.last_seen(c)
        utcnow = dt.datetime.now(dt.timezone.utc)
        for pid, name in names.items():
            t = seen.get(pid)
            off = t is None or (utcnow - t).total_seconds() > al["offline_min"] * 60
            key = "offline:" + pid
            if off and _state_get(c, key) != "1":
                self.broadcast("🔴 Нет данных от «%s» больше %d мин." % (name, al["offline_min"]), self.admins)
                _state_set(c, key, "1")
            elif not off and _state_get(c, key) == "1":
                self.broadcast("🟢 Данные от «%s» снова поступают." % name, self.admins)
                _state_set(c, key, "0")
        # 2) рост энергии отдачи — ошибка подключения ТТ; админам, не чаще раза в сутки по точке
        for pid, kwh in report.export_growth(c, 60).items():
            key = "export:" + pid
            if kwh > al["export_kwh_per_hour"] and _state_get(c, key) != now.date().isoformat():
                self.broadcast("🟠 «%s»: за час выросла энергия отдачи на %.2f кВт·ч. Похоже на ошибку подключения ТТ."
                               % (names.get(pid, pid), kwh), self.admins)
                _state_set(c, key, now.date().isoformat())
        # 3) объект выше ночного порога — всем, только в окне наблюдения
        if not _in_window(now, al["window"]):
            return
        avg = report.avg_power(c, al["sustain_min"])
        for o in config_mod.objects(cfg, with_total=False):
            thr = o.get("night_max_kw")
            kw = avg.get(o["id"])
            if thr is None or kw is None or kw <= thr:
                continue
            last = self.alert_sent.get(o["id"])
            if last is None or time.time() - last > al["repeat_min"] * 60:
                self.broadcast("🔴 «%s» потребляет %.1f кВт (порог %.1f кВт) уже %d мин. Похоже, не выключен."
                               % (o["name"], kw, thr, al["sustain_min"]), self.admins + self.guards, keyboard(False))
                self.alert_sent[o["id"]] = time.time()

    def run(self):
        me = self.tg.call("getMe")
        log.info("бот @%s запущен: админов %d, охраны %d", me.get("username"), len(self.admins), len(self.guards))
        try:
            self.tg.call("setMyCommands", commands=[
                {"command": "now", "description": "Мощность объектов сейчас"},
                {"command": "report", "description": "Отчёт по объектам"},
                {"command": "chart", "description": "График за 3 часа"},
                {"command": "energy", "description": "Расход за месяц"},
                {"command": "status", "description": "Связь со счётчиками"},
                {"command": "id", "description": "id этого чата"}])
        except Exception as e:  # noqa: BLE001
            log.warning("setMyCommands: %s", e)
        offset, next_check = None, 0.0
        while True:
            try:
                # длинный опрос Telegram 25 с; ждём ответа HTTP чуть дольше
                for upd in self.tg.call("getUpdates", http_timeout=40, offset=offset, timeout=25,
                                        allowed_updates=["message", "callback_query", "channel_post"]):
                    offset = upd["update_id"] + 1
                    try:
                        self.handle(upd)
                    except Exception as e:  # noqa: BLE001
                        log.error("ошибка обработки: %s", e)
            except Exception as e:  # noqa: BLE001
                log.error("ошибка опроса Telegram: %s", e)
                time.sleep(10)
            if time.time() >= next_check:
                next_check = time.time() + 60
                try:
                    self.scheduled()
                except Exception as e:  # noqa: BLE001
                    log.error("ошибка расписания: %s", e)
                    try:
                        self.conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.conn = db.connect()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Bot(config_mod.load()).run()


if __name__ == "__main__":
    main()
