#!/usr/bin/env python3
"""
Liquidity Sweep Alerts — 100% gratuit, autonome.

Réplique fidèlement l'indicateur "Liquidity Swings & Sweeps" (OutOfOptions) :
  - swings via pivots (gauche=5, droite=2), comme ta.pivothigh / ta.pivotlow
  - un niveau est "balayé" dès qu'une bougie le traverse :
        swing high  -> high > niveau   (Liquidity Sweep — Highs)
        swing low   -> low  < niveau   (Liquidity Sweep — Lows)
  - notation 💥🔥✅❄️ selon le temps pendant lequel la liquidité est restée
Les données viennent de Yahoo Finance (crypto + forex + or, sans clé API),
et les alertes partent sur Telegram. TradingView n'intervient plus du tout.

Lancer une fois (cron / GitHub Actions) :   python liquidity_alerts.py
En boucle (serveur / ta machine)        :   python liquidity_alerts.py --loop --every 300

Identifiants Telegram via l'environnement :
    export TG_BOT_TOKEN="123456:AA..."     # @BotFather -> /mybots -> API Token
    export TG_CHAT_ID="1878865956"
"""

import os
import json
import time
import argparse

import requests
import yfinance as yf

# ---------------------------------------------------------------------------
# CONFIGURATION  — modifie librement
# ---------------------------------------------------------------------------
SYMBOLS = {
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "GBPUSD": "GBPUSD=X",
    "EURUSD": "EURUSD=X",
    "XAUUSD": "GC=F",        # or (futures) comme proxy
}

INTERVAL    = "15m"   # 1m,2m,5m,15m,30m,60m,1h,1d  (ta capture montrait 15m)
LOOKBACK    = "5d"    # historique chargé à chaque passe

PIVOT_LEFT  = 5       # = leftLength de ton indicateur
PIVOT_RIGHT = 2       # = rightLength de ton indicateur

RECENT_BARS = 3       # n'alerte que pour les sweeps des X dernières bougies
                      # (évite de spammer tout l'historique au 1er lancement)
PRICE_DIFF_THRESHOLD = 10.0   # seuil "déplacement significatif" pour la note
                              # (en unités de prix ; dépend de l'instrument)

STATE_FILE = "sweep_state.json"

TG_TOKEN = os.environ.get("8994383299:AAHq20MRly2EEfGwLnKZs62Fm5Rgpa9f4Bk", "")
TG_CHAT  = os.environ.get("1878865956", "")

# minutes par bougie, pour la notation basée sur le temps
_INTERVAL_MIN = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
                 "60m": 60, "1h": 60, "90m": 90, "1d": 1440}


# ---------------------------------------------------------------------------
# DONNÉES
# ---------------------------------------------------------------------------
def fetch_candles(ticker):
    df = yf.download(ticker, period=LOOKBACK, interval=INTERVAL,
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "get_level_values"):     # colonnes multi-niveaux
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


# ---------------------------------------------------------------------------
# PIVOTS  (équivalent de ta.pivothigh / ta.pivotlow)
# ---------------------------------------------------------------------------
def pivot_highs(highs):
    """(index_formation, prix) des swing highs confirmés."""
    out, n = [], len(highs)
    for i in range(PIVOT_LEFT, n - PIVOT_RIGHT):
        h = highs[i]
        left_ok  = all(h > highs[j] for j in range(i - PIVOT_LEFT, i))
        right_ok = all(h > highs[j] for j in range(i + 1, i + PIVOT_RIGHT + 1))
        if left_ok and right_ok:
            out.append((i, float(h)))
    return out


def pivot_lows(lows):
    out, n = [], len(lows)
    for i in range(PIVOT_LEFT, n - PIVOT_RIGHT):
        l = lows[i]
        left_ok  = all(l < lows[j] for j in range(i - PIVOT_LEFT, i))
        right_ok = all(l < lows[j] for j in range(i + 1, i + PIVOT_RIGHT + 1))
        if left_ok and right_ok:
            out.append((i, float(l)))
    return out


# ---------------------------------------------------------------------------
# DÉTECTION DES SWEEPS  (logique du break-check de l'indicateur)
# ---------------------------------------------------------------------------
def detect_sweeps(df):
    """Parcourt les bougies et renvoie les événements de sweep :
       (kind, niveau, index_formation, index_declenchement)."""
    highs = df["High"].to_numpy()
    lows  = df["Low"].to_numpy()
    n = len(df)

    # un pivot formé en i devient "actif" à sa confirmation (i + PIVOT_RIGHT)
    add_high, add_low = {}, {}
    for i, p in pivot_highs(highs):
        add_high.setdefault(i + PIVOT_RIGHT, []).append((i, p))
    for i, p in pivot_lows(lows):
        add_low.setdefault(i + PIVOT_RIGHT, []).append((i, p))

    active_highs, active_lows, events = [], [], []
    for b in range(n):
        # 1) break-check d'abord (comme dans le Pine)
        kept = []
        for fi, p in active_highs:
            if highs[b] > p:
                events.append(("high_sweep", p, fi, b))
            else:
                kept.append((fi, p))
        active_highs = kept

        kept = []
        for fi, p in active_lows:
            if lows[b] < p:
                events.append(("low_sweep", p, fi, b))
            else:
                kept.append((fi, p))
        active_lows = kept

        # 2) puis on ajoute les pivots confirmés sur cette bougie
        active_highs += add_high.get(b, [])
        active_lows  += add_low.get(b, [])

    return events


def rate_sweep(kind, price, formed_i, trig_b, df):
    """Note 💥🔥✅❄️ selon le temps de repos + l'ampleur, comme l'indicateur."""
    tf_min = _INTERVAL_MIN.get(INTERVAL, 15)
    rested = (trig_b - formed_i) * tf_min          # minutes de liquidité au repos
    if kind == "high_sweep":
        pdiff = float(df["High"].iloc[trig_b]) - price
    else:
        pdiff = price - float(df["Low"].iloc[trig_b])

    if rested >= 60:
        r = "💥"
    elif rested >= 15:
        r = "🔥"
    elif rested >= 5:
        r = "🔥" if pdiff >= PRICE_DIFF_THRESHOLD else "✅"
    elif pdiff >= PRICE_DIFF_THRESHOLD:
        r = "✅"
    else:
        r = "❄️"
    return r, rested


# ---------------------------------------------------------------------------
# NOTIFICATION
# ---------------------------------------------------------------------------
def fmt_price(p):
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[!] Telegram non configuré. Message :\n" + text + "\n")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TG_CHAT, "text": text,
                                     "parse_mode": "HTML"}, timeout=15)
        if r.status_code != 200:
            print("[!] Erreur Telegram :", r.text)
    except Exception as e:
        print("[!] Échec d'envoi Telegram :", e)


# ---------------------------------------------------------------------------
# ÉTAT (anti-doublon)
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print("[!] Impossible d'écrire l'état :", e)


# ---------------------------------------------------------------------------
# BOUCLE PRINCIPALE
# ---------------------------------------------------------------------------
def run_once():
    state = load_state()
    for label, ticker in SYMBOLS.items():
        try:
            df = fetch_candles(ticker)
            if df is None or len(df) < PIVOT_LEFT + PIVOT_RIGHT + 2:
                print(f"[{label}] pas de données")
                continue

            n = len(df)
            for kind, level, formed_i, trig_b in detect_sweeps(df):
                # on n'alerte que pour les sweeps très récents
                if trig_b < n - RECENT_BARS:
                    continue

                ts = df.index[trig_b]
                key = f"{label}:{ts.isoformat()}:{kind}:{round(level, 6)}"
                if state.get(key):
                    continue

                rating, rested = rate_sweep(kind, level, formed_i, trig_b, df)
                if kind == "high_sweep":
                    emoji, desc = "🔴", "Liquidity Sweep — Highs (swing high balayé)"
                else:
                    emoji, desc = "🟢", "Liquidity Sweep — Lows (swing low balayé)"

                msg = (f"{emoji} <b>{label}</b>  {rating}\n"
                       f"{desc}\n"
                       f"Niveau pris : {fmt_price(level)}\n"
                       f"Repos liquidité : {rested} min\n"
                       f"TF {INTERVAL} · {ts:%Y-%m-%d %H:%M} UTC")
                send_telegram(msg)
                print("ALERTE :", key, rating)
                state[key] = True

        except Exception as e:
            print(f"[{label}] erreur : {e}")

    if len(state) > 800:
        state = dict(list(state.items())[-500:])
    save_state(state)


def main():
    ap = argparse.ArgumentParser(description="Liquidity sweep alerts (gratuit)")
    ap.add_argument("--loop", action="store_true",
                    help="tourne en continu au lieu d'une seule passe")
    ap.add_argument("--every", type=int, default=300,
                    help="secondes entre deux passes (avec --loop)")
    args = ap.parse_args()

    if args.loop:
        print(f"Démarrage en boucle, toutes les {args.every}s. Ctrl+C pour arrêter.")
        while True:
            run_once()
            time.sleep(args.every)
    else:
        run_once()


if __name__ == "__main__":
    main()
