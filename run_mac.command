#!/bin/bash
set -e
cd "$(dirname "$0")"

# このアプリの古いサーバーが残っている場合だけ停止する。
existing_pid="$(lsof -tiTCP:8765 -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$existing_pid" ]; then
  existing_command="$(ps -p "$existing_pid" -o command= 2>/dev/null || true)"
  if [[ "$existing_command" == *"uvicorn app.main:app"* ]]; then
    echo "古いサーバーを停止しています..."
    kill "$existing_pid"
    for _ in {1..20}; do
      kill -0 "$existing_pid" 2>/dev/null || break
      sleep 0.1
    done
  else
    echo "エラー: ポート8765は別のアプリが使用中です。"
    exit 1
  fi
fi

python_command="python3"
if [ -x /opt/homebrew/bin/python3.12 ]; then
  python_command="/opt/homebrew/bin/python3.12"
fi
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "Python 3.10以上の環境へ更新しています..."
  mv .venv .venv-python39-backup
fi
if [ ! -d .venv ]; then
  "$python_command" -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
(sleep 2; open http://127.0.0.1:8765 >/dev/null 2>&1 || true) &
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
