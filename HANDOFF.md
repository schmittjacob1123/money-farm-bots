# Money Farm — Handoff Document
**Last updated: 2026-03-31**

---

## Farm Overview

Two live bots trading paper money. One on crypto futures (Seraphina), one on Polymarket prediction markets (Vesper). All infrastructure runs on a single AWS EC2 instance.

- **Live URL:** https://jacobsmoneyfarm.duckdns.org
- **EC2 IP:** 98.80.2.233 (Ubuntu)
- **GitHub:** https://github.com/schmittjacob1123/money-farm-bots
- **SSH:** `ssh -i polymarket-bot.pem ubuntu@98.80.2.233`

---

## Server Architecture

```
/home/ubuntu/               ← nginx web root (what the browser loads)
  index.html
  seraphina_bot.py
  seraphina_dashboard.html
  seraphina_state.json
  seraphina_data.json
  seraphina_daily.json
  seraphina.log
  vesper_bot.py
  vesper_dashboard.html
  vesper_state.json
  vesper_data.json
  vesper_daily.json
  farm_api.py
  login.html

/home/ubuntu/money-farm-bots/   ← Git repo (version control only)
  vesper.log
  archive/jacob/
  archive/loachy/
  seraphina_dashboard.html      (git-tracked copy)
  vesper_dashboard.html         (git-tracked copy)
  vesper_bot.py                 (git-tracked copy)
  seraphina_bot.py              (git-tracked copy)
  ...
```

> **CRITICAL:** nginx serves from `/home/ubuntu/` — NOT from the git repo.
> Always SCP files to `/home/ubuntu/<file>` when deploying. The git repo is for version control only and does NOT auto-sync with the web root.

---

## Screen Sessions

| Session | Purpose |
|---|---|
| `seraphina` | Seraphina bot process |
| `vesper` | Vesper bot process |
| `farmapi` | Flask API on port 5000 |
| `deploy` | Auto-deploy watcher |

**Check sessions:** `screen -ls`
**Attach:** `screen -r seraphina`
**Detach:** `Ctrl+A, D`

**Restart Seraphina:**
```bash
screen -S seraphina -X stuff $'\003'
sleep 1
cd /home/ubuntu && screen -dmS seraphina bash -c 'python3 seraphina_bot.py 2>&1 | tee seraphina.log'
```

**Restart Vesper:**
```bash
screen -S vesper -X stuff $'\003'
sleep 1
cd /home/ubuntu && screen -dmS vesper bash -c 'python3 vesper_bot.py 2>&1 | tee money-farm-bots/vesper.log'
```

**Wipe Vesper state and restart fresh:**
```bash
rm -f /home/ubuntu/vesper_state.json /home/ubuntu/vesper_data.json /home/ubuntu/vesper_daily.json
# then restart as above
```

---

## Deploying a Change

1. Edit locally
2. SCP to web root: `scp -i polymarket-bot.pem <file> ubuntu@98.80.2.233:/home/ubuntu/<file>`
3. SCP to git repo: `scp -i polymarket-bot.pem <file> ubuntu@98.80.2.233:/home/ubuntu/money-farm-bots/<file>`
4. Restart the screen session for any bot file changes
5. Commit and push locally: `git add <file> && git commit && git push origin main`

---

## Bot 1: Seraphina

**Strategy:** Crypto grid trading with RSI momentum filter and bear mode.

**Paper budget:** $1,000 | **Status:** Running

**Current state (2026-03-31):**
- Portfolio: ~$885 (peaked at $1,001 — briefly profitable)
- Total PnL: -$76 | Win rate: 31%
- Total trades: ~378 | Total fees paid: $73
- 7 open longs (BTC, ETH, SOL, DOGE mix) — all coins in uptrend

**How it works:**
- Builds a price grid per coin (1% spacing, 8 levels above/below center)
- Buys when price crosses a grid level downward
- Exits via: 3.5% TP, 3% SL, trailing stop (arms at +1.5%), or RSI overbought
- Bear mode: when price is ≥1% below MA50 and RSI > 50, flips to shorting rallies
- Hard gate: no long entries when price is below MA50 (v12 fix)

**Key CONFIG:**
```python
"rsi_buy_max":        70    # no buys above RSI 70
"rsi_sell_min":       73    # RSI exit threshold — MUST stay above rsi_buy_max
"rsi_ob_min_hold_m":  20    # RSI exit requires 20-min hold (prevents instant churn loops)
"take_profit_pct":    0.035
"stop_loss_pct":      0.030
"grid_spacing_pct":   0.010
"max_open_per_coin":  2
"max_open_total":     12
```

**Important — why rsi_sell_min must be > rsi_buy_max:**
If `rsi_sell_min` (65) < `rsi_buy_max` (70), there's an overlap zone where the bot buys AND immediately wants to exit. Price barely ticks up → unrealized PnL > 0 → instant exit. Net result: ~-$0.07 fee per round trip, repeated dozens of times per hour. This cost $73 in fees before being fixed. Keep `rsi_sell_min` at 73 or higher.

---

## Bot 2: Vesper

**Strategy:** Polymarket prediction market oracle. Finds price inefficiencies using NOAA weather data, price momentum, volume spikes, and mean reversion.

**Paper budget:** $500 | **Status:** Running (clean restart 2026-03-31)

**Current positions (2026-03-31):**
- Will Bitcoin dip to $45,000 by Dec 31, 2026? (momentum)
- San Diego Padres win 2026 NLCS? (reversion)
- Milwaukee Brewers win 2026 NLCS? (reversion)
- San Antonio Spurs win 2026 NBA Finals? (volume)
- Ronaldo Caiado win 2026 Brazilian election? (volume)
- Will Israel strike 4 countries in 2026? (volume)
- Will Bitcoin dip to $66,000 March 30-April 5? (volume)
- Will Bitcoin dip to $64,000 March 30-April 5? (volume)

**Signal types (in priority order):**
1. **Weather** — NOAA precip probability vs market price (two-step API: `/points/{lat},{lon}` → forecast URL → parse)
2. **Momentum** — 1-day price change >3% = directional signal
3. **Reversion** — 1-week price change >10% = mean reversion candidate
4. **Volume** — 24h volume >1.8x 7-day average = market interest spike

**Key CONFIG:**
```python
"max_open_positions": 8
"position_size_usd":  35.0
"min_edge":           0.04    # 4% edge minimum
"take_profit_pct":    0.25    # 25% TP
"stop_loss_pct":      0.15    # 15% SL
"max_hold_hours":     72
"min_end_hours":      24      # skip markets resolving within 24h
"momentum_threshold": 0.03
"volume_spike_mult":  1.8
"signal_refresh_s":   300     # refresh signals every 5 min (matches scan interval)
```

**Prop keyword filter:** Skips any market whose question contains:
`points o/u, rebounds o/u, assists o/u, steals o/u, blocks o/u, threes o/u, turnovers o/u, pts o/u, reb o/u, ast o/u, passing yards, rushing yards, receiving yards, touchdowns o/u, strikeouts o/u, hits o/u, home runs o/u`

**Gamma API quirk:** `outcomePrices` and `outcomes` are returned as JSON-encoded strings, not lists. Must call `json.loads()` on them if they're of type `str`. This was a silent bug that caused 0 signals on first run.

**APIs used (both free, no key needed):**
- Polymarket Gamma: `https://gamma-api.polymarket.com/markets`
- NOAA Weather: `https://api.weather.gov/` (requires `User-Agent` header)

---

## Archived Bots

Stopped, hidden from homepage. Code in `/home/ubuntu/money-farm-bots/archive/`.

- `archive/jacob/` — Jacob bot (original Polymarket bot)
- `archive/loachy/` — Loachy bot

To reactivate: copy files to `/home/ubuntu/`, re-add to `BOTS` dict in `farm_api.py`, start a screen session, and unhide the building/card in `index.html`.

---

## Homepage (index.html)

- 2-bot grid: Seraphina and Vesper
- Jacob and Loachy hidden via `display:none` on their buildings, stat chips, and cards
- Footer: "TWO BOTS · ONE MISSION · ONE FARM"

**Dashboard navigation (all active pages link to each other, no links to archived bots):**
- Seraphina dashboard: Farm | Vesper
- Vesper dashboard: Farm | Seraphina

---

## Farm API (farm_api.py)

Flask on port 5000, proxied via nginx. Active bots in BOTS dict: seraphina, vesper. Jacob and Loachy commented out.

---

## GitHub PAT

The token previously hardcoded in `farm_deploy.html` was exposed and is compromised. Go to GitHub → Settings → Developer Settings → Personal Access Tokens → revoke old token, create new one with `repo` scope. Paste into the deploy tool at the farm URL when needed.

---

## What to Watch Next

**Seraphina:**
- Win rate should climb toward 40%+ now that RSI churn is fixed
- Fee drain should drop sharply — watch `totalFees` in the dashboard
- If BTC/ETH/SOL drop below MA50, bear mode activates and she'll start shorting

**Vesper:**
- Watch for weather signals appearing — they're highest quality but rare
- Positions are held up to 72h, so give it a few days before judging P&L
- If it keeps filling slots with volume signals and no weather signals appear, may need to loosen weather signal detection or check NOAA data parsing
