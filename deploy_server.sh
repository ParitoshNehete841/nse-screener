#!/usr/bin/env bash
# Sets up the screener on a fresh Ubuntu server (22.04 / 24.04). A Mumbai-region server with a RESERVED static IP
# (e.g. AWS Elastic IP) is ideal: register that IP in the Kite developer console to place orders from it.
# 1) Copy the app files (and your .env) into ~/nse-screener  2) run:  bash deploy_server.sh
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/nse-screener}"
sudo timedatectl set-timezone Asia/Kolkata
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip ufw curl
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
chmod 600 .env 2>/dev/null || true            # API keys: readable by you only
sudo ufw allow OpenSSH && sudo ufw --force enable   # nothing else is exposed to the internet
sudo tee /etc/systemd/system/nse-app.service >/dev/null <<UNIT
[Unit]
Description=NSE swing screener
After=network-online.target
[Service]
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m streamlit run screener.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now nse-app
# phone alerts on weekdays at 09:25 IST (edit --indices to taste; remove this line to disable)
( crontab -l 2>/dev/null | grep -v alert_bot.py || true; echo "25 9 * * 1-5 cd $APP_DIR && .venv/bin/python alert_bot.py --indices 'Nifty 100' >> data/bot.log 2>&1" ) | crontab -
echo; echo "Outgoing public IP of this server (register THIS in the Kite developer console):"; curl -s https://api.ipify.org || true; echo
echo "Open the app from your own computer with:  ssh -L 8501:localhost:8501 $USER@<server-ip>   then browse to http://localhost:8501"
