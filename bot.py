"""
Minuten-Bot für GitHub Actions (Alpaca Paper = Demo-Konto).
SMA-Crossover auf 1-Minuten-Kerzen, 20 Symbole, Zusammenfassung jede Minute,
Liquidation kurz vor Börsenschluss. Keys kommen aus GitHub Secrets.
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta

import requests

API_KEY = os.environ.get("ALPACA_KEY")
API_SECRET = os.environ.get("ALPACA_SECRET")
if not API_KEY or not API_SECRET:
    sys.exit("FEHLER: Secrets ALPACA_KEY / ALPACA_SECRET fehlen.")

MODE = os.environ.get("MODE", "run")   # "run" oder "sell_all"

SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META",
           "TSLA", "AMD", "NFLX", "AVGO", "JPM", "XOM", "COST", "INTC", "PLTR", "SOFI"]
QTY = 10
FAST, SLOW = 5, 20
INTERVAL_SEC = 60
CLOSE_BUFFER_SEC = 150      # so viele Sekunden vor Börsenschluss wird alles verkauft
MAX_RUNTIME_SEC = 20700     # 5 h 45 min, dann Übergabe an den nächsten Lauf
WAIT_FOR_OPEN_SEC = 900     # nur warten, wenn die Börse in <= 15 Min öffnet

TRADE_URL = "https://paper-api.alpaca.markets/v2"
DATA_URL = "https://data.alpaca.markets/v2"
HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}

trades_total = 0
buys = 0
sells = 0


def get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def sma(values, n):
    v = values[-n:]
    return sum(v) / len(v)


def parse_time(s):
    return datetime.fromisoformat(re.sub(r"\.\d+", "", s))


def seconds_between(a, b):
    return (parse_time(b) - parse_time(a)).total_seconds()


def all_signals():
    start = (datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = get(f"{DATA_URL}/stocks/bars", {
        "symbols": ",".join(SYMBOLS), "timeframe": "1Min",
        "start": start, "limit": 10000, "feed": "iex"})
    signals = {}
    for sym, bars in (data.get("bars") or {}).items():
        closes = [b["c"] for b in bars]
        if len(closes) < SLOW + 1:
            continue
        fn, sn = sma(closes, FAST), sma(closes, SLOW)
        fp, sp = sma(closes[:-1], FAST), sma(closes[:-1], SLOW)
        if fp <= sp and fn > sn:
            signals[sym] = "buy"
        elif fp >= sp and fn < sn:
            signals[sym] = "sell"
    return signals


def buy(sym):
    global trades_total, buys
    r = requests.post(f"{TRADE_URL}/orders", headers=HEADERS, timeout=15, json={
        "symbol": sym, "qty": str(QTY), "side": "buy",
        "type": "market", "time_in_force": "day"})
    if r.ok:
        trades_total += 1
        buys += 1
        print("  KAUF", QTY, "x", sym)
    else:
        print("  Kauf fehlgeschlagen", sym, r.text[:100])


def sell(sym):
    global trades_total, sells
    r = requests.delete(f"{TRADE_URL}/positions/{sym}", headers=HEADERS, timeout=15)
    if r.ok:
        trades_total += 1
        sells += 1
        print("  VERKAUF", sym)
    else:
        print("  Verkauf fehlgeschlagen", sym, r.text[:100])


def liquidate_all():
    global trades_total, sells
    r = requests.delete(f"{TRADE_URL}/positions", headers=HEADERS, timeout=30,
                        params={"cancel_orders": "true"})
    if r.ok:
        n = len(r.json())
        trades_total += n
        sells += n
        print(f"  ALLES VERKAUFT: {n} Positionen")
    else:
        print("  Liquidation fehlgeschlagen:", r.text[:150])


def summary(start_equity):
    acc = get(f"{TRADE_URL}/account")
    positions = get(f"{TRADE_URL}/positions")
    equity = float(acc["equity"])
    diff = equity - start_equity
    unreal = sum(float(p["unrealized_pl"]) for p in positions)
    print("-" * 34)
    print(datetime.utcnow().strftime("%H:%M:%S"), "UTC  ZUSAMMENFASSUNG")
    print(f"Kontowert:  {equity:,.2f} $")
    print(f"Seit Start: {diff:+,.2f} $ ({diff / start_equity * 100:+.3f} %)")
    print(f"Cash:       {float(acc['cash']):,.2f} $")
    print(f"Trades:     {trades_total} (Käufe {buys}, Verkäufe {sells})")
    print(f"Offene Positionen: {len(positions)} | offener G/V: {unreal:+,.2f} $")
    for p in positions:
        print(f"  {p['symbol']}: {p['qty']} Stk., G/V {float(p['unrealized_pl']):+,.2f} $")
    print("-" * 34, flush=True)


def run():
    t0 = time.time()
    start_equity = float(get(f"{TRADE_URL}/account")["equity"])
    print(f"Verbindung OK. Startwert: {start_equity:,.2f} $ | Modus: {MODE}", flush=True)

    if MODE == "sell_all":
        liquidate_all()
        time.sleep(5)
        summary(start_equity)
        return

    liquidated = False
    while True:
        loop_start = time.time()
        try:
            if loop_start - t0 > MAX_RUNTIME_SEC:
                print("Maximale Laufzeit erreicht, Übergabe an den nächsten Lauf.")
                summary(start_equity)
                return

            clock = get(f"{TRADE_URL}/clock")

            if not clock["is_open"]:
                wait = seconds_between(clock["timestamp"], clock["next_open"])
                if wait > WAIT_FOR_OPEN_SEC:
                    print("Börse geschlossen, nächste Öffnung in",
                          int(wait / 60), "Min. Bot beendet sich.")
                    summary(start_equity)
                    return
                print("Börse öffnet gleich, warte...", flush=True)
                time.sleep(30)
                continue

            if liquidated:
                print("Alles verkauft, warte auf Börsenschluss...", flush=True)
                time.sleep(30)
                continue

            if seconds_between(clock["timestamp"], clock["next_close"]) <= CLOSE_BUFFER_SEC:
                print("Börsenschluss naht, verkaufe alles.")
                liquidate_all()
                liquidated = True
                time.sleep(10)
                summary(start_equity)
                continue

            held = {p["symbol"] for p in get(f"{TRADE_URL}/positions")}
            for sym, sig in all_signals().items():
                if sig == "buy" and sym not in held:
                    buy(sym)
                elif sig == "sell" and sym in held:
                    sell(sym)
            summary(start_equity)
        except Exception as e:
            print("Fehler:", e, flush=True)
        time.sleep(max(1, INTERVAL_SEC - (time.time() - loop_start)))


run()
