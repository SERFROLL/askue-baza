"""Установить аватар бота из assets/bot_logo.png через Bot API (setMyProfilePhoto).
Запуск в контейнере bot: python - < scripts/set_bot_photo.py  (картинка передаётся через /logo.png)."""
import json
import os
import sys

import requests

token = os.environ["TELEGRAM_BOT_TOKEN"]
path = sys.argv[1] if len(sys.argv) > 1 else "/logo.png"
png = open(path, "rb").read()
r = requests.post("https://api.telegram.org/bot%s/setMyProfilePhoto" % token,
                  data={"photo": json.dumps({"type": "static", "photo": "attach://logo"})},
                  files={"logo": ("logo.png", png, "image/png")}, timeout=60)
print(r.status_code, r.text[:300])
