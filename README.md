# 🕯️ BitTensor CopyTrader — Global Copy-Staking Bot

Real-time, PM2-managed bot that mirrors **stake/unstake** actions from chosen BitTensor **hotkeys** across **all subnets** using your wallet — with Discord alerts, daily summaries (00:00 UTC), automatic low-balance pause/resume, **SQLite analytics**, **weighted proportional mode**, and **dry-run** simulation.

---

![status](https://img.shields.io/badge/status-alpha-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![bittensor](https://img.shields.io/badge/bittensor-finney-orange)

## ✨ Features
- **Global copy-staking**: watches your list of **hotkeys** on **every subnet**; mirrors stake adds/removes.
- **Real-time events** via **local Subtensor node** (primary) with **Finney WS** fallback + **polling** as safety.
- **Trade sizing modes**: `fixed`, `proportional`, or **weighted proportional** (per-hotkey weight).
- **Discord**:
  - **Live alerts** (colored embeds for each mirrored stake/unstake).
  - **Daily summary** at **00:00 UTC** to a separate webhook: totals, net gain/loss, wallet balance with **📈/📉 trend**.
- **Safety**: Auto-pause when TAO balance falls below threshold; **auto-resume** when balance recovers.
- **Analytics**: All mirrored actions stored in **SQLite** (`~/.candles/copytrader.db`).
- **Dry-run** mode for safe testing (no on-chain transactions).
- **PM2** integration for process supervision.

---

## 🧱 Architecture

```mermaid
flowchart LR
  A[Local Subtensor Node<br/>ws://127.0.0.1:9944] -- StakeAdded/Removed --> E{Event Listener}
  B[Finney WS Backup<br/>wss://finney.subtensor.ai:443] -- if local fails --> E
  E --> Q[(Event Queue)]
  Q --> L[Logic: filter watched hotkeys<br/>calc amount (mode/weights)]
  L --> S[Safety: low balance pause/resume]
  S -->|ok| T[btcli stake add/remove]
  S -->|paused| D1[Discord: pause alert]
  T --> D2[Discord: live embed]
  T --> DB[(SQLite trades)]
  CR[00:00 UTC Scheduler] --> DS[Discord: daily summary<br/>wallet balance + trend]
```

## Install
Prereqs: Python 3.10+, Node.js (for PM2), btcli configured, and (recommended) a local subtensor node exposing WebSocket ws://127.0.0.1:9944

### Clone and enter
`git clone YOUR_REPO_URL.git bittensor-copytrader`
`cd bittensor-copytrader`

### Python deps
`python3 -m venv .venv && source .venv/bin/activate`
`pip install -r requirements.txt`

### (Optional) Install PM2 for supervision
`npm i -g pm2`

## 🚀 First-Run Setup
`python3 copytrader_all.py setup`

You’ll be prompted for:
Network (e.g., finney)
Your wallet name (for btcli --wallet-name)
Fixed TAO per trade (used only if fixed mode)
Watched hotkeys (each line supports optional weight, e.g. 5Eabc.. 0.6)
Polling seconds (fallback heartbeat; 20–60s recommended)
Trade type: fixed or proportional
Discord webhooks: one for live alerts, one for daily summary
Low/resume balance thresholds (for auto-pause/resume)

Files created:
~/.candles/copytrader_config.json
~/.candles/copytrader_state.json
~/.candles/copytrader.db
~/.candles/copytrader.log
~/.candles/last_balance.json

## Run
`python3 copytrader_all.py run --summary-now`
# add --dry-run to simulate without btcli transactions

### PM2:
`pm2 start ecosystem.config.js --name bt-copytrader -- "run --summary-now"
pm2 logs bt-copytrader --lines 200
pm2 save`

## 🖼️ Discord Examples
Live trade embed
“Stake Added” (green) or “Stake Removed” (red) with subnet, hotkey, Δ, mirrored amount.
Daily summary (00:00 UTC, neutral color)
Total trades, subnets touched, total staked/unstaked
🟩/🟥 Net gain/loss
💰 Wallet balance with 📈/📉 since last report

##🛡️ Safety & Ops
Auto-pause when balance < low_balance → Discord alert
Auto-resume when balance >= resume_balance → Discord notice
Event-driven via WS, with Finney fallback and polling safety net
Dry-run for rehearsals
SQLite for audit/analytics:
`sqlite3 ~/.candles/copytrader.db \
  "SELECT timestamp,action,netuid,hotkey,amount,delta FROM trades ORDER BY id DESC LIMIT 20;"`

## 🧪 Tips
If you have many watched hotkeys, prefer event mode (local node) and set polling 60–120s.
Use weights to bias toward trusted wallets.
Add min/max caps in code if you want to clamp mirrored amounts.

## 🤝 Contributing

---

### `LICENSE`
```text
MIT License

Copyright (c) 2025

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
