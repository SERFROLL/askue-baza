"""Telegram-бот АСКУЭ.

Переменные окружения:
  TELEGRAM_BOT_TOKEN   токен от @BotFather (обязательно)
  TELEGRAM_CHAT_IDS    id чатов администраторов через запятую: всё, включая энергию, утреннюю сводку и технику
  TELEGRAM_GUARD_IDS   id чатов охраны через запятую: мощность, сводка, вечером только «не выключенные»
  TELEGRAM_PROXY       необязательно: прокси для api.telegram.org
  TELEGRAM_API_BASE    необязательно: свой адрес-ретранслятор вместо https://api.telegram.org

Диалог (меню → объект(ы) → период → ответ с кнопками уточнения) — в dialog.py.
Бот пишет в журнал id каждого чата, который ему пишет, — так подключаются новые чаты.
"""
import datetime as dt
import json
import logging
import os
import time
from zoneinfo import ZoneInfo

import requests

from . import config as config_mod
from . import db, report
from .dialog import Dialog, View, home_kb

log = logging.getLogger("bot")

HELP = ("<b>Как пользоваться</b>\nНажмите «🏠 Меню» или напишите /menu.\n"
        "⚡ Мощность — выберите объект(ы) и период: сейчас, 15 мин, 3 ч, 12 ч, 24 ч.\n"
        "🔋 Энергия — объект(ы) и период; ответ числом, затем можно открыть график нарастающим итогом "
        "или по часам/дням. «Свой период» — напишите даты, например 01.11–15.11.\n"
        "🟢 Сводка сейчас — все объекты одним сообщением.\n\nКоманды: /menu, /power, /energy, /now, /report, /status, /id")


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

    def _multipart(self, method, data, png):
        r = requests.post(self._url(method), data=data, files={"photo": ("chart.png", png, "image/png")},
                          timeout=60, proxies=self.proxies)
        res = r.json()
        if not res.get("ok"):
            raise RuntimeError("Telegram %s: %s" % (method, res.get("description")))
        return res["result"]

    def send(self, chat_id, text, keyboard=None):
        parts = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
        msg = None
        for n, part in enumerate(parts):
            last = n == len(parts) - 1
            msg = self.call("sendMessage", chat_id=chat_id, text=part, parse_mode="HTML", disable_web_page_preview=True,
                            reply_markup=keyboard if last and keyboard else None)
        return msg

    def photo(self, chat_id, png, caption=None, keyboard=None):
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if keyboard:
            data["reply_markup"] = json.dumps(keyboard)
        return self._multipart("sendPhoto", data, png)

    def edit_text(self, chat_id, msg_id, text, keyboard=None):
        return self.call("editMessageText", chat_id=chat_id, message_id=msg_id, text=text[:4000], parse_mode="HTML",
                         disable_web_page_preview=True, reply_markup=keyboard)

    def edit_photo(self, chat_id, msg_id, png, caption=None, keyboard=None):
        media = {"type": "photo", "media": "attach://photo"}
        if caption:
            media["caption"] = caption
        data = {"chat_id": chat_id, "message_id": msg_id, "media": json.dumps(media)}
        if keyboard:
            data["reply_markup"] = json.dumps(keyboard)
        return self._multipart("editMessageMedia", data, png)

    def delete(self, chat_id, msg_id):
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=msg_id)
        except Exception:  # noqa: BLE001 — старое сообщение могло уже исчезнуть
            pass


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
        # открытый доступ: любой чат получает всё меню; рассылки — только в чаты из списков выше
        self.open = os.environ.get("TELEGRAM_OPEN_ACCESS", "").strip().lower() in ("1", "true", "yes", "да")
        self.conn = db.connect()
        self.dialog = Dialog(cfg)
        self.alert_sent = {}

    def allowed(self, chat_id):
        return self.open or str(chat_id) in self.admins + self.guards

    def is_admin(self, chat_id):
        return self.open or str(chat_id) in self.admins

    def broadcast(self, text, to, kb=None):
        for c in to:
            try:
                self.tg.send(c, text, kb)
            except Exception as e:  # noqa: BLE001 — один недоступный чат не должен ронять бота
                log.error("не удалось отправить в %s: %s", c, e)

    def render(self, chat_id, view, msg=None):
        """Показать экран: править текущее сообщение, если тип совпадает, иначе заменить новым."""
        if view is None:
            return
        cur_kind = None if msg is None else ("photo" if msg.get("photo") else "text")
        try:
            if cur_kind == view.kind == "text":
                return self.tg.edit_text(chat_id, msg["message_id"], view.text, view.kb)
            if cur_kind == view.kind == "photo":
                return self.tg.edit_photo(chat_id, msg["message_id"], view.png, view.text, view.kb)
        except RuntimeError as e:
            if "message is not modified" in str(e):
                return None
            log.warning("правка не удалась (%s), шлю новое сообщение", e)
        if msg is not None:
            self.tg.delete(chat_id, msg["message_id"])
        if view.kind == "photo":
            return self.tg.photo(chat_id, view.png, view.text, view.kb)
        return self.tg.send(chat_id, view.text, view.kb)

    # ----- входящие -----
    def handle(self, upd):
        if "callback_query" in upd:
            cq = upd["callback_query"]
            msg = cq["message"]
            chat_id = msg["chat"]["id"]
            if not self.allowed(chat_id):
                return
            try:
                self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:  # noqa: BLE001
                pass
            admin = self.is_admin(chat_id)
            self.render(chat_id, self.dialog.handle(self.conn, chat_id, cq.get("data", ""), admin), msg)
            return
        msg = upd.get("message") or upd.get("channel_post")
        if not msg or "text" not in msg:
            return
        chat = msg["chat"]
        chat_id = chat["id"]
        raw = msg["text"].strip()
        text = raw.lower().split("@")[0]
        log.info("сообщение из чата id=%s (%s %s): %s", chat_id, chat.get("type"),
                 chat.get("title") or chat.get("username") or chat.get("first_name") or "", text[:40])
        if text in ("/id", "id"):
            self.tg.send(chat_id, "id этого чата: <code>%s</code>" % chat_id)
            return
        if not self.allowed(chat_id):
            if text == "/start":
                self.tg.send(chat_id, "Бот АСКУЭ базы. Этот чат пока не подключён. id чата: <code>%s</code>" % chat_id)
            return
        admin = self.is_admin(chat_id)
        d, c = self.dialog, self.conn
        d.conn = c
        view = d.custom_period(chat_id, raw) if admin else None
        if view is None:
            if text in ("/start", "/menu", "меню"):
                view = d.menu(admin)
            elif text in ("/power", "мощность"):
                view = d.handle(c, chat_id, "P:0", admin)
            elif text in ("/energy", "энергия") and admin:
                view = d.handle(c, chat_id, "E:0", admin)
            elif text in ("/now", "сейчас"):
                view = d.handle(c, chat_id, "S", admin)
            elif text in ("/report", "отчёт", "отчет"):
                view = View("text", report.evening_report(c, self.cfg, title="Отчёт по запросу", full=admin), kb=home_kb())
            elif text == "/status" and admin:
                view = d.handle(c, chat_id, "C", admin)
            elif text in ("/help", "help", "помощь"):
                view = View("text", HELP, kb=home_kb())
        self.render(chat_id, view)

    # ----- расписание -----
    def scheduled(self):
        cfg, c = self.cfg, self.conn
        tz = ZoneInfo(cfg["timezone"])
        now = dt.datetime.now(tz)
        today = now.date().isoformat()
        evening = dt.time.fromisoformat(cfg["reports"]["evening_time"])
        morning = dt.time.fromisoformat(cfg["reports"].get("morning_time", "08:00"))
        if now.time() >= evening and _state_get(c, "evening_sent") != today:
            self.broadcast(report.evening_report(c, cfg), self.admins, home_kb())
            if self.guards:
                self.broadcast(report.evening_report(c, cfg, full=False), self.guards, home_kb())
            _state_set(c, "evening_sent", today)
        if now.time() >= morning and _state_get(c, "morning_sent") != today:
            self.broadcast(report.morning_report(c, cfg), self.admins, home_kb())
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
                               % (o["name"], kw, thr, al["sustain_min"]), self.admins + self.guards, home_kb())
                self.alert_sent[o["id"]] = time.time()

    def run(self):
        me = self.tg.call("getMe")
        log.info("бот @%s запущен: админов %d, охраны %d, открытый доступ: %s", me.get("username"),
                 len(self.admins), len(self.guards), "да" if self.open else "нет")
        try:
            self.tg.call("setMyCommands", commands=[
                {"command": "menu", "description": "Меню"},
                {"command": "power", "description": "Мощность: объект и период"},
                {"command": "energy", "description": "Энергия: объект и период"},
                {"command": "now", "description": "Сводка сейчас"},
                {"command": "report", "description": "Отчёт по объектам"},
                {"command": "status", "description": "Связь со счётчиками"},
                {"command": "help", "description": "Как пользоваться"}])
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
                        log.exception("ошибка обработки: %s", e)
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
