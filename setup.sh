#!/usr/bin/env bash
# 本地部署：建 venv、裝依賴、跑測試、離線驗證一輪。
set -euo pipefail
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "需要 python3（建議 3.11+）"; exit 1; }
echo "==> 建立虛擬環境 .venv"
python3 -m venv .venv
source .venv/bin/activate

echo "==> 安裝依賴"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "==> 跑全套測試"
python -m pytest -q

echo "==> 離線驗證一輪（synthetic provider）"
python run.py scan --offline

cat <<'EOF'

✔ 部署完成。接下來：

  1) 台股資料源 token（免費註冊 https://finmindtrade.com）：
       export FINMIND_TOKEN="你的token"        # 建議放進 ~/.zshrc 或 ~/.bashrc

  2) 跑真實資料：
       source .venv/bin/activate
       python run.py scan                      # us + tw 全掃
       open out/memo_*.html                    # macOS；Linux 用 xdg-open

  3) 排程（crontab -e，時間為台北 UTC+8）：
       30 5  * * 2-6  cd $PWD && ./.venv/bin/python run.py scan --market us >> data/cron.log 2>&1
       30 14 * * 1-5  cd $PWD && ./.venv/bin/python run.py scan --market tw >> data/cron.log 2>&1
     （cron 環境讀不到 shell 的 export，FINMIND_TOKEN 要寫在 crontab 開頭或包一層腳本）
EOF
