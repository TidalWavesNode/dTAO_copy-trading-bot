#!/usr/bin/env python3
"""
TAOplicate — Real-Time BitTensor Copy-Trading Bot

Key features:
- Mirrors stake/unstake actions from watched hotkeys across ALL subnets.
- Real-time (optional) via WebSocket; robust polling fallback.
- Trade sizing: 1) fixed amount OR 2) proportional % of delta, with optional per-hotkey weights.
- Discord live alerts + daily summary (00:00 UTC) with net gain/loss and wallet trend.
- Uses TAOStats on the backend (no prompts) to focus polling on relevant subnets.
- Wallet balance via `btcli w balance --wallet-name <wallet> --network <net>`.
- SQLite audit log.
- PM2 integration: prompt at end of setup (or use --pm2 / --no-pm2 flags).
"""

import os, sys, time, json, shutil, sqlite3, subprocess, datetime, threading, queue, re, logging
from pathlib import Path

import requests
import bittensor as bt
from substrateinterface import SubstrateInterface

# Quiet noisy logs
try:
    logging.getLogger("bittensor").setLevel(logging.WARNING)
    logging.getLogger("substrateinterface").setLevel(logging.WARNING)
except Exception:
    pass

# ANSI
try:
    import colorama
    colorama.init()
except Exception:
    pass
GREEN = "\033[92m"; RESET = "\033[0m"
def ginput(prompt_text: str) -> str:
    try:
        return input(f"{GREEN}{prompt_text}{RESET}")
    except Exception:
        return input(prompt_text)
def gprint(text: str):
    try:
        print(f"{GREEN}{text}{RESET}")
    except Exception:
        print(text)

# Paths
CONFIG_DIR  = os.path.expanduser("~/.taoplicate")
CONFIG_PATH = os.path.join(CONFIG_DIR, "taoplicate_config.json")
STATE_PATH  = os.path.join(CONFIG_DIR, "taoplicate_state.json")
LOG_PATH    = os.path.join(CONFIG_DIR, "taoplicate.log")
DB_PATH     = Path(CONFIG_DIR) / "taoplicate.db"

event_queue = queue.Queue()

# Utils
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

# DB
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
    conn.commit(); conn.close()

def log_trade_to_db(action, netuid, hk, amount, delta, balance):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO trades(timestamp, action, netuid, hotkey, amount, delta, balance) VALUES(?,?,?,?,?,?,?)",
        (datetime.datetime.utcnow().isoformat(), action, netuid, hk, amount, delta, balance)
    )
    conn.commit(); conn.close()

# Discord
def post_embed(webhook, embed):
    if not webhook: return
    try: requests.post(webhook, json={"embeds":[embed]}, timeout=8)
    except Exception as e: log(f"Discord send failed: {e}")

def send_trade_embed(cfg, action, netuid, hk, amount, delta):
    webhook = cfg.get("live_webhook")
    if not webhook: return
    color = 0x00FF00 if action=="add" else 0xFF0000
    title = f"🕯️ {'Stake Added' if action=='add' else 'Stake Removed'}"
    desc = (
        f"**Subnet:** `{netuid}`\n"
        f"**Hotkey:** `{hk}`\n"
        f"**Change Detected:** `{delta:+.4f} TAO`\n"
        f"**Mirrored Amount:** `{amount:.4f} TAO`"
    )
    embed = {"title":title,"description":desc,"color":color,
             "footer":{"text":f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} | TAOplicate"}}
    post_embed(webhook, embed)

def notify_text(webhook, content):
    if not webhook: return
    try: requests.post(webhook, json={"content": content}, timeout=8)
    except Exception as e: log(f"Discord text failed: {e}")

# btcli
def run_btcli(cmd):
    log(" ".join(cmd))
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate(timeout=120)
        if out: log(out.strip())
        if err: log(err.strip())
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        log("btcli timed out."); return 124, "", "timeout"
    except FileNotFoundError:
        log("btcli not found on PATH."); return 127, "", "not found"

def _extract_first_float(text: str):
    cleaned = re.sub(r"[^0-9.\n ]+", " ", text)
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", cleaned)
    return float(m.group(1)) if m else None

def get_wallet_balance_via_btcli(cfg):
    try:
        cmd = [cfg["btcli_path"], "w", "balance",
               "--wallet-name", cfg["my_wallet"],
               "--network", cfg["network"]]
        log("Fetching wallet balance via btcli...")
        rc, out, err = run_btcli(cmd)
        if not out: return 0.0
        best = None
        for line in out.splitlines():
            low = line.lower()
            if any(k in low for k in ["balance","free","total","available"]):
                val = _extract_first_float(line)
                if val is not None: best = val; break
        if best is None:
            val = _extract_first_float(out); return val if val is not None else 0.0
        return best
    except Exception as e:
        log(f"Error fetching wallet balance: {e}"); return 0.0

def get_wallet_balance(cfg) -> float:
    return get_wallet_balance_via_btcli(cfg)

# TAOStats (backend only)
TAOSTATS_TIMEOUT = 10
_TAOSTATS_CACHE = {"map": {}, "ts": 0}

def _fetch_taostats_html(url: str):
    try:
        resp = requests.get(url, timeout=TAOSTATS_TIMEOUT, headers={"User-Agent":"TAOplicate/1.0"})
        if resp.status_code == 200: return resp.text
    except Exception as e:
        log(f"TAOStats fetch error: {e}")
    return None

def _parse_taostats_subnets(html: str) -> set[int]:
    subnets = set()
    for m in re.finditer(r"(?:#|subnet\s+|netuid\s+)(\d{1,3})", html, flags=re.IGNORECASE):
        try:
            n = int(m.group(1))
            if 0 <= n < 128: subnets.add(n)
        except: continue
    return subnets

def taostats_subnets_for_hotkey(hotkey: str, ttl: int = 600) -> set[int]:
    now = time.time()
    cached = _TAOSTATS_CACHE["map"].get(hotkey)
    if cached and now - cached[1] < ttl: return set(cached[0])
    url = f"https://taostats.io/account/{hotkey}"
    html = _fetch_taostats_html(url)
    if not html: return set()
    subnets = _parse_taostats_subnets(html)
    _TAOSTATS_CACHE["map"][hotkey] = (set(subnets), now)
    return subnets

# Mirror stake
def mirror_stake(action, netuid, cfg, amount, hk, delta, summary):
    if amount <= 0: return
    cmd = [cfg["btcli_path"], "stake", action,
           "--netuid", str(netuid),
           "--wallet-name", cfg["my_wallet"],
           "--amount", f"{amount:.4f}",
           "--network", cfg["network"]]
    rc, out, err = run_btcli(cmd)
    msg = f"{'🟢' if action=='add' else '🔴'} {action.upper()} {amount:.4f} TAO on subnet {netuid} (mirroring {hk} Δ{delta:+.4f})"
    log(msg); send_trade_embed(cfg, action, netuid, hk, amount, delta)
    summary["trades"] += 1; summary["subnets"].add(netuid)
    if action == "add": summary["add_tao"] += amount
    else: summary["rem_tao"] += amount

# Summary
def send_summary_embed(cfg, summary):
    webhook = cfg.get("summary_webhook")
    if not webhook: return
    total_balance = get_wallet_balance(cfg)
    total_add = summary.get("add_tao", 0.0)
    total_rem = summary.get("rem_tao", 0.0)
    net = total_add - total_rem
    trend_file = os.path.join(CONFIG_DIR, "last_balance.json")
    last_balance = 0.0
    if os.path.exists(trend_file):
        try: last_balance = json.load(open(trend_file)).get("balance", 0.0)
        except Exception: pass
    delta_bal = total_balance - last_balance
    if abs(delta_bal) < 1e-9: trend_emoji, trend_line = "⚪", "no change"
    elif delta_bal > 0: trend_emoji, trend_line = "📈", f"+{delta_bal:.4f} TAO"
    else: trend_emoji, trend_line = "📉", f"{delta_bal:.4f} TAO"
    json.dump({"balance": total_balance}, open(trend_file, "w"))
    net_line = f"🟩 **Net Gain:** `{net:+.4f} TAO`" if net >= 0 else f"🟥 **Net Loss:** `{net:+.4f} TAO`"
    embed = {"title":"📊 Daily Summary — TAOplicate","color":0x2B6CB0,
             "description":(
                f"**Total Trades:** {summary.get('trades', 0)}\n"
                f"**Subnets Touched:** {len(summary.get('subnets', set()))}\n"
                f"**Total Staked:** `{total_add:.4f} TAO`\n"
                f"**Total Unstaked:** `{total_rem:.4f} TAO`\n"
                f"{net_line}\n\n"
                f"💰 **Wallet Balance:** `{total_balance:.4f} TAO` ({trend_emoji} {trend_line} since last report)"
             ),
             "footer":{"text":f"Report generated {datetime.datetime.now():%Y-%m-%d %H:%M:%S} | TAOplicate"}}
    post_embed(webhook, embed)

def summary_scheduler(cfg, summary):
    while True:
        sleep_s = _seconds_until_next_utc_midnight()
        log(f"Daily summary scheduled in {sleep_s} seconds (00:00 UTC).")
        time.sleep(sleep_s)
        send_summary_embed(cfg, summary.copy())
        summary.update({"trades":0,"add_tao":0.0,"rem_tao":0.0,"subnets":set()})

# Events (optional WS)
def start_event_listener(cfg):
    urls = cfg.get("ws_endpoints") or ["ws://127.0.0.1:9944"]
    sub = None
    for url in urls:
        try:
            sub = SubstrateInterface(url=url, type_registry_preset="subtensor")
            log(f"Connected to {url} for real-time stake events.")
            notify_text(cfg.get("live_webhook"), f"🛰 Connected to `{url}` for real-time stake events.")
            break
        except Exception as e:
            log(f"WS connect failed: {url} ({e})"); sub = None
    if sub is None:
        log("Running in polling-only mode (no WebSocket)."); return

    def events_handler(event, update_nr, subscription_id):
        try:
            ev = event.get("event", {})
            if ev.get("module_id") == "SubtensorModule" and ev.get("event_id") in ["StakeAdded","StakeRemoved"]:
                attrs = ev.get("attributes", [])
                if len(attrs) >= 3:
                    netuid = int(attrs[0]); hotkey = str(attrs[1]); delta = float(attrs[2])
                    action = "add" if ev["event_id"] == "StakeAdded" else "remove"
                    event_queue.put((action, netuid, hotkey, delta))
        except Exception as e:
            log(f"Event parse error: {e}")

    try:
        sub.subscribe_events(events_handler)
    except Exception as e:
        log(f"Subscription error (polling-only continues): {e}")

# Subnet discovery
_NETUID_CACHE = {"list": [], "ts": 0}
def discover_netuids(subtensor, max_scan=128, cache_secs=600):
    now = time.time()
    if _NETUID_CACHE["list"] and now - _NETUID_CACHE["ts"] < cache_secs:
        return _NETUID_CACHE["list"]
    try:
        if hasattr(subtensor, "get_all_subnet_netuids"):
            netuids = list(subtensor.get_all_subnet_netuids())
            if netuids:
                netuids = sorted(set(int(n) for n in netuids if 0 <= int(n) < max_scan))
                _NETUID_CACHE.update({"list":netuids,"ts":now}); return netuids
    except Exception: pass
    try:
        if hasattr(subtensor, "subnets"):
            subs = subtensor.subnets()
            netuids = [int(getattr(s,"netuid",-1)) for s in subs if getattr(s,"netuid",None) is not None]
            netuids = sorted(set(n for n in netuids if 0 <= n < max_scan))
            if netuids:
                _NETUID_CACHE.update({"list":netuids,"ts":now}); return netuids
    except Exception: pass
    found = []
    for uid in range(max_scan):
        try:
            mg = subtensor.metagraph(netuid=uid)
            if getattr(mg, "hotkeys", None) is not None: found.append(uid)
        except Exception: continue
    netuids = sorted(set(found))
    _NETUID_CACHE.update({"list":netuids,"ts":now}); return netuids

# PM2
def start_pm2_or_hint():
    pm2 = shutil.which("pm2")
    if not pm2:
        gprint("pm2 is not installed or not on PATH. Install with: npm i -g pm2")
        gprint("Then start later with: pm2 start taoplicate.py --name TAOplicate --interpreter python3 -- run")
        return
    script_path = str(Path(__file__).resolve())
    cmd = [pm2,"start",script_path,"--name","TAOplicate",
           "--interpreter", shutil.which("python3") or "python3",
           "--","run"]
    try:
        log(" ".join(cmd))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate(timeout=60)
        if out: print(out)
        if err: print(err)
        if p.returncode == 0:
            gprint("Started with pm2 as 'TAOplicate'.")
            save = (ginput("Save pm2 process list for restart on boot? [y/N]: ").strip().lower() or "n")
            if save == "y":
                try: subprocess.run([pm2,"save"], check=False); gprint("pm2 list saved. To enable at boot: pm2 startup")
                except Exception: pass
        else:
            gprint("pm2 failed to start the process. You can try manually:")
            gprint("  pm2 start taoplicate.py --name TAOplicate --interpreter python3 -- run")
    except Exception as e:
        log(f"pm2 start error: {e}")
        gprint("Could not start via pm2. Start manually with the command above.")

def maybe_start_with_pm2(auto_flag: str | None):
    """
    auto_flag: 'pm2' -> auto-start; 'nopm2' -> skip; None -> interactive prompt if TTY.
    """
    if auto_flag == "pm2":
        start_pm2_or_hint(); return
    if auto_flag == "nopm2":
        gprint("Setup complete. You can later run:\n  pm2 start taoplicate.py --name TAOplicate --interpreter python3 -- run")
        return
    # Interactive prompt if TTY; otherwise just print hint.
    if sys.stdin.isatty():
        yn = (ginput("Start TAOplicate now with pm2? [y/N]: ").strip().lower() or "n")
        if yn == "y":
            start_pm2_or_hint()
        else:
            gprint("Setup complete. You can later run:\n  pm2 start taoplicate.py --name TAOplicate --interpreter python3 -- run")
    else:
        gprint("Setup complete. Non-interactive session detected.\nStart later with:\n  pm2 start taoplicate.py --name TAOplicate --interpreter python3 -- run")

# Setup
def setup():
    print("\nTAOplicate — mirror/copy TAO staking actions across BitTensor subnets in real time.\n")

    network = ginput("Network (default finney): ").strip() or "finney"
    my_wallet = ginput("Your wallet name: ").strip()

    print()
    gprint("Trade sizing mode:")
    gprint("  1) fixed — use a constant TAO amount each time")
    gprint("  2) proportional — mirror a percentage of the watched wallet’s stake change")
    mode_sel = (ginput("Choose 1 or 2 [1/2]: ").strip() or "1")
    while mode_sel not in ("1","2"):
        mode_sel = (ginput("Please choose 1 (fixed) or 2 (proportional): ").strip() or "1")
    mode = "fixed" if mode_sel == "1" else "proportional"

    fixed_amount = ""; proportional_pct = ""; weights_in_fixed = False
    if mode == "fixed":
        fixed_amount = ginput("Fixed TAO per trade (e.g., 0.25): ").strip()
        while True:
            try: _ = float(fixed_amount); break
            except: fixed_amount = ginput("Please enter a numeric TAO amount (e.g., 0.25): ").strip()
        print()
        gprint("Option: Per-hotkey weights in FIXED mode")
        gprint("  If enabled, your fixed amount is multiplied per wallet by its weight.")
        gprint("  Examples:")
        gprint("    • Fixed = 0.25 TAO, weight 2.0  → you trade 0.50 TAO for that wallet")
        gprint("    • Fixed = 0.25 TAO, weight 0.5  → you trade 0.125 TAO for that wallet")
        gprint("  Impact: lets you prioritize or downweight specific wallets without changing the global fixed size.")
        yn = (ginput("Enable per-hotkey weights in FIXED mode? [y/N]: ").strip().lower() or "n")
        weights_in_fixed = yn == "y"
    else:
        print()
        gprint("Proportional mode = If a watched wallet stakes +1.00 TAO and you set 50%, you will stake +0.50 TAO (before per-wallet weighting). Same idea for unstakes.")
        proportional_pct = ginput("Proportional percentage to mirror (1–100, default 100): ").strip() or "100"
        while True:
            try:
                v = float(proportional_pct)
                if 0 < v <= 100: break
                else: proportional_pct = ginput("Enter a number between 1 and 100: ").strip()
            except: proportional_pct = ginput("Enter a number between 1 and 100: ").strip()

    print()
    n = int(ginput("How many hotkeys to copy: ").strip())
    hotkeys, weights = [], []
    gprint("Enter each hotkey + optional weight (default 1.0). Example: 5Eabc123 0.6")
    for i in range(n):
        parts = ginput(f"Hotkey {i+1}: ").split()
        hotkeys.append(parts[0]); weights.append(float(parts[1]) if len(parts) > 1 else 1.0)

    print()
    gprint("Weight recap & example:")
    if mode == "fixed":
        fa = float(fixed_amount)
        if weights_in_fixed:
            gprint(f"  You chose FIXED = {fa:.4f} TAO and ENABLED weights in fixed mode.")
            gprint("  That means each wallet's trade = FIXED × its weight.")
            for idx, (hk, w) in enumerate(zip(hotkeys, weights), 1):
                gprint(f"    Wallet {idx} ({hk[:8]}…): weight {w:.2f} → example trade {fa*w:.4f} TAO")
        else:
            gprint(f"  You chose FIXED = {fa:.4f} TAO and DISABLED weights in fixed mode.")
            gprint("  That means each wallet's trade = FIXED (weights are ignored).")
            for idx, hk in enumerate(hotkeys, 1):
                gprint(f"    Wallet {idx} ({hk[:8]}…): example trade {fa:.4f} TAO")
    else:
        pct = float(proportional_pct) / 100.0; base = 1.00 * pct
        gprint(f"  You chose PROPORTIONAL = {pct*100:.0f}% of the detected delta.")
        gprint("  Example if a watched wallet stakes +1.00 TAO:")
        for idx, (hk, w) in enumerate(zip(hotkeys, weights), 1):
            gprint(f"    Wallet {idx} ({hk[:8]}…): weight {w:.2f} → base {base:.4f} × {w:.2f} = {base*w:.4f} TAO")
        gprint("  (For unstakes, the same amounts are removed.)")

    print()
    poll = int(ginput("Polling seconds for backup (default 30): ") or "30")
    live_webhook = ginput("Discord Webhook for LIVE trade alerts: ").strip()
    summary_webhook = ginput("Discord Webhook for DAILY summary reports: ").strip()
    low_bal = float(ginput("Low-balance threshold (pause if below, e.g., 1.0 TAO): ").strip() or 1.0)
    resume_bal = float(ginput("Resume-balance threshold (resume when above, e.g., 2.0 TAO): ").strip() or 2.0)

    cfg = {
        "network": network, "my_wallet": my_wallet,
        "trade_mode": mode, "fixed_amount": fixed_amount, "proportional_pct": proportional_pct,
        "weights_in_fixed": weights_in_fixed,
        "hotkeys": hotkeys, "weights": weights,
        "poll_seconds": poll,
        "live_webhook": live_webhook, "summary_webhook": summary_webhook,
        "low_balance": low_bal, "resume_balance": resume_bal,
        "btcli_path": shutil.which("btcli") or "btcli",
    }
    save_json(CONFIG_PATH, cfg)
    save_json(STATE_PATH, {"last_stakes": {}, "active_map": {}, "cycle": 0})
    init_db()
    log("Setup complete.")

    # PM2 prompt / flags
    auto_flag = None
    if "--pm2" in sys.argv: auto_flag = "pm2"
    elif "--no-pm2" in sys.argv: auto_flag = "nopm2"
    maybe_start_with_pm2(auto_flag)

# Run
def run():
    print("\nTAOplicate — mirror/copy TAO staking actions across BitTensor subnets in real time.\n")
    dry_run = "--dry-run" in sys.argv
    summary_now = "--summary-now" in sys.argv
    poll_only = "--poll-only" in sys.argv
    no_poll   = "--no-poll" in sys.argv

    cfg = load_json(CONFIG_PATH, None)
    if not cfg: log("Run setup first."); return

    state = load_json(STATE_PATH, {"last_stakes": {}, "active_map": {}, "cycle": 0})
    if "active_map" not in state or not isinstance(state["active_map"], dict): state["active_map"] = {}
    if "last_stakes" not in state or not isinstance(state["last_stakes"], dict): state["last_stakes"] = {}
    if "cycle" not in state or not isinstance(state["cycle"], int): state["cycle"] = 0

    init_db()
    paused = False
    subtensor = bt.subtensor(network=cfg["network"])
    summary = {"trades":0,"add_tao":0.0,"rem_tao":0.0,"subnets":set()}

    threading.Thread(target=summary_scheduler, args=(cfg, summary), daemon=True).start()
    if poll_only: log("Flag --poll-only: skipping WS listener.")
    else: threading.Thread(target=start_event_listener, args=(cfg,), daemon=True).start()

    if cfg.get("summary_webhook"): notify_text(cfg["summary_webhook"], "⏰ Daily summary set to post at **00:00 UTC**.")
    if summary_now:
        log("Running --summary-now: sending immediate daily summary.")
        send_summary_embed(cfg, summary.copy())

    poll = int(cfg["poll_seconds"])
    while True:
        try:
            while not event_queue.empty():
                action, netuid, hk, delta = event_queue.get()
                if hk not in cfg["hotkeys"]: continue
                i = cfg["hotkeys"].index(hk); weight = cfg["weights"][i]
                if cfg["trade_mode"] == "fixed":
                    base = float(cfg["fixed_amount"])
                    weight_to_use = weight if cfg.get("weights_in_fixed", False) else 1.0
                else:
                    pct = float(cfg.get("proportional_pct", "100") or "100")/100.0
                    base = abs(delta) * pct; weight_to_use = weight
                amount = base * weight_to_use

                current_balance = get_wallet_balance(cfg)
                if not paused and current_balance < cfg["low_balance"]:
                    paused = True; notify_text(cfg.get("live_webhook"), f"⛔ **Paused** — balance {current_balance:.4f} TAO < {cfg['low_balance']} TAO.")
                if paused:
                    if current_balance >= cfg["resume_balance"]:
                        paused = False; notify_text(cfg.get("live_webhook"), f"✅ **Resumed** — balance {current_balance:.4f} TAO ≥ {cfg['resume_balance']} TAO.")
                    else: continue

                if dry_run: log(f"[DRY-RUN] Would {action.upper()} {amount:.4f} TAO on netuid {netuid} for {hk} (base {base:.4f}, weight {weight_to_use:.2f})")
                else:
                    mirror_stake(action, netuid, cfg, amount, hk, delta, summary)
                    log_trade_to_db(action, netuid, hk, amount, delta, current_balance)

            if not no_poll:
                working = set()
                for hk in cfg["hotkeys"]:
                    working.update(taostats_subnets_for_hotkey(hk, ttl=600))
                active_map = state.get("active_map", {})
                for hk in cfg["hotkeys"]:
                    for n in active_map.get(hk, []):
                        if 0 <= int(n) < 128: working.add(int(n))
                cycle = int(state.get("cycle", 0))
                if cycle % 20 == 0 or not working:
                    discovered = discover_netuids(subtensor, max_scan=128, cache_secs=600)
                    working.update(discovered)
                state["cycle"] = cycle + 1

                netuids = sorted(working)
                for netuid in netuids:
                    mg = subtensor.metagraph(netuid=netuid)
                    if not getattr(mg, "hotkeys", None): continue
                    for i, hk in enumerate(cfg["hotkeys"]):
                        if hk not in mg.hotkeys: continue
                        idx = mg.hotkeys.index(hk)
                        stake_val = float(mg.stake[idx])
                        state["last_stakes"].setdefault(str(netuid), {})
                        last = state["last_stakes"][str(netuid)].get(hk)
                        state["last_stakes"][str(netuid)][hk] = stake_val
                        if last is None: continue
                        delta = stake_val - last
                        if abs(delta) < 1e-9: continue

                        s = set(active_map.get(hk, [])); s.add(int(netuid))
                        active_map[hk] = sorted(s); state["active_map"] = active_map

                        weight = cfg["weights"][i]
                        if cfg["trade_mode"] == "fixed":
                            base = float(cfg["fixed_amount"])
                            weight_to_use = weight if cfg.get("weights_in_fixed", False) else 1.0
                        else:
                            pct = float(cfg.get("proportional_pct", "100") or "100")/100.0
                            base = abs(delta) * pct; weight_to_use = weight
                        amount = base * weight_to_use

                        current_balance = get_wallet_balance(cfg)
                        if not paused and current_balance < cfg["low_balance"]:
                            paused = True; notify_text(cfg.get("live_webhook"), f"⛔ **Paused** — balance {current_balance:.4f} TAO < {cfg['low_balance']} TAO.")
                        if paused:
                            if current_balance >= cfg["resume_balance"]:
                                paused = False; notify_text(cfg.get("live_webhook"), f"✅ **Resumed** — balance {current_balance:.4f} TAO ≥ {cfg['resume_balance']} TAO.")
                            else: continue

                        if dry_run:
                            log(f"[DRY-RUN] Would {'ADD' if delta>0 else 'REMOVE'} {amount:.4f} TAO (netuid {netuid}) for {hk} (base {base:.4f}, weight {weight_to_use:.2f})")
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

# Entrypoint
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ["setup","run"]:
        print("Usage: python3 taoplicate.py setup|run [--pm2|--no-pm2] [--summary-now] [--dry-run] [--poll-only] [--no-poll]")
        sys.exit(1)
    if sys.argv[1] == "setup":
        setup()
    else:
        run()
