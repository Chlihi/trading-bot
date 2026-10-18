"""
Aktien-Bot für GitHub Actions (Alpaca Paper = Demo-Konto). NUR Aktien/ETFs.
Filter (Trend, RSI, Volatilität), Stop-Loss, Trailing-Stop, Tagesverlust-Limit,
Verkauf aller Aktien kurz vor Börsenschluss. Keys kommen aus GitHub Secrets.
Läufe sind auf 6 Stunden begrenzt: Der Bot übergibt nach 5h45 an den nächsten Lauf
und beendet sich selbst, wenn die Börse nicht bald öffnet.
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

API_KEY = os.environ.get("ALPACA_KEY")
API_SECRET = os.environ.get("ALPACA_SECRET")
if not API_KEY or not API_SECRET:
    sys.exit("FEHLER: Secrets ALPACA_KEY / ALPACA_SECRET fehlen.")
MODE = os.environ.get("MODE", "run")   # "run" oder "sell_all"

SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META",
           "TSLA", "AMD", "NFLX", "AVGO", "JPM", "XOM", "COST", "INTC", "PLTR", "SOFI"]

RISK_PCT = 0.04          # Anteil des Kontowerts pro Kauf
MAX_POSITIONS = 8
FAST, SLOW, TREND = 5, 20, 30
MARGIN = 0.0002
RSI_MAX = 70
MIN_VOL = 0.0001
STOP_LOSS = -0.006       # -0,6 %
TRAIL_ACTIVATE = 0.006   # ab +0,6 % startet der Trailing-Stop
TRAIL_PCT = 0.004        # verkaufen, wenn Kurs 0,4 % unter das Hoch fällt
MIN_HOLD_SEC = 300
COOLDOWN_SEC = 300
MAX_DAILY_LOSS = 0.02    # -2 % seit gestrigem Börsenschluss -> alles verkaufen
INTERVAL_SEC = 30
CLOSE_BUFFER_SEC = 150   # so viele Sekunden vor Börsenschluss wird alles verkauft
MAX_RUNTIME_SEC = 20700  # 5 h 45 min, dann Übergabe an den nächsten Lauf
WAIT_FOR_OPEN_SEC = 900  # nur warten, wenn die Börse in <= 15 Min öffnet

TRADE_URL = "https://paper-api.alpaca.markets/v2"
DATA_URL = "https://data.alpaca.markets/v2"
HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}

buys = sells = wins = losses = 0
realized = 0.0
last_trade = {}
entry_time = {}


def get(url, params=None):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)


def parse_time(s):
    return datetime.fromisoformat(re.sub(r"\.\d+", "", s))


def sma(values, n):
    v = values[-n:]
    return sum(v) / len(v)


def rsi(closes, n=14):
    diffs = [closes[i] - closes[i - 1] for i in range(len(closes) - n, len(closes))]
    gains = sum(d for d in diffs if d > 0) / n
    losses_ = -sum(d for d in diffs if d < 0) / n
    return 100.0 if losses_ == 0 else 100 - 100 / (1 + gains / losses_)


def volatility(closes, n=30):
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - n, len(closes))]
    m = sum(rets) / n
    return (sum((x - m) ** 2 for x in rets) / n) ** 0.5


def stock_positions():
    return {p["symbol"]: p for p in get(f"{TRADE_URL}/positions")
            if p.get("asset_class") != "crypto"}


def analyze():
    start = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = get(f"{DATA_URL}/stocks/bars", {
        "symbols": ",".join(SYMBOLS), "timeframe": "1Min",
        "start": start, "limit": 10000, "feed": "iex"})
    out = {}
    for sym, bars in (data.get("bars") or {}).items():
        closes = [b["c"] for b in bars]
        if len(closes) < max(TREND, SLOW) + 1:
            continue
        fn, sn = sma(closes, FAST), sma(closes, SLOW)
        fp, sp = sma(closes[:-1], FAST), sma(closes[:-1], SLOW)
        sig = None
        if fp <= sp * (1 + MARGIN) and fn > sn * (1 + MARGIN):
            sig = "buy"
        elif fp >= sp and fn < sn:
            sig = "sell"
        out[sym] = {"sig": sig, "rsi": rsi(closes), "vol": volatility(closes),
                    "trend_ok": closes[-1] > sma(closes, TREND)}
    return out


def buy(sym, size, why):
    global buys
    r = requests.post(f"{TRADE_URL}/orders", headers=HEADERS, timeout=15, json={
        "symbol": sym, "notional": f"{size:.2f}", "side": "buy",
        "type": "market", "time_in_force": "day"})
    if r.ok:
        buys += 1
        last_trade[sym] = entry_time[sym] = time.time()
        print(f"  KAUF {sym} für {size:,.0f} $ ({why})", flush=True)
        return True
    print(f"  Kauf fehlgeschlagen {sym}: {r.text[:100]}", flush=True)
    return False


def sell(sym, why, p=None):
    global sells, wins, losses, realized
    r = requests.delete(f"{TRADE_URL}/positions/{sym}", headers=HEADERS, timeout=15)
    if r.ok:
        pl = float(p["unrealized_pl"]) if p else 0.0
        sells += 1
        realized += pl
        if pl >= 0:
            wins += 1
        else:
            losses += 1
        last_trade[sym] = time.time()
        entry_time.pop(sym, None)
        print(f"  VERKAUF {sym} ({why}) G/V {pl:+,.2f} $", flush=True)
        return True
    print(f"  Verkauf fehlgeschlagen {sym}: {r.text[:100]}", flush=True)
    return False


def liquidate_stocks(why):
    for sym, p in stock_positions().items():
        sell(sym, why, p)


def summary(equity, last_equity, start_equity):
    pos = stock_positions()
    unreal = sum(float(p["unrealized_pl"]) for p in pos.values())
    n = wins + losses
    rate = f"{wins / n * 100:.0f} %" if n else "-"
    print("-" * 36)
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), "UTC  AKTIEN-BOT")
    print(f"Kontowert:   {equity:,.2f} $")
    print(f"Seit Start:  {equity - start_equity:+,.2f} $")
    print(f"Heute:       {equity - last_equity:+,.2f} $ ({(equity / last_equity - 1) * 100:+.3f} %)")
    print(f"Trades:      Käufe {buys}, Verkäufe {sells}")
    print(f"Gewinn/Verl: {wins}/{losses} (Quote {rate}), realisiert {realized:+,.2f} $")
    print(f"Offen:       {len(pos)} Positionen, G/V {unreal:+,.2f} $")
    for sym, p in pos.items():
        print(f"  {sym}: {float(p['unrealized_pl']):+,.2f} $ ({float(p['unrealized_plpc']) * 100:+.2f} %)")
    print("-" * 36, flush=True)


def run():
    t0 = time.time()
    acc = get(f"{TRADE_URL}/account")
    start_equity = float(acc["equity"])
    print(f"Verbindung OK. Startwert: {start_equity:,.2f} $ | Modus: {MODE}", flush=True)

    if MODE == "sell_all":
        liquidate_stocks("sell_all")
        time.sleep(5)
        acc = get(f"{TRADE_URL}/account")
        summary(float(acc["equity"]), float(acc["last_equity"]), start_equity)
        return

    liquidated = paused = False
    peak = {}
    last_minute = 0

    while True:
        t = time.time()
        try:
            if t - t0 > MAX_RUNTIME_SEC:
                print("Maximale Laufzeit erreicht, Übergabe an den nächsten Lauf.")
                acc = get(f"{TRADE_URL}/account")
                summary(float(acc["equity"]), float(acc["last_equity"]), start_equity)
                return

            clock = get(f"{TRADE_URL}/clock")
            if not clock["is_open"]:
                wait = (parse_time(clock["next_open"]) - parse_time(clock["timestamp"])).total_seconds()
                if wait > WAIT_FOR_OPEN_SEC:
                    print(f"Börse geschlossen, nächste Öffnung in {int(wait / 60)} Min. Bot beendet sich.")
                    acc = get(f"{TRADE_URL}/account")
                    summary(float(acc["equity"]), float(acc["last_equity"]), start_equity)
                    return
                print("Börse öffnet gleich, warte...", flush=True)
                time.sleep(30)
                continue

            if liquidated:
                print("Alles verkauft, warte auf Börsenschluss...", flush=True)
                time.sleep(30)
                continue

            secs_to_close = (parse_time(clock["next_close"]) - parse_time(clock["timestamp"])).total_seconds()
            acc = get(f"{TRADE_URL}/account")
            equity, cash = float(acc["equity"]), float(acc["cash"])
            last_equity = float(acc["last_equity"])

            if secs_to_close <= CLOSE_BUFFER_SEC:
                print("Börsenschluss naht, verkaufe alle Aktien.", flush=True)
                liquidate_stocks("Börsenschluss")
                liquidated = True
                time.sleep(10)
                acc = get(f"{TRADE_URL}/account")
                summary(float(acc["equity"]), last_equity, start_equity)
                continue

            held = stock_positions()
            for sym, p in held.items():
                cur = float(p["current_price"])
                peak[sym] = max(peak.get(sym, cur), cur)
            for sym in list(peak):
                if sym not in held:
                    peak.pop(sym)

            if not paused and equity <= last_equity * (1 - MAX_DAILY_LOSS):
                print("TAGESVERLUST-LIMIT erreicht: verkaufe alles und pausiere.", flush=True)
                liquidate_stocks("Tageslimit")
                held.clear()
                paused = True

            if not paused:
                for sym, p in list(held.items()):
                    gain = float(p["current_price"]) / float(p["avg_entry_price"]) - 1
                    cur = float(p["current_price"])
                    if gain <= STOP_LOSS:
                        sell(sym, "Stop-Loss", p)
                        held.pop(sym)
                    elif gain >= TRAIL_ACTIVATE and cur <= peak[sym] * (1 - TRAIL_PCT):
                        sell(sym, "Trailing-Stop", p)
                        held.pop(sym)

            if t - last_minute >= 60:
                last_minute = t
                if not paused:
                    info = analyze()
                    for sym, i in info.items():
                        if i["sig"] == "sell" and sym in held:
                            if t - entry_time.get(sym, 0) >= MIN_HOLD_SEC:
                                sell(sym, "Signal", held[sym])
                                held.pop(sym)
                    cands = [s for s, i in info.items()
                             if i["sig"] == "buy" and s not in held and i["trend_ok"]
                             and i["rsi"] < RSI_MAX and i["vol"] >= MIN_VOL
                             and t - last_trade.get(s, 0) >= COOLDOWN_SEC]
                    size = max(50.0, equity * RISK_PCT)
                    for sym in cands:
                        if len(held) >= MAX_POSITIONS or cash < size:
                            break
                        if buy(sym, size, f"RSI {info[sym]['rsi']:.0f}"):
                            held[sym] = None
                            cash -= size
                summary(equity, last_equity, start_equity)
        except Exception as e:
            print("Fehler:", e, flush=True)
        time.sleep(max(1, INTERVAL_SEC - (time.time() - t)))


run()
