"""Проверка всех веток диалога без отправки в Telegram: docker compose run --rm -T --no-deps bot python scripts/test_dialog.py
(файл копируется в контейнер через stdin: ... bot python - < scripts/test_dialog.py)."""
import time

from askue import config, db
from askue.dialog import Dialog

cfg = config.load()
conn = db.connect()
d = Dialog(cfg)
ALL = d.all_mask
one = 1 << 3          # Склад 2
two = (1 << 0) | (1 << 3)
ids = [o["id"] for o in cfg["objects"]]
tot = 1 << ids.index("total")
sub = (1 << ids.index("sklad3")) | (1 << ids.index("servis777"))
codes = ["m", "S", "C", "P:0", "Pt:0:3", "Pa:0", "Pd:%d" % one,
         "Pp:%d:now" % one, "Pp:%d:now" % ALL, "Pp:%d:15" % one, "Pp:%d:180" % one, "Pp:%d:720" % two, "Pp:%d:1440" % ALL,
         "E:0", "Ed:%d" % two, "Ep:%d:today" % two, "Ep:%d:today" % (ALL | tot), "Ep:%d:yday" % one, "Ep:%d:mon" % tot,
         "Ep:%d:pmon" % one, "Ep:%d:cust" % one,
         "Ev:%d:today:c" % (ALL | tot), "Ev:%d:today:b" % (ALL | tot), "Ev:%d:7d:b" % two, "Ev:%d:mon:c" % one,
         "chart:3", "now", "energy", "I", "Sub", "m", "Sub", "m",
         "Pp:%d:now" % sub, "Pp:%d:180" % sub, "Ep:%d:today" % sub, "Ev:%d:today:b" % (sub | ALL | tot), "Ev:%d:today:c" % sub,
         "W", "Wp:today", "Wp:yday", "Wp:7d", "Wp:mon", "Wp:pmon", "Wp:cust"]
bad = 0
for c in codes:
    t0 = time.time()
    try:
        v = d.handle(conn, 1, c, True)
        kbl = max((len(b["callback_data"]) for row in v.kb.get("inline_keyboard", []) for b in row), default=0)
        info = ("png %d КБ" % (len(v.png) // 1024)) if v.kind == "photo" else ("текст %d симв." % len(v.text))
        print("OK  %-22s %-5s %-14s cb<=%d  %.1f с | %s" % (c, v.kind, info, kbl, time.time() - t0,
                                                         v.text.replace("\n", " / ")[:110]))
        if kbl > 64:
            bad += 1
            print("   !!! callback_data длиннее 64 байт")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print("ERR %-22s %s: %s" % (c, type(e).__name__, e))
v = d.custom_period(1, "24.11–26.11")
print("OK  W custom 24.11–26.11 ", v.kind, v.text.replace("\n", " / ")[:160])
print("--- полный текст «Рабочее / нерабочее» за 24.11–26.11:\n" + v.text)
d.pending[1] = one
v = d.custom_period(1, "20.11–23.11")
print("OK  custom 20.11–23.11   ", v.kind, v.text.replace("\n", " / ")[:160])
d.pending[1] = one
v = d.custom_period(1, "чепуха")
print("OK  custom чепуха         ", v.text[:80])
print("ошибок:", bad)
