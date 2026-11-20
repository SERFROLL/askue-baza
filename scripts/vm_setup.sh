#!/usr/bin/env bash
# Первичная настройка виртуалки (Ubuntu 24.04). Запускать один раз от пользователя askue:
#   bash vm_setup.sh
# Ставит Docker, создаёт ключ для чтения репозитория с GitHub, клонирует проект и создаёт .env.
set -euo pipefail

REPO="git@github.com:SERFROLL/askue-baza.git"
DIR="$HOME/askue-baza"

echo "== Docker"
if ! command -v docker >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-v2 git
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER"
  echo "Docker установлен. Группа docker заработает после повторного входа по SSH."
fi

echo "== Ключ для GitHub (только чтение)"
KEY="$HOME/.ssh/github_askue"
if [ ! -f "$KEY" ]; then
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  ssh-keygen -q -t ed25519 -f "$KEY" -N "" -C "askue-vm-readonly"
  cat >> "$HOME/.ssh/config" <<EOF
Host github.com
  IdentityFile $KEY
  IdentitiesOnly yes
EOF
  chmod 600 "$HOME/.ssh/config"
fi
echo "Добавьте этот ключ в GitHub: репозиторий -> Settings -> Deploy keys (без права записи):"
cat "$KEY.pub"

echo "== Репозиторий"
if [ ! -d "$DIR/.git" ]; then
  ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null
  git clone "$REPO" "$DIR" || { echo "Не удалось клонировать: добавьте ключ выше и запустите скрипт ещё раз."; exit 1; }
fi

echo "== .env"
cd "$DIR"
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 16)|" .env
  sed -i "s|^GRAFANA_ADMIN_PASSWORD=.*|GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 12)|" .env
  chmod 600 .env
  echo "Создан .env со случайными паролями. Токен бота впишите позже."
fi

echo "Готово. Запуск: cd $DIR && docker compose up -d --build"
