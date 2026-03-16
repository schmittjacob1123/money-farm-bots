# Jacob's Money Farm — Handoff Doc
*Last updated: 2026-03-15*

---

## What This Is

Three paper-trading bots running 24/7 on an AWS Ubuntu server, managed through a homepage dashboard at `https://jacobsmoneyfarm.duckdns.org`.

| Bot | Strategy | Budget | Version |
|-----|----------|--------|---------|
| **Jacob** 🦎 | US stocks, long-only, MA50 + daily RSI14, AI confirmed | $500 | v5 |
| **Seraphina** 🐱 | Crypto (BTC/ETH/SOL/DOGE), MA50 + RSI + funding rate | $1,000 | v9 |
| **Loachy** 🐟 | Sports betting, de-vigged edge detection, AI veto | $50 | v4 |

All bots are in **DRY_RUN=true** (paper mode). No real money is being traded.

---

## Infrastructure

| Thing | Detail |
|-------|--------|
| Server | AWS EC2, Ubuntu, `/home/ubuntu/` |
| Homepage | `https://jacobsmoneyfarm.duckdns.org` |
| Control API | `farm_api.py` on port 5000, screen session `farmapi` |
| Git repo | `https://github.com/schmittjacob1123/money-farm-bots` |
| Screen sessions | `loachy`, `seraphina`, `jacob`, `farmapi` |
| Deploy flow | Edit locally → `git push` → SSH `git pull` on server → restart affected bot |

**Key files on server (not in repo):**
- `/home/ubuntu/.env` — all API keys (never commit this)
- `*_state.json` — wallet state per bot
- `*_data.json` — dashboard data per bot

---

## Bot Details

### Jacob v5 (`jacob_bot.py`)
- **Watchlist:** 20 tickers across tech, finance, energy, healthcare sectors
- **Entry:** RSI < 40 AND price above MA50 AND score ≥ 68 AND not BEAR regime AND AI confirmed ≥ 75% confidence
- **Exit:** TP +2.5%, RSI > 65, stop-loss -6%, trailing stop (arms at +2.5%, fires at -1.5% from peak), quick-cut -5% in 24h, max hold 5 days
- **Sizing:** 10% of cash per trade, max $80, min $5
- **Regime:** SPY + QQQ 5-day momentum → BEAR/NEUTRAL/BULL (no new longs in BEAR)
- **Data:** Yahoo Finance daily candles (3mo range, 1d interval), 4.5min cache
- **Scan interval:** Every 15min during market hours (9:30am–4pm ET, weekdays)
- **Note:** farm_api was previously pointing to old `polymarket_bot.py` — now fixed to `jacob_bot.py` on screen `jacob`

### Seraphina v9 (`seraphina_bot.py`)
- **Coins:** BTC, ETH, SOL, DOGE (via Kraken public OHLC API)
- **Entry:** RSI < 35 AND price above MA50 (50-period on 1h candles)
- **Exit:** TP +2.5%, RSI > 65, stop-loss -4%, trailing stop (arms at +1.5%, fires at -1% from peak)
- **Sizing:** 20% of cash per trade, max 4 open positions simultaneously
- **Funding rate:** Collects income every 8h when Binance funding rate > 0.03%/8h
- **Circuit breaker:** Pauses if portfolio drops 15% from peak
- **Scan interval:** Every hour, 24/7
- **Status as of handoff:** 81 scans, 0 trades — crypto RSI elevated (60-72), waiting for dip

### Loachy v4 (`loachy_bot.py`)
- **Sports:** NCAAB, NBA, NHL, MLB, EPL (trimmed from 9 — NFL/NCAAF off-season, MMA/MLS thin coverage)
- **Edge detection:** Additive de-vig — averages implied probs per outcome across books, normalizes to true probability, edge = true_prob − to_prob(best_price)
- **Gates:** ≥2% real edge AND ≥3 books confirming price AND odds between -280 and +200
- **AI role:** Veto mechanism (not confidence generator) — vetoes on injuries, weather, back-to-backs
- **Kelly sizing:** Fractional Kelly using live wallet balance (not fixed budget), sport-specific fractions
- **Pending system:** Borderline picks go to pending (55%+ confidence), auto-bet at 72%+, longshots always pending
- **Parlay builder:** Suggests 2-leg parlays from high-confidence confirmed picks
- **CLV tracking:** Tracks closing line value to measure long-term edge quality
- **Scan interval:** Every hour, 24/7
- **Odds API:** `the-odds-api.com` — ~4 credits/scan, ~96/day. Free tier = 500 credits (~5 days). Swap key from homepage banner.
- **Status as of handoff:** Running, valid API key (464 credits), finding 47 games/scan, 0 qualifying edges yet (normal — Sunday night, games in progress)

---

## Homepage (`index.html`)

- Live stats cards for all 3 bots, updates every 30 seconds
- Start/stop/reset toggles for each bot (requires farm password)
- **Easter eggs:** Konami code (↑↑↓↓←→←→BA) → DEGEN MODE, click characters for animations, secret words: `farm`, `money`, `loach`, `jacob`, `purr`, `yolo`
- **Odds API credit banner:** Appears at 150 / 50 / 0 credits with SWAP KEY button
- **Key swap modal:** Paste new Odds API key → writes to `.env` → restarts Loachy automatically
- **Loachy next-scan countdown:** Live ticker counting down to next scan, turns yellow in last 2 min

---

## Known Issues / Watch List

| Issue | Severity | Notes |
|-------|----------|-------|
| Odds API credits burn fast | Medium | ~5 days per free account. Swap via homepage modal. |
| MLB/EPL not fetching for Loachy | Low | Old `loachy_sports_config.json` on server may not include new sports. Delete it and let it regenerate. |
| `datetime.utcnow()` deprecation warnings | Low | Python 3.12 warning, no functional impact. Fix eventually by switching to `datetime.now(UTC)`. |
| Jacob never started yet | — | Pull latest on server (`git pull`), then start from homepage toggle. |
| `nextScanAt` null on Loachy scan 1 | Low | Cosmetic — shows correctly from scan 2 onward. |

---

## Roadmap / Future Ideas

### High Priority
- [ ] **Turn on Jacob** — pull latest on server, start from homepage, verify $500 budget loads correctly
- [ ] **Fix MLB/EPL for Loachy** — delete `loachy_sports_config.json` on server so it regenerates with new 5-sport list
- [ ] **Add force-scan button** to homepage (currently need SSH `--once` to trigger manually)

### Medium Priority
- [ ] **Loachy: add NCAAF/NFL back** when season starts (August). Update sports list and PRIORITY.
- [ ] **Jacob: live trading** — when paper results look good, flip `DRY_RUN=false` and add brokerage API (Alpaca recommended)
- [ ] **Seraphina: live trading** — flip to live when paper results are solid, use Kraken API keys
- [ ] **Fix `datetime.utcnow()` deprecation** across all bots — replace with `datetime.now(timezone.utc)`
- [ ] **Jacob trigger file** — already implemented (`jacob_trigger.json`). Add homepage button to trigger early scan.
- [ ] **Loachy trigger file** — not implemented yet. Add same mechanism as Jacob for manual force-scan.

### Low Priority / Nice to Have
- [ ] **Email/SMS alerts** — `farm_alerts.py` exists but unclear if configured. Wire up for big wins, losses, or errors.
- [ ] **Parlay approval UI** — Loachy suggests parlays but there's no UI to approve them on the homepage
- [ ] **Pending bet approval UI** — Loachy pending bets need homepage UI to approve/reject (endpoint exists at `/api/pending-approve`)
- [ ] **Jacob sector breakdown** — show open positions by sector on homepage
- [ ] **Seraphina funding rate history** — graph cumulative funding income over time
- [ ] **Rotate Odds API keys automatically** — build key pool in `.env`, auto-rotate when credits hit 0

---

## Useful Commands (SSH)

```bash
# Check what's running
screen -ls

# Attach to a bot's output
screen -r loachy
screen -r seraphina
screen -r jacob

# Restart a bot
screen -X -S loachy quit && screen -dmS loachy python3 /home/ubuntu/loachy_bot.py

# Force a one-off scan (doesn't affect running instance)
python3 /home/ubuntu/loachy_bot.py --once

# Pull latest code
cd /home/ubuntu && git pull

# Restart control API
screen -X -S farmapi quit && screen -dmS farmapi python3 /home/ubuntu/farm_api.py

# Edit API keys
nano /home/ubuntu/.env
```

---

## API Keys Needed (in `/home/ubuntu/.env`)

```
ANTHROPIC_API_KEY=...     # Claude AI — used by Jacob + Loachy for confirmations
ODDS_API_KEY=...          # the-odds-api.com — Loachy sports odds
DRY_RUN=true              # set to false to go live (don't until paper results are good!)
FARM_PASSWORD=...         # homepage login password
```
