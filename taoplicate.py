#!/usr/bin/env python3
"""
TAOplicate — Real-Time BitTensor Copy-Trading Bot
-------------------------------------------------
- Watches chosen hotkeys across ALL subnets for stake/unstake changes.
- Realtime via local Subtensor node WS; fallback to Finney WS; fallback polling.
- Mirrors actions with btcli using fixed / proportional / weighted proportional sizing.
- Discord live alerts + daily summary (00:00 UTC) with net gain/loss and wallet trend.
- Auto-pause on low balance, auto-resume on recovery.
- SQLite logging, --dry-run support, --summary-now at startup.
"""

import os, sys, time, json, shutil, sqlite3, subprocess, datetime, threading, queue, re
from pathlib import Path

import requests
import bittensor as bt
from substrateinterface import SubstrateInterface

# === Rebranded paths/files ===
CONFIG_DIR  = os.path.expanduser("~/.taoplicate")
CONFIG_PATH = os.path.join(CONFIG_DIR, "taoplicate_config.json")
STATE_PATH  = os.path.join(CONFIG_DIR, "taoplicate_state.json")
LOG_PATH    = os.path.join(CONFIG_DIR, "taoplicate.log")
DB_PATH     = Path(CONFIG_DIR) / "taoplicate.db"

event_queue = queue.Queue()

# ----------------- Utility -----------------
def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _seconds_until_next_utc_midnight():
    now = datetime.datetime.utcnow()
    next_midnight = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1, int((next_midnight - now).total_seconds()))

# ----------------- DB -----------------
def init_db():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS trades(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    action TEXT,
                    netuid INTEGER,
                    hotkey TEXT,
                    amount REAL,
                    delta REAL,
                    balance REAL
                )""")
    conn.commit()
    conn.close()

def log_trade_to_db(action, netuid, hk, amount, delta, balance):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO trades(timestamp, action, netuid, hotkey, amount, delta, balance) VALUES(?,?,?,?,?,?,?)",
        (datetime.datetime.utcnow().isoformat(), action, netuid, hk, amount, delta, balance)
    )
    conn.commit()
    conn.close()

# ----------------- Discord -----------------
def post_embed(webhook, embed):
    if not webhook:
        return
    try:
        requests.post(webhook, json={"embeds": [embed]}, timeout=8)
    except Exception as e:
        log(f"Discord send failed: {e}")

def send_trade_embed(cfg, action, netuid, hk, amount, delta):
    webhook = cfg.get("live_webhook")
    if not webhook: return
    color = 0x00FF00 if action == "add" else 0xFF0000
    title = f"🕯️ {'Stake Added' if action=='add' else 'Stake Removed'}"
    desc = (
        f"**Subnet:** `{netuid}`\n"
        f"**Hotkey:** `{hk}`\n"
        f"**Change Detected:** `{delta:+.4f} TAO`\n"
        f"**Mirrored Amount:** `{amount:.4f} TAO`"
    )
    embed = {
        "title": title,
        "description": desc,
        "color": color,
        "footer": {"text": f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} | TAOplicate"},
    }
    post_embed(webhook, embed)

def notify_text(webhook, content):
    if not webhook: return
    try:
        requests.post(webhook, json={"content": content}, timeout=8)
    except Exception as e:
        log(f"Discord text failed: {e}")

# ----------------- btcli helpers -----------------
def run_btcli(cmd):
    log(" ".join(cmd))
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate(timeout=120)
        if out: log(out.strip())
        if err: log(err.strip())
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        log("btcli timed out.")
        return 124, "", "timeout"
    except FileNotFoundError:
        log("btcli not found on PATH.")
        return 127, "", "not found"

def get_wallet_balance_via_btcli(cfg):
    """Parse wallet total balance using `btcli w balance --all` output."""
    try:
        cmd = [cfg["btcli_path"], "w", "balance", "--all", "--wallet-name", cfg["my_wallet"], "--network", cfg["network"]]
        log("Fetching wallet balance via btcli...")
        rc, out, err = run_btcli(cmd)
        if not out:
            return 0.0
        # Prefer a 'Total Balance' line, else first float fallback
        for line in out.splitlines():
            if "Total" in line and "Balance" in line:
                for tok in line.replace(",", " ").split():
                    try:
                        return float(tok)
                    except:
                        pass
        m = re.search(r"([0-9]+\.[0-9]+)", out)
        return float(m.group(1)) if m else 0.0
    except Exception as e:
        log(f"Error fetching wallet balance: {e}")
        return 0.0

def mirror_stake(action, netuid, cfg, amount, hk, delta, summary):
    if amount <= 0:
        return
    cmd = [
        cfg["btcli_path"], "stake", action,
        "--netuid", str(netuid),
        "--wallet-name", cfg["my_wallet"],
        "--amount", f"{amount:.4f}",
        "--network", cfg["network"]
    ]
    rc, out, err = run_btcli(cmd)
    msg = f"{'🟢' if action=='add' else '🔴'} {action.upper()} {amount:.4f} TAO on subnet {netuid} (mirroring {hk} Δ{delta:+.4f})"
    log(msg)
    send_trade_embed(cfg, action, netuid, hk, amount, delta)
    summary["trades"] += 1
    summary["subnets"].add(netuid)
    if action == "add": summary["add_tao"] += amount
    else: summary["rem_tao"] += amount

# ----------------- Summary -----------------
def send_summary_embed(cfg, summary):
    webhook = cfg.get("summary_webhook")
    if not webhook: return

    total_balance = get_wallet_balance_via_btcli(cfg)
    total_add = summary.get("add_tao", 0.0)
    total_rem = summary.get("rem_tao", 0.0)
    net = total_add - total_rem

    # trend arrows (balance change since last report)
    trend_file = os.path.join(CONFIG_DIR, "last_balance.json")
    last_balance = 0.0
    if os.path.exists(trend_file):
        try:
            last_balance = json.load(open(trend_file)).get("balance", 0.0)
        except Exception:
            pass

    delta_bal = total_balance - last_balance
    if abs(delta_bal) < 1e-9:
        trend_emoji, trend_line = "⚪", "no change"
    elif delta_bal > 0:
        trend_emoji, trend_line = "📈", f"+{delta_bal:.4f} TAO"
    else:
        trend_emoji, trend_line = "📉", f"{delta_bal:.4f} TAO"
    json.dump({"balance": total_balance}, open(trend_file, "w"))

    net_line = f"🟩 **Net Gain:** `{net:+.4f} TAO`" if net >= 0 else f"🟥 **Net Loss:** `{net:+.4f} TAO`"

    embed = {
        "title": "📊 Daily Summary — TAOplicate",
        "color": 0x2B6CB0,
        "description": (
            f"**Total Trades:** {summary.get('trades', 0)}\n"
            f"**Subnets Touched:** {len(summary.get('subnets', set()))}\n"
            f"**Total Staked:** `{total_add:.4f} TAO`\n"
            f"**Total Unstaked:** `{total_rem:.4f} TAO`\n"
            f"{net_line}\n\n"
            f"💰 **Wallet Balance:** `{total_balance:.4f} TAO` ({trend_emoji} {trend_line} since last report)"
        ),
        "footer": {"text": f"Report generated {datetime.datetime.now():%Y-%m-%d %H:%M:%S} | TAOplicate"},
    }
    post_embed(webhook, embed)

def summary_scheduler(cfg, summary):
    while True:
        sleep_s = _seconds_until_next_utc_midnight()
        log(f"Daily summary scheduled in {sleep_s} seconds (00:00 UTC).")
        time.sleep(sleep_s)
        send_summary_embed(cfg, summary.copy())
        summary.update({"trades": 0, "add_tao": 0.0, "rem_tao": 0.0, "subnets": set()})

# ----------------- Event Listener -----------------
def start_event_listener(cfg):
    """Connect to local node for realtime; fallback to Finney; push events into queue."""
    urls = ["ws://127.0.0.1:9944", "wss://finney.subtensor.ai:443"]
    sub = None
    for url in urls:
        try:
            sub = SubstrateInterface(url=url, type_registry_preset="subtensor")
            log(f"Connected to {url} for real-time stake events.")
            notify_text(cfg.get("live_webhook"), f"🛰 Connected to `{url}` for real-time stake events.")
            break
        except Exception as e:
            log(f"Failed to connect to {url}: {e}")
            sub = None
    if sub is None:
        log("❌ Could not connect to any WebSocket endpoints.")
        return

    def events_handler(event, update_nr, subscription_id):
        try:
            ev = event.get("event", {})
            if ev.get("module_id") == "SubtensorModule" and ev.get("event_id") in ["StakeAdded", "StakeRemoved"]:
                attrs = ev.get("attributes", [])
                # Expected order: [netuid, hotkey, amount]
                if len(attrs) >= 3:
                    netuid = int(attrs[0])
                    hotkey = str(attrs[1])
                    delta = float(attrs[2])
                    action = "add" if ev["event_id"] == "StakeAdded" else "remove"
                    event_queue.put((action, netuid, hotkey, delta))
        except Exception as e:
            log(f"Event parse error: {e}")

    try:
        sub.subscribe_events(events_handler)
    except Exception as e:
        log(f"Subscription error: {e}")

# ----------------- Setup / Run -----------------
def setup():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    network = input("Network (default finney): ").strip() or "finney"
    my_wallet = input("Your wallet name: ").strip()
    amt = input("Fixed TAO per trade (e.g. 0.1): ").strip()
    n = int(input("How many hotkeys to copy: ").strip())
    hotkeys, weights = [], []
    print("Enter each hotkey + optional weight (default 1.0). Example: 5Eabc123 0.6")
    for i in range(n):
        parts = input(f"Hotkey {i+1}: ").split()
        hotkeys.append(parts[0])
        weights.append(float(parts[1]) if len(parts) > 1 else 1.0)

    poll = int(input("Polling seconds (default 30): ") or "30")
    mode = (input("Trade type — 'fixed' or 'proportional'? ").strip().lower() or "fixed")
    live_webhook = input("Discord Webhook for LIVE trade alerts: ").strip()
    summary_webhook = input("Discord Webhook for DAILY summary reports: ").strip()
    low_bal = float(input("Low-balance threshold (pause when below, e.g. 1.0 TAO): ").strip() or 1.0)
    resume_bal = float(input("Resume-balance threshold (resume when above, e.g. 2.0 TAO): ").strip() or 2.0)

    cfg = {
        "network": network,
        "my_wallet": my_wallet,
        "fixed_amount": amt,
        "hotkeys": hotkeys,
        "weights": weights,
        "poll_seconds": poll,
        "trade_mode": mode,
        "live_webhook": live_webhook,
        "summary_webhook": summary_webhook,
        "low_balance": low_bal,
        "resume_balance": resume_bal,
        "btcli_path": shutil.which("btcli") or "btcli"
    }
    save_json(CONFIG_PATH, cfg)
    save_json(STATE_PATH, {"last_stakes": {}})
    init_db()
    log("Setup complete.")
    if live_webhook:
        notify_text(live_webhook, f"✅ TAOplicate setup complete for {len(hotkeys)} wallets on `{network}`.")
    if summary_webhook:
        notify_text(summary_webhook, "📊 TAOplicate daily summary channel initialized.")

def run():
    dry_run = "--dry-run" in sys.argv
    summary_now = "--summary-now" in sys.argv

    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        log("Run setup first.")
        return

    state = load_json(STATE_PATH, {"last_stakes": {}})
    init_db()

    paused = False
    subtensor = bt.subtensor(network=cfg["network"])
    summary = {"trades":0,"add_tao":0.0,"rem_tao":0.0,"subnets":set()}

    # scheduler + WS listener
    threading.Thread(target=summary_scheduler, args=(cfg, summary), daemon=True).start()
    threading.Thread(target=start_event_listener, args=(cfg,), daemon=True).start()

    if cfg.get("summary_webhook"):
        notify_text(cfg["summary_webhook"], "⏰ Daily summary set to post at **00:00 UTC**.")
    if summary_now:
        log("Running --summary-now: sending immediate daily summary.")
        send_summary_embed(cfg, summary.copy())

    poll = int(cfg["poll_seconds"])

    while True:
        try:
            # process realtime events
            while not event_queue.empty():
                action, netuid, hk, delta = event_queue.get()
                if hk not in cfg["hotkeys"]:
                    continue
                i = cfg["hotkeys"].index(hk)
                weight = cfg["weights"][i]
                amount = float(cfg["fixed_amount"]) if cfg["trade_mode"] == "fixed" else abs(delta) * weight

                # safety
                current_balance = get_wallet_balance_via_btcli(cfg)
                if not paused and current_balance < cfg["low_balance"]:
                    paused = True
                    notify_text(cfg.get("live_webhook"), f"⛔ **Paused** — balance {current_balance:.4f} TAO < {cfg['low_balance']} TAO.")
                if paused:
                    if current_balance >= cfg["resume_balance"]:
                        paused = False
                        notify_text(cfg.get("live_webhook"), f"✅ **Resumed** — balance {current_balance:.4f} TAO ≥ {cfg['resume_balance']} TAO.")
                    else:
                        continue

                if dry_run:
                    log(f"[DRY-RUN] Would {action.upper()} {amount:.4f} TAO on netuid {netuid} for {hk}")
                else:
                    mirror_stake(action, netuid, cfg, amount, hk, delta, summary)
                    log_trade_to_db(action, netuid, hk, amount, delta, current_balance)

            # fallback polling (heartbeat)
            n_subnets = subtensor.subnet_count
            for netuid in range(n_subnets):
                mg = subtensor.metagraph(netuid=netuid)
                if not mg.hotkeys:
                    continue
                for i, hk in enumerate(cfg["hotkeys"]):
                    if hk not in mg.hotkeys:
                        continue
                    idx = mg.hotkeys.index(hk)
                    stake_val = float(mg.stake[idx])
                    state["last_stakes"].setdefault(str(netuid), {})
                    last = state["last_stakes"][str(netuid)].get(hk)
                    state["last_stakes"][str(netuid)][hk] = stake_val
                    if last is None:
                        continue
                    delta = stake_val - last
                    if abs(delta) < 1e-9:
                        continue

                    weight = cfg["weights"][i]
                    amount = float(cfg["fixed_amount"]) if cfg["trade_mode"] == "fixed" else abs(delta) * weight

                    current_balance = get_wallet_balance_via_btcli(cfg)
                    if not paused and current_balance < cfg["low_balance"]:
                        paused = True
                        notify_text(cfg.get("live_webhook"), f"⛔ **Paused** — balance {current_balance:.4f} TAO < {cfg['low_balance']} TAO.")
                    if paused:
                        if current_balance >= cfg["resume_balance"]:
                            paused = False
                            notify_text(cfg.get("live_webhook"), f"✅ **Resumed** — balance {current_balance:.4f} TAO ≥ {cfg['resume_balance']} TAO.")
                        else:
                            continue

                    if dry_run:
                        log(f"[DRY-RUN] Would {'ADD' if delta>0 else 'REMOVE'} {amount:.4f} TAO (netuid {netuid}) for {hk}")
                    else:
                        if delta > 0:
                            mirror_stake("add", netuid, cfg, amount, hk, delta, summary)
                            log_trade_to_db("add", netuid, hk, amount, delta, current_balance)
                        else:
                            mirror_stake("remove", netuid, cfg, amount, hk, delta, summary)
                            log_trade_to_db("remove", netuid, hk, amount, delta, current_balance)

            save_json(STATE_PATH, state)

        except Exception as e:
            log(f"Loop error: {e}")
            notify_text(cfg.get("live_webhook"), f"⚠️ TAOplicate error: `{e}`")

        time.sleep(poll)

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ["setup", "run"]:
        print("Usage: python3 taoplicate.py setup|run [--summary-now] [--dry-run]")
        sys.exit(1)
    if sys.argv[1] == "setup":
        setup()
    else:
        run()
