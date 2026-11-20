"""Telegram-бот: вечерний отчёт по расписанию, отчёт по запросу, предупреждения ночью.

Переменные окружения:
  TELEGRAM_BOT_TOKEN   токен от @BotFather (обязательно)
  TELEGRAM_CHAT_IDS    id чатов через запятую, куда слать отчёты и кому отвечать
  TELEGRAM_PROXY       необязательно: прокси для api.telegram.org, например socks5h://host:1080
  TELEGRAM_API_BASE    необязательно: свой адрес-ретранслятор вместо https://api.telegram.org

Команды: /report — отчёт сейчас, /status — связь со счётчиками, /id — показать id чата, /help.
Пока TELEGRAM_CHAT_IDS пуст, бот отвечает только на /id: так узнаётся id группы охраны.
"""
import datetime as dt
import logging
import os
import time
from zoneinfo import ZoneInfo

import requests

from . import config as config_mod
from . import db, report

log = logging.getLogger("bot")

HELP = ("Команды:\n/report — отчёт по объектам сейчас\n/status — связь со счётчиками\n"
        "/id — id этого чата\nМожно написать просто «отчёт».")


class Telegram:
    def __init__(self):
        self.base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
        self.token = os.environ["TELEGRAM_BOT_TOKEN"]
        proxy = os.environ.get("TELEGRAM_PROXY")
        self.proxies = {"https": proxy, "http": proxy} if proxy else None

    def call(self, method, http_timeout=30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        r = requests.post("%s/bot%s/%s" % (self.base, self.token, method), json=params,
                          timeout=http_timeout, proxies=self.proxies)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram %s: %s" % (method, data.get("description")))
        return data["result"]

    def send(self, chat_id, text):
        # Telegram ограничивает сообщение 4096 символами
        for i in range(0, len(text), 4000):
            self.call("sendMessage", chat_id=chat_id, text=text[i:i + 4000], parse_mode="HTML",
                      disable_web_page_preview=True)


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


def check_alerts(conn, cfg, tg, chats, sent):
    al = cfg["alerts"]
    if not al.get("enabled"):
        return
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(tz)
    names = {p["id"]: p["name"] for p in cfg["points"]}

    # 1) нет связи — в любое время, только при смене состояния
    seen = report.last_seen(conn)
    utcnow = dt.datetime.now(dt.timezone.utc)
    for pid, name in names.items():
        t = seen.get(pid)
        offline = t is None or (utcnow - t).total_seconds() > al["offline_min"] * 60
        key = "offline:" + pid
        if offline and _state_get(conn, key) != "1":
            _broadcast(tg, chats, "🔴 Нет данных от точки «%s» больше %d мин." % (name, al["offline_min"]))
            _state_set(conn, key, "1")
        elif not offline and _state_get(conn, key) == "1":
            _broadcast(tg, chats, "🟢 Данные от точки «%s» снова поступают." % name)
            _state_set(conn, key, "0")

    # 2) превышение ночного порога — только в окне наблюдения
    if not _in_window(now, al["window"]):
        return
    avg = report.current_power(conn, al["sustain_min"])
    for p in cfg["points"]:
        thr = p.get("night_max_kw")
        if thr is None or p["id"] not in avg:
            continue
        kw = avg[p["id"]][0]
        last = sent.get(p["id"])
        if kw is not None and kw > thr and (last is None or (time.time() - last) > al["repeat_min"] * 60):
            _broadcast(tg, chats, "🔴 «%s» потребляет %.1f кВт (порог %.1f кВт) уже %d мин. Похоже, не выключен." % (
                p["name"], kw, thr, al["sustain_min"]))
            sent[p["id"]] = time.time()


def _broadcast(tg, chats, text):
    for c in chats:
        try:
            tg.send(c, text)
        except Exception as e:  # noqa: BLE001 — один недоступный чат не должен ронять бота
            log.error("не удалось отправить в %s: %s", c, e)


def handle_update(upd, conn, cfg, tg, chats):
    msg = upd.get("message") or upd.get("channel_post")
    if not msg or "text" not in msg:
        return
    chat_id = msg["chat"]["id"]
    text = msg["text"].strip().lower().split("@")[0]
    if text in ("/id", "id"):
        tg.send(chat_id, "id этого чата: <code>%s</code>" % chat_id)
        return
    if str(chat_id) not in chats:
        return  # чужим чатам ничего не показываем
    if text in ("/report", "отчёт", "отчет", "/start"):
        tg.send(chat_id, report.evening_report(conn, cfg, title="Отчёт по запросу"))
    elif text == "/status":
        tg.send(chat_id, report.status_report(conn, cfg))
    elif text == "/help":
        tg.send(chat_id, HELP)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config_mod.load()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]
    tg = Telegram()
    me = tg.call("getMe")
    log.info("бот @%s запущен, чатов для отчётов: %d", me.get("username"), len(chats))
    conn = db.connect()
    tz = ZoneInfo(cfg["timezone"])
    evening = dt.time.fromisoformat(cfg["reports"]["evening_time"])
    offset = None
    alert_sent = {}
    next_check = 0.0
    while True:
        try:
            # длинный опрос Telegram 25 с; ждём ответа HTTP чуть дольше
            updates = tg.call("getUpdates", http_timeout=40, offset=offset, timeout=25)
            for upd in updates:
                offset = upd["update_id"] + 1
                handle_update(upd, conn, cfg, tg, chats)
        except Exception as e:  # noqa: BLE001
            log.error("ошибка опроса Telegram: %s", e)
            time.sleep(10)

        if time.time() >= next_check:
            next_check = time.time() + 60
            try:
                now = dt.datetime.now(tz)
                today = now.date().isoformat()
                if now.time() >= evening and _state_get(conn, "evening_sent") != today:
                    _broadcast(tg, chats, report.evening_report(conn, cfg))
                    _state_set(conn, "evening_sent", today)
                check_alerts(conn, cfg, tg, chats, alert_sent)
            except Exception as e:  # noqa: BLE001
                log.error("ошибка расписания: %s", e)
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = db.connect()


if __name__ == "__main__":
    main()
