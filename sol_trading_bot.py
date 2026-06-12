#!/usr/bin/env python3
"""APEX SNIPER BOT - Advanced Paper Trading Bot"""

import asyncio as _asyncio
import time as _time
import json as _json
import urllib.parse as _urlparse
import re as _re
import logging
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)
import httpx
import io
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    MATPLOTLIB_OK = True
except ImportError:
    MATPLOTLIB_OK = False
try:
    from PIL import Image, ImageDraw, ImageFont
    PILLOW_OK = True
except ImportError:
    PILLOW_OK = False

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var not set. Add it in Railway → Variables.")

# ── ADMIN ACCESS ──────────────────────────────────────────────────────────────
# Set ADMIN_IDS in Railway env vars as comma-separated Telegram user IDs.
# Example: ADMIN_IDS=123456789,987654321
_admin_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set = {int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit()}

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ── ACCESS CONTROL ────────────────────────────────────────────────────────────
# When ACCESS_CONTROL_ENABLED=true, new users must be approved by admin.
# Set ACCESS_ADMIN_USERNAME in Railway env vars (without @), e.g. auracle_x
# Admins (ADMIN_IDS) are always approved automatically.
ACCESS_ADMIN_USERNAME   = os.getenv("ACCESS_ADMIN_USERNAME", "auracle_x")
ACCESS_CONTROL_ENABLED  = os.getenv("ACCESS_CONTROL_ENABLED", "true").lower() == "true"


# Tracks users waiting for admin approval: uid -> {"name": str, "username": str}
_pending_access: dict = {}


DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens/{}"
PRICE_CHECK_INTERVAL      = 20   # standard holdings checker
APEX_PRICE_CHECK_INTERVAL = 8    # APEX positions — faster, uses Helius when available
MAX_BALANCE = 10_000.0
MIN_BALANCE = 1.0
SNIPER_SEEN_EXPIRY_H = 2.0   # don't re-signal same token for 2h once seen (was 24h — caused silence)

# ── RugCheck rate limiter — max 3 concurrent calls to avoid 429s ─────────────
# Initialized in post_init (event loop guaranteed running). Never created at
# module level — asyncio.Semaphore must be created inside a running event loop.
_rugcheck_semaphore: "_asyncio.Semaphore | None" = None

async def _get_rugcheck_semaphore() -> "_asyncio.Semaphore":
    """Return the shared RugCheck semaphore. Always initialized by post_init
    before any handler runs, so the None branch is a last-resort safety net."""
    global _rugcheck_semaphore
    if _rugcheck_semaphore is None:
        # Should never happen after post_init, but guard anyway
        _rugcheck_semaphore = _asyncio.Semaphore(2)
    return _rugcheck_semaphore
SNIPER_LOG_MAX = 200          # max sniper log entries per user

# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# SIMULATED TRADING COSTS — mirrors real Solana DEX costs for accurate P&L
# ══════════════════════════════════════════════════════════════════════════════
# Every buy and sell deducts these costs so paper trading results match live.
#
# Real Solana costs per trade:
#   Gas (network fee)   : ~$0.002  flat per tx
#   DEX fee (PumpSwap)  : 0.25%    of trade size (LP fee)
#   Slippage (micro-cap): 0.5%     avg on thin liq tokens ($10K–$500K MC)
#   ──────────────────────────────────────────────────────
#   Total per trade     : ~0.75% + $0.002
#   Round trip (buy+sell): ~1.5% + $0.004
#
# Change SIM_FEES_ENABLED = False to disable for comparison.
SIM_FEES_ENABLED    = True
SIM_GAS_USD         = 0.002    # flat SOL network fee per transaction
SIM_DEX_FEE_PCT     = 0.0025   # 0.25% — PumpSwap/Raydium LP fee
SIM_SLIPPAGE_PCT    = 0.005    # 0.50% — avg slippage on micro-cap Solana tokens
SIM_TOTAL_PCT       = SIM_DEX_FEE_PCT + SIM_SLIPPAGE_PCT  # 0.75% per trade

# ── APEX Re-entry Watchlist constants ─────────────────────────────────────────
WATCHLIST_EXPIRY_S    = 43200   # 12h — auto-expire dead/dormant tokens
WATCHLIST_CHECK_EVERY = 120     # check each token every 2 min (save API credits)
WATCHLIST_BOUNCE_PCT  = 0.15    # price must bounce 15%+ from post-exit bottom
WATCHLIST_BUY_PCT_MIN = 52      # buy pressure must recover above 52% on M5
WATCHLIST_LIQ_DROP_MAX= 0.15    # liq must stay within 15% of exit liq
WATCHLIST_VOL_MIN     = 0.50    # vol_m5 must be >= 50% of avg baseline

# APEX — Autonomous Profit & Exit eXecution  (v1.0)
# ══════════════════════════════════════════════════════════════════════════════
APEX_TRAIL_ACTIVATE_X   = 1.5
APEX_TRAIL_PCT_EARLY    = 0.22   # 1.5x-2x:  widened from 0.15 — micro-caps need room to breathe
APEX_TRAIL_PCT_MID      = 0.12   # 2x-5x:    was 0.15
APEX_TRAIL_PCT_HIGH     = 0.10   # 5x-10x:   was 0.12
APEX_TRAIL_PCT_MOON     = 0.07   # 10x+:     was 0.08
APEX_LOCK_2X_PCT        = 0.50
APEX_LOCK_5X_PCT        = 0.75
APEX_HEAT_SAFE          = 0.40
APEX_HEAT_CAUTION       = 0.60
APEX_HEAT_STOP          = 0.80
APEX_MAX_POSITIONS      = 999  # unlimited
APEX_DAILY_LOSS_LIMIT   = 0.20
APEX_CONFIRM_WAIT_S     = 45
APEX_DRAWDOWN_1_MULT    = 0.70
APEX_DRAWDOWN_2_MULT    = 0.50
APEX_DRAWDOWN_3_PAUSE   = 30
APEX_MIN_CONFIDENCE     = 6    # raised from 3 — prevents low-confidence trades poisoning learning memory
APEX_SELF_LEARN_WINDOW  = 50

# ── APEX Risk Profiles ────────────────────────────────────────────────────────
# Hunter uses a RATCHET model — capital is protected tightly on entry.
# Wide stops only apply AFTER the token proves itself by moving in your favour.
# This stops the "lose 35% on every loser" problem of naive wide-stop modes.
#
# Ratchet phases (Hunter only):
#   Phase 0  cx < 1.3x  : Default-tight SL (20/16/12%). Immediate RED exit.
#                          Protect capital. Don't give bad tokens extra rope.
#   Phase 1  cx >= 1.3x : SL ratchets to -5% from entry (near break-even).
#                          RED now needs 2× consecutive cycles to exit.
#   Phase 2  cx >= 1.6x : SL locks to entry price (never lose money on winner).
#                          Trail activates at wide %. Playing with profit.
#   Phase 3  cx >= 2.5x+: Trail widens further. Full house-money mode.
#
# The net effect: losers are cut at -12 to -20% (same as Default).
#                 winners get room to run to 10x, 50x, 100x.
APEX_PROFILES = {
    "default": None,   # None = use raw APEX_* constants (unchanged behaviour)
    "hunter": {
        # ── Entry protection (Phase 0, before token proves itself) ──────────
        "sl_low":            18.0,   # LOW rug risk initial stop  (was 20 — tightened)
        "sl_med":            14.0,   # MEDIUM rug risk            (was 16)
        "sl_high":           10.0,   # HIGH rug risk              (was 12)
        # ── Ratchet thresholds ───────────────────────────────────────────────
        "ratchet_1x":        1.25,   # at 1.25x: SL moves to -4% from entry (was 1.3x)
        "ratchet_2x":        1.50,   # at 1.50x: SL locks to entry break-even (was 1.6x)
        "red_2x_start":      1.25,   # after 1.25x: require 2x RED before exit
        # ── Trail (only active once position is profitable) ──────────────────
        # CRITICAL FIX: trail_activate_x MUST match ratchet_2x so Hunter never
        # enters the trail block while the ratchet SL is still in Phase 0/1.
        # Old value was 1.6x but ratchet_2x was also 1.6x — the default trail
        # (APEX_TRAIL_PCT_EARLY=18%) could fire via the Default path at 1.5x,
        # bypassing Hunter logic entirely. Now both are 1.5x and trail only
        # activates after break-even is locked.
        "trail_activate_x":  1.50,   # must equal ratchet_2x
        "trail_1_6":         0.22,   # 1.5x-2x:  was 0.30 — too wide, gave back gains
        "trail_2_0":         0.28,   # 2x-5x:    was 0.38 — tightened
        "trail_5_0":         0.20,   # 5x-10x:   was 0.28
        "trail_10":          0.14,   # 10x+:     was 0.18
        # ── Entry gate (Hunter is selective) ─────────────────────────────────
        "min_confidence":     6,     # only 6+/10 confidence tokens
        "min_score":         58,     # only 58+/100 score tokens
        "daily_loss_limit":   0.20,  # 20% daily loss tolerance (was 25% — too loose)
        "heat_stop":          0.75,  # tighter heat cap than default (was 0.80)
        # ── ORANGE/YELLOW trail tightening ───────────────────────────────────
        "orange_tighten":     0.08,  # tighter on orange (was 0.10)
        "yellow_tighten":     0.12,  # tighter on yellow (was 0.15)
        # ── SR multiplier cap: prevents SR from widening trail above floor ───
        # Without this, apex_sr_trail_multiplier can push the trail stop BELOW
        # the ratchet floor, defeating the entire break-even protection.
        "sr_mult_cap":        1.0,   # SR never widens Hunter trail (cap at 1x)
    },
}


# ── HUNTER MODE SUSPENSION ────────────────────────────────────────────────────
# Set True to force all users onto Default profile while Hunter is being fixed.
# No settings are lost — apex_risk_profile stays stored, this just bypasses it.
APEX_HUNTER_SUSPENDED = True

def get_apex_profile(ud: dict) -> dict | None:
    """Return the Hunter profile dict, or None for default behaviour.
    While APEX_HUNTER_SUSPENDED is True, always returns None (=default mode)."""
    if APEX_HUNTER_SUSPENDED:
        return None
    name = ud.get("apex_risk_profile", "default")
    return APEX_PROFILES.get(name)   # None = default, dict = named profile


def _hunter_sl_for_cx(h: dict, cx: float, p: dict) -> float | None:
    """
    Return the effective stop-loss floor for Hunter mode given current cx.
    Returns a price (not pct), or None if no floor applies yet.
    Uses the ratchet to step stop-loss UP as the position gains.
    """
    avg = h.get("avg_price", 0)
    if avg <= 0:
        return None
    if cx >= p["ratchet_2x"]:
        # Phase 2: floor at entry price (break-even). Never lose on a winner.
        return avg * 1.005           # entry + 0.5% cushion (covers spread)
    if cx >= p["ratchet_1x"]:
        # Phase 1: floor at -5% from entry. Almost break-even.
        return avg * 0.95
    # Phase 0: standard pct stop handled by existing stop_loss_pct mechanism
    return None


def _hunter_trail_pct(cx: float, p: dict) -> float:
    """Wide trail pcts for Hunter — only used once trail_activate_x is reached."""
    if cx >= 10.0: return p["trail_10"]
    if cx >= 5.0:  return p["trail_5_0"]
    if cx >= 2.0:  return p["trail_2_0"]
    return p["trail_1_6"]
_apex_entry_queue: dict  = {}
_apex_paused_until: dict = {}
_competitions: dict = {}    # code → comp dict — persisted via autosave
_apex_learn_memory: dict = {}
# Post-exit tracker: uid -> {contract -> {exit_price, exit_reason, exit_at, symbol,
#   entry_price, snapshots:[{ts,price,x_vs_exit,x_vs_entry,checked_at}]}}
_apex_post_exit: dict = {}
# Re-entry Watchlist: uid -> {contract -> {symbol, exit_price, exit_liq, entry_price,
#   exit_reason, exit_x, exit_at, bottom_price, bottom_ts, reversal_alerted,
#   re_entry_queued, last_check_ts, status: watching|reversed|dead|expired}}
_apex_watchlist: dict = {}
_apex_last_check: dict = {}  # uid -> {contract -> last_check_timestamp}
# Rejected token outcome tracker: uid -> {contract -> {symbol, mc_at_reject, reject_reason,
#   reject_ts, score, chain, checked_24h, outcome_x_24h}}
# Tracks near-miss rejections (score/MC boundary) — checks 24h later if they pumped.
_apex_rejected:  dict = {}
_user_locks:        dict = {}  # asyncio.Lock per uid — prevents concurrent buy/sell


from persistence import load_all, save_user, save_trade_log, autosave_job
_http: httpx.AsyncClient | None = None

async def get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=8, limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    return _http

# ── Token price cache (12s TTL) ──────────────────────────────────────────────
_token_cache: dict = {}
CACHE_TTL = 30.0   # seconds — raised to 30s to reduce DexScreener 429s during sniper scans
_ch_card_cache: dict = {}   # {contract[:32]: {channel_id, msg_id, info, sc, ai, expanded}}
_sol_price_cache: dict = {"price": 150.0, "ts": 0.0}  # cached SOL/USD, refreshed hourly

# ── RugCheck cache (5 min TTL) ────────────────────────────────────────────────
# RugCheck security data (mint, freeze, LP burn, holders, risk flags) doesn't
# change within seconds — caching for 5 minutes reduces API calls by ~25x and
# eliminates the 429s seen in production. On a 429 with no cache entry the token
# is flagged rc_rate_limited=True so users can see it in the sniper log.
_rugcheck_cache: dict = {}
RUGCHECK_CACHE_TTL = 300.0   # 5 minutes

# ── Dump log channel queue ────────────────────────────────────────────────────
# Instead of blasting 200 messages instantly (triggers Telegram 429 rate-limit),
# SKIP and SNIPE messages are pushed here and drained by dump_log_drain_job.
# Telegram channel limit: ~20 messages/minute per bot.
# Drain at 3 messages every 4 seconds = ~45 messages/minute — safely under limit.
# A 100-token cycle delivers in ~2.5 minutes instead of hitting flood control.
# Structure: {uid: deque([{chat_id, text, disable_notification}, ...])}
import collections as _collections
_dl_queue: dict = {}          # uid -> deque of pending message dicts
DL_BATCH_SIZE  = 3            # messages per drain cycle (3/4s = ~45/min, under Telegram's ~20/min per channel)
DL_DRAIN_EVERY = 4            # seconds between drain cycles
_dl_flood_until: dict = {}    # uid -> timestamp: don't drain until flood delay expires




logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
# ── Suppress httpx/httpcore INFO logs — they expose API keys + bot token in URLs ──
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

async def get_sol_price() -> float:
    """Get current SOL/USD price. Cached for 60s.
    Source priority:
      1. DexScreener  — SOL/USDC pair (most accurate for trading)
      2. CoinGecko    — free public API, no key required
      3. Last cached  — stale but better than a hardcoded guess
    """
    now = _time.time()
    if now - _sol_price_cache["ts"] < 60:
        return _sol_price_cache["price"]

    client = await get_http()

    # ── Source 1: DexScreener ─────────────────────────────────────────────────
    try:
        r = await client.get(
            "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112",
            timeout=5,
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            sol_pairs = [p for p in pairs if p.get("quoteToken", {}).get("symbol") == "USDC"]
            if sol_pairs:
                price = float(sol_pairs[0].get("priceUsd", 0) or 0)
                if price > 0:
                    _sol_price_cache["price"] = price
                    _sol_price_cache["ts"]    = now
                    logger.debug("SOL price $%.2f (DexScreener)", price)
                    return price
    except Exception as e:
        logger.warning("SOL price DexScreener failed: %s", e)

    # ── Source 2: CoinGecko (free tier, no API key needed) ───────────────────
    try:
        r = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            headers={"Accept": "application/json"},
            timeout=5,
        )
        if r.status_code == 200:
            price = float(r.json().get("solana", {}).get("usd", 0) or 0)
            if price > 0:
                _sol_price_cache["price"] = price
                _sol_price_cache["ts"]    = now
                logger.info("SOL price $%.2f (CoinGecko fallback)", price)
                return price
    except Exception as e:
        logger.warning("SOL price CoinGecko failed: %s", e)

    # ── Source 3: Last known cache (stale but non-zero) ───────────────────────
    logger.warning("SOL price: all sources failed — using last cached $%.2f", _sol_price_cache["price"])
    return _sol_price_cache["price"]



async def get_helius_maker_pct(contract: str, api_key: str) -> dict:
    """
    Optional Helius enrichment — fetches last 100 txns and calculates:
      - maker_pct:    % of unique wallets that are net buyers
      - maker_count:  number of unique buyer wallets
      - top3_vol_pct: % of volume from top 3 wallets (wash trade signal)
    Returns empty dict gracefully if key missing / rate limited.
    """
    if not api_key:
        return {}
    try:
        client = await get_http()
        url = f"https://api.helius.xyz/v0/addresses/{contract}/transactions"
        params = {"api-key": api_key, "limit": "100", "type": "SWAP"}
        r = await client.get(url, params=params, timeout=8)
        if r.status_code == 429:
            logger.info("Helius rate limit hit — skipping maker enrichment")
            return {}
        if r.status_code != 200:
            return {}
        txns = r.json()
        if not isinstance(txns, list) or not txns:
            return {}

        buyer_vols:  dict = {}   # wallet → volume bought
        seller_vols: dict = {}   # wallet → volume sold

        for tx in txns:
            try:
                fee_payer = tx.get("feePayer", "")
                native_transfers = tx.get("nativeTransfers", []) or []
                token_transfers  = tx.get("tokenTransfers",  []) or []
                # Determine if this is a buy (feePayer received token) or sell
                is_buy = any(
                    t.get("toUserAccount") == fee_payer
                    for t in token_transfers
                    if t.get("mint") == contract
                )
                # Approximate volume from native SOL moved
                sol_moved = sum(
                    abs(t.get("amount", 0))
                    for t in native_transfers
                    if t.get("fromUserAccount") == fee_payer or t.get("toUserAccount") == fee_payer
                ) / 1e9  # lamports → SOL
                if is_buy:
                    buyer_vols[fee_payer]  = buyer_vols.get(fee_payer,  0) + sol_moved
                else:
                    seller_vols[fee_payer] = seller_vols.get(fee_payer, 0) + sol_moved
            except Exception:
                continue

        total_wallets = len(set(buyer_vols.keys()) | set(seller_vols.keys()))
        if total_wallets == 0:
            return {}

        maker_pct   = round(len(buyer_vols) / total_wallets * 100) if total_wallets > 0 else 50
        maker_count = len(buyer_vols)

        # Top 3 wallet concentration — sum buy+sell vol per wallet (don't overwrite)
        all_vols: dict = {}
        for _w, _v in buyer_vols.items():
            all_vols[_w] = all_vols.get(_w, 0) + _v
        for _w, _v in seller_vols.items():
            all_vols[_w] = all_vols.get(_w, 0) + _v
        total_vol = sum(all_vols.values()) or 1
        top3_vol  = sum(sorted(all_vols.values(), reverse=True)[:3])
        top3_vol_pct = round(top3_vol / total_vol * 100)

        return {
            "maker_pct":    maker_pct,
            "maker_count":  maker_count,
            "top3_vol_pct": top3_vol_pct,
        }
    except Exception as e:
        logger.debug(f"Helius maker enrichment failed: {e}")
        return {}

async def get_helius_pool_price(contract: str, pair_addr: str, api_key: str) -> dict:
    """
    Query Solana pool reserves directly via Helius RPC.
    Returns {price, liq, liq_drop_pct} in near-real-time (~400ms block time).
    Falls back gracefully if pool layout unreadable.
    """
    if not api_key or not pair_addr:
        return {}
    try:
        client   = await get_http()
        rpc_url  = f"https://mainnet.helius-rpc.com/?api-key={api_key}"

        # Step 1: get pool account info to find vault addresses
        pool_resp = await client.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [pair_addr, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        }, timeout=5)
        if pool_resp.status_code != 200:
            return {}

        pool_data = pool_resp.json().get("result", {}).get("value")
        if not pool_data:
            return {}

        # Step 2: get token accounts for this pool
        # Use getTokenAccountsByOwner to find what the pool holds
        ta_resp = await client.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 2,
            "method": "getTokenAccountsByOwner",
            "params": [pair_addr,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed", "commitment": "confirmed"}
            ]
        }, timeout=5)
        if ta_resp.status_code != 200:
            return {}

        accounts = ta_resp.json().get("result", {}).get("value", [])
        if not accounts:
            return {}

        # Parse token balances — find base (token) and quote (SOL wrapped / USDC)
        WSOL  = "So11111111111111111111111111111111111111112"
        USDC  = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        USDT  = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

        base_amt  = None
        quote_usd = None

        for acct in accounts:
            info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            mint    = info.get("mint", "")
            balance = float(info.get("tokenAmount", {}).get("uiAmount") or 0)

            if mint == contract:
                base_amt = balance
            elif mint == WSOL:
                # Use live SOL/USD price from cache (refreshed every 60s)
                sol_price = _sol_price_cache.get("price", 150.0)
                quote_usd = balance * sol_price
            elif mint in (USDC, USDT):
                quote_usd = balance

        if base_amt and base_amt > 0 and quote_usd and quote_usd > 0:
            price = quote_usd / base_amt
            liq   = quote_usd * 2  # total liq = 2× quote side
            return {"price": price, "liq": liq, "source": "helius_rpc"}

        return {}
    except Exception as e:
        logger.debug(f"Helius RPC pool price failed: {e}")
        return {}


async def get_helius_rug_signal(contract: str, api_key: str) -> dict:
    """
    Check last 5 transactions on the token for large liquidity removals.
    Returns {rug_detected: bool, reason: str}
    """
    if not api_key:
        return {"rug_detected": False}
    try:
        client = await get_http()
        url    = f"https://api.helius.xyz/v0/addresses/{contract}/transactions"
        r = await client.get(url, params={
            "api-key": api_key, "limit": "5",
            "type": "REMOVE_LIQUIDITY"
        }, timeout=5)
        if r.status_code != 200:
            return {"rug_detected": False}

        txns = r.json()
        if not isinstance(txns, list) or not txns:
            return {"rug_detected": False}

        # Any REMOVE_LIQUIDITY in last 5 txns = rug signal
        for tx in txns:
            desc = tx.get("description", "").lower()
            if "remove" in desc or "withdraw" in desc:
                return {
                    "rug_detected": True,
                    "reason": f"Liquidity removal detected on-chain"
                }
        return {"rug_detected": False}
    except Exception as e:
        logger.debug(f"Helius rug signal check failed: {e}")
        return {"rug_detected": False}


# ── Helius holder cache (5 min TTL) ──────────────────────────────────────────
_helius_holder_cache: dict = {}
HELIUS_HOLDER_CACHE_TTL = 300.0   # 5 minutes

async def get_helius_top_holders(contract: str, api_key: str) -> dict:
    """
    Fetch accurate top-20 holder data via Helius RPC.
    Uses three parallel-ish calls:
      1. getTokenLargestAccounts  → top 20 token accounts + raw amounts
      2. getTokenSupply           → total supply for % calculation
      3. getMultipleAccounts      → resolve token accounts → owner wallets

    Returns:
      top10_pct    : float   — % held by top 10 real wallets
      top20_pct    : float
      holders_data : list[{address, pct, amount}]
      source       : "helius_rpc"

    Protocol addresses and obvious pools (>70% of supply) are excluded.
    Same multi-pass outlier filter applied as the RugCheck path.
    Falls back gracefully → returns {} on any error.
    """
    if not api_key:
        return {}

    # ── Cache check ───────────────────────────────────────────────────────────
    cached = _helius_holder_cache.get(contract)
    if cached and (_time.time() - cached["ts"]) < HELIUS_HOLDER_CACHE_TTL:
        return cached["data"]

    try:
        client  = await get_http()
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"

        # ── Step 1 & 2: largest accounts + total supply in parallel ──────────
        largest_resp, supply_resp = await _asyncio.gather(
            client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenLargestAccounts",
                "params": [contract, {"commitment": "confirmed"}]
            }, timeout=8),
            client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 2,
                "method": "getTokenSupply",
                "params": [contract, {"commitment": "confirmed"}]
            }, timeout=8),
            return_exceptions=True
        )

        if isinstance(largest_resp, Exception) or isinstance(supply_resp, Exception):
            return {}
        if largest_resp.status_code != 200 or supply_resp.status_code != 200:
            return {}

        largest_data = largest_resp.json().get("result", {}).get("value", [])
        supply_info  = supply_resp.json().get("result", {}).get("value", {})
        total_supply = float(supply_info.get("uiAmount") or 0)

        if not largest_data or total_supply <= 0:
            return {}

        # ── Step 3: resolve token accounts → owner wallets ───────────────────
        # getTokenLargestAccounts returns token account addresses, not owner wallets.
        # One getMultipleAccounts call resolves all 20 at once.
        token_acct_addrs = [e.get("address", "") for e in largest_data if e.get("address")]
        if not token_acct_addrs:
            return {}

        multi_resp = await client.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 3,
            "method": "getMultipleAccounts",
            "params": [token_acct_addrs, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        }, timeout=8)

        if multi_resp.status_code != 200:
            return {}

        acct_values = multi_resp.json().get("result", {}).get("value", [])

        # ── Known protocol addresses — always exclude ─────────────────────────
        _PROTO = {
            "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",
            "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",
            "4wTV81aCK4cYbJGYLTBZaC7DqHBnZ7VJ4C3MeH9zGD5P",
            "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
            "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
            "7YttLkHDoNj9wyDur5pM1ejNaAvT9X4eqaYcHQqtj2G5",
            "FRhB8L7Y9Qq41qZXYLtC2nw8An1RJfLLxRF2x9RwLLMo",
            "So11111111111111111111111111111111111111112",
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        }

        # ── Build holder list ─────────────────────────────────────────────────
        holders = []
        seen_owners: set = set()   # deduplicate wallets with multiple token accounts
        for acct in acct_values:
            if not acct:
                continue
            parsed = acct.get("data", {}).get("parsed", {}).get("info", {})
            owner  = parsed.get("owner", "")
            amount = float(parsed.get("tokenAmount", {}).get("uiAmount") or 0)
            if not owner or amount <= 0 or owner in _PROTO or owner in seen_owners:
                continue
            seen_owners.add(owner)
            pct = round(amount / total_supply * 100, 2)
            # Skip obvious pool/bonding curve accounts (>70% of supply)
            if pct > 70.0:
                logger.debug(f"Helius holders: skipping {owner[:12]} ({pct}% — pool/BC)")
                continue
            holders.append({"address": owner, "pct": pct, "amount": amount})

        if not holders:
            return {}

        holders.sort(key=lambda x: -x["pct"])

        # ── Multi-pass outlier filter (same as RugCheck path) ─────────────────
        _removed = 0
        for _ in range(5):
            if len(holders) < 4:
                break
            top       = holders[0]["pct"]
            rest_pcts = [h["pct"] for h in holders[1:]]
            mean_rest = sum(rest_pcts) / len(rest_pcts) if rest_pcts else 0
            if top >= 8.0 and mean_rest > 0 and top / mean_rest > 5.0:
                logger.debug(f"Helius outlier removed: {holders[0]['address'][:12]} ({top}%)")
                holders = holders[1:]
                _removed += 1
            else:
                break

        top10_pct = round(sum(h["pct"] for h in holders[:10]), 1)
        top20_pct = round(sum(h["pct"] for h in holders[:20]), 1)

        result = {
            "top10_pct":    top10_pct,
            "top20_pct":    top20_pct,
            "holders_data": holders[:20],
            "total_supply": total_supply,
            "source":       "helius_rpc",
        }
        _helius_holder_cache[contract] = {"data": result, "ts": _time.time()}
        logger.debug(
            f"Helius holders {contract[:12]}: top10={top10_pct}% "
            f"({len(holders)} real holders, {_removed} pools removed)"
        )
        return result

    except Exception as e:
        logger.debug(f"Helius top holders failed {contract[:12]}: {e}")
        return {}


TRANSLATIONS: dict = {
    "en": {
        "welcome":        "👋 Welcome to *APEX SNIPER BOT*!\n\nAdvanced multi-chain paper trading bot.\n\nSet your starting balance:\nMin: $1  |  Max: $10,000\n\nEnter your starting balance:",
        "welcome_back":   "⚡ *APEX SNIPER BOT*\n\nWelcome back, *{username}*!\n💰 Balance: *{balance}*\n💎 Savings: *{savings}*\n\nPaste any crypto CA to trade 👇",
        "buy_exec":       "✅ *BUY EXECUTED*\n\n*{name} (${symbol})*\nSpent: *{spent}*\nGot: *{tokens} {symbol}*\nPrice: *{price}*\nMC: *{mc}*\nLiq: *{liq}*\nCash left: *{cash}*",
        "sell_exec":      "✅ *SELL EXECUTED*\n\nReceived: *{received}*\nPrice: *{price}*  |  *{cx}x*\nHeld: *{held}h*\nPnL: *{pnl}*\nCash: *{cash}*",
        "risk_card":      "🧮 *RISK CALCULATOR*\n\n*${symbol}*  |  MC: {mc}\n\nYou are risking *{amount}* ({pct}% of balance)\n\n📈 *If it goes up:*\n  2x → *+{gain2x}* (have {bal2x})\n  5x → *+{gain5x}* (have {bal5x})\n  10x → *+{gain10x}* (have {bal10x})\n\n📉 *If it goes down:*\n  -50% → *-{loss50}* (have {balL50})\n  -80% → *-{loss80}* (have {balL80})\n  -100% → *-{amount}* (have {balL100})\n\nProceed with this trade?",
        "sniper_on":      "🎯 Sniper mode *ON* — watching for new tokens matching your filters.",
        "sniper_off":     "🎯 Sniper mode *OFF*.",
        "sniper_fired":   "🎯 *SNIPER FIRED!*\n\n*${symbol}* matched your filters!\nScore: *{score}/100* — {verdict}\nMC: *{mc}*\nLiq: *{liq}*\nBought: *{amount}*\nPrice: *{price}*\nCash left: *{cash}*",
        "dca_set":        "✅ *DCA Orders Set for ${symbol}*\n\n{lines}\n\nThe bot will auto-buy when each MC target is reached.",
        "dca_fired":      "📉 *DCA BUY TRIGGERED*\n\n*${symbol}* reached {mc} MC!\nBought: *{amount}*\nPrice: *{price}*\nCash left: *{cash}*",
        "lang_set":       "✅ Language set to *English*.",
        "confirm_buy":    "✅ Confirm Buy",
        "cancel":         "❌ Cancel",
        "back":           "Back",
        "main_menu":      "🏠 Main Menu",
    },
    "es": {
        "welcome":        "👋 ¡Bienvenido a *APEX SNIPER BOT*!\n\nBot avanzado de trading simulado multi-cadena.\n\nConfigura tu saldo inicial:\nMín: $1  |  Máx: $10,000\n\nIngresa tu saldo inicial:",
        "welcome_back":   "⚡ *APEX SNIPER BOT*\n\n¡Bienvenido de nuevo, *{username}*!\n💰 Saldo: *{balance}*\n💎 Ahorros: *{savings}*\n\nPega cualquier CA para operar 👇",
        "buy_exec":       "✅ *COMPRA EJECUTADA*\n\n*{name} (${symbol})*\nGastado: *{spent}*\nRecibido: *{tokens} {symbol}*\nPrecio: *{price}*\nMC: *{mc}*\nLiq: *{liq}*\nSaldo restante: *{cash}*",
        "sell_exec":      "✅ *VENTA EJECUTADA*\n\nRecibido: *{received}*\nPrecio: *{price}*  |  *{cx}x*\nMantenido: *{held}h*\nGanancia: *{pnl}*\nSaldo: *{cash}*",
        "risk_card":      "🧮 *CALCULADORA DE RIESGO*\n\n*${symbol}*  |  MC: {mc}\n\nEstás arriesgando *{amount}* ({pct}% del saldo)\n\n📈 *Si sube:*\n  2x → *+{gain2x}* (tendrás {bal2x})\n  5x → *+{gain5x}* (tendrás {bal5x})\n  10x → *+{gain10x}* (tendrás {bal10x})\n\n📉 *Si baja:*\n  -50% → *-{loss50}* (tendrás {balL50})\n  -80% → *-{loss80}* (tendrás {balL80})\n  -100% → *-{amount}* (tendrás {balL100})\n\n¿Proceder con esta operación?",
        "sniper_on":      "🎯 Modo sniper *ACTIVADO* — buscando nuevos tokens según tus filtros.",
        "sniper_off":     "🎯 Modo sniper *DESACTIVADO*.",
        "sniper_fired":   "🎯 *¡SNIPER DISPARADO!*\n\n*${symbol}* coincide con tus filtros!\nPuntaje: *{score}/100* — {verdict}\nMC: *{mc}*\nLiq: *{liq}*\nComprado: *{amount}*\nPrecio: *{price}*\nSaldo restante: *{cash}*",
        "dca_set":        "✅ *Órdenes DCA configuradas para ${symbol}*\n\n{lines}\n\nEl bot comprará automáticamente cuando se alcance cada MC objetivo.",
        "dca_fired":      "📉 *COMPRA DCA ACTIVADA*\n\n*${symbol}* alcanzó {mc} de MC!\nComprado: *{amount}*\nPrecio: *{price}*\nSaldo restante: *{cash}*",
        "lang_set":       "✅ Idioma establecido a *Español*.",
        "confirm_buy":    "✅ Confirmar Compra",
        "cancel":         "❌ Cancelar",
        "back":           "Volver",
        "main_menu":      "🏠 Menú Principal",
    },
    "pt": {
        "welcome":        "👋 Bem-vindo ao *APEX SNIPER BOT*!\n\nBot avançado de trading simulado multi-chain.\n\nDefina seu saldo inicial:\nMín: $1  |  Máx: $10,000\n\nDigite seu saldo inicial:",
        "welcome_back":   "⚡ *APEX SNIPER BOT*\n\nBem-vindo de volta, *{username}*!\n💰 Saldo: *{balance}*\n💎 Poupança: *{savings}*\n\nCole qualquer CA para negociar 👇",
        "buy_exec":       "✅ *COMPRA EXECUTADA*\n\n*{name} (${symbol})*\nGasto: *{spent}*\nRecebido: *{tokens} {symbol}*\nPreço: *{price}*\nMC: *{mc}*\nLiq: *{liq}*\nSaldo restante: *{cash}*",
        "sell_exec":      "✅ *VENDA EXECUTADA*\n\nRecebido: *{received}*\nPreço: *{price}*  |  *{cx}x*\nMantido: *{held}h*\nLucro: *{pnl}*\nSaldo: *{cash}*",
        "risk_card":      "🧮 *CALCULADORA DE RISCO*\n\n*${symbol}*  |  MC: {mc}\n\nVocê está arriscando *{amount}* ({pct}% do saldo)\n\n📈 *Se subir:*\n  2x → *+{gain2x}* (terá {bal2x})\n  5x → *+{gain5x}* (terá {bal5x})\n  10x → *+{gain10x}* (terá {bal10x})\n\n📉 *Se cair:*\n  -50% → *-{loss50}* (terá {balL50})\n  -80% → *-{loss80}* (terá {balL80})\n  -100% → *-{amount}* (terá {balL100})\n\nProsseguir com esta operação?",
        "sniper_on":      "🎯 Modo sniper *ATIVADO* — procurando novos tokens com seus filtros.",
        "sniper_off":     "🎯 Modo sniper *DESATIVADO*.",
        "sniper_fired":   "🎯 *SNIPER DISPARADO!*\n\n*${symbol}* corresponde aos seus filtros!\nPontuação: *{score}/100* — {verdict}\nMC: *{mc}*\nLiq: *{liq}*\nComprado: *{amount}*\nPreço: *{price}*\nSaldo restante: *{cash}*",
        "dca_set":        "✅ *Ordens DCA configuradas para ${symbol}*\n\n{lines}\n\nO bot comprará automaticamente quando cada MC alvo for atingido.",
        "dca_fired":      "📉 *COMPRA DCA ATIVADA*\n\n*${symbol}* atingiu {mc} de MC!\nComprado: *{amount}*\nPreço: *{price}*\nSaldo restante: *{cash}*",
        "lang_set":       "✅ Idioma definido para *Português*.",
        "confirm_buy":    "✅ Confirmar Compra",
        "cancel":         "❌ Cancelar",
        "back":           "Voltar",
        "main_menu":      "🏠 Menu Principal",
    },
    "fr": {
        "welcome":        "👋 Bienvenue sur *APEX SNIPER BOT*!\n\nBot de trading papier multi-chaîne avancé.\n\nDéfinissez votre solde de départ:\nMin: $1  |  Max: $10 000\n\nEntrez votre solde de départ:",
        "welcome_back":   "⚡ *APEX SNIPER BOT*\n\nBienvenue, *{username}*!\n💰 Solde: *{balance}*\n💎 Épargne: *{savings}*\n\nCollez n'importe quelle CA pour trader 👇",
        "buy_exec":       "✅ *ACHAT EXÉCUTÉ*\n\n*{name} (${symbol})*\nDépensé: *{spent}*\nReçu: *{tokens} {symbol}*\nPrix: *{price}*\nMC: *{mc}*\nLiq: *{liq}*\nSolde restant: *{cash}*",
        "sell_exec":      "✅ *VENTE EXÉCUTÉE*\n\nReçu: *{received}*\nPrix: *{price}*  |  *{cx}x*\nDétenu: *{held}h*\nGain: *{pnl}*\nSolde: *{cash}*",
        "risk_card":      "🧮 *CALCULATEUR DE RISQUE*\n\n*${symbol}*  |  MC: {mc}\n\nVous risquez *{amount}* ({pct}% du solde)\n\n📈 *Si ça monte:*\n  2x → *+{gain2x}* (aurez {bal2x})\n  5x → *+{gain5x}* (aurez {bal5x})\n  10x → *+{gain10x}* (aurez {bal10x})\n\n📉 *Si ça baisse:*\n  -50% → *-{loss50}* (aurez {balL50})\n  -80% → *-{loss80}* (aurez {balL80})\n  -100% → *-{amount}* (aurez {balL100})\n\nProcéder avec ce trade?",
        "sniper_on":      "🎯 Mode sniper *ACTIVÉ* — surveillance de nouveaux tokens selon vos filtres.",
        "sniper_off":     "🎯 Mode sniper *DÉSACTIVÉ*.",
        "sniper_fired":   "🎯 *SNIPER DÉCLENCHÉ!*\n\n*${symbol}* correspond à vos filtres!\nScore: *{score}/100* — {verdict}\nMC: *{mc}*\nLiq: *{liq}*\nAcheté: *{amount}*\nPrix: *{price}*\nSolde restant: *{cash}*",
        "dca_set":        "✅ *Ordres DCA configurés pour ${symbol}*\n\n{lines}\n\nLe bot achètera automatiquement à chaque MC cible atteint.",
        "dca_fired":      "📉 *ACHAT DCA DÉCLENCHÉ*\n\n*${symbol}* a atteint {mc} de MC!\nAcheté: *{amount}*\nPrix: *{price}*\nSolde restant: *{cash}*",
        "lang_set":       "✅ Langue définie sur *Français*.",
        "confirm_buy":    "✅ Confirmer l'achat",
        "cancel":         "❌ Annuler",
        "back":           "Retour",
        "main_menu":      "🏠 Menu Principal",
    },
    "zh": {
        "welcome":        "👋 欢迎使用 *APEX SNIPER BOT*!\n\n高级多链模拟交易机器人。\n\n设置起始余额:\n最低: $1  |  最高: $10,000\n\n请输入起始余额:",
        "welcome_back":   "⚡ *APEX SNIPER BOT*\n\n欢迎回来，*{username}*！\n💰 余额: *{balance}*\n💎 储蓄: *{savings}*\n\n粘贴任意合约地址开始交易 👇",
        "buy_exec":       "✅ *买入成功*\n\n*{name} (${symbol})*\n花费: *{spent}*\n获得: *{tokens} {symbol}*\n价格: *{price}*\n市值: *{mc}*\n流动性: *{liq}*\n剩余余额: *{cash}*",
        "sell_exec":      "✅ *卖出成功*\n\n收到: *{received}*\n价格: *{price}*  |  *{cx}x*\n持有时间: *{held}h*\n盈亏: *{pnl}*\n余额: *{cash}*",
        "risk_card":      "🧮 *风险计算器*\n\n*${symbol}*  |  市值: {mc}\n\n您正在冒险 *{amount}*（余额的 {pct}%）\n\n📈 *如果上涨:*\n  2x → *+{gain2x}*（将有 {bal2x}）\n  5x → *+{gain5x}*（将有 {bal5x}）\n  10x → *+{gain10x}*（将有 {bal10x}）\n\n📉 *如果下跌:*\n  -50% → *-{loss50}*（将有 {balL50}）\n  -80% → *-{loss80}*（将有 {balL80}）\n  -100% → *-{amount}*（将有 {balL100}）\n\n确认进行此交易？",
        "sniper_on":      "🎯 狙击手模式已 *开启* — 正在监控符合您筛选条件的新代币。",
        "sniper_off":     "🎯 狙击手模式已 *关闭*。",
        "sniper_fired":   "🎯 *狙击触发！*\n\n*${symbol}* 符合您的筛选条件！\n评分: *{score}/100* — {verdict}\n市值: *{mc}*\n流动性: *{liq}*\n已买入: *{amount}*\n价格: *{price}*\n剩余余额: *{cash}*",
        "dca_set":        "✅ *已为 ${symbol} 设置 DCA 订单*\n\n{lines}\n\n机器人将在每个市值目标达到时自动买入。",
        "dca_fired":      "📉 *DCA 买入触发*\n\n*${symbol}* 市值达到 {mc}！\n已买入: *{amount}*\n价格: *{price}*\n剩余余额: *{cash}*",
        "lang_set":       "✅ 语言已设置为 *中文*。",
        "confirm_buy":    "✅ 确认买入",
        "cancel":         "❌ 取消",
        "back":           "返回",
        "main_menu":      "🏠 主菜单",
    },
}

def t(ud: dict, key: str, **kwargs) -> str:
    """Return translated string for the user's language, falling back to English."""
    lang = ud.get("language", "en") if ud else "en"
    lang_dict = TRANSLATIONS.get(lang, TRANSLATIONS["en"])
    text = lang_dict.get(key, TRANSLATIONS["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text


def risk_card_text(ud: dict, symbol: str, mc: float, amount: float) -> str:
    """Build the risk calculator card text.

    bal_after = cash remaining after spending `amount` on the buy.
    All 'you will have' projections are: bal_after + what you get back from the position.
    Old code used `bal` (pre-buy) as the base which overstated every scenario by `amount`.
    """
    bal       = ud.get("balance", 0)
    bal_after = bal - amount          # cash left after spending
    pct       = round(amount / bal * 100, 1) if bal > 0 else 0
    gain2x    = money(amount * 1)     # net profit at 2x
    gain5x    = money(amount * 4)     # net profit at 5x
    gain10x   = money(amount * 9)     # net profit at 10x
    loss50    = money(amount * 0.5)   # dollar loss at -50%
    loss80    = money(amount * 0.8)   # dollar loss at -80%
    return t(ud, "risk_card",
        symbol=symbol, mc=mc_str(mc), amount=money(amount), pct=pct,
        gain2x=gain2x,  bal2x=money(bal_after + amount * 2),
        gain5x=gain5x,  bal5x=money(bal_after + amount * 5),
        gain10x=gain10x,bal10x=money(bal_after + amount * 10),
        loss50=loss50,  balL50=money(bal_after + amount * 0.5),
        loss80=loss80,  balL80=money(bal_after + amount * 0.2),
        balL100=money(bal_after),
    )

users: dict = {}
trade_log: dict = {}
pending: dict = {}
chart_msg_ids: dict = {}   # uid -> message_id of last chart sent, for deletion on refresh
_rf_locks: dict = {}       # uid -> asyncio.Lock — prevents concurrent refresh calls
_ohlcv_cache: dict = {}    # contract -> {data, ts} — 30s TTL, avoids redundant GeckoTerminal fetches
OHLCV_CACHE_TTL = 30.0
_sniper_analysis_cache: dict = {}  # uid -> {contract -> {info, sc, ai}} for View Analysis button

# ── Channel Milestone Tracker ─────────────────────────────────────────────────
# Tracks every token broadcast to a channel so we can post milestone updates.
# Structure: {uid: {contract: {
#     "symbol":      str,
#     "entry_mc":    float,    # MC at time of call
#     "entry_price": float,    # price at time of call
#     "called_at":   str,      # ISO timestamp
#     "channel_id":  int,      # channel to post updates to
#     "milestones_hit": set,   # e.g. {2, 5, 10} to avoid re-posting
# }}}
_channel_calls: dict = {}
_kol_last_sig:  dict = {}      # uid -> {wallet_addr -> last_sig}
_kol_hot_contracts: dict = {}  # contract -> [{label, sol_spent, ts}] — KOL recent buys
_first_scanner:       dict = {}  # contract -> {uid, username, price, mc, scanned_at}
_ath_cache:           dict = {}  # contract -> {price, mc, ts}  — persists across refreshes
_contract_scan_count: dict = {}  # contract -> int — how many times this CA was scanned


# ══ M·T·V INTELLIGENCE HELPERS ═══════════════════════════════════════════════
# Shared by group card (Markdown) and channel card (HTML).
# Makers column uses Helius data when available, falls back to a 45% estimate.

def _mtv_m_icon(makers: int) -> str:
    """Colour icon for unique maker (wallet) count."""
    if makers >= 100: return "🟢"
    if makers >= 40:  return "🟡"
    return "🔴"

def _mtv_t_icon(trades: int) -> str:
    """Colour icon for raw transaction count."""
    if trades >= 200: return "🟢"
    if trades >= 80:  return "🟡"
    return "🔴"

def _mtv_bp_icon(buy_pct: float) -> str:
    """Colour icon for buy pressure %."""
    if buy_pct >= 60: return "🟢"
    if buy_pct >= 50: return "🟡"
    return "🔴"

def _mtv_tm_icon(tm_ratio: float) -> str:
    """Colour icon for T/M ratio (wash-trade signal)."""
    if tm_ratio <= 3.0: return "🟢"
    if tm_ratio <= 4.5: return "🟡"
    return "🔴"

def _mtv_signal(info: dict) -> str:
    """
    One-line signal summary derived from M·T·V data.
    Returns the most significant finding as an emoji + label string.
    Multiple signals are joined with  |  up to the most important 3.
    """
    signals: list = []

    bp_m5  = info.get("buy_pct_m5", 50)
    bp_h1  = info.get("buy_pct_h1", 50)
    bp_h24 = info.get("buy_pct",    50)

    # 1. Buy-pressure trend (M5 vs H24)
    bp_drop = bp_h24 - bp_m5   # positive = fading (H24 was higher than M5 now)
    if bp_drop >= 15:
        signals.append("🔴 Buy pressure fading fast")
    elif bp_drop >= 8:
        signals.append("🟡 Buy pressure softening")
    elif bp_m5 > bp_h24 + 5:
        signals.append("🟢 Buy pressure building")

    # 2. T/M ratio — wash trade detection
    mk_h24    = info.get("maker_count", 0) or 0
    t_h24     = (info.get("buys", 0) or 0) + (info.get("sells", 0) or 0)
    if mk_h24 == 0 and t_h24 > 0:
        mk_h24 = max(1, int(t_h24 * 0.45))
    tm_h24 = round(t_h24 / mk_h24, 1) if mk_h24 > 0 else 0
    if tm_h24 > 4.5:
        signals.append("🔴 High repeat trading (wash risk)")
    elif tm_h24 > 3.5:
        signals.append("🟡 Some repeat trading detected")

    # 3. Bundle launch: all timeframes nearly equal
    t_h1 = (info.get("buys_h1", 0) or 0) + (info.get("sells_h1", 0) or 0)
    if t_h1 > 0 and t_h24 > 0:
        ratio = t_h24 / t_h1
        if ratio < 2.0:
            signals.append("🔴 All timeframes equal — bundle launch")

    # 4. Volume spike
    vol_m5 = info.get("vol_m5", 0) or 0
    vol_h1 = info.get("vol_h1", 1) or 1
    avg_5m = vol_h1 / 12
    if avg_5m > 0 and vol_m5 > avg_5m * 3:
        signals.append("🟢 Volume spike now 🔥")

    # 5. Maker health
    if mk_h24 < 50:
        signals.append("🔴 Very few unique buyers")
    elif mk_h24 >= 200:
        signals.append("🟢 Strong unique buyer base")

    top3 = signals[:3]
    return "  |  ".join(top3) if top3 else "🟢 Clean"


def _build_mtv_markdown(info: dict) -> str:
    """
    M·T·V block for group token card (Telegram Markdown).
    Columns: M (makers) · T (trades) · Buy% · Vol · T/M
    Rows: M5 / H1 / H24
    Signal summary on last line.
    """
    def _mf(v: float) -> str:
        if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
        if v >= 1_000:     return f"${v/1_000:.1f}K"
        return f"${v:.0f}"

    buys_m5   = info.get("buys_m5",  0) or 0
    sells_m5  = info.get("sells_m5", 0) or 0
    buys_h1   = info.get("buys_h1",  0) or 0
    sells_h1  = info.get("sells_h1", 0) or 0
    buys_h24  = info.get("buys",     0) or 0
    sells_h24 = info.get("sells",    0) or 0

    t_m5  = buys_m5  + sells_m5
    t_h1  = buys_h1  + sells_h1
    t_h24 = buys_h24 + sells_h24

    bp_m5  = info.get("buy_pct_m5", 50)
    bp_h1  = info.get("buy_pct_h1", 50)
    bp_h24 = info.get("buy_pct",    50)

    vol_m5  = info.get("vol_m5",  0) or 0
    vol_h1  = info.get("vol_h1",  0) or 0
    vol_h24 = info.get("vol_h24", 0) or 0

    # Makers — Helius if set, else 45% estimate
    mk_h24 = info.get("maker_count", 0) or 0
    mk_h1  = info.get("maker_count_h1", 0) or 0
    mk_m5  = info.get("maker_count_m5", 0) or 0
    if mk_h24 == 0 and t_h24 > 0: mk_h24 = max(1, int(t_h24 * 0.45))
    if mk_h1  == 0 and t_h1  > 0: mk_h1  = max(1, int(t_h1  * 0.45))
    if mk_m5  == 0 and t_m5  > 0: mk_m5  = max(1, int(t_m5  * 0.45))

    tm_m5  = round(t_m5  / mk_m5,  1) if mk_m5  > 0 else 0
    tm_h1  = round(t_h1  / mk_h1,  1) if mk_h1  > 0 else 0
    tm_h24 = round(t_h24 / mk_h24, 1) if mk_h24 > 0 else 0

    helius_note = "" if info.get("maker_count") else " _(M est.)_"
    signal = _mtv_signal(info)

    # Markdown monospace rows via inline code blocks
    # Format: ICON  M    T    Buy%   Vol     T/M
    hdr = "     `  M      T   Buy%     Vol   T/M`"
    r_m5  = (f"`M5  {_mtv_m_icon(mk_m5)}{str(mk_m5):>4} "
             f" {_mtv_t_icon(t_m5)}{str(t_m5):>5} "
             f" {_mtv_bp_icon(bp_m5)}{str(bp_m5):>3}% "
             f" {_mf(vol_m5):>7} "
             f" {_mtv_tm_icon(tm_m5)}{str(tm_m5):>4}x`")
    r_h1  = (f"`H1  {_mtv_m_icon(mk_h1)}{str(mk_h1):>4} "
             f" {_mtv_t_icon(t_h1)}{str(t_h1):>5} "
             f" {_mtv_bp_icon(bp_h1)}{str(bp_h1):>3}% "
             f" {_mf(vol_h1):>7} "
             f" {_mtv_tm_icon(tm_h1)}{str(tm_h1):>4}x`")
    r_h24 = (f"`H24 {_mtv_m_icon(mk_h24)}{str(mk_h24):>4} "
             f" {_mtv_t_icon(t_h24)}{str(t_h24):>5} "
             f" {_mtv_bp_icon(bp_h24)}{str(bp_h24):>3}% "
             f" {_mf(vol_h24):>7} "
             f" {_mtv_tm_icon(tm_h24)}{str(tm_h24):>4}x`")

    return (
        "─────────────────────────\n"
        "📊 *M · T · V  Intelligence*" + helius_note + "\n"
        + hdr + "\n"
        + r_m5  + "\n"
        + r_h1  + "\n"
        + r_h24 + "\n"
        "🔍 " + signal + "\n"
    )


def _build_mtv_html(info: dict) -> str:
    """
    M·T·V block for channel card (HTML parse_mode).
    Columns: M (makers) · T (trades) · Buy% · Vol · T/M
    Rows: M5 / H1 / H24
    Signal summary on last line.
    """
    def _mf(v: float) -> str:
        if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
        if v >= 1_000:     return f"${v/1_000:.1f}K"
        return f"${v:.0f}"

    buys_m5   = info.get("buys_m5",  0) or 0
    sells_m5  = info.get("sells_m5", 0) or 0
    buys_h1   = info.get("buys_h1",  0) or 0
    sells_h1  = info.get("sells_h1", 0) or 0
    buys_h6   = info.get("buys_h6",  0) or 0
    sells_h6  = info.get("sells_h6", 0) or 0
    buys_h24  = info.get("buys",     0) or 0
    sells_h24 = info.get("sells",    0) or 0

    t_m5  = buys_m5  + sells_m5
    t_h1  = buys_h1  + sells_h1
    t_h6  = buys_h6  + sells_h6
    t_h24 = buys_h24 + sells_h24

    bp_m5  = info.get("buy_pct_m5", 50)
    bp_h1  = info.get("buy_pct_h1", 50)
    bp_h6  = info.get("buy_pct_h6", 50)
    bp_h24 = info.get("buy_pct",    50)

    vol_m5  = info.get("vol_m5",  0) or 0
    vol_h1  = info.get("vol_h1",  0) or 0
    vol_h6  = info.get("vol_h6",  0) or 0
    vol_h24 = info.get("vol_h24", 0) or 0

    # Makers — Helius if set, else 45% estimate
    mk_h24 = info.get("maker_count", 0) or 0
    mk_h1  = info.get("maker_count_h1", 0) or 0
    mk_m5  = info.get("maker_count_m5", 0) or 0
    if mk_h24 == 0 and t_h24 > 0: mk_h24 = max(1, int(t_h24 * 0.45))
    if mk_h1  == 0 and t_h1  > 0: mk_h1  = max(1, int(t_h1  * 0.45))
    if mk_m5  == 0 and t_m5  > 0: mk_m5  = max(1, int(t_m5  * 0.45))
    mk_h6  = max(1, int(t_h6 * 0.45)) if t_h6 > 0 else 0

    tm_m5  = round(t_m5  / mk_m5,  1) if mk_m5  > 0 else 0
    tm_h1  = round(t_h1  / mk_h1,  1) if mk_h1  > 0 else 0
    tm_h6  = round(t_h6  / mk_h6,  1) if mk_h6  > 0 else 0
    tm_h24 = round(t_h24 / mk_h24, 1) if mk_h24 > 0 else 0

    helius_note = "" if info.get("maker_count") else " <i>(M est.)</i>"
    signal = _mtv_signal(info)

    # HTML code block for monospace alignment
    header_row = f"{'':5}{'M':>6}  {'T':>6}  {'Buy%':>5}  {'Vol':>8}  {'T/M':>5}"
    def _row(label, mi, mk, ti, t, bi, bp, vi, vol, tmi, tm):
        return (
            f"{label:<4} {mi}{str(mk):>5}  {ti}{str(t):>5}  "
            f"{bi}{str(bp):>3}%  {str(_mf(vol)):>8}  {tmi}{str(tm):>4}x"
        )

    r_m5  = _row("M5",  _mtv_m_icon(mk_m5),  mk_m5,  _mtv_t_icon(t_m5),  t_m5,
                 _mtv_bp_icon(bp_m5),  bp_m5,  None, vol_m5,  _mtv_tm_icon(tm_m5),  tm_m5)
    r_h1  = _row("H1",  _mtv_m_icon(mk_h1),  mk_h1,  _mtv_t_icon(t_h1),  t_h1,
                 _mtv_bp_icon(bp_h1),  bp_h1,  None, vol_h1,  _mtv_tm_icon(tm_h1),  tm_h1)
    r_h6  = _row("H6",  _mtv_m_icon(mk_h6),  mk_h6,  _mtv_t_icon(t_h6),  t_h6,
                 _mtv_bp_icon(bp_h6),  bp_h6,  None, vol_h6,  _mtv_tm_icon(tm_h6),  tm_h6)
    r_h24 = _row("H24", _mtv_m_icon(mk_h24), mk_h24, _mtv_t_icon(t_h24), t_h24,
                 _mtv_bp_icon(bp_h24), bp_h24, None, vol_h24, _mtv_tm_icon(tm_h24), tm_h24)

    return (
        f"📊 <b>M · T · V  Intelligence</b>{helius_note}\n"
        f"<code>"
        f"{header_row}\n"
        f"{r_m5}\n"
        f"{r_h1}\n"
        f"{r_h6}\n"
        f"{r_h24}"
        f"</code>\n"
        f"🔍 {signal}"
    )

def format_token_price(p: float) -> str:
    """
    Format a token price without scientific notation.
    1.41e-05  →  $0.0000141
    0.002345  →  $0.002345
    1.23      →  $1.23
    """
    if p is None or p == 0:
        return "$0"
    if p >= 1_000_000:
        return "$" + str(round(p / 1_000_000, 2)) + "M"
    if p >= 1_000:
        return "$" + str(round(p / 1_000, 2)) + "K"
    if p >= 1:
        return "$" + str(round(p, 4))
    # For small prices: find enough decimal places to show 4 significant digits
    import math as _math
    try:
        _mag = -int(_math.floor(_math.log10(abs(p))))
        _decimals = min(_mag + 4, 12)
        _s = f"{p:.{_decimals}f}".rstrip("0")
        if _s.endswith("."): _s += "0"
        return "$" + _s
    except Exception:
        return "$" + str(p)
_buy_pct_prev:  dict = {}      # contract -> buy_pct_h1 from last sniper cycle (velocity)
_pf_reply_prev: dict = {}      # contract -> (reply_count, timestamp) for reply velocity
_social_mention_cache: dict = {}  # contract -> {count, last_seen_ts, spike_detected}
_sol_price_history: list = []  # rolling 12-entry SOL price log (one per sniper_job run)
_sol_bearish: bool = False          # module-level — updated by sniper_job, read by position manager
_market_regime: str = "ACTIVE"   # DEAD / FORMING / ACTIVE — updated each sniper cycle
_market_regime_cycles: int = 0   # consecutive cycles in current regime
_narrative_tracker: dict = {}    # keyword → count this cycle
_narrative_history: list = []    # recent active narratives [{kw, count, ts}]
_rug_liq_prev: dict = {}            # uid -> {contract -> last_liq} for rug pull detection
_bot_username: str = ""                 # fetched once on first sniper_job run, used for deep links

_FONT_CACHE: dict = {}   # {"bold": path, "regular": path} — resolved once

def _resolve_fonts() -> tuple:
    """Scan font paths once and cache the result."""
    if _FONT_CACHE:
        return _FONT_CACHE.get("bold"), _FONT_CACHE.get("regular")
    _dir = os.path.dirname(os.path.abspath(__file__))
    bold = None
    for _p in [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
        os.path.join(_dir, "DejaVuSans-Bold.ttf"),
        os.path.join("/app", "DejaVuSans-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]:
        if os.path.exists(_p): bold = _p; break
    regular = None
    for _p in [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-DemiLight.ttc",
        os.path.join(_dir, "DejaVuSans.ttf"),
        os.path.join("/app", "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]:
        if os.path.exists(_p): regular = _p; break
    _FONT_CACHE["bold"] = bold
    _FONT_CACHE["regular"] = regular
    return bold, regular


def generate_trade_card(symbol: str, chain: str, pnl_str: str, x_val: str, held_h: str, bought_str: str, position_str: str, username: str, pnl_pct: str, pnl_positive: bool, closed_at: datetime | None = None, bought_label: str = "Bought", position_label: str = "Position") -> "io.BytesIO | None":
    if not PILLOW_OK:
        return None
    try:
        W, H = 1100, 580
        img = Image.new("RGB", (W, H), color=(8, 10, 18))
        draw = ImageDraw.Draw(img)

        # ── Fonts: use cached path resolution ────────────────────────────────
        _bold, _regular = _resolve_fonts()

        try:
            if not _bold or not _regular:
                raise Exception("fonts missing")
            font_pill        = ImageFont.truetype(_bold,    68)
            font_label       = ImageFont.truetype(_regular, 30)
            font_value       = ImageFont.truetype(_bold,    30)
            font_brand       = ImageFont.truetype(_bold,    24)
            font_user        = ImageFont.truetype(_bold,    28)
            font_tiny        = ImageFont.truetype(_regular, 19)
            font_badge       = ImageFont.truetype(_bold,    22)
            font_stamp       = ImageFont.truetype(_regular, 24)
            font_stamp_bold  = ImageFont.truetype(_bold,    24)
        except Exception:
            font_pill = font_label = font_value = font_brand = font_user = \
            font_tiny = font_badge = font_stamp = font_stamp_bold = ImageFont.load_default()

        # ── Chain label & colours ─────────────────────────────────────────────
        chain_short  = {"solana":"SOL","sol":"SOL","ethereum":"ETH","eth":"ETH",
                        "base":"BASE","bsc":"BNB","bnb":"BNB","arbitrum":"ARB",
                        "arb":"ARB","polygon":"MATIC","matic":"MATIC",
                        "avalanche":"AVAX","avax":"AVAX","sui":"SUI"}
        chain_label  = chain_short.get(chain.lower(), chain.upper()[:4])
        chain_colors = {"SOL":(153,69,255),"ETH":(98,126,234),"BASE":(0,82,255),
                        "BNB":(243,186,47),"ARB":(40,160,240),"MATIC":(130,71,229),
                        "AVAX":(232,65,66),"SUI":(78,122,255)}
        badge_col = chain_colors.get(chain_label, (80,100,160))

        # ── Background gradient ───────────────────────────────────────────────
        for y in range(H):
            t = y / H
            draw.line([(0,y),(W,y)], fill=(int(8+t*4), int(10+t*6), int(18+t*14)))

        # ── Side glow (green win / red loss) ─────────────────────────────────
        glow_col = (0,45,20) if pnl_positive else (45,6,6)
        for i in range(100, 0, -1):
            draw.rectangle([0, 0, i*2, H], fill=glow_col)

        # ── Character art right side ──────────────────────────────────────────
        base_dir  = os.path.dirname(os.path.abspath(__file__))
        char_file = "win_char.jpg" if pnl_positive else "loss_char.jpg"
        char_path = os.path.join(base_dir, char_file)
        if not os.path.exists(char_path):
            char_path = os.path.join("/app", char_file)
        if os.path.exists(char_path):
            char   = Image.open(char_path).convert("RGBA")
            char_w = int(char.width * H / char.height)
            char   = char.resize((char_w, H), Image.LANCZOS)
            char_x = W - char_w - 5
            img.paste(char, (char_x, 0), char)
            # Fade edge so text on left stays readable
            overlay  = Image.new("RGBA", (W, H), (0,0,0,0))
            ov_draw  = ImageDraw.Draw(overlay)
            fade_end = char_x + 180
            for x in range(max(0, char_x), min(fade_end, W)):
                t2    = (x - char_x) / 180
                alpha = int((1 - t2) * 200)
                ov_draw.line([(x,0),(x,H)], fill=(8,10,18,alpha))
            img  = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
            draw = ImageDraw.Draw(img)

        # ── Chain badge (top left) ────────────────────────────────────────────
        badge_w = len(chain_label) * 15 + 30
        draw.rounded_rectangle([38, 22, 38+badge_w, 58], radius=12, fill=badge_col)
        draw.text((38+badge_w//2, 40), chain_label, font=font_badge, fill=(255,255,255), anchor="mm")

        # ── Brand (top right) ────────────────────────────────────────────────
        draw.text((W-40, 40), "APEX SNIPER BOT", font=font_brand, fill=(185,200,230), anchor="rm")

        # ── Token symbol — dynamic size so any ticker fits ───────────────────
        sym_display = "$" + symbol
        sym_font    = ImageFont.truetype(_bold, 86) if _bold else font_pill
        for size in [86, 72, 60, 48, 38]:
            _f   = ImageFont.truetype(_bold, size) if _bold else font_pill
            bbox = _f.getbbox(sym_display)
            if bbox[2] - bbox[0] < 620:
                sym_font = _f
                break
        draw.text((38, 75), sym_display, font=sym_font, fill=(240, 245, 255))

        # ── PnL pill ─────────────────────────────────────────────────────────
        clean_pnl      = pnl_str.lstrip("$")
        clean_bought   = bought_str.lstrip("$")
        clean_position = position_str.lstrip("$")
        pill_col = (0,200,105) if pnl_positive else (205,38,38)
        txt_col  = (5,15,8)   if pnl_positive else (255,235,235)
        prefix   = "+"        if pnl_positive else "-"
        draw.rounded_rectangle([38, 195, 590, 298], radius=20, fill=pill_col)
        draw.text((68, 246), chain_label + "  " + prefix + "$" + clean_pnl,
                  font=font_pill, fill=txt_col, anchor="lm")

        # ── Stats rows ───────────────────────────────────────────────────────
        pnl_col = (0,220,120) if pnl_positive else (220,75,75)
        stats = [
            ("PNL",           prefix + pnl_pct,                    pnl_col),
            (bought_label,    chain_label + " - $" + clean_bought, (195,210,235)),
            (position_label,  chain_label + " - $" + clean_position,(195,210,235)),
            ("Held",          held_h,                               (195,210,235)),
        ]
        for i, (lbl, val, vcol) in enumerate(stats):
            y = 322 + i * 50
            draw.text((38,  y), lbl, font=font_label, fill=(125,140,170))
            draw.text((370, y), val, font=font_value,  fill=vcol)

        # ── Divider — full width ──────────────────────────────────────────────
        draw.line([(38, H-72), (W-40, H-72)], fill=(35,45,68), width=1)

        # ── Bottom left: avatar circle + @username ───────────────────────────
        ax, ay = 55, H - 36
        draw.ellipse([ax-22, ay-22, ax+22, ay+22], fill=(50,70,140))
        draw.text((ax, ay), (username[0].upper() if username else "A"),
                  font=font_tiny, fill=(200,220,255), anchor="mm")
        draw.text((ax+32, ay), "@" + username, font=font_user,
                  fill=(200,215,240), anchor="lm")

        # ── Bottom right: time  •  date ──────────────────────────────────────
        ts = closed_at if closed_at else datetime.now()
        time_str = ts.strftime("%I:%M %p").lstrip("0")  # e.g. 3:40 PM  (cross-platform)
        date_str = ts.strftime("%b %d, %Y")           # e.g. Mar 05, 2026
        dot      = "  •  "

        # Right-align the three pieces: [time][dot][date]
        t_w  = font_stamp_bold.getbbox(time_str)[2] - font_stamp_bold.getbbox(time_str)[0]
        dt_w = font_stamp.getbbox(dot)[2]            - font_stamp.getbbox(dot)[0]
        d_w  = font_stamp.getbbox(date_str)[2]       - font_stamp.getbbox(date_str)[0]

        rx = W - 40
        draw.text((rx, ay), date_str,  font=font_stamp,      fill=(130,145,175), anchor="rm")
        rx -= d_w
        draw.text((rx, ay), dot,       font=font_stamp,      fill=(60,75,100),   anchor="rm")
        rx -= dt_w
        draw.text((rx, ay), time_str,  font=font_stamp_bold, fill=(185,200,230), anchor="rm")

        # ── Save ─────────────────────────────────────────────────────────────
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    except Exception as e:
        logger.error("Card generation error: " + str(e))
        return None


async def enrich_dev_wallet_history(contract: str, info: dict, api_key: str) -> dict:
    """
    Checks the deployer wallet's on-chain history via Helius.
    Looks at past tokens launched by same wallet — serial rugger detection.
    Returns: {dev_rug_count, dev_token_count, dev_rug_rate, dev_flags, dev_risk}
    """
    result = {
        "dev_rug_count":  0,
        "dev_token_count": 0,
        "dev_rug_rate":   0.0,
        "dev_flags":      [],
        "dev_risk":       "UNKNOWN",
    }
    if not api_key:
        return result

    try:
        client = await get_http()
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"

        # Step 1: get token mint account to find deployer/mint authority
        mint_resp = await client.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [contract, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        }, timeout=6)

        if mint_resp.status_code != 200:
            return result

        mint_data = mint_resp.json().get("result", {}).get("value", {})
        parsed    = mint_data.get("data", {}).get("parsed", {}).get("info", {}) if mint_data else {}

        # Get mint authority (deployer) — if None, was already renounced
        deployer = parsed.get("mintAuthority") or parsed.get("freezeAuthority")
        if not deployer:
            # Try RugCheck stored data
            deployer = info.get("deployer") or info.get("creator")
        if not deployer:
            result["dev_risk"] = "LOW"  # can't find deployer = likely renounced = good
            return result

        # Step 2: get recent transactions from deployer wallet
        tx_resp = await client.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 2,
            "method": "getSignaturesForAddress",
            "params": [deployer, {"limit": 50, "commitment": "confirmed"}]
        }, timeout=6)

        if tx_resp.status_code != 200:
            return result

        sigs = tx_resp.json().get("result", []) or []

        # Step 3: use Helius enhanced API to check what tokens this wallet created
        hist_resp = await client.get(
            f"https://api.helius.xyz/v0/addresses/{deployer}/transactions",
            params={"api-key": api_key, "limit": "50", "type": "CREATE_MINT"},
            timeout=7
        )

        tokens_launched = []
        if hist_resp.status_code == 200:
            txns = hist_resp.json() or []
            for tx in txns:
                for ti in tx.get("tokenTransfers", []):
                    mint = ti.get("mint", "")
                    if mint and mint != contract and mint not in tokens_launched:
                        tokens_launched.append(mint)

        result["dev_token_count"] = len(tokens_launched)

        if not tokens_launched:
            # No prior tokens — could be fresh wallet (red flag) or legit
            if len(sigs) < 5:
                result["dev_flags"].append("Fresh wallet — very few on-chain transactions")
                result["dev_risk"] = "MEDIUM"
            else:
                result["dev_risk"] = "LOW"
            return result

        # Step 4: check each prior token's current state (are they dead?)
        rug_count = 0
        checked   = 0
        for prev_contract in tokens_launched[:8]:  # check up to 8 prior tokens
            try:
                prev_info = await get_token(prev_contract)
                checked  += 1
                if not prev_info:
                    rug_count += 1  # can't fetch = likely dead/rugged
                    continue
                prev_liq = prev_info.get("liq", 0)
                prev_mc  = prev_info.get("mc",  0)
                prev_age = prev_info.get("age_h", 0)
                # Dead token: liq < $500, mc < $5K, and older than 2h
                if prev_liq < 500 and prev_mc < 5_000 and prev_age > 2:
                    rug_count += 1
            except Exception:
                rug_count += 1  # error = assume dead
            await _asyncio.sleep(0.1)  # don't hammer API

        if checked > 0:
            rug_rate = rug_count / checked
            result["dev_rug_count"] = rug_count
            result["dev_rug_rate"]  = round(rug_rate, 2)

            if rug_rate >= 0.8:
                result["dev_flags"].append(f"🚨 SERIAL RUGGER — {rug_count}/{checked} prior tokens dead")
                result["dev_risk"] = "HIGH"
            elif rug_rate >= 0.5:
                result["dev_flags"].append(f"⚠️ Dev rugged {rug_count}/{checked} prior tokens")
                result["dev_risk"] = "MEDIUM"
            elif rug_rate >= 0.3:
                result["dev_flags"].append(f"Dev has {rug_count} prior failed tokens")
                result["dev_risk"] = "MEDIUM"
            else:
                result["dev_risk"] = "LOW"
                if checked >= 3:
                    result["dev_flags"].append(f"✅ Dev history clean ({checked} prior tokens checked)")
        else:
            result["dev_risk"] = "LOW"

        return result

    except Exception as e:
        logger.debug(f"Dev wallet history check failed: {e}")
        return result


async def enrich_wallet_clustering(contract: str, top_holders: list, api_key: str) -> dict:
    """
    Checks if top holder wallets share a common funding source.
    If multiple top holders funded from same wallet = coordinated/insider group.
    Returns: {cluster_detected, cluster_pct, cluster_flags}
    """
    result = {
        "cluster_detected": False,
        "cluster_pct":      0.0,
        "cluster_flags":    [],
    }
    if not api_key or not top_holders:
        return result

    try:
        client  = await get_http()
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"

        funding_sources: dict = {}  # source_wallet -> [holder_addr, ...]
        holder_pcts:     dict = {}  # holder_addr -> pct

        for holder in top_holders[:8]:  # check top 8 holders
            addr = holder.get("addr_full") or holder.get("addr", "")
            pct  = holder.get("pct", 0)
            if not addr or len(addr) < 30:
                continue
            holder_pcts[addr] = pct

            # Get first few transactions of this wallet = funding source
            tx_resp = await client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [addr, {"limit": 5, "commitment": "confirmed"}]
            }, timeout=5)

            if tx_resp.status_code != 200:
                continue

            sigs = tx_resp.json().get("result", []) or []
            if not sigs:
                continue

            # Get the oldest (first) transaction = funding tx
            oldest_sig = sigs[-1].get("signature", "")
            if not oldest_sig:
                continue

            tx_detail = await client.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 2,
                "method": "getTransaction",
                "params": [oldest_sig, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}]
            }, timeout=5)

            if tx_detail.status_code != 200:
                continue

            tx = tx_detail.json().get("result", {})
            if not tx:
                continue

            # Find who funded this wallet (sender in first tx)
            accounts = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
            if accounts:
                funder = accounts[0].get("pubkey", "") if isinstance(accounts[0], dict) else str(accounts[0])
                if funder and funder != addr:
                    funding_sources.setdefault(funder, []).append(addr)

            await _asyncio.sleep(0.1)

        # Analyse clustering
        for funder, holders_from_same in funding_sources.items():
            if len(holders_from_same) >= 2:
                clustered_pct = sum(holder_pcts.get(h, 0) for h in holders_from_same)
                if clustered_pct >= 10:
                    result["cluster_detected"] = True
                    result["cluster_pct"]      = round(clustered_pct, 1)
                    result["cluster_flags"].append(
                        f"🚨 {len(holders_from_same)} wallets funded from same source — hold {clustered_pct:.1f}%"
                    )

        return result

    except Exception as e:
        logger.debug(f"Wallet clustering check failed: {e}")
        return result


async def enrich_volume_pattern(contract: str, api_key: str) -> dict:
    """
    Analyses individual swap transactions to detect manufactured volume.
    Real organic pumps: random sizes, many wallets, irregular timing.
    Fake pumps: round numbers, same wallets, regular intervals.
    Returns: {vol_organic_score, vol_flags, round_number_pct, wallet_repeat_pct}
    """
    result = {
        "vol_organic_score": -1,  # -1 = unknown/unenriched — score_token skips it
        "vol_flags":         [],
        "round_number_pct":  0.0,
        "wallet_repeat_pct": 0.0,
    }
    if not api_key:
        return result  # no Helius key — stays -1, no score impact

    try:
        client = await get_http()
        r = await client.get(
            f"https://api.helius.xyz/v0/addresses/{contract}/transactions",
            params={"api-key": api_key, "limit": "50", "type": "SWAP"},
            timeout=8
        )
        if r.status_code != 200:
            return result

        txns = r.json() or []
        if len(txns) < 5:
            return result

        amounts       = []
        wallets       = []
        timestamps    = []
        score         = 5   # neutral baseline once real tx data available

        for tx in txns:
            fee_payer = tx.get("feePayer", "")
            wallets.append(fee_payer)
            ts = tx.get("timestamp", 0)
            timestamps.append(ts)

            # Get SOL amount moved
            native = tx.get("nativeTransfers", []) or []
            sol_moved = sum(
                abs(t.get("amount", 0))
                for t in native
                if t.get("fromUserAccount") == fee_payer or t.get("toUserAccount") == fee_payer
            ) / 1e9
            if sol_moved > 0:
                amounts.append(sol_moved)

        if not amounts:
            return result

        # ── Check 1: Round number buys (fake organic) ─────────────────────────
        round_count = sum(1 for a in amounts if round(a, 1) == a and a > 0.1)
        round_pct   = round(round_count / len(amounts) * 100, 1)
        result["round_number_pct"] = round_pct
        if round_pct > 70:
            result["vol_flags"].append(f"⚠️ {round_pct}% round-number buys — likely bot activity")
            score -= 2

        # ── Check 2: Wallet repeat rate (same wallets buying repeatedly) ──────
        from collections import Counter
        wallet_counts  = Counter(wallets)
        repeat_wallets = sum(1 for w, c in wallet_counts.items() if c >= 3)
        repeat_pct     = round(repeat_wallets / max(len(set(wallets)), 1) * 100, 1)
        result["wallet_repeat_pct"] = repeat_pct
        if repeat_pct > 40:
            result["vol_flags"].append(f"⚠️ {repeat_pct}% wallets trading 3+ times — wash trading")
            score -= 2
        elif repeat_pct < 10:
            score += 1  # many unique wallets = organic

        # ── Check 3: Timing regularity (bots trade at fixed intervals) ────────
        if len(timestamps) >= 6:
            timestamps_sorted = sorted(timestamps)
            intervals = [timestamps_sorted[i+1] - timestamps_sorted[i]
                         for i in range(len(timestamps_sorted)-1) if timestamps_sorted[i] > 0]
            if intervals:
                avg_interval = sum(intervals) / len(intervals)
                # Low variance = bot (all trades equally spaced)
                variance = sum((x - avg_interval)**2 for x in intervals) / len(intervals)
                std_dev  = variance ** 0.5
                cv       = std_dev / avg_interval if avg_interval > 0 else 1
                if cv < 0.2:  # coefficient of variation < 20% = very regular = bot
                    result["vol_flags"].append("⚠️ Highly regular trade timing — bot pattern")
                    score -= 2
                elif cv > 0.8:  # high variance = organic irregular human buying
                    score += 1

        # ── Check 4: Unique buyer count ───────────────────────────────────────
        unique_buyers = len(set(wallets))
        if unique_buyers >= 20:
            score += 1
        elif unique_buyers < 8:
            result["vol_flags"].append(f"Only {unique_buyers} unique wallets in last 50 swaps")
            score -= 2

        result["vol_organic_score"] = max(0, min(10, score))
        return result

    except Exception as e:
        logger.debug(f"Volume pattern analysis failed: {e}")
        return result


async def get_token(contract: str, force: bool = False) -> dict | None:
    # ── Cache check ──────────────────────────────────────────────────────────
    if not force:
        cached = _token_cache.get(contract)
        if cached and (_time.time() - cached["ts"]) < CACHE_TTL:
            return cached["data"]
    try:
        client = await get_http()
        r = await client.get(DEXSCREENER_API.format(contract))
        if r.status_code == 429:
            # Rate limited — serve stale cache if we have any, otherwise retry once
            stale = _token_cache.get(contract)
            if stale:
                logger.debug(f"DexScreener 429 — serving stale cache for {contract[:12]}")
                return stale["data"]
            # No cache — wait briefly and retry once
            await _asyncio.sleep(0.5)
            try:
                r2 = await client.get(DEXSCREENER_API.format(contract))
                if r2.status_code != 200:
                    logger.warning(f"DexScreener HTTP {r2.status_code} for {contract} (retry)")
                    return None
                r = r2
            except Exception:
                return None
        elif r.status_code != 200:
            logger.warning(f"DexScreener HTTP {r.status_code} for {contract}")
            return None
        data = r.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        price = float(best.get("priceUsd") or 0)
        if not price:
            return None
        mc = float(best.get("marketCap") or best.get("fdv") or 0)
        liq = float(best.get("liquidity", {}).get("usd", 0) or 0)
        liq_pct = round((liq / mc * 100) if mc > 0 else 0, 2)
        vol_h24 = float(best.get("volume", {}).get("h24", 0) or 0)
        vol_h1  = float(best.get("volume", {}).get("h1", 0) or 0)
        vol_m5  = float(best.get("volume", {}).get("m5", 0) or 0)
        txns = best.get("txns", {})
        buys  = int(txns.get("h24", {}).get("buys",  0) or 0)
        sells = int(txns.get("h24", {}).get("sells", 0) or 0)
        total_tx = buys + sells
        buy_pct = round(buys / total_tx * 100) if total_tx > 0 else 50
        # Multi-timeframe txn data for M/T/V intelligence card
        def _bpct(b, s): return round(b / (b + s) * 100) if (b + s) > 0 else 50
        buys_m5  = int(txns.get("m5",  {}).get("buys",  0) or 0)
        sells_m5 = int(txns.get("m5",  {}).get("sells", 0) or 0)
        buys_h1  = int(txns.get("h1",  {}).get("buys",  0) or 0)
        sells_h1 = int(txns.get("h1",  {}).get("sells", 0) or 0)
        buys_h6  = int(txns.get("h6",  {}).get("buys",  0) or 0)
        sells_h6 = int(txns.get("h6",  {}).get("sells", 0) or 0)
        vol_h6   = float(best.get("volume", {}).get("h6", 0) or 0)
        pair_created = best.get("pairCreatedAt")
        age_h = None
        if pair_created:
            age_h = (datetime.now() - datetime.fromtimestamp(pair_created / 1000)).total_seconds() / 3600
        ch = best.get("priceChange", {})

        # Extract social links + token images
        socials_raw  = best.get("info", {}).get("socials", [])
        websites_raw = best.get("info", {}).get("websites", [])
        token_image  = best.get("info", {}).get("imageUrl", "")    # token logo
        header_image = best.get("info", {}).get("header", "")      # banner image
        _header_confirmed = bool(header_image)   # True = came from DexScreener directly
        # Fallback: DexScreener CDN banner URL pattern
        _chain_early = best.get("chainId", "").lower()
        if not header_image and _chain_early in ("solana", "sol"):
            _ca_early = best.get("baseToken", {}).get("address", "")
            if _ca_early:
                header_image = f"https://dd.dexscreener.com/ds-data/tokens/solana/{_ca_early}/header.png"
        twitter = ""
        telegram = ""
        website  = ""
        for s in socials_raw:
            stype = s.get("type","").lower()
            url   = s.get("url","")
            if stype in ("twitter","x") and not twitter:
                twitter = url
            elif stype == "telegram" and not telegram:
                telegram = url
        for w in websites_raw:
            url = w.get("url","")
            if url and not website:
                website = url

        # ATH — persistent across refreshes for accurate tracking.
        # Strategy:
        #   1. Reconstruct "recent ATH" from available priceChange windows (h24/h6/h1/m5).
        #      These are the best we can do from a single DexScreener call.
        #   2. Compare against the persistent _ath_cache (built up over multiple calls).
        #      If the cache holds a higher price, it is the true ATH.
        #   3. If the current price exceeds the cache, update the cache (new ATH).
        ath_price = 0.0
        ath_mc    = 0.0
        ath_ts    = 0.0   # unix timestamp when ATH was (approximately) set
        import time as _atht
        try:
            cur_price  = price
            ch_h24_v   = float(ch.get("h24", 0) or 0)
            ch_h6_v    = float(ch.get("h6",  0) or 0)
            ch_h1_v    = float(ch.get("h1",  0) or 0)
            ch_m5_v    = float(ch.get("m5",  0) or 0)
            # Reconstruct past prices from percentage changes
            p_24h = cur_price / (1 + ch_h24_v / 100) if ch_h24_v != -100 else cur_price
            p_6h  = cur_price / (1 + ch_h6_v  / 100) if ch_h6_v  != -100 else cur_price
            p_1h  = cur_price / (1 + ch_h1_v  / 100) if ch_h1_v  != -100 else cur_price
            p_m5  = cur_price / (1 + ch_m5_v  / 100) if ch_m5_v  != -100 else cur_price
            # Best reconstructed ATH from current API response
            reconstructed_ath = max(cur_price, p_24h, p_6h, p_1h, p_m5)

            # Merge with persistent cache
            cached = _ath_cache.get(contract, {})
            cached_price = cached.get("price", 0.0)

            if cur_price > cached_price:
                # New all-time high — update cache
                _ath_cache[contract] = {
                    "price": cur_price,
                    "mc":    mc,
                    "ts":    _atht.time(),
                }
                ath_price = cur_price
                ath_mc    = mc
                ath_ts    = _atht.time()
            elif reconstructed_ath > cached_price:
                # Reconstructed ATH beats cache (e.g. first ever call for this token)
                ath_price = reconstructed_ath
                ath_mc    = mc * (ath_price / cur_price) if cur_price > 0 else 0
                ath_ts    = 0.0  # unknown when it peaked
                # Seed the cache with the reconstructed value so future calls improve
                _ath_cache[contract] = {
                    "price": ath_price,
                    "mc":    ath_mc,
                    "ts":    0.0,
                }
            else:
                # Cached ATH is higher than anything we can reconstruct now
                ath_price = cached_price
                ath_mc    = cached.get("mc", mc * (ath_price / cur_price) if cur_price > 0 else 0)
                ath_ts    = cached.get("ts", 0.0)
        except Exception:
            pass

        # ── RugCheck security audit (Solana only, best-effort) ───────────────
        no_mint        = None
        freeze         = None
        lp_burn        = None
        top10          = None
        top20          = None
        insider        = None
        dev_pct_rc     = None
        rug_risks      = []
        rc_rate_limited = False   # True = 429 with no cache — security data unavailable
        rc_data         = None    # RugCheck response dict — None for non-Solana or on error
        chain_id_raw = best.get("chainId", "").lower()
        if chain_id_raw in ("solana", "sol"):
            try:
                # ── 5-minute RugCheck cache — cuts API calls by ~25x ─────────
                # Security data (mint auth, LP burn, holders) doesn't change in
                # 5 minutes. On a 429 we fall back to stale cache so security
                # data is never silently lost.
                _rc_cached = _rugcheck_cache.get(contract)
                _rc_fresh  = _rc_cached and (_time.time() - _rc_cached["ts"]) < RUGCHECK_CACHE_TTL
                if _rc_fresh:
                    rc_data = _rc_cached["data"]
                else:
                    rc_data = None
                    sem = await _get_rugcheck_semaphore()
                    async with sem:
                        await _asyncio.sleep(0.1)   # small delay to avoid burst — semaphore already limits concurrency
                        rc = await get_http()
                        rc_r = await rc.get(
                            f"https://api.rugcheck.xyz/v1/tokens/{contract}/report",
                            headers={"Accept": "application/json"},
                        )
                    if rc_r.status_code == 429:
                        if _rc_cached:
                            rc_data = _rc_cached["data"]
                            logger.debug(f"RugCheck 429 — serving stale cache for {contract[:12]}")
                        else:
                            rc_rate_limited = True
                            logger.debug(f"RugCheck 429 — no cache for {contract[:12]}, flagging rc_rate_limited")
                    elif rc_r.status_code in (500, 502, 503, 504):
                        # RugCheck server error — treat same as 429
                        # Serve stale cache if available; flag as rate-limited if not
                        if _rc_cached:
                            rc_data = _rc_cached["data"]
                            logger.debug(f"RugCheck {rc_r.status_code} — serving stale cache for {contract[:12]}")
                        else:
                            rc_rate_limited = True
                            logger.debug(f"RugCheck {rc_r.status_code} — no cache for {contract[:12]}, flagging rc_rate_limited")
                    elif rc_r.status_code == 200:
                        rc_data = rc_r.json()
                        _rugcheck_cache[contract] = {"data": rc_data, "ts": _time.time()}

                if rc_data:
                    # Mint / freeze authority
                    no_mint = rc_data.get("mintAuthority") is None
                    freeze  = rc_data.get("freezeAuthority") is None
                    # LP burn %
                    markets = rc_data.get("markets") or []
                    if markets:
                        lp_burn = markets[0].get("lp", {}).get("lpLockedPct", None)
                        if lp_burn is not None:
                            lp_burn = round(float(lp_burn), 1)
                    # Top holders
                    holders = rc_data.get("topHolders") or []
                    if holders:
                        # ── Filter out protocol/LP/AMM program addresses ──────────
                        # Pump.fun bonding curve, AMM pool, Raydium LP accounts are
                        # included in RugCheck's topHolders but are NOT real holders.
                        # They inflate top10% dramatically.
                        #
                        # Layer 1 — Static: well-known program IDs that never change.
                        _PROGRAM_IDS = {
                            "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun program
                            "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1", # pump.fun fee acct
                            "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",  # pump.fun bonding curve authority
                            "4wTV81aCK4cYbJGYLTBZaC7DqHBnZ7VJ4C3MeH9zGD5P", # raydium AMM authority
                            "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # raydium AMM authority v4
                            "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", # raydium AMM program
                            "7YttLkHDoNj9wyDur5pM1ejNaAvT9X4eqaYcHQqtj2G5", # pump.fun migration auth
                            "FRhB8L7Y9Qq41qZXYLtC2nw8An1RJfLLxRF2x9RwLLMo", # pump.fun AMM pool
                            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL token program
                            "11111111111111111111111111111111",                 # system program
                        }
                        # Layer 2 — Dynamic: per-token pool/bonding curve addresses
                        # from the RugCheck markets array. The pump.fun bonding curve
                        # is unique per token so it can never be in the static set —
                        # this was causing top10 to show 97%+ for pump.fun tokens.
                        _dynamic_pool_addrs: set = set()
                        for _mk in (rc_data.get("markets") or []):
                            _pk = (_mk.get("pubkey") or "").strip()
                            if _pk:
                                _dynamic_pool_addrs.add(_pk)
                            _lp_info = _mk.get("lp") or {}
                            for _lp_key in ("lpLockedInAccount", "address", "lpTokenMint"):
                                _lp_addr = (_lp_info.get(_lp_key) or "").strip()
                                if _lp_addr:
                                    _dynamic_pool_addrs.add(_lp_addr)
                            _mk_addr = (_mk.get("market") or "").strip()
                            if _mk_addr:
                                _dynamic_pool_addrs.add(_mk_addr)
                        if _dynamic_pool_addrs:
                            logger.debug(
                                f"Dynamic pool exclusion: {len(_dynamic_pool_addrs)} addrs "
                                f"for {contract[:12]}: {list(_dynamic_pool_addrs)[:3]}"
                            )
                        # Name-based filter
                        _PROTOCOL_NAME_PATTERNS = (
                            "pump_fun", "pump.fun", "pumpfun",
                            "raydium", "ray_amm",
                            "meteora", "orca_pool",
                        )

                        def _is_protocol(holder: dict) -> bool:
                            # Check address directly against static + dynamic sets
                            addr = (holder.get("address") or "").strip()
                            if addr in _PROGRAM_IDS or addr in _dynamic_pool_addrs:
                                return True
                            # Check owner field (the program that owns the account)
                            owner = (holder.get("owner") or "").strip()
                            if owner in _PROGRAM_IDS or owner in _dynamic_pool_addrs:
                                return True
                            # Check RugCheck-supplied name/label
                            name = (holder.get("name") or "").lower()
                            if any(p in name for p in _PROTOCOL_NAME_PATTERNS):
                                return True
                            # Check tags supplied by RugCheck
                            tags = {t.lower() for t in (holder.get("tags") or [])}
                            if tags & {"program", "amm", "pool", "liquidity",
                                       "lp_account", "pump_fun", "raydium"}:
                                return True
                            # Check account/accountType field
                            acct = (holder.get("account") or holder.get("accountType") or "").lower()
                            if acct in ("program", "pool", "amm", "lp"):
                                return True
                            return False

                        # Real holders only — used for ALL concentration metrics
                        real_holders = [h for h in holders if not _is_protocol(h)]
                        _filtered_out = len(holders) - len(real_holders)
                        if _filtered_out > 0:
                            _filtered_pct = round(sum(float(h.get("pct",0)) for h in holders if _is_protocol(h)), 1)
                            logger.debug(f"topHolders filter: removed {_filtered_out} protocol accounts ({_filtered_pct}%) for {contract[:12]}")

                        # ── Fallback: outlier pool/bonding curve detection ────────
                        # RugCheck doesn't always tag the per-token bonding curve.
                        # Detection logic:
                        #   1. Top holder > 2% (filters dust)
                        #   2. Top holder > 3x the second holder
                        #   3. ALL remaining holders are tiny (<1.5%)
                        # This catches pools at 3.7–97% while never filtering a legit
                        # dev wallet (those always have competition at ≥1.5%).
                        # ── Multi-pass outlier pool/bonding curve filter ──────────
                        # Catches protocol addresses missed by static list + dynamic extraction.
                        # Reference = mean of ALL holders excluding the top one.
                        # When dev=12% and others=8%,5%,3%... mean is high (~2.6%)
                        #   → ratio 12/2.6=4.6x → doesn't fire ✓
                        # When pool=20% and real holders=2%,2%,2%... mean is low (~2%)
                        #   → ratio 20/2=10x → fires ✓
                        # When 2 pools: remove pool1 first, then pool2 becomes top
                        #   → pool2/mean_of_real = still high → fires on pass 2 ✓
                        # Threshold: top > 8% AND ratio > 5x mean-excl-top
                        _removed_addrs = []
                        for _pass in range(5):
                            if len(real_holders) < 4:
                                break
                            _top_pct  = float(real_holders[0].get("pct", 0))
                            _rest_pcts = [float(h.get("pct", 0)) for h in real_holders[1:]]
                            _mean_rest = sum(_rest_pcts) / len(_rest_pcts) if _rest_pcts else 0
                            _is_outlier = (
                                _top_pct >= 8.0
                                and _mean_rest > 0
                                and _top_pct / _mean_rest > 5.0
                            )
                            if _is_outlier:
                                _addr = real_holders[0].get('address', '?')[:12]
                                _removed_addrs.append(f"{_addr}({_top_pct}%)")
                                real_holders = real_holders[1:]
                            else:
                                break
                        if _removed_addrs:
                            logger.debug(
                                f"Outlier pool filter: removed {len(_removed_addrs)} protocol accounts "
                                f"{_removed_addrs} for {contract[:12]}"
                            )

                        # Store filtered list on rc_data so the result dict can reference
                        # it for top_holder_amounts bundle detection without re-filtering.
                        rc_data["_real_holders"] = real_holders
                        # dev = first real holder (skip protocol-owned accounts)
                        dev_h      = real_holders[0] if real_holders else {}
                        dev_pct_rc = round(float(dev_h.get("pct", 0)), 2) if dev_h else None
                        top10 = round(sum(float(h.get("pct", 0)) for h in real_holders[:10]), 1)
                        top20 = round(sum(float(h.get("pct", 0)) for h in real_holders[:20]), 1)
                        # ── Wallet type breakdown from RugCheck tags ──────────
                        _sniper_pct   = 0.0
                        _fresh_pct    = 0.0
                        _smart_pct    = 0.0
                        _bundle_pct   = 0.0
                        _sniper_count = 0
                        _smart_count  = 0
                        for _h in real_holders:
                            _hpct  = float(_h.get("pct", 0))
                            _htags = [t.lower() for t in (_h.get("tags") or [])]
                            _hins  = _h.get("insider", False)
                            if "sniper" in _htags:
                                _sniper_pct   += _hpct
                                _sniper_count += 1
                            if "fresh" in _htags or "new_wallet" in _htags:
                                _fresh_pct    += _hpct
                            if "smart_money" in _htags or "smartmoney" in _htags or "smart" in _htags:
                                _smart_pct   += _hpct
                                _smart_count += 1
                            if "bundle" in _htags or ("sniper" in _htags and _hins):
                                _bundle_pct  += _hpct
                        if _sniper_count > 0 or _smart_count > 0 or _bundle_pct > 0:
                            rc_data["_sniper_pct"]   = round(_sniper_pct, 1)
                            rc_data["_sniper_count"] = _sniper_count
                            rc_data["_fresh_pct"]    = round(_fresh_pct, 1)
                            rc_data["_smart_pct"]    = round(_smart_pct, 1)
                            rc_data["_smart_count"]  = _smart_count
                            rc_data["_bundle_pct"]   = round(_bundle_pct, 1)
                    # Insider / dev %
                    insider_pct = rc_data.get("insiderNetworkStats", {}).get("insiderPct", None)
                    if insider_pct is not None:
                        insider = round(float(insider_pct), 1)
                    # Risk flags
                    for risk in rc_data.get("risks", []):
                        lvl    = risk.get("level", "").lower()
                        name_r = risk.get("name", "")
                        if lvl in ("danger", "warn") and name_r:
                            rug_risks.append(name_r)
            except Exception as rc_err:
                logger.debug(f"RugCheck fetch failed: {rc_err}")

        result = {
            "symbol":   best.get("baseToken", {}).get("symbol", "???"),
            "name":     best.get("baseToken", {}).get("name", "Unknown"),
            "chain":    best.get("chainId", "unknown"),
            "dex":      best.get("dexId", "unknown"),
            "pair_addr":best.get("pairAddress",""),
            "price":    price,
            "mc":       mc,
            "liq":      liq,
            "liq_pct":  liq_pct,
            "vol_h24":  vol_h24,
            "vol_h1":   vol_h1,
            "vol_m5":   vol_m5,
            "ch_m5":    float(ch.get("m5",  0) or 0),
            "ch_h1":    float(ch.get("h1",  0) or 0),
            "ch_h6":    float(ch.get("h6",  0) or 0),
            "ch_h24":   float(ch.get("h24", 0) or 0),
            "buys":     buys,
            "sells":    sells,
            "buy_pct":  buy_pct,
            "age_h":    age_h,
            "twitter":   twitter,
            "telegram":  telegram,
            "website":   website,
            "token_image":  token_image,   # logo URL
            "header_image": header_image,       # banner URL
            "_header_confirmed": _header_confirmed,
            "ath_price": round(ath_price, 12) if ath_price else 0,
            "ath_mc":    round(ath_mc, 2)    if ath_mc    else 0,
            "ath_ts":    ath_ts,   # unix timestamp when ATH was recorded (0 = unknown)
            # Multi-timeframe trade data (for M/T/V intelligence display)
            "buys_m5":    buys_m5,   "sells_m5":  sells_m5,
            "buys_h1":    buys_h1,   "sells_h1":  sells_h1,
            "buys_h6":    buys_h6,   "sells_h6":  sells_h6,
            "buy_pct_m5": _bpct(buys_m5, sells_m5),
            "buy_pct_h1": _bpct(buys_h1, sells_h1),
            "buy_pct_h6": _bpct(buys_h6, sells_h6),
            "vol_h6":     vol_h6,
            # RugCheck security fields (None = not available)
            "no_mint":   no_mint,
            "no_freeze": freeze,
            "lp_burn":   lp_burn,
            "top10_pct":   top10,
            "top20_pct":   top20,
            "dev_pct_rc":  dev_pct_rc,
            "insider_pct": insider,
            "rug_risks":   rug_risks,
            "rc_rate_limited": rc_rate_limited,  # True = 429 with no cache
            # Raw top holder amounts for uniform-distribution bundle detection
            # Each entry: {"pct": float, "amount": float}
            # Uses real_holders (protocol accounts already filtered) so bundle
            # detection isn't poisoned by the bonding curve holding ~95% of supply.
            "top_holder_amounts": [
                {"pct": float(h.get("pct", 0)), "amount": float(h.get("amount", 0))}
                for h in (rc_data.get("_real_holders") or rc_data.get("topHolders") or [])[:10]
            ] if rc_data else [],
            # Full address list for inline holder links in token_card.
            # Each entry: {"address": str, "pct": float, "tags": list}
            "top_holders_data": [
                {
                    "address": h.get("address", ""),
                    "pct":     round(float(h.get("pct", 0)), 2),
                    "tags":    [t.lower() for t in (h.get("tags") or [])],
                    "insider": bool(h.get("insider", False)),
                }
                for h in (rc_data.get("_real_holders") or [])[:10]
            ] if rc_data else [],
            # Wallet intelligence (from RugCheck topHolders tags)
            "sniper_pct":    rc_data.get("_sniper_pct",   None) if rc_data else None,
            "sniper_count":  rc_data.get("_sniper_count", None) if rc_data else None,
            "fresh_wallet_pct": rc_data.get("_fresh_pct", None) if rc_data else None,
            "smart_wallet_pct": rc_data.get("_smart_pct", None) if rc_data else None,
            "smart_wallet_count": rc_data.get("_smart_count", None) if rc_data else None,
            "bundle_wallet_pct": rc_data.get("_bundle_pct", None) if rc_data else None,
            # Pump.fun enrichment (filled by sniper_scan when available)
            "pf_curve":     None,   # bonding curve % 0-100
            "pf_replies":   0,      # community reply count
            "pf_graduated": False,  # True = graduated to Raydium
            "pf_dev_pct":   None,   # dev holding %
            # Helius wallet intelligence (filled when API key set)
            "maker_pct":    None,   # unique buyer wallet % (requires Helius)
            "maker_count":  None,   # unique buyer wallets
            "top3_vol_pct": None,   # top 3 wallet % of volume (wash trade signal)
            # Boost spend (social attention proxy)
            "boost_amount": 0,      # total SOL spent boosting on DexScreener
        }

        # ── Helius holder enrichment — overwrites RugCheck top10/holders_data ─
        # Only for Solana tokens. RugCheck holder data is often stale/inaccurate;
        # Helius RPC reads directly from on-chain token accounts. Falls back
        # gracefully if Helius key not set or call fails.
        _helius_key_gt = os.environ.get("HELIUS_API_KEY", "")
        if _helius_key_gt and chain_id_raw in ("solana", "sol"):
            try:
                _hh = await get_helius_top_holders(contract, _helius_key_gt)
                if _hh:
                    result["top10_pct"]      = _hh["top10_pct"]
                    result["top20_pct"]      = _hh["top20_pct"]
                    result["top_holders_data"] = [
                        {"address": h["address"], "pct": h["pct"],
                         "tags": [], "insider": False}
                        for h in _hh["holders_data"][:10]
                    ]
                    result["top_holder_amounts"] = [
                        {"pct": h["pct"], "amount": h["amount"]}
                        for h in _hh["holders_data"][:10]
                    ]
                    result["_holders_source"] = "helius_rpc"
            except Exception as _hhe:
                logger.debug(f"Helius holder enrichment failed: {_hhe}")

        _token_cache[contract] = {"data": result, "ts": _time.time()}
        return result
    except Exception as e:
        logger.error(f"DexScreener: {e}")
        return None


def sniper_score(info: dict) -> dict:
    """
    Sniper scoring — tightened rug filters.
    Max 100 pts. Hard flags auto-fail.
    """
    score     = 0
    strengths = []
    warnings  = []
    flags     = []

    age_h        = info.get("age_h") or 0
    liq          = info.get("liq", 0)
    mc           = info.get("mc", 0)
    liq_pct      = info.get("liq_pct", 0)
    buy_pct      = info.get("buy_pct", 50)
    buy_pct_h1   = info.get("buy_pct_h1", buy_pct)
    buy_pct_m5   = info.get("buy_pct_m5", buy_pct)
    buys         = info.get("buys", 0)
    sells        = info.get("sells", 0)
    buys_h1      = info.get("buys_h1", 0)
    sells_h1     = info.get("sells_h1", 0)
    buys_m5      = info.get("buys_m5", 0)
    sells_m5     = info.get("sells_m5", 0)
    vol_h1       = info.get("vol_h1", 0)
    vol_m5       = info.get("vol_m5", 0)
    vol_h24      = info.get("vol_h24", 0)
    ch_m5        = info.get("ch_m5", 0)
    ch_h1        = info.get("ch_h1", 0)
    twitter      = info.get("twitter", "")
    telegram     = info.get("telegram", "")
    website      = info.get("website", "")
    no_mint      = info.get("no_mint")
    no_freeze    = info.get("no_freeze")
    lp_burn      = info.get("lp_burn")
    top10_pct    = info.get("top10_pct")
    insider_pct  = info.get("insider_pct")
    rug_risks    = info.get("rug_risks", []) or []
    pf_curve     = info.get("pf_curve")
    pf_replies   = info.get("pf_replies", 0) or 0
    pf_graduated = info.get("pf_graduated", False)
    maker_pct    = info.get("maker_pct")
    top3_vol_pct = info.get("top3_vol_pct")
    boost_amount = info.get("boost_amount", 0) or 0
    maker_count      = info.get("maker_count") or 0
    dev_pct          = float(info.get("pf_dev_pct") or info.get("dev_pct_rc") or 0)
    is_solana        = info.get("chain", "").lower() in ("solana", "sol")
    # New enrichment fields
    dev_rug_rate     = info.get("dev_rug_rate", -1.0)
    dev_risk         = info.get("dev_risk", "UNKNOWN")
    dev_flags_new    = info.get("dev_flags", [])
    cluster_detected = info.get("cluster_detected", False)
    cluster_pct      = info.get("cluster_pct", 0.0)
    vol_organic      = info.get("vol_organic_score", -1)
    vol_flags_new    = info.get("vol_flags", [])
    tw_fresh         = info.get("tw_is_fresh_acct", False)
    tw_age_d         = info.get("tw_account_age_d")
    # Wallet intelligence
    sniper_pct        = info.get("sniper_pct")       or 0.0
    sniper_count      = info.get("sniper_count")     or 0
    fresh_wallet_pct  = info.get("fresh_wallet_pct") or 0.0
    smart_wallet_pct  = info.get("smart_wallet_pct") or 0.0
    smart_wallet_cnt  = info.get("smart_wallet_count") or 0
    bundle_wallet_pct = info.get("bundle_wallet_pct") or 0.0

    # ── RUG RISK PARSING ──────────────────────────────────────────────────────
    danger_str   = [r for r in rug_risks if isinstance(r, str)]
    bundle_flag  = any("bundle" in r.lower() for r in danger_str)
    dev_sold     = any("deployer sold" in r.lower() or "creator sold" in r.lower() for r in danger_str)
    copycat      = any("copycat" in r.lower() for r in danger_str)
    honeypot     = any("honeypot" in r.lower() for r in danger_str)

    # INSTANT HARD FLAGS — these are rug signals, not warnings
    if honeypot:
        flags.append("🚨 Honeypot detected — cannot sell")
    # Bundle: use actual wallet % if available, fall back to RugCheck risk string
    _effective_bundle_pct = bundle_wallet_pct if bundle_wallet_pct > 0 else (sniper_pct if sniper_count >= 3 else 0.0)
    if _effective_bundle_pct >= 25.0:
        flags.append(f"🚨 Bundle bags hold {_effective_bundle_pct:.1f}% — coordinated dump risk")
    elif bundle_flag and _effective_bundle_pct == 0.0:
        # RugCheck flagged bundle but we have no pct data — still flag but softer
        flags.append("🚨 Bundle sniped at launch — insider pump")
    elif sniper_count >= 3 and sniper_pct >= 15.0:
        warnings.append(f"⚠️ {sniper_count} sniper wallets hold {sniper_pct:.1f}%")
    # Dev sold: community takeover possible — downgrade to warning unless bundle bags also present
    if dev_sold:
        if _effective_bundle_pct >= 25.0:
            flags.append("🚨 Dev sold + bundle bags holding — dump setup")
        else:
            warnings.append("⚠️ Dev sold — watch for community takeover or abandon")
    if copycat:
        flags.append("🚨 Copycat token")
    if no_mint is False:
        flags.append("🚨 Mint authority active — supply can inflate")
    if liq < 4_000 and not pf_curve:
        flags.append(f"🚨 Liq ${liq:,.0f} — too thin to trade")
    if liq_pct < 5 and mc > 0 and not pf_curve:
        flags.append(f"🚨 Liq only {liq_pct:.1f}% of MC — drain risk")
    if top10_pct is not None and top10_pct > 40:
        flags.append(f"🚨 Top10 wallets hold {top10_pct:.1f}% — whale trap")
    if insider_pct is not None and insider_pct > 25:
        flags.append(f"🚨 Insider/dev holds {insider_pct:.1f}% — dump risk")
    elif insider_pct is not None and insider_pct > 10:
        warnings.append(f"⚠️ Insider holds {insider_pct:.1f}% — elevated risk")
    if dev_pct > 20:
        flags.append(f"🚨 DEV wallet holds {dev_pct:.1f}% — dump risk")
    elif dev_pct > 10:
        warnings.append(f"⚠️ DEV wallet holds {dev_pct:.1f}% — watch for dump")
    if top3_vol_pct is not None and top3_vol_pct > 65 and vol_h1 > 20_000:
        flags.append(f"🚨 Top 3 wallets = {top3_vol_pct:.0f}% of volume — wash trade")
    elif top3_vol_pct is not None and top3_vol_pct > 65:
        warnings.append(f"Top 3 wallets = {top3_vol_pct:.0f}% of volume (thin market)")
    vol_mc_ratio = (vol_h1 / mc) if mc > 0 else 0
    if vol_mc_ratio > 5:
        flags.append(f"🚨 Vol/MC={vol_mc_ratio:.1f}x — likely wash trading")
    if maker_count > 0 and maker_count < 30:
        flags.append(f"🚨 Only {maker_count} unique wallets — too few real buyers")
    # Dev wallet history flags
    if dev_risk == "HIGH":
        flags.append(f"🚨 Serial rugger — dev wallet history shows rug pattern")
    elif dev_risk == "MEDIUM" and dev_rug_rate >= 0.5:
        flags.append(f"⚠️ Dev rugged {round(dev_rug_rate*100)}% of prior tokens")
    # Wallet clustering flags
    if cluster_detected and cluster_pct >= 20:
        flags.append(f"🚨 Wallet cluster: {cluster_pct}% held by coordinated insiders")
    # Twitter fresh account flag
    if tw_fresh:
        flags.append("🚨 Twitter account created same week as token — scam signal")
    # Volume pattern flags — only fire when data confidence is high (score >= 0 means data available)
    # vol_organic = -1 means Helius data unavailable, don't penalise
    if vol_organic >= 0 and vol_organic <= 2:
        flags.append(f"🚨 Volume pattern analysis: likely manufactured ({vol_organic}/10 organic)")
    elif vol_organic == 3:
        warnings.append(f"Suspicious volume pattern ({vol_organic}/10 organic)")

    # ── BUNDLE PATTERN DETECTION — 4 on-chain fingerprints ───────────────────
    #
    # These checks catch bundle launches that RugCheck misses because the wallets
    # aren't tagged yet (too fresh). They use data already returned by DexScreener.
    #
    # Check 1: Identical timeframe transactions
    #   M5 txns ≈ H1 txns ≈ H24 txns is IMPOSSIBLE for organic trading.
    #   It means ALL activity happened in one burst — classic bundle launch.
    #   Only applies when token is < 30 min old (H1/H24 wouldn't differ yet otherwise).
    _t_m5  = buys_m5 + sells_m5
    _t_h1  = buys_h1 + sells_h1
    _t_h24 = buys + sells
    if (age_h < 0.5 and _t_m5 > 0 and _t_h1 > 0 and _t_h24 > 0
            and abs(_t_m5 - _t_h1) <= 2 and abs(_t_h1 - _t_h24) <= 2):
        flags.append(
            f"🚨 Bundle launch pattern — M5/H1/H24 txns identical "
            f"({_t_m5}/{_t_h1}/{_t_h24}) — all activity in one burst"
        )

    # Check 2: Volume collapse across timeframes
    #   H1 vol ≈ H24 vol means zero sustained trading — token is dead/artificial.
    #   Real tokens show H24 vol >> H1 vol as trading builds over time.
    #   Only flag when vol is non-trivial (> $500) to avoid false positives on dust.
    if (vol_h24 > 500 and vol_h1 > 500
            and abs(vol_h24 - vol_h1) / vol_h24 < 0.05):   # within 5%
        flags.append(
            f"🚨 Vol collapse — H1 vol ≈ H24 vol (${vol_h1:,.0f} ≈ ${vol_h24:,.0f}) "
            f"— no sustained organic activity"
        )

    # Check 3: Combined dev + concentration — both above moderate thresholds
    #   A single metric above threshold can be a legit project.
    #   Both elevated simultaneously = coordinated insider control.
    if dev_pct > 15 and top10_pct is not None and top10_pct > 35:
        flags.append(
            f"🚨 Insider control — Dev {dev_pct:.1f}% + Top10 {top10_pct:.1f}% "
            f"— coordinated dump risk"
        )

    # Check 4: Age + concentration combo
    #   Bundles always front-run the launch. If token is < 30 min old AND
    #   top10 already holds > 40%, those wallets were there from block 1.
    if age_h < 0.5 and top10_pct is not None and top10_pct > 40:
        flags.append(
            f"🚨 Launch bundle — token {round(age_h*60)}m old, "
            f"Top10 already holds {top10_pct:.1f}% — sniped at birth"
        )

    # Check 5: Uniform holder amounts — multi-wallet bundle fingerprint
    #   When one person buys a large supply and splits across wallets,
    #   each wallet ends up with nearly identical token amounts.
    #   Real organic buyers always buy different amounts at different times.
    #   Method: compute coefficient of variation (std/mean) of top holder amounts.
    #   CV < 0.15 across 4+ holders = suspiciously uniform = multi-wallet bundle.
    _holder_amts = info.get("top_holder_amounts", [])
    if len(_holder_amts) >= 4:
        _amounts = [h["amount"] for h in _holder_amts if h["amount"] > 0]
        if len(_amounts) >= 4:
            _mean = sum(_amounts) / len(_amounts)
            if _mean > 0:
                _variance = sum((a - _mean) ** 2 for a in _amounts) / len(_amounts)
                _cv = (_variance ** 0.5) / _mean   # coefficient of variation
                if _cv < 0.15:
                    # Very uniform — check that combined % is meaningful (> 20%)
                    _combined_pct = sum(h["pct"] for h in _holder_amts[:len(_amounts)])
                    if _combined_pct >= 20:
                        flags.append(
                            f"🚨 Uniform holder amounts (CV={round(_cv, 3)}) — "
                            f"top {len(_amounts)} wallets hold near-identical token counts "
                            f"({_combined_pct:.1f}% combined) — multi-wallet bundle"
                        )

    # ── CATEGORY 1: SAFETY (0–30 pts) ────────────────────────────────────────
    if is_solana:
        if lp_burn is not None:
            if lp_burn >= 90:   score += 12; strengths.append(f"🔒 LP burned {lp_burn}%")
            elif lp_burn >= 50: score += 8;  strengths.append(f"LP {lp_burn}% burned")
            elif lp_burn >= 20: score += 3;  warnings.append(f"LP only {lp_burn}% burned")
            else:               warnings.append(f"LP barely burned ({lp_burn}%)")
        else:
            score += 3
    else:
        score += 6

    if no_mint is True:   score += 8; strengths.append("✅ No mint authority")
    if no_freeze is True: score += 5; strengths.append("✅ No freeze authority")
    elif no_freeze is False: warnings.append("Freeze authority enabled")

    # Healthy top10 bonus
    if top10_pct is not None:
        if top10_pct <= 20:   score += 5; strengths.append(f"🟢 Low concentration (top10={top10_pct:.0f}%)")
        elif top10_pct <= 35: score += 3
        elif top10_pct <= 50: warnings.append(f"Top10 at {top10_pct:.0f}% — watch for dump")

    # DEV green signal
    if dev_pct > 0 and dev_pct <= 3:
        score += 3; strengths.append(f"Low dev holding ({dev_pct:.1f}%)")
    elif dev_pct > 20 and dev_pct <= 40:
        score -= 8; warnings.append(f"Dev holds {dev_pct:.1f}% — monitor for dump")
    elif dev_pct > 10 and dev_pct <= 20:
        score -= 3; warnings.append(f"Dev holds {dev_pct:.1f}%")
    # Dev history bonus
    if dev_risk == "LOW" and dev_rug_rate >= 0:
        score += 5; strengths.append(f"✅ Dev history clean — no prior rugs")
    elif dev_risk == "MEDIUM":
        score -= 5
    # Wallet clustering penalty
    if cluster_detected:
        score -= 8; warnings.append(f"Coordinated wallets hold {cluster_pct}%")
    elif cluster_pct == 0 and top10_pct is not None:
        score += 2; strengths.append("✅ No wallet clustering detected")
    # Volume organics bonus
    if vol_organic >= 8:
        score += 4; strengths.append(f"Organic volume pattern ({vol_organic}/10)")
    elif vol_organic >= 6:
        score += 2
    elif 0 <= vol_organic <= 4:
        score -= 4; warnings.append(f"Suspicious volume pattern ({vol_organic}/10)")
    # Twitter account age
    if tw_age_d is not None:
        if tw_age_d > 365:
            score += 3; strengths.append(f"Established Twitter ({tw_age_d}d old)")
        elif tw_age_d < 7:
            score -= 5; warnings.append(f"Twitter only {tw_age_d}d old")

    # ── CATEGORY 2: LAUNCH TIMING (0–20 pts) ─────────────────────────────────
    if pf_curve is not None and is_solana:
        if pf_graduated:
            score += 12; strengths.append("🎓 Graduated to Raydium — proven demand")
        elif 30 <= pf_curve <= 65:
            score += 20; strengths.append(f"⚡ Sweet spot curve ({pf_curve}%)")
        elif 15 <= pf_curve < 30:
            score += 12; strengths.append(f"Early curve ({pf_curve}%) — fresh")
        elif 65 < pf_curve < 85:
            score += 8;  warnings.append(f"Curve {pf_curve}% — near graduation")
        elif pf_curve >= 85 and not pf_graduated:
            score += 5;  warnings.append(f"Curve {pf_curve}% — graduation imminent, may dump")
        elif pf_curve < 5:
            warnings.append(f"Curve only {pf_curve}% — very early, mostly bots")
        else:
            score += 4
    else:
        if age_h < 0.25:    score += 4;  warnings.append("Under 15min — bots still active")
        elif age_h < 0.5:   score += 16; strengths.append("🔥 Very fresh (15–30min)")
        elif age_h < 1.5:   score += 20; strengths.append("⚡ Optimal window (30min–1.5h)")
        elif age_h < 3:     score += 12; strengths.append("Early entry (1.5–3h)")
        elif age_h < 5:     score += 6;  warnings.append(f"Getting late ({round(age_h,1)}h)")
        elif age_h < 6:     score += 2;  warnings.append(f"Nearly too old ({round(age_h,1)}h)")

    # ── CATEGORY 3: SOCIAL ATTENTION (0–15 pts) ──────────────────────────────
    social_pts   = 0
    social_count = sum([bool(twitter), bool(telegram), bool(website)])
    if social_count >= 3:
        social_pts += 8; strengths.append("Full socials (TW+TG+Web)")
    elif social_count == 2:
        social_pts += 5; strengths.append("Twitter + Telegram confirmed")
    elif social_count == 1:
        social_pts += 1; warnings.append("Only 1 social — incomplete profile")
    # No socials handled as flag in pre-filter

    if boost_amount >= 50:  social_pts += 5; strengths.append(f"High boost ({boost_amount:.0f} SOL)")
    elif boost_amount >= 20: social_pts += 3; strengths.append(f"Boost active ({boost_amount:.0f} SOL)")
    elif boost_amount >= 5:  social_pts += 1

    if pf_replies >= 100: social_pts += 4; strengths.append(f"Active community ({pf_replies} replies)")
    elif pf_replies >= 40: social_pts += 2

    score += min(social_pts, 15)

    # ── CATEGORY 4: ENTRY MC (0–15 pts) ──────────────────────────────────────
    if 20_000 <= mc <= 80_000:
        score += 15; strengths.append(f"🎯 Micro cap ({mc_str(mc)})")
    elif 80_000 < mc <= 300_000:
        score += 12; strengths.append(f"Good entry ({mc_str(mc)})")
    elif 300_000 < mc <= 800_000:
        score += 7;  warnings.append(f"Mid MC ({mc_str(mc)}) — less upside")
    elif 800_000 < mc <= 2_000_000:
        score += 3;  warnings.append(f"High MC ({mc_str(mc)})")
    elif mc < 20_000:
        score += 3;  warnings.append(f"Very low MC — ultra risky")
    else:
        warnings.append(f"Already pumped ({mc_str(mc)})")

    # ── CATEGORY 5: ORGANIC SPREAD (0–10 pts) ────────────────────────────────
    h1_tx   = buys_h1 + sells_h1
    h1_buys = buys_h1 if h1_tx > 0 else buys
    eff_age = max(age_h, 0.25)
    buyers_per_hour = h1_buys / min(eff_age, 1)

    if buyers_per_hour >= 80:   score += 10; strengths.append(f"🚀 Viral ({int(buyers_per_hour)}/hr buyers)")
    elif buyers_per_hour >= 40: score += 8;  strengths.append(f"Strong spread ({int(buyers_per_hour)}/hr)")
    elif buyers_per_hour >= 15: score += 5
    elif buyers_per_hour >= 5:  score += 2;  warnings.append(f"Low buyer rate ({int(buyers_per_hour)}/hr)")
    else:                        warnings.append("Very few buyers — thin interest")

    avg_buy_size = (vol_h1 / h1_buys) if h1_buys > 0 else 0
    if 0 < avg_buy_size < 200:
        score += 3; strengths.append(f"Retail organic (avg ${avg_buy_size:.0f})")
    elif avg_buy_size > 2_000:
        warnings.append(f"Large avg buy (${avg_buy_size:,.0f}) — whale dominated")

    # Healthy holder count
    if maker_count >= 200: score += 2; strengths.append(f"Healthy holders ({maker_count})")
    elif maker_count >= 80: score += 1

    # ── CATEGORY 6: BUY PRESSURE + MOMENTUM (0–10 pts) ───────────────────────
    momentum_pts = 0
    if buy_pct_h1 >= 65:   momentum_pts += 6; strengths.append(f"Dominant buy pressure ({buy_pct_h1}%)")
    elif buy_pct_h1 >= 57: momentum_pts += 4
    elif buy_pct_h1 >= 53: momentum_pts += 2
    else: warnings.append(f"Weak buy pressure H1 ({buy_pct_h1}%)")

    if buy_pct_m5 >= 62 and ch_m5 > 0:
        momentum_pts += 4; strengths.append(f"Accelerating now (5m:{ch_m5:+.1f}%)")
    elif ch_m5 > 0 and ch_h1 > 0 and ch_h1 < 150:
        momentum_pts += 2; strengths.append(f"Building (5m:{ch_m5:+.1f}% 1h:{ch_h1:+.1f}%)")
    elif ch_h1 >= 200:
        momentum_pts += 0; warnings.append(f"Parabolic (+{ch_h1:.0f}%) — late entry")
    elif ch_m5 < -10 and ch_h1 < -10:
        warnings.append(f"Dumping ({ch_m5:.1f}% / {ch_h1:.1f}%)")

    vol_h1_per_5m = (vol_h1 / 12) if vol_h1 > 0 else 0
    if vol_m5 > vol_h1_per_5m * 2 and vol_m5 > 500:
        momentum_pts += 2; strengths.append("Volume spike now 🔥")

    if maker_pct is not None:
        if maker_pct >= 60:   momentum_pts += 3; strengths.append(f"Healthy maker dist ({maker_pct}%)")
        elif maker_pct >= 50: momentum_pts += 1
        elif maker_pct < 35:  warnings.append(f"Few unique buyers ({maker_pct}%) — possible shill")

    score += min(momentum_pts, 10)

    # ── CATEGORY 7: PUMPFUN BONUS (0–5 pts) ──────────────────────────────────
    if is_solana and pf_curve is not None:
        if pf_graduated: score += 5
        elif pf_curve > 40 and age_h < 2:
            score += 3; strengths.append(f"Fast curve fill ({pf_curve}% in {round(age_h,1)}h)")

    # ── CATEGORY 8: VELOCITY BONUS (0–8 pts) ───────────────────────────────────
    velocity = info.get("buy_pct_velocity", 0.0)
    if velocity >= 10:
        score += 8; strengths.append(f"🚀 Buy pressure accelerating (+{velocity:.0f}%)")
    elif velocity >= 5:
        score += 5; strengths.append(f"📈 Momentum building (+{velocity:.0f}%)")
    elif velocity >= 2:
        score += 2
    elif velocity <= -8:
        score -= 8; warnings.append(f"📉 Buy pressure fading ({velocity:.0f}%)")
    elif velocity <= -4:
        score -= 4; warnings.append(f"Buy pressure declining ({velocity:.0f}%)")

    # ── WALLET INTELLIGENCE SCORING ────────────────────────────────────────────
    # Smart wallet entry is a strong signal — known profitable wallets buying early
    if smart_wallet_cnt >= 2:
        score += 12; strengths.append(f"💰 {smart_wallet_cnt} smart wallets entered")
    elif smart_wallet_cnt == 1:
        score += 7;  strengths.append(f"💰 Smart wallet entry detected")
    # Fresh wallet penalty: >20% held by brand new wallets = wash/shill
    if fresh_wallet_pct >= 20.0:
        score -= 6;  warnings.append(f"Fresh wallets hold {fresh_wallet_pct:.1f}%")
    elif fresh_wallet_pct >= 10.0:
        score -= 2

    # ── CATEGORY 9: KOL SIGNAL BONUS (0–20 pts) ──────────────────────────────
    kol_count  = info.get("kol_buy_count", 0)
    kol_sol    = info.get("kol_sol_total", 0)
    kol_labels = info.get("kol_labels", [])
    if kol_count >= 2:
        score += 20; strengths.append(f"👀 {kol_count} KOL wallets bought ({kol_sol:.1f} SOL)")
    elif kol_count == 1:
        _kl = kol_labels[0] if kol_labels else "KOL wallet"
        if kol_sol >= 1.0:
            score += 15; strengths.append(f"👀 {_kl} bought {kol_sol:.1f} SOL")
        else:
            score += 8;  strengths.append(f"👀 KOL wallet entry detected")

    # ── CATEGORY 10: CHAIN-SPECIFIC AGE ADJUSTMENT ───────────────────────────
    _chain_low = info.get("chain", "").lower()
    if _chain_low in ("solana", "sol"):
        if age_h > 4.0:   score -= 8;  warnings.append(f"SOL token {round(age_h,1)}h old — very late entry")
        elif age_h > 3.0: score -= 6;  warnings.append(f"SOL token {round(age_h,1)}h old — late entry")
        elif age_h > 2.0: score -= 3;  warnings.append(f"SOL token {round(age_h,1)}h old — rug window open")
        elif age_h > 1.0: score -= 1
    elif _chain_low in ("ethereum", "eth"):
        if age_h < 0.5:   score -= 5;  warnings.append("ETH token under 30min — bots active")
        elif age_h <= 6:  score += 3
    elif _chain_low in ("bsc",):
        score -= 5;  warnings.append("BSC — higher bot/rug base rate")

    # ── HARD FLAG PENALTIES ───────────────────────────────────────────────────
    flag_count = len(flags)
    if flag_count >= 3:   score = max(0, score - 45)
    elif flag_count == 2: score = max(0, score - 25)
    elif flag_count == 1: score = max(0, score - 12)

    # ── CATEGORY 11: SOCIAL VELOCITY (0–15 pts) ──────────────────────────────
    # Uses pf_replies velocity (already fetched), plus injected social_velocity
    # signal from the mention tracker (populated by _social_mention_tracker below).
    # No external API key required — pump.fun reply_count is free.
    pf_replies      = info.get("pf_replies", 0)
    pf_reply_vel    = info.get("pf_reply_velocity", 0.0)   # replies/min, computed in sniper_job
    social_vel      = info.get("social_velocity", 0.0)     # 0-10 composite score from mention tracker
    tg_spike        = info.get("tg_mention_spike", False)  # True if TG mention count spiked 3x+
    if pf_reply_vel >= 10:
        score += 10; strengths.append(f"🔥 Viral reply velocity ({pf_reply_vel:.0f}/min on pump.fun)")
    elif pf_reply_vel >= 4:
        score += 6;  strengths.append(f"📢 High reply velocity ({pf_reply_vel:.0f}/min)")
    elif pf_reply_vel >= 1.5:
        score += 3
    if pf_replies >= 200:
        score += 5; strengths.append(f"💬 {pf_replies} pump.fun replies — strong community pull")
    elif pf_replies >= 80:
        score += 3
    if social_vel >= 7:
        score += 8; strengths.append(f"🌐 Social velocity {social_vel:.1f}/10 — cross-platform signal")
    elif social_vel >= 4:
        score += 4
    if tg_spike:
        score += 5; strengths.append("📱 Telegram mention spike detected")
    score = min(score + 0, 100)   # re-apply cap after social bonus

    score = min(score, 100)

    # ── Verdict ───────────────────────────────────────────────────────────────
    if flag_count >= 2:
        verdict = "SKIP"
    elif score >= 72:
        verdict = "SNIPE"
    elif score >= 54:
        verdict = "WAIT"
    else:
        verdict = "SKIP"

    return {
        "score":     score,
        "verdict":   verdict,
        "strengths": strengths[:5],
        "warnings":  warnings[:4],
        "flags":     flags,
        "icon":      "🟢" if score >= 72 else "🟡" if score >= 54 else "🔴",
    }





def score_token(info: dict) -> dict:
    """APEX token scoring for manual CA scans (distinct from sniper_score)."""
    score = 0
    strengths = []
    warnings = []

    liq = info.get("liq", 0)
    if liq >= 100_000:
        score += 15
        strengths.append("Strong liquidity (>$100K)")
    elif liq >= 50_000:
        score += 10
        strengths.append("Good liquidity (>$50K)")
    elif liq >= 20_000:
        score += 5
        warnings.append("Low liquidity (<$50K)")
    else:
        warnings.append("Very low liquidity - HIGH RISK")

    liq_pct = info.get("liq_pct", 0)
    if liq_pct >= 5:
        score += 8
        strengths.append("High liquidity ratio")
    elif liq_pct >= 2:
        score += 4
    else:
        warnings.append("Low liquidity ratio")

    age_h = info.get("age_h")
    if age_h is not None:
        if age_h < 1:
            warnings.append("Less than 1 hour old - EXTREME RISK")
        elif age_h < 24:
            score += 3
            warnings.append("New token (under 24h)")
        elif age_h < 168:
            score += 5
            strengths.append("Token age: " + str(round(age_h/24, 1)) + " days")
        else:
            score += 7
            strengths.append("Established token")

    if info.get("ch_m5", 0) > 0 and info.get("ch_h1", 0) > 0:
        score += 10
        strengths.append("Positive momentum (5m & 1h)")
    elif info.get("ch_m5", 0) > 0 or info.get("ch_h1", 0) > 0:
        score += 5
    else:
        warnings.append("Negative short-term momentum")

    buy_pct = info.get("buy_pct", 50)
    if buy_pct >= 65:
        score += 10
        strengths.append("Strong buy pressure (" + str(buy_pct) + "% buys)")
    elif buy_pct >= 55:
        score += 5
    else:
        warnings.append("Sell pressure (" + str(100 - buy_pct) + "% sells)")

    if info.get("vol_h24", 0) >= 500_000:
        score += 5
        strengths.append("Very high volume")
    elif info.get("vol_h24", 0) >= 100_000:
        score += 3

    mc = info.get("mc", 0)
    if 100_000 <= mc <= 10_000_000:
        score += 15
        strengths.append("Sweet spot MC")
    elif 10_000_000 < mc <= 100_000_000:
        score += 8
    elif mc < 100_000:
        score += 3
        warnings.append("Very low MC - ultra risky")
    else:
        score += 5
        warnings.append("High MC - less upside")

    total_tx = info.get("buys", 0) + info.get("sells", 0)
    if total_tx >= 1000:
        score += 10
        strengths.append("High transaction count")
    elif total_tx >= 500:
        score += 7
    elif total_tx >= 100:
        score += 3
    else:
        warnings.append("Low transaction count")

    if liq < 50_000:
        score = max(0, score - 10)
    if age_h is not None and age_h < 1:
        score = max(0, score - 10)
    if buy_pct < 40:
        score = max(0, score - 5)

    score = min(score, 100)

    if score >= 80:
        verdict = "STRONG BUY"
        icon = "[GREEN]"
    elif score >= 60:
        verdict = "GOOD TRADE"
        icon = "[YELLOW]"
    elif score >= 40:
        verdict = "RISKY - CAUTION"
        icon = "[ORANGE]"
    else:
        verdict = "AVOID"
        icon = "[RED]"

    return {
        "score":     score,
        "verdict":   verdict,
        "icon":      icon,
        "strengths": strengths[:3],
        "warnings":  warnings[:3],
    }


def get_user(uid: int, uname: str) -> dict:
    if uid not in users:
        users[uid] = {
            "username":         uname or "User" + str(uid),
            "balance":          None,
            "starting_balance": None,
            "savings":          0.0,
            "auto_save_pct":    None,
            "holdings":         {},
            "realized_pnl":     0.0,
            "total_fees_paid":  0.0,   # cumulative simulated fees (gas+DEX+slippage)
            "limit_orders":     [],
            "price_alerts":     [],
            "joined_at":        datetime.now(),
            "preset_buy":       None,
            "preset_sell":      None,
            "risk_pct":         None,
            "max_positions":    None,
            "daily_limit":      None,
            "daily_trades":     0,
            "last_day":         None,
            "planned":          0,
            "impulse":          0,
            "followed":         0,
            "broken":           0,
            "streak":           0,
            "best_streak":      0,
            "target_equity":    None,
            "peak_equity":      0.0,
            "max_drawdown":     0.0,
            "consec_losses":    0,
            "trade_hours":      {},
            "mood_tracking":    False,
            "mood_stats":       {},
            "daily_trade_counts": [],
            "avg_daily_trades": 0,
            "balance_limit":    10_000.0,
            "unlocked_rewards": [],
            "competitions":     {},
            "watchlist":        {},
            "price_alerts_mc":  {},
            "limit_orders_mc":  {},
            "challenge":        None,
            "referrer":         None,
            "referrals":        [],
            "channel_id":       None,
            "accounts":         {},
            "active_account":   "main",
            "whale_alerts":     True,
            "copy_trading":     None,
            "copy_paused":      False,
            # Risk Calculator
            "risk_calc":        False,
            # Token Sniper v2
            "sniper_auto":       False,   # Mode 1: fully automatic
            # ── APEX Mode ─────────────────────────────────────────────────────
            "apex_mode":              False,
            "apex_vault":             0.0,
            "apex_vault_profit_split": 0.50,  # fraction of profit sent to main balance on each APEX close
            "apex_vault_total_profit_sent": 0.0,  # lifetime $ sent from vault to main balance
            "apex_vault_peak":          0.0,  # highest vault balance ever reached

            "apex_session_start_bal": 0.0,
            "apex_daily_pnl":         0.0,
            "apex_daily_date":        None,
            "apex_consec_losses":     0,
            "apex_total_trades":      0,
            "apex_total_wins":        0,
            "apex_learn_threshold":   6,    # raised from 3 — prevents low-confidence learning trades
            "apex_learn_score_min":   45,   # raised from 30 — prevents low-score learning trades
            "apex_phase":             "learning",  # learning / calibrating / optimised
            "apex_size_mult":         1.0,          # position size multiplier — self-tuned
            "apex_max_positions_learned": 999,      # unlimited in learning
            "apex_vault_trade_on":    False,        # vault trades as separate balance
            "apex_vault_pnl":         0.0,          # lifetime vault P&L
            # ── Equity history: daily snapshot {date, equity, balance, pnl}
            # appended by daily_summary_job at 23:59. Used for balance curve display.
            "equity_history":         [],
            "sniper_advisory":   False,   # Mode 2: AI report, user confirms
            "sniper_auto_notify":    True,
            "sniper_adv_notify":     True,
            "sniper_auto_sl":        True,   # auto stop loss after snipe
            "sniper_auto_sl_pct":    40.0,
            "sniper_auto_tp":        True,   # auto take profit after snipe
            "sniper_auto_tp_x":      [2.0, 5.0],  # sell 50% at 2x, 50% at 5x
            "sniper_daily_budget":   500.0,
            "sniper_daily_spent":    0.0,
            "sniper_daily_date":     None,
            "sniper_chains": {
                "solana": True, "ethereum": True, "base": True,
                "bsc": True, "arbitrum": True,
            },
            "sniper_filters": {
                "min_score":        35,      # GemTools-style: low threshold, volume over quality
                "min_liq":          5_000,   # micro-caps have thin liq — $5K min
                "min_mc":           10_000,  # calls as low as $16K MC
                "max_mc":           100_000, # tightened: APEX focuses on $20K-$100K for 5-10x potential
                "max_age_h":        6.0,     # tightened — fresh tokens only, reduces noise
                "buy_amount":       20,      # lowered default for safer paper trading start
                "min_buys_h1":      10,      # micro-caps have lower buy counts
                "min_buy_pct":      45,      # relaxed from 50 — more signals, still bullish
                "max_vol_mc_ratio": 10.0,    # micro-caps spike hard
                "min_liq_pct":      3,       # very small MC tokens have low liq%
                "max_top10_pct":    28,      # GemTools max seen: 26.5%
                "min_lp_burn":      50,      # relaxed for micro-caps
            },
            "sniper_bought":    [],
            "sniper_seen":      {},   # {contract: timestamp} — persistent dedup memory
            "sniper_log":       [],   # history of every sniper decision
            "kol_wallets":      [],   # list of {address, label, chain} to track
            "kol_alerts_on":    True, # KOL buy alert notifications
            "sniper_broadcast_channel": None,   # channel/group ID for signal broadcasts
            "sniper_log_channel":       None,   # channel/group ID for dump scan log
            "sniper_log_channel_name":  "",     # display name for dump log channel
            "sniper_log_channel_on":    True,   # enable/disable dump log channel
            # Quick Buy
            "quick_buy_amount":  100.0,        # Feature: one-tap quick buy amount
            # Milestone notifications
            "milestone_notif":      True,       # Feature: holdings x milestone alerts
            "milestone_notif_dump": True,       # Feature: -50% dump alert
            # Rug Pull Warning
            "rug_warn_enabled":  False,         # Feature: liq drop early warning (OFF by default)
            "rug_warn_threshold": 30,           # Feature: % liq drop in one cycle to trigger
            # DCA by Market Cap
            "dca_orders":       [],
            # Language
            "language":         "en",
        }
        trade_log[uid] = []
        save_user(uid, users[uid])
    return users[uid]


async def fetch_ohlcv(pair_addr: str, chain_id: str) -> list:
    try:
        url = (
            f"https://api.geckoterminal.com/api/v2/networks/{chain_id}"
            f"/pools/{pair_addr}/ohlcv/minute?aggregate=5&limit=60"
        )
        client = await get_http()
        r = await client.get(url, headers={"Accept": "application/json"})
        if r.status_code == 200:
            return r.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    except Exception:
        pass
    return []


def generate_price_chart(info: dict, ohlcv: list):
    try:
        import matplotlib.patches as patches
        from datetime import datetime as dt

        bg_col    = "#0a0d18"
        green_col = "#00c86a"
        red_col   = "#e02626"
        grid_col  = "#1a2035"
        text_col  = "#8090b0"
        symbol = info.get("symbol", "TOKEN")
        price  = float(info.get("price", 0))
        ch_24  = float(info.get("ch_h24", 0))
        ch_col = green_col if ch_24 >= 0 else red_col
        ch_str = ("+" if ch_24 >= 0 else "") + str(round(ch_24, 1)) + "%"
        mc     = info.get("mc", 0)

        fig, ax = plt.subplots(figsize=(9, 4))
        fig.patch.set_facecolor(bg_col)
        ax.set_facecolor(bg_col)
        ax.spines[:].set_visible(False)
        ax.grid(axis="y", color=grid_col, linewidth=0.7, zorder=0)
        ax.tick_params(colors=text_col, labelsize=8)

        def _mc_str(v):
            if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
            if v >= 1_000:     return f"${v/1_000:.1f}K"
            return f"${v:.0f}"

        if ohlcv and len(ohlcv) >= 3:
            candles = list(reversed(ohlcv))[-48:]  # oldest left, newest right
            w = 0.6
            for i, c in enumerate(candles):
                o, h, l, cl = c[1], c[2], c[3], c[4]
                col = green_col if cl >= o else red_col
                ax.plot([i, i], [l, h], color=col, linewidth=0.9, zorder=2)
                rect = patches.Rectangle(
                    (i - w/2, min(o, cl)), w,
                    max(abs(cl - o), (h - l) * 0.015),
                    facecolor=col, edgecolor=col, linewidth=0, zorder=3
                )
                ax.add_patch(rect)
            tick_pos = list(range(0, len(candles), 12))
            tick_lbl = [dt.utcfromtimestamp(candles[i][0]).strftime("%H:%M") for i in tick_pos]
            ax.set_xticks(tick_pos)
            ax.set_xticklabels(tick_lbl, color=text_col, fontsize=8)
            ax.set_xlim(-1, len(candles))
            highs = [c[2] for c in candles]
            ath_i = int(np.argmax(highs))
            ax.annotate(f"ATH ${highs[ath_i]:.6g}", xy=(ath_i, highs[ath_i]),
                        xytext=(0, 8), textcoords="offset points",
                        color="#ffd700", fontsize=7, ha="center", fontweight="bold")
        else:
            ch_m5  = float(info.get("ch_m5",  0)) / 100
            ch_h1  = float(info.get("ch_h1",  0)) / 100
            ch_h6  = float(info.get("ch_h6",  0)) / 100
            ch_h24v = float(info.get("ch_h24", 0)) / 100
            p_24h  = price / (1 + ch_h24v) if ch_h24v != -1 else price
            p_6h   = price / (1 + ch_h6)  if ch_h6  != -1 else price
            p_1h   = price / (1 + ch_h1)  if ch_h1  != -1 else price
            p_5m   = price / (1 + ch_m5)  if ch_m5  != -1 else price
            times  = ["-24h", "-6h", "-1h", "-5m", "Now"]
            prices = [p_24h, p_6h, p_1h, p_5m, price]
            lc     = green_col if price >= p_24h else red_col
            xs     = np.arange(len(times))
            ax.plot(xs, prices, color=lc, linewidth=2.5, zorder=3)
            ax.fill_between(xs, prices, min(prices)*0.995, color=lc+"22", zorder=2)
            ax.scatter(xs, prices, color=lc, s=55, zorder=4)
            ax.set_xticks(xs)
            ax.set_xticklabels(times, color=text_col, fontsize=10)

        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:.6g}"))
        fig.text(0.03, 0.96, f"${symbol}  |  MC: {_mc_str(mc)}  |  5M Chart",
                 color="white", fontsize=12, fontweight="bold", va="top")
        fig.text(0.97, 0.96, f"24h: {ch_str}  Now: ${price:.6g}",
                 color=ch_col, fontsize=10, fontweight="bold", va="top", ha="right")
        plt.tight_layout(rect=[0, 0, 1, 0.92])
        buf = io.BytesIO()
        plt.savefig(buf, format="PNG", dpi=140, bbox_inches="tight",
                    facecolor=bg_col, edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.warning(f"Chart error: {e}")
        return None


def money(n: float) -> str:
    if abs(n) >= 1_000_000_000:
        v = round(n/1_000_000_000, 2)
        return "$" + (str(int(v)) if v == int(v) else str(v)) + "B"
    if abs(n) >= 1_000_000:
        v = round(n/1_000_000, 2)
        return "$" + (str(int(v)) if v == int(v) else str(v)) + "M"
    if abs(n) >= 1_000:
        v = round(n, 2)
        return "$" + ("{:,.0f}".format(v) if v == int(v) else "{:,.2f}".format(v))
    if abs(n) >= 1:
        v = round(n, 2)
        return "$" + ("{:.0f}".format(v) if v == int(v) else "{:.2f}".format(v))
    return "${:.8f}".format(n).rstrip("0").rstrip(".")


def mc_str(n: float) -> str:
    if n >= 1_000_000_000:
        return "$" + str(round(n/1_000_000_000, 2)) + "B"
    if n >= 1_000_000:
        return "$" + str(round(n/1_000_000, 2)) + "M"
    if n >= 1_000:
        return "$" + str(round(n/1_000)) + "K"
    return "$" + str(round(n))


def pstr(n: float) -> str:
    if n >= 0:
        return "+" + money(n)
    return "-" + money(abs(n))


def _safe_dt(val) -> datetime:
    """Convert closed_at to datetime safely — handles datetime objects and ISO strings."""
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val)
        except Exception:
            return datetime.min
    return datetime.min


def _md(text) -> str:
    """Escape underscores and asterisks in user-controlled strings for Telegram Markdown."""
    return str(text or "").replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")


def age_str(h: float) -> str:
    """Human-readable age from hours: 30m / 4.5h / 3.2d / 2.1mo / 1.4y"""
    if h < 1:
        return str(round(h * 60)) + "m"
    if h < 24:
        return str(round(h, 1)) + "h"
    days = h / 24
    if days < 30:
        return str(round(days, 1)) + "d"
    months = days / 30.44
    if months < 12:
        return str(round(months, 1)) + "mo"
    years = days / 365.25
    return str(round(years, 1)) + "y"


# ── POSITION HISTORY HELPERS ──────────────────────────────────────────────────
# These read from h["price_history"] (all positions) and h["sr_history"] (APEX).
# Both are populated by checker_job every cycle. sr_history is preferred for
# APEX positions because it carries volume data; price_history is the fallback
# for manual/sniper positions that don't have sr_history.

def _position_sparkline(h: dict) -> str:
    """
    Build a 10-char Unicode block sparkline from price history.

    Reads sr_history first (APEX positions, richer data), falls back to
    price_history (all positions). Returns "" if fewer than 3 snapshots exist.

    Output example:  ▁▂▃▅▇█▇▅▃▂   (price path left→right, oldest→newest)
    """
    BLOCKS = "▁▂▃▄▅▆▇█"
    # Prefer sr_history (APEX) — same price field name
    history = h.get("sr_history") or h.get("price_history") or []
    if len(history) < 3:
        return ""
    # Sample up to 10 evenly-spaced points
    n       = len(history)
    step    = max(1, n // 10)
    sampled = [history[i]["price"] for i in range(0, n, step)][-10:]
    lo, hi  = min(sampled), max(sampled)
    if hi == lo:
        return "▄" * len(sampled)   # flat line
    return "".join(
        BLOCKS[min(7, int((p - lo) / (hi - lo) * 7))]
        for p in sampled
    )


def _position_history_line(h: dict, current_price: float) -> str:
    """
    Build the single history line shown under each position in the positions list.

    Sparkline chars (▁▂▃▄▅▆▇█) are NOT used here — they render as blank boxes
    on many Android fonts when outside a monospace code block. The full sparkline
    is available on the token card 📜 History toggle inside a code block.

    Format:  Peak 3.4x  Low 0.82x
    """
    avg = h.get("avg_price", 0)
    if avg <= 0:
        return ""
    peak_p = h.get("peak_price", current_price)
    peak_x = round(peak_p / avg, 2) if avg > 0 else 0
    history = h.get("sr_history") or h.get("price_history") or []
    if history:
        low_p = min(snap["price"] for snap in history)
        low_x = round(low_p / avg, 2) if avg > 0 else 0
        low_txt = "  Low " + str(low_x) + "x" if low_x < 0.99 else ""
    else:
        low_txt = ""
    if peak_x < 1.01 and not low_txt:
        return ""
    return "  Peak " + str(peak_x) + "x" + low_txt


def chain_icon(c: str) -> str:
    icons = {
        "solana": "SOL", "ethereum": "ETH", "bsc": "BNB",
        "base": "BASE", "arbitrum": "ARB", "polygon": "MATIC",
        "avalanche": "AVAX", "sui": "SUI"
    }
    return icons.get(c.lower(), c.upper())


def check_daily(d: dict) -> bool:
    today = datetime.now().date()
    if d["last_day"] != today:
        d["daily_trades"] = 0
        d["last_day"] = today
    lim = d.get("daily_limit")
    return not (lim and d["daily_trades"] >= lim)


def sell_core(ud: dict, uid: int, contract: str, usd: float, price: float, reason: str = "manual") -> dict:
    h = ud["holdings"][contract]
    tokens = usd / price
    ratio = min(tokens / h["amount"], 1.0) if h["amount"] > 0 else 1.0
    cost = h["total_invested"] * ratio

    # ── Simulated trading costs on sell (gas + DEX fee + slippage) ───────────
    _sell_fee = 0.0
    if SIM_FEES_ENABLED:
        _sell_fee = round(usd * SIM_TOTAL_PCT + SIM_GAS_USD, 4)
        usd = round(usd - _sell_fee, 4)   # net proceeds after fees
        ud["total_fees_paid"] = round(ud.get("total_fees_paid", 0.0) + _sell_fee, 4)
        h.setdefault("total_fees_paid", 0.0)
        h["total_fees_paid"] = round(h.get("total_fees_paid", 0.0) + _sell_fee, 4)

    realized = usd - cost
    ud["realized_pnl"] += realized
    # ── APEX vault trade: proceeds → vault, profit split → main balance ─────
    _apex_mood = h.get("mood", "") in ("APEX", "AI-Sniper", "APEX-DCA")
    _vault_funded = _apex_mood  # all APEX trades now route through vault
    if _apex_mood:
        # All proceeds return to vault first
        ud["apex_vault"] = round(ud.get("apex_vault", 0.0) + usd, 4)
        ud["apex_vault_pnl"] = round(ud.get("apex_vault_pnl", 0.0) + realized, 4)
        # ── 50% profit split to main balance ─────────────────────────────
        if realized > 0:
            _split_pct    = ud.get("apex_vault_profit_split", 0.50)
            _split_amt    = round(realized * _split_pct, 4)
            # Transfer split from vault to main balance
            ud["apex_vault"] = round(ud["apex_vault"] - _split_amt, 4)
            ud["balance"]    = round(ud.get("balance", 0.0) + _split_amt, 4)
            ud["apex_vault_total_profit_sent"] = round(
                ud.get("apex_vault_total_profit_sent", 0.0) + _split_amt, 4
            )
        # Track vault peak
        if ud["apex_vault"] > ud.get("apex_vault_peak", 0.0):
            ud["apex_vault_peak"] = ud["apex_vault"]
    else:
        ud["balance"] = round(ud.get("balance", 0.0) + usd, 4)
    h["amount"] -= tokens
    h["total_invested"] = max(0, h["total_invested"] - cost)
    h["total_sold"]     = h.get("total_sold", 0.0) + usd
    hold_h = (datetime.now() - h.get("bought_at", datetime.now())).total_seconds() / 3600
    auto_saved = 0.0

    # Auto-save only applies to manual trades — APEX vault trades already route
    # profit to main balance via the split mechanism; applying auto-save on top
    # would deduct from main balance funds unrelated to this trade's profit.
    if realized > 0 and ud.get("auto_save_pct") and not _apex_mood:
        save_amt = realized * ud["auto_save_pct"] / 100
        if save_amt > 0 and ud["balance"] >= save_amt:
            ud["balance"] -= save_amt
            ud["savings"] += save_amt
            auto_saved = save_amt

    hour = str(datetime.now().hour)
    if hour not in ud["trade_hours"]:
        ud["trade_hours"][hour] = {"wins": 0, "losses": 0, "pnl": 0.0}
    ud["trade_hours"][hour]["pnl"] += realized
    is_win = realized > 0
    if is_win:
        ud["trade_hours"][hour]["wins"] += 1
        ud["consec_losses"] = 0
    else:
        ud["trade_hours"][hour]["losses"] += 1
        ud["consec_losses"] = ud.get("consec_losses", 0) + 1
    # Track mood performance (shared logic for win & loss)
    mood = h.get("mood", "")
    if mood:
        ms = ud.setdefault("mood_stats", {})
        if mood not in ms:
            ms[mood] = {"trades": 0, "wins": 0, "pnl": 0.0}
        ms[mood]["trades"] += 1
        if is_win:
            ms[mood]["wins"] += 1
        ms[mood]["pnl"] += realized

    closed = False
    if h["amount"] < 0.000001:
        total_invested_full = h["total_invested"] + cost  # includes cost of this last sell
        total_returned_full = h.get("total_sold", 0.0)    # all proceeds including this sell
        # Blended x = total money out / total money in
        x_val = round(total_returned_full / total_invested_full, 4) if total_invested_full > 0 else 0
        trade_log.setdefault(uid, []).append({
            "symbol":        h["symbol"],
            "contract":      contract,
            "chain":         h.get("chain", "unknown"),
            "invested":      total_invested_full,
            "returned":      total_returned_full,
            "realized_pnl":  realized,
            "x":             x_val,
            "hold_h":        round(hold_h, 1),
            "reason":        reason,
            "closed_at":     datetime.now(),
            "bought_at":     h.get("bought_at", datetime.now()),
            "avg_price":     h.get("avg_price", 0),
            "exit_price":    price,
            "peak_price":    h.get("peak_price", price),
            "journal":       h.get("journal", ""),
            "mood":          h.get("mood", ""),
            "planned":       h.get("planned", True),
            "followed_plan": h.get("followed_plan", None),
            "auto_saved":    auto_saved,
            "fees_paid":     round(h.get("total_fees_paid", 0.0), 4),
        })
        del ud["holdings"][contract]
        closed = True

    # ── APEX vault reconciliation on full close ───────────────────────────────
    # Vault credit is now handled in the vault block above (sell_core).
    # The 2x/5x milestone reservation system has been replaced by the
    # per-trade 50% profit split directly in sell_core.
    # Nothing additional needed here.
    if closed:
        pass  # vault already credited above
    save_user(uid, ud)
    if closed:
        save_trade_log(uid, trade_log.get(uid, []))
    return {
        "received":   usd,
        "realized":   realized,
        "closed":     closed,
        "hold_h":     round(hold_h, 1),
        "auto_saved": auto_saved,
    }


async def portfolio_val(ud: dict) -> tuple:
    holdings = ud["holdings"]
    if not holdings:
        return 0.0, 0.0
    contracts = list(holdings.keys())
    infos = await _asyncio.gather(*[get_token(c) for c in contracts])
    tv, tc = 0.0, 0.0
    for c, info in zip(contracts, infos):
        if info:
            tv += holdings[c]["amount"] * info["price"]
            tc += holdings[c]["total_invested"]
    return tv, tv - tc


_CHAIN_ICONS: dict = {
    "SOLANA": "◎", "ETHEREUM": "Ξ", "BSC": "⬡",
    "BASE": "🔵", "ARBITRUM": "🔷", "POLYGON": "⬟",
    "SUI": "💧", "AVALANCHE": "🔺",
}
_DEX_CHAIN_MAP: dict = {
    "solana":"solana","sol":"solana","ethereum":"ethereum","eth":"ethereum",
    "bsc":"bsc","bnb":"bsc","base":"base","arbitrum":"arbitrum",
}
_GMGN_CHAIN_MAP: dict = {
    "solana":"sol","sol":"sol","ethereum":"eth","eth":"eth",
    "bsc":"bsc","bnb":"bsc","base":"base",
}

def _card_stats_block(info: dict, contract: str, first_scan_line: str = "", show_mtv: bool = False) -> str:
    """
    Shared card body used by both token_card() and group_token_card().
    Returns the stats + security + scanner + socials + links block.
    show_mtv=True inserts M·T·V Intelligence between the stats block and the security block.
    """
    def fc(v):
        v = float(v or 0)
        if v >= 100: e = "🚀"
        elif v >= 20: e = "📈"
        elif v >= 0:  e = "🟢"
        elif v >= -20: e = "🔴"
        else:          e = "💀"
        return e + " " + ("+" if v >= 0 else "") + str(round(v, 1)) + "%"

    def _c(v: float) -> str:
        if abs(v) >= 1_000_000: return "$" + str(round(v / 1_000_000, 1)) + "M"
        if abs(v) >= 1_000:     return "$" + str(round(v / 1_000, 1)) + "K"
        return "$" + str(round(v, 1))

    def _safe(s):
        return _re.sub(r'[_*\[\]()~`>#+\-=|{}.!]', '', str(s))

    def _bp_icon(pct: float) -> str:
        return "🟢" if pct >= 60 else ("🟡" if pct >= 50 else "🔴")

    price    = info.get("price", 0)
    mc       = info.get("mc", 0)
    liq      = info.get("liq", 0)
    liq_pct  = info.get("liq_pct", 0)
    age_h    = info.get("age_h") or 0
    buy_pct  = info.get("buy_pct", 50)
    sell_pct = 100 - buy_pct
    buys     = info.get("buys", 0)
    sells    = info.get("sells", 0)
    vol_h24  = info.get("vol_h24", 0)   # ← total 24h volume (was vol_h1)
    ath_price= info.get("ath_price", 0)
    ath_mc   = info.get("ath_mc", 0)
    ath_ts   = info.get("ath_ts", 0.0)
    chain_raw= str(info.get("chain", "solana")).lower()

    # ── Age display — full scale: m / h / d / mo / y ──────────────────────────
    if age_h < 1:
        age_display = str(round(age_h * 60)) + "m"
    elif age_h < 24:
        age_display = str(round(age_h, 1)) + "h"
    elif age_h < 24 * 30:
        age_display = str(round(age_h / 24, 1)) + "d"
    elif age_h < 24 * 365:
        age_display = str(round(age_h / 24 / 30.44, 1)) + "mo"
    else:
        age_display = str(round(age_h / 24 / 365.25, 1)) + "y"

    # ── ATH with time-since ───────────────────────────────────────────────────
    import time as _csbt
    if ath_price and ath_price > price and price > 0:
        ath_down = round((1 - price / ath_price) * 100, 1)
        ath_str  = mc_str(ath_mc) + " (-" + str(ath_down) + "%)"
        # Append time-since-ATH when we have a valid timestamp
        if ath_ts and ath_ts > 0:
            _ath_elapsed = _csbt.time() - ath_ts
            if _ath_elapsed < 3600:
                _ath_age = str(int(_ath_elapsed / 60)) + "m"
            elif _ath_elapsed < 86400:
                _ath_age = str(round(_ath_elapsed / 3600, 1)) + "h"
            elif _ath_elapsed < 86400 * 30:
                _ath_age = str(round(_ath_elapsed / 86400, 1)) + "d"
            else:
                _ath_age = str(round(_ath_elapsed / 86400 / 30.44, 1)) + "mo"
            ath_str += " / " + _ath_age
    else:
        ath_str = "N/A"

    # ── M·T·V Intelligence block (optional — group card only) ─────────────────
    mtv_block = ""
    if show_mtv:
        mtv_block = _build_mtv_markdown(info)

    # ── Security ──────────────────────────────────────────────────────────────
    no_mint   = info.get("no_mint")
    no_freeze = info.get("no_freeze")
    lp_burn   = info.get("lp_burn")
    top10     = info.get("top10_pct")
    insider   = info.get("insider_pct")
    rug_risks = info.get("rug_risks", []) or []
    _wi_fresh_p  = info.get("fresh_wallet_pct") or 0.0
    _maker_count = info.get("maker_count") or 0
    _wi_smart_cnt= info.get("smart_wallet_count") or 0
    _wi_sniper_cnt = info.get("sniper_count") or 0
    _wi_sniper_p   = info.get("sniper_pct") or 0.0
    _wi_bundle_p   = info.get("bundle_wallet_pct") or 0.0
    _boost_amount  = info.get("boost_amount") or 0
    _dev_sold      = any("deployer sold" in r.lower() or "creator sold" in r.lower() for r in rug_risks)
    _rc_avail      = no_mint is not None

    # Dev Sold / Dev Paid status dots
    _dev_sold_dot = "🔴" if _dev_sold else "🟢"
    _dev_paid_dot = "🟢" if _boost_amount >= 5 else "🔴"

    sec_block = ""
    if info.get("rc_rate_limited") and not any(x is not None for x in [no_mint, no_freeze, lp_burn, top10, insider]):
        sec_block = (
            "─────────────────────────\n"
            "🔒 ⚠️ Security audit loading — tap 🔄 Refresh\n"
        )
    elif any(x is not None for x in [no_mint, no_freeze, lp_burn, top10, insider]):
        audit_parts = []
        if no_mint   is not None: audit_parts.append("✅ No Mint"   if no_mint   else "🚨 Mint")
        if no_freeze is not None: audit_parts.append("✅ No Freeze" if no_freeze else "🚨 Freeze")
        if lp_burn   is not None: audit_parts.append("🔥 LP " + str(lp_burn) + "%")

        # Top10 — always clickable via RugCheck holders page
        top10_str = ""
        if top10 is not None:
            _rc_url = "https://rugcheck.xyz/tokens/" + contract
            top10_str = "[Top10: " + str(top10) + "%](" + _rc_url + ")"

        # Fresh / Unique wallet line
        _wallet_parts = []
        if _wi_fresh_p > 0:
            _wallet_parts.append("Fresh " + str(round(_wi_fresh_p)) + "%")
        if _maker_count > 0:
            _unique_str = (str(_maker_count // 1000) + "K" if _maker_count >= 1000 else str(_maker_count))
            _wallet_parts.append("Unique " + _unique_str)
        if _wi_sniper_cnt > 0:
            _snp_icon = "🎯" if _wi_sniper_p < 10 else ("🟡" if _wi_sniper_p < 25 else "🔴")
            _wallet_parts.append(_snp_icon + " Snipers: " + str(_wi_sniper_cnt))
        if _wi_smart_cnt > 0:
            _wallet_parts.append("💰 Smart: " + str(_wi_smart_cnt))
        if _wi_bundle_p >= 5:
            _bdl_icon = "🟡" if _wi_bundle_p < 15 else "🔴"
            _wallet_parts.append(_bdl_icon + " Bundle: " + str(round(_wi_bundle_p)) + "%")

        # Filter out low-signal noise flags — they consume space without adding value
        _NOISE_FLAGS = ("low amount of lp", "low liquidity", "low lp")
        _risk_flags = [
            r for r in rug_risks
            if isinstance(r, str) and not any(n in r.lower() for n in _NOISE_FLAGS)
        ][:2]

        sec_block = (
            "─────────────────────────\n"
            "├─ " + "  ".join(audit_parts) + "\n"
            + ("├─ " + top10_str + "\n" if top10_str else "")
            + "├─ Dev Sold " + _dev_sold_dot + "  Dev Paid " + _dev_paid_dot + "\n"
            + ("└─ " + " — ".join(_wallet_parts) + "\n" if _wallet_parts else "")
            + ("└─ ⚠️ " + "  |  ".join(_risk_flags) + "\n" if _risk_flags and not _wallet_parts else "")
        )

    # ── Socials ───────────────────────────────────────────────────────────────
    twitter      = info.get("twitter", "")
    telegram_url = info.get("telegram", "")
    website      = info.get("website", "")
    soc_parts = []
    if twitter:      soc_parts.append("🐦 [Twitter / X]("  + twitter      + ")")
    if telegram_url: soc_parts.append("💬 [Telegram]("     + telegram_url + ")")
    if website:      soc_parts.append("🌐 [Website]("      + website      + ")")
    social_line = ("─────────────────────────\n" + "  ·  ".join(soc_parts) + "\n") if soc_parts else ""

    # ── X search ─────────────────────────────────────────────────────────────
    _xbase  = "https://x.com/search?q={}&src=typed_query"
    _sym_raw  = str(info.get("symbol", ""))
    _name_raw = str(info.get("name", ""))
    _safe_sym = _re.sub(r'[_*\[\]()~`>#+\-=|{}.!]', '', _sym_raw).strip()[:8] or "SYM"
    _safe_nm  = _re.sub(r'[_*\[\]()~`>#+\-=|{}.!]', '', _name_raw).strip()[:10] or "Token"
    _combined = _urlparse.quote("$" + _sym_raw + " OR " + _name_raw, safe="")
    _sym_q    = _urlparse.quote("$" + _sym_raw, safe="")
    _ca_q     = _urlparse.quote(contract, safe="")
    _name_q   = _urlparse.quote(_name_raw, safe="")
    x_line = (
        "🔍 Search 𝕏:  "
        + "[All]("   + _xbase.format(_combined) + ")  "
        + "[CA]("    + _xbase.format(_ca_q)     + ")  "
        + "[" + _safe_nm  + "](" + _xbase.format(_name_q) + ")  "
        + "[$" + _safe_sym + "](" + _xbase.format(_sym_q) + ")\n"
    )

    # ── DEX links ─────────────────────────────────────────────────────────────
    dex_chain = _DEX_CHAIN_MAP.get(chain_raw, "solana")
    gt_url = "https://www.geckoterminal.com/" + dex_chain + "/pools/" + contract
    dt_url = "https://www.dextools.io/app/en/" + dex_chain + "/pair-explorer/" + contract
    ds_url = "https://dexscreener.com/"        + dex_chain + "/" + contract
    dv_url = "https://www.dexview.com/"        + dex_chain + "/" + contract
    be_url = "https://birdeye.so/token/"       + contract  + "?chain=" + dex_chain
    pf_url = "https://pump.fun/"               + contract
    if chain_raw in ("solana", "sol"):
        ext_line = (
            "─────────────────────────\n"
            "[GT](" + gt_url + ")  [DT](" + dt_url + ")  [DS](" + ds_url + ")  "
            "[DV](" + dv_url + ")  [BE](" + be_url + ")  [PF](" + pf_url + ")\n"
        )
    else:
        ext_line = (
            "─────────────────────────\n"
            "[GT](" + gt_url + ")  [DT](" + dt_url + ")  [DS](" + ds_url + ")  "
            "[DV](" + dv_url + ")  [BE](" + be_url + ")\n"
        )

    return (
        "─────────────────────────\n"
        "├─ 💲 *" + format_token_price(price) + "*\n"
        "├─ 📊 MC *" + mc_str(mc) + "*  Vol *" + _c(vol_h24) + "* (24h)\n"
        "├─ 💧 Liq *" + _c(liq) + "*  (" + str(round(liq_pct, 1)) + "%)\n"
        "├─ ⏰ Age *" + age_display + "*  🟢 " + str(buys) + " (" + str(buy_pct) + "%)\n"
        "└─ 🏆 ATH *" + ath_str + "*  🔴 " + str(sells) + " (" + str(sell_pct) + "%)\n"
        + mtv_block
        + sec_block
        + (first_scan_line if first_scan_line else "")
        + social_line
        + "\n"
        + x_line
        + "\n"
        + ext_line
    )


def token_card(info: dict, contract: str, ud: dict, sc: dict = None) -> str:
    def _safe(s):
        return _re.sub(r'[_*\[\]()~`>#+\-=|{}.!]', '', str(s))

    def _compact(v: float) -> str:
        """Compact number: $218K not $218,146.20"""
        if abs(v) >= 1_000_000: return "$" + str(round(v / 1_000_000, 1)) + "M"
        if abs(v) >= 1_000:     return "$" + str(round(v / 1_000, 1)) + "K"
        return "$" + str(round(v, 0))[:-2] if str(round(v, 0)).endswith(".0") else "$" + str(round(v, 1))

    try:
        name      = str(info.get("name", "Unknown"))
        symbol    = str(info.get("symbol", "???"))
        chain     = str(info.get("chain", "SOL")).upper()
        dex       = str(info.get("dex", "")).upper()
        chain_raw = str(info.get("chain", "solana")).lower()
        chain_sym = _CHAIN_ICONS.get(chain, "⛓")
        dex_clean = dex.replace("_V2","").replace("_V3","").replace("_","")
        price     = info.get("price", 0)
        mc        = info.get("mc", 0)

        rug_risks   = info.get("rug_risks", []) or []
        _is_copycat = any("copycat" in r.lower() for r in rug_risks)
        _origin_badge = "🔴 COPY" if _is_copycat else "🟢 OG"

        # ── YOUR POSITION ─────────────────────────────────────────────────────
        pos_block = ""
        if contract in ud.get("holdings", {}):
            h     = ud["holdings"][contract]
            cv    = h["amount"] * price
            cx    = price / h["avg_price"] if h.get("avg_price", 0) > 0 else 0
            ppnl  = cv - h["total_invested"]
            pnl_e = "💚" if ppnl >= 0 else "🔴"
            cx_e  = "🚀" if cx >= 3 else "📈" if cx >= 1.5 else "📉" if cx < 1 else "➡️"
            avg_mc = h.get("avg_cost_mc", 0)
            sold   = h.get("total_sold", 0)
            pos_block = (
                "─────────────────────────\n"
                "> 💰 *YOUR POSITION*\n"
                ">\n"
                "> 💵 Value      " + money(cv) + "   " + cx_e + " *" + str(round(cx, 2)) + "x*\n"
                "> " + pnl_e + " PnL        " + pstr(ppnl) + "\n"
                "> 🧾 Invested   " + money(h.get("total_invested", 0)) + "\n"
                + ("> 💸 Sold      " + money(sold) + "\n" if sold > 0 else "")
                + "> 🪙 Holding   " + str(round(h["amount"], 4)) + "\n"
                + ("> 📍 Avg MC    " + mc_str(avg_mc) + "\n" if avg_mc else "")
                + ">\n"
            )

        # ── First scanner line ────────────────────────────────────────────────
        first_scan_line = ""
        _fs = _first_scanner.get(contract)
        if _fs:
            import time as _fst
            _fs_uname = _fs.get("username", "?")
            _fs_mc    = _fs.get("mc", 0)
            _fs_ts    = _fs.get("scanned_at", 0)
            _elapsed  = _fst.time() - _fs_ts
            _fs_age   = (str(int(_elapsed/60)) + "m") if _elapsed < 3600 else (str(round(_elapsed/3600,1)) + "h") if _elapsed < 86400 else (str(round(_elapsed/86400,1)) + "d")
            if _fs_mc > 0 and mc > 0:
                _fs_ratio = mc / _fs_mc
                if _fs_ratio >= 2.0:
                    _fs_gain = str(round(_fs_ratio, 1)) + "x"
                    _fs_icon = "🚀"
                elif _fs_ratio >= 1.0:
                    _fs_gain = "+" + str(round((_fs_ratio - 1) * 100, 1)) + "%"
                    _fs_icon = "📈"
                else:
                    _fs_gain = str(round((_fs_ratio - 1) * 100, 1)) + "%"
                    _fs_icon = "📉"
            else:
                _fs_gain, _fs_icon = "+0%", "➡️"
            first_scan_line = (
                "─────────────────────────\n"
                "👤 [@" + _fs_uname + "](https://t.me/" + _fs_uname + ") @" + mc_str(_fs_mc)
                + " → " + _fs_icon + " [" + _fs_gain + "]  (" + _fs_age + " ago)\n"
            )

        stats = _card_stats_block(info, contract, first_scan_line)

        card = (
            "🪙 *" + name + "* ($" + symbol + ")  " + _origin_badge + "\n"
            + chain_sym + " " + chain + "  🏦 " + dex_clean + "\n\n"
            + "📋 `" + contract + "`\n"
            + pos_block
            + stats
        )

        if len(card) > 4096:
            card = card[:4092] + "…"
        return card

    except Exception as e:
        logger.warning(f"token_card render error: {e}")
        return (
            "🪙 *" + str(info.get("name", "Token")) + "* ($" + str(info.get("symbol", "???")) + ")\n"
            + "`" + contract + "`\n\n"
            + "Price: " + format_token_price(info.get("price", 0)) + "\n"
            + "MC: " + mc_str(info.get("mc", 0))
        )


async def send_token_card(
    target,           # update.message  OR  callback query (q)
    info: dict,
    contract: str,
    ud: dict,
    sc: dict,
    ctx,              # ContextTypes.DEFAULT_TYPE
    is_query: bool = False,
):
    """
    Refresh: edit the existing message in-place so the card doesn't flash.
    Falls back to delete+send only if the message is a photo (can't be edited).
    First open (from CA paste): sends banner image (if available) then text card.
    """
    card_txt = token_card(info, contract, ud, sc)
    kb       = buy_kb(contract, ud)

    # Determine chat_id
    if is_query:
        chat_id = target.message.chat_id
    else:
        chat_id = target.chat_id

    if is_query:
        # ── Try editing in-place first (no flash, no scroll jump) ────────────
        # Message is text with link preview (image rendered by Telegram from URL).
        # Re-prepend header URL so image stays on refresh.
        _header_url = info.get("header_image", "") if info else ""
        _header_ok  = bool(_header_url and (
            (info.get("_header_confirmed") if info else False)
            or not _header_url.startswith("https://dd.dexscreener.com/ds-data/")
        ))
        _hidden_link = "[\u200b](" + _header_url + ")\n" if _header_ok else ""
        _edit_txt    = _hidden_link + card_txt
        if target.message.photo or target.message.document:
            # Actual photo message (legacy) — delete and resend as text+preview
            try:
                await target.message.delete()
            except Exception:
                pass
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=_edit_txt,
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=False,
            )
        else:
            # Text message (normal case) — edit in-place
            try:
                await target.message.edit_text(
                    _edit_txt,
                    parse_mode="Markdown",
                    reply_markup=kb,
                    disable_web_page_preview=False,
                )
            except Exception:
                pass
    else:
        # ── First open from CA paste ──────────────────────────────────────────
        # Image as link preview: prepend the banner URL to the message text.
        # Telegram renders the first URL in a message as a large image preview
        # above the text — one single message, no 1024-char caption limit.
        header_url = info.get("header_image", "")
        _use_header = bool(header_url and (
            info.get("_header_confirmed")
            or not header_url.startswith("https://dd.dexscreener.com/ds-data/")
        ))
        if _use_header:
            # Zero-width space link: invisible in chat but Telegram renders image preview
            hidden_link = "[\u200b](" + header_url + ")\n"
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=hidden_link + card_txt,
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=False,
            )
        else:
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=card_txt,
                parse_mode="Markdown",
                reply_markup=kb,
            )




def group_token_card(info: dict, contract: str) -> str:
    """
    Public-only token card for group chats.
    No position block. Includes M·T·V intelligence and ticker scan count.
    No DexScreener preview image link (removed — can interfere with group display).
    """
    name      = str(info.get("name", "Unknown"))
    symbol    = str(info.get("symbol", "???"))
    chain     = str(info.get("chain", "SOL")).upper()
    dex       = str(info.get("dex", "")).upper().replace("_V2","").replace("_V3","").replace("_","")
    chain_sym = _CHAIN_ICONS.get(chain, "⛓")
    rug_risks   = info.get("rug_risks", []) or []
    _is_copycat = any("copycat" in r.lower() for r in rug_risks)

    # ── Ticker scan count — how many times this CA has been called ────────────
    _scan_count = _contract_scan_count.get(contract, 0)
    _count_tag  = " (" + str(_scan_count) + ")" if _scan_count > 1 else ""
    _badge = ("🔴 COPY" if _is_copycat else "🟢 OG") + _count_tag

    # First scanner line (public — shows who first called it)
    first_scan_line = ""
    _fs = _first_scanner.get(contract)
    if _fs:
        import time as _gfst
        _fs_uname = _fs.get("username", "?")
        _fs_mc    = _fs.get("mc", 0)
        _fs_ts    = _fs.get("scanned_at", 0)
        _elapsed  = _gfst.time() - _fs_ts
        if _elapsed < 3600:
            _fs_age = str(int(_elapsed / 60)) + "m"
        elif _elapsed < 86400:
            _fs_age = str(round(_elapsed / 3600, 1)) + "h"
        elif _elapsed < 86400 * 30:
            _fs_age = str(round(_elapsed / 86400, 1)) + "d"
        else:
            _fs_age = str(round(_elapsed / 86400 / 30.44, 1)) + "mo"
        mc = info.get("mc", 0)
        if _fs_mc > 0 and mc > 0:
            _fs_ratio = mc / _fs_mc
            if _fs_ratio >= 2.0:
                _fs_gain, _fs_icon = str(round(_fs_ratio, 1)) + "x", "🚀"
            elif _fs_ratio >= 1.0:
                _fs_gain, _fs_icon = "+" + str(round((_fs_ratio - 1) * 100, 1)) + "%", "📈"
            else:
                _fs_gain, _fs_icon = str(round((_fs_ratio - 1) * 100, 1)) + "%", "📉"
        else:
            _fs_gain, _fs_icon = "+0%", "➡️"
        first_scan_line = (
            "─────────────────────────\n"
            "👤 [@" + _fs_uname + "](https://t.me/" + _fs_uname + ") @" + mc_str(_fs_mc)
            + " → " + _fs_icon + " [" + _fs_gain + "]  (" + _fs_age + " ago)\n"
        )

    # show_mtv=True → inserts M·T·V Intelligence block in the group card
    stats = _card_stats_block(info, contract, first_scan_line, show_mtv=True)

    card = (
        "🪙 *" + name + "* ($" + symbol + ")  " + _badge + "\n"
        + chain_sym + " " + chain + "  🏦 " + dex + "\n\n"
        + "📋 `" + contract + "`\n"
        + stats
    )
    if len(card) > 4096:
        card = card[:4092] + "…"
    return card

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Positions",      callback_data="v_pos"),
         InlineKeyboardButton("⏰ Orders",         callback_data="v_orders")],
        [InlineKeyboardButton("👛 Wallet",         callback_data="v_wallet"),
         InlineKeyboardButton("👁 Watchlist",      callback_data="v_watchlist")],
        [InlineKeyboardButton("👥 Accounts",       callback_data="v_accounts"),
         InlineKeyboardButton("⚙️ Settings",       callback_data="v_settings")],
        [InlineKeyboardButton("📋 More ▸",         callback_data="v_more"),
         InlineKeyboardButton("🎯 Sniper",         callback_data="v_sniper")],
        [InlineKeyboardButton("⚡ BUY & SELL NOW!", callback_data="v_trade")],
    ])


def more_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",          callback_data="v_stats"),
         InlineKeyboardButton("📜 History",        callback_data="v_history")],
        [InlineKeyboardButton("🏆 Leaderboard",    callback_data="v_leader"),
         InlineKeyboardButton("🏁 Compete",        callback_data="v_compete")],
        [InlineKeyboardButton("🎯 Challenge",      callback_data="v_challenge"),
         InlineKeyboardButton("🔁 Copy Trading",   callback_data="v_copy")],
        [InlineKeyboardButton("🔔 Alerts",         callback_data="v_alerts"),
         InlineKeyboardButton("🐋 Whales",         callback_data="v_whale")],
        [InlineKeyboardButton("🚀 Milestones",     callback_data="v_milestone_notif"),
         InlineKeyboardButton("🔥 Rug Warning",    callback_data="v_rug_warn")],
        [InlineKeyboardButton("🔗 Referrals",      callback_data="v_referrals"),
         InlineKeyboardButton("👤 Profile",        callback_data="v_profile")],
        [InlineKeyboardButton("📁 Export CSV",     callback_data="v_export"),
         InlineKeyboardButton("📖 Help & Docs",    callback_data="v_help")],
        [InlineKeyboardButton("🏠 Main Menu",      callback_data="mm")],
    ])


def buy_kb(contract: str, ud: dict) -> InlineKeyboardMarkup:
    """Main token card keyboard — matches the approved sketch layout."""
    # Guard: migrate watchlist from list to dict if user has old session
    if isinstance(ud.get("watchlist"), list):
        ud["watchlist"] = {}
    held      = contract in ud["holdings"]
    h         = ud["holdings"].get(contract, {})
    has_as    = bool(h.get("auto_sells"))
    has_sl    = bool(h.get("stop_loss_pct"))
    as_lbl    = "🎯 Auto Sell ✅" if has_as else "🎯 Auto Sell"
    sl_lbl    = "🛑 Stop Loss ✅" if has_sl else "🛑 Stop Loss"
    track_lbl = "👁 Track ✅"     if contract in ud.get("watchlist", {}) else "👁 Track"

    # Alert — show Cancel if active for this token
    has_alert = any(a.get("contract") == contract for a in ud.get("price_alerts", []))
    alert_lbl = "🔔 Alert ✅"  if has_alert else "🔔 Set Alert"
    alert_cb  = "al_cancel_ca_" + contract if has_alert else "pal_" + contract

    # External link row — chain-aware
    chain_raw  = ud.get("last_chain", "solana").lower()
    dex_chain  = {"solana":"solana","sol":"solana","ethereum":"ethereum","eth":"ethereum",
                  "bsc":"bsc","bnb":"bsc","base":"base","arbitrum":"arbitrum","arb":"arbitrum"}.get(chain_raw,"solana")
    gmgn_chain = {"solana":"sol","sol":"sol","ethereum":"eth","eth":"eth",
                  "bsc":"bsc","bnb":"bsc","base":"base"}.get(chain_raw,"sol")
    dex_url  = f"https://dexscreener.com/{dex_chain}/{contract}"
    gmgn_url = f"https://gmgn.ai/{gmgn_chain}/token/{contract}"
    pump_url = f"https://pump.fun/{contract}"
    axiom_url= f"https://axiom.trade/t/{contract}"

    ext_row = [
        InlineKeyboardButton("📊 Dex",   url=dex_url),
        InlineKeyboardButton("🔍 GmGn",  url=gmgn_url),
    ]
    if chain_raw in ("solana", "sol"):
        ext_row.append(InlineKeyboardButton("🎯 Pump",  url=pump_url))
        ext_row.append(InlineKeyboardButton("⚡ Axiom", url=axiom_url))

    # Auto-sell button: if targets set → go to targets view (where you can cancel)
    as_cb = "vtg_" + contract if has_as else "asm_" + contract
    # Stop loss button: if SL set → go to targets view (where you can cancel SL)
    sl_cb = "vtg_" + contract if has_sl else "slm_" + contract

    # Quick Buy label shows configured amount
    qb_amt  = ud.get("quick_buy_amount", 100.0)
    qb_lbl  = "⚡ Quick Buy $" + str(int(qb_amt))
    # AUTOMATE button — label shows vault balance so user knows what's available
    _vault   = ud.get("apex_vault", 0.0)
    _auto_lbl = "🤖 AUTOMATE  🏦" + money(_vault) if _vault >= 1 else "🤖 AUTOMATE  (fund vault)"
    rows = [
        [InlineKeyboardButton("🔄 Refresh",       callback_data="rf_"  + contract)],
        [InlineKeyboardButton("⚡ Buy",            callback_data="bts_" + contract),
         InlineKeyboardButton("🔴 Sell",           callback_data="sts_" + contract)],
        [InlineKeyboardButton(qb_lbl,              callback_data="qb_"  + contract),
         InlineKeyboardButton("📊 Limit Buy",      callback_data="lbo_" + contract)],
        [InlineKeyboardButton(_auto_lbl,           callback_data="apex_auto_buy_" + contract)],
        [InlineKeyboardButton(as_lbl,              callback_data=as_cb),
         InlineKeyboardButton(sl_lbl,              callback_data=sl_cb)],
        [InlineKeyboardButton("📉 DCA",            callback_data="dca_" + contract),
         InlineKeyboardButton(alert_lbl,           callback_data=alert_cb)],
        [InlineKeyboardButton(track_lbl,           callback_data="wl_"  + contract),
         InlineKeyboardButton("🧠 Score",          callback_data="tks_" + contract),
         InlineKeyboardButton("📜 History",        callback_data="th_"  + contract)],
        [InlineKeyboardButton("◀ Back",            callback_data="mm")],
    ]
    return InlineKeyboardMarkup(rows)


def _is_group(update) -> bool:
    """True if the message/callback is from a group or supergroup."""
    try:
        chat = update.effective_chat
        return chat and chat.type in ("group", "supergroup")
    except Exception:
        return False


def group_buy_kb(contract: str) -> InlineKeyboardMarkup:
    """
    Group-mode keyboard — all action buttons are deep-link URLs that open
    each user's OWN private DM with the bot. No callback buttons that could
    execute on the admin's account. Refresh is the only in-group callback
    (safe — only fetches public token data).
    """
    bot_username = _bot_username  # set once at first sniper_job run
    def _dm(payload: str) -> str:
        if bot_username:
            return f"https://t.me/{bot_username}?start={payload}"
        return f"https://t.me/?start={payload}"   # fallback (bot_username not yet resolved)

    chain_raw  = "solana"   # default; group cards are mostly Solana
    dex_ch     = "solana"
    dex_url    = f"https://dexscreener.com/{dex_ch}/{contract}"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Buy",        url=_dm("buy_"   + contract)),
            InlineKeyboardButton("🔴 Sell",        url=_dm("sell_"  + contract)),
        ],
        [
            InlineKeyboardButton("🔔 Alert",       url=_dm("alert_" + contract)),
            InlineKeyboardButton("👁 Track",        url=_dm("track_" + contract)),
        ],
        [
            InlineKeyboardButton("🔄 Rescan",      callback_data="grp_rf_" + contract),
            InlineKeyboardButton("📊 Chart",       url=dex_url),
        ],
    ])


def buy_sub_kb(contract: str, ud: dict) -> InlineKeyboardMarkup:
    """Buy amount submenu — shown when user taps ⚡ Buy."""
    pb         = ud.get("preset_buy")
    preset_lbl = "⚡ $" + str(int(pb)) + " [Preset]" if pb else "⚙️ Set Preset"
    preset_cb  = "bp_" + contract if pb else "set_preset"
    qb_amt     = ud.get("quick_buy_amount", 100.0)
    qb_set_lbl = "⚡ Quick Buy: $" + str(int(qb_amt)) + " ⚙️"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("$25",         callback_data="ba_25_"   + contract),
         InlineKeyboardButton("$50",         callback_data="ba_50_"   + contract),
         InlineKeyboardButton("$100",        callback_data="ba_100_"  + contract),
         InlineKeyboardButton("$250",        callback_data="ba_250_"  + contract)],
        [InlineKeyboardButton("$500",        callback_data="ba_500_"  + contract),
         InlineKeyboardButton("$1000",       callback_data="ba_1000_" + contract),
         InlineKeyboardButton("✏️ Custom",   callback_data="bc_"      + contract),
         InlineKeyboardButton(preset_lbl,    callback_data=preset_cb)],
        [InlineKeyboardButton(qb_set_lbl,    callback_data="qb_set_"  + contract)],
        [InlineKeyboardButton("◀ Back",      callback_data="btt_" + contract)],
    ])


def sell_sub_kb(contract: str) -> InlineKeyboardMarkup:
    """Sell amount submenu — shown when user taps 🔴 Sell."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("25%",          callback_data="sp_25_"  + contract),
         InlineKeyboardButton("50%",          callback_data="sp_50_"  + contract),
         InlineKeyboardButton("75%",          callback_data="sp_75_"  + contract),
         InlineKeyboardButton("100%",         callback_data="sp_100_" + contract)],
        [InlineKeyboardButton("✏️ Custom %",  callback_data="sca_"    + contract),
         InlineKeyboardButton("🎯 Limit Sell",callback_data="lso_"    + contract)],
        [InlineKeyboardButton("◀ Back",       callback_data="btt_"    + contract)],
    ])


def sell_kb(contract: str) -> InlineKeyboardMarkup:
    """Kept for legacy references — delegates to sell_sub_kb."""
    return sell_sub_kb(contract)


def settings_kb(ud: dict) -> InlineKeyboardMarkup:
    pb  = "$" + str(int(ud["preset_buy"])) if ud.get("preset_buy") else "not set"
    ps  = str(ud["preset_sell"]) if ud.get("preset_sell") else "not set"
    rsk = str(ud["risk_pct"]) + "%" if ud.get("risk_pct") else "not set"
    mp  = str(ud["max_positions"]) if ud.get("max_positions") else "not set"
    dl  = str(ud["daily_limit"]) if ud.get("daily_limit") else "not set"
    asp = str(ud["auto_save_pct"]) + "%" if ud.get("auto_save_pct") else "not set"
    tgt = money(ud["target_equity"]) if ud.get("target_equity") else "not set"
    mdt = "ON" if ud.get("mood_tracking", True) else "OFF"
    rct = "ON ✅" if ud.get("risk_calc", True) else "OFF ❌"
    lang_labels = {"en": "🇬🇧 English", "es": "🇪🇸 Español", "pt": "🇧🇷 Português", "fr": "🇫🇷 Français", "zh": "🇨🇳 中文"}
    lang_lbl = lang_labels.get(ud.get("language", "en"), "🌐 Language")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Default Buy: " + pb,        callback_data="cfg_buy")],
        [InlineKeyboardButton("Default Sell: " + ps,       callback_data="cfg_sell")],
        [InlineKeyboardButton("Max Risk/Trade: " + rsk,    callback_data="cfg_risk")],
        [InlineKeyboardButton("Max Positions: " + mp,      callback_data="cfg_maxpos")],
        [InlineKeyboardButton("Daily Limit: " + dl,        callback_data="cfg_daily")],
        [InlineKeyboardButton("Auto-Save: " + asp,         callback_data="cfg_autosave")],
        [InlineKeyboardButton("Target Equity: " + tgt,     callback_data="cfg_target")],
        [InlineKeyboardButton("Mood Tracking: " + mdt,     callback_data="cfg_mood")],
        [InlineKeyboardButton("Risk Calc: " + rct,         callback_data="cfg_riskcalc")],
        [InlineKeyboardButton("🌐 Language: " + lang_lbl,  callback_data="cfg_lang")],
        [InlineKeyboardButton("Reset Account",             callback_data="rst_prompt")],
        [InlineKeyboardButton("◀ Back",                    callback_data="mm")],
    ])


def _manual_equity(ud: dict) -> float:
    """
    Challenge/Competition equity = balance + manual-only open positions + savings.
    APEX/AI-Sniper holdings and vault are excluded so APEX gains don't count.
    Savings included because they came from manual trading profits.
    """
    manual_invested = sum(
        h.get("total_invested", 0)
        for h in ud.get("holdings", {}).values()
        if h.get("mood") not in ("APEX", "AI-Sniper", "APEX-DCA")
    )
    return round(ud.get("balance", 0) + manual_invested + ud.get("savings", 0), 4)


def _challenge_share_card(ud: dict, ch: dict, current_eq: float, title: str, pnl_positive: bool) -> "io.BytesIO | None":
    """Generate a trade-card-style image for challenge completion/failure shares."""
    start_eq  = ch.get("start_eq", 0)
    target_eq = ch.get("target_eq", 0)
    days      = ch.get("days", 30)
    started   = ch.get("started", datetime.now().isoformat())
    try:
        started_dt  = datetime.fromisoformat(started)
        days_elapsed = (datetime.now() - started_dt).days
    except Exception:
        days_elapsed = days
    net_pnl    = current_eq - start_eq
    pnl_pct    = round(abs(net_pnl) / max(start_eq, 1) * 100, 1)
    x_val      = round(current_eq / max(start_eq, 1), 2)
    return generate_trade_card(
        symbol       = "CHALLENGE",
        chain        = "manual",
        pnl_str      = money(abs(net_pnl)).lstrip("$"),
        x_val        = str(x_val),
        held_h       = str(days_elapsed) + "d",
        bought_str   = money(start_eq).lstrip("$"),
        position_str = money(current_eq).lstrip("$"),
        username     = ud.get("username", "trader"),
        pnl_pct      = str(pnl_pct) + "%",
        pnl_positive = pnl_positive,
        closed_at    = datetime.now(),
        bought_label = "Started",
        position_label = "Final",
    )


async def _check_challenge(bot, uid: int, ud: dict, daily: bool = False) -> None:
    """
    Run after every manual trade close AND nightly from daily_summary_job.

    ── REDESIGNED LOGIC ─────────────────────────────────────────────────────────
    The challenge tracks CUMULATIVE REALIZED PnL from manual trades made AFTER
    the challenge started. This is the only correct measure — it is completely
    independent of savings, vault, balance from before, or any other funds.

    Example: User starts challenge with $100 capital, goal $1,000.
      - They need to EARN +$900 in profits from their trades.
      - If they have $10,000 savings it does NOT count — only trade PnL counts.
      - challenge_equity = start_capital + challenge_pnl
      - WIN:  challenge_equity >= target_eq  (e.g. $100 + $900 profit = $1,000)
      - FAIL: challenge_pnl <= -start_capital (lost all their starting capital)
      - TIME: days expire without hitting goal

    start_capital = the min_capital set at creation (their declared risk amount).
    ─────────────────────────────────────────────────────────────────────────────
    """
    ch = ud.get("challenge")
    if not ch or ch.get("ended"):
        return

    from datetime import date as _date

    # ── Resolve challenge start datetime ──────────────────────────────────────
    try:
        _challenge_start_dt = datetime.fromisoformat(ch["started"])
    except Exception:
        _challenge_start_dt = datetime.min

    # ── Cumulative PnL from manual trades made DURING this challenge ──────────
    # This is the correct measure. Savings, vault, pre-existing balance are ignored.
    challenge_pnl = round(sum(
        t.get("realized_pnl", 0)
        for t in trade_log.get(uid, [])
        if t.get("mood") not in ("APEX", "AI-Sniper", "APEX-DCA")
        and _safe_dt(t.get("closed_at")) >= _challenge_start_dt
    ), 4)

    # start_capital = how much they declared they're risking (min_capital from setup)
    start_capital   = ch.get("min_capital", ch.get("start_capital", 100.0))
    challenge_equity = round(start_capital + challenge_pnl, 4)

    target_eq   = ch.get("target_eq", 0)
    days_total  = ch.get("days", 30)

    # ── Duration ──────────────────────────────────────────────────────────────
    today = _date.today()
    if ch.get("end_date"):
        try:
            end_date = _date.fromisoformat(ch["end_date"])
        except Exception:
            end_date = today + timedelta(days=max(0, days_total - 1))
    else:
        try:
            start_date = _challenge_start_dt.date()
        except Exception:
            start_date = today
        end_date = start_date + timedelta(days=days_total - 1)
        ch["end_date"] = end_date.isoformat()
        save_user(uid, ud)

    days_elapsed = (today - (end_date - timedelta(days=days_total - 1))).days
    days_elapsed = max(0, days_elapsed)
    remaining    = (end_date - today).days

    # ── Progress: how far through the profit journey ─────────────────────────
    # profit_needed = target_eq - start_capital  (e.g. $1000 - $100 = $900 needed)
    profit_needed = max(target_eq - start_capital, 1)
    progress_pct  = round(challenge_pnl / profit_needed * 100, 1)
    progress_pct_clamped = max(0.0, min(100.0, progress_pct))
    bar_filled    = int(progress_pct_clamped / 10)
    progress_bar  = "█" * bar_filled + "░" * (10 - bar_filled)
    color         = "🟢" if challenge_pnl >= 0 else "🔴"

    # ── Count challenge trades ────────────────────────────────────────────────
    challenge_trades = [
        t for t in trade_log.get(uid, [])
        if t.get("mood") not in ("APEX", "AI-Sniper", "APEX-DCA")
        and _safe_dt(t.get("closed_at")) >= _challenge_start_dt
    ]
    total_trades = len(challenge_trades)
    wins         = sum(1 for t in challenge_trades if t.get("realized_pnl", 0) > 0)

    # ── 1. Milestone pings (25% / 50% / 75% of profit goal) ──────────────────
    milestones_hit = ch.setdefault("milestones_hit", [])
    for ms_pct, ms_icon, ms_label in [(25, "📈", "Quarter"), (50, "🔥", "Halfway"), (75, "🚀", "Three Quarters")]:
        if progress_pct_clamped >= ms_pct and ms_pct not in milestones_hit:
            milestones_hit.append(ms_pct)
            save_user(uid, ud)
            try:
                await bot.send_message(
                    chat_id=uid, parse_mode="Markdown",
                    text=(
                        ms_icon + " *CHALLENGE MILESTONE — " + ms_label.upper() + " WAY THERE!*\n\n"
                        "`" + progress_bar + "` *" + str(progress_pct_clamped) + "%*\n\n"
                        "💰 Capital: *" + money(challenge_equity) + "*\n"
                        "📈 Profit so far: *" + pstr(challenge_pnl) + "*\n"
                        "🎯 Goal: *" + money(target_eq) + "*\n"
                        "⏰ " + str(max(0, remaining)) + " days remaining"
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🎯 View Challenge", callback_data="v_challenge"),
                    ]])
                )
            except Exception:
                pass

    # ── 2. Goal reached ───────────────────────────────────────────────────────
    # challenge_equity >= target means start_capital + all profits >= goal
    if challenge_equity >= target_eq:
        ch["ended"]      = True
        ch["end_reason"] = "goal_reached"
        save_user(uid, ud)
        card = _challenge_share_card(ud, ch, challenge_equity, "CHALLENGE COMPLETE", True)
        wr_str = str(round(wins / total_trades * 100)) + "%" if total_trades > 0 else "N/A"
        summary_txt = (
            "🏆 *CHALLENGE COMPLETE!*\n\n"
            "You turned *" + money(start_capital) + "* into *" + money(challenge_equity) + "*!\n\n"
            "`" + progress_bar + "` *100%*\n\n"
            "🏁 Capital in:  *" + money(start_capital) + "*\n"
            "🎯 Goal:        *" + money(target_eq) + "*\n"
            "✅ Final:       *" + money(challenge_equity) + "*\n"
            "📈 Total profit: *+" + money(challenge_pnl) + "*\n"
            "📊 Trades: *" + str(total_trades) + "*  WR: *" + wr_str + "*\n"
            "📅 Completed in: *" + str(days_elapsed) + " day" + ("s" if days_elapsed != 1 else "") + "*\n\n"
            "_Start a new one from More → Challenge._"
        )
        share_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎯 New Challenge", callback_data="v_challenge"),
            InlineKeyboardButton("🏠 Menu",          callback_data="mm"),
        ]])
        try:
            if card:
                await bot.send_photo(chat_id=uid, photo=card, caption=summary_txt,
                                     parse_mode="Markdown", reply_markup=share_kb)
            else:
                await bot.send_message(chat_id=uid, text=summary_txt,
                                       parse_mode="Markdown", reply_markup=share_kb)
        except Exception:
            pass
        return

    # ── 3. Capital exhausted — lost all starting capital ─────────────────────
    # Only triggers when there are no open manual positions (might still recover)
    open_manual = any(
        h.get("mood") not in ("APEX", "AI-Sniper", "APEX-DCA")
        for h in ud.get("holdings", {}).values()
    )
    if challenge_pnl <= -start_capital and not open_manual:
        ch["ended"]      = True
        ch["end_reason"] = "capital_exhausted"
        save_user(uid, ud)
        card = _challenge_share_card(ud, ch, challenge_equity, "CHALLENGE FAILED", False)
        summary_txt = (
            "💀 *CHALLENGE FAILED — Capital Lost*\n\n"
            "Your *" + money(start_capital) + "* challenge capital was exhausted.\n\n"
            "`" + progress_bar + "` *" + str(max(0, progress_pct_clamped)) + "%*\n\n"
            "🏁 Capital in: *" + money(start_capital) + "*\n"
            "🎯 Goal:       *" + money(target_eq) + "*\n"
            "❌ Lost:       *" + money(abs(challenge_pnl)) + "*\n"
            "📊 Trades: *" + str(total_trades) + "*\n"
            "📅 Day *" + str(days_elapsed) + "* of *" + str(days_total) + "*\n\n"
            "_Review your trades and try again._"
        )
        retry_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Try Again", callback_data="v_challenge"),
            InlineKeyboardButton("🏠 Menu",      callback_data="mm"),
        ]])
        try:
            if card:
                await bot.send_photo(chat_id=uid, photo=card, caption=summary_txt,
                                     parse_mode="Markdown", reply_markup=retry_kb)
            else:
                await bot.send_message(chat_id=uid, text=summary_txt,
                                       parse_mode="Markdown", reply_markup=retry_kb)
        except Exception:
            pass
        return

    # ── 4. Time expired ───────────────────────────────────────────────────────
    if remaining < 0:
        ch["ended"]      = True
        ch["end_reason"] = "time_expired"
        reached = challenge_equity >= target_eq
        save_user(uid, ud)
        card = _challenge_share_card(ud, ch, challenge_equity, "CHALLENGE ENDED", reached)
        wr_str2 = str(round(wins / total_trades * 100)) + "%" if total_trades > 0 else "N/A"
        summary_txt = (
            ("✅" if reached else "⏰") + " *CHALLENGE ENDED — Time Up*\n\n"
            "`" + progress_bar + "` *" + str(max(0, progress_pct_clamped)) + "%*\n\n"
            "🏁 Capital in: *" + money(start_capital) + "*\n"
            "🎯 Goal:       *" + money(target_eq) + "*\n"
            "💰 Final:      *" + money(challenge_equity) + "*\n"
            "📈 Profit:     *" + pstr(challenge_pnl) + "*\n"
            "📊 Trades: *" + str(total_trades) + "*  WR: *" + wr_str2 + "*\n"
            "📅 Duration: *" + str(days_total) + " days*\n\n"
            + ("✅ *Goal reached!*" if reached else "❌ Goal not reached — try again!")
        )
        end_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎯 New Challenge", callback_data="v_challenge"),
            InlineKeyboardButton("🏠 Menu",          callback_data="mm"),
        ]])
        try:
            if card:
                await bot.send_photo(chat_id=uid, photo=card, caption=summary_txt,
                                     parse_mode="Markdown", reply_markup=end_kb)
            else:
                await bot.send_message(chat_id=uid, text=summary_txt,
                                       parse_mode="Markdown", reply_markup=end_kb)
        except Exception:
            pass
        return

    # ── 5. Daily progress nudge ───────────────────────────────────────────────
    if daily:
        from datetime import date as _date2
        _today2 = _date2.today()
        today_trades = [
            t for t in challenge_trades
            if (lambda v: v.date() if isinstance(v, datetime) else
                (datetime.fromisoformat(v).date() if isinstance(v, str) else None)
               )(_safe_dt(t.get("closed_at"))) == _today2
        ]
        day_pnl    = sum(t.get("realized_pnl", 0) for t in today_trades)
        day_wins   = sum(1 for t in today_trades if t.get("realized_pnl", 0) > 0)
        still_need = max(0, target_eq - challenge_equity)
        pace_emoji = "🟢" if days_elapsed > 0 and progress_pct_clamped / days_elapsed * days_total >= 90 else ("🟡" if progress_pct_clamped > 0 else "🔴")

        daily_txt = (
            "📅 *CHALLENGE UPDATE — Day " + str(days_elapsed + 1) + "/" + str(days_total) + "*\n\n"
            "`" + progress_bar + "` *" + str(progress_pct_clamped) + "%*\n\n"
            + color + " Capital now: *" + money(challenge_equity) + "*\n"
            "📈 Profit so far: *" + pstr(challenge_pnl) + "*\n"
            "🎯 Goal:          *" + money(target_eq) + "*\n"
            "📍 Still need:    *+" + money(still_need) + "* profit\n"
            "⏰ " + str(max(0, remaining)) + " days left\n"
            "📊 Total trades: *" + str(total_trades) + "*\n"
        )
        if today_trades:
            wr_d = round(day_wins / len(today_trades) * 100)
            daily_txt += (
                "\n*Today:* " + str(len(today_trades)) + " trade" + ("s" if len(today_trades) != 1 else "")
                + "  WR:" + str(wr_d) + "%  " + pstr(day_pnl) + "\n"
            )
        daily_txt += "\n" + pace_emoji + " *Pace:* " + (
            "On track 🎯" if days_elapsed > 0 and progress_pct_clamped / days_elapsed * days_total >= 90
            else "Behind pace — push harder! 💪" if progress_pct_clamped > 0
            else "No trades yet — make your first move!"
        )
        try:
            await bot.send_message(
                chat_id=uid, parse_mode="Markdown",
                text=daily_txt,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎯 View Challenge", callback_data="v_challenge"),
                ]])
            )
        except Exception:
            pass





async def _check_apex_vault_exhausted(bot, uid: int, ud: dict) -> None:
    """
    Called after every APEX position close.
    If vault < minimum trade size AND no open APEX positions remain,
    auto-disable APEX and DM the user with a clear explanation.
    """
    if not ud.get("apex_mode"):
        return
    if ud.get("_apex_vault_low_notified"):
        return
    vault     = ud.get("apex_vault", 0.0)
    min_trade = float(ud.get("sniper_filters", {}).get("buy_amount", 20.0))
    open_apex = sum(
        1 for h in ud.get("holdings", {}).values()
        if h.get("mood") in ("APEX", "AI-Sniper", "APEX-DCA")
    )
    if vault < min_trade and open_apex == 0:
        ud["apex_mode"]                = False
        ud["_apex_vault_low_notified"] = True
        save_user(uid, ud)
        try:
            await bot.send_message(
                chat_id=uid, parse_mode="Markdown",
                text=(
                    "🔴 *APEX AUTO-DISABLED — Vault Exhausted*\n\n"
                    "All APEX positions have closed and your vault balance "
                    "(*" + money(vault) + "*) is below the minimum trade size "
                    "of *" + money(min_trade) + "*.\n\n"
                    "APEX has been *paused automatically*.\n\n"
                    "To resume:\n"
                    "1️⃣ Go to *APEX → Vault → Fund Vault*\n"
                    "2️⃣ Transfer funds from your main balance\n"
                    "3️⃣ Return to *APEX* and tap *Enable APEX*\n\n"
                    "💵 Main balance available: *" + money(ud.get("balance", 0)) + "*"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏦 Fund Vault", callback_data="apex_vault_fund"),
                     InlineKeyboardButton("🏠 Menu",       callback_data="mm")],
                ])
            )
        except Exception as _vex:
            logger.debug(f"Vault exhausted notify failed uid={uid}: {_vex}")


def back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="mm")]])


def back_more() -> InlineKeyboardMarkup:
    """Back button for screens accessed from the More menu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀ Back to More", callback_data="v_more")],
        [InlineKeyboardButton("🏠 Main Menu",   callback_data="mm")],
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="mm")]])


def buy_done_kb(contract: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Auto-Sell", callback_data="asm_" + contract),
         InlineKeyboardButton("🛑 Stop Loss", callback_data="slm_" + contract)],
        [InlineKeyboardButton("📝 Journal",   callback_data="jnl_" + contract),
         InlineKeyboardButton("View Token",   callback_data="btt_" + contract)],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="mm")],
    ])


async def run_checker(app: Application):
    import time as _time_local

    await bundle_sell_detector(app)

    # ── APEX: position manager is handled exclusively by apex_checker_job ─────
    # (runs every 8s with adaptive per-threat intervals).
    # Calling it here too caused double-processing of exit logic every 20s.

    # ── APEX: entry confirmation queue ────────────────────────────────────────
    for _apex_uid, _apex_ud in list(users.items()):
        if _apex_uid in _apex_entry_queue:
            try:
                await apex_process_entry_queue(app, _apex_uid, _apex_ud)
            except Exception as _apex_qe:
                logger.error(f"APEX entry queue error for {_apex_uid}: {_apex_qe}", exc_info=True)

    # ── APEX: re-entry watchlist checker (throttled per-token to 2 min) ───────
    try:
        await apex_watchlist_checker(app)
    except Exception as _wlce:
        logger.debug(f"Watchlist checker error: {_wlce}")

    # ── APEX: post-exit snapshot collector ────────────────────────────────────
    for _apex_uid, _apex_ud in list(users.items()):
        if _apex_uid in _apex_post_exit and _apex_post_exit[_apex_uid]:
            try:
                await apex_post_exit_tracker_run(app, _apex_uid)
            except Exception as _pete:
                logger.debug(f"Post-exit tracker error {_apex_uid}: {_pete}")


    # ── Pre-warm cache: collect all unique contracts across all users ──────────
    # then fetch them ALL in parallel. Subsequent get_token() calls in the loop
    # hit the cache instantly instead of making separate HTTP requests.
    all_contracts: set = set()
    for ud in users.values():
        if ud.get("balance") is None:
            continue
        for a in ud.get("price_alerts", []):
            if not a.get("triggered"):
                all_contracts.add(a["contract"])
        for o in ud.get("limit_orders", []):
            if not o.get("triggered") and not o.get("cancelled"):
                all_contracts.add(o["contract"])
        all_contracts.update(ud.get("holdings", {}).keys())
        all_contracts.update(ud.get("watchlist", {}).keys())
        for dca in ud.get("dca_orders", []):
            if not dca.get("cancelled"):
                all_contracts.add(dca["contract"])
    if all_contracts:
        await _asyncio.gather(*[get_token(c, force=True) for c in all_contracts])
    # ─────────────────────────────────────────────────────────────────────────

    for uid, ud in list(users.items()):
        if ud.get("balance") is None:
            continue

        # Price alerts
        for alert in list(ud.get("price_alerts", [])):
            if alert.get("triggered"):
                continue
            info = await get_token(alert["contract"])
            if not info:
                continue
            hit = (
                (alert["direction"] == "above" and info["price"] >= alert["target"]) or
                (alert["direction"] == "below" and info["price"] <= alert["target"])
            )
            if hit:
                alert["triggered"] = True
                try:
                    await app.bot.send_message(
                        chat_id=uid, parse_mode="Markdown",
                        text=(
                            "🔔 *PRICE ALERT*\n\n"
                            "*$" + _md(alert["symbol"]) + "* hit your target!\n"
                            "Price: *" + money(info["price"]) + "*\n"
                            "Target: *" + money(alert["target"]) + "*"
                        ),
                        reply_markup=main_menu_kb()
                    )
                except Exception as e:
                    logger.error(e)
        ud["price_alerts"] = [a for a in ud.get("price_alerts", []) if not a.get("triggered")]

        # Limit orders
        for order in list(ud.get("limit_orders", [])):
            if order.get("triggered") or order.get("cancelled"):
                continue
            info = await get_token(order["contract"])
            if not info:
                continue
            price = info["price"]

            if order["type"] == "buy" and price <= order["target_price"]:
                order["triggered"] = True
                if ud["balance"] >= order["amount"]:
                    amt = order["amount"]
                    tokens = amt / price
                    c = order["contract"]
                    ud["balance"] = round(ud["balance"] - amt, 4)
                    if c in ud["holdings"]:
                        h = ud["holdings"][c]
                        nt = h["total_invested"] + amt
                        na = h["amount"] + tokens
                        h["avg_price"] = nt / na
                        h["amount"] = na
                        h["total_invested"] = nt
                    else:
                        ud["holdings"][c] = {
                            "symbol": info["symbol"], "name": info["name"],
                            "chain": info["chain"], "amount": tokens,
                            "avg_price": price, "total_invested": amt,
                            "total_sold": 0.0,
                            "auto_sells": [], "stop_loss_pct": None,
                            "bought_at": datetime.now(), "journal": "",
                            "mood": "", "planned": True, "followed_plan": None,
                            "peak_price": price,
                            "price_history": [], "liq_history": [],
                            "stop_loss_history": [], "auto_sell_history": [], "threat_history": [],
                        }
                    try:
                        await app.bot.send_message(
                            chat_id=uid, parse_mode="Markdown",
                            text=(
                                "✅ *LIMIT BUY EXECUTED*\n\n"
                                "*$" + _md(info["symbol"]) + "* hit " + money(order["target_price"]) + "\n"
                                "Bought: " + money(amt) + "\n"
                                "Price: " + money(price) + "\n"
                                "Cash left: " + money(ud["balance"])
                            ),
                            reply_markup=main_menu_kb()
                        )
                    except Exception as e:
                        logger.error(e)

            elif order["type"] == "sell" and order["contract"] in ud["holdings"]:
                if price >= order["target_price"]:
                    order["triggered"] = True
                    h = ud["holdings"][order["contract"]]
                    cv = h["amount"] * price
                    sell_amt = min(order["amount"], cv)
                    result = sell_core(ud, uid, order["contract"], sell_amt, price, "limit_sell")
                    try:
                        await app.bot.send_message(
                            chat_id=uid, parse_mode="Markdown",
                            text=(
                                "✅ *LIMIT SELL EXECUTED*\n\n"
                                "*$" + _md(info["symbol"]) + "* hit " + money(order["target_price"]) + "\n"
                                "Sold: " + money(sell_amt) + "\n"
                                "Price: " + money(price) + "\n"
                                "PnL: " + pstr(result["realized"]) + "\n"
                                "Cash: " + money(ud["balance"])
                            ),
                            reply_markup=main_menu_kb()
                        )
                    except Exception as e:
                        logger.error(e)

        ud["limit_orders"] = [
            o for o in ud.get("limit_orders", [])
            if not o.get("triggered") and not o.get("cancelled")
        ]

        # Auto-sells and stop losses
        for contract, h in list(ud["holdings"].items()):
            info = await get_token(contract)
            if not info:
                continue
            price = info["price"]
            avg = h.get("avg_price", price)
            cx = price / avg if avg > 0 else 0

            # ── Skip ALL APEX/AI-Sniper positions — apex_run_position_manager
            # and apex_checker_job own these exclusively. Do NOT gate this on
            # apex_trail_stop being set: before 1.5x the trail is None and both
            # loops would fire on the same position causing a double-sell race.
            if h.get("mood") in ("APEX", "AI-Sniper", "APEX-DCA"):
                continue

            sl = h.get("stop_loss_pct")
            if sl:
                drop = (price - avg) / avg * 100
                if drop <= -sl:
                    cv = h["amount"] * price
                    result = sell_core(ud, uid, contract, cv, price, "stop_loss")
                    ud["followed"] += 1
                    ud["streak"] += 1
                    ud["best_streak"] = max(ud["best_streak"], ud["streak"])
                    txt = (
                        "🛑 *STOP LOSS HIT*\n\n"
                        "*$" + _md(h["symbol"]) + "* dropped " + str(round(drop, 1)) + "%\n"
                        "Sold 100% → " + money(result["received"]) + "\n"
                        "PnL: " + pstr(result["realized"]) + "\n"
                        "Cash: " + money(ud["balance"])
                    )
                    if result["auto_saved"] > 0:
                        txt += "\nAuto-saved: " + money(result["auto_saved"])
                    try:
                        await app.bot.send_message(chat_id=uid, parse_mode="Markdown", text=txt, reply_markup=main_menu_kb())
                    except Exception as e:
                        logger.error(e)
                    continue

            for t in sorted([a for a in h.get("auto_sells", []) if not a.get("triggered")], key=lambda a: a["x"]):
                if cx < t["x"] or contract not in ud["holdings"]:
                    break
                t["triggered"] = True
                cv = h["amount"] * price
                sv = cv * t["pct"]
                if sv < 0.001:
                    continue
                result = sell_core(ud, uid, contract, sv, price, "auto_sell")
                ud["followed"] += 1
                ud["streak"] += 1
                ud["best_streak"] = max(ud["best_streak"], ud["streak"])
                # ── Log auto-sell event so the trigger price and PnL are
                # never lost (the target gets deleted from auto_sells on close)
                import time as _tas
                h.setdefault("auto_sell_history", []).append({
                    "x":    t["x"],
                    "pct":  t["pct"],
                    "price": price,
                    "pnl":  result["realized"],
                    "ts":   _tas.time(),
                })
                txt = (
                    "🤖 *AUTO-SELL TRIGGERED*\n\n"
                    "*$" + _md(h["symbol"]) + "* hit " + str(t["x"]) + "x!\n"
                    "Sold " + str(int(t["pct"]*100)) + "% → " + money(result["received"]) + "\n"
                    "Price: " + money(price) + "  |  " + str(round(cx, 2)) + "x\n"
                    "PnL: " + pstr(result["realized"]) + "\n"
                    "Cash: " + money(ud["balance"])
                )
                if result["auto_saved"] > 0:
                    txt += "\nAuto-saved: " + money(result["auto_saved"])
                try:
                    await app.bot.send_message(chat_id=uid, parse_mode="Markdown", text=txt, reply_markup=main_menu_kb())
                except Exception as e:
                    logger.error(e)

        # Notify copy followers about sells too
        # (handled separately - followers see position updates via portfolio)

        # Whale alerts — detect large volume spikes on held/watched tokens
        if ud.get("whale_alerts", True):
            whale_candidates = set(ud["holdings"].keys()) | set(ud.get("watchlist", {}).keys())
            for wca in whale_candidates:
                try:
                    wi = await get_token(wca)
                    if not wi:
                        continue
                    vol_m5 = wi.get("vol_m5", 0)
                    vol_h1  = wi.get("vol_h1", 0)
                    # Average 5-min slice over the last hour
                    avg_5m = vol_h1 / 12 if vol_h1 > 0 else 0
                    # Whale threshold: last 5-min volume is 4x the hourly average
                    # AND the absolute spike is at least $20K to filter micro-caps noise
                    if avg_5m > 0 and vol_m5 >= avg_5m * 4 and vol_m5 >= 20_000:
                        sym = wi.get("symbol", "?")
                        spike_x = round(vol_m5 / avg_5m, 1)
                        # Deduplicate: only alert once per token per hour
                        last_whale = ud.setdefault("_whale_last", {})
                        now_h = datetime.now().strftime("%Y%m%d%H")
                        alert_key = wca + "_" + now_h
                        if alert_key not in last_whale:
                            last_whale[alert_key] = True
                            # Clean up old keys
                            ud["_whale_last"] = {k: v for k, v in last_whale.items()
                                                 if k.split("_")[-1] >= (datetime.now() - timedelta(hours=2)).strftime("%Y%m%d%H")}
                            holding_line = ""
                            if wca in ud["holdings"]:
                                h = ud["holdings"][wca]
                                cv = h["amount"] * wi["price"]
                                cx = wi["price"] / h["avg_price"] if h.get("avg_price", 0) > 0 else 0
                                holding_line = "\n📌 Your position: " + money(cv) + "  (" + str(round(cx, 2)) + "x)"
                            try:
                                await app.bot.send_message(
                                    chat_id=uid,
                                    parse_mode="Markdown",
                                    text=(
                                        "🐋 *WHALE ALERT*\n\n"
                                        "*$" + sym + "* is seeing a massive volume spike!\n"
                                        "5m Volume: *" + money(vol_m5) + "* (" + str(spike_x) + "x avg)\n"
                                        "Price: *" + money(wi["price"]) + "*\n"
                                        "MC: *" + mc_str(wi["mc"]) + "*" + holding_line
                                    ),
                                    reply_markup=main_menu_kb()
                                )
                            except Exception as _we:
                                logger.warning(f"Whale alert send failed: {_we}")
                except Exception:
                    continue

        # Watchlist alerts
        for wca, wt in list(ud.get("watchlist", {}).items()):
            try:
                wi = await get_token(wca)
                if not wi:
                    continue
                tp = wt.get("target_price")
                tm = wt.get("target_mc")
                if tp and wi["price"] >= tp:
                    await app.bot.send_message(uid,
                        f"👁 *WATCHLIST ALERT*\n\n"
                        f"${wt['symbol']} hit your target price!\n"
                        f"Price: ${wi['price']:.8g} (target: ${tp:.8g})\n"
                        f"MC: {mc_str(wi['mc'])}",
                        parse_mode="Markdown")
                    ud["watchlist"][wca]["target_price"] = None
                if tm and wi["mc"] >= tm:
                    await app.bot.send_message(uid,
                        f"👁 *WATCHLIST ALERT*\n\n"
                        f"${wt['symbol']} hit your target MC!\n"
                        f"MC: {mc_str(wi['mc'])} (target: {mc_str(tm)})\n"
                        f"Price: ${wi['price']:.8g}",
                        parse_mode="Markdown")
                    ud["watchlist"][wca]["target_mc"] = None
            except Exception:
                continue

        # ── HOLDINGS: peak price update + milestone alerts + rug pull warning ──
        for hca, h in list(ud.get("holdings", {}).items()):
            try:
                hi = await get_token(hca)
                if not hi:
                    continue
                cur_price = hi["price"]

                # Update peak price for replay
                if cur_price > h.get("peak_price", 0):
                    h["peak_price"] = cur_price

                # ── Append price snapshot to universal history ────────────────
                # APEX positions also have sr_history (vol-aware, used by S/R engine).
                # price_history is simpler and works for ALL position types —
                # it's what the positions screen sparkline reads from.
                import time as _th
                _now_ts = _th.time()
                ph = h.setdefault("price_history", [])
                ph.append({
                    "price": cur_price,
                    "mc":    hi.get("mc", 0),
                    "ts":    _now_ts,
                })
                if len(ph) > 500:
                    h["price_history"] = ph[-500:]

                # ── Append liquidity snapshot ─────────────────────────────────
                # liq_at_buy is fixed at entry. liq_history shows the full curve
                # so you can see liquidity drain before it triggers a threat.
                lh = h.setdefault("liq_history", [])
                lh.append({
                    "liq": hi.get("liq", 0),
                    "ts":  _now_ts,
                })
                if len(lh) > 500:
                    h["liq_history"] = lh[-500:]

                avg_price = h.get("avg_price", 0)
                if avg_price <= 0:
                    continue
                current_x = cur_price / avg_price

                # ── MILESTONE ALERTS ──────────────────────────────────────────
                if ud.get("milestone_notif", True):
                    milestones_hit = h.setdefault("milestones_hit", [])
                    invested = h.get("total_invested", 0)
                    cur_value = h["amount"] * cur_price

                    # Build dynamic milestone list: every integer from 2x up to
                    # the current x — unlimited, no cap.
                    _max_milestone = int(current_x)
                    for level in range(2, _max_milestone + 1):
                        if level not in milestones_hit:
                            milestones_hit.append(level)
                            profit = cur_value - invested
                            try:
                                await app.bot.send_message(
                                    chat_id=uid, parse_mode="Markdown",
                                    text=(
                                        "🎯 *" + str(level) + "× MILESTONE HIT*\n\n"
                                        "*$" + _md(h["symbol"]) + "*  ·  " + h.get("chain","?").upper() + "\n"
                                        "━━━━━━━━━━━━━━━━\n"
                                        "Invested: *" + money(invested) + "*\n"
                                        "Value now: *" + money(cur_value) + "*\n"
                                        "Profit: *+" + money(profit) + "*"
                                    ),
                                    reply_markup=InlineKeyboardMarkup([
                                        [InlineKeyboardButton("View Token", callback_data="btt_" + hca),
                                         InlineKeyboardButton("Sell Now",   callback_data="sts_" + hca)],
                                    ])
                                )
                            except Exception as _me:
                                logger.warning(f"Milestone alert error: {_me}")

                # ── DUMP ALERT ────────────────────────────────────────────────
                if ud.get("milestone_notif_dump", True):
                    dump_hit = h.setdefault("dump_alerted", False)
                    if not dump_hit and current_x <= 0.5:
                        h["dump_alerted"] = True
                        invested = h.get("total_invested", 0)
                        cur_value = h["amount"] * cur_price
                        loss = cur_value - invested
                        try:
                            await app.bot.send_message(
                                chat_id=uid, parse_mode="Markdown",
                                text=(
                                    "🚨 *DUMP ALERT  –50%*\n\n"
                                    "*$" + _md(h["symbol"]) + "*  ·  " + h.get("chain","?").upper() + "\n"
                                    "━━━━━━━━━━━━━━━━\n"
                                    "Invested: *" + money(invested) + "*\n"
                                    "Value now: *" + money(cur_value) + "*\n"
                                    "Loss: *" + money(loss) + "*"
                                ),
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("🔍 View Token", callback_data="btt_" + hca),
                                     InlineKeyboardButton("🛑 Cut Loss",   callback_data="sts_" + hca)],
                                ])
                            )
                        except Exception as _de:
                            logger.warning(f"Dump alert error: {_de}")

                # ── RUG PULL WARNING ──────────────────────────────────────────
                if ud.get("rug_warn_enabled", False):
                    cur_liq  = hi.get("liq", 0)
                    threshold = ud.get("rug_warn_threshold", 30) / 100.0
                    prev_liq  = _rug_liq_prev.setdefault(uid, {}).get(hca, cur_liq)
                    if prev_liq > 0 and cur_liq < prev_liq * (1 - threshold):
                        drop_pct = round((1 - cur_liq / prev_liq) * 100, 1)
                        invested = h.get("total_invested", 0)
                        cur_value = h["amount"] * cur_price
                        rug_key = hca + "_rug_" + str(int(prev_liq))
                        if rug_key not in h.get("rug_warned", []):
                            h.setdefault("rug_warned", []).append(rug_key)
                            try:
                                await app.bot.send_message(
                                    chat_id=uid, parse_mode="Markdown",
                                    text=(
                                        "🔥 *RUG PULL WARNING*\n\n"
                                        "*$" + _md(h["symbol"]) + "*  ·  " + h.get("chain","?").upper() + "\n"
                                        "━━━━━━━━━━━━━━━━\n"
                                        "💧 Liquidity dropped *–" + str(drop_pct) + "%* this cycle\n"
                                        "Was: *" + money(prev_liq) + "*  →  Now: *" + money(cur_liq) + "*\n\n"
                                        "Your bag: *" + money(cur_value) + "*\n"
                                        "⚠️ LP may be being pulled — consider exiting"
                                    ),
                                    reply_markup=InlineKeyboardMarkup([
                                        [InlineKeyboardButton("😤 Ignore",          callback_data="btt_" + hca),
                                         InlineKeyboardButton("🚨 Sell Everything", callback_data="sts_" + hca)],
                                    ])
                                )
                            except Exception as _re:
                                logger.warning(f"Rug warn error: {_re}")
                    _rug_liq_prev.setdefault(uid, {})[hca] = cur_liq

            except Exception as _hce:
                logger.warning(f"Holdings checker error for {hca}: {_hce}")

        # DCA by Market Cap — trigger buys when token hits set MC milestones
        for dca in list(ud.get("dca_orders", [])):
            if dca.get("cancelled"):
                continue
            try:
                di = await get_token(dca["contract"])
                if not di:
                    continue
                for tgt in dca.get("mc_targets", []):
                    if tgt.get("triggered"):
                        continue
                    if di["mc"] >= tgt["mc"]:
                        tgt["triggered"] = True
                        buy_amt = tgt["amount"]
                        if ud["balance"] < buy_amt:
                            await app.bot.send_message(
                                chat_id=uid, parse_mode="Markdown",
                                text="📉 *DCA SKIPPED*\n\n$" + dca["symbol"] + " hit " + mc_str(tgt["mc"]) + " MC but you don't have enough balance.",
                                reply_markup=main_menu_kb()
                            )
                            continue
                        result = await do_buy_core(ud, uid, dca["contract"], buy_amt, planned=True, mood="DCA")
                        if isinstance(result, tuple):
                            info2, _ = result
                            await app.bot.send_message(
                                chat_id=uid, parse_mode="Markdown",
                                text=t(ud, "dca_fired",
                                    symbol=dca["symbol"], mc=mc_str(tgt["mc"]),
                                    amount=money(buy_amt), price=money(info2["price"]),
                                    cash=money(ud["balance"])
                                ),
                                reply_markup=main_menu_kb()
                            )
                # Clean fully triggered DCA orders
                all_done = all(t2.get("triggered") for t2 in dca.get("mc_targets", []))
                if all_done:
                    dca["cancelled"] = True
            except Exception as _dce:
                logger.warning(f"DCA checker error: {_dce}")
        ud["dca_orders"] = [d for d in ud.get("dca_orders", []) if not d.get("cancelled")]


async def daily_summary_job(ctx: ContextTypes.DEFAULT_TYPE):
    for uid, ud in list(users.items()):
        if ud.get("balance") is None:
            continue
        today = datetime.now().date()
        logs = trade_log.get(uid, [])

        def _to_date(val):
            if isinstance(val, datetime):
                return val.date()
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val).date()
                except Exception:
                    return None
            return None

        today_trades = [t for t in logs if _to_date(t.get("closed_at")) == today]

        # ── Daily equity snapshot ─────────────────────────────────────────────
        # Saved regardless of whether user trades today. This gives a complete
        # equity curve including idle days with open positions.
        _eq = (
            ud.get("balance", 0)
            + sum(h.get("total_invested", 0) for h in ud.get("holdings", {}).values())
            + ud.get("savings", 0)
            + ud.get("apex_vault", 0.0)
        )
        _day_pnl = sum(t["realized_pnl"] for t in today_trades) if today_trades else 0.0
        ud.setdefault("equity_history", []).append({
            "date":    today.isoformat(),
            "equity":  round(_eq, 4),
            "balance": round(ud.get("balance", 0), 4),
            "pnl":     round(_day_pnl, 4),
        })
        # Keep 365 days
        if len(ud["equity_history"]) > 365:
            ud["equity_history"] = ud["equity_history"][-365:]
        # ── Daily challenge check ─────────────────────────────────────────────
        if ud.get("challenge") and not ud["challenge"].get("ended"):
            try:
                await _check_challenge(ctx.bot, uid, ud, daily=True)
            except Exception:
                pass
        # APEX report: always send if apex_mode is on, OR if user had any APEX trades today
        if ud.get("apex_mode") or any(t.get("mood") in ("APEX","AI-Sniper") for t in today_trades):
            await apex_daily_report(ctx.bot, uid, ud)
            continue
        if not today_trades:
            continue
        wins = [t for t in today_trades if t["realized_pnl"] > 0]
        tpnl = sum(t["realized_pnl"] for t in today_trades)
        wr = round(len(wins)/len(today_trades)*100) if today_trades else 0
        try:
            await ctx.bot.send_message(
                chat_id=uid, parse_mode="Markdown",
                text=(
                    "\U0001f4c5 *DAILY SUMMARY*\n\n"
                    "Trades: " + str(len(today_trades)) + "  |  WR: " + str(wr) + "%\n"
                    "PnL: " + pstr(tpnl) + "\n"
                    "Cash: " + money(ud["balance"]) + "\n"
                    "Savings: " + money(ud["savings"])
                ),
                reply_markup=main_menu_kb()
            )
        except Exception:
            pass


async def monthly_report_job(ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    if now.day != 1:
        return
    month_ago = now - timedelta(days=30)
    for uid, ud in list(users.items()):
        if ud.get("balance") is None:
            continue
        logs = trade_log.get(uid, [])
        monthly = [t for t in logs if _safe_dt(t.get("closed_at")) >= month_ago]
        if not monthly:
            continue
        wins = [t for t in monthly if t["realized_pnl"] > 0]
        losses = [t for t in monthly if t["realized_pnl"] <= 0]
        tpnl = sum(t["realized_pnl"] for t in monthly)
        wr = round(len(wins) / len(monthly) * 100) if monthly else 0
        aw = sum(t["realized_pnl"] for t in wins) / len(wins) if wins else 0
        al = sum(t["realized_pnl"] for t in losses) / len(losses) if losses else 0
        best = max(monthly, key=lambda t: t["realized_pnl"])
        worst = min(monthly, key=lambda t: t["realized_pnl"])
        sb = ud.get("starting_balance", 0)
        eq = ud["balance"] + sum(h["total_invested"] for h in ud["holdings"].values()) + ud["savings"]
        growth = round((eq - sb) / sb * 100, 1) if sb > 0 else 0

        mood_txt = ""
        if ud.get("mood_stats"):
            best_mood = max(ud["mood_stats"].items(), key=lambda x: x[1]["pnl"])
            worst_mood = min(ud["mood_stats"].items(), key=lambda x: x[1]["pnl"])
            mood_txt = (
                "\n\nBest entry reason: " + best_mood[0] + " (" + pstr(best_mood[1]["pnl"]) + ")\n"
                "Worst entry reason: " + worst_mood[0] + " (" + pstr(worst_mood[1]["pnl"]) + ")"
            )

        try:
            await ctx.bot.send_message(
                chat_id=uid, parse_mode="Markdown",
                text=(
                    "📊 *MONTHLY REPORT*\n\n"
                    "Trades: " + str(len(monthly)) + "  (" + str(len(wins)) + "W / " + str(len(losses)) + "L)\n"
                    "Win Rate: " + str(wr) + "%\n"
                    "Avg Win: " + money(aw) + "\n"
                    "Avg Loss: " + money(al) + "\n"
                    "Total PnL: " + pstr(tpnl) + "\n\n"
                    "Best Trade: " + pstr(best["realized_pnl"]) + " ($" + best["symbol"] + ")\n"
                    "Worst Trade: " + pstr(worst["realized_pnl"]) + " ($" + worst["symbol"] + ")\n\n"
                    "Account Equity: " + money(eq) + "\n"
                    "Savings: " + money(ud["savings"]) + "\n"
                    "Growth: " + str(growth) + "%" + mood_txt
                ),
                reply_markup=main_menu_kb()
            )
        except Exception:
            pass


async def checker_job(ctx: ContextTypes.DEFAULT_TYPE):
    await run_checker(ctx.application)


async def rejected_token_outcome_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs every hour. Checks near-miss rejected tokens at the 24h mark.
    If any pumped 2x+, DMs the user so they know the filter missed a winner.
    This data also feeds into the Sunday weekly filter analysis report.
    Only tracks borderline score/MC rejections — hard flag rejections are
    correct by definition and not worth tracking.
    """
    import time as _rjo
    now_ts = _rjo.time()
    for uid, rejected in list(_apex_rejected.items()):
        ud = users.get(uid)
        if not ud:
            continue
        pumped_tokens = []
        for contract, rec in list(rejected.items()):
            if rec.get("checked_24h"):
                if now_ts - rec.get("reject_ts", 0) > 172800:  # expire after 48h
                    del rejected[contract]
                continue
            if now_ts - rec.get("reject_ts", 0) < 82800:  # wait at least 23h
                continue
            try:
                info_now = await get_token(contract)
            except Exception:
                info_now = None
            rec["checked_24h"] = True
            if not info_now:
                continue
            mc_now    = info_now.get("mc", 0)
            mc_reject = rec.get("mc_at_reject", 0)
            if mc_reject <= 0:
                continue
            outcome_x = round(mc_now / mc_reject, 2)
            rec["outcome_x_24h"] = outcome_x
            if outcome_x >= 2.0:
                pumped_tokens.append({
                    "symbol":        rec.get("symbol", "?"),
                    "contract":      contract,
                    "score":         rec.get("score", 0),
                    "reject_reason": rec.get("reject_reason", "?"),
                    "mc_reject":     mc_reject,
                    "mc_now":        mc_now,
                    "outcome_x":     outcome_x,
                    "chain":         rec.get("chain", "sol"),
                })
        if not pumped_tokens:
            continue
        pumped_tokens.sort(key=lambda x: -x["outcome_x"])
        lines = [
            "🔍 *MISSED OPPORTUNITIES — 24h CHECK*\n",
            "_These near-miss tokens were rejected by filters then pumped:_\n",
        ]
        for tk in pumped_tokens[:5]:
            ds_link = ("https://dexscreener.com/solana/" + tk["contract"]
                       if tk["chain"].lower() in ("sol", "solana") else "")
            reason_label = "Score too low" if tk["reject_reason"] == "score" else "MC out of range"
            lines.append(
                "• *$" + tk["symbol"] + "* — " + reason_label + "\n"
                "  Score: *" + str(tk["score"]) + "/100*  |  Rejected at MC: *" + mc_str(tk["mc_reject"]) + "*\n"
                "  24h later: *" + str(tk["outcome_x"]) + "x* → " + mc_str(tk["mc_now"])
                + ("\n  🔗 " + ds_link if ds_link else "") + "\n"
            )
        lines.append("\n_Review your filters in APEX → Settings or wait for the Sunday weekly report._")
        msg_text = "\n".join(lines)
        if len(msg_text) > 4000:
            msg_text = msg_text[:3900] + "_[truncated]_"
        try:
            await ctx.bot.send_message(
                chat_id=uid, parse_mode="Markdown",
                text=msg_text, reply_markup=main_menu_kb()
            )
            logger.info(f"Rejected outcomes sent uid={uid}: {len(pumped_tokens)} pumped tokens")
        except Exception as _rje:
            logger.debug(f"Rejected outcome DM error uid={uid}: {_rje}")


async def apex_auto_apply_suggestions_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs daily at 00:10 UTC. Auto-applies ONLY the safest self-learning change:
      apex_avoid_hours — skip UTC hours that are statistically net-losing.

    This is safe to auto-apply because it only prevents NEW entries during bad
    hours. It never touches live SL levels, trail settings, or entry thresholds.

    Everything else (confidence, score min, SL %, trail x) remains suggestion-only
    — shown in the daily report for the user to apply manually from APEX Settings.
    """
    for uid, ud in list(users.items()):
        if not ud.get("apex_mode"):
            continue
        mem = _apex_learn_memory.get(uid, [])
        if len(mem) < 10:
            continue
        recent = mem[-APEX_SELF_LEARN_WINDOW:]
        hour_pnl: dict = {}
        for t in recent:
            hr = t.get("entry_hour")
            if hr is not None:
                hour_pnl.setdefault(hr, []).append(t.get("pnl", 0))
        losing_hours = sorted([
            hr for hr, pnls in hour_pnl.items()
            if len(pnls) >= 3 and sum(pnls) < 0
        ])
        current_avoid = ud.get("apex_avoid_hours", [])
        if losing_hours != current_avoid:
            ud["apex_avoid_hours"] = losing_hours
            save_user(uid, ud)
            if losing_hours:
                logger.info(f"Auto-applied avoid_hours uid={uid}: {losing_hours}")


async def weekly_filter_analysis_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs every Sunday at 09:00 UTC.
    Analyses 7 days of sniper_daily_stats per user to:
      1. Identify filter bottlenecks (what's blocking the most tokens)
      2. Detect near-misses (tokens almost passing score/MC filters)
      3. Spot dominant hard flags (market regime signals)
      4. Generate concrete, actionable filter suggestions
      5. Feed insights back into APEX self-calibration as a loosening signal

    DMs the user a clean report with specific recommendations.
    """
    now = datetime.utcnow()
    # Only run on Sunday (UTC — Railway runs in UTC)
    if now.weekday() != 6:
        return

    for uid, ud in list(users.items()):
        if not (ud.get("apex_mode") or ud.get("sniper_auto") or ud.get("sniper_advisory")):
            continue
        dsa = ud.get("sniper_daily_stats", {})
        if not dsa:
            continue

        # ── Aggregate last 7 days ─────────────────────────────────────────────
        week_days = sorted(dsa.keys())[-7:]
        if len(week_days) < 2:
            continue   # not enough data yet

        total_tokens   = 0
        total_passed   = 0
        total_hf       = 0
        total_score    = 0
        total_mc       = 0
        total_other    = 0
        near_miss_sc   = []
        near_miss_mc   = []
        flag_types     = {}

        for day_key in week_days:
            day = dsa[day_key]
            total_tokens += day.get("total", 0)
            total_passed += day.get("passed", 0)
            total_hf     += day.get("hard_flag", 0)
            total_score  += day.get("score", 0)
            total_mc     += day.get("mc_range", 0)
            total_other  += day.get("other", 0)
            near_miss_sc.extend(day.get("near_miss_scores", []))
            near_miss_mc.extend(day.get("near_miss_mc", []))
            for flag, cnt in day.get("flag_types", {}).items():
                flag_types[flag] = flag_types.get(flag, 0) + cnt

        if total_tokens == 0:
            continue

        days_counted    = len(week_days)
        pass_rate       = round(total_passed / total_tokens * 100, 1) if total_tokens > 0 else 0
        hf_rate         = round(total_hf / total_tokens * 100, 1)
        score_rate      = round(total_score / total_tokens * 100, 1)
        mc_rate         = round(total_mc / total_tokens * 100, 1)
        daily_avg       = round(total_tokens / days_counted)
        daily_pass_avg  = round(total_passed / days_counted, 1)

        sf              = ud.get("sniper_filters", {})
        cur_min_score   = int(sf.get("min_score", 35))
        cur_max_mc      = int(sf.get("max_mc", 100_000))
        cur_min_liq     = int(sf.get("min_liq", 5_000))

        # ── Build suggestions ────────────────────────────────────────────────
        suggestions  = []
        calibrations = []   # changes to feed back into APEX calibration

        # 1. Pass rate critically low — system is too tight overall
        if pass_rate < 2.0 and total_tokens > 50:
            suggestions.append(
                "⚠️ *Only " + str(pass_rate) + "% of tokens pass filters* — "
                "APEX has almost no candidates to evaluate. Market is either very poor "
                "quality OR filters are over-tuned. See breakdowns below."
            )

        # 2. Score near-misses — lots of tokens just below threshold
        if near_miss_sc:
            avg_near_sc = round(sum(near_miss_sc) / len(near_miss_sc), 1)
            pct_near    = round(len(near_miss_sc) / max(total_score, 1) * 100)
            if pct_near >= 40 and avg_near_sc >= cur_min_score - 5:
                new_sc = max(30, cur_min_score - 3)
                suggestions.append(
                    "📉 *Score filter:* " + str(len(near_miss_sc)) + " tokens scored "
                    + str(avg_near_sc) + "/100 avg — just *" + str(round(cur_min_score - avg_near_sc, 1))
                    + " pts below* your " + str(cur_min_score) + "/100 threshold.\n"
                    "  → Consider lowering `min_score` from *" + str(cur_min_score)
                    + "* to *" + str(new_sc) + "* to capture these near-misses."
                )
                calibrations.append(("sniper_min_score_suggestion", new_sc))
            elif total_score > 0 and pct_near < 20:
                suggestions.append(
                    "✅ *Score filter:* Near-misses are rare (" + str(len(near_miss_sc))
                    + " tokens). Threshold " + str(cur_min_score) + "/100 appears well-calibrated."
                )

        # 3. MC range near-misses — tokens just above max_mc boundary
        if near_miss_mc:
            just_above = [mc for mc in near_miss_mc if mc <= cur_max_mc * 1.15]
            if len(just_above) >= 5:
                new_mc = int(cur_max_mc * 1.20)
                suggestions.append(
                    "📊 *MC filter:* " + str(len(just_above)) + " tokens sat just above your "
                    + mc_str(cur_max_mc) + " max MC cap (within 15%).\n"
                    "  → Consider raising `max_mc` from *" + mc_str(cur_max_mc)
                    + "* to *" + mc_str(new_mc) + "* to include these."
                )
                calibrations.append(("sniper_max_mc_suggestion", new_mc))

        # 4. Hard flag breakdown — identify dominant flags
        if flag_types:
            top_flags = sorted(flag_types.items(), key=lambda x: -x[1])[:4]
            flag_lines = "\n".join(
                "  • " + flag + ": *" + str(cnt) + "* times"
                for flag, cnt in top_flags
            )
            suggestions.append(
                "🚩 *Top hard flags this week:*\n" + flag_lines + "\n"
                "_These are market-level signals, not filter problems. "
                "High counts = low-quality token cycle._"
            )
            # If one flag dominates (>50% of all hard flags) — market regime note
            if top_flags and top_flags[0][1] > total_hf * 0.5:
                dominant_flag = top_flags[0][0]
                suggestions.append(
                    "📡 *Market regime:* \"" + dominant_flag + "\" accounts for *>"
                    + str(round(top_flags[0][1] / max(total_hf, 1) * 100))
                    + "%* of all rejections. This is a market pattern, not a filter issue. "
                    "No filter changes recommended until this clears."
                )

        # 5. APEX calibration feedback — if pass rate is low and score near-misses exist,
        #    suggest the self-learning engine consider loosening slightly
        if pass_rate < 3.0 and near_miss_sc and len(near_miss_sc) >= 10:
            _cur_apex_sc = ud.get("apex_learn_score_min", 45)
            if _cur_apex_sc > 43:
                suggestions.append(
                    "🧠 *APEX engine:* Low pass rate + " + str(len(near_miss_sc))
                    + " near-miss score tokens suggests APEX entry gates may be slightly over-tuned.\n"
                    "  → APEX min_score is currently *" + str(_cur_apex_sc) + "/100*. "
                    "If WR > 35% across last 20 trades, consider lowering to *"
                    + str(_cur_apex_sc - 2) + "* via the APEX Settings menu."
                )

        # 6. Healthy market confirmation
        if pass_rate >= 5.0 and not suggestions:
            suggestions.append(
                "✅ *Filters are well-calibrated.* " + str(pass_rate) + "% pass rate "
                "with " + str(daily_pass_avg) + " tokens/day reaching APEX evaluation. "
                "No changes needed."
            )

        # ── Build report message ─────────────────────────────────────────────
        if not suggestions:
            suggestions.append(
                "📊 Not enough data yet for strong recommendations. "
                "Check again next Sunday after more scan cycles."
            )

        report_lines = [
            "📊 *WEEKLY FILTER ANALYSIS*\n",
            "Period: *" + week_days[0] + "* → *" + week_days[-1] + "*  (" + str(days_counted) + " days)\n",
            "─────────────────────",
            "🔢 *FUNNEL SUMMARY*",
            "Tokens scanned: *" + str(total_tokens) + "* (~" + str(daily_avg) + "/day)",
            "Reached AI analysis: *" + str(total_passed) + "* (" + str(pass_rate) + "% pass rate)",
            "",
            "🚫 *REJECTION BREAKDOWN*",
            "Hard flags:    *" + str(total_hf) + "* (" + str(hf_rate) + "%)",
            "Score too low: *" + str(total_score) + "* (" + str(score_rate) + "%)",
            "MC out of range: *" + str(total_mc) + "* (" + str(mc_rate) + "%)",
            "Other:         *" + str(total_other) + "*",
            "",
            "📋 *CURRENT FILTERS*",
            "Min score: *" + str(cur_min_score) + "/100*  |  Max MC: *" + mc_str(cur_max_mc) + "*",
            "",
            "─────────────────────",
            "💡 *RECOMMENDATIONS*\n",
        ]
        for s in suggestions:
            report_lines.append(s + "\n")

        report_txt = "\n".join(report_lines)

        # Telegram message limit — truncate if needed
        if len(report_txt) > 4000:
            report_txt = report_txt[:3900] + "\n\n_[Report truncated — see full details in Railway logs]_"

        try:
            await ctx.bot.send_message(
                chat_id=uid,
                parse_mode="Markdown",
                text=report_txt,
                reply_markup=main_menu_kb()
            )
            logger.info(f"Weekly filter analysis sent to uid={uid}: pass_rate={pass_rate}%, suggestions={len(suggestions)}")
        except Exception as _wfa_err:
            logger.error(f"Weekly filter analysis send error uid={uid}: {_wfa_err}")


async def _dca_show_plan(q, contract: str, p: dict):
    """Show the current DCA plan being built with Add More / Confirm buttons."""
    targets = p.get("targets", [])
    sym     = p.get("symbol", "?")
    lines   = "\n".join(
        "  " + str(i+1) + ". Buy *" + money(tgt["amount"]) + "* at *" + mc_str(tgt["mc"]) + "* MC"
        for i, tgt in enumerate(sorted(targets, key=lambda x: x["mc"]))
    )
    await q.edit_message_text(
        "📉 *DCA PLAN — $" + sym + "*\n\n" + lines + "\n\n"
        "Add another target or confirm to save:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Another Target", callback_data="dca_addmore_" + contract)],
            [InlineKeyboardButton("✅ Confirm & Save",     callback_data="dca_confirm_" + contract)],
            [InlineKeyboardButton("🗑 Start Over",         callback_data="dca_" + contract)],
        ])
    )


async def sniper_scan() -> list:
    """
    3-feed scan strategy:
      1. pump.fun/coins — brand new Solana tokens at birth (minutes 1-30)
      2. dexscreener token-boosts — tokens teams paid to promote (social signal)
      3. dexscreener token-profiles — tokens with complete social profiles

    All 3 feeds are fetched in parallel for speed.
    Returns merged, deduplicated list with extra metadata attached.
    """
    results: dict = {}   # tokenAddress → item

    async def _fetch_pumpfun():
        try:
            client = await get_http()
            # frontend-api.pump.fun is deprecated — try multiple endpoints
            pf_resp = None
            for pf_url in [
                "https://frontend-api-v3.pump.fun/coins",
                "https://frontend-api.pump.fun/coins",
                "https://client-api-2-74b1891ee9f9.herokuapp.com/coins",
            ]:
                try:
                    r = await client.get(
                        pf_url,
                        params={"offset": 0, "limit": 50, "sort": "creation_time", "order": "DESC"},
                        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                        timeout=6,
                    )
                    if r.status_code == 200:
                        pf_resp = r
                        break
                except Exception:
                    continue
            if pf_resp and pf_resp.status_code == 200:
                pf_data = pf_resp.json()
                if isinstance(pf_data, list):
                    _pf_initial = 30.0
                    _pf_grad    = 55.0
                    out = {}
                    for coin in pf_data:
                        mint = coin.get("mint", "")
                        if not mint:
                            continue
                        v_sol = float(coin.get("virtual_sol_reserves", 0) or 0)
                        curve_pct = round(min(max((v_sol - _pf_initial) / (_pf_grad - _pf_initial) * 100, 0), 100), 1) if v_sol > _pf_initial else 0.0
                        graduated = bool(coin.get("raydium_pool")) or bool(coin.get("complete"))
                        if graduated:
                            curve_pct = 100.0
                        out[mint] = {
                            "tokenAddress": mint,
                            "chainId":      "solana",
                            "source":       "pumpfun",
                            "links":        [],
                            "_pf_curve":    curve_pct,
                            "_pf_replies":  int(coin.get("reply_count", 0) or 0),
                            "_pf_graduated":graduated,
                            # pump.fun returns raw token amount, not %. 
                            # Total supply is always 1,000,000,000 tokens on pump.fun
                            "_pf_dev_pct":  round(float(coin.get("creator_token_holdings", 0) or 0) / 1_000_000_000 * 100, 2),
                            "_pf_name":     coin.get("name", ""),
                            "_pf_symbol":   coin.get("symbol", ""),
                            "_pf_twitter":  coin.get("twitter", ""),
                            "_pf_telegram": coin.get("telegram", ""),
                            "_pf_website":  coin.get("website", ""),
                        }
                    return out
        except Exception as e:
            logger.warning(f"Pump.fun feed error: {e}")
        return {}

    async def _fetch_ds(url: str):
        try:
            client = await get_http()
            r = await client.get(url, headers={"Accept": "application/json"}, timeout=8)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
        except Exception as e:
            logger.warning(f"DexScreener feed error {url}: {e}")
        return []

    # ── Fetch all 3 feeds in parallel ─────────────────────────────────────────
    pf_results, ds_boosts, ds_profiles = await _asyncio.gather(
        _fetch_pumpfun(),
        _fetch_ds("https://api.dexscreener.com/token-boosts/latest/v1"),
        _fetch_ds("https://api.dexscreener.com/token-profiles/latest/v1"),
    )

    results.update(pf_results)
    logger.info(f"Pump.fun feed: {len(pf_results)} tokens")

    # ── Merge DexScreener feeds ────────────────────────────────────────────────
    for ds_list in (ds_boosts, ds_profiles):
        for item in ds_list:
            addr = item.get("tokenAddress", "")
            if not addr:
                continue
            if addr not in results:
                item["source"] = "dexscreener"
                item["_pf_curve"]     = None
                item["_pf_replies"]   = 0
                item["_pf_graduated"] = False
                item["_pf_dev_pct"]   = None
                results[addr] = item
            elif "totalAmount" in item:
                results[addr]["_boost_amount"] = float(item.get("totalAmount", 0) or 0)

    # ── Pre-filter: must have at least 1 social signal ────────────────────────
    filtered = []
    for item in results.values():
        links = item.get("links", []) or []
        has_any_social = (
            any(l.get("type","").lower() in ("twitter","x","telegram","website","web") for l in links)
            or bool(item.get("_pf_twitter"))
            or bool(item.get("_pf_telegram"))
            or bool(item.get("_pf_website"))
        )
        if has_any_social:
            filtered.append(item)

    logger.info(f"Sniper scan: {len(results)} raw → {len(filtered)} with social signals")
    return filtered



_SNIPER_CHAIN_MAP: dict = {
    "solana": "solana", "sol": "solana",
    "ethereum": "ethereum", "eth": "ethereum",
    "base": "base",
    "bsc": "bsc", "bnb": "bsc",
    "arbitrum": "arbitrum", "arb": "arbitrum",
}

def _sniper_chain_id(chain: str) -> str:
    """Normalise DexScreener chainId to sniper_chains key."""
    return _SNIPER_CHAIN_MAP.get(chain.lower(), "")


def _sniper_daily_reset(ud: dict):
    """Reset daily sniper budget if it's a new day."""
    today = datetime.now().date()
    if ud.get("sniper_daily_date") != today:
        ud["sniper_daily_spent"] = 0.0
        ud["sniper_daily_date"] = today


def _build_history_context(ud: dict, uid: int) -> str:
    """Summarise user's past sniper trades for AI context."""
    sniper_trades = [
        t for t in trade_log.get(uid, [])
        if t.get("mood") == "AI-Sniper"
    ]
    if not sniper_trades:
        return "No past sniper trades yet."
    wins   = [t for t in sniper_trades if t["realized_pnl"] > 0]
    losses = [t for t in sniper_trades if t["realized_pnl"] <= 0]
    wr     = round(len(wins) / len(sniper_trades) * 100) if sniper_trades else 0
    avg_w  = round(sum(t["realized_pnl"] for t in wins) / len(wins), 2) if wins else 0
    avg_l  = round(sum(t["realized_pnl"] for t in losses) / len(losses), 2) if losses else 0
    best_x = max((t.get("x", 0) for t in sniper_trades), default=0)
    lines  = [
        f"Past sniper trades: {len(sniper_trades)} total, {wr}% win rate",
        f"Avg win: ${avg_w}  |  Avg loss: ${avg_l}  |  Best X: {round(best_x,2)}x",
    ]
    for t in sorted(sniper_trades, key=lambda x: _safe_dt(x.get("closed_at")), reverse=True)[:5]:
        outcome = "WIN" if t["realized_pnl"] > 0 else "LOSS"
        lines.append(
            f"  {outcome} ${t['symbol']} {round(t.get('x',0),2)}x  "
            f"PnL:{round(t['realized_pnl'],2)}  Held:{t.get('hold_h',0)}h  "
            f"Mood:{t.get('mood','?')}"
        )
    return "\n".join(lines)


async def ai_analyze_token(info: dict, sc: dict, ud: dict, uid: int = 0) -> dict:
    """
    Rule-based token analysis — derives verdict from sniper_score data.
    No API key required. Falls back gracefully if Anthropic API is unavailable.
    """
    sf      = ud.get("sniper_filters", {})
    max_buy = float(sf.get("buy_amount", 100))
    bal     = ud.get("balance", 0)

    score      = sc.get("score", 0)
    flags      = sc.get("flags", [])
    strengths  = sc.get("strengths", [])
    warnings   = sc.get("warnings", [])
    flag_count = len(flags)

    symbol    = info.get("symbol", "?")
    mc        = info.get("mc", 0)
    age_h     = round(info.get("age_h") or 0, 1)
    liq       = info.get("liq", 0)
    buy_pct   = info.get("buy_pct_h1", info.get("buy_pct", 50))
    ch_h1     = info.get("ch_h1", 0)
    ch_m5     = info.get("ch_m5", 0)
    lp_burn   = info.get("lp_burn")
    no_mint   = info.get("no_mint")
    no_freeze = info.get("no_freeze")
    pf_curve  = info.get("pf_curve")
    insider   = info.get("insider_pct")
    top10     = info.get("top10_pct")
    dev_pct   = float(info.get("pf_dev_pct") or info.get("dev_pct_rc") or 0)
    vol_h1    = info.get("vol_h1", 0)
    buys_h1   = info.get("buys_h1", 0)

    # ── Verdict + Confidence ──────────────────────────────────────────────────
    # Confidence maps score to 1-10 scale.
    # Formula: (score + 15) // 10 so that score=45 → 6/10, score=55 → 7/10 etc.
    # This aligns with APEX_MIN_CONFIDENCE=6 — any SNIPE-worthy token (score≥45)
    # now meets the APEX entry gate. Old formula (score//10) mapped score=45→4,
    # meaning APEX never triggered on tokens scoring 45-59 despite them passing
    # all pre-filters and getting verdict=SNIPE.
    if flag_count >= 3 or score < 30:
        verdict    = "SKIP"
        confidence = max(1, min(3, 10 - score // 10))
    elif score >= 45 and flag_count <= 1:
        verdict    = "SNIPE"
        confidence = min(10, max(4, (score + 15) // 10))
    elif score >= 35:
        verdict    = "WAIT"
        confidence = min(6, max(3, score // 12))
    else:
        verdict    = "SKIP"
        confidence = max(1, score // 15)

    # ── Rug Risk ──────────────────────────────────────────────────────────────
    if flag_count >= 2:
        rug_risk = "HIGH"
    elif flag_count == 1:
        rug_risk = "MEDIUM"
    elif (top10 and top10 > 40) or (insider and insider > 8) or (dev_pct and dev_pct > 5):
        rug_risk = "MEDIUM"
    elif lp_burn and lp_burn >= 90 and no_mint is True and no_freeze is not False:
        rug_risk = "LOW"
    else:
        rug_risk = "MEDIUM"

    # ── Momentum ─────────────────────────────────────────────────────────────
    if buy_pct >= 65 and ch_m5 > 0 and ch_h1 > 0:
        momentum = "STRONG"
    elif buy_pct >= 55 and ch_h1 > 0:
        momentum = "MODERATE"
    elif buy_pct < 48 or ch_h1 < -10:
        momentum = "NEGATIVE"
    else:
        momentum = "WEAK"

    # ── Social Score ─────────────────────────────────────────────────────────
    soc_count = sum([bool(info.get("twitter")), bool(info.get("telegram")), bool(info.get("website"))])
    if soc_count >= 3:   social_score = "GOOD"
    elif soc_count >= 1: social_score = "PARTIAL"
    else:                social_score = "NONE"

    # ── Thesis ───────────────────────────────────────────────────────────────
    thesis_parts = []

    if verdict == "SNIPE":
        thesis_parts.append(f"${symbol} shows strong entry signals at {mc_str(mc)} MC.")
        if lp_burn and lp_burn >= 90:
            thesis_parts.append(f"LP fully burned ({lp_burn}%) — rug protection confirmed.")
        if buy_pct >= 60:
            thesis_parts.append(f"Buy pressure solid at {buy_pct}% with {buys_h1} H1 buys.")
        if pf_curve and 30 <= pf_curve <= 65:
            thesis_parts.append(f"Pump.fun curve at {pf_curve}% — sweet spot for entry.")
    elif verdict == "WAIT":
        thesis_parts.append(f"${symbol} has potential but needs confirmation.")
        if warnings:
            thesis_parts.append(warnings[0] + ".")
        if ch_m5 <= 0:
            thesis_parts.append("Wait for 5m green candle before entry.")
        else:
            thesis_parts.append(f"Monitor momentum — 1h change {ch_h1:+.1f}%.")
    else:  # SKIP
        if flags:
            thesis_parts.append(f"Skipping — {flags[0].replace('🚨 ', '')}.")
        elif score < 45:
            thesis_parts.append(f"Score too low ({score}/100) — does not meet entry criteria.")
        else:
            thesis_parts.append("Setup does not meet entry criteria.")
        if warnings:
            thesis_parts.append(warnings[0] + ".")

    thesis = " ".join(thesis_parts) or f"Score {score}/100. {sc.get('verdict', '')}."

    # ── Red / Green flags ─────────────────────────────────────────────────────
    red_flags   = [f.replace("🚨 ", "") for f in flags[:4]]
    if warnings: red_flags += [warnings[0]]

    green_flags = [s.replace("✅ ", "").replace("🔒 ", "").replace("🎯 ", "").replace("🚀 ", "").replace("⚡ ", "") for s in strengths[:4]]

    # ── Suggested entry amount ────────────────────────────────────────────────
    if verdict == "SNIPE":
        confidence_factor = confidence / 10
        suggested = round(min(max_buy * confidence_factor, max_buy, bal), 2)
    elif verdict == "WAIT":
        suggested = round(min(max_buy * 0.5, bal), 2)
    else:
        suggested = 0.0

    # ── Try real AI if key available + credits ───────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and verdict != "SKIP":
        try:
            history_ctx = _build_history_context(ud, uid)
            prompt = (
                f"Token: ${symbol} | MC: {mc_str(mc)} | Age: {age_h}h | Score: {score}/100\n"
                f"LP: {lp_burn}% burned | Mint: {'disabled' if no_mint else 'ACTIVE'} | Buy%: {buy_pct}%\n"
                f"Flags: {'; '.join(flags) or 'None'} | Strengths: {'; '.join(strengths[:3]) or 'None'}\n"
                f"Curve: {pf_curve}% | Insiders: {insider}% | Top10: {top10}%\n"
                f"Socials: TW={'yes' if info.get('twitter') else 'no'} TG={'yes' if info.get('telegram') else 'no'}\n"
                f"History: {history_ctx}\n\n"
                "Give a 2-sentence sniper verdict. Reply ONLY in JSON:\n"
                '{"verdict":"SNIPE"|"SKIP"|"WAIT","confidence":<1-10>,"thesis":"<2 sentences>",'
                f'"suggested_amount":<float max {max_buy}>,"red_flags":[],"green_flags":[],'
                '"rug_risk":"LOW"|"MEDIUM"|"HIGH","momentum":"STRONG"|"MODERATE"|"WEAK"|"NEGATIVE",'
                '"social_score":"GOOD"|"PARTIAL"|"NONE"}'
            )
            # Rate limit
            now_ts = datetime.now().timestamp()
            if not hasattr(ai_analyze_token, "_call_times"):
                ai_analyze_token._call_times = []
            ai_analyze_token._call_times = [t for t in ai_analyze_token._call_times if now_ts - t < 60]
            if len(ai_analyze_token._call_times) < 4:
                ai_analyze_token._call_times.append(now_ts)
                client = await get_http()
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"Content-Type":"application/json","x-api-key":api_key,"anthropic-version":"2023-06-01"},
                    json={"model":"claude-haiku-4-5-20251001","max_tokens":400,
                      "system":"You are a Solana memecoin sniper AI. Respond ONLY with valid JSON, no markdown.",
                      "messages":[{"role":"user","content":prompt}]},
                    timeout=12.0
                )
                if resp.status_code == 200:
                    raw  = resp.json()["content"][0]["text"].strip().replace("```json","").replace("```","")
                    result = _json.loads(raw)
                    if result.get("verdict") in ("SNIPE","SKIP","WAIT"):
                        result["suggested_amount"] = min(float(result.get("suggested_amount", suggested)), max_buy, bal)
                        return result
        except Exception as _ai_err:
            logger.debug(f"AI call skipped (using rule-based): {_ai_err}")

    return {
        "verdict":          verdict,
        "confidence":       confidence,
        "suggested_amount": suggested,
        "thesis":           thesis,
        "red_flags":        red_flags[:4],
        "green_flags":      green_flags[:4],
        "rug_risk":         rug_risk,
        "momentum":         momentum,
        "social_score":     social_score,
    }


def _compact_pill_text(info: dict, sc: dict, ai: dict) -> str:
    """Compact DM advisory pill — 6 lines, Markdown."""
    symbol     = info.get("symbol", "?")
    mc         = info.get("mc", 0)
    liq        = info.get("liq", 0)
    age_h      = info.get("age_h") or 0
    verdict    = ai.get("verdict", "WAIT")
    confidence = ai.get("confidence", 0)
    rug_risk   = ai.get("rug_risk", "MEDIUM")
    momentum   = ai.get("momentum", "MODERATE")
    thesis     = ai.get("thesis", "")
    score      = sc.get("score", 0)
    buy_pct    = info.get("buy_pct", 50)
    vol_h1     = info.get("vol_h1", 0)

    v_icon   = {"SNIPE": "🟢", "WAIT": "🟡", "SKIP": "🔴"}.get(verdict, "🟡")
    rug_icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(rug_risk, "🟡")
    conf_bar = "█" * confidence + "░" * (10 - confidence)
    age_str  = (str(round(age_h * 60)) + "m") if age_h < 1 else (str(round(age_h, 1)) + "h")

    def _mf(v):
        if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
        if v >= 1_000:     return f"${v/1_000:.1f}K"
        return f"${v:.0f}"

    return (
        f"{v_icon} *${_md(symbol)}* — {verdict}  `{conf_bar}` {confidence}/10\n"
        f"Score: *{score}/100*  |  Rug: {rug_icon} *{rug_risk}*  |  Mom: *{momentum}*\n"
        f"MC: *{_mf(mc)}*  |  Liq: *{_mf(liq)}*  |  Age: *{age_str}*\n"
        f"Buy%: *{buy_pct}%*  |  Vol1h: *{_mf(vol_h1)}*\n"
        + (f"_{_md(thesis[:140])}_\n" if thesis else "")
        + "\n_Tap View Analysis for full report_"
    )


def _ai_report_text(info: dict, sc: dict, ai: dict, contract: str = "", expanded: bool = True) -> str:
    """Full advisory report — Markdown, used for snp_view_ callback."""
    symbol      = info.get("symbol", "?")
    name        = info.get("name", symbol)
    chain       = info.get("chain", "SOL").upper()
    mc          = info.get("mc", 0)
    liq         = info.get("liq", 0)
    liq_pct     = info.get("liq_pct", 0)
    age_h       = info.get("age_h") or 0
    buy_pct     = info.get("buy_pct", 50)
    vol_h1      = info.get("vol_h1", 0)
    no_mint     = info.get("no_mint")
    lp_burn     = info.get("lp_burn")
    verdict     = ai.get("verdict", "WAIT")
    confidence  = ai.get("confidence", 0)
    rug_risk    = ai.get("rug_risk", "MEDIUM")
    momentum    = ai.get("momentum", "MODERATE")
    social_sc   = ai.get("social_score", "PARTIAL")
    thesis      = ai.get("thesis", "")
    green_flags = ai.get("green_flags", [])
    red_flags   = ai.get("red_flags", [])
    suggested   = ai.get("suggested_amount", 0)
    score       = sc.get("score", 0)
    s_icon      = sc.get("icon", "🟡")

    v_icon    = {"SNIPE": "🟢", "WAIT": "🟡", "SKIP": "🔴"}.get(verdict, "🟡")
    rug_icon  = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(rug_risk, "🟡")
    conf_bar  = "█" * confidence + "░" * (10 - confidence)
    score_bar = "█" * round(score / 10) + "░" * (10 - round(score / 10))
    age_str   = (str(round(age_h * 60)) + "m") if age_h < 1 else (str(round(age_h, 1)) + "h")
    mint_str  = "✅ Disabled" if no_mint else "⚠️ Active"
    lp_str    = ("✅ " + str(round(lp_burn)) + "% burned") if lp_burn and lp_burn > 0 else "⚠️ Not burned"

    def _mf(v):
        if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
        if v >= 1_000:     return f"${v/1_000:.1f}K"
        return f"${v:.0f}"

    gf = "\n".join(["  ✅ " + _md(f) for f in green_flags]) if green_flags else "  _None_"
    rf = "\n".join(["  ⚠️ " + _md(f) for f in red_flags])   if red_flags   else "  _None_"

    # Social velocity line (only shown when signal is non-trivial)
    _svel        = info.get("social_velocity", 0.0)
    _rv          = info.get("pf_reply_velocity", 0.0)
    _tg_spike    = info.get("tg_mention_spike", False)
    _svel_str    = ""
    if _svel >= 4 or _rv >= 2 or _tg_spike:
        _svel_bar = "🔥" if _svel >= 7 else ("📈" if _svel >= 4 else "📊")
        _svel_parts = [f"SocVel: {_svel_bar} *{_svel:.1f}/10*"]
        if _rv >= 1:   _svel_parts.append(f"Reply vel: *{_rv:.1f}/min*")
        if _tg_spike:  _svel_parts.append("*📱 TG spike*")
        _svel_str = "  |  ".join(_svel_parts) + "\n"

    return (
        f"{v_icon} *{verdict}* — *${_md(symbol)}* _{_md(name)}_\n"
        f"`{contract}`\n\n"
        f"Confidence: `{conf_bar}` *{confidence}/10*\n"
        f"{rug_icon} Rug: *{rug_risk}*  |  Momentum: *{momentum}*  |  Social: *{social_sc}*\n"
        + _svel_str +
        f"\nScore: `{score_bar}` *{score}/100* {s_icon}\n\n"
        f"MC: *{_mf(mc)}*  |  Liq: *{_mf(liq)}* ({round(liq_pct,1)}%)\n"
        f"Age: *{age_str}*  |  Buy%: *{buy_pct}%*  |  Vol1h: *{_mf(vol_h1)}*\n"
        f"Chain: *{chain}*  |  Mint: {mint_str}  |  LP: {lp_str}\n"
        + (f"\n📝 _{_md(thesis)}_\n" if thesis else "")
        + f"\n*Green Flags:*\n{gf}\n\n*Red Flags:*\n{rf}"
        + (f"\n\n💰 *Suggested Entry:* ${suggested:,.2f}" if suggested > 0 else "")
    )


def _channel_card_text(info: dict, sc: dict, ai: dict, contract: str) -> str:
    """
    APEX Signals channel card — HTML.

    Layout order:
      1. Header: APEX SIGNALS label
      2. Token name · $TICKER (full caps) · CHAIN
      3. CA (copyable)
      4. MC · Liq · Age · Buy% · Vol/h · Mint · LP   ← above score
      5. SCORE bar
      6. M·T·V Intelligence
      7. Holder Intel
      8. Socials (clickable) + X search inlink
      9. Verdict · Confidence · Rug · Momentum        ← below X search
     10. Thesis (italic)
     11. ✅ Green Flags  /  ⚠️ Red Flags
     12. 💰 Suggested Entry
     13. ─── inline DEX links: GT · DT · DS · DV · BE · PF
    """
    # ── Raw data ──────────────────────────────────────────────────────────────
    symbol_raw  = info.get("symbol", "?")
    symbol      = symbol_raw.upper()                    # fix 1 — enforce CAPS
    name        = info.get("name", symbol)
    chain       = info.get("chain", "SOL").upper()
    chain_raw   = info.get("chain", "solana").lower()
    mc          = info.get("mc", 0)
    liq         = info.get("liq", 0)
    liq_pct     = info.get("liq_pct", 0)
    age_h       = info.get("age_h") or 0
    buy_pct     = info.get("buy_pct", 50)
    vol_h1      = info.get("vol_h1", 0)
    vol_h24     = info.get("vol_h24", 0)
    vol_m5      = info.get("vol_m5", 0)
    no_mint     = info.get("no_mint")
    no_freeze   = info.get("no_freeze")
    lp_burn     = info.get("lp_burn")
    # M·T·V
    buys_m5     = info.get("buys_m5", 0)
    sells_m5    = info.get("sells_m5", 0)
    buy_pct_m5  = info.get("buy_pct_m5", 50)
    buys_h1     = info.get("buys_h1", 0)
    sells_h1    = info.get("sells_h1", 0)
    buy_pct_h1  = info.get("buy_pct_h1", 50)
    buys_h24    = info.get("buys", 0)
    sells_h24   = info.get("sells", 0)
    # Holder Intel
    top10       = info.get("top10_pct")
    top20       = info.get("top20_pct")
    dev_pct     = float(info.get("pf_dev_pct") or info.get("dev_pct_rc") or 0)
    boost_amt   = info.get("boost_amount", 0) or 0
    # AI / Score
    verdict     = ai.get("verdict", "WAIT")
    confidence  = ai.get("confidence", 0)
    rug_risk    = ai.get("rug_risk", "MEDIUM")
    momentum    = ai.get("momentum", "MODERATE")
    thesis      = ai.get("thesis", "")
    green_flags = ai.get("green_flags", [])
    red_flags   = ai.get("red_flags", [])
    suggested   = ai.get("suggested_amount", 0)
    score       = sc.get("score", 0)
    # Socials
    twitter     = info.get("twitter", "")
    telegram    = info.get("telegram", "")
    website     = info.get("website", "")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _mf(v):
        if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
        if v >= 1_000:     return f"${v/1_000:.1f}K"
        return f"${v:.0f}"

    def _bp_icon(pct):
        return "🟢" if pct >= 60 else ("🟡" if pct >= 50 else "🔴")

    def _pct_icon(val, warn, danger):
        """Green if val < warn, yellow if < danger, red otherwise."""
        if val is None: return "⚪"
        return "🟢" if val < warn else ("🟡" if val < danger else "🔴")

    v_icon    = {"SNIPE": "🟢", "WAIT": "🟡", "SKIP": "🔴"}.get(verdict, "🟡")
    rug_icon  = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(rug_risk, "🟡")
    conf_bar  = "█" * confidence + "░" * (10 - confidence)
    score_bar = "█" * round(score / 10) + "░" * (10 - round(score / 10))
    age_display = age_str(age_h)   # FIX: renamed from age_str to avoid shadowing module-level age_str()
    mint_str  = "✅ Disabled" if no_mint is True else "⚠️ Active"
    lp_str    = (f"✅ {round(lp_burn)}% burned") if lp_burn and lp_burn > 0 else "⚠️ Not burned"

    # ── MC / Stats block (fix 3 — above score) ───────────────────────────────
    stats_block = (
        f"💰 MC: <b>{_mf(mc)}</b>  ·  Liq: <b>{_mf(liq)}</b> <i>({round(liq_pct, 1)}%)</i>\n"
        f"⏱ Age: <b>{age_display}</b>  ·  Buy%: <b>{buy_pct}%</b>  ·  Vol/H: <b>{_mf(vol_h1)}</b>\n"
        f"🔐 Mint: {mint_str}  ·  LP: {lp_str}"
    )

    # ── M·T·V Intelligence block ──────────────────────────────────────────────
    mtv_block = _build_mtv_html(info)

    # ── Holder Intel block (fix 4) ────────────────────────────────────────────
    top10_icon = _pct_icon(top10, 20, 40)
    top20_icon = _pct_icon(top20, 35, 60)
    dev_icon   = _pct_icon(dev_pct, 5, 15) if dev_pct > 0 else "⚪"
    dex_paid   = "✅" if boost_amt >= 5 else "❌"
    top10_str  = f"{top10_icon} <b>{top10:.1f}%</b>" if top10 is not None else "⚪ N/A"
    top20_str  = f"{top20_icon} <b>{top20:.1f}%</b>" if top20 is not None else "⚪ N/A"
    dev_str    = f"{dev_icon} <b>{dev_pct:.2f}%</b>" if dev_pct > 0 else "⚪ N/A"
    # ── Wallet intelligence (from RugCheck topHolder tags) ────────────────────
    _sniper_cnt  = info.get("sniper_count")     or 0
    _sniper_p    = info.get("sniper_pct")       or 0.0
    _smart_cnt   = info.get("smart_wallet_count") or 0
    _smart_p     = info.get("smart_wallet_pct") or 0.0
    _fresh_p     = info.get("fresh_wallet_pct") or 0.0
    _bundle_p    = info.get("bundle_wallet_pct") or 0.0
    _insider_p   = info.get("insider_pct")      or 0.0
    _rc_avail_ch = info.get("no_mint") is not None
    # Build wallet intel line — always show at minimum the insiders field
    _wi_parts = []
    _ins_icon  = "🐭" if _insider_p == 0 else ("🟡" if _insider_p < 15 else "🔴")
    if info.get("insider_pct") is not None:
        _wi_parts.append(f"{_ins_icon} Insiders: <b>{_insider_p:.0f}%</b>")
    elif _rc_avail_ch:
        _wi_parts.append(f"🐭 Insiders: <b>0%</b>")
    else:
        _wi_parts.append(f"🐭 Insiders: <b>N/A</b>")
    if _sniper_cnt > 0:
        _snp_icon = "🎯" if _sniper_p < 10 else ("🟡" if _sniper_p < 25 else "🔴")
        _wi_parts.append(f"{_snp_icon} Snipers: <b>{_sniper_cnt}</b>")
    if _fresh_p > 0:
        _fr_icon = "🫧" if _fresh_p < 10 else ("🟡" if _fresh_p < 20 else "🔴")
        _wi_parts.append(f"{_fr_icon} Fresh: <b>{_fresh_p:.0f}%</b>")
    if _smart_cnt > 0:
        _wi_parts.append(f"💰 SmartWallets: <b>{_smart_cnt}</b>")
    if _bundle_p >= 5:
        _bdl_icon = "🟡" if _bundle_p < 15 else "🔴"
        _wi_parts.append(f"{_bdl_icon} Bundle: <b>{_bundle_p:.0f}%</b>")
    _wallet_intel_line = "  ".join(_wi_parts) if _wi_parts else ""
    holder_block = (
        f"👥 <b>Holder Intel</b>\n"
        f"Top10: {top10_str}  ·  Top20: {top20_str}\n"
        f"👤 DEV: {dev_str}  ·  ⚡ DEX PAID: {dex_paid}"
        + (f"\n{_wallet_intel_line}" if _wallet_intel_line else "")
    )

    # ── Clickable socials + X search (fix — socials are links) ───────────────
    soc_parts = []
    if twitter:  soc_parts.append(f'<a href="{twitter}">🐦 Twitter</a>')
    if telegram: soc_parts.append(f'<a href="{telegram}">💬 Telegram</a>')
    if website:  soc_parts.append(f'<a href="{website}">🌐 Website</a>')
    soc_line  = "  ·  ".join(soc_parts) if soc_parts else "<i>None</i>"
    x_search  = f'<a href="https://x.com/search?q=%24{symbol}">🔍 Search <b>${symbol}</b> on X</a>'

    # ── Verdict block (fix 2 — below X search) ───────────────────────────────
    verdict_block = (
        f"{v_icon} <b>VERDICT: {verdict}</b>  ·  Conf: <code>{conf_bar}</code> {confidence}/10\n"
        f"{rug_icon} Rug Risk: <b>{rug_risk}</b>  ·  Momentum: <b>{momentum}</b>"
    )

    # ── Flags ─────────────────────────────────────────────────────────────────
    gf = "\n".join([f"  ✅ {f}" for f in green_flags]) if green_flags else "  <i>None</i>"
    rf = "\n".join([f"  ⚠️ {f}" for f in red_flags])   if red_flags   else "  <i>None detected</i>"

    # ── DEX inline links (fix 6 — text inlinks, NOT buttons) ─────────────────
    dex_map = {"solana":"solana","ethereum":"ethereum","base":"base","bsc":"bsc","arbitrum":"arbitrum"}
    dt_map  = {"solana":"solana","ethereum":"ether","base":"base","bsc":"bnb","arbitrum":"arbitrum"}
    d_ch    = dex_map.get(chain_raw, chain_raw)
    d_dt    = dt_map.get(chain_raw, chain_raw)
    _gt  = f"https://www.geckoterminal.com/{d_ch}/pools/{contract}"
    _dt  = f"https://www.dextools.io/app/en/{d_dt}/pair-explorer/{contract}"
    _ds  = f"https://dexscreener.com/{d_ch}/{contract}"
    _dv  = f"https://www.dexview.com/{d_ch}/{contract}"
    _be  = f"https://birdeye.so/token/{contract}?chain={d_ch}"
    _pf  = f"https://pump.fun/{contract}"
    # Build inline links — PF only shown for Solana tokens
    dex_links = f'<a href="{_gt}">GT</a>  <a href="{_dt}">DT</a>  <a href="{_ds}">DS</a>  <a href="{_dv}">DV</a>  <a href="{_be}">BE</a>'
    if chain_raw in ("solana", "sol"):
        dex_links += f'  <a href="{_pf}">PF</a>'

    # ── Social velocity block (shown when signal is meaningful) ──────────────
    _svel     = info.get("social_velocity", 0.0)
    _rv       = info.get("pf_reply_velocity", 0.0)
    _tg_spike = info.get("tg_mention_spike", False)
    _svel_block = ""
    if _svel >= 3 or _rv >= 1.5 or _tg_spike:
        _sv_icon  = "🔥" if _svel >= 7 else ("📈" if _svel >= 4 else "📊")
        _sv_parts = [f"SocVel {_sv_icon} <b>{_svel:.1f}/10</b>"]
        if _rv >= 1:    _sv_parts.append(f"Reply vel: <b>{_rv:.1f}/min</b>")
        if _tg_spike:   _sv_parts.append("<b>📱 TG spike</b>")
        _svel_block = "  ·  ".join(_sv_parts) + "\n"

    # ── Assemble card ─────────────────────────────────────────────────────────
    text = (
        f"{v_icon} <b>${symbol}</b>  ·  {name}  ·  {chain}\n"
        f"<code>{contract}</code>\n\n"
        f"{'━' * 22}\n"
        f"{stats_block}\n\n"
        f"📈 SCORE: <code>{score_bar}</code> <b>{score}/100</b>\n\n"
        f"{mtv_block}\n\n"
        f"{holder_block}\n\n"
        f"{'━' * 22}\n"
        + (_svel_block if _svel_block else "")
        + f"{verdict_block}\n\n"
        f"{'━' * 22}\n"
        f"🔗 {soc_line}\n\n"
        f"{x_search}\n\n"
        f"{'─' * 22}\n{dex_links}"
    )
    return text


async def _broadcast_to_channel(bot, channel_id: int, info: dict, sc: dict, ai: dict, contract: str, uid: int = 0) -> bool:
    """Post channel card. Clean design — only 2 action buttons, DEX links are inline text."""
    try:
        text    = _channel_card_text(info, sc, ai, contract)
        chain   = info.get("chain", "solana").lower().replace(" ", "")
        symbol  = info.get("symbol", "TOKEN").upper()
        dex_ch  = {"solana":"solana","ethereum":"ethereum","base":"base","bsc":"bsc","arbitrum":"arbitrum"}.get(chain, chain)

        bot_url = ("https://t.me/" + _bot_username + "?start=" + contract) if _bot_username else None
        dex_url = "https://dexscreener.com/" + dex_ch + "/" + contract

        # fix 5 — only 2 buttons: Buy + View Token Live (no DS/GT/DXT button row)
        rows = []
        if bot_url:
            rows.append([InlineKeyboardButton("⚡ Buy on APEX Sniper", url=bot_url)])
        rows.append([InlineKeyboardButton("🔍 View Token Live ↗", url=dex_url)])

        # Prepend an invisible HTML anchor so Telegram picks it as the FIRST link
        # and renders the DexScreener banner image as the web page preview.
        # Must be FIRST — Telegram previews the first URL it finds.
        # The card already has many <a href> links (Twitter, GT, DS, etc.) so
        # appending at the end would let those be picked instead.
        # &#8203; = zero-width space — no visible text, no raw URL printed.
        # (Markdown [\u200b](url) does NOT work in HTML parse_mode — leaks as raw text.)
        header_url = info.get("header_image", "")
        _use_header = bool(header_url and (
            info.get("_header_confirmed")
            or not header_url.startswith("https://dd.dexscreener.com/ds-data/")
        ))
        if _use_header:
            text = f'<a href="{header_url}">&#8203;</a>\n' + text

        msg = await bot.send_message(
            chat_id=channel_id, text=text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
            disable_web_page_preview=not _use_header,
        )
        _ch_card_cache[contract[:32]] = {
            "channel_id": channel_id, "message_id": msg.message_id,
            "info": info, "sc": sc, "ai": ai, "contract": contract,
            "bot_url": bot_url, "dex_url": dex_url,
        }
        if uid:
            _register_channel_call(uid, contract, info, channel_id, message_id=msg.message_id)
        return True
    except Exception as e:
        logger.warning(f"Broadcast to channel {channel_id} failed: {e}")
        return False

async def get_kol_recent_buys(wallet: str, helius_key: str, last_sig: str | None = None) -> list:
    """
    Fetch recent SWAP transactions for a wallet via Helius.
    Returns list of NEW buy events since last_sig (newest-first from API).
    Each item: {mint, sol_spent, signature, timestamp}
    
    FIX: Never pass 'before' param — that fetches OLDER txns.
    Always fetch latest 20, then stop when we hit last_sig.
    """
    if not helius_key:
        return []
    try:
        client = await get_http()
        # Always fetch latest — DO NOT pass 'before' (that returns older txns)
        params: dict = {
            "api-key": helius_key,
            "limit":   "20",
            "type":    "SWAP",
        }
        url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
        r = await client.get(url, params=params, timeout=8)
        if r.status_code == 429:
            logger.info("Helius KOL rate limit — skipping this cycle")
            return []
        if r.status_code != 200:
            return []
        txns = r.json()
        if not isinstance(txns, list):
            return []

        buys = []
        for tx in txns:
            sig = tx.get("signature", "")
            if sig == last_sig:
                break   # reached last known tx
            try:
                ts        = tx.get("timestamp", 0)
                tt        = tx.get("tokenTransfers", []) or []
                nt        = tx.get("nativeTransfers", []) or []
                fee_payer = tx.get("feePayer", "")

                # Find tokens received by this wallet (buys)
                for transfer in tt:
                    if transfer.get("toUserAccount") == fee_payer:
                        mint = transfer.get("mint", "")
                        if not mint or mint in ("So11111111111111111111111111111111111111112",):
                            continue   # skip wrapped SOL
                        # SOL spent = outgoing native transfers from this wallet
                        sol_spent = sum(
                            abs(n.get("amount", 0))
                            for n in nt
                            if n.get("fromUserAccount") == fee_payer
                        ) / 1e9
                        buys.append({
                            "mint":       mint,
                            "sol_spent":  round(sol_spent, 4),
                            "signature":  sig,
                            "timestamp":  ts,
                        })
            except Exception:
                continue

        return buys

    except Exception as e:
        logger.debug(f"KOL fetch error for {wallet}: {e}")
        return []


async def kol_tracker_job(ctx: ContextTypes.DEFAULT_TYPE):
    """
    Runs every 5 minutes alongside sniper_job.
    For each user with KOL wallets set up:
      - Checks each wallet for new SWAP buys via Helius
      - If new buy found, fetches token data and sends alert
    Requires HELIUS_API_KEY.
    """
    helius_key = os.environ.get("HELIUS_API_KEY", "")
    if not helius_key:
        return   # silently skip — no key, no tracker

    active_users = [
        (uid, ud) for uid, ud in users.items()
        if ud.get("kol_wallets") and ud.get("kol_alerts_on", True) and ud.get("balance", 0) > 0
    ]
    if not active_users:
        return

    for uid, ud in active_users:
        wallets = ud.get("kol_wallets", [])
        user_sigs = _kol_last_sig.setdefault(uid, {})

        for wallet_entry in wallets:
            try:
                wallet_addr  = wallet_entry.get("address", "")
                wallet_label = wallet_entry.get("label", wallet_addr[:8] + "...")
                wallet_chain = wallet_entry.get("chain", "solana")
                if not wallet_addr or wallet_chain != "solana":
                    continue   # Helius = Solana only

                last_sig = user_sigs.get(wallet_addr)
                new_buys = await get_kol_recent_buys(wallet_addr, helius_key, last_sig)

                if not new_buys:
                    continue

                # Update last seen signature
                user_sigs[wallet_addr] = new_buys[0]["signature"]

                # ── Feature 3: Feed KOL buys into sniper score ────────
                for _kb in new_buys[:3]:
                    _km = _kb.get("mint", "")
                    if _km:
                        _kol_hot_contracts.setdefault(_km, []).append({
                            "label":     wallet_label,
                            "sol_spent": _kb.get("sol_spent", 0),
                            "ts":        _time.time(),
                        })
                        # Keep max 10 entries per contract, expire after 2h
                        _kol_hot_contracts[_km] = [
                            e for e in _kol_hot_contracts[_km]
                            if _time.time() - e["ts"] < 7200
                        ][-10:]

                # For each new buy, fetch token data and alert
                for buy in new_buys[:3]:   # max 3 alerts per wallet per cycle
                    mint = buy["mint"]
                    try:
                        info = await get_token(mint)
                        if not info:
                            # Token too new for DexScreener — alert with basic data
                            await ctx.bot.send_message(
                                chat_id=uid,
                                parse_mode="Markdown",
                                text=(
                                    "👀 *KOL WALLET APE DETECTED*\n\n"
                                    "🏷 Wallet: *" + wallet_label + "*\n"
                                    "`" + wallet_addr + "`\n\n"
                                    "🪙 Token: `" + mint + "`\n"
                                    "💰 SOL spent: *" + str(buy['sol_spent']) + " SOL*\n\n"
                                    "_Token too new for full data. Check manually._\n"
                                    "[View on Solscan](https://solscan.io/tx/" + buy['signature'] + ")"
                                ),
                                disable_web_page_preview=True
                            )
                            continue

                        sc  = sniper_score(info)
                        sol = buy["sol_spent"]
                        sol_usd = await get_sol_price()
                        usd_est = sol * sol_usd

                        # Build alert message
                        score_tag  = "🔴 WEAK" if sc["score"] < 40 else "🟡 MODERATE" if sc["score"] < 65 else "🟢 STRONG"
                        pf_line    = ""
                        if info.get("pf_curve") is not None:
                            pf_line = "🟣 Curve: *" + str(info["pf_curve"]) + "%*" + (" 🎓 GRADUATED" if info.get("pf_graduated") else "") + "\n"

                        chain = info.get("chain","solana").lower()
                        dex_chain = {"solana":"solana","ethereum":"ethereum","base":"base","bsc":"bsc","arbitrum":"arbitrum"}.get(chain, chain)
                        gt_url = "https://www.geckoterminal.com/" + dex_chain + "/pools/" + mint
                        ds_url = "https://dexscreener.com/" + dex_chain + "/" + mint

                        kb = InlineKeyboardMarkup([
                            [InlineKeyboardButton("📈 DexScreener", url=ds_url),
                             InlineKeyboardButton("🔍 GeckoTerminal", url=gt_url)],
                            [InlineKeyboardButton("⚡ Trade on APEX Sniper", callback_data="tc_" + mint)],
                        ])

                        await ctx.bot.send_message(
                            chat_id=uid,
                            parse_mode="Markdown",
                            text=(
                                "👀 *KOL APE ALERT*\n"
                                "━━━━━━━━━━━━━━━━━━\n"
                                "🏷 *" + wallet_label + "*\n"
                                "`" + wallet_addr[:20] + "...`\n\n"
                                "🪙 *$" + info["symbol"] + "*  ·  " + info.get("chain","?").upper() + "\n"
                                "`" + mint + "`\n\n"
                                "💰 Bought: *" + str(sol) + " SOL* (~$" + f"{usd_est:,.0f}" + ")\n"
                                "📊 MC: *" + mc_str(info["mc"]) + "*  ·  Age: *" + str(round(info.get("age_h",0),1)) + "h*\n"
                                "💧 Liq: *" + money(info["liq"]) + "*\n"
                                + pf_line
                                + "🧠 Score: *" + str(sc["score"]) + "/100*  " + score_tag + "\n\n"
                                + ("\n".join("  ✅ " + s for s in sc.get("strengths",[])[:3]) + "\n" if sc.get("strengths") else "")
                                + ("\n".join("  🚨 " + f for f in sc.get("flags",[])) + "\n" if sc.get("flags") else "")
                            ),
                            reply_markup=kb,
                            disable_web_page_preview=True
                        )

                    except Exception as _te:
                        logger.warning(f"KOL token alert error {mint}: {_te}")

            except Exception as _we:
                logger.warning(f"KOL wallet error {wallet_entry}: {_we}")

    # ── EXTERNAL COPY WALLET: auto-mirror buys from tracked wallets ───────────
    # Any user who has set copy_ext_wallet will have that wallet checked here.
    # When a new buy is detected, the bot auto-buys the same token using
    # the user's configured copy_ext_amount (default $50).
    ext_copy_users = [
        (uid, ud) for uid, ud in users.items()
        if ud.get("copy_ext_wallet") and not ud.get("copy_paused") and ud.get("balance", 0) > 0
    ]
    for uid, ud in ext_copy_users:
        ext_w      = ud["copy_ext_wallet"]
        wallet_addr = ext_w.get("address", "")
        wallet_label = ext_w.get("label", wallet_addr[:8] + "...")
        copy_amt   = ud.get("copy_ext_amount", 50.0)
        if not wallet_addr or copy_amt < 1:
            continue
        try:
            user_sigs = _kol_last_sig.setdefault(uid, {})
            last_sig  = user_sigs.get("__copy_ext_" + wallet_addr)
            new_buys  = await get_kol_recent_buys(wallet_addr, helius_key, last_sig)
            if not new_buys:
                continue
            user_sigs["__copy_ext_" + wallet_addr] = new_buys[0]["signature"]
            for buy in new_buys[:2]:  # max 2 auto-buys per cycle
                mint = buy["mint"]
                try:
                    # Skip if already holding or recently bought
                    if mint in ud.get("holdings", {}):
                        continue
                    if mint in ud.get("sniper_bought", []):
                        continue
                    info = await get_token(mint)
                    if not info:
                        continue
                    amt = min(copy_amt, ud.get("balance", 0))
                    if amt < 1:
                        continue
                    result = await do_buy_core(ud, uid, mint, amt, planned=True, mood="AI-Sniper")
                    if isinstance(result, tuple):
                        info2, _ = result
                        ud.setdefault("sniper_bought", []).append(mint)
                        await ctx.bot.send_message(
                            chat_id=uid,
                            parse_mode="Markdown",
                            text=(
                                "🔁 *COPY TRADE EXECUTED*\n\n"
                                "🏷 Wallet: *" + _md(wallet_label) + "*\n"
                                "🪙 *$" + _md(info2["symbol"]) + "*  ·  " + info2.get("chain","?").upper() + "\n"
                                "💰 Copied: *" + money(amt) + "*\n"
                                "MC: *" + mc_str(info2["mc"]) + "*\n\n"
                                "_Auto-mirrored from wallet buy._"
                            )
                        )
                    else:
                        logger.warning(f"Copy-ext buy failed uid={uid} mint={mint}: {result}")
                except Exception as _cbe:
                    logger.warning(f"Copy-ext trade error uid={uid} mint={mint}: {_cbe}")
        except Exception as _cwe:
            logger.warning(f"Copy-ext wallet scan error uid={uid}: {_cwe}")


# CHANNEL MILESTONE TRACKER
# After a token is broadcast to a channel, this job tracks its price and
# posts milestone updates (every integer x up to 100x) back to the channel.
# ════════════════════════════════════════════════════════════════════════════

async def channel_milestone_job(ctx: ContextTypes.DEFAULT_TYPE):
    """
    Runs every 5 minutes. For each tracked channel call, checks current MC.
    If it crossed a new milestone (2x, 5x, 10x, 20x, 50x), posts an update
    to the channel — just like the GemTools bot.
    Auto-removes calls older than 7 days.
    """
    if not _channel_calls:
        return

    now = datetime.now()
    cutoff = (now - timedelta(days=7)).isoformat()

    for uid, calls in list(_channel_calls.items()):
        for contract, call_data in list(calls.items()):
            try:
                # Auto-expire old calls
                if call_data.get("called_at", "9999") < cutoff:
                    del calls[contract]
                    continue

                ch_id       = call_data.get("channel_id")
                entry_mc    = call_data.get("entry_mc", 0)
                entry_price = call_data.get("entry_price", 0)
                symbol      = call_data.get("symbol", "?")
                called_at   = call_data.get("called_at", now.isoformat())
                milestones  = call_data.setdefault("milestones_hit", set())

                if not ch_id or entry_mc <= 0 or entry_price <= 0:
                    continue

                # Fetch current price
                info = await get_token(contract)
                if not info:
                    continue

                cur_mc    = info.get("mc", 0)
                cur_price = info.get("price", 0)
                if cur_mc <= 0 or cur_price <= 0:
                    continue

                # Calculate current multiple
                current_x = cur_mc / entry_mc

                # Check every integer milestone up to current x (cap 100x)
                _max_ms = min(int(current_x), 100)
                for milestone_x in range(2, _max_ms + 1):
                    if milestone_x in milestones:
                        continue   # already announced
                    milestones.add(milestone_x)

                    # Calculate elapsed time
                    try:
                        called_dt  = datetime.fromisoformat(called_at)
                        elapsed    = now - called_dt
                        hrs  = int(elapsed.total_seconds() // 3600)
                        mins = int((elapsed.total_seconds() % 3600) // 60)
                        elapsed_str = f"{hrs}h {mins}m" if hrs > 0 else f"{mins}m"
                    except Exception:
                        elapsed_str = "?"

                    # Build milestone message — single emoji, clean
                    ms_text = (
                        f"<b>🎯 ${symbol} — {milestone_x}x</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📊 MC: <b>{mc_str(entry_mc)}</b> → <b>{mc_str(cur_mc)}</b>\n"
                        f"⏱ Time to {milestone_x}x: <b>{elapsed_str}</b>\n"
                    )
                    chain = info.get("chain", "solana").lower()
                    dex_chain = {"solana":"solana","ethereum":"ethereum","base":"base","bsc":"bsc","arbitrum":"arbitrum"}.get(chain, chain)
                    buy_url = ("https://t.me/" + _bot_username + "?start=" + contract) if _bot_username else f"https://dexscreener.com/{dex_chain}/{contract}"
                    orig_msg_id = call_data.get("message_id", 0)
                    try:
                        await ctx.bot.send_message(
                            chat_id=ch_id,
                            text=ms_text,
                            parse_mode="HTML",
                            reply_to_message_id=orig_msg_id if orig_msg_id else None,
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("⚡ Buy on APEX Sniper", url=buy_url)],
                            ]),
                                disable_web_page_preview=True
                            )
                        logger.info(f"Milestone {milestone_x}x posted for ${symbol} to channel {ch_id}")
                    except Exception as _me:
                        logger.warning(f"Milestone post failed for {contract}: {_me}")

            except Exception as _ce:
                logger.warning(f"Milestone tracker error for {contract}: {_ce}")


def _register_channel_call(uid: int, contract: str, info: dict, channel_id: int, message_id: int = 0):
    """Record a token call to the channel so milestone_job can track it."""
    user_calls = _channel_calls.setdefault(uid, {})
    if contract not in user_calls:   # don't reset if already tracking
        user_calls[contract] = {
            "symbol":         info.get("symbol", "?"),
            "entry_mc":       info.get("mc", 0),
            "entry_price":    info.get("price", 0),
            "called_at":      datetime.now().isoformat(),
            "channel_id":     channel_id,
            "message_id":     message_id,   # original call msg — milestones reply to this
            "milestones_hit": set(),
        }
        # Cap at 500 tracked calls per user
        if len(user_calls) > 500:
            oldest_key = min(user_calls, key=lambda k: user_calls[k].get("called_at",""))
            del user_calls[oldest_key]

def _prune_sniper_log(ud: dict):
    """
    Remove SKIPPED tokens from sniper_log that are older than 10 minutes.
    Bought/SNIPE/WAIT entries are kept permanently.
    Called at the start of every sniper_job cycle.
    """
    log = ud.get("sniper_log")
    if not log:
        return
    cutoff = (datetime.now() - timedelta(minutes=10)).isoformat()
    ud["sniper_log"] = [
        entry for entry in log
        if entry.get("bought") or                          # always keep bought
           entry.get("verdict") not in ("SKIP",) or       # keep non-skip verdicts
           entry.get("timestamp", "9999") >= cutoff        # keep recent skips
    ]



def _name_similarity(a: str, b: str) -> float:
    """
    Simple character-overlap similarity between two token names.
    Strips common suffixes/prefixes (coin, token, $) and lowercases.
    Returns 0.0–1.0. >= 0.65 = likely same rug family.
    """
    import re as _re2
    def _clean(s: str) -> str:
        s = s.lower().strip()
        s = _re2.sub(r"[$\s]", "", s)
        for suffix in ("coin", "token", "inu", "ai", "sol", "pump", "fun"):
            if s.endswith(suffix) and len(s) > len(suffix) + 2:
                s = s[:-len(suffix)]
        return s
    a, b = _clean(a), _clean(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s)-1)) if len(s) >= 2 else {s}
    ba, bb = bigrams(a), bigrams(b)
    if not ba or not bb:
        return 1.0 if a in b or b in a else 0.0
    return len(ba & bb) / len(ba | bb)


def _recent_loss_names(uid: int, hours: float = 12.0) -> list:
    """
    Returns lowercased symbol names from closed APEX trades that were
    losses in the last `hours` hours. Used by the pre-filter to block
    rug-family re-entries (e.g. Lobster → Bull Lobster → LobsterCoin).
    """
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(hours=hours)
    logs   = trade_log.get(uid, [])
    return [
        t["symbol"].lower().strip()
        for t in logs
        if t.get("realized_pnl", 0) < 0
        and _safe_dt(t.get("closed_at")) >= cutoff
    ]


def _sniper_auto_kb(contract: str) -> InlineKeyboardMarkup:
    """Keyboard shown after an auto-snipe buy notification."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 View Position",  callback_data="btt_" + contract),
         InlineKeyboardButton("🔴 Sell Now",       callback_data="sts_" + contract)],
        [InlineKeyboardButton("🏠 Main Menu",      callback_data="mm")],
    ])


async def sniper_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Main AI sniper job — runs every 5 minutes."""
    if _admin_sniper_paused:
        return  # Admin has paused sniper scanning globally
    # global declaration must come before ANY assignment to these variables
    global _bot_username, _sol_bearish, _market_regime, _market_regime_cycles, _narrative_tracker, _narrative_history
    try:
        if not _bot_username:
            try:
                me = await ctx.bot.get_me()
                _bot_username = me.username or ""
            except Exception:
                pass

        active_users = [
            (uid, ud) for uid, ud in users.items()
            if (ud.get("sniper_auto") or ud.get("sniper_advisory") or ud.get("apex_mode"))
            and (
                ud.get("balance", 0) > 0       # auto/apex need balance to buy
                or ud.get("sniper_advisory")    # advisory never spends — always include
            )
        ]
        # Dump log users: anyone with a channel set — even without a trading mode on.
        # These users get every scan result pushed to their channel regardless of mode.
        dump_log_users = {
            uid: ud for uid, ud in users.items()
            if ud.get("sniper_log_channel") and ud.get("sniper_log_channel_on", True)
        }
        if not active_users and not dump_log_users:
            return

        # ── Feature 2: SOL market condition check ─────────────────────
        try:
            _sol_now = await get_sol_price()
            _sol_price_history.append((_time.time(), _sol_now))
            # Keep only last 12 entries (~1 hour of 5-min runs)
            if len(_sol_price_history) > 12:
                _sol_price_history.pop(0)
            # Bearish if SOL dropped >4% vs 30 min ago (6 entries back)
            _sol_bearish = False
            if len(_sol_price_history) >= 6:
                _sol_30m_ago = _sol_price_history[-6][1]
                if _sol_30m_ago > 0 and _sol_now < _sol_30m_ago * 0.96:
                    _sol_bearish = True
                    logger.info(f"SOL market bearish — {_sol_30m_ago:.2f} → {_sol_now:.2f} (-{round((1-_sol_now/_sol_30m_ago)*100,1)}%). APEX entries paused.")
        except Exception:
            _sol_bearish = False

        # ── #20 Market regime detection ─────────────────────────────────
        # Tracks hard-flag rate and pass rate across cycles.
        # DEAD:    >85% hard-flag rate for 3+ consecutive cycles
        # FORMING: narrative keyword spike detected
        # ACTIVE:  normal market, tokens passing filters
        try:
            _cycle_skip_counts = [ud3.get("sniper_skip_counts", {}) for _, ud3 in active_users]
            _total_hf  = sum(d.get("hard_flag", 0) for d in _cycle_skip_counts)
            _total_all = sum(sum(d.values()) for d in _cycle_skip_counts)
            _hf_rate   = (_total_hf / _total_all) if _total_all > 0 else 0.0
            if _hf_rate >= 0.85:
                _market_regime_cycles = _market_regime_cycles + 1  # increment every high-hf cycle
                if _market_regime_cycles >= 3:
                    if _market_regime != "DEAD":
                        _market_regime = "DEAD"
                        logger.info("Market regime: DEAD — hard-flag rate %.0f%% for 3+ cycles. APEX entries paused.", _hf_rate * 100)
                        for _uid3, _ud3 in active_users:
                            if _ud3.get("apex_mode") or _ud3.get("sniper_advisory"):
                                try:
                                    await ctx.bot.send_message(
                                        chat_id=_uid3, parse_mode="Markdown",
                                        text=(
                                            "\U0001f6d1 *MARKET DEAD*\n\n"
                                            "Over 85% of tokens are failing hard flags (rugs, bundles, honeypots).\n"
                                            "APEX entries are paused until the market recovers.\n\n"
                                            "_Advisory signals will resume automatically._"
                                        )
                                    )
                                except Exception:
                                    pass
            else:
                if _market_regime == "DEAD":
                    _market_regime = "ACTIVE"
                    _market_regime_cycles = 0
                    logger.info("Market regime: RECOVERED from DEAD state.")
                elif _market_regime != "FORMING":
                    _market_regime = "ACTIVE"
                    _market_regime_cycles = 0
        except Exception as _re:
            logger.debug(f"Market regime detection error: {_re}")

        raw_tokens = await sniper_scan()
        if not raw_tokens:
            logger.warning("sniper_job: all 3 feeds (pump.fun + DexScreener x2) returned 0 tokens — API may be down or rate-limited. Skipping cycle.")
            return

        # ── Deduplicate raw feed ──────────────────────────────────────────────
        seen_this_run: set = set()
        unique_items: list = []
        for item in raw_tokens:
            c = item.get("tokenAddress", "")
            if c and c not in seen_this_run:
                seen_this_run.add(c)
                unique_items.append(item)

        # ── Pre-fetch all token data in parallel (hits DexScreener + RugCheck) ─
        await _asyncio.gather(*[get_token(c["tokenAddress"], force=True) for c in unique_items], return_exceptions=True)

        # ── #21 Narrative keyword tracker ────────────────────────────────
        # Count repeated keywords in token names this cycle.
        # If 5+ tokens share a keyword → active narrative detected.
        # APEX lowers score threshold 5pts for that narrative cluster.
        try:
            _kw_counts: dict = {}
            # Expanded stop words — filters generic words, articles, verbs,
            # pump.fun description words that appear in almost every token name
            _STOP_WORDS = {
                # Articles / prepositions / conjunctions
                "the","and","for","with","on","of","in","to","a","an","is","it",
                "at","by","be","as","or","but","not","are","was","has","have",
                # Common pump.fun description words (appear in token names/descriptions)
                "token","coin","inu","sol","made","make","just","this","that",
                "eve","ever","new","now","buy","sell","get","got","one","two",
                "big","top","hot","fun","run","did","day","way","let","old",
                "all","our","you","they","will","can","more","good","best","real",
                "world","time","free","life","play","love","dog","cat","man",
                "moon","sun","star","fire","sky","god","war","win","lost","boom",
                "read","readd","readdd","article","post","news","update","base",
                "pump","dump","gem","dex","nft","dao","lol","yolo","fomo","ngmi",
                "based","gm","gn","wen","ser","fren","chad","alpha","beta","gamma",
                "club","gang","team","game","meme","send","sent","ads","pay",
            }
            for _ti in unique_items:
                _sym = ((_ti.get("baseToken", {}) or {}).get("symbol", "") or _ti.get("_pf_symbol", "") or "").lower()
                _nm  = ((_ti.get("baseToken", {}) or {}).get("name", "") or _ti.get("_pf_name", "") or "").lower()
                for _word in (_sym + " " + _nm).split():
                    _w = _word.strip("$").strip()
                    if len(_w) >= 3 and _w not in _STOP_WORDS and _w.isalpha():
                        _kw_counts[_w] = _kw_counts.get(_w, 0) + 1
            _narrative_tracker = _kw_counts
            _active_narratives = [kw for kw, cnt in _kw_counts.items() if cnt >= 5]
            if _active_narratives:
                _market_regime = "FORMING"
                _ts_now = _time.time()
                for _nkw in _active_narratives:
                    # Only alert if this narrative is new (not seen in last 30 min)
                    _recent_nkws = [x["kw"] for x in _narrative_history if _ts_now - x["ts"] < 1800]
                    if _nkw not in _recent_nkws:
                        _narrative_history.append({"kw": _nkw, "count": _kw_counts[_nkw], "ts": _ts_now})
                        if len(_narrative_history) > 50:
                            _narrative_history = _narrative_history[-50:]
                        logger.info(f"Narrative detected: {_nkw} ({_kw_counts[_nkw]} tokens this cycle)")
                        # Narratives are logged to history only — no DM spam.
                        # Users can view them in the Narrative History tab.
        except Exception as _ne:
            logger.debug(f"Narrative tracker error: {_ne}")

        # ── Helius API key (optional) ─────────────────────────────────────────
        helius_key = os.environ.get("HELIUS_API_KEY", "")

        # ── Expire stale sniper_seen entries + prune old skips from log ─────
        now_ts = datetime.now().timestamp()
        expiry_secs = SNIPER_SEEN_EXPIRY_H * 3600
        for uid, ud in active_users:
            # Expire seen memory
            seen_map = ud.get("sniper_seen", {})
            stale = [k for k, ts in seen_map.items() if now_ts - ts > expiry_secs]
            for k in stale:
                del seen_map[k]
            # Auto-delete skipped tokens from log after 10 min
            _prune_sniper_log(ud)

        # ════════════════════════════════════════════════════════════════════
        # MAIN LOOP — for each token × each user (properly nested)
        # ════════════════════════════════════════════════════════════════════
        # Build active uid set once before the loop — not per-token
        _active_uids_set = {_au for _au, _ in active_users}
        for item in unique_items:
            contract = item.get("tokenAddress", "")
            if not contract:
                continue

            try:
                info = await get_token(contract)
                if not info:
                    continue

                # ── Enrich info with pump.fun fields from scan metadata ────
                info["pf_curve"]     = item.get("_pf_curve")
                info["pf_replies"]   = item.get("_pf_replies", 0)
                info["pf_graduated"] = item.get("_pf_graduated", False)
                info["pf_dev_pct"]   = item.get("_pf_dev_pct")
                info["boost_amount"] = item.get("_boost_amount", 0)
                # Override socials from pump.fun if DexScreener has none
                if not info.get("twitter")  and item.get("_pf_twitter"):
                    info["twitter"]  = item["_pf_twitter"]
                if not info.get("telegram") and item.get("_pf_telegram"):
                    info["telegram"] = item["_pf_telegram"]
                if not info.get("website")  and item.get("_pf_website"):
                    info["website"]  = item["_pf_website"]

                token_chain = _sniper_chain_id(info.get("chain", ""))

                # ── Feature 1: Velocity — buy% acceleration/deceleration ──
                _cur_bp = info.get("buy_pct_h1", info.get("buy_pct", 50))
                _prev_bp = _buy_pct_prev.get(contract)
                if _prev_bp is not None:
                    info["buy_pct_velocity"] = round(_cur_bp - _prev_bp, 1)
                else:
                    info["buy_pct_velocity"] = 0.0
                _buy_pct_prev[contract] = _cur_bp
                # Expire old velocity entries (keep cache under 2000)
                if len(_buy_pct_prev) > 2000:
                    _stale = list(_buy_pct_prev.keys())[:200]
                    for _k in _stale: _buy_pct_prev.pop(_k, None)

                # ── Feature 2: Reply velocity — pump.fun community acceleration ─
                # Tracks how fast pump.fun reply_count is growing. A token going
                # from 10 to 200 replies in 10 minutes is a far stronger signal
                # than one that has had 200 replies for 3 hours.
                _cur_replies = info.get("pf_replies", 0) or 0
                _prev_reply_entry = _pf_reply_prev.get(contract)
                if _prev_reply_entry:
                    _prev_count, _prev_ts = _prev_reply_entry
                    _reply_elapsed_min = max(0.5, (_time.time() - _prev_ts) / 60.0)
                    _reply_delta = max(0, _cur_replies - _prev_count)
                    info["pf_reply_velocity"] = round(_reply_delta / _reply_elapsed_min, 2)
                else:
                    info["pf_reply_velocity"] = 0.0
                _pf_reply_prev[contract] = (_cur_replies, _time.time())
                if len(_pf_reply_prev) > 2000:
                    _stale2 = list(_pf_reply_prev.keys())[:200]
                    for _k2 in _stale2: _pf_reply_prev.pop(_k2, None)

                # ── Feature 4: Social velocity composite score ─────────────────
                # Combines reply velocity, buy_pct_velocity and KOL signal
                # into a 0-10 composite social_velocity score that sniper_score()
                # uses in Category 11. No external API needed.
                _svel = 0.0
                _rv = info.get("pf_reply_velocity", 0.0)
                _bv = info.get("buy_pct_velocity", 0.0)
                _kol = info.get("kol_buy_count", 0)
                _boosts = info.get("boost_amount", 0) or 0
                # reply velocity component (0-4)
                if _rv >= 10:   _svel += 4.0
                elif _rv >= 4:  _svel += 2.5
                elif _rv >= 1:  _svel += 1.0
                # buy_pct acceleration component (0-3)
                if _bv >= 12:   _svel += 3.0
                elif _bv >= 6:  _svel += 2.0
                elif _bv >= 2:  _svel += 1.0
                elif _bv <= -8: _svel -= 1.5
                # KOL component (0-2)
                if _kol >= 2:   _svel += 2.0
                elif _kol == 1: _svel += 1.0
                # DS boost component (0-1)
                if _boosts > 0: _svel += min(1.0, _boosts / 200.0)
                info["social_velocity"] = round(min(10.0, max(0.0, _svel)), 2)

                # ── Feature 5: TG mention spike tracker ───────────────────────
                # Lightweight in-memory tracker: records every time a contract
                # appears in our scan feed and flags a 3x+ count spike within
                # 10 minutes as a cross-platform coordination signal.
                # (No Telegram API key needed — uses our existing pump.fun feed)
                _smc = _social_mention_cache.setdefault(contract, {"count": 0, "window_start": _time.time(), "spike": False})
                _smc["count"] += 1
                _window_age = _time.time() - _smc["window_start"]
                if _window_age > 600:   # reset 10-min window
                    _smc["prev_count"]   = _smc["count"]
                    _smc["count"]        = 1
                    _smc["window_start"] = _time.time()
                    _smc["spike"]        = False
                elif _smc["count"] >= 3 * max(1, _smc.get("prev_count", 1)):
                    _smc["spike"] = True   # 3x+ repeat appearances = cross-scanner spike
                info["tg_mention_spike"] = _smc.get("spike", False)
                # Expire old entries
                if len(_social_mention_cache) > 3000:
                    _oldest3 = sorted(_social_mention_cache.keys())[:300]
                    for _k3 in _oldest3: _social_mention_cache.pop(_k3, None)
                _kol_hits = [
                    e for e in _kol_hot_contracts.get(contract, [])
                    if _time.time() - e["ts"] < 3600  # only last 1h
                ]
                info["kol_buy_count"]  = len(_kol_hits)
                info["kol_sol_total"]  = round(sum(e["sol_spent"] for e in _kol_hits), 2)
                info["kol_labels"]     = list({e["label"] for e in _kol_hits})

                sc = sniper_score(info)

            except Exception as e:
                logger.warning(f"sniper_job token fetch failed {contract}: {e}")
                continue

            # ── Dump log for channel-only users ──────────────────────────────
            # Push this token's scan result to any user who has a dump log channel
            # but is NOT in active_users (no trading mode on). Active users get
            # their push inside the per-user loop below where skip_reason is known.
            for _dlu_id, _dlu_ud in dump_log_users.items():
                if _dlu_id in _active_uids_set:
                    continue  # handled in per-user loop
                _dlu_ch = _dlu_ud.get("sniper_log_channel")
                if not _dlu_ch:
                    continue
                try:
                    _dlu_chain = info.get("chain", "?").upper()[:3]
                    _dlu_mc    = mc_str(info.get("mc", 0))
                    _dlu_sc    = sc.get("score", 0)
                    _dlu_flags = sc.get("flags", [])
                    _dlu_rc    = " ⚠️RC" if info.get("rc_rate_limited") else ""
                    _dlu_link  = "https://dexscreener.com/solana/" + contract if info.get("chain","").lower() in ("sol","solana") else ""
                    if _dlu_flags:
                        _dlu_reason = "Hard flag: " + _dlu_flags[0]
                        _dlu_icon   = "🔴"
                    elif _dlu_sc < int(_dlu_ud.get("sniper_filters", {}).get("min_score", 35)):
                        _dlu_reason = f"Score too low ({_dlu_sc}/100)"
                        _dlu_icon   = "🔴"
                    else:
                        _dlu_reason = f"Score {_dlu_sc}/100 — passed pre-filter"
                        _dlu_icon   = "🟡"
                    _dlu_text = (
                        _dlu_icon + " $" + info.get("symbol", "?") + "  " + _dlu_chain
                        + "  " + str(_dlu_sc) + "/100  " + _dlu_mc + _dlu_rc + "\n"
                        + "└ " + _dlu_reason
                        + ("\n🔗 " + _dlu_link if _dlu_link else "")
                    )
                    _dl_queue.setdefault(_dlu_id, _collections.deque()).append({
                        "chat_id": int(_dlu_ch),
                        "text": _dlu_text,
                        "disable_notification": False,
                    })
                except Exception as _dlu_err:
                    logger.debug(f"Dump log push error (uid={_dlu_id}): {_dlu_err}")

            # ── Per-user evaluation ───────────────────────────────────────
            for uid, ud in active_users:
                try:
                    sf     = ud.get("sniper_filters", {})
                    chains = ud.get("sniper_chains", {})

                    # Chain filter
                    if token_chain and not chains.get(token_chain, False):
                        continue

                    # ── Dedup: sniper_seen is the source of truth ─────────
                    seen_map      = ud.setdefault("sniper_seen", {})
                    sniper_bought = ud.setdefault("sniper_bought", [])
                    if contract in seen_map or contract in sniper_bought:
                        # Log seen tokens to dump channel silently (no message, just count)
                        # Do NOT send a message per seen token — 31K+ per cycle is spam.
                        # The skip_counts hard_flag bucket already covers these.
                        continue
                    # NOTE: seen_map is written AFTER signal is sent (below),
                    # not here. Writing it before filters caused all filtered
                    # tokens to be locked out for 24h, silencing the sniper.

                    # ── Trim seen map if oversized ────────────────────────
                    if len(seen_map) > 2000:
                        oldest = sorted(seen_map.items(), key=lambda x: x[1])[:200]
                        for k, _ in oldest:
                            del seen_map[k]

                    # ── Pre-filters ───────────────────────────────────────
                    skip_reason  = None
                    age_h        = info.get("age_h") or 0
                    buys_h1      = info.get("buys_h1", 0)
                    sells_h1     = info.get("sells_h1", 0)
                    buy_pct_h1   = info.get("buy_pct_h1", info.get("buy_pct", 50))
                    vol_h1       = info.get("vol_h1", 0)
                    mc           = info.get("mc", 1)
                    liq          = info.get("liq", 0)
                    is_pumpfun   = info.get("pf_curve") is not None
                    pf_curve_val = info.get("pf_curve") or 0
                    vol_mc_ratio = (vol_h1 / mc) if mc > 0 else 0
                    maker_count  = info.get("maker_count") or 0

                    # ── Use saved filter settings directly ──
                    eff_min_score   = int(sf.get("min_score",   35))
                    # Narrative bonus: lower score threshold 5pts if token
                    # matches an active narrative keyword detected this cycle
                    if _narrative_tracker:
                        _tok_sym = info.get("symbol", "").lower()
                        _tok_nm  = info.get("name", "").lower()
                        _tok_words = set((_tok_sym + " " + _tok_nm).split())
                        _active_kws = {kw for kw, cnt in _narrative_tracker.items() if cnt >= 5}
                        if _tok_words & _active_kws:
                            eff_min_score = max(25, eff_min_score - 5)
                    eff_min_liq     = float(sf.get("min_liq",    5_000))
                    eff_min_mc      = float(sf.get("min_mc",    10_000))
                    eff_max_mc      = float(sf.get("max_mc",   100_000))   # tightened from 200K
                    eff_max_age     = float(sf.get("max_age_h",   72.0))
                    eff_min_buys    = int(sf.get("min_buys_h1",    10))
                    eff_min_buy_pct = int(sf.get("min_buy_pct",    50))
                    eff_max_vol_mc  = float(sf.get("max_vol_mc_ratio", 10.0))

                    # 0. Rug-family name block — catches serial rug token families
                    # e.g. Lobster → Bull Lobster → LobsterCoin → LOBCOIN all from
                    # same deployer team. Checks new token name against recent losses.
                    # Threshold 0.65 catches "Bull Lobster"/"LobsterCoin" without
                    # false-positives on common words like "moon", "dog", "sol".
                    _new_sym    = info.get("symbol", "")
                    _loss_names = _recent_loss_names(uid)
                    _name_match = next(
                        (ln for ln in _loss_names
                         if _name_similarity(_new_sym, ln) >= 0.65
                         # Short-name fallback: strip suffixes first, then check if the
                         # cleaned prefix appears inside a recent loss name.
                         # "LOBCOIN" → strip "coin" → "LOB" → found in "lobster" ✓
                         or (
                             len(_new_sym) >= 3
                             and (lambda p: len(p) >= 3 and p in ln.replace("coin","").replace("token",""))(
                                 _new_sym.lower().replace("coin","").replace("token","")[:5]
                             )
                         )
                        ),
                        None
                    )
                    if _name_match:
                        skip_reason = (
                            f"Rug-family block: '${_new_sym}' matches "
                            f"recent loss '${_name_match.upper()}'"
                        )

                    # 1. Hard flags = instant skip, no exceptions
                    elif sc.get("flags"):
                        skip_reason = "Hard flag: " + sc["flags"][0]

                    # 2. Score threshold
                    elif sc["score"] < eff_min_score:
                        skip_reason = f"Score too low ({sc['score']}/100 < {eff_min_score})"

                    # 3. Liquidity
                    elif is_pumpfun and liq < 5_000 and pf_curve_val < 10:
                        skip_reason = f"Pump.fun too early — liq ${liq:,.0f} / curve {pf_curve_val}%"
                    elif not is_pumpfun and liq < eff_min_liq:
                        skip_reason = f"Liq too low (${liq:,.0f})"

                    # 4. MC range
                    elif not (eff_min_mc <= mc <= eff_max_mc):
                        skip_reason = f"MC out of range ({mc_str(mc)})"

                    # 5. Age
                    elif age_h > eff_max_age:
                        skip_reason = f"Too old ({round(age_h,1)}h)"

                    # 6. Activity
                    elif buys_h1 < eff_min_buys and (not is_pumpfun or buys_h1 < 20):
                        skip_reason = f"Low activity H1 ({buys_h1} buys)"

                    # 7. Buy pressure
                    elif buy_pct_h1 < eff_min_buy_pct:
                        skip_reason = f"Sell pressure H1 ({buy_pct_h1}% buys)"

                    # 8. Wash trading
                    elif vol_mc_ratio > eff_max_vol_mc:
                        skip_reason = f"Wash trade signal (vol/MC={round(vol_mc_ratio,1)}x)"

                    # 9. No socials — soft penalty (−10 score) instead of hard skip
                    #    Pump.fun tokens and early launches often lack socials on DexScreener.
                    #    Hard-skipping them was the #1 cause of 183 skipped / 0 advisory signals.
                    _no_socials = not info.get("twitter") and not info.get("telegram")
                    if _no_socials:
                        sc["score"] = max(0, sc.get("score", 0) - 5)
                        sc.setdefault("red_flags", []).append("No socials (−10 score)")
                        # Re-check score threshold after penalty
                        if sc["score"] < eff_min_score:
                            skip_reason = f"Score too low after no-socials penalty ({sc['score']}/100)"

                    # 10. Too few unique holders — only applies to tokens >30min old
                    # New tokens naturally have few holders right after launch
                    if not skip_reason and maker_count > 0 and maker_count < 25 and age_h > 0.5:
                        skip_reason = f"Too few holders ({maker_count})"

                    # 11. RugCheck rate-limited — security data unavailable ────────
                    # When RugCheck returned 429 with no cache, mint authority, LP burn,
                    # holder concentration and rug risk flags are all None/empty.
                    # Mitigation: require a score 15pts higher than normal so the token
                    # must have very strong on-chain signals to compensate.
                    # BYPASS for advisory-only mode: the user reviews the full card
                    # themselves and can see the ⚠️RC badge — no need to penalise.
                    _advisory_only = ud.get("sniper_advisory") and not ud.get("sniper_auto") and not ud.get("apex_mode")
                    if not skip_reason and info.get("rc_rate_limited") and not _advisory_only:
                        _rc_min_score = eff_min_score + 15
                        if sc["score"] < _rc_min_score:
                            skip_reason = (
                                f"RugCheck unavailable — score {sc['score']}/100 "
                                f"below raised threshold {_rc_min_score} (normal: {eff_min_score})"
                            )

                    # Log skips briefly (just symbol + reason, no AI call)
                    if skip_reason:
                        log = ud.setdefault("sniper_log", [])
                        log.append({
                            "contract":  contract,
                            "symbol":    info["symbol"],
                            "chain":     info.get("chain", "?"),
                            "mc":        info["mc"],
                            "liq":       info.get("liq", 0),
                            "score":     sc["score"],
                            "verdict":   "SKIP",
                            "confidence":0,
                            "rug_risk":  "UNKNOWN",
                            "momentum":  "UNKNOWN",
                            "social":    "UNKNOWN",
                            "thesis":    skip_reason,
                            "red_flags": [skip_reason],
                            "green_flags":[],
                            "hard_flags": sc.get("flags", []),
                            "timestamp": datetime.now().isoformat(),
                            "bought":    False,
                            "skip_stage":"pre-filter",
                            "rc_rate_limited": info.get("rc_rate_limited", False),
                        })
                        # ── Option 3: Skip reason counter ─────────────────
                        # Bucket the reason into a short category key for display
                        _reason_key = (
                            "hard_flag"    if "Hard flag"    in skip_reason else
                            "score"        if "Score"        in skip_reason else
                            "liquidity"    if "Liq"          in skip_reason else
                            "mc_range"     if "MC out"       in skip_reason else
                            "age"          if "old"          in skip_reason else
                            "low_activity" if "activity"     in skip_reason else
                            "sell_pressure"if "Sell pressure"in skip_reason else
                            "wash_trade"   if "Wash"         in skip_reason else
                            "no_socials"   if "socials"      in skip_reason else
                            "few_holders"  if "holders"      in skip_reason else
                            "other"
                        )
                        skip_counts = ud.setdefault("sniper_skip_counts", {})
                        skip_counts[_reason_key] = skip_counts.get(_reason_key, 0) + 1
                        # ── Daily stats accumulator for weekly filter analysis ──
                        # Stores lightweight per-day stats so the weekly report can
                        # analyse 7 days of rejection patterns and suggest filter tweaks.
                        _dsa_date = datetime.now().strftime("%Y-%m-%d")
                        _dsa = ud.setdefault("sniper_daily_stats", {})
                        _day = _dsa.setdefault(_dsa_date, {
                            "total": 0, "hard_flag": 0, "score": 0,
                            "mc_range": 0, "other": 0,
                            "near_miss_scores": [],    # scores 40-49 (just below threshold)
                            "near_miss_mc": [],        # MCs within 20% of max_mc boundary
                            "flag_types": {},          # {flag_name: count}
                            "passed": 0,               # tokens that reached AI analysis
                        })
                        _day["total"] += 1
                        _day[_reason_key] = _day.get(_reason_key, 0) + 1
                        # Near-miss: score was close to threshold
                        _sc_val = sc.get("score", 0)
                        _min_sc = int(ud.get("sniper_filters", {}).get("min_score", 35))
                        if _reason_key == "score" and _sc_val >= _min_sc - 8:
                            _day["near_miss_scores"].append(_sc_val)
                            if len(_day["near_miss_scores"]) > 50:
                                _day["near_miss_scores"] = _day["near_miss_scores"][-50:]
                        # Near-miss: MC just above max_mc boundary
                        _mc_val = info.get("mc", 0)
                        _max_mc = float(ud.get("sniper_filters", {}).get("max_mc", 100_000))
                        if _reason_key == "mc_range" and _mc_val > 0 and _mc_val <= _max_mc * 1.25:
                            _day["near_miss_mc"].append(round(_mc_val))
                            if len(_day["near_miss_mc"]) > 50:
                                _day["near_miss_mc"] = _day["near_miss_mc"][-50:]
                        # Flag type breakdown
                        if _reason_key == "hard_flag" and sc.get("flags"):
                            _flag_name = sc["flags"][0][:40]
                            _day["flag_types"][_flag_name] = _day["flag_types"].get(_flag_name, 0) + 1
                        # Keep only last 8 days to avoid Supabase bloat
                        if len(_dsa) > 8:
                            _oldest_day = sorted(_dsa.keys())[0]
                            del _dsa[_oldest_day]
                        # ── Rejected token outcome tracker ─────────────────────
                        # Store borderline rejections (score/MC boundary only —
                        # hard flags are correct rejections, not worth tracking)
                        # so we can check 24h later whether they pumped.
                        import time as _rjt_inner
                        if _reason_key in ("score", "mc_range") and contract:
                            _rj_map = _apex_rejected.setdefault(uid, {})
                            if len(_rj_map) >= 50:  # cap to avoid memory growth
                                _oldest_rj = min(_rj_map.items(), key=lambda x: x[1].get("reject_ts", 0))
                                del _rj_map[_oldest_rj[0]]
                            _rj_map[contract] = {
                                "symbol":        info.get("symbol", "?"),
                                "mc_at_reject":  info.get("mc", 0),
                                "reject_reason": _reason_key,
                                "reject_ts":     _rjt_inner.time(),
                                "score":         sc.get("score", 0),
                                "chain":         info.get("chain", "sol"),
                                "checked_24h":   False,
                                "outcome_x_24h": None,
                            }
                            # Snapshot to ud so autosave persists it across restarts
                            ud["_persisted_rejected"] = _rj_map
                        # Trim log
                        if len(log) > SNIPER_LOG_MAX:
                            ud["sniper_log"] = log[-SNIPER_LOG_MAX:]
                        # ── Dump log channel: queue compact line for every skip ─
                        _dl_ch_skip = ud.get("sniper_log_channel")
                        if _dl_ch_skip and ud.get("sniper_log_channel_on", True):
                            _rc_badge  = " ⚠️RC" if info.get("rc_rate_limited") else ""
                            _dl_chain  = info.get("chain", "?").upper()[:3]
                            _dl_mc     = mc_str(info.get("mc", 0))
                            _dl_sc     = sc.get("score", 0)
                            _dl_reason = skip_reason[:80]
                            _ds_link   = "https://dexscreener.com/solana/" + contract if info.get("chain","").lower() in ("sol","solana") else ""
                            # ── Extra fields ──────────────────────────────────
                            _dl_age    = info.get("age_h") or 0
                            _dl_age_s  = (str(round(_dl_age, 1)) + "h") if _dl_age < 48 else (str(round(_dl_age/24, 1)) + "d")
                            _dl_ath_mc = info.get("ath_mc", 0)
                            _dl_cur_mc = info.get("mc", 0)
                            _dl_ath_s  = ("ATH " + mc_str(_dl_ath_mc)) if _dl_ath_mc and _dl_ath_mc > _dl_cur_mc else ""
                            _dl_top10  = info.get("top10_pct")
                            _dl_top10_s = ("Top10: " + str(round(_dl_top10)) + "%") if _dl_top10 is not None else ""
                            _dl_insider = info.get("insider_pct")
                            _dl_bundle_s = ("Bundle: " + str(round(_dl_insider)) + "%") if _dl_insider is not None else ""
                            _dl_lp     = info.get("lp_burn")
                            _dl_lp_s   = ("LP: " + str(round(_dl_lp)) + "%") if _dl_lp is not None else ""
                            # Build compact details line — only show non-empty fields
                            # Smart wallet / sniper intel
                            _dl_smart   = info.get("smart_wallet_count") or 0
                            _dl_smart_s = ("Smart: " + str(_dl_smart)) if _dl_smart else ""
                            _dl_sniper_c = info.get("sniper_count") or 0
                            _dl_snp_s   = ("Snipers: " + str(_dl_sniper_c)) if _dl_sniper_c else ""
                            _dl_details = "  ".join(x for x in [_dl_age_s, _dl_ath_s, _dl_top10_s, _dl_bundle_s, _dl_lp_s, _dl_smart_s, _dl_snp_s] if x)
                            _dl_text   = (
                                "🔴 SKIP  $" + info.get("symbol","?") + "  " + _dl_chain
                                + "  " + str(_dl_sc) + "/100  " + _dl_mc + _rc_badge + "\n"
                                + ("📋 " + _dl_details + "\n" if _dl_details else "")
                                + "└ " + _dl_reason
                                + ("\n🔗 " + _ds_link if _ds_link else "")
                            )
                            _dl_queue.setdefault(uid, _collections.deque()).append({
                                "chat_id": int(_dl_ch_skip),
                                "text": _dl_text,
                                "disable_notification": False,
                            })
                        # ── Mark skipped token as seen ────────────────────────
                        # Suppress re-scanning based on WHY it was skipped.
                        # Hard flags are on-chain facts that don't change in minutes.
                        # Score-borderline tokens can improve as buy pressure builds.
                        # TTL is stored as an offset so the existing expiry cleanup works.
                        _expiry_secs = SNIPER_SEEN_EXPIRY_H * 3600
                        if _reason_key == "score":
                            _skip_ttl = 300           # 5 min — score can improve quickly with momentum
                        elif _reason_key == "mc_range":
                            _skip_ttl = 900           # 15 min — MC can drift back in range
                        else:
                            _skip_ttl = 600           # 10 min — reduced from 30min: pump.fun recycles same
                                                      # tokens repeatedly, 30min was filling seen_map and
                                                      # blocking the entire feed causing zero advisory pings
                        seen_map[contract] = _time.time() - _expiry_secs + _skip_ttl
                        continue

                    # ── Daily budget check (advisory-only users never spend) ──
                    _sniper_daily_reset(ud)
                    budget  = ud.get("sniper_daily_budget", 500.0)
                    spent   = ud.get("sniper_daily_spent", 0.0)
                    max_buy = float(sf.get("buy_amount", 100))
                    # _advisory_only already set above (RC rate-limit section)
                    if not _advisory_only and spent + max_buy > budget:
                        logger.info(f"Sniper daily budget hit for {uid}")
                        continue
                    # Count tokens that passed all pre-filters in daily stats
                    _dsa2_date = datetime.now().strftime("%Y-%m-%d")
                    _dsa2 = ud.setdefault("sniper_daily_stats", {})
                    _dsa2.setdefault(_dsa2_date, {
                        "total": 0, "hard_flag": 0, "score": 0,
                        "mc_range": 0, "other": 0,
                        "near_miss_scores": [], "near_miss_mc": [],
                        "flag_types": {}, "passed": 0,
                    })["passed"] = _dsa2.get(_dsa2_date, {}).get("passed", 0) + 1

                    # ── Helius enrichment (optional, Solana only) ─────────
                    if helius_key and info.get("chain","").lower() in ("solana","sol"):
                        try:
                            helius_data = await get_helius_maker_pct(contract, helius_key)
                            if helius_data:
                                info["maker_pct"]    = helius_data.get("maker_pct")
                                info["maker_count"]  = helius_data.get("maker_count")
                                info["top3_vol_pct"] = helius_data.get("top3_vol_pct")
                                # Re-score with maker data
                                sc = sniper_score(info)
                        except Exception as _he:
                            logger.debug(f"Helius enrichment skip: {_he}")

                    # ── Meta/Twitter/Holder enrichments — run in parallel ─
                    async def _safe_meta():
                        try: return await enrich_token_meta(info, item)
                        except Exception as e: logger.debug(f"Meta error: {e}"); return {}
                    async def _safe_tw():
                        try:
                            _hc = await get_http()
                            _res = await enrich_twitter_momentum(info, _hc)
                            return _res if _res is not None else {}
                        except Exception as e: logger.debug(f"Twitter error: {e}"); return {}
                    async def _safe_hd():
                        try:
                            _hc = await get_http()
                            return await enrich_holder_distribution(contract, info.get("chain",""), _hc)
                        except Exception as e: logger.debug(f"Holder error: {e}"); return {}
                    _enrich_results = await _asyncio.gather(_safe_meta(), _safe_tw(), _safe_hd())
                    for _er in _enrich_results:
                        if _er is not None:
                            info.update(_er)

                    # ── Helius deep enrichment (Solana only) — parallel ───────
                    if helius_key and info.get("chain","").lower() in ("solana","sol"):
                        async def _safe_dev():
                            try: return await enrich_dev_wallet_history(contract, info, helius_key)
                            except Exception as e: logger.debug(f"Dev history error: {e}"); return {}
                        async def _safe_clust():
                            try:
                                _th = info.get("holder_distribution", [])
                                return await enrich_wallet_clustering(contract, _th, helius_key)
                            except Exception as e: logger.debug(f"Clustering error: {e}"); return {}
                        async def _safe_vp():
                            try: return await enrich_volume_pattern(contract, helius_key)
                            except Exception as e: logger.debug(f"Vol pattern error: {e}"); return {}
                        _h_results = await _asyncio.gather(_safe_dev(), _safe_clust(), _safe_vp())
                        for _hr in _h_results:
                            info.update(_hr)
                        sc = sniper_score(info)

                    # ── AI Analysis ───────────────────────────────────────
                    ai = await ai_analyze_token(info, sc, ud, uid)

                    # Build full log entry
                    log_entry = {
                        "contract":    contract,
                        "symbol":      info["symbol"],
                        "chain":       info.get("chain", "?"),
                        "mc":          info["mc"],
                        "liq":         info.get("liq", 0),
                        "age_h":       round(age_h, 2),
                        "score":       sc["score"],
                        "verdict":     ai["verdict"],
                        "confidence":  ai["confidence"],
                        "rug_risk":    ai["rug_risk"],
                        "momentum":    ai["momentum"],
                        "social":      ai["social_score"],
                        "thesis":      ai.get("thesis", ""),
                        "red_flags":   ai.get("red_flags", []),
                        "green_flags": ai.get("green_flags", []),
                        "hard_flags":  sc.get("flags", []),
                        "sniper_strengths": sc.get("strengths", []),
                        "sniper_warnings":  sc.get("warnings", []),
                        "pf_curve":    info.get("pf_curve"),
                        "pf_graduated":info.get("pf_graduated", False),
                        "maker_pct":   info.get("maker_pct"),
                        "top3_vol_pct":info.get("top3_vol_pct"),
                        "timestamp":   datetime.now().isoformat(),
                        "bought":      False,
                        "rc_rate_limited": info.get("rc_rate_limited", False),
                    }
                    log = ud.setdefault("sniper_log", [])
                    log.append(log_entry)
                    if len(log) > SNIPER_LOG_MAX:
                        ud["sniper_log"] = log[-SNIPER_LOG_MAX:]

                    # ── Dump log channel: queue compact line for PASS tokens ─
                    _dl_ch2 = ud.get("sniper_log_channel")
                    if _dl_ch2 and ud.get("sniper_log_channel_on", True):
                        _rc_b2 = " ⚠️RC" if info.get("rc_rate_limited") else ""
                        _v_icon = {"SNIPE":"🟢","WAIT":"🟡","SKIP":"🔴"}.get(ai.get("verdict","?"),"⚪")
                        _ds_link2  = "https://dexscreener.com/solana/" + contract if info.get("chain","").lower() in ("sol","solana") else ""
                        # ── Extra fields ──────────────────────────────────────
                        _dl2_age   = info.get("age_h") or 0
                        _dl2_age_s = (str(round(_dl2_age, 1)) + "h") if _dl2_age < 48 else (str(round(_dl2_age/24, 1)) + "d")
                        _dl2_ath_mc = info.get("ath_mc", 0)
                        _dl2_cur_mc = info.get("mc", 0)
                        _dl2_ath_s  = ("ATH " + mc_str(_dl2_ath_mc)) if _dl2_ath_mc and _dl2_ath_mc > _dl2_cur_mc else ""
                        _dl2_top10  = info.get("top10_pct")
                        _dl2_top10_s = ("Top10: " + str(round(_dl2_top10)) + "%") if _dl2_top10 is not None else ""
                        _dl2_insider = info.get("insider_pct")
                        _dl2_bundle_s = ("Bundle: " + str(round(_dl2_insider)) + "%") if _dl2_insider is not None else ""
                        _dl2_lp    = info.get("lp_burn")
                        _dl2_lp_s  = ("LP: " + str(round(_dl2_lp)) + "%") if _dl2_lp is not None else ""
                        _dl2_details = "  ".join(x for x in [_dl2_age_s, _dl2_ath_s, _dl2_top10_s, _dl2_bundle_s, _dl2_lp_s] if x)
                        _dl_text2 = (
                            _v_icon + " " + ai.get("verdict","?") + "  $" + info.get("symbol","?") + "  "
                            + info.get("chain","?").upper()[:3] + "  "
                            + str(sc.get("score",0)) + "/100  "
                            + mc_str(info.get("mc",0)) + "  conf:" + str(ai.get("confidence",0)) + "/10"
                            + _rc_b2 + "\n"
                            + ("📋 " + _dl2_details + "\n" if _dl2_details else "")
                            + "└ " + (ai.get("thesis","")[:80] or "No thesis")
                            + ("\n🔗 " + _ds_link2 if _ds_link2 else "")
                        )
                        _dl_queue.setdefault(uid, _collections.deque()).append({
                            "chat_id": int(_dl_ch2),
                            "text": _dl_text2,
                            "disable_notification": False,
                        })

                    # ════════════════════════════════════════════════════
                    # MODE 0 — APEX AUTONOMOUS ENGINE
                    # ════════════════════════════════════════════════════
                    # APEX only enters on SNIPE verdict. WAIT was previously allowed
                    # in learning phase to gather data, but caused APEX to buy
                    # low-quality tokens indiscriminately ("buying every coin" bug).
                    _apex_verdict_ok = ai["verdict"] == "SNIPE"
                    if ud.get("apex_mode") and _apex_verdict_ok:
                        apex_reset_daily(ud)
                        _ok = True
                        _apex_phase = apex_get_phase(ud)
                        _hp_gate    = get_apex_profile(ud)     # None = default
                        _min_conf   = max(ud.get("apex_learn_threshold", APEX_MIN_CONFIDENCE),
                                         _hp_gate["min_confidence"] if _hp_gate else 0)
                        _min_score  = max(ud.get("apex_learn_score_min", 45),
                                         _hp_gate["min_score"] if _hp_gate else 0)
                        _heat_cap   = _hp_gate["heat_stop"] if _hp_gate else APEX_HEAT_STOP
                        # ── Entry gates apply in BOTH learning and optimised phases ──────
                        # Previously learning phase skipped confidence and score filters,
                        # causing bad trades to poison the learning memory from the start.
                        # Now full gates apply always — learning phase is MORE conservative.
                        if _apex_phase == "learning":
                            if apex_capital_heat(ud) >= _heat_cap:              _ok = False
                            elif apex_is_daily_loss_halted(ud):                  _ok = False
                            elif _sol_bearish:                                   _ok = False
                            elif _market_regime == "DEAD":                       _ok = False  # market dead — pause all entries
                            elif ai.get("confidence",0) < _min_conf:             _ok = False  # fixed: was skipped in learning
                            elif sc.get("score",0) < _min_score:                 _ok = False  # fixed: was skipped in learning
                            else:
                                _avoid_hrs = ud.get("apex_avoid_hours", [])
                                if _avoid_hrs:
                                    _now_hr = datetime.utcnow().hour
                                    if _now_hr in _avoid_hrs:
                                        _ok = False
                        else:
                            if apex_is_paused(uid):                              _ok = False
                            elif apex_is_daily_loss_halted(ud):                  _ok = False
                            elif _market_regime == "DEAD":                       _ok = False  # market dead — pause all entries
                            elif apex_capital_heat(ud) >= _heat_cap:             _ok = False
                            elif apex_count_positions(ud) >= ud.get("apex_max_positions_learned", APEX_MAX_POSITIONS): _ok = False
                            elif ai.get("confidence",0) < _min_conf:             _ok = False
                            elif sc.get("score",0) < _min_score:                 _ok = False
                            else:
                                # Hour avoidance (learned from losing patterns)
                                _avoid_hrs = ud.get("apex_avoid_hours", [])
                                if _avoid_hrs:
                                    _now_hr = datetime.utcnow().hour
                                    if _now_hr in _avoid_hrs:
                                        _ok = False
                        if _ok:
                            # Guard: don't overwrite an already-queued entry (resets 45s timer)
                            _already_queued = contract in _apex_entry_queue.get(uid, {})
                            if not _already_queued:
                                base_amt = ud.get("sniper_filters", {}).get("buy_amount", 50.0)
                                _apex_entry_queue.setdefault(uid, {})[contract] = {
                                    "info": info, "sc": sc, "ai": ai,
                                    "queued_at": datetime.now(), "base_amount": base_amt,
                                }
                                # Mark as seen so sniper won't re-scan it next cycle
                                seen_map[contract] = _time.time()
                                save_user(uid, ud)  # persist immediately
                                logger.info(f"APEX queued {contract} ({info.get('symbol','?')}) for {uid}")
                            else:
                                logger.debug(f"APEX: {contract} already in queue for {uid}, skipping")

                    # ════════════════════════════════════════════════════
                    # MODE 1 — FULL AUTO
                    # ════════════════════════════════════════════════════
                    if ud.get("sniper_auto") and not ud.get("apex_mode") and ai["verdict"] == "SNIPE":
                        buy_amt = min(ai["suggested_amount"], ud["balance"], budget - spent)
                        if buy_amt < 1:
                            continue
                        sniper_bought.append(contract)
                        if len(sniper_bought) > 500:
                            ud["sniper_bought"] = sniper_bought[-500:]
                        ud["sniper_daily_spent"] = spent + buy_amt
                        ud["sniper_log"][-1]["bought"] = True
                        ud["sniper_log"][-1]["amount"] = buy_amt

                        result = await do_buy_core(ud, uid, contract, buy_amt, planned=True, mood="AI-Sniper")
                        if isinstance(result, tuple):
                            info2, _ = result
                            h = ud["holdings"].get(contract, {})
                            if ud.get("sniper_auto_sl") and h:
                                # Tier-based SL defaults — tighter for riskier tokens
                                _rug = ai.get("rug_risk", "MEDIUM")
                                sl_pct = {"LOW": 22.0, "MEDIUM": 18.0, "HIGH": 12.0}.get(_rug, 18.0)
                                # Allow user override only if it's tighter than the tier default
                                _user_sl = ud.get("sniper_auto_sl_pct", 40.0)
                                if _user_sl < sl_pct:
                                    sl_pct = _user_sl
                                h["stop_loss_pct"] = sl_pct
                            if ud.get("sniper_auto_tp") and h:
                                tp_xs    = ud.get("sniper_auto_tp_x", [2.0, 5.0])
                                pct_each = round(1.0 / len(tp_xs), 2)
                                h["auto_sells"] = [{"pct": pct_each, "x": x, "triggered": False} for x in tp_xs]
                                if h["auto_sells"]:
                                    h["auto_sells"][-1]["pct"] = round(1.0 - pct_each * (len(tp_xs) - 1), 2)

                            if ud.get("sniper_auto_notify", True):
                                sl_line = f"\n🛑 Stop Loss: {ud.get('sniper_auto_sl_pct',40)}%" if ud.get("sniper_auto_sl") else ""
                                tp_line = ""
                                if ud.get("sniper_auto_tp"):
                                    tp_xs2   = ud.get("sniper_auto_tp_x", [2.0, 5.0])
                                    pct_e2   = round(1.0 / len(tp_xs2), 2)
                                    tp_parts = [f"{int(pct_e2*100)}% at {x}x" for x in tp_xs2]
                                    tp_line  = "\n🎯 TP: " + "  |  ".join(tp_parts)
                                try:
                                    await ctx.bot.send_message(
                                        chat_id=uid, parse_mode="Markdown",
                                        text=(
                                            "🤖 *AI AUTO-SNIPE EXECUTED*\n\n"
                                            "*$" + _md(info2["symbol"]) + "*  " + info2.get("chain","").upper() + "\n"
                                            "Confidence: *" + str(ai["confidence"]) + "/10*\n"
                                            "Rug Risk: *" + ai["rug_risk"] + "*\n\n"
                                            "📝 " + ai["thesis"] + "\n\n"
                                            "💵 Bought: *" + money(buy_amt) + "*\n"
                                            "Price: *" + money(info2["price"]) + "*\n"
                                            "MC: *" + mc_str(info2["mc"]) + "*\n"
                                            "Cash left: *" + money(ud["balance"]) + "*"
                                            + sl_line + tp_line
                                        ),
                                        reply_markup=_sniper_auto_kb(contract)
                                    )
                                except Exception as _ne:
                                    logger.error(f"Auto snipe notify error: {_ne}")

                    # ════════════════════════════════════════════════════
                    # MODE 2 — ADVISORY
                    # Runs independently of APEX mode — both can be active.
                    # DM advisory and channel advisory always fire when enabled.
                    # Paused when market regime is DEAD (nothing clean to show).
                    # Exclusive toggle:
                    #   sniper_adv_notify = True  → DM pill only, channel silent
                    #   sniper_adv_notify = False → Channel only, no DM
                    # Skip if APEX already queued this token this cycle — user
                    # will get an APEX entry alert and doesn't need a duplicate pill.
                    # ════════════════════════════════════════════════════
                    _apex_already_queued = contract in _apex_entry_queue.get(uid, {})
                    if ud.get("sniper_advisory") and _market_regime != "DEAD" and not _apex_already_queued:
                        _sniper_analysis_cache.setdefault(uid, {})[contract] = {
                            "info": info, "sc": sc, "ai": ai
                        }
                        cache = _sniper_analysis_cache[uid]
                        if len(cache) > 20:
                            for k in list(cache.keys())[:-20]:
                                del cache[k]

                        dm_notify = ud.get("sniper_adv_notify", True)
                        ch_id     = ud.get("sniper_broadcast_channel")

                        if dm_notify:
                            # ── DM MODE: send pill to user, channel is silent ──
                            # Only signal SNIPE and WAIT — SKIP verdicts are noise in DM
                            if ai["verdict"] not in ("SNIPE", "WAIT"):
                                seen_map[contract] = _time.time()
                                save_user(uid, ud)
                            else:
                                pill = _compact_pill_text(info, sc, ai)
                                suggested = ai.get("suggested_amount", 0)
                                _adv_kb_rows = [
                                    [InlineKeyboardButton("👁 View Analysis", callback_data="snp_view_" + contract)],
                                ]
                                if ai["verdict"] == "SNIPE" and suggested > 0:
                                    _adv_kb_rows.append([
                                        InlineKeyboardButton(
                                            "✅ Buy " + money(suggested),
                                            callback_data="snp_confirm_" + contract + "_" + str(round(suggested, 2))
                                        ),
                                        InlineKeyboardButton("❌ Dismiss", callback_data="snp_skip_" + contract),
                                    ])
                                else:
                                    _adv_kb_rows.append([
                                        InlineKeyboardButton("❌ Dismiss", callback_data="snp_skip_" + contract),
                                    ])
                                try:
                                    await ctx.bot.send_message(
                                        chat_id=uid, parse_mode="Markdown",
                                        text=pill,
                                        reply_markup=InlineKeyboardMarkup(_adv_kb_rows)
                                    )
                                    # Mark seen only AFTER successful send
                                    seen_map[contract] = _time.time()
                                    save_user(uid, ud)  # persist immediately so redeploy doesn't re-ping
                                except Exception as _ae:
                                    logger.error(f"Advisory pill error {uid}: {_ae}")
                        else:
                            # ── CHANNEL MODE: broadcast to channel, no DM ──────
                            if ch_id:
                                await _broadcast_to_channel(ctx.bot, int(ch_id), info, sc, ai, contract, uid=uid)
                                seen_map[contract] = _time.time()
                                save_user(uid, ud)  # persist immediately so redeploy doesn't rebroadcast
                            else:
                                # No channel configured — fall back to DM
                                if ai["verdict"] not in ("SNIPE", "WAIT"):
                                    seen_map[contract] = _time.time()
                                    save_user(uid, ud)
                                else:
                                    pill = _compact_pill_text(info, sc, ai)
                                    suggested = ai.get("suggested_amount", 0)
                                    _adv_kb_rows2 = [
                                        [InlineKeyboardButton("👁 View Analysis", callback_data="snp_view_" + contract)],
                                    ]
                                    if ai["verdict"] == "SNIPE" and suggested > 0:
                                        _adv_kb_rows2.append([
                                            InlineKeyboardButton(
                                                "✅ Buy " + money(suggested),
                                                callback_data="snp_confirm_" + contract + "_" + str(round(suggested, 2))
                                            ),
                                            InlineKeyboardButton("❌ Dismiss", callback_data="snp_skip_" + contract),
                                        ])
                                    else:
                                        _adv_kb_rows2.append([
                                            InlineKeyboardButton("❌ Dismiss", callback_data="snp_skip_" + contract),
                                        ])
                                    try:
                                        await ctx.bot.send_message(
                                            chat_id=uid, parse_mode="Markdown",
                                            text=pill,
                                            reply_markup=InlineKeyboardMarkup(_adv_kb_rows2)
                                        )
                                        seen_map[contract] = _time.time()
                                        save_user(uid, ud)  # persist immediately
                                    except Exception as _ae:
                                        logger.error(f"Advisory fallback DM error {uid}: {_ae}")

                except Exception as _ue:
                    logger.warning(f"Sniper job user error {uid}: {_ue}")

        # ── Skip reason summary — shows up in Railway logs every sniper cycle ──
        for uid2, ud2 in active_users:
            skip_counts = ud2.get("sniper_skip_counts", {})
            seen_count  = len(ud2.get("sniper_seen", {}))
            if skip_counts or seen_count:
                logger.info(
                    f"Sniper skip summary uid={uid2}: seen={seen_count} | "
                    + " | ".join(f"{k}={v}" for k, v in sorted(skip_counts.items(), key=lambda x: -x[1]))
                )
                # Reset per-cycle counts
                ud2["sniper_skip_counts"] = {}

    except Exception as e:
        logger.error(f"sniper_job crashed: {e}", exc_info=True)


async def bundle_sell_detector(app):
    """
    Runs inside run_checker. Watches AI-sniped positions for dump patterns
    and exits immediately if detected.
    """
    for uid, ud in list(users.items()):
        if not ud.get("sniper_auto"):
            continue
        for contract, h in list(ud.get("holdings", {}).items()):
            # Skip positions managed by APEX (it has own threat detection)
            if h.get("mood") == "APEX":
                continue
            # Skip AI-Sniper positions that APEX has taken over (apex_trail_stop set)
            if h.get("mood") == "AI-Sniper" and h.get("apex_trail_stop") is not None:
                continue
            if h.get("mood") not in ("AI-Sniper", "Sniper"):
                continue
            try:
                info = await get_token(contract)
                if not info:
                    continue

                price  = info["price"]
                avg    = h.get("avg_price", price)
                drop   = (price - avg) / avg * 100 if avg > 0 else 0
                vol_m5 = info.get("vol_m5", 0)
                vol_h1 = info.get("vol_h1", 0)
                buy_pct = info.get("buy_pct", 50)
                liq     = info.get("liq", 0)

                # Bundle sell signals:
                # 1. Price dropped >25% from entry AND sell pressure >70%
                # 2. 5m volume spike 5x hourly avg AND buy_pct < 35% (dump in progress)
                # 3. Liquidity dropped >40% — LP being pulled
                avg_5m = vol_h1 / 12 if vol_h1 > 0 else 0
                liq_at_buy = h.get("liq_at_buy", liq)

                bundle_detected = False
                reason = ""

                if drop <= -25 and buy_pct < 30:
                    bundle_detected = True
                    reason = f"Price dropped {round(drop,1)}% + heavy sell pressure ({100-buy_pct}% sells)"
                elif avg_5m > 0 and vol_m5 >= avg_5m * 5 and buy_pct < 35:
                    bundle_detected = True
                    reason = f"Massive volume spike ({round(vol_m5/avg_5m,1)}x avg) with {100-buy_pct}% sells"
                elif liq_at_buy > 0 and liq < liq_at_buy * 0.6:
                    bundle_detected = True
                    reason = f"Liquidity pulled: {money(liq_at_buy)} → {money(liq)}"

                if bundle_detected and contract in ud["holdings"]:
                    cv = h["amount"] * price
                    result = sell_core(ud, uid, contract, cv, price, "bundle_sell_exit")
                    if ud.get("sniper_auto_notify", True):
                        try:
                            await app.bot.send_message(
                                chat_id=uid, parse_mode="Markdown",
                                text=(
                                    "🚨 *BUNDLE SELL DETECTED — EMERGENCY EXIT*\n\n"
                                    "*$" + _md(h["symbol"]) + "* — AI sniper position closed!\n\n"
                                    "⚠️ *Signal:* " + reason + "\n\n"
                                    "Sold: *" + money(cv) + "*\n"
                                    "PnL: *" + pstr(result["realized"]) + "*\n"
                                    "Cash: *" + money(ud["balance"]) + "*"
                                ),
                                reply_markup=main_menu_kb()
                            )
                        except Exception as _be:
                            logger.error(f"Bundle sell notify error: {_be}")
            except Exception:
                continue


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ud = get_user(u.id, u.username or u.first_name)

    # ── ACCESS CONTROL GATE ───────────────────────────────────────────────────
    if ACCESS_CONTROL_ENABLED and not is_admin(u.id):
        # Already approved users pass through instantly
        if not ud.get("access_approved"):
            # Already waiting — just remind them
            if u.id in _pending_access:
                if update.message:
                    await update.message.reply_text(
                        "⏳ Your access request is pending admin approval.\n\n"
                        "Please contact @" + ACCESS_ADMIN_USERNAME + " if you haven't already."
                    )
                return
            # New request — notify all admins
            _raw_name     = u.full_name or u.first_name or "Unknown"
            _raw_username = "@" + u.username if u.username else "no username"
            _pending_access[u.id] = {
                "name":     _raw_name,
                "username": _raw_username,
            }
            # Escape ALL user-controlled strings — names/usernames with underscores
            # or asterisks cause Telegram Markdown BadRequest which was silently
            # swallowed, meaning admin NEVER received new-user notifications.
            _safe_name = _md(_raw_name)
            _safe_uname = _raw_username.replace("_", "\\_")
            for _aid in ADMIN_IDS:
                try:
                    await ctx.bot.send_message(
                        chat_id=_aid,
                        parse_mode="Markdown",
                        text=(
                            "🔔 *NEW ACCESS REQUEST*\n\n"
                            "Name: *" + _safe_name + "*\n"
                            "Username: " + _safe_uname + "\n"
                            "User ID: `" + str(u.id) + "`"
                        ),
                        reply_markup=InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("✅ Approve", callback_data="access_approve_" + str(u.id)),
                                InlineKeyboardButton("❌ Deny",    callback_data="access_deny_"    + str(u.id)),
                            ]
                        ])
                    )
                except Exception as _notify_err:
                    logger.error(f"Access notify to admin {_aid} failed: {_notify_err}")
            if update.message:
                await update.message.reply_text(
                    "🔒 *Access Required*\n\n"
                    "This bot is private. Your request has been sent to the admin.\n\n"
                    "Please contact @" + ACCESS_ADMIN_USERNAME.replace("_", "\\_") + " for access.",
                    parse_mode="Markdown"
                )
            return
    # ── END ACCESS GATE ───────────────────────────────────────────────────────

    # ── Block /start in groups — bot should only respond to CAs there ───────
    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        # Silently ignore /start in groups — don't post balance/menu
        return

    # Handle deep links: /start holders_CA  or  /start buy_CA  or  /start CA
    payload = (ctx.args[0] if ctx.args else "").strip()

    # ── Group deep-link: buy_CA ───────────────────────────────────────────────
    if payload.startswith("buy_") and len(payload) > 10:
        contract_dl = payload[4:]
        if ud.get("balance") is None:
            pending[u.id] = {"action": "set_balance"}
            await update.message.reply_text(
                "👋 Welcome! Set your starting balance first (e.g. 1000):",
                reply_markup=cancel_kb()
            )
            return
        info_dl = await get_token(contract_dl)
        if info_dl and update.message:
            sc_dl = score_token(info_dl)
            ud["last_chain"] = info_dl.get("chain", "solana")
            await send_token_card(update.message, info_dl, contract_dl, ud, sc_dl, ctx, is_query=False)
        return

    # ── Group deep-link: sell_CA ──────────────────────────────────────────────
    if payload.startswith("sell_") and len(payload) > 10:
        contract_dl = payload[5:]
        if ud.get("balance") is None:
            await update.message.reply_text("Use /start to set up your account first.")
            return
        if contract_dl not in ud.get("holdings", {}):
            info_dl = await get_token(contract_dl)
            sym_dl  = info_dl["symbol"] if info_dl else contract_dl[:8]
            await update.message.reply_text(
                "You don't hold *$" + _md(sym_dl) + "* yet.\n\nPaste the CA to buy first.",
                parse_mode="Markdown",
                reply_markup=main_menu_kb()
            )
            return
        await update.message.reply_text(
            "🔴 *Sell $" + _md(ud["holdings"][contract_dl]["symbol"]) + "*\n\nHow much to sell?",
            parse_mode="Markdown",
            reply_markup=sell_sub_kb(contract_dl)
        )
        return

    # ── Group deep-link: alert_CA ─────────────────────────────────────────────
    if payload.startswith("alert_") and len(payload) > 12:
        contract_dl = payload[6:]
        if ud.get("balance") is None:
            await update.message.reply_text("Use /start to set up your account first.")
            return
        info_dl = await get_token(contract_dl)
        sym_dl  = info_dl["symbol"] if info_dl else contract_dl[:8]
        price_dl= info_dl["price"]  if info_dl else 0
        pending[u.id] = {"action": "price_alert", "contract": contract_dl,
                         "symbol": sym_dl, "current_price": price_dl}
        await update.message.reply_text(
            "🔔 *Set Price Alert — $" + _md(sym_dl) + "*\n\n"
            "Current price: " + money(price_dl) + "\n\nEnter your target price:",
            parse_mode="Markdown",
            reply_markup=cancel_kb()
        )
        return

    # ── Group deep-link: track_CA ─────────────────────────────────────────────
    if payload.startswith("track_") and len(payload) > 12:
        contract_dl = payload[6:]
        if ud.get("balance") is None:
            await update.message.reply_text("Use /start to set up your account first.")
            return
        info_dl = await get_token(contract_dl)
        if info_dl:
            if not isinstance(ud.get("watchlist"), dict):
                ud["watchlist"] = {}
            ud["watchlist"][contract_dl] = {
                "symbol": info_dl["symbol"], "name": info_dl["name"],
                "added_price": info_dl["price"], "added_mc": info_dl["mc"],
                "target_price": None, "target_mc": None,
            }
            save_user(u.id, ud)
            await update.message.reply_text(
                "👁 *$" + _md(info_dl["symbol"]) + "* added to your watchlist!\n\nYou'll be alerted on price moves.",
                parse_mode="Markdown",
                reply_markup=main_menu_kb()
            )
        return

    # ── Holder card deep link ─────────────────────────────────────────────────
    if payload.startswith("holders_") and ud.get("balance") is not None:
        contract_dl = payload[8:]
        info_dl = await get_token(contract_dl)
        if info_dl and update.message:
            holders_dl  = info_dl.get("top_holders_data", [])
            sym_dl      = info_dl.get("symbol", "?")
            chain_dl    = info_dl.get("chain", "solana").lower()
            top10_dl    = info_dl.get("top10_pct")
            top10_str_dl = ("  |  Top10: *" + str(top10_dl) + "%*") if top10_dl is not None else ""
            _EXPL_DL = {
                "solana": "https://solscan.io/account/", "sol": "https://solscan.io/account/",
                "ethereum": "https://etherscan.io/address/", "eth": "https://etherscan.io/address/",
                "bsc": "https://bscscan.com/address/", "base": "https://basescan.org/address/",
                "arbitrum": "https://arbiscan.io/address/",
            }
            expl_dl = _EXPL_DL.get(chain_dl, "https://solscan.io/account/")
            _TICONS_DL = {
                "sniper": "\U0001f3af", "smart_money": "\U0001f4b0", "smartmoney": "\U0001f4b0",
                "smart": "\U0001f4b0", "fresh": "\U0001fab7", "new_wallet": "\U0001fab7",
                "bundle": "\U0001f4e6", "insider": "\U0001f42d", "kol": "\U0001f440", "whale": "\U0001f433",
            }
            medals_dl = ["\U0001f947", "\U0001f948", "\U0001f949"]
            if not holders_dl:
                text_dl = "\U0001f465 *TOP HOLDERS \u2014 $" + _md(sym_dl) + "*\n\n_No holder data available yet._\n_Token may be too new for RugCheck to index._"
            else:
                dl_lines = ["\U0001f465 *TOP HOLDERS \u2014 $" + _md(sym_dl) + "*" + top10_str_dl]
                for _hi, _hw in enumerate(holders_dl):
                    _addr  = _hw.get("address", "")
                    _pct   = _hw.get("pct", 0)
                    _tags  = _hw.get("tags", [])
                    _ins   = _hw.get("insider", False)
                    _ticons = "".join(_TICONS_DL[t] for t in _tags if t in _TICONS_DL)
                    if _ins and "\U0001f42d" not in _ticons:
                        _ticons += "\U0001f42d"
                    _addr_s = (_addr[:4] + "..." + _addr[-4:]) if len(_addr) >= 8 else _addr
                    _rank   = medals_dl[_hi] if _hi < 3 else ("#" + str(_hi + 1))
                    if _addr:
                        dl_lines.append(_rank + " [" + _addr_s + "](" + expl_dl + _addr + ") *" + str(_pct) + "%*" + (" " + _ticons if _ticons else ""))
                    else:
                        dl_lines.append(_rank + " *" + str(_pct) + "%*" + (" " + _ticons if _ticons else ""))
                text_dl = "\n".join(dl_lines)
            await update.message.reply_text(
                text_dl,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("\u25c0 Back to Token", callback_data="btt_" + contract_dl)
                ]])
            )
        return

    # ── Token card deep link ──────────────────────────────────────────────────
    if payload and len(payload) > 20 and ud.get("balance") is not None:
        # Looks like a CA — auto-load the token card
        info_dl = await get_token(payload)
        if info_dl:
            card_text = token_card(info_dl, payload, ud)
            if update.message:
                await update.message.reply_text(card_text, parse_mode="Markdown", reply_markup=buy_kb(payload, ud))
            return

    if ud.get("balance") is None:
        pending[u.id] = {"action": "set_balance"}
        text = (
            "👋 Welcome to *APEX SNIPER BOT*!\n\n"
            "Advanced multi-chain paper trading bot.\n\n"
            "Set your starting balance:\n"
            "Min: $1  |  Max: $10,000\n\n"
            "Enter your starting balance:"
        )
        if update.message:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=cancel_kb())
        else:
            await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=cancel_kb())
        return
    text = (
        "⚡ *APEX SNIPER BOT*\n\n"
        "Welcome back, *" + _md(ud["username"]) + "*!\n"
        "💰 Balance: *" + money(ud["balance"]) + "*\n"
        "💎 Savings: *" + money(ud["savings"]) + "*\n\n"
        "Paste any crypto CA to trade 👇"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())


def _get_user_lock(uid: int):
    """Get or create a per-user asyncio lock (prevents concurrent buy/sell races)."""
    if uid not in _user_locks:
        import asyncio as _al
        _user_locks[uid] = _al.Lock()
    return _user_locks[uid]


def apex_get_phase(ud: dict) -> str:
    """
    Returns the APEX learning phase for a user.
    - 'learning'  : < 20 total APEX trades — gather data, relaxed gates, calibrate every 5 trades
    - 'active'    : ≥ 20 trades — full learned gates applied, calibrate every 10 trades
    """
    total = ud.get("apex_total_trades", 0)
    if total < 20:
        return "learning"
    return "active"


def apex_capital_heat(ud: dict) -> float:
    # ── IMPORTANT: only count APEX/AI-Sniper holdings as "deployed" capital.
    # Manual paper trades must NOT inflate heat or they will incorrectly block
    # APEX entries and reduce position sizes even when APEX capital is free.
    balance  = ud.get("balance", 0)
    deployed = sum(
        h.get("total_invested", 0)
        for h in ud.get("holdings", {}).values()
        if h.get("mood") in ("APEX", "AI-Sniper", "APEX-DCA")
    )
    total = balance + sum(h.get("total_invested", 0) for h in ud.get("holdings", {}).values())
    return (deployed / total) if total > 0 else 0.0


def apex_position_size(ud: dict, ai_confidence: int, base_amount: float, info: dict | None = None, rug_risk: str = "LOW") -> float:
    heat = apex_capital_heat(ud)
    if heat >= APEX_HEAT_STOP:
        return 0.0
    if ai_confidence >= 9:
        conf_mult = 1.0
    elif ai_confidence >= 7:
        conf_mult = 0.70
    elif ai_confidence >= 5:
        conf_mult = 0.50
    else:
        return 0.0
    if heat >= APEX_HEAT_CAUTION:
        heat_mult = 0.50
    elif heat >= APEX_HEAT_SAFE:
        heat_mult = 0.75
    else:
        heat_mult = 1.0
    consec = ud.get("apex_consec_losses", 0)
    dd_mult = APEX_DRAWDOWN_2_MULT if consec >= 2 else APEX_DRAWDOWN_1_MULT if consec >= 1 else 1.0
    # During learning phase, ignore drawdown multiplier — need full data
    _phase = apex_get_phase(ud)
    if _phase == "learning":
        dd_mult = 1.0
    size_mult = ud.get("apex_size_mult", 1.0)
    # ── #12 Entry quality multiplier ─────────────────────────────────────
    # Perfect setup: fresh token, strong buy pressure, KOL signal → size up
    # Marginal setup: old token, high MC, rug flags → size down
    quality_mult = 1.0
    if info is not None:
        age_h       = info.get("age_h") or 0
        buy_pct_m5  = info.get("buy_pct_m5", info.get("buy_pct", 50))
        kol_count   = info.get("kol_buy_count", 0)
        mc          = info.get("mc", 0)
        # rug_risk param passed directly from ai["rug_risk"] at call site
        # Perfect: very fresh + strong buy pressure + KOL signal
        if age_h < 0.5 and buy_pct_m5 >= 65 and kol_count >= 1:
            quality_mult = 1.2
        elif age_h < 1.0 and buy_pct_m5 >= 60:
            quality_mult = 1.1
        # Marginal: old OR very high MC OR rug flag
        elif age_h > 2.0 or mc > 80_000 or rug_risk == "HIGH":
            quality_mult = 0.6
        elif age_h > 1.0 or mc > 60_000:
            quality_mult = 0.8
    return max(1.0, round(base_amount * conf_mult * heat_mult * dd_mult * size_mult * quality_mult, 2))


def apex_trail_pct(current_x: float, ud: dict | None = None) -> float:
    """Return trail pct for current_x. Uses calibrated learned values when ud supplied."""
    if ud is not None:
        if current_x >= 10.0: return ud.get("apex_trail_pct_learned_moon", APEX_TRAIL_PCT_MOON)
        if current_x >= 5.0:  return ud.get("apex_trail_pct_learned_high", APEX_TRAIL_PCT_HIGH)
        if current_x >= 2.0:  return ud.get("apex_trail_pct_learned_mid",  APEX_TRAIL_PCT_MID)
        return ud.get("apex_trail_pct_learned_early", APEX_TRAIL_PCT_EARLY)
    if current_x >= 10.0: return APEX_TRAIL_PCT_MOON
    if current_x >= 5.0:  return APEX_TRAIL_PCT_HIGH
    if current_x >= 2.0:  return APEX_TRAIL_PCT_MID
    return APEX_TRAIL_PCT_EARLY


def apex_check_threat(info: dict, h: dict) -> str:
    """
    Rug/dump threat detector — runs every 8–15s.

    SMART RED THRESHOLDS (entry-aligned + peak-aware):
    ─────────────────────────────────────────────────
    Base stop is read from h["stop_loss_pct"] which APEX sets at entry
    based on the AI rug-risk assessment:
        HIGH rug risk  → 12%   tight, suspicious token
        MEDIUM         → 18%   moderate room
        LOW            → 22%   healthy token, needs space

    Peak-aware expansion: once a token has proven buyers exist by
    reaching a peak above entry, it earns additional room proportional
    to how high it went — because deep retraces after a pump are normal:
        Peak 1.2x–2x   → +8%  extra room  (e.g. LOW: 22% → 30%)
        Peak 2x+        → +15% extra room  (e.g. LOW: 22% → 37%)
        Peak never > 1.2x → no expansion  (token never showed strength)

    HARD FLOORS — never change regardless of peak or base stop:
        Liq drain -25%                  → RED always (LP removal)
        Buy% below 20%                  → RED always (panic)
        Liq -15% + buy% < 40%           → RED always (coordinated dump)
        Volume spike 4x + buy% < 30%    → RED always (bundle sell)
    """
    price      = info.get("price", 0)
    avg        = h.get("avg_price", price)
    liq        = info.get("liq", 0)
    liq_at_buy = h.get("liq_at_buy", liq)
    vol_m5     = info.get("vol_m5", 0)
    vol_h1     = info.get("vol_h1", 1)
    buy_pct_m5 = info.get("buy_pct_m5", info.get("buy_pct", 50))
    buy_pct_h1 = info.get("buy_pct_h1", info.get("buy_pct", 50))
    drop_pct   = (price - avg) / avg * 100 if avg > 0 else 0
    liq_drop   = ((liq_at_buy - liq) / liq_at_buy * 100) if liq_at_buy > 0 else 0
    avg_5m_vol = vol_h1 / 12 if vol_h1 > 0 else 0

    # ── Compute smart RED threshold ───────────────────────────────────────────
    # Base: read the per-token stop set at entry (rug-risk aligned)
    base_stop = h.get("stop_loss_pct", 15.0)   # fallback 15% for legacy positions

    # Peak-aware expansion: token proved buyers exist → earn more retrace room
    peak       = h.get("apex_peak_price", avg)
    cx_peak    = (peak / avg) if avg > 0 else 1.0
    cx_now     = (price / avg) if avg > 0 else 1.0   # current position vs entry
    if cx_peak >= 2.0:
        red_threshold = -(base_stop + 15.0)
    elif cx_peak >= 1.2:
        red_threshold = -(base_stop + 8.0)
    else:
        red_threshold = -base_stop

    # ══ HARD FLOOR RED — rug signals ══════════════════════════════════════════
    #
    # CONTEXT-AWARE: buy_pct_m5 < 20 previously fired unconditionally.
    # On thin Solana micro-caps one large sell in a 5m window pushes buy% below
    # 20% momentarily — this was exiting profitable positions (BUBBA at 1.44x,
    # HOPEFUL at 0.96x) on what was just a thin-book blip.
    #
    # Rule: require liquidity drain confirmation when position is AT or ABOVE
    # entry. True rugs drain liq AND kill buy pressure simultaneously.
    # Momentary sell-offs only kill buy pressure.
    # ─────────────────────────────────────────────────────────────────────────

    # 1. Liq drain — always RED regardless of position
    if liq_drop >= 25:
        return "RED"

    # 2. Buy pressure panic — context-aware
    if buy_pct_m5 < 10:
        # Extreme: < 10% buys. Exit unconditionally — this is a cascade.
        return "RED"
    elif buy_pct_m5 < 20:
        if cx_now < 0.95:
            # Position already losing + buy pressure gone → RED
            return "RED"
        elif liq_drop >= 10:
            # At/above entry but liq also draining → real rug confirmation
            return "RED"
        # else: buy% momentarily < 20% but price healthy and liq stable
        # → skip, wait for next cycle (thin book blip, not a rug)

    # 3. Coordinated dump: liq drain + sustained sell pressure
    if liq_drop >= 15 and buy_pct_m5 < 40:
        return "RED"

    # 4. Bundle/whale sell: volume spike + heavy sell pressure
    if avg_5m_vol > 0 and vol_m5 >= avg_5m_vol * 4 and buy_pct_m5 < 30:
        return "RED"

    # ══ SMART PRICE RED — entry-aligned + peak-aware ══════════════════════════
    if drop_pct <= red_threshold:
        return "RED"
    # Combined signal: price breach + sell pressure.
    # Only below entry — above entry the trail handles exits.
    # Prevents exiting a near-entry position on a mild dip + weak buy%.
    if cx_now < 1.0 and drop_pct <= (red_threshold * 0.7) and buy_pct_m5 < 40:
        return "RED"

    # ══ ORANGE — tighten trail stop ══════════════════════════════════════════
    orange_threshold = red_threshold * 0.55   # ~55% of RED threshold
    sigs = 0
    if drop_pct <= orange_threshold:                           sigs += 1
    if liq_drop >= 10:                                         sigs += 1
    if buy_pct_m5 < 40:                                        sigs += 1
    if buy_pct_h1 < 42:                                        sigs += 1
    if avg_5m_vol > 0 and vol_m5 >= avg_5m_vol * 2.5 and buy_pct_m5 < 45: sigs += 1
    if sigs >= 2: return "ORANGE"

    # ══ YELLOW — watch closely ════════════════════════════════════════════════
    yellow_threshold = red_threshold * 0.30
    warn = 0
    if drop_pct <= yellow_threshold:                           warn += 1
    if liq_drop >= 5:                                          warn += 1
    if buy_pct_m5 < 48:                                        warn += 1
    if buy_pct_h1 < 45:                                        warn += 1
    if warn >= 2 or sigs >= 1: return "YELLOW"
    return "CLEAR"


def apex_reset_daily(ud: dict) -> None:
    import time as _t
    today = _t.strftime("%Y-%m-%d")
    if ud.get("apex_daily_date") != today:
        ud["apex_daily_date"]         = today
        ud["apex_daily_pnl"]          = 0.0
        # Session start = vault balance + open APEX positions value
        ud["apex_session_start_bal"] = (
            ud.get("apex_vault", 0.0)
            + sum(
                h.get("total_invested", 0)
                for h in ud.get("holdings", {}).values()
                if h.get("mood") in ("APEX", "AI-Sniper", "APEX-DCA")
            )
        )


def apex_is_paused(uid: int) -> bool:
    from datetime import datetime as _dt
    pause_until = _apex_paused_until.get(uid)
    if pause_until and _dt.now() < pause_until:
        return True
    _apex_paused_until.pop(uid, None)
    return False


def apex_is_daily_loss_halted(ud: dict) -> bool:
    apex_reset_daily(ud)
    # Per-user setting — user can disable halt from APEX Settings
    if ud.get("apex_daily_loss_halt_disabled", False):
        return False
    vault = ud.get("apex_vault", 0.0)
    start = ud.get("apex_session_start_bal", vault)
    if start is None or start <= 0:
        return vault <= 0.0
    _hp = get_apex_profile(ud)
    _limit = _hp["daily_loss_limit"] if _hp else APEX_DAILY_LOSS_LIMIT
    return ud.get("apex_daily_pnl", 0.0) <= -(start * _limit)


def apex_count_positions(ud: dict) -> int:
    return sum(1 for h in ud.get("holdings", {}).values() if h.get("mood") in ("APEX", "AI-Sniper"))


def apex_learn_record(uid: int, entry: dict) -> None:
    """
    Record a completed trade into the learning memory.

    Minimum fields (existing call sites):
        score, confidence, outcome_x, pnl, reason

    Extended fields (new — populated at each call site):
        rug_risk        : "LOW" / "MEDIUM" / "HIGH"   — AI rug assessment at entry
        entry_mc        : float  — market cap at entry ($)
        entry_liq       : float  — liquidity at entry ($)
        entry_age_h     : float  — token age in hours at entry
        entry_buy_pct   : float  — buy% (m5) at entry
        hold_time_h     : float  — how long position was held
        peak_x          : float  — highest cx reached before exit
        open_pos_count  : int    — number of APEX positions open at entry
        entry_hour      : int    — UTC hour of entry (0-23)
        post_exit_x_1h  : float  — price ratio vs exit at +1h (fed back later)
        post_exit_x_4h  : float  — price ratio vs exit at +4h (fed back later)

    All fields are optional — calibrate uses what's available.
    """
    import time as _lrt
    entry.setdefault("ts", _lrt.time())
    mem = _apex_learn_memory.setdefault(uid, [])
    mem.append(entry)
    if len(mem) > APEX_SELF_LEARN_WINDOW * 2:
        _apex_learn_memory[uid] = mem[-APEX_SELF_LEARN_WINDOW:]
    if uid in users:
        ud_mem = users[uid].setdefault("apex_memory", [])
        ud_mem.append(entry)
        if len(ud_mem) > APEX_SELF_LEARN_WINDOW * 2:
            users[uid]["apex_memory"] = ud_mem[-APEX_SELF_LEARN_WINDOW:]


def _apex_rich_record(h: dict, cx: float, peak: float, avg: float,
                      pnl: float, reason: str, ud: dict) -> dict:
    """
    Build a rich learning record from a completed position.
    Call this at every exit point instead of manually assembling the dict.
    Returns a dict ready for apex_learn_record().
    """
    import time as _rrt
    import datetime as _rdt
    held_s  = 0.0
    if h.get("bought_at"):
        try:
            held_s = _rrt.time() - h["bought_at"].timestamp()
        except Exception:
            held_s = 0.0
    entry_hour = 0
    if h.get("bought_at"):
        try:
            entry_hour = h["bought_at"].utctimetuple().tm_hour
        except Exception:
            entry_hour = 0
    return {
        # ── Core fields (always present) ─────────────────────────────────────
        "score":          h.get("apex_entry_score", 0),
        "confidence":     h.get("apex_entry_conf", 0),
        "verdict":        "SNIPE",
        "outcome_x":      round(cx, 3),
        "peak_x":         round(peak / avg, 3) if avg > 0 else 1.0,
        "pnl":            pnl,
        "reason":         reason,
        # ── Token fingerprint at entry ────────────────────────────────────────
        "rug_risk":       h.get("apex_entry_rug", "LOW"),
        "entry_mc":       h.get("avg_cost_mc", 0),
        "entry_liq":      h.get("liq_at_buy", 0),
        "entry_age_h":    h.get("apex_entry_age_h", 0),
        "entry_buy_pct":  h.get("apex_entry_buy_pct", 50),
        "open_pos_count": h.get("apex_entry_pos_count", 1),
        "entry_hour":     entry_hour,
        # ── Hold behaviour ────────────────────────────────────────────────────
        "hold_time_h":    round(held_s / 3600, 3),
        "token_type":     h.get("apex_token_type", "organic"),
        # ── Post-exit slots (filled later by post-exit tracker) ───────────────
        "post_exit_x_1h": None,
        "post_exit_x_4h": None,
        "ts":             _rrt.time(),
    }


def apex_self_calibrate(ud: dict, uid: int, suggest_only: bool = True) -> dict:
    """
    Extended self-calibration engine — tunes 8 parameters from real trade data.

    Parameters tuned:
      1. apex_learn_threshold     — minimum AI confidence to enter
      2. apex_learn_score_min     — minimum sniper score to enter
      3. apex_sl_learned_low      — stop-loss % for LOW rug risk tokens
      4. apex_sl_learned_med      — stop-loss % for MEDIUM rug risk tokens
      5. apex_sl_learned_high     — stop-loss % for HIGH rug risk tokens
      6. apex_trail_activate_x    — multiplier at which trail activates
      7. apex_size_mult           — position size multiplier (0.5–1.5×)
      8. apex_avoid_hours         — UTC hours with statistically losing trades

    Requires >= 10 completed trades to produce any output.
    All adjustments are gradual (small steps) to avoid overcorrection.
    """
    mem = _apex_learn_memory.get(uid, [])
    if len(mem) < 10:
        return {}
    recent  = mem[-APEX_SELF_LEARN_WINDOW:]
    n       = len(recent)
    wins    = [t for t in recent if t.get("pnl", 0) > 0]
    losses  = [t for t in recent if t.get("pnl", 0) <= 0]
    wr      = len(wins) / n if n else 0
    changes = {}

    # ── 1. Confidence threshold ───────────────────────────────────────────────
    # Find confidence buckets that consistently lose money.
    # Bucket size = 2 (0-1, 2-3, 4-5, 6-7, 8-9)
    cur_conf = ud.get("apex_learn_threshold", APEX_MIN_CONFIDENCE)
    conf_buckets = {}
    for t in recent:
        b = (t.get("confidence", 5) // 2) * 2
        conf_buckets.setdefault(b, []).append(t.get("pnl", 0))
    losing_conf = [b for b, pnls in conf_buckets.items()
                   if sum(pnls) < 0 and len(pnls) >= 3]
    if losing_conf:
        new_thresh = min(max(losing_conf) + 2, 8)
        if new_thresh != cur_conf:
            if not suggest_only:
                ud["apex_learn_threshold"] = new_thresh
            changes["apex_learn_threshold"] = {
                "old": cur_conf, "new": new_thresh,
                "reason": f"Conf<={max(losing_conf)} buckets net-losing on {len(losing_conf)} bands",
            }

    # ── 2. Score minimum ──────────────────────────────────────────────────────
    cur_score  = ud.get("apex_learn_score_min", 45)
    score_low  = [t.get("pnl", 0) for t in recent if t.get("score", 50) < 40]
    if len(score_low) >= 5 and sum(score_low) < 0 and cur_score < 55:
        new_score_floor = min(55, cur_score + 5)
        if not suggest_only:
            ud["apex_learn_score_min"] = new_score_floor
        changes["apex_learn_score_min"] = {
            "old": cur_score, "new": new_score_floor,
            "reason": f"{len(score_low)} low-score trades consistently losing",
        }
    elif wr > 0.60 and cur_score > 40:
        new_s = max(40, cur_score - 3)
        if new_s != cur_score:
            if not suggest_only:
                ud["apex_learn_score_min"] = new_s
            changes["apex_learn_score_min"] = {
                "old": cur_score, "new": new_s,
                "reason": f"High WR {round(wr*100)}% — relaxing score filter",
            }

    # ── 3 & 4 & 5. Stop-loss per rug-risk tier ───────────────────────────────
    # For each rug risk tier, compute average outcome_x of losers.
    # If losers consistently exit well above the current SL (token was stopped too
    # early) OR well below (SL too wide and we held too long), adjust.
    # Adjustments: ±2% per calibration cycle, clamped to safe ranges.
    for tier, ud_key, default_sl, sl_min, sl_max in [
        ("LOW",    "apex_sl_learned_low",  22.0, 15.0, 35.0),
        ("MEDIUM", "apex_sl_learned_med",  18.0, 12.0, 28.0),
        ("HIGH",   "apex_sl_learned_high", 12.0,  8.0, 20.0),
    ]:
        tier_trades = [t for t in recent if t.get("rug_risk") == tier]
        if len(tier_trades) < 5:
            continue   # not enough data for this tier yet
        tier_losses = [t for t in tier_trades if t.get("pnl", 0) < 0]
        tier_wins   = [t for t in tier_trades if t.get("pnl", 0) > 0]
        cur_sl      = ud.get(ud_key, default_sl)

        if not tier_losses:
            # Pure wins on this tier — can try widening very slightly
            if tier_wins and cur_sl < sl_max - 3:
                # Only widen if avg peak_x suggests we have room
                avg_peak = sum(t.get("peak_x", 1) for t in tier_wins) / len(tier_wins)
                if avg_peak > 1.5:
                    new_sl = min(sl_max, cur_sl + 1.0)
                    if not suggest_only:
                        ud[ud_key] = new_sl
                    changes[ud_key] = {"old": cur_sl, "new": new_sl,
                                       "reason": f"{tier} tier: all wins, avg peak {round(avg_peak,2)}x — widening"}
            continue

        tier_wr  = len(tier_wins) / len(tier_trades)
        avg_loss_x = sum(t.get("outcome_x", 0.9) for t in tier_losses) / len(tier_losses)

        if tier_wr < 0.35:
            # Losing >65% on this tier — tighten SL to cut losses faster
            new_sl = max(sl_min, cur_sl - 2.0)
            if new_sl != cur_sl:
                if not suggest_only:
                    ud[ud_key] = new_sl
                changes[ud_key] = {
                    "old": cur_sl, "new": new_sl,
                    "reason": f"{tier} WR={round(tier_wr*100)}% — tightening SL",
                }
        elif tier_wr > 0.65 and avg_loss_x < 0.85:
            # Winning often but losses are bad — check if SL was triggered too early
            # by comparing avg loss exit vs current SL: if exit_x > 1-SL%, token recovered
            # (premature stop). Widen a little.
            new_sl = min(sl_max, cur_sl + 2.0)
            if new_sl != cur_sl:
                if not suggest_only:
                    ud[ud_key] = new_sl
                changes[ud_key] = {
                    "old": cur_sl, "new": new_sl,
                    "reason": f"{tier} WR={round(tier_wr*100)}% but losses at avg {round(avg_loss_x,2)}x — widening SL",
                }

    # ── 6. Trail activate threshold ───────────────────────────────────────────
    # If winning trades consistently peak well above the current trail_activate
    # value, raising it lets winners run longer before the trail kicks in and
    # tightens. If they peak close to activate, it's already optimal.
    cur_trail_x = ud.get("apex_trail_activate_x_learned", APEX_TRAIL_ACTIVATE_X)
    winning_peaks = [t.get("peak_x", 1.0) for t in wins if t.get("peak_x", 1.0) > 1.0]
    if len(winning_peaks) >= 8:
        avg_win_peak = sum(winning_peaks) / len(winning_peaks)
        # If average winner peaks at 3x+ and current trail activates at 1.5x,
        # we might be tightening the trail before the move fully plays out.
        # Step the activate threshold up toward avg_win_peak * 0.5 (halfway).
        ideal = round(min(avg_win_peak * 0.50, 3.0), 1)   # cap at 3x
        if ideal > cur_trail_x + 0.2:
            new_trail_x = round(min(cur_trail_x + 0.2, ideal), 1)
            if not suggest_only:
                ud["apex_trail_activate_x_learned"] = new_trail_x
            changes["apex_trail_activate_x_learned"] = {
                "old": cur_trail_x, "new": new_trail_x,
                "reason": f"Avg winner peaks at {round(avg_win_peak,2)}x — lifting trail activate",
            }
        elif ideal < cur_trail_x - 0.2 and wr < 0.40:
            # Low WR + winners peaking lower — trail activate too high, lower it
            new_trail_x = round(max(cur_trail_x - 0.2, APEX_TRAIL_ACTIVATE_X), 1)
            if not suggest_only:
                ud["apex_trail_activate_x_learned"] = new_trail_x
            changes["apex_trail_activate_x_learned"] = {
                "old": cur_trail_x, "new": new_trail_x,
                "reason": f"Low WR {round(wr*100)}%, winners peaking at {round(avg_win_peak,2)}x — lowering trail activate",
            }

    # ── 7. Position size multiplier ───────────────────────────────────────────
    # Scale up when winning, scale back when losing streaks accumulate.
    cur_mult = ud.get("apex_size_mult", 1.0)
    recent_5 = recent[-5:]   # last 5 trades for size decisions (more reactive)
    if len(recent_5) >= 5:
        wr5 = sum(1 for t in recent_5 if t.get("pnl", 0) > 0) / 5
        if wr5 >= 0.80 and cur_mult < 1.4:
            new_mult = min(1.4, round(cur_mult + 0.1, 2))
            ud["apex_size_mult"] = new_mult
            changes["apex_size_mult"] = {
                "old": cur_mult, "new": new_mult,
                "reason": f"Last-5 WR={int(wr5*100)}% — scaling up size",
            }
        elif wr5 <= 0.20 and cur_mult > 0.6:
            new_mult = max(0.6, round(cur_mult - 0.15, 2))
            ud["apex_size_mult"] = new_mult
            changes["apex_size_mult"] = {
                "old": cur_mult, "new": new_mult,
                "reason": f"Last-5 WR={int(wr5*100)}% — scaling back size",
            }
        elif 0.40 <= wr5 <= 0.60 and cur_mult != 1.0 and abs(cur_mult - 1.0) > 0.1:
            # WR near 50%: drift back toward 1.0
            new_mult = round(cur_mult + (0.05 if cur_mult < 1.0 else -0.05), 2)
            ud["apex_size_mult"] = new_mult
            changes["apex_size_mult"] = {
                "old": cur_mult, "new": new_mult,
                "reason": f"WR normalising — returning size mult toward 1.0",
            }

    # ── 9. Trail width calibration ───────────────────────────────────────────
    # Look at apex_trail_exit trades only. Compute giveback ratio:
    #   giveback = (peak_x - outcome_x) / peak_x
    # If avg giveback > 35% → trail is too tight (we give back too much before exit)
    # If avg giveback < 12% → trail may be too wide (exiting too early before peak)
    # Adjust each zone ±0.01 per calibration cycle, clamped to safe ranges.
    trail_exits = [t for t in recent if t.get("reason") == "apex_trail_exit"]
    if len(trail_exits) >= 5:
        for zone_key, ud_key, default_val, z_min, z_max in [
            ("early", "apex_trail_pct_learned_early", APEX_TRAIL_PCT_EARLY, 0.15, 0.30),
            ("mid",   "apex_trail_pct_learned_mid",   APEX_TRAIL_PCT_MID,   0.10, 0.22),
            ("high",  "apex_trail_pct_learned_high",  APEX_TRAIL_PCT_HIGH,  0.08, 0.18),
            ("moon",  "apex_trail_pct_learned_moon",  APEX_TRAIL_PCT_MOON,  0.05, 0.12),
        ]:
            cur_tpct = ud.get(ud_key, default_val)
            # Filter to zone-relevant exits based on peak_x
            zone_exits = {
                "early": [t for t in trail_exits if t.get("peak_x", 0) < 2.0],
                "mid":   [t for t in trail_exits if 2.0 <= t.get("peak_x", 0) < 5.0],
                "high":  [t for t in trail_exits if 5.0 <= t.get("peak_x", 0) < 10.0],
                "moon":  [t for t in trail_exits if t.get("peak_x", 0) >= 10.0],
            }[zone_key]
            if len(zone_exits) < 3:
                continue
            # Giveback: how much of the peak gain we gave back before trail fired
            givebacks = []
            for t in zone_exits:
                pk = t.get("peak_x", 1.0)
                ox = t.get("outcome_x", pk)
                if pk > 1.0:
                    givebacks.append((pk - ox) / pk)
            if not givebacks:
                continue
            avg_giveback = sum(givebacks) / len(givebacks)
            new_tpct = cur_tpct
            if avg_giveback > 0.35:
                # Giving back > 35% of gain before exit — trail is too tight, widen
                new_tpct = round(min(z_max, cur_tpct + 0.01), 3)
            elif avg_giveback < 0.12:
                # Giving back < 12% — trail may be letting us exit too early, tighten
                new_tpct = round(max(z_min, cur_tpct - 0.01), 3)
            if new_tpct != cur_tpct:
                if not suggest_only:
                    ud[ud_key] = new_tpct
                changes[ud_key] = {
                    "old": cur_tpct, "new": new_tpct,
                    "reason": f"{zone_key} zone avg giveback {round(avg_giveback*100,1)}% "
                              f"({'widening' if new_tpct > cur_tpct else 'tightening'} trail)",
                }

    # ── 8. Losing hour detection ──────────────────────────────────────────────
    # Group trades by UTC entry hour. Flag hours with >=3 trades AND net loss.
    hour_pnl = {}
    for t in recent:
        hr = t.get("entry_hour")
        if hr is not None:
            hour_pnl.setdefault(hr, []).append(t.get("pnl", 0))
    losing_hours = sorted([
        hr for hr, pnls in hour_pnl.items()
        if len(pnls) >= 3 and sum(pnls) < 0
    ])
    if losing_hours:
        if not suggest_only:
            ud["apex_avoid_hours"] = losing_hours
        changes["apex_avoid_hours"] = {
            "old": ud.get("apex_avoid_hours", []),
            "new": losing_hours,
            "reason": f"Hours {losing_hours} UTC consistently net-losing",
        }
    elif not losing_hours and ud.get("apex_avoid_hours"):
        # Remove stale hour blocks if no data supports them anymore
        if not suggest_only:
            ud.pop("apex_avoid_hours", None)

    # ── 10. Pattern memory by token type ────────────────────────────────────
    # Tracks win rate and avg peak per token type (pumpfun/graduated/kol/organic).
    try:
        _pm = ud.setdefault("apex_pattern_memory", {})
        for _ttype in ("pumpfun", "graduated", "kol", "organic"):
            _type_trades = [t for t in recent if t.get("token_type", "organic") == _ttype]
            if len(_type_trades) < 3:
                continue
            _type_wins  = [t for t in _type_trades if t.get("pnl", 0) > 0]
            _type_wr    = round(len(_type_wins) / len(_type_trades), 3)
            _type_peaks = [t.get("peak_x", 1.0) for t in _type_trades]
            _type_avg_peak = round(sum(_type_peaks) / len(_type_peaks), 2)
            _type_avg_pnl  = round(sum(t.get("pnl", 0) for t in _type_trades) / len(_type_trades), 2)
            _pm[_ttype] = {
                "trades":   len(_type_trades),
                "wr":       _type_wr,
                "avg_peak": _type_avg_peak,
                "avg_pnl":  _type_avg_pnl,
            }
            if _type_wr < 0.30 and len(_type_trades) >= 5:
                changes[f"pattern_{_ttype}"] = {
                    "old": None, "new": _type_wr,
                    "reason": f"{_ttype} WR={round(_type_wr*100)}% on {len(_type_trades)} trades — consistently losing",
                }
    except Exception as _pme:
        logger.debug(f"Pattern memory error: {_pme}")

    # ── Log calibration run ───────────────────────────────────────────────────
    if changes:
        import time as _calt
        ud.setdefault("apex_calibration_log", []).append({
            "ts":      _calt.time(),
            "trades":  n,
            "wr":      round(wr, 3),
            "changes": changes,
        })
        # Keep only last 20 calibration events
        if len(ud["apex_calibration_log"]) > 20:
            ud["apex_calibration_log"] = ud["apex_calibration_log"][-20:]
        logger.info(f"APEX calibrate uid={uid}: {list(changes.keys())}")

    return changes


# ══════════════════════════════════════════════════════════════════════════════
# APEX ASYNC FUNCTIONS (need bot context — injected into bot.py scope)
# ══════════════════════════════════════════════════════════════════════════════

async def apex_run_position_manager(app, uid: int, ud: dict, positions_due: list = None) -> None:
    from datetime import timedelta
    async with _get_user_lock(uid):
        for contract, h in list(ud.get("holdings", {}).items()):
            if h.get("mood") not in ("APEX", "AI-Sniper", "APEX-DCA"):
                continue
            # Skip if not due for check this cycle (adaptive interval)
            if positions_due is not None and contract not in positions_due:
                continue
            # ── HOLDING SANITIZER ─────────────────────────────────────────
            # Every numeric field used in arithmetic inside this loop MUST be
            # a float/int. If entry setup ever crashed, these can be None,
            # causing "NoneType + float" which kills the entire loop iteration
            # via the except block — meaning SL and trail NEVER execute.
            # This block runs before any arithmetic, every cycle, zero cost.
            def _n(val, default=0.0):
                """Return val if it is a real number, else default."""
                return val if isinstance(val, (int, float)) and val == val else default

            avg_price_raw = h.get("avg_price") or h.get("price") or 0.0
            h["avg_price"]          = _n(h.get("avg_price"),          avg_price_raw)
            h["amount"]             = _n(h.get("amount"),             0.0)
            h["total_invested"]     = _n(h.get("total_invested"),     0.0)
            h["total_sold"]         = _n(h.get("total_sold"),         0.0)
            h["avg_cost_mc"]        = _n(h.get("avg_cost_mc"),        0.0)
            h["liq_at_buy"]         = _n(h.get("liq_at_buy"),         0.0)
            h["apex_vault_reserved"]= _n(h.get("apex_vault_reserved"),0.0)
            h["sr_peak_vol"]        = _n(h.get("sr_peak_vol"),        0.0)
            h["sr_peak_visit_vol"]  = _n(h.get("sr_peak_visit_vol"),  0.0)
            h["apex_dca_count"]     = _n(h.get("apex_dca_count"),     0)
            h["apex_last_dca_ts"]   = _n(h.get("apex_last_dca_ts"),   0.0)
            h["apex_entry_score"]   = _n(h.get("apex_entry_score"),   0)
            h["apex_entry_conf"]    = _n(h.get("apex_entry_conf"),    0)
            h["apex_hunter_floor"]  = _n(h.get("apex_hunter_floor"),  0.0)
            h["apex_threat"]        = h.get("apex_threat") or "CLEAR"
            h["apex_trail_pct"]     = _n(h.get("apex_trail_pct"),     0.15)
            # apex_peak_price: must be >= avg_price
            _avg = h["avg_price"]
            h["apex_peak_price"]    = max(_avg, _n(h.get("apex_peak_price"), _avg))
            # stop_loss_pct: critical — derive from rug risk if missing
            if not _n(h.get("stop_loss_pct"), 0.0):
                _rug_sl = h.get("apex_entry_rug", "LOW")
                h["stop_loss_pct"] = {"LOW": 22.0, "MEDIUM": 18.0, "HIGH": 12.0}.get(_rug_sl, 20.0)
                logger.warning(f"APEX position manager: assigned fallback SL {h['stop_loss_pct']}% to {h.get('symbol','?')} ({contract[:8]})")
            # apex_vault_locked: must be a dict
            if not isinstance(h.get("apex_vault_locked"), dict):
                h["apex_vault_locked"] = {}
            # sr_history: must be a list
            if not isinstance(h.get("sr_history"), list):
                h["sr_history"] = []
            try:
                info = await get_token(contract)
                if not info:
                    continue
                # ── Helius RPC: faster live price + on-chain rug signal ────
                _helius_key = os.environ.get("HELIUS_API_KEY", "")
                if _helius_key and info.get("chain","").lower() in ("solana","sol"):
                    _pair = h.get("pair_addr", "") or info.get("pair_addr", "")
                    if _pair:
                        _live = await get_helius_pool_price(contract, _pair, _helius_key)
                        if _live.get("price"):
                            info["price"] = _live["price"]
                            info["liq"]   = _live.get("liq", info["liq"])
                            # ── Write Helius price back to shared cache so all
                            # callers in this cycle see the same price, not stale
                            # DexScreener data from up to 12s ago.
                            if contract in _token_cache:
                                _token_cache[contract]["data"]["price"] = info["price"]
                                _token_cache[contract]["data"]["liq"]   = info["liq"]
                    # Check for on-chain liquidity removal (instant rug signal)
                    _rug_sig = await get_helius_rug_signal(contract, _helius_key)
                    if _rug_sig.get("rug_detected"):
                        logger.warning(f"Helius rug signal: {contract} — {_rug_sig['reason']}")
                        info["_helius_rug"] = True
                # ── Normalise info fields — DexScreener/Helius can return None
                # for vol_m5, buy_pct_m5, mc etc. Any None reaching arithmetic
                # causes "NoneType + float" which kills the entire loop iteration.
                def _nf(v, d=0.0): return v if isinstance(v, (int, float)) else d
                info["price"]       = _nf(info.get("price"), 0.0)
                info["mc"]          = _nf(info.get("mc"), 0.0)
                info["liq"]         = _nf(info.get("liq"), 0.0)
                info["vol_m5"]      = _nf(info.get("vol_m5"), 0.0)
                info["vol_h1"]      = _nf(info.get("vol_h1"), 0.0)
                info["vol_h24"]     = _nf(info.get("vol_h24"), 0.0)
                info["buy_pct"]     = _nf(info.get("buy_pct"), 50.0)
                info["buy_pct_m5"]  = _nf(info.get("buy_pct_m5"), info["buy_pct"])
                info["buy_pct_h1"]  = _nf(info.get("buy_pct_h1"), info["buy_pct"])
                info["buys_m5"]     = _nf(info.get("buys_m5"), 0)
                info["sells_m5"]    = _nf(info.get("sells_m5"), 0)
                info["age_h"]       = _nf(info.get("age_h"), 0.0)
                info["liq_pct"]     = _nf(info.get("liq_pct"), 0.0)

                price = info["price"]
                avg   = h.get("avg_price", price)
                if avg <= 0:
                    continue
                # Skip if position already fully exited (race condition guard)
                if h.get("amount", 0) <= 0:
                    continue
                cx = price / avg
                if price > h.get("apex_peak_price", avg):
                    h["apex_peak_price"] = price
                peak   = h.get("apex_peak_price", price)
                # Read the profile the position was opened with — not the current setting.
                # This prevents Hunter floors being applied by Default logic (or vice versa)
                # if the user switches profiles or Hunter is suspended mid-position.
                _stored_profile = h.get("apex_profile_at_entry", "default")
                if APEX_HUNTER_SUSPENDED or _stored_profile == "default":
                    _hp = None
                else:
                    _hp = APEX_PROFILES.get(_stored_profile)   # None = default, dict = hunter
                threat = apex_check_threat(info, h)

                _prev_threat = h.get("apex_threat", "CLEAR")
                # Reset bundle flush flags when threat clears ────────────
                if threat == "CLEAR" and _prev_threat in ("ORANGE", "RED"):
                    h.pop("_bundle_flushed", None)
                    h.pop("_bundle_still_selling", None)
                if threat != _prev_threat:
                    import time as _tth
                    h.setdefault("threat_history", []).append({
                        "from": _prev_threat, "to": threat,
                        "cx": round(cx, 3), "price": price, "ts": _tth.time(),
                    })
                h["apex_threat"] = threat

                apex_sr_record_candle(h, price, info.get("mc", 0),
                                      info.get("vol_m5", 0),
                                      info.get("buy_pct_m5", info.get("buy_pct", 50)))

                # ── #10 SOL macro awareness: tighten trails on crash ────────
                # If SOL dropped >4% in 30 min, all open positions are at
                # higher risk of following SOL down. Tighten trail by 5%
                # once per position per crash event (guarded by flag).
                if _sol_bearish and not h.get("_sol_crash_tightened"):
                    h["_sol_crash_tightened"] = True
                    # Store tighten multiplier on ud so apex_trail_pct(cx, ud) picks it up.
                    # Use a 0.80 multiplier (tighten by ~20%) — undone when SOL recovers.
                    ud["_sol_trail_mult"] = 0.80
                    logger.info(f"SOL macro: trail tightened 20%% for all positions (SOL bearish)")
                elif not _sol_bearish and h.get("_sol_crash_tightened"):
                    # SOL recovered — reset flag and multiplier
                    h["_sol_crash_tightened"] = False
                    ud.pop("_sol_trail_mult", None)

                # ── Hunter ratchet: update stop-loss floor as position gains ─────
                if _hp:
                    _ratchet_floor = _hunter_sl_for_cx(h, cx, _hp)
                    if _ratchet_floor is not None:
                        # Store as an absolute price floor on the holding
                        _cur_floor = h.get("apex_hunter_floor", 0.0)
                        if _ratchet_floor > _cur_floor:
                            h["apex_hunter_floor"] = _ratchet_floor
                            _phase_label = ("break-even" if cx >= _hp["ratchet_2x"] else "near-BE")
                            if _ratchet_floor > _cur_floor + (avg * 0.02):
                                try:
                                    await app.bot.send_message(
                                        chat_id=uid, parse_mode="Markdown",
                                        text=(
                                            "🔒 *HUNTER — FLOOR LOCKED*\n\n"
                                            "*$" + _md(h["symbol"]) + "* at *" + str(round(cx, 2)) + "x*\n"
                                            "Stop floor: *" + _phase_label + "* (" + str(round((_ratchet_floor/avg - 1)*100, 1)) + "% from entry)\n"
                                            "_Capital protected. Now riding with house money._"
                                        ),
                                        reply_markup=main_menu_kb()
                                    )
                                except Exception:
                                    pass

                # ── RED: emergency exit ───────────────────────────────────────────
                # Hunter: after ratchet_1x, require 2× consecutive RED cycles
                # (protects against single-candle wick-outs on thin books)
                _is_red = (threat == "RED" or info.get("_helius_rug"))
                if _is_red:
                    _hunter_requires_2x = _hp and cx >= _hp["red_2x_start"]
                    if _hunter_requires_2x and _prev_threat != "RED":
                        # First RED — record it but don't exit yet
                        h["apex_threat"] = "RED"
                        continue

                    # ── #9 Pullback investigation: ask WHY before exiting ──────────
                    # Fetch last 100 Helius txns to identify WHO is selling.
                    # Also checks dev wallet history to distinguish dump types.
                    # Different sellers → different actions:
                    #   Dev/minted supply selling   → EXIT immediately (rug confirmed)
                    #   Bundle bag dump             → EXIT immediately (coordinated dump)
                    #   Single whale profit-take    → HOLD, tighten trail 5%
                    #   Organic thin-book pressure  → HOLD, wait 1 more cycle
                    #   No Helius / can't determine → EXIT as before (safe default)
                    _pi_key = os.environ.get("HELIUS_API_KEY", "")
                    _pi_skip_exit = False   # True = investigation says hold
                    _pi_reason_label = "Critical dump/rug signal"   # shown in exit DM
                    if _pi_key and not info.get("_helius_rug") and not _hp:
                        # Only investigate in Default mode — Hunter has ratchet floor
                        # Don't investigate Helius-confirmed rugs (LP drained)
                        _pi_last_ts = h.get("_pullback_invest_ts", 0)
                        _pi_cooldown = 24   # seconds — don't hammer Helius every 8s
                        if _time.time() - _pi_last_ts > _pi_cooldown:
                            h["_pullback_invest_ts"] = _time.time()
                            try:
                                _pi_data = await get_helius_maker_pct(contract, _pi_key)
                                if _pi_data:
                                    _pi_top3  = _pi_data.get("top3_vol_pct", 0)
                                    _pi_maker = _pi_data.get("maker_pct", 50)
                                    _pi_count = _pi_data.get("maker_count", 0)

                                    # ── Dev wallet check: is the deployer selling? ────
                                    # Fetch dev wallet history to confirm if it's a rug.
                                    _pi_dev_selling = False
                                    _pi_dev_risk    = "UNKNOWN"
                                    try:
                                        _pi_dev_data = await enrich_dev_wallet_history(contract, info, _pi_key)
                                        _pi_dev_risk = _pi_dev_data.get("dev_risk", "UNKNOWN")
                                        # HIGH dev risk + concentrated selling = dev dump confirmed
                                        if _pi_dev_risk == "HIGH" and _pi_top3 >= 40:
                                            _pi_dev_selling = True
                                    except Exception:
                                        pass

                                    # ── Case A: Dev wallet dumping ────────────────────
                                    if _pi_dev_selling:
                                        h["_pullback_reason"] = f"dev_dump:risk={_pi_dev_risk},top3={_pi_top3}%"
                                        _pi_reason_label = (
                                            "🚨 Dev wallet selling confirmed\n"
                                            "Top wallets: *" + str(_pi_top3) + "%* of volume\n"
                                            "Dev risk: *" + _pi_dev_risk + "*"
                                        )
                                        logger.info(f"Pullback [{h['symbol']}]: DEV DUMP confirmed — top3={_pi_top3}%, dev_risk={_pi_dev_risk}, exiting")

                                    # ── Case B: Bundle/coordinated dump ──────────────
                                    elif _pi_top3 >= 60:
                                        h["_pullback_reason"] = f"bundle_dump:top3={_pi_top3}%"
                                        _pi_reason_label = (
                                            "🚨 Bundle/coordinated dump\n"
                                            "Top 3 wallets = *" + str(_pi_top3) + "%* of sell volume\n"
                                            "Concentrated exit — not organic"
                                        )
                                        logger.info(f"Pullback [{h['symbol']}]: bundle/concentrated dump top3={_pi_top3}%, exiting")

                                    # ── Case C: Whale profit-take ─────────────────────
                                    elif _pi_maker >= 55 and _pi_top3 < 50:
                                        _pi_skip_exit = True
                                        h["apex_trail_pct"] = max(0.04, h.get("apex_trail_pct", 0.22) - 0.05)
                                        h["_pullback_reason"] = f"whale_profit_take:top3={_pi_top3}%,makers={_pi_maker}%"
                                        logger.info(f"Pullback [{h['symbol']}]: whale profit-take, trail tightened, holding")
                                        try:
                                            await app.bot.send_message(
                                                chat_id=uid, parse_mode="Markdown",
                                                text=(
                                                    "🔍 *APEX — PULLBACK INVESTIGATION*\n\n"
                                                    "*$" + _md(h["symbol"]) + "*  " + str(round(cx, 2)) + "x\n"
                                                    "─────────────────────\n"
                                                    "🐋 *Whale profit-taking detected*\n"
                                                    "Top wallets: *" + str(_pi_top3) + "%* of volume\n"
                                                    "Unique buyers still: *" + str(_pi_maker) + "%*\n\n"
                                                    "✅ *Decision: HOLDING*\n"
                                                    "_Single large exit — rest of market still buying._\n"
                                                    "Trail tightened by 5% to protect gains."
                                                ),
                                                reply_markup=main_menu_kb()
                                            )
                                        except Exception:
                                            pass

                                    # ── Case D: Organic thin-book pressure ────────────
                                    elif _pi_maker >= 45 and _pi_top3 < 35:
                                        _pi_skip_exit = True
                                        h["_pullback_reason"] = f"organic_pressure:makers={_pi_maker}%"
                                        logger.info(f"Pullback [{h['symbol']}]: organic thin-book pressure, holding 1 cycle")
                                        try:
                                            await app.bot.send_message(
                                                chat_id=uid, parse_mode="Markdown",
                                                text=(
                                                    "🔍 *APEX — PULLBACK INVESTIGATION*\n\n"
                                                    "*$" + _md(h["symbol"]) + "*  " + str(round(cx, 2)) + "x\n"
                                                    "─────────────────────\n"
                                                    "📊 *Organic thin-book dip*\n"
                                                    "Selling is distributed: *" + str(_pi_maker) + "%* unique wallets\n"
                                                    "No single whale or dev detected\n\n"
                                                    "✅ *Decision: HOLDING 1 cycle*\n"
                                                    "_Distributed selling = normal micro-cap volatility._\n"
                                                    "Re-evaluating next cycle."
                                                ),
                                                reply_markup=main_menu_kb()
                                            )
                                        except Exception:
                                            pass

                            except Exception as _pie:
                                logger.debug(f"Pullback investigation error: {_pie}")

                    if _pi_skip_exit:
                        # Investigation says hold — skip exit this cycle
                        # RED threat is still set so next cycle re-evaluates
                        save_user(uid, ud)
                        continue
                    # Exit: either Default mode, or 2nd consecutive RED, or helius rug
                    cv     = h["amount"] * price
                    result = sell_core(ud, uid, contract, cv, price, "apex_threat_red")
                    ud["apex_daily_pnl"] = (ud.get("apex_daily_pnl") or 0.0) + (result.get("realized") or 0.0)
                    if result["realized"] < 0:
                        ud["apex_consec_losses"] = ud.get("apex_consec_losses", 0) + 1
                        if ud["apex_consec_losses"] >= 3:
                            _apex_paused_until[uid] = datetime.now() + timedelta(minutes=APEX_DRAWDOWN_3_PAUSE)
                    else:
                        ud["apex_consec_losses"] = 0
                        ud["apex_total_wins"] = ud.get("apex_total_wins", 0) + 1
                    ud["apex_total_trades"] = ud.get("apex_total_trades", 0) + 1
                    apex_learn_record(uid, _apex_rich_record(h, cx, peak, avg, result["realized"], "apex_threat_red", ud))
                    # ── Self-calibrate on RED exits too, not just trail exits.
                    # Rug/dump losses are the most important signal for the learning engine.
                    _cal_freq = 5 if apex_get_phase(ud) == "learning" else 10
                    if ud.get("apex_total_trades", 0) % _cal_freq == 0:
                        apex_self_calibrate(ud, uid, suggest_only=False)
                    _apex_last_check.get(uid, {}).pop(contract, None)
                    # ── Register post-exit tracker ────────────────────────────
                    _apex_post_exit.setdefault(uid, {})[contract] = {
                        "symbol":      h["symbol"],
                        "contract":    contract,
                        "exit_price":  price,
                        "entry_price": avg,
                        "exit_reason": "apex_threat_red",
                        "exit_x":      round(cx, 3),
                        "peak_x":      round(peak / avg, 3) if avg > 0 else 0,
                        "held_h":      round((h.get("bought_at") and (_time.time() - h["bought_at"].timestamp())) / 3600, 2) if h.get("bought_at") else 0,
                        "entry_mc":    h.get("avg_cost_mc", 0),
                        "invested":    h.get("total_invested", 0),
                        "exit_at":     _time.time(),
                        "snapshots":   [],
                    }
                    # ── Register re-entry watchlist ───────────────────────────
                    # ONLY watchlist RED exits that look recoverable:
                    #   • Token didn't collapse >60% (cx >= 0.40)
                    #   • MC still alive (>= $20K at exit) — rules out rugs/dead tokens
                    #   • Liquidity not drained (>= $500 at exit)
                    # A TRUE rug (Lenny-style, MC=$2K at exit) must NEVER be watchlisted.
                    # Also: NEVER watchlist a position that was itself a watchlist re-entry
                    #   — this breaks chained re-buys (Monk ×4, WSL ×2, etc.)
                    _now_exit_ts = _time.time()
                    _exit_liq    = info.get("liq", 0)
                    _exit_mc     = info.get("mc", 0)
                    _watchlist_eligible = (
                        not h.get("apex_watchlist_reentry", False)  # no chained re-entries
                        and round(cx, 3) >= 0.40          # didn't collapse >60%
                        and _exit_mc  >= 20_000            # MC still alive (not a rug)
                        and _exit_liq >= 500               # liquidity not drained
                    )
                    if _watchlist_eligible:
                        _apex_watchlist.setdefault(uid, {})[contract] = {
                            "symbol":          h["symbol"],
                            "exit_price":      price,
                            "exit_liq":        _exit_liq,
                            "entry_price":     avg,
                            "exit_reason":     "apex_threat_red",
                            "exit_x":          round(cx, 3),
                            "exit_at":         _time.time(),
                            "bottom_price":    price,
                            "bottom_ts":       _time.time(),
                            "reversal_alerted":False,
                            "re_entry_queued": False,
                            "last_check_ts":   0.0,
                            "status":          "watching",
                            "entry_score":     h.get("apex_entry_score", 0),
                            "entry_conf":      h.get("apex_entry_conf", 0),
                        }
                    _sent_red = round(result["realized"] * ud.get("apex_vault_profit_split",0.50), 4) if result["realized"] > 0 else 0.0
                    try:
                        await app.bot.send_message(chat_id=uid, parse_mode="Markdown",
                            text=("*\U0001f6a8 APEX \u2014 THREAT RED \u2014 EMERGENCY EXIT*\n\n"
                                  "*$" + _md(h["symbol"]) + "*\n"
                                  "Threat: *\U0001f534 " + _pi_reason_label + "*\n"
                                  "Exit: *" + str(round(cx, 2)) + "x*  |  PnL: *" + pstr(result["realized"]) + "*"
                                  + (" | Fee: *" + money(h.get("total_fees_paid",0)) + "*" if SIM_FEES_ENABLED else "") + "\n"
                                  + ("\U0001f4b8 *" + money(_sent_red) + "* \u2192 Main Balance\n" if _sent_red > 0 else "")
                                  + "\U0001f3e6 Vault: *" + money(ud.get("apex_vault",0)) + "*  |  Cash: *" + money(ud["balance"]) + "*"),
                            reply_markup=main_menu_kb())
                    except Exception:
                        pass
                    # ── Vault exhaustion check ────────────────────────────────
                    try:
                        await _check_apex_vault_exhausted(app.bot, uid, ud)
                    except Exception:
                        pass
                    continue

                # ── Exit ladder: partial sells at 1.3x and 2x ────────────────────
                # Sell 25% of the position at 1.3x and 25% at 2x to lock real cash
                # before the trail can give it back. The remaining ~50% trails freely.
                # Each milestone fires once per position (guarded by ladder flags).
                # The 1.3x sell also moves the SL to break-even (complements fix #1).
                # Only fires in Default mode (_hp is None) — Hunter has its own ratchet.
                if not _hp:
                    _ladder_amt = h.get("amount", 0)
                    _ladder_cv  = _ladder_amt * price
                    # Use user-configured ladder settings (defaults: 2x trigger, 50% sell)
                    _lad_trigger = ud.get("apex_ladder_trigger_x", 2.0)
                    _lad_sell    = ud.get("apex_ladder_sell_pct", 0.50)

                    # ── Ladder sell at user-configured trigger ────────────────
                    if cx >= _lad_trigger and not h.get("apex_ladder_sold_2x") and _ladder_cv >= 0.02:
                        _sell_val = _ladder_cv * _lad_sell
                        if _sell_val >= 0.01:
                            _lad_result = sell_core(ud, uid, contract, _sell_val, price, "apex_ladder_2x")
                            if isinstance(_lad_result, dict):
                                h["apex_ladder_sold_2x"] = True
                                ud["apex_daily_pnl"] = (ud.get("apex_daily_pnl") or 0.0) + (_lad_result.get("realized") or 0.0)
                                _lad_pnl = _lad_result.get("realized", 0)
                                _lad_sold_usd = round(_sell_val, 2)
                                _lad_pct_label = str(int(_lad_sell * 100))
                                try:
                                    await app.bot.send_message(
                                        chat_id=uid, parse_mode="Markdown",
                                        text=(
                                            "💰 *APEX — LADDER SELL (" + str(_lad_trigger) + "x)*\n\n"
                                            "*$" + _md(h["symbol"]) + "*  hit *" + str(_lad_trigger) + "x*\n"
                                            "Sold *" + _lad_pct_label + "%* (~" + money(_lad_sold_usd) + ") → locked *" + pstr(_lad_pnl) + "*\n"
                                            "Remaining *" + str(100 - int(_lad_sell * 100)) + "%* trailing — riding profits…"
                                        ),
                                        reply_markup=main_menu_kb()
                                    )
                                except Exception:
                                    pass

                # ── Profit lock milestones at 2x and 5x ──────────────────────────
                # DESIGN NOTE: These milestones are RESERVATIONS, not cash transfers.
                # We record the intended lock amount on the holding in
                # h["apex_vault_reserved"] so sell_core can credit the real vault
                # proportionally when the position closes. ud["apex_vault"] must
                # never be incremented here — that would create phantom money if
                # the position later loses back the gain before closing.
                vault_locks = h.setdefault("apex_vault_locked", {})
                cv          = h["amount"] * price
                entry_val   = h.get("total_invested", cv)
                if cx >= 5.0 and "5x" not in vault_locks:
                    profit   = cv - entry_val
                    lock_amt = max(0.0, profit * APEX_LOCK_5X_PCT)
                    if lock_amt > 0:
                        vault_locks["5x"] = lock_amt
                        # Store total reserved on holding for sell_core to consume
                        h["apex_vault_reserved"] = (h.get("apex_vault_reserved") or 0.0) + (lock_amt or 0.0)
                        try:
                            await app.bot.send_message(chat_id=uid, parse_mode="Markdown",
                                text=("\U0001f3e6 *APEX \u2014 PROFIT MILESTONE (5x)*\n\n"
                                      "*$" + _md(h["symbol"]) + "* hit *5x*!\n"
                                      "Reserving: *" + money(lock_amt) + "* \u2192 Vault on close\n"
                                      "\U0001f512 Will lock when position closes\n"
                                      "Trail continues\u2026"),
                                reply_markup=main_menu_kb())
                        except Exception:
                            pass
                elif cx >= 2.0 and "2x" not in vault_locks:
                    profit   = cv - entry_val
                    lock_amt = max(0.0, profit * APEX_LOCK_2X_PCT)
                    if lock_amt > 0:
                        vault_locks["2x"] = lock_amt
                        # Store total reserved on holding for sell_core to consume
                        h["apex_vault_reserved"] = (h.get("apex_vault_reserved") or 0.0) + (lock_amt or 0.0)
                        try:
                            await app.bot.send_message(chat_id=uid, parse_mode="Markdown",
                                text=("\U0001f3e6 *APEX \u2014 PROFIT MILESTONE (2x)*\n\n"
                                      "*$" + _md(h["symbol"]) + "* hit *2x*!\n"
                                      "Reserving: *" + money(lock_amt) + "* \u2192 Vault on close\n"
                                      "\U0001f512 Will lock when position closes\n"
                                      "Trail now active \u2014 riding the move\u2026"),
                                reply_markup=main_menu_kb())
                        except Exception:
                            pass

                # ── Gap 4: Support break alert + partial exit ─────────────────
                # If price breaks below a confirmed support zone (one that held
                # before), sell 30% immediately and fire a DM alert.
                # Only fires once per position (guarded by flag).
                # Skip if no profitable support to speak of (cx < 0.90).
                if cx < 0.98 and cx >= 0.70 and not _hp and not h.get("_sr_support_break_fired"):
                    _sr_zones_sb  = apex_sr_compute_zones(h)
                    _sup_mc       = _sr_zones_sb.get("active_support")
                    if _sup_mc and _sup_mc > 0:
                        _mc_now_sb  = info.get("mc", 0)
                        _bpm5_sb    = info.get("buy_pct_m5", info.get("buy_pct", 50))
                        # Support break: price is now BELOW the support zone
                        _below_sup  = _mc_now_sb < _sup_mc * 0.97   # 3% below support
                        _sell_press = _bpm5_sb < 40
                        if _below_sup and _sell_press:
                            h["_sr_support_break_fired"] = True
                            _sb_cv = h["amount"] * price * 0.30
                            if _sb_cv >= 0.01:
                                _sb_result = sell_core(ud, uid, contract, _sb_cv, price, "apex_support_break")
                                ud["apex_daily_pnl"] = (ud.get("apex_daily_pnl") or 0.0) + (_sb_result.get("realized") or 0.0)
                                logger.info(f"Support break [{h['symbol']}]: sold 30% at support break MC ${_mc_now_sb:,.0f} < support ${_sup_mc:,.0f}")
                                try:
                                    await app.bot.send_message(
                                        chat_id=uid, parse_mode="Markdown",
                                        text=(
                                            "\U0001f534 *APEX \u2014 SUPPORT BROKEN*\n\n"
                                            "*$" + _md(h["symbol"]) + "* broke below support\n"
                                            "Buy pressure: *" + str(round(_bpm5_sb)) + "%*\n"
                                            "Sold *30%* as protection\n"
                                            "PnL: *" + pstr(_sb_result.get("realized", 0)) + "*\n"
                                            "_Remaining 70% trailing. If RED fires \u2014 full exit._"
                                        ),
                                        reply_markup=main_menu_kb()
                                    )
                                except Exception:
                                    pass

                # ── Gap 4b: Resistance zone profit take ───────────────────────
                # When price enters a strong resistance zone and position is
                # profitable (cx >= 1.4): sell 15% to lock some profit.
                # Different from ladder sells (fixed at 1.3x/2x) — this is
                # zone-aware and fires based on where sellers are waiting.
                if cx >= 1.4 and not _hp and not h.get("_sr_resistance_partial_taken"):
                    _sr_zones_rp  = apex_sr_compute_zones(h)
                    _res_mc_rp    = _sr_zones_rp.get("active_resistance")
                    if _res_mc_rp and _res_mc_rp > 0:
                        _mc_now_rp  = info.get("mc", 0)
                        _dist_rp    = (_res_mc_rp - _mc_now_rp) / _mc_now_rp if _mc_now_rp > 0 else 1.0
                        _res_zone   = _sr_zones_rp["resistance_zones"][0] if _sr_zones_rp["resistance_zones"] else None
                        # Only fire on strong zones (strength >= 6) at proximity
                        if (_dist_rp < APEX_SR_ZONE_PROXIMITY * 0.5
                                and _res_zone and _res_zone.get("strength", 0) >= 6):
                            h["_sr_resistance_partial_taken"] = True
                            _rp_cv = h["amount"] * price * 0.15
                            if _rp_cv >= 0.01:
                                _rp_result = sell_core(ud, uid, contract, _rp_cv, price, "apex_resistance_partial")
                                ud["apex_daily_pnl"] = (ud.get("apex_daily_pnl") or 0.0) + (_rp_result.get("realized") or 0.0)
                                logger.info(f"Resistance partial [{h['symbol']}]: sold 15% at strong resistance zone")
                                try:
                                    await app.bot.send_message(
                                        chat_id=uid, parse_mode="Markdown",
                                        text=(
                                            "\U0001f4ca *APEX \u2014 RESISTANCE ZONE SELL*\n\n"
                                            "*$" + _md(h["symbol"]) + "* at strong resistance\n"
                                            "Zone strength: *" + str(round(_res_zone["strength"], 1)) + "/10*\n"
                                            "Sold *15%* (~" + money(round(_rp_cv, 2)) + ") to lock profit at zone\n"
                                            "PnL: *" + pstr(_rp_result.get("realized", 0)) + "*\n"
                                            "_Remaining position trailing._"
                                        ),
                                        reply_markup=main_menu_kb()
                                    )
                                except Exception:
                                    pass

                # ── #11 Token age decay: exit dead positions ─────────────────
                # After 3h of hold time: if vol_m5 < 10% of hourly average
                # the token is dead (capital trapped). Exit cleanly.
                # Skip if already profitable (trail will handle it) or if
                # token is still showing signs of life.
                if h.get("bought_at"):
                    try:
                        _age_held_h = (_time.time() - h["bought_at"].timestamp()) / 3600
                        if _age_held_h >= 3.0 and cx < 1.5:   # only if not profitable
                            _vol_m5_now  = info.get("vol_m5", 0)
                            _vol_h1_now  = info.get("vol_h1", 1)
                            _avg_5m_vol  = _vol_h1_now / 12.0 if _vol_h1_now > 0 else 0
                            _vol_dead    = _avg_5m_vol > 0 and _vol_m5_now < _avg_5m_vol * 0.10
                            if _vol_dead:
                                _cv_dead = h["amount"] * price
                                if _cv_dead >= 0.01:
                                    _res_dead = sell_core(ud, uid, contract, _cv_dead, price, "apex_age_decay")
                                    if isinstance(_res_dead, dict):
                                        ud["apex_daily_pnl"] = (ud.get("apex_daily_pnl") or 0.0) + (_res_dead.get("realized") or 0.0)
                                    if isinstance(_res_dead, dict) and _res_dead.get("realized", 0) < 0:
                                        ud["apex_consec_losses"] = ud.get("apex_consec_losses", 0) + 1
                                    else:
                                        ud["apex_consec_losses"] = 0
                                        ud["apex_total_wins"] = ud.get("apex_total_wins", 0) + 1
                                    ud["apex_total_trades"] = ud.get("apex_total_trades", 0) + 1
                                    apex_learn_record(uid, _apex_rich_record(h, cx, peak, avg, _res_dead["realized"], "apex_age_decay", ud))
                                    _apex_post_exit.setdefault(uid, {})[contract] = {
                                        "symbol": h["symbol"], "contract": contract,
                                        "exit_price": price, "entry_price": avg,
                                        "exit_reason": "apex_age_decay",
                                        "exit_x": round(cx, 3),
                                        "peak_x": round(peak / avg, 3) if avg > 0 else 0,
                                        "held_h": round(_age_held_h, 2),
                                        "entry_mc": h.get("avg_cost_mc", 0),
                                        "invested": h.get("total_invested", 0),
                                        "exit_at": _time.time(), "snapshots": [],
                                    }
                                    logger.info(f"Age decay exit: ${h['symbol']} held {round(_age_held_h,1)}h, vol dead ({round(_vol_m5_now,1)} vs avg {round(_avg_5m_vol,1)})")
                                    try:
                                        await app.bot.send_message(
                                            chat_id=uid, parse_mode="Markdown",
                                            text=(
                                                "\u23f0 *APEX \u2014 DEAD VOLUME EXIT*\n\n"
                                                "*$" + _md(h["symbol"]) + "* held *" + str(round(_age_held_h, 1)) + "h* with dead volume\n"
                                                "Vol M5: *" + money(_vol_m5_now) + "* (avg: " + money(_avg_5m_vol) + ")\n"
                                                "PnL: *" + pstr(_res_dead["realized"]) + "*\n"
                                                "_Capital freed from dead token._"
                                            ),
                                            reply_markup=main_menu_kb()
                                        )
                                    except Exception:
                                        pass
                                    continue
                            else:
                                # Volume still alive after 3h — token is just slow.
                                # Widen the trail by 20% to give it more room (#11 spec).
                                # Only widen once per position (guarded by flag).
                                if not h.get("_age_trail_widened"):
                                    h["_age_trail_widened"] = True
                                    _cur_tpct = h.get("apex_trail_pct", APEX_TRAIL_PCT_EARLY)
                                    h["apex_trail_pct"] = round(min(_cur_tpct * 1.20, 0.35), 3)
                                    logger.info(f"Age trail widened [{h['symbol']}]: {round(_age_held_h,1)}h old, vol alive — trail {_cur_tpct:.2f} → {h['apex_trail_pct']:.2f}")
                    except Exception as _ade:
                        logger.debug(f"Age decay check error: {_ade}")

                # ── Trail-activate threshold — defined HERE so both the SL block
                # (cx < _trail_activate guard) and the trailing-stop block below
                # can reference the same value without a NameError.
                _trail_activate = _hp["trail_activate_x"] if _hp else ud.get("apex_trail_activate_x_learned", APEX_TRAIL_ACTIVATE_X)

                # ── Stop-loss: both Default pct SL and Hunter Phase-0 protection ─
                # run_checker skips APEX moods — we own all stop-loss logic here.
                sl_pct        = h.get("stop_loss_pct")
                _hunter_floor = h.get("apex_hunter_floor", 0.0)
                _sl_triggered = False
                _sl_reason    = "stop_loss"

                if _hp and _hunter_floor > 0 and price < _hunter_floor:
                    # Hunter Phase 1/2: price broke below ratchet floor → exit
                    _sl_triggered = True
                    _sl_reason    = "hunter_floor_breach"
                elif sl_pct and cx < _trail_activate:
                    # Default pct SL (or Hunter Phase 0 before trail activates)
                    drop_pct_sl = (price - avg) / avg * 100
                    if drop_pct_sl <= -sl_pct:
                        _sl_triggered = True

                if _sl_triggered:
                    cv_sl = h["amount"] * price
                    if cv_sl >= 0.01:
                        result = sell_core(ud, uid, contract, cv_sl, price, _sl_reason)
                        ud["apex_daily_pnl"] = (ud.get("apex_daily_pnl") or 0.0) + (result.get("realized") or 0.0)
                        if result["realized"] < 0:
                            ud["apex_consec_losses"] = ud.get("apex_consec_losses", 0) + 1
                            if ud["apex_consec_losses"] >= 3:
                                _apex_paused_until[uid] = datetime.now() + timedelta(minutes=APEX_DRAWDOWN_3_PAUSE)
                        else:
                            ud["apex_consec_losses"] = 0
                            ud["apex_total_wins"] = ud.get("apex_total_wins", 0) + 1
                        ud["apex_total_trades"] = ud.get("apex_total_trades", 0) + 1
                        apex_learn_record(uid, _apex_rich_record(h, cx, peak, avg, result["realized"], _sl_reason, ud))
                        _cal_freq = 5 if apex_get_phase(ud) == "learning" else 10
                        if ud.get("apex_total_trades", 0) % _cal_freq == 0:
                            apex_self_calibrate(ud, uid, suggest_only=False)
                        _apex_post_exit.setdefault(uid, {})[contract] = {
                            "symbol":      h["symbol"], "contract": contract,
                            "exit_price":  price, "entry_price": avg,
                            "exit_reason": _sl_reason, "exit_x": round(cx, 3),
                            "peak_x":      round(peak / avg, 3) if avg > 0 else 0,
                            "held_h":      round((_time.time() - h["bought_at"].timestamp()) / 3600, 2) if h.get("bought_at") else 0,
                            "entry_mc":    h.get("avg_cost_mc", 0),
                            "invested":    h.get("total_invested", 0),
                            "exit_at":     _time.time(), "snapshots": [],
                        }
                        _drop_str = str(round((price/avg - 1)*100, 1)) + "%"
                        _reason_str = ("Floor breach (" + str(round((_hunter_floor/avg - 1)*100, 1)) + "% floor)")  if _sl_reason == "hunter_floor_breach" else ("SL -" + str(sl_pct) + "%")
                        try:
                            await app.bot.send_message(
                                chat_id=uid, parse_mode="Markdown",
                                text=(
                                    "🛑 *APEX — STOP LOSS*\n\n"
                                    "*$" + _md(h["symbol"]) + "*  " + _reason_str + "\n"
                                    "Exit: *" + str(round(cx, 2)) + "x*  " + _drop_str + "\n"
                                    "PnL: *" + pstr(result["realized"]) + "*\n"
                                    "🏦 Vault: *" + money(ud.get("apex_vault", 0)) + "*"
                                ),
                                reply_markup=main_menu_kb()
                            )
                        except Exception:
                            pass
                    # Always skip trail block after SL triggers — amount may be
                    # near-zero from a prior partial sell and we must not double-exit
                    # ── Vault exhaustion check ────────────────────────────────
                    try:
                        await _check_apex_vault_exhausted(app.bot, uid, ud)
                    except Exception:
                        pass
                    continue

                # ── Trailing stop ─────────────────────────────────────────────────
                if cx >= _trail_activate:
                    if _hp:
                        tpct = _hunter_trail_pct(cx, _hp)
                        if threat == "ORANGE":
                            tpct = min(tpct, _hp["orange_tighten"])
                        elif threat == "YELLOW":
                            tpct = min(tpct, _hp["yellow_tighten"])
                    else:
                        tpct = apex_trail_pct(cx, ud)   # uses learned trail width when available
                        # SOL macro: apply crash tighten multiplier if set
                        _sol_tm = ud.get("_sol_trail_mult", 1.0)
                        if _sol_tm < 1.0:
                            tpct = max(0.04, round(tpct * _sol_tm, 3))
                        if threat == "ORANGE":
                            tpct = min(tpct, 0.06)
                        elif threat == "YELLOW":
                            tpct = min(tpct, 0.10)
                    sr_mult = apex_sr_trail_multiplier(h, info.get("mc", 0), info.get("vol_m5", 0), info.get("buy_pct_m5", info.get("buy_pct", 50)))
                    # Hunter: SR multiplier is capped at 1.0 — SR can tighten the
                    # trail but NEVER widen it above the base pct. Without this cap
                    # the multiplier could push the trail stop below the ratchet
                    # floor, defeating Hunter's break-even protection entirely.
                    if _hp:
                        sr_mult = min(sr_mult, _hp.get("sr_mult_cap", 1.0))
                    tpct = tpct * sr_mult
                    tpct = max(tpct, 0.04)
                    trail_stop = peak * (1.0 - tpct)
                    # Hunter: trail stop can NEVER go below the ratchet floor.
                    # Apply the floor BEFORE writing to h so the stored value
                    # is always correct, not just the comparison below.
                    if _hp:
                        _floor = h.get("apex_hunter_floor", 0.0)
                        if _floor > 0:
                            trail_stop = max(trail_stop, _floor)
                    else:
                        # Default mode break-even floor:
                        # Once trail is active the position has already proved itself
                        # by reaching _trail_activate (1.5x). The trail stop must
                        # NEVER go below entry price — we can never sell at a loss
                        # on a token that touched 1.5x. Without this fix a crash
                        # from 1.5x back below entry would trigger a loss exit.
                        trail_stop = max(trail_stop, avg)
                    h["apex_trail_stop"] = trail_stop
                    h["apex_trail_pct"]  = tpct
                    if price <= trail_stop:
                        cv2    = h["amount"] * price
                        if cv2 < 0.01:
                            continue
                        result = sell_core(ud, uid, contract, cv2, price, "apex_trail_exit")
                        ud["apex_daily_pnl"] = (ud.get("apex_daily_pnl") or 0.0) + (result.get("realized") or 0.0)
                        if result["realized"] < 0:
                            ud["apex_consec_losses"] = ud.get("apex_consec_losses", 0) + 1
                            if ud["apex_consec_losses"] >= 3:
                                _apex_paused_until[uid] = datetime.now() + timedelta(minutes=APEX_DRAWDOWN_3_PAUSE)
                        else:
                            ud["apex_consec_losses"] = 0
                            ud["apex_total_wins"] = ud.get("apex_total_wins", 0) + 1
                        ud["apex_total_trades"] = ud.get("apex_total_trades", 0) + 1
                        apex_learn_record(uid, _apex_rich_record(h, cx, peak, avg, result["realized"], "apex_trail_exit", ud))
                        if ud.get("apex_total_trades", 0) % 10 == 0:
                            apex_self_calibrate(ud, uid, suggest_only=False)
                        _realized_trail = result["realized"]
                        _split_pct      = ud.get("apex_vault_profit_split", 0.50)
                        _sent_to_bal    = round(_realized_trail * _split_pct, 4) if _realized_trail > 0 else 0.0
                        try:
                            await app.bot.send_message(chat_id=uid, parse_mode="Markdown",
                                text=("\U0001f4c8 *APEX \u2014 TRAIL EXIT*\n\n"
                                      "*$" + _md(h["symbol"]) + "*\n"
                                      "Peak: *" + str(round(peak / avg, 2)) + "x*  \u2192  Exit: *" + str(round(cx, 2)) + "x*\n"
                                      "Trail triggered at *" + str(round(tpct * 100, 1)) + "%* below peak\n"
                                      "PnL: *" + pstr(_realized_trail) + "*"
                                      + (" | Fee: *" + money(h.get("total_fees_paid",0)) + "*" if SIM_FEES_ENABLED else "") + "\n"
                                      + ("\U0001f4b8 *" + money(_sent_to_bal) + "* \u2192 Main Balance (" + str(int(_split_pct*100)) + "% split)\n" if _sent_to_bal > 0 else "")
                                      + "\U0001f3e6 Vault: *" + money(ud.get("apex_vault",0)) + "*  |  Cash: *" + money(ud["balance"]) + "*"),
                                reply_markup=main_menu_kb())
                        except Exception:
                            pass
                        # ── Register post-exit tracker ────────────────────
                        _apex_post_exit.setdefault(uid, {})[contract] = {
                            "symbol":      h["symbol"],
                            "contract":    contract,
                            "exit_price":  price,
                            "entry_price": avg,
                            "exit_reason": "apex_trail_exit",
                            "exit_x":      round(cx, 3),
                            "peak_x":      round(peak / avg, 3) if avg > 0 else 0,
                            "held_h":      round((h.get("bought_at") and (_time.time() - h["bought_at"].timestamp())) / 3600, 2) if h.get("bought_at") else 0,
                            "entry_mc":    h.get("avg_cost_mc", 0),
                            "invested":    h.get("total_invested", 0),
                            "exit_at":     _time.time(),
                            "snapshots":   [],
                        }
                        # ── Register re-entry watchlist (trail exits only) ────────
                        # NEVER watchlist a profitable trail exit (cx ≥ 1.10):
                        # the trail did its job — re-entering the same token
                        # after a win chases a retracement and bleeds profit.
                        # Only watchlist if we trail-exited at a near-loss/break-even.
                        # Also: NEVER watchlist a position that was itself a watchlist re-entry.
                        if round(cx, 3) < 1.10 and not h.get("apex_watchlist_reentry", False):
                            _apex_watchlist.setdefault(uid, {})[contract] = {
                                "symbol":          h["symbol"],
                                "exit_price":      price,
                                "exit_liq":        info.get("liq", 0),
                                "entry_price":     avg,
                                "exit_reason":     "apex_trail_exit",
                                "exit_x":          round(cx, 3),
                                "exit_at":         _time.time(),
                                "bottom_price":    price,
                                "bottom_ts":       _time.time(),
                                "reversal_alerted":False,
                                "re_entry_queued": False,
                                "last_check_ts":   0.0,
                                "status":          "watching",
                                "entry_score":     h.get("apex_entry_score", 0),
                                "entry_conf":      h.get("apex_entry_conf", 0),
                            }
                        # ── Vault exhaustion check ────────────────────────────
                        try:
                            await _check_apex_vault_exhausted(app.bot, uid, ud)
                        except Exception:
                            pass
                        continue

                # ── Gap 3: Resistance rejection alert ──────────────────────────
                # If price was at/near resistance last cycle and has now dropped
                # with low buy% — rejection confirmed. Fire ORANGE + DM once.
                if cx >= 1.10 and not _hp:   # only when position is profitable
                    _sr_zones_rej = apex_sr_compute_zones(h)
                    _res_mc_rej   = _sr_zones_rej.get("active_resistance")
                    if _res_mc_rej and _res_mc_rej > 0:
                        _mc_now = info.get("mc", 0)
                        _dist_rej = (_res_mc_rej - _mc_now) / _mc_now if _mc_now > 0 else 1.0
                        _was_near = h.get("_sr_was_near_resistance", False)
                        _bpm5_rej = info.get("buy_pct_m5", info.get("buy_pct", 50))
                        _price_fell = price < h.get("_sr_resistance_entry_price", price)
                        # Entering resistance zone — record entry price
                        if _dist_rej < APEX_SR_ZONE_PROXIMITY and not _was_near:
                            h["_sr_was_near_resistance"] = True
                            h["_sr_resistance_entry_price"] = price
                            h["_sr_rejection_alerted"] = False
                        # Rejection: was near resistance, now falling with sell pressure
                        elif _was_near and _dist_rej > APEX_SR_ZONE_PROXIMITY * 0.5:
                            # Price moving away from resistance
                            if _price_fell and _bpm5_rej < 45 and not h.get("_sr_rejection_alerted"):
                                h["_sr_rejection_alerted"] = True
                                h["_sr_was_near_resistance"] = False
                                logger.info(f"SR rejection [{h['symbol']}]: rejected at resistance MC ${_res_mc_rej:,.0f}")
                                try:
                                    await app.bot.send_message(
                                        chat_id=uid, parse_mode="Markdown",
                                        text=(
                                            "\U0001f7e0 *APEX \u2014 RESISTANCE REJECTED*\n\n"
                                            "*$" + _md(h["symbol"]) + "* failed at resistance\n"
                                            "Buy pressure: *" + str(round(_bpm5_rej)) + "%*  (sellers dominating)\n"
                                            "Trail tightened to *" + str(round(APEX_SR_ZONE_PROXIMITY*100)) + "%* zone\n"
                                            "_Watching for support hold or exit._"
                                        ),
                                        reply_markup=main_menu_kb()
                                    )
                                except Exception:
                                    pass
                        elif _dist_rej >= APEX_SR_ZONE_PROXIMITY * 2:
                            # Far from resistance — reset flags
                            h["_sr_was_near_resistance"] = False
                            h["_sr_rejection_alerted"]   = False

                # ── #25 Bundle bag flush detector ───────────────────────────────
                # On ORANGE threat: check Helius to identify bundle bag dumps.
                # If top wallet = >30% of recent sell volume AND sells stopped
                # → the bundle bag flushed → DCA into the dip.
                # If still selling → hold and wait, avoid catching a falling knife.

                # ── #9 ORANGE pullback investigation ─────────────────────────────
                # Run the WHY investigation at ORANGE too — earlier warning gives
                # more time to act. Notifies user with a diagnosis before RED fires.
                if threat == "ORANGE" and not _hp and not h.get("_orange_invest_done"):
                    _oi_key = os.environ.get("HELIUS_API_KEY", "")
                    if _oi_key:
                        _oi_last_ts = h.get("_orange_invest_ts", 0)
                        if _time.time() - _oi_last_ts > 30:   # 30s cooldown
                            h["_orange_invest_ts"] = _time.time()
                            try:
                                _oi_data = await get_helius_maker_pct(contract, _oi_key)
                                if _oi_data:
                                    _oi_top3  = _oi_data.get("top3_vol_pct", 0)
                                    _oi_maker = _oi_data.get("maker_pct", 50)

                                    # Dev wallet check at ORANGE
                                    _oi_dev_risk = "UNKNOWN"
                                    try:
                                        _oi_dev = await enrich_dev_wallet_history(contract, info, _oi_key)
                                        _oi_dev_risk = _oi_dev.get("dev_risk", "UNKNOWN")
                                    except Exception:
                                        pass

                                    # Build diagnosis message
                                    if _oi_dev_risk == "HIGH" and _oi_top3 >= 40:
                                        _oi_verdict = "🚨 *Dev wallet activity detected*\nHigh-risk deployer + concentrated selling.\n_Prepare to exit on RED._"
                                        h["_orange_invest_done"] = True
                                    elif _oi_top3 >= 60:
                                        _oi_verdict = "⚠️ *Bundle/coordinated selling*\nTop 3 wallets = *" + str(_oi_top3) + "%* of volume.\n_Exit likely on next RED cycle._"
                                        h["_orange_invest_done"] = True
                                    elif _oi_maker >= 55 and _oi_top3 < 50:
                                        _oi_verdict = "🐋 *Single whale selling*\n*" + str(_oi_maker) + "%* of wallets still buying.\n_Likely profit-take — monitoring._"
                                    elif _oi_maker >= 45 and _oi_top3 < 35:
                                        _oi_verdict = "📊 *Organic dip — distributed selling*\nNo single large seller detected.\n_Normal micro-cap volatility — holding._"
                                    else:
                                        _oi_verdict = "❓ *Mixed signals*\nTop3: *" + str(_oi_top3) + "%*  |  Makers: *" + str(_oi_maker) + "%*\n_Watching closely._"

                                    try:
                                        await app.bot.send_message(
                                            chat_id=uid, parse_mode="Markdown",
                                            text=(
                                                "🔍 *APEX — WHY IS IT DUMPING?*\n\n"
                                                "*$" + _md(h["symbol"]) + "*  " + str(round(cx, 2)) + "x  🟠 ORANGE\n"
                                                "─────────────────────\n"
                                                + _oi_verdict + "\n\n"
                                                "Top 3 wallets: *" + str(_oi_top3) + "%* of sell volume\n"
                                                "Unique buyers: *" + str(_oi_maker) + "%*\n"
                                                "Dev risk: *" + _oi_dev_risk + "*"
                                            ),
                                            reply_markup=main_menu_kb()
                                        )
                                    except Exception:
                                        pass
                            except Exception as _oie:
                                logger.debug(f"ORANGE investigation error: {_oie}")
                if threat == "ORANGE" and not _hp:
                    _bb_key = os.environ.get("HELIUS_API_KEY", "")
                    _bb_last_ts = h.get("_bundle_check_ts", 0)
                    if _bb_key and _time.time() - _bb_last_ts > 30:  # 30s cooldown
                        h["_bundle_check_ts"] = _time.time()
                        try:
                            _bb_data = await get_helius_maker_pct(contract, _bb_key)
                            if _bb_data:
                                _bb_top3 = _bb_data.get("top3_vol_pct", 0)
                                _bb_maker = _bb_data.get("maker_pct", 50)
                                # Bundle flush: concentrated selling, but buyers recovering
                                if _bb_top3 >= 30 and _bb_maker >= 48:
                                    h["_bundle_flushed"] = True
                                    logger.info(f"Bundle bag flush detected [{h['symbol']}]: "
                                                f"top3={_bb_top3}% sold, makers={_bb_maker}% recovering")
                                    try:
                                        await app.bot.send_message(
                                            chat_id=uid, parse_mode="Markdown",
                                            text=(
                                                "\U0001f4e6 *BUNDLE BAG FLUSH DETECTED*\n\n"
                                                "*$" + _md(h["symbol"]) + "*\n"
                                                "Top wallets sold " + str(_bb_top3) + "% of volume\n"
                                                "Buyers recovering: " + str(_bb_maker) + "% makers\n"
                                                "_Bag flushed \u2014 attempting DCA into the dip._"
                                            ),
                                            reply_markup=main_menu_kb()
                                        )
                                    except Exception:
                                        pass
                                    # Execute DCA now — bag flushed + buyers recovering = confirmed dip entry
                                    # Skip the normal _dca_range guard; bundle flush is its own confirmation.
                                    if not _sol_bearish:
                                        try:
                                            await apex_try_smart_dca(app, uid, ud, contract, h, info)
                                        except Exception as _bb_dca_err:
                                            logger.debug(f"Bundle flush DCA error: {_bb_dca_err}")
                                elif _bb_top3 >= 30 and _bb_maker < 45:
                                    # Still selling — flag, wait
                                    h["_bundle_still_selling"] = True
                                    logger.info(f"Bundle still selling [{h['symbol']}]: "
                                                f"top3={_bb_top3}%, makers only {_bb_maker}%")
                        except Exception as _bbe:
                            logger.debug(f"Bundle flush check error: {_bbe}")

                # ── Smart DCA at support (before momentum exit check) ────────────
                # Skip DCA entirely when SOL macro is bearish — don't average into
                # positions during a broader market crash (#10 spec).
                _dca_range = APEX_DCA_MIN_CX <= cx <= APEX_DCA_MAX_CX
                if _dca_range and cx < APEX_TRAIL_ACTIVATE_X and not _sol_bearish:
                    try:
                        await apex_try_smart_dca(app, uid, ud, contract, h, info)
                    except Exception as _dca_err:
                        logger.debug(f"APEX DCA error: {_dca_err}")

                # ── Momentum decay early exit — staged partial exit ────────────────
                if cx < 1.2:
                    bpm5 = info.get("buy_pct_m5", info.get("buy_pct", 50))
                    if bpm5 < 45:
                        import time as _mdt
                        _partial_ts = h.get("apex_partial_exit_ts", 0)
                        _now_ts     = _time.time()
                        if _partial_ts == 0:
                            # First decay signal — sell 40%, wait 60s before full exit
                            _cv3p = h["amount"] * price * 0.40
                            if _cv3p >= 0.50:
                                _res_partial = sell_core(ud, uid, contract, _cv3p, price, "apex_momentum_decay_partial")
                                if isinstance(_res_partial, dict):
                                    ud["apex_daily_pnl"] = (ud.get("apex_daily_pnl") or 0.0) + (_res_partial.get("realized") or 0.0)
                                h["apex_partial_exit_ts"] = _now_ts
                                try:
                                    await app.bot.send_message(chat_id=uid, parse_mode="Markdown",
                                        text=("⚠️ *APEX — Momentum Warning*\n\n"
                                              "*$" + _md(h["symbol"]) + "*\n"
                                              "Buy pressure: *" + str(bpm5) + "%* at *" + str(round(cx,2)) + "x*\n"
                                              "Sold 40% — watching for recovery (60s)"),
                                        reply_markup=main_menu_kb())
                                except Exception: pass
                            continue
                        elif _now_ts - _partial_ts < 180:   # 3-minute grace window (was 60s)
                            continue
                        # 3 min passed and still decaying — exit remainder
                        h.pop("apex_partial_exit_ts", None)
                        # Guard: partial sell may have closed the position already
                        if h.get("amount", 0) <= 0 or contract not in ud.get("holdings", {}):
                            continue
                        cv3    = h["amount"] * price
                        result = sell_core(ud, uid, contract, cv3, price, "apex_momentum_decay")
                        ud["apex_daily_pnl"] = (ud.get("apex_daily_pnl") or 0.0) + (result.get("realized") or 0.0)
                        if result["realized"] < 0:
                            ud["apex_consec_losses"] = ud.get("apex_consec_losses", 0) + 1
                        else:
                            ud["apex_consec_losses"] = 0
                            ud["apex_total_wins"] = ud.get("apex_total_wins", 0) + 1
                        ud["apex_total_trades"] = ud.get("apex_total_trades", 0) + 1
                        # ── Pause check must read the UPDATED consec_losses value
                        if ud["apex_consec_losses"] >= 3:
                            _apex_paused_until[uid] = datetime.now() + timedelta(minutes=APEX_DRAWDOWN_3_PAUSE)
                        apex_learn_record(uid, _apex_rich_record(h, cx, peak, avg, result["realized"], "apex_momentum_decay", ud))
                        if ud.get("apex_total_trades", 0) % 10 == 0:
                            apex_self_calibrate(ud, uid, suggest_only=False)
                        try:
                            await app.bot.send_message(chat_id=uid, parse_mode="Markdown",
                                text=("\u26a0\ufe0f *APEX \u2014 EARLY EXIT (Momentum Decay)*\n\n"
                                      "*$" + _md(h["symbol"]) + "*\n"
                                      "M5 buy pressure: *" + str(bpm5) + "%* \u2014 thesis broken\n"
                                      "Exit: *" + str(round(cx, 2)) + "x*  |  PnL: *" + pstr(result["realized"]) + "*\n"
                                      "Small loss taken early \u2014 protecting capital"),
                                reply_markup=main_menu_kb())
                        except Exception:
                            pass
                        # ── Register post-exit tracker ────────────────────
                        _apex_post_exit.setdefault(uid, {})[contract] = {
                            "symbol":      h["symbol"],
                            "contract":    contract,
                            "exit_price":  price,
                            "entry_price": avg,
                            "exit_reason": "apex_momentum_decay",
                            "exit_x":      round(cx, 3),
                            "peak_x":      round(peak / avg, 3) if avg > 0 else 0,
                            "held_h":      round((h.get("bought_at") and (_time.time() - h["bought_at"].timestamp())) / 3600, 2) if h.get("bought_at") else 0,
                            "entry_mc":    h.get("avg_cost_mc", 0),
                            "invested":    h.get("total_invested", 0),
                            "exit_at":     _time.time(),
                            "snapshots":   [],
                        }
                        # ── Register re-entry watchlist (momentum decay) ─────
                        # Same eligibility filter as RED exits: skip dead/rug tokens
                        # and never re-watchlist a position that was already a re-entry.
                        _md_liq = info.get("liq", 0)
                        _md_mc  = info.get("mc", 0)
                        if (not h.get("apex_watchlist_reentry", False)
                                and cx >= 0.40 and _md_mc >= 20_000 and _md_liq >= 500):
                            _apex_watchlist.setdefault(uid, {})[contract] = {
                                "symbol":          h["symbol"],
                                "exit_price":      price,
                                "exit_liq":        _md_liq,
                                "entry_price":     avg,
                                "exit_reason":     "apex_momentum_decay",
                                "exit_x":          round(cx, 3),
                                "exit_at":         _time.time(),
                                "bottom_price":    price,
                                "bottom_ts":       _time.time(),
                                "reversal_alerted":False,
                                "re_entry_queued": False,
                                "last_check_ts":   0.0,
                                "status":          "watching",
                                "entry_score":     h.get("apex_entry_score", 0),
                                "entry_conf":      h.get("apex_entry_conf", 0),
                            }
                        continue

                # ── No exit this cycle: persist state changes (apex_threat,
                # apex_peak_price, apex_trail_stop, sr_history) so they survive
                # a bot restart. sell_core handles saves on exits; we handle it
                # here for the non-exit path.
                save_user(uid, ud)
            except Exception as _e:
                logger.warning(f"APEX position manager {uid}/{contract}: {_e}")


async def apex_process_entry_queue(app, uid: int, ud: dict) -> None:
    queue = _apex_entry_queue.get(uid, {})
    if not queue:
        return
    from datetime import timedelta
    now = datetime.now()
    processed = []
    async with _get_user_lock(uid):
      for contract, entry in list(queue.items()):
        if (now - entry.get("queued_at", now)).total_seconds() < APEX_CONFIRM_WAIT_S:
            continue
        processed.append(contract)
        info_orig = entry["info"]
        sc        = entry["sc"]
        ai        = entry["ai"]
        base_amt  = entry.get("base_amount", 50.0)
        try:
            info_live = await get_token(contract)
        except Exception:
            info_live = None
        if not info_live:
            continue
        bpct_now = info_live.get("buy_pct_m5", info_live.get("buy_pct", 50))
        mc_runup  = info_live.get("mc", 0) / max(info_orig.get("mc", 1), 1)
        if bpct_now < 50:
            logger.info(f"APEX queue: {contract} rejected — buy% faded to {bpct_now}%")
            continue
        if mc_runup > 2.0:
            logger.info(f"APEX queue: {contract} rejected — already {round(mc_runup,1)}x since signal")
            continue
        # Guard: don't double-buy if already in holdings
        if contract in ud.get("holdings", {}):
            logger.info(f"APEX queue: {contract} already in holdings — skipping duplicate buy")
            continue
        buy_amt = apex_position_size(ud, ai.get("confidence", 5), base_amt, info_orig, ai.get("rug_risk", "LOW"))
        if buy_amt < 1.0:
            continue
        _eq_phase = apex_get_phase(ud)
        if _eq_phase != "learning":
            if apex_count_positions(ud) >= ud.get("apex_max_positions_learned", APEX_MAX_POSITIONS):
                continue
            if apex_is_daily_loss_halted(ud):
                continue
        _sniper_daily_reset(ud)
        budget    = ud.get("sniper_daily_budget", 500.0)
        spent     = ud.get("sniper_daily_spent", 0.0)
        _vault_bal = ud.get("apex_vault", 0.0)
        # APEX always trades from vault — if vault can't cover the buy, auto-disable
        # APEX and notify the user. They must re-fund and re-enable manually.
        if _vault_bal < buy_amt:
            logger.info(f"APEX entry skipped: vault {_vault_bal:.2f} < buy {buy_amt:.2f}")
            # Only auto-disable and notify once (guard: don't spam every cycle)
            if ud.get("apex_mode") and not ud.get("_apex_vault_low_notified"):
                ud["apex_mode"]               = False
                ud["_apex_vault_low_notified"] = True
                save_user(uid, ud)
                try:
                    await app.bot.send_message(
                        chat_id=uid, parse_mode="Markdown",
                        text=(
                            "\U0001f534 *APEX AUTO-DISABLED — Vault Insufficient*\n\n"
                            "APEX tried to enter a trade but your vault only has *"
                            + money(_vault_bal) + "* — not enough for the calculated "
                            "position size of *" + money(buy_amt) + "*.\n\n"
                            "APEX has been *paused automatically* to prevent failed entries.\n\n"
                            "To resume:\n"
                            "1\ufe0f\u20e3 Go to *APEX \u2192 Vault \u2192 Fund Vault*\n"
                            "2\ufe0f\u20e3 Add funds from your main balance\n"
                            "3\ufe0f\u20e3 Return to *APEX* and tap *Enable APEX*\n\n"
                            "\U0001f4b5 Main balance available: *" + money(ud.get("balance", 0)) + "*"
                        ),
                        reply_markup=main_menu_kb()
                    )
                except Exception as _vln_err:
                    logger.debug(f"Vault low notify failed uid={uid}: {_vln_err}")
            continue
        buy_amt = min(buy_amt, _vault_bal, budget - spent)
        if buy_amt < 1.0:
            continue
        ud["sniper_daily_spent"] = spent + buy_amt
        result = await do_buy_core(ud, uid, contract, buy_amt, planned=True, mood="APEX", vault_buy=True)
        if not isinstance(result, tuple):
            logger.warning(f"APEX buy failed: {result}")
            continue
        info_post, _ = result
        h = ud["holdings"].get(contract, {})
        if not h:
            continue
        # ── Post-buy setup: everything from here to send_message is wrapped in a
        # safety try/except. If ANY error occurs after do_buy_core succeeds, the
        # fallback block applies an emergency SL so the position is never orphaned
        # with no stop-loss. This was the root cause of the auramaxxing incident:
        # _hp and _apex_sl were referenced before they were defined (NameError),
        # the buy succeeded but stop_loss_pct/apex_hunter_floor were never written,
        # no alert was sent, and the SL block silently skipped a -51% position.
        try:
            # Compute _hp and _apex_sl FIRST — everything else references them
            _rug  = ai.get("rug_risk", "LOW")
            _hp   = get_apex_profile(ud)
            if _hp:
                _apex_sl = _hp["sl_high"] if _rug == "HIGH" else (_hp["sl_med"] if _rug == "MEDIUM" else _hp["sl_low"])
            else:
                _sl_defaults = {"LOW": 22.0, "MEDIUM": 18.0, "HIGH": 12.0}
                _sl_learned  = {
                    "LOW":    ud.get("apex_sl_learned_low",  22.0),
                    "MEDIUM": ud.get("apex_sl_learned_med",  18.0),
                    "HIGH":   ud.get("apex_sl_learned_high", 12.0),
                }
                _apex_sl = _sl_learned.get(_rug, _sl_defaults.get(_rug, 22.0))
            h["stop_loss_pct"] = _apex_sl
            import time as _tsla
            h.setdefault("stop_loss_history", []).append({
                "old":    None,
                "new":    _apex_sl,
                "source": "apex_entry",
                "cx":     1.0,
                "ts":     _tsla.time(),
            })
            h["apex_peak_price"]   = info_post["price"]
            h["apex_trail_stop"]   = None
            h["apex_trail_pct"]    = APEX_TRAIL_PCT_EARLY
            h["apex_threat"]       = "CLEAR"
            h["apex_vault_locked"] = {}
            # Store the profile name at entry — position manager always reads this,
            # not the current user setting. Prevents Hunter→Default bleed if profile
            # is changed or suspended while the position is open.
            h["apex_profile_at_entry"] = ud.get("apex_risk_profile", "default") if not APEX_HUNTER_SUSPENDED else "default"
            # Hunter: floor at entry price x (1 - SL%) — protection is immediate
            if _hp:
                h["apex_hunter_floor"] = round(info_post["price"] * (1.0 - _apex_sl / 100.0), 10)
            else:
                h["apex_hunter_floor"] = 0.0
            h["apex_entry_score"]  = sc.get("score", 0)
            h["apex_entry_conf"]   = ai.get("confidence", 0)
            h["liq_at_buy"]        = info_post.get("liq", 0)
            h["pair_addr"]         = info_post.get("pair_addr", "")
            h["apex_entry_rug"]       = _rug
            h["apex_entry_age_h"]     = info_post.get("age_h") or 0
            h["apex_entry_buy_pct"]   = info_post.get("buy_pct_m5", info_post.get("buy_pct", 50))
            h["apex_entry_pos_count"] = apex_count_positions(ud)
            if entry.get("reentry"):
                h["apex_watchlist_reentry"] = True
            h["sr_history"]        = []
            h["sr_peak_vol"]       = 0.0
            h["sr_peak_visit_vol"] = 0.0
            h["sr_buy_pct_dipped"] = False
            h["apex_dca_count"]    = 0
            h["apex_last_dca_ts"]  = 0.0
            h["apex_dca_history"]  = []
            # Exit ladder flags — track which partial sells have fired
            h["apex_ladder_sold_2x"]   = False   # 50% sold at 2x
            # ── #13 Token type — for pattern memory segmentation ────────
            # pumpfun: bonding curve token not yet graduated
            # graduated: pump.fun token that moved to Raydium
            # kol: KOL wallet triggered the signal
            # organic: standard DexScreener discovery
            # Use info_orig (pre-queue sniper data) — it has pf_curve and
            # kol_buy_count from sniper_scan enrichment. info_post is a fresh
            # get_token() call and never has these enrichment fields.
            _tt = "organic"
            _info_for_type = entry.get("info", info_post)  # prefer pre-queue info
            if _info_for_type.get("pf_curve") is not None:
                _tt = "graduated" if _info_for_type.get("pf_graduated") else "pumpfun"
            elif _info_for_type.get("kol_buy_count", 0) >= 1:
                _tt = "kol"
            h["apex_token_type"] = _tt
            ud.setdefault("sniper_bought", []).append(contract)
            ud.setdefault("sniper_log", []).append({
                "contract":   contract, "symbol": info_post["symbol"],
                "chain":      info_post.get("chain", "?"), "mc": info_post["mc"],
                "score":      sc.get("score", 0), "verdict": "SNIPE",
                "confidence": ai.get("confidence", 0), "rug_risk": ai.get("rug_risk", "?"),
                "thesis":     ai.get("thesis", ""), "timestamp": datetime.now().isoformat(),
                "bought":     True, "amount": buy_amt, "mode": "APEX",
            })
            if len(ud["sniper_log"]) > SNIPER_LOG_MAX:
                ud["sniper_log"] = ud["sniper_log"][-SNIPER_LOG_MAX:]
            # ── CRITICAL: save NOW so apex fields survive a bot restart.
            # do_buy_core already saved once (with basic holding fields only).
            # This second save persists stop_loss_pct, apex_hunter_floor,
            # apex_peak_price, etc. so the position manager has a working SL
            # even if the bot restarts before the first position manager cycle.
            save_user(uid, ud)
            heat = round(apex_capital_heat(ud) * 100, 1)
            _profile_label = "Hunter" if _hp else "Default"
            # Escape Markdown-special chars in free-text fields so Telegram
            # never rejects the message with a 400 error and silently eats
            # the entry alert. The user MUST receive this notification.
            def _mde(s):
                t = str(s)
                for ch in ("_", "*", "`", "["):
                    t = t.replace(ch, "\\" + ch)
                return t
            try:
                await app.bot.send_message(chat_id=uid, parse_mode="Markdown",
                    text=("\U0001f3af *APEX ENTRY CONFIRMED*\n\n"
                          "*$" + _mde(info_post["symbol"]) + "*  " + info_post.get("chain", "").upper() + "\n"
                          "Profile: *" + _profile_label + "*  |  Confidence: *" + str(ai.get("confidence", 0)) + "/10*  |  Score: *" + str(sc.get("score", 0)) + "/100*\n"
                          "Rug Risk: *" + ai.get("rug_risk", "?") + "*\n\n"
                          "\U0001f4dd " + _mde(ai.get("thesis", "\u2014")) + "\n\n"
                          "\U0001f4b5 Bought: *" + money(buy_amt) + "*"
                          + (" | Fee: *" + money(round(buy_amt * SIM_TOTAL_PCT + SIM_GAS_USD, 4)) + "*" if SIM_FEES_ENABLED else "") + "\n"
                          "Entry MC: *" + mc_str(info_post["mc"]) + "*\n"
                          "Heat: *" + str(heat) + "%*  |  Positions: *" + str(apex_count_positions(ud)) + "/\u221e*\n"
                          "\U0001f6d1 SL: *" + str(h["stop_loss_pct"]) + "%*  |  \U0001f501 Trail: *activates at 1.5x*"),
                    reply_markup=main_menu_kb())
            except Exception as _ne:
                logger.error(f"APEX entry notify failed {uid}/{info_post.get('symbol','?')}: {_ne}")
        except Exception as _entry_err:
            # Safety net — apply emergency SL so the position is never left naked
            logger.error(f"APEX post-buy setup error {uid}/{contract}: {_entry_err}", exc_info=True)
            _h2 = ud.get("holdings", {}).get(contract, {})
            if _h2:
                if not _h2.get("stop_loss_pct"):
                    _h2["stop_loss_pct"]     = 18.0
                    _h2["apex_hunter_floor"] = 0.0
                    _h2["apex_peak_price"]   = _h2.get("avg_price", 0)
                    _h2["apex_trail_stop"]   = None
                    _h2["apex_threat"]       = "CLEAR"
                    logger.warning(f"APEX emergency SL 18% applied to orphaned {contract}")
                _h2.setdefault("sniper_log_note", "entry_setup_error")
            save_user(uid, ud)
            # Always notify user even in error path — they MUST know a buy happened
            try:
                await app.bot.send_message(chat_id=uid,
                    text=(f"⚠️ APEX bought ${info_post.get('symbol','?')} (${buy_amt:.2f}) "
                          f"but hit a setup error. Position is live with emergency 18% SL. "
                          f"Check Open Positions now."),
                    reply_markup=main_menu_kb())
            except Exception:
                pass
        # ── Clean up processed entries inside the lock so a concurrent caller
        # (apex_checker_job + run_checker both invoke this) cannot re-process
        # the same queue entry before cleanup completes.
        for c in processed:
            queue.pop(c, None)
        if not queue:
            _apex_entry_queue.pop(uid, None)



async def apex_post_exit_tracker_run(app, uid: int) -> None:
    """
    For each recently exited APEX position, fetches current price at
    30min / 1h / 4h checkpoints and records what the token did after exit.
    Sends a DM if the token pumps 50%+ vs exit price — so you can review
    whether RED exits were rugs or retracements you missed.
    """
    import time as _pet
    CHECKPOINTS = [1800, 3600, 14400]  # 30m, 1h, 4h
    EXPIRY      = 18000                 # 5h
    now_ts      = _pet.time()
    exits       = _apex_post_exit.get(uid, {})

    for contract, rec in list(exits.items()):
        try:
            age = now_ts - rec.get("exit_at", now_ts)
            if age > EXPIRY:
                del exits[contract]
                continue

            snapshots   = rec.setdefault("snapshots", [])
            snaps_taken = {s["checkpoint_s"] for s in snapshots}
            due         = [cp for cp in CHECKPOINTS if cp not in snaps_taken and age >= cp]
            if not due:
                continue

            info = await get_token(contract)
            if not info:
                continue

            cur_price   = info.get("price", 0)
            exit_price  = rec.get("exit_price", 0)
            entry_price = rec.get("entry_price", 0)
            if cur_price <= 0 or exit_price <= 0:
                continue

            x_vs_exit  = round(cur_price / exit_price,  3)
            x_vs_entry = round(cur_price / entry_price, 3) if entry_price > 0 else 0

            for cp in due:
                snapshots.append({
                    "checkpoint_s":     cp,
                    "checkpoint_label": "30m" if cp == 1800 else ("1h" if cp == 3600 else "4h"),
                    "price":     cur_price,
                    "x_vs_exit": x_vs_exit,
                    "x_vs_entry":x_vs_entry,
                    "mc":        info.get("mc", 0),
                    "checked_at":now_ts,
                })

            # ── Feed post-exit data back into learning memory ─────────────────
            # Find the matching memory record by timestamp and contract.
            # Update post_exit_x_1h / _4h so calibrate can detect premature exits.
            if uid in _apex_learn_memory:
                _ct_str = rec.get("contract", "")
                for _mr in reversed(_apex_learn_memory[uid]):
                    # Match by exit timestamp proximity (within 5 min)
                    if abs(_mr.get("ts", 0) - rec.get("exit_at", 0)) < 300:
                        if 3600 in due or any(s["checkpoint_s"] == 3600 for s in snapshots):
                            _snap_1h = next((s for s in snapshots if s["checkpoint_s"] == 3600), None)
                            if _snap_1h:
                                _mr["post_exit_x_1h"] = _snap_1h["x_vs_exit"]
                        if 14400 in due or any(s["checkpoint_s"] == 14400 for s in snapshots):
                            _snap_4h = next((s for s in snapshots if s["checkpoint_s"] == 14400), None)
                            if _snap_4h:
                                _mr["post_exit_x_4h"] = _snap_4h["x_vs_exit"]
                        break

            # Alert once if token pumped 50%+ since APEX sold
            if not rec.get("pump_alerted") and x_vs_exit >= 1.5:
                rec["pump_alerted"] = True
                age_label  = "30m" if age < 3600 else ("1h" if age < 14400 else "4h")
                exit_r     = rec.get("exit_reason","?").replace("apex_","").replace("_"," ").title()
                exit_x     = rec.get("exit_x", 0)
                peak_x     = rec.get("peak_x",  0)   # highest cx seen while held
                held_h     = rec.get("held_h",  0)   # how long position was held
                entry_mc   = rec.get("entry_mc", 0)  # MC at entry
                cur_mc     = info.get("mc", 0)
                cur_liq    = info.get("liq", 0)
                buy_pct    = info.get("buy_pct_m5", info.get("buy_pct", 0))
                vol_m5     = info.get("vol_m5", 0)
                missed_pnl = rec.get("invested", 0) * (x_vs_exit - 1.0)  # approx missed gain
                dex_chain  = "solana" if info.get("chain","").lower() in ("sol","solana") else info.get("chain","solana").lower()
                contract_r = rec.get("contract", "")

                # ── Build insight lines ───────────────────────────────────────
                _exit_icon = "🟡" if "trail" in rec.get("exit_reason","") else "🔴"
                _mc_delta  = ""
                if entry_mc > 0 and cur_mc > 0:
                    mc_mult = round(cur_mc / entry_mc, 2)
                    _mc_delta = f"  →  *{mc_mult}x* now"

                try:
                    from telegram import InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
                    _btns = []
                    if contract_r:
                        _btns.append([
                            _IKB("📊 Open Token Card", callback_data="btt_" + contract_r),
                            _IKB("🔗 DexScreener",     url=f"https://dexscreener.com/{dex_chain}/{contract_r}"),
                        ])
                    _kb = _IKM(_btns) if _btns else None

                    await app.bot.send_message(
                        chat_id=uid, parse_mode="Markdown",
                        reply_markup=_kb,
                        text=(
                            "📈 *POST-EXIT INSIGHT*\n\n"
                            "*$" + rec.get("symbol","?") + "*  " + info.get("chain","SOL").upper() + "\n"
                            "─────────────────────\n"
                            + _exit_icon + " Exit: *" + exit_r + "*  at  *" + str(exit_x) + "x*"
                            + ("  |  Held *" + str(round(held_h, 1)) + "h*" if held_h else "") + "\n"
                            + ("📍 Peak while held: *" + str(round(peak_x, 2)) + "x*\n" if peak_x > 0 else "")
                            + "\n"
                            "⏱ *" + age_label + " after exit:*\n"
                            "  Price: *+" + str(round((x_vs_exit-1)*100,1)) + "% vs exit*"
                            + "  |  *" + str(round(x_vs_entry,2)) + "x vs entry*\n"
                            + ("  MC: *" + mc_str(entry_mc) + "*" + _mc_delta + "\n" if entry_mc > 0 else "")
                            + ("  Liq: *$" + f"{cur_liq:,.0f}" + "*"
                               + "  |  Buy%: *" + str(buy_pct) + "%*"
                               + ("  |  Vol5m: *$" + f"{vol_m5:,.0f}" + "*" if vol_m5 > 0 else "")
                               + "\n" if cur_liq > 0 else "")
                            + ("\n💸 Missed gain (est): *+" + f"${missed_pnl:.2f}" + "*\n" if missed_pnl > 0 else "")
                            + "\n_Saved in your daily JSON export._"
                        )
                    )
                except Exception:
                    pass

        except Exception as _e:
            logger.debug(f"Post-exit tracker error {contract}: {_e}")


# ══ APEX RE-ENTRY WATCHLIST ══════════════════════════════════════════════════

def apex_watchlist_reversal(info: dict, rec: dict) -> tuple:
    """
    Check if a watchlisted token shows a genuine reversal.
    Returns (True, reason_str) or (False, reason_str).
    All 5 conditions must be true simultaneously:
      1. MC is alive (>= $20K) — hard block on dead/rug tokens
      2. Price bounced 15%+ from the lowest point seen after exit
      3. Buy pressure recovered above 52% on M5
      4. Liquidity still within 15% of what it was at exit (not being drained)
      5. Volume alive (>= 50% of hourly average baseline)
    """
    cur_price  = info.get("price", 0)
    cur_mc     = info.get("mc", 0)
    buy_pct_m5 = info.get("buy_pct_m5", info.get("buy_pct", 50))
    liq        = info.get("liq", 0)
    vol_m5     = info.get("vol_m5", 0)
    vol_h1     = info.get("vol_h1", 1)
    avg_5m_vol = vol_h1 / 12.0 if vol_h1 > 0 else 0
    bottom     = rec.get("bottom_price", cur_price)
    exit_liq   = rec.get("exit_liq", liq)

    if cur_price <= 0 or bottom <= 0:
        return False, "No price data"

    # 0. MC floor — dead tokens (rugs) can never pass this gate
    if cur_mc < 20_000:
        return False, f"MC too low (${int(cur_mc):,}) — token appears dead"

    # 1. Bounce from bottom
    bounce_pct = (cur_price - bottom) / bottom if bottom > 0 else 0
    if bounce_pct < WATCHLIST_BOUNCE_PCT:
        return False, f"Bounce only {round(bounce_pct*100,1)}% (need {int(WATCHLIST_BOUNCE_PCT*100)}%)"

    # 2. Buy pressure
    if buy_pct_m5 < WATCHLIST_BUY_PCT_MIN:
        return False, f"Buy pressure {buy_pct_m5}% (need {WATCHLIST_BUY_PCT_MIN}%)"

    # 3. Liq stable
    if exit_liq > 0:
        liq_drop = (exit_liq - liq) / exit_liq
        if liq_drop > WATCHLIST_LIQ_DROP_MAX:
            return False, f"Liq dropped {round(liq_drop*100,1)}% since exit"

    # 4. Volume alive
    if avg_5m_vol > 0 and vol_m5 < avg_5m_vol * WATCHLIST_VOL_MIN:
        return False, f"Volume dead ({round(vol_m5)} vs avg {round(avg_5m_vol)})"

    return True, (
        f"MC ${int(cur_mc):,} | "
        f"Bounce +{round(bounce_pct*100,1)}% from bottom | "
        f"Buy% {buy_pct_m5}% | Liq stable | Vol alive"
    )


async def apex_watchlist_checker(app) -> None:
    """
    Runs every apex_checker_job cycle (throttled per-token to WATCHLIST_CHECK_EVERY secs).
    Tracks post-exit price movements, detects reversals, fires alerts with buy button.
    Tokens expire after 12h or are removed early if they go dead.
    """
    import time as _wtime
    now_ts = _wtime.time()

    for uid, watchlist in list(_apex_watchlist.items()):
        ud = users.get(uid)
        if not ud:
            continue
        for contract, rec in list(watchlist.items()):
            try:
                age = now_ts - rec.get("exit_at", now_ts)

                # 12h expiry
                if age > WATCHLIST_EXPIRY_S:
                    del watchlist[contract]
                    continue

                # Skip resolved entries
                if rec.get("status") in ("reversed", "dead", "expired"):
                    continue

                # Throttle — only check every 2 minutes
                if now_ts - rec.get("last_check_ts", 0) < WATCHLIST_CHECK_EVERY:
                    continue
                rec["last_check_ts"] = now_ts

                # Skip if we re-entered this token already
                if contract in ud.get("holdings", {}):
                    rec["status"] = "reversed"
                    continue

                info = await get_token(contract)
                if not info:
                    continue

                cur_price = info.get("price", 0)
                cur_liq   = info.get("liq", 0)
                if cur_price <= 0:
                    continue

                # Track bottom price
                if cur_price < rec.get("bottom_price", cur_price):
                    rec["bottom_price"] = cur_price
                    rec["bottom_ts"]    = now_ts

                # Dead token: liq drained >50% or price near zero
                exit_liq = rec.get("exit_liq", cur_liq)
                if exit_liq > 0 and cur_liq < exit_liq * 0.50:
                    rec["status"] = "dead"
                    del watchlist[contract]
                    continue
                if rec.get("exit_price", 1) > 0 and cur_price < rec["exit_price"] * 0.05:
                    rec["status"] = "dead"
                    del watchlist[contract]
                    continue

                # Already alerted
                if rec.get("reversal_alerted"):
                    continue

                # Reversal check
                reversed_ok, reason = apex_watchlist_reversal(info, rec)
                if not reversed_ok:
                    continue

                # ── REVERSAL DETECTED ─────────────────────────────────────
                rec["reversal_alerted"] = True
                rec["status"]           = "reversed"
                apex_on = ud.get("apex_mode", False)

                exit_price   = rec.get("exit_price", cur_price)
                entry_price  = rec.get("entry_price", cur_price)
                age_h        = round(age / 3600, 1)
                x_vs_exit    = round(cur_price / exit_price,  3) if exit_price  > 0 else 0
                x_vs_entry   = round(cur_price / entry_price, 3) if entry_price > 0 else 0

                # Optional auto re-entry — DISABLED, alert only
                reentry_note = ""

                try:
                    await app.bot.send_message(
                        chat_id=uid, parse_mode="Markdown",
                        text=(
                            "\U0001f441 *WATCHLIST \u2014 REVERSAL DETECTED*\n\n"
                            "*$" + rec.get("symbol","?") + "*\n\n"
                            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                            "Original exit: *" + rec.get("exit_reason","?").replace("apex_","").replace("_"," ").title() + "* at *" + str(rec.get("exit_x","?")) + "x*\n"
                            "Time since exit: *" + str(age_h) + "h*\n\n"
                            "\U0001f4cd Bottom hit: *" + money(rec.get("bottom_price", cur_price)) + "*\n"
                            "\U0001f4c8 Now: *" + money(cur_price) + "*  (*+" + str(round((x_vs_exit-1)*100, 1)) + "% from exit*)"
                            + ("  |  *" + str(x_vs_entry) + "x vs entry*" if x_vs_entry > 0 else "") + "\n\n"
                            "\u2705 *Reversal signals:*\n_" + reason + "_"
                            + reentry_note
                        ),
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(
                                "\u26a1 Buy $" + rec.get("symbol","?"),
                                callback_data="btt_" + contract
                            )],
                            [InlineKeyboardButton(
                                "\U0001f4c4 View on DexScreener",
                                url="https://dexscreener.com/solana/" + contract
                            )],
                        ])
                    )
                except Exception as _wae:
                    logger.error(f"Watchlist alert error {uid}: {_wae}")

                del watchlist[contract]

            except Exception as _wce:
                logger.debug(f"Watchlist checker error {contract}: {_wce}")


async def apex_daily_report(bot, uid: int, ud: dict) -> None:
    today = datetime.now().date()
    logs  = trade_log.get(uid, [])

    def _to_date(val):
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val).date()
            except Exception:
                return None
        return None

    apex_trades = [t for t in logs
                   if _to_date(t.get("closed_at")) == today
                   and t.get("mood") in ("APEX", "AI-Sniper")]
    if not apex_trades:
        try:
            await bot.send_message(chat_id=uid, parse_mode="Markdown",
                text=("\U0001f4c5 *APEX DAILY REPORT*\n\n"
                      "No APEX trades executed today.\n"
                      "Vault: *" + money(ud.get("apex_vault", 0)) + "*\n"
                      "Balance: *" + money(ud.get("balance", 0)) + "*"),
                reply_markup=main_menu_kb())
        except Exception:
            pass
        return
    wins  = [t for t in apex_trades if t.get("realized_pnl", 0) > 0]
    losses= [t for t in apex_trades if t.get("realized_pnl", 0) <= 0]
    total_pnl = sum(t.get("realized_pnl", 0) for t in apex_trades)
    wr   = round(len(wins) / len(apex_trades) * 100) if apex_trades else 0
    aw   = sum(t.get("realized_pnl", 0) for t in wins) / len(wins) if wins else 0
    al   = sum(t.get("realized_pnl", 0) for t in losses) / len(losses) if losses else 0
    best = max(apex_trades, key=lambda t: t.get("realized_pnl", 0))
    wrst = min(apex_trades, key=lambda t: t.get("realized_pnl", 0))
    reasons = {}
    for t in apex_trades:
        r = t.get("reason", "manual")
        reasons[r] = reasons.get(r, 0) + 1
    reason_lines = []
    reason_icons = {"apex_trail_exit": "\U0001f4c8", "apex_momentum_decay": "\u26a0\ufe0f",
                    "apex_threat_red": "\U0001f6a8", "stop_loss": "\U0001f6d1",
                    "apex_threat_orange": "\U0001f7e0", "manual": "\U0001f4cc"}
    for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
        icon = reason_icons.get(r, "\u25aa\ufe0f")
        reason_lines.append("  " + icon + " " + r.replace("apex_", "").replace("_", " ") + ": " + str(cnt))
    learn_changes = apex_self_calibrate(ud, uid, suggest_only=True)
    learn_txt = ""
    if learn_changes:
        learn_txt = "\n\n\U0001f4a1 *SELF-LEARNING SUGGESTIONS:*\n"
        learn_txt += "_These are suggestions only — apply them in APEX Settings._\n\n"
        for key, chg in learn_changes.items():
            label = key.replace("apex_learn_", "").replace("apex_", "").replace("_", " ").title()
            learn_txt += "  • " + label + ": *" + str(chg["old"]) + "* → *" + str(chg["new"]) + "*\n"
            learn_txt += "  _" + chg["reason"] + "_\n"
    mem     = _apex_learn_memory.get(uid, [])
    mem_wr  = round(len([m for m in mem if m.get("pnl", 0) > 0]) / len(mem) * 100) if mem else 0
    life_pnl= sum(m.get("pnl", 0) for m in mem)
    try:
        await bot.send_message(chat_id=uid, parse_mode="Markdown",
            text=("\U0001f4c5 *APEX DAILY REPORT \u2014 " + str(today) + "*\n\n"
                  "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                  "\U0001f3af *TODAY'S PERFORMANCE*\n"
                  "Trades: *" + str(len(apex_trades)) + "*  (" + str(len(wins)) + "W / " + str(len(losses)) + "L)\n"
                  "Win Rate: *" + str(wr) + "%*\n"
                  "Total PnL: *" + pstr(total_pnl) + "*\n"
                  "Avg Win: *" + money(aw) + "*  |  Avg Loss: *" + money(abs(al)) + "*\n\n"
                  "\U0001f3c6 Best: *$" + best.get("symbol", "?") + "* " + pstr(best.get("realized_pnl", 0)) + " (" + str(round(best.get("x", 0), 2)) + "x)\n"
                  "\U0001f494 Worst: *$" + wrst.get("symbol", "?") + "* " + pstr(wrst.get("realized_pnl", 0)) + " (" + str(round(wrst.get("x", 0), 2)) + "x)\n\n"
                  "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                  "\U0001f6aa *EXIT BREAKDOWN*\n" + "\n".join(reason_lines) + "\n\n"
                  "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                  "\U0001f3e6 *CAPITAL STATUS*\n"
                  "Balance: *" + money(ud.get("balance", 0)) + "*\n"
                  "Vault (locked): *" + money(ud.get("apex_vault", 0)) + "*\n"
                  "Heat: *" + str(round(apex_capital_heat(ud) * 100, 1)) + "%*\n"
                  + ("💸 Fees paid today: *" + money(sum(t.get("fees_paid",0) for t in apex_trades)) + "* _(gas+DEX+slippage)_\n" if SIM_FEES_ENABLED else "") +
                  "\n"
                  "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                  "\U0001f9e0 *LIFETIME LEARNING (" + str(len(mem)) + " trades)*\n"
                  "All-time WR: *" + str(mem_wr) + "%*\n"
                  "All-time PnL: *" + pstr(life_pnl) + "*\n"
                  "Min Confidence: *" + str(ud.get("apex_learn_threshold", APEX_MIN_CONFIDENCE)) + "/10*\n"
                  "Min Score: *" + str(ud.get("apex_learn_score_min", 45)) + "/100*"
                  + learn_txt),
            reply_markup=main_menu_kb())
    except Exception as _rpe:
        logger.error(f"APEX daily report: {_rpe}")

    # ── JSON trade log export ─────────────────────────────────────────────────
    try:
        import json as _json_mod
        import io   as _io_mod

        all_apex = [t for t in trade_log.get(uid, [])
                    if t.get("mood") in ("APEX", "AI-Sniper", "APEX-DCA")]

        # Attach post-exit snapshot data to each trade in the export
        post_exits = _apex_post_exit.get(uid, {})
        export_trades = []
        for t in all_apex:
            entry = {
                "symbol":        t.get("symbol"),
                "contract":      t.get("contract"),
                "chain":         t.get("chain"),
                "mood":          t.get("mood"),
                "invested":      round(t.get("invested", 0), 4),
                "returned":      round(t.get("returned", 0), 4),
                "realized_pnl":  round(t.get("realized_pnl", 0), 4),
                "x":             t.get("x"),
                "hold_h":        t.get("hold_h"),
                "exit_reason":   t.get("reason"),
                "bought_at":     t["bought_at"].isoformat() if hasattr(t.get("bought_at"), "isoformat") else str(t.get("bought_at","")),
                "closed_at":     t["closed_at"].isoformat() if hasattr(t.get("closed_at"), "isoformat") else str(t.get("closed_at","")),
                "avg_entry_price": t.get("avg_price"),
                "exit_price":    t.get("exit_price"),
                "peak_price":    t.get("peak_price"),
                # Post-exit snapshots: what the token did AFTER APEX sold
                "post_exit_snapshots": post_exits.get(t.get("contract",""), {}).get("snapshots", []),
            }
            export_trades.append(entry)

        export_payload = {
            "export_date":   str(datetime.now().date()),
            "generated_at":  datetime.now().isoformat(),
            "summary": {
                "total_trades":   len(all_apex),
                "wins":           len([t for t in all_apex if t.get("realized_pnl",0) > 0]),
                "losses":         len([t for t in all_apex if t.get("realized_pnl",0) <= 0]),
                "total_pnl":      round(sum(t.get("realized_pnl",0) for t in all_apex), 4),
                "total_fees_paid":round(ud.get("total_fees_paid", 0.0), 4),
                "net_pnl_after_fees": round(sum(t.get("realized_pnl",0) for t in all_apex) - ud.get("total_fees_paid",0), 4),
                "vault_balance":  round(ud.get("apex_vault", 0), 4),
                "current_balance":round(ud.get("balance", 0), 4),
                "fee_config": {"enabled": SIM_FEES_ENABLED, "dex_pct": SIM_DEX_FEE_PCT, "slippage_pct": SIM_SLIPPAGE_PCT, "gas_usd": SIM_GAS_USD},
            },
            "exit_breakdown": reasons,
            "learning_state": {
                "total_trades":    ud.get("apex_total_trades", 0),
                "phase":           apex_get_phase(ud),
                "min_confidence":  ud.get("apex_learn_threshold", 3),
                "min_score":       ud.get("apex_learn_score_min", 35),
                "size_mult":       ud.get("apex_size_mult", 1.0),
            },
            "trades": export_trades,
        }

        json_bytes = _json_mod.dumps(export_payload, indent=2, default=str).encode("utf-8")
        filename   = "apex_trades_" + str(datetime.now().date()) + ".json"
        await bot.send_document(
            chat_id=uid,
            document=_io_mod.BytesIO(json_bytes),
            filename=filename,
            caption=(
                "📁 *APEX Trade Log — " + str(datetime.now().date()) + "*\n"
                "Contains all APEX trades with post-exit snapshots.\n"
                "Use this to analyse which exits were correct."
            ),
            parse_mode="Markdown",
        )
    except Exception as _je:
        logger.error(f"APEX JSON export error: {_je}")

# ══ APEX S/R ENGINE + SMART DCA ══════════════════════════════════════════
APEX_SR_HISTORY_MAX    = 48
APEX_SR_ZONE_PROXIMITY = 0.07
APEX_SR_VOL_THRESHOLD  = 0.70
APEX_SR_DOUBLE_TOP_VOL = 0.65
APEX_SR_BREAKOUT_VOL   = 1.30

# ── S/R ENGINE CONSTANTS ──────────────────────────────────────────────────────
APEX_SR_HISTORY_MAX    = 48      # max candle snapshots stored per position (48 × 30s = 24 min)
APEX_SR_ZONE_PROXIMITY = 0.07    # 7% — how close to a zone before it's "active"
APEX_SR_VOL_THRESHOLD  = 0.70    # resistance zone vol must be ≥70% of peak vol to be significant
APEX_SR_DOUBLE_TOP_VOL = 0.65    # second resistance attempt with <65% vol of first = double top signal
APEX_SR_BREAKOUT_VOL   = 1.30    # vol spike 30%+ above resistance zone = breakout confirmation

# ── SMART DCA CONSTANTS ───────────────────────────────────────────────────────
APEX_DCA_MIN_DIP        = 0.15   # position must have pulled back ≥15% from peak to consider DCA
APEX_DCA_MIN_CX         = 0.75   # only DCA if position is still ≥0.75x (don't average into -25%+)
APEX_DCA_MAX_CX         = 1.30   # don't DCA if still near ATH (not a real dip)
APEX_DCA_BUY_PCT_FLOOR  = 45.0   # buy% must have dipped below this before recovery
APEX_DCA_BUY_PCT_RECOV  = 52.0   # buy% must recover above this to confirm bounce
APEX_DCA_MAX_HEAT       = 0.65   # don't DCA if heat ≥65%
APEX_DCA_SIZE_MULT      = 0.50   # DCA size = 50% of original position size
APEX_DCA_MAX_PER_POS    = 1      # max 1 DCA per position


# ══════════════════════════════════════════════════════════════════════════════
# S/R ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def apex_sr_record_candle(h: dict, price: float, mc: float, vol_m5: float, buy_pct: float) -> None:
    """
    Called every checker cycle (30s) for each APEX position.
    Stores a candle snapshot: {mc, price, vol, buy_pct, ts}
    Maintains a rolling window of APEX_SR_HISTORY_MAX snapshots.
    Separately tracks the volume-weighted MC levels for S/R zone calculation.
    """
    import time as _t
    # Coerce None to safe numeric defaults — DexScreener can return null fields
    mc       = mc       if isinstance(mc,       (int, float)) else 0.0
    vol_m5   = vol_m5   if isinstance(vol_m5,   (int, float)) else 0.0
    buy_pct  = buy_pct  if isinstance(buy_pct,  (int, float)) else 50.0
    price    = price    if isinstance(price,    (int, float)) else 0.0
    history = h.setdefault("sr_history", [])
    history.append({
        "mc":      mc,
        "price":   price,
        "vol":     vol_m5,
        "buy_pct": buy_pct,
        "ts":      _t.time(),
    })
    if len(history) > APEX_SR_HISTORY_MAX:
        h["sr_history"] = history[-APEX_SR_HISTORY_MAX:]

    # Update peak vol tracker (used for resistance zone significance scoring)
    if vol_m5 > h.get("sr_peak_vol", 0):
        h["sr_peak_vol"] = vol_m5


def apex_sr_compute_zones(h: dict) -> dict:
    """
    Analyses sr_history to identify significant S/R zones.

    Returns:
      resistance_zones : list[{mc, vol, strength}] — sorted by vol desc
      support_zones    : list[{mc, vol, strength}]
      active_resistance: float | None — nearest resistance MC above current
      active_support   : float | None — nearest support MC below current
    """
    history   = h.get("sr_history", [])
    peak_vol  = h.get("sr_peak_vol", 1)
    avg_price = h.get("avg_price", 0)
    result    = {
        "resistance_zones":  [],
        "support_zones":     [],
        "active_resistance": None,
        "active_support":    None,
    }

    if len(history) < 5 or peak_vol == 0:
        return result

    # ── Find volume-weighted levels ────────────────────────────────────────────
    # Group snapshots into MC buckets (5% width) and sum volume in each bucket
    bucket_size = 0.05   # 5% MC buckets
    buckets: dict = {}
    import time as _srt
    _now_ts = _srt.time()
    for snap in history:
        mc  = snap["mc"]
        vol = snap["vol"]
        if mc <= 0:
            continue
        # ── Zone age decay: recent candles weighted fully, old ones decay ─
        # Candles < 3 min old: weight 1.0
        # Candles 3–20 min old: weight decays linearly from 1.0 → 0.30
        # Candles > 20 min old: weight 0.30 (still meaningful but stale)
        _snap_age_s = _now_ts - snap.get("ts", _now_ts)
        _decay_min  = _snap_age_s / 60.0
        if _decay_min <= 3:
            _age_weight = 1.0
        elif _decay_min <= 20:
            _age_weight = 1.0 - 0.70 * ((_decay_min - 3) / 17.0)
        else:
            _age_weight = 0.30
        # Bucket key = round to nearest 5%
        entry_mc = h.get("avg_cost_mc", mc)
        if entry_mc <= 0:
            entry_mc = mc
        bucket_key = round(mc / (entry_mc * bucket_size)) * (entry_mc * bucket_size)
        b = buckets.setdefault(bucket_key, {"mc": bucket_key, "vol": 0, "buy_pcts": [], "count": 0, "weighted_vol": 0})
        b["vol"]         += vol
        b["weighted_vol"] += vol * _age_weight   # age-weighted volume
        b["buy_pcts"].append(snap["buy_pct"])
        b["count"]       += 1

    if not buckets:
        return result

    # ── Score each bucket ──────────────────────────────────────────────────────
    sorted_buckets = sorted(buckets.values(), key=lambda x: -x["vol"])
    current_mc     = history[-1]["mc"] if history else 0
    entry_mc_ref   = h.get("avg_cost_mc", current_mc) or current_mc

    for b in sorted_buckets:
        # Use age-weighted volume for strength — stale zones are weaker
        _wvol       = b.get("weighted_vol", b["vol"])
        vol_ratio   = _wvol / peak_vol if peak_vol > 0 else 0
        avg_buy_pct = sum(b["buy_pcts"]) / len(b["buy_pcts"]) if b["buy_pcts"] else 50
        strength    = round(vol_ratio * 10, 1)   # 0–10 strength score (age-weighted)

        if vol_ratio < 0.30:
            continue   # ignore low-volume zones

        zone = {"mc": b["mc"], "vol": b["vol"], "vol_ratio": vol_ratio,
                "avg_buy_pct": avg_buy_pct, "strength": strength}

        if b["mc"] > current_mc:
            # Above current price = resistance
            result["resistance_zones"].append(zone)
        else:
            # Below current price = support
            result["support_zones"].append(zone)

    # Sort: resistance by mc ascending (nearest first), support by mc descending (nearest first)
    result["resistance_zones"].sort(key=lambda x: x["mc"])
    result["support_zones"].sort(key=lambda x: -x["mc"])

    # Nearest active zones
    if result["resistance_zones"]:
        result["active_resistance"] = result["resistance_zones"][0]["mc"]
    if result["support_zones"]:
        result["active_support"] = result["support_zones"][0]["mc"]

    return result



def apex_detect_wave(h: dict) -> dict:
    """
    #24 Wave structure detection from sr_history candles.
    Uses price, volume and buy_pct to identify Elliott-style wave phases.

    Returns dict with:
        wave_state:  NONE / WAVE1 / RETRACE / WAVE2 / FAILED
        wave1_peak:  price at Wave 1 peak (0 if not detected)
        pre_wave:    price before Wave 1 started (entry reference)
        retrace_low: lowest price seen during retrace
        confidence:  0-3 signal count

    Requires >= 6 candles (3 minutes of data at 30s intervals).
    """
    history = h.get("sr_history", [])
    if len(history) < 6:
        return {"wave_state": "NONE", "wave1_peak": 0, "pre_wave": 0, "retrace_low": 0, "confidence": 0}

    avg = h.get("avg_price", 0)
    if avg <= 0:
        return {"wave_state": "NONE", "wave1_peak": 0, "pre_wave": 0, "retrace_low": 0, "confidence": 0}

    # Filter all three arrays together — only candles with valid price
    # This keeps indices aligned: peak_idx in prices == same in vols/buys
    _valid = [c for c in history if c.get("price", 0) > 0]
    prices   = [c["price"]   for c in _valid]
    vols     = [c["vol"]     for c in _valid]
    buys     = [c["buy_pct"] for c in _valid]

    if len(prices) < 6:
        return {"wave_state": "NONE", "wave1_peak": 0, "pre_wave": 0, "retrace_low": 0, "confidence": 0}

    pre_wave    = prices[0]          # price at first candle (approximation)
    peak_price  = max(prices)
    peak_idx    = prices.index(peak_price)
    cur_price   = prices[-1]
    avg_vol     = sum(vols) / len(vols) if vols else 0

    # ── Wave 1: price rose >40% in first half of history on volume spike ──────
    half = max(3, len(prices) // 2)
    first_half_peak = max(prices[:half])
    first_half_vols = vols[:half]
    avg_fh_vol = sum(first_half_vols) / len(first_half_vols) if first_half_vols else 0

    wave1_detected = (
        pre_wave > 0
        and first_half_peak / pre_wave >= 1.40          # rose 40%+ from entry candle
        and avg_fh_vol > 0
        and max(first_half_vols) >= avg_fh_vol * 2.5    # volume spike
    )

    if not wave1_detected:
        return {"wave_state": "NONE", "wave1_peak": 0, "pre_wave": pre_wave, "retrace_low": 0, "confidence": 0}

    wave1_peak = first_half_peak

    # ── Retrace: pullback from Wave 1 peak ───────────────────────────────────
    post_peak_prices = prices[peak_idx:]
    if not post_peak_prices:
        return {"wave_state": "WAVE1", "wave1_peak": wave1_peak, "pre_wave": pre_wave, "retrace_low": 0, "confidence": 1}

    retrace_low  = min(post_peak_prices)
    pullback_pct = (wave1_peak - retrace_low) / wave1_peak if wave1_peak > 0 else 0
    post_vols    = vols[peak_idx:] if peak_idx < len(vols) else []
    avg_post_vol = sum(post_vols) / len(post_vols) if post_vols else avg_vol

    retrace_confirmed = (
        pullback_pct >= 0.15                             # pulled back at least 15%
        and pullback_pct <= 0.55                         # but not a crash (< 55%)
        and retrace_low > pre_wave * 0.85                # held above pre-wave level
        and avg_post_vol < avg_fh_vol * 0.7              # volume dropped on retrace
    )

    if not retrace_confirmed:
        # Retrace too shallow or too deep — state is still WAVE1
        return {"wave_state": "WAVE1", "wave1_peak": wave1_peak, "pre_wave": pre_wave,
                "retrace_low": retrace_low, "confidence": 1}

    # ── Check for Wave 2 or Failed wave ──────────────────────────────────────
    # Failed: price broke below pre-wave level
    if retrace_low < pre_wave * 0.85:
        return {"wave_state": "FAILED", "wave1_peak": wave1_peak, "pre_wave": pre_wave,
                "retrace_low": retrace_low, "confidence": 2}

    # Wave 2: current price breaking above Wave 1 peak
    post_buys = buys[peak_idx:] if peak_idx < len(buys) else []
    avg_post_buy = sum(post_buys) / len(post_buys) if post_buys else 50

    wave2_signals = 0
    if cur_price >= wave1_peak * 0.95:   wave2_signals += 1   # approaching/exceeding wave1 peak
    if avg_post_buy >= 52:               wave2_signals += 1   # buy pressure recovering
    if cur_price > retrace_low * 1.15:   wave2_signals += 1   # bounced 15%+ from low

    if wave2_signals >= 2:
        return {"wave_state": "WAVE2", "wave1_peak": wave1_peak, "pre_wave": pre_wave,
                "retrace_low": retrace_low, "confidence": wave2_signals}

    return {"wave_state": "RETRACE", "wave1_peak": wave1_peak, "pre_wave": pre_wave,
            "retrace_low": retrace_low, "confidence": 2}


def apex_sr_trail_multiplier(h: dict, current_mc: float, current_vol: float, current_buy_pct: float = 50.0) -> float:
    """
    Returns a trail tightening multiplier based on S/R proximity.
    1.0 = no change to trail
    <1.0 = trail tightened (multiply trail_pct by this)

    Examples:
      0.5 → trail cut in half (e.g. 18% → 9%)
      0.4 → trail at 40% of normal (e.g. 18% → 7.2%)
      1.0 → no change
    """
    zones      = apex_sr_compute_zones(h)
    res_mc     = zones.get("active_resistance")
    peak_vol   = h.get("sr_peak_vol", 1) or 1
    history    = h.get("sr_history", [])

    if not res_mc or res_mc <= 0:
        return 1.0   # no data — no change

    distance_pct = (res_mc - current_mc) / current_mc if current_mc > 0 else 1.0

    # ── Double top detection ───────────────────────────────────────────────────
    # If we've visited resistance before and current vol < APEX_SR_DOUBLE_TOP_VOL × peak
    peak_visit_vol = h.get("sr_peak_visit_vol", 0)
    if peak_visit_vol > 0:
        vol_ratio = current_vol / peak_visit_vol if peak_visit_vol > 0 else 1.0
        if vol_ratio < APEX_SR_DOUBLE_TOP_VOL and distance_pct < APEX_SR_ZONE_PROXIMITY:
            # Double top forming — aggressively tighten
            return 0.35   # trail at 35% of normal

    # ── Breakout detection ─────────────────────────────────────────────────────
    if (distance_pct < APEX_SR_ZONE_PROXIMITY and
        current_vol > peak_vol * APEX_SR_BREAKOUT_VOL):
        # High volume through resistance = breakout — don't tighten
        h["sr_peak_visit_vol"] = current_vol   # update peak visit vol
        return 1.0

    # ── Approaching resistance ─────────────────────────────────────────────────
    if distance_pct < APEX_SR_ZONE_PROXIMITY:
        # Inside the resistance zone
        # Record visit volume (for double top detection on next approach)
        if current_vol > h.get("sr_peak_visit_vol", 0):
            h["sr_peak_visit_vol"] = current_vol
        # Tighten proportionally to how close we are
        closeness = 1.0 - (distance_pct / APEX_SR_ZONE_PROXIMITY)   # 0–1
        base_mult = max(0.30, 1.0 - (closeness * 0.55))
        # ── Gap 2: Buy pressure adjustment at resistance ──────────────────
        # Sellers dominating at resistance → extra tighten (rejection likely)
        # Buyers absorbing resistance heavily → ease trail (breakout possible)
        if current_buy_pct < 45:
            base_mult = max(0.25, base_mult * 0.80)   # sellers dominating — tighten 20% more
        elif current_buy_pct > 65:
            base_mult = min(1.0, base_mult * 1.25)    # buyers absorbing — ease trail
        return base_mult

    elif distance_pct < APEX_SR_ZONE_PROXIMITY * 2:
        # Approaching — start pre-tightening
        closeness = 1.0 - (distance_pct / (APEX_SR_ZONE_PROXIMITY * 2))
        return max(0.70, 1.0 - (closeness * 0.25))   # up to 25% reduction

    return 1.0   # far from resistance — no change


# ══════════════════════════════════════════════════════════════════════════════
# SMART DCA ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def apex_dca_should_consider(h: dict, info: dict, ud: dict) -> tuple:
    """
    Fast pre-check: should we even evaluate DCA for this position?
    Returns (True, reason) or (False, reason).
    """
    # Already DCA'd max times?
    if h.get("apex_dca_count", 0) >= APEX_DCA_MAX_PER_POS:
        return False, "Max DCA reached for this position"

    # Already DCA'd recently (30 min cooldown)?
    last_dca = h.get("apex_last_dca_ts", 0)
    import time as _t
    if _t.time() - last_dca < 1800:
        return False, "DCA cooldown active"

    avg   = h.get("avg_price", 0)
    price = info.get("price", 0)
    peak  = h.get("apex_peak_price", price)

    if avg <= 0 or price <= 0:
        return False, "Invalid price data"

    cx        = price / avg
    peak_cx   = peak / avg if avg > 0 else 1.0
    pullback  = (peak - price) / peak if peak > 0 else 0

    # Position too underwater — don't average down into a failing trade
    if cx < APEX_DCA_MIN_CX:
        return False, f"Position too underwater ({round(cx,2)}x < {APEX_DCA_MIN_CX}x min)"

    # Position still too close to ATH — not a real dip
    if pullback < APEX_DCA_MIN_DIP:
        return False, f"Dip too small ({round(pullback*100,1)}% < {round(APEX_DCA_MIN_DIP*100)}% min)"

    # Not a huge winner yet — no point DCA-ing at 1.05x after 20% pullback
    if peak_cx < 1.40:
        return False, f"Peak too low ({round(peak_cx,2)}x) — DCA reserved for meaningful winners"

    # Capital heat check
    heat = apex_capital_heat(ud)   # defined in bot.py scope
    if heat >= APEX_DCA_MAX_HEAT:
        return False, f"Heat too high ({round(heat*100,1)}%)"

    # ── Liquidity health — liq drained 15%+ since buy = token is dying ──────────
    liq_now    = info.get("liq", 0)
    liq_at_buy = h.get("liq_at_buy", liq_now)
    if liq_at_buy > 0:
        liq_drain = (liq_at_buy - liq_now) / liq_at_buy
        if liq_drain >= 0.15:
            return False, f"Liq drained {round(liq_drain*100,1)}% since entry — token may be dead"

    # ── #27 Wave structure — only DCA on confirmed Wave 1 retrace ────────────
    # Run wave detection and only allow DCA when wave confirms a structured
    # pullback. RETRACE = Wave1 completed, pulling back with volume drop.
    # NONE / FAILED / WAVE1 without retrace → skip DCA (not the right moment).
    _wave = apex_detect_wave(h)
    _wave_state = _wave.get("wave_state", "NONE")
    if _wave_state not in ("RETRACE", "NONE"):
        # Only block DCA for FAILED or WAVE1 (no retrace yet)
        # NONE = not enough data → allow DCA by existing rules
        # RETRACE = ideal DCA moment → allow
        # WAVE2 = already recovering → allow
        if _wave_state == "FAILED":
            return False, f"Wave structure FAILED — price broke below pre-wave level"
        # WAVE1 with no retrace yet — not the right entry point
        if _wave_state == "WAVE1":
            return False, f"Wave1 in progress — waiting for retrace before DCA"

    # ── Token age — if it's over 4h old and losing, it won't recover ─────────
    age_h = info.get("age_h") or 0
    if age_h > 4.0 and cx < 1.0:
        return False, f"Token {round(age_h,1)}h old and underwater — no recovery expected"

    # ── Must have a green M5 candle with real volume to confirm life ──────────
    ch_m5  = info.get("ch_m5", 0)
    vol_m5 = info.get("vol_m5", 0)
    vol_h1 = info.get("vol_h1", 0)
    avg_5m = vol_h1 / 12 if vol_h1 > 0 else 0
    if ch_m5 <= 0:
        return False, f"No green M5 candle — price still falling, not a bounce"
    if avg_5m > 0 and vol_m5 < avg_5m * 0.3:
        return False, f"M5 volume dead ({round(vol_m5)} vs avg {round(avg_5m)}) — no real buying"

    # ── Sell domination check — if sells overwhelming, this is breakdown ──────
    buy_pct_m5 = info.get("buy_pct_m5", info.get("buy_pct", 50))
    if buy_pct_m5 < 40:
        return False, f"M5 sell dominated ({buy_pct_m5}% buys) — not safe to add"

    # ── Balance check — need at least $5 to DCA ───────────────────────────────
    orig_invested = h.get("total_invested", 0) + sum(
        d.get("amount", 0) for d in h.get("apex_dca_history", [])
    )
    dca_size = max(5.0, orig_invested * APEX_DCA_SIZE_MULT)
    if ud.get("balance", 0) < dca_size:
        return False, "Insufficient balance for DCA"

    return True, f"Pre-check passed (cx={round(cx,2)}x, pullback={round(pullback*100,1)}%)"


def apex_dca_confirm_bounce(h: dict, info: dict) -> tuple:
    """
    4-condition confirmation that this is a real bounce, not a breakdown.
    All 4 must pass for DCA to execute.

    Returns (True, signals_dict) or (False, signals_dict)
    """
    signals = {
        "buy_pct_m5":        info.get("buy_pct_m5", info.get("buy_pct", 50)),
        "buy_pct_h1":        info.get("buy_pct_h1", info.get("buy_pct", 50)),
        "vol_m5":            info.get("vol_m5", 0),
        "vol_h1":            info.get("vol_h1", 1),
        "threat":            h.get("apex_threat", "CLEAR"),
        "at_support":        False,
        "buy_pct_was_low":   h.get("sr_buy_pct_dipped", False),
        "conditions_met":    [],
        "conditions_failed": [],
    }

    # ── Condition 1: Threat is not RED or ORANGE ──────────────────────────────
    if signals["threat"] in ("RED", "ORANGE"):
        signals["conditions_failed"].append(f"Threat level {signals['threat']} — not a safe dip")
        return False, signals
    signals["conditions_met"].append("Threat level safe (CLEAR/YELLOW)")

    # ── Condition 2: Buy pressure dipped then recovered (bounce confirmation) ──
    # Track if buy_pct previously dipped below floor
    bpm5 = signals["buy_pct_m5"]
    if bpm5 < APEX_DCA_BUY_PCT_FLOOR:
        h["sr_buy_pct_dipped"] = True
        signals["buy_pct_was_low"] = True

    if h.get("sr_buy_pct_dipped") and bpm5 >= APEX_DCA_BUY_PCT_RECOV:
        signals["conditions_met"].append(f"Buy pressure recovered: {bpm5}% (was below {APEX_DCA_BUY_PCT_FLOOR}%)")
        h["sr_buy_pct_dipped"] = False   # reset after confirmation
    elif not h.get("sr_buy_pct_dipped"):
        # Hasn't dipped yet — no recovery to confirm
        signals["conditions_failed"].append(f"No buy pressure dip detected yet (m5={bpm5}%)")
    else:
        signals["conditions_failed"].append(f"Buy pressure not yet recovered ({bpm5}% < {APEX_DCA_BUY_PCT_RECOV}%)")

    # ── Condition 3: At or near support zone ─────────────────────────────────
    zones        = apex_sr_compute_zones(h)
    active_sup   = zones.get("active_support")
    current_mc   = info.get("mc", 0)

    if active_sup and current_mc > 0:
        dist_to_sup = abs(current_mc - active_sup) / current_mc
        if dist_to_sup <= APEX_SR_ZONE_PROXIMITY * 1.5:   # 10.5% proximity
            signals["at_support"] = True
            signals["conditions_met"].append(f"At support zone (MC {_mc_str(current_mc)} near {_mc_str(active_sup)})")
        else:
            signals["conditions_failed"].append(f"Not near support zone (dist={round(dist_to_sup*100,1)}%)")
    else:
        # No computed support yet — use entry MC as reference
        entry_mc = h.get("avg_cost_mc", current_mc) or current_mc
        if entry_mc > 0:
            dist = abs(current_mc - entry_mc) / entry_mc
            if dist <= 0.12:   # within 12% of entry MC = near entry support
                signals["at_support"] = True
                signals["conditions_met"].append(f"Near entry MC ({_mc_str(current_mc)}) — key level")
            else:
                signals["conditions_failed"].append("No support zone data yet")
        else:
            signals["conditions_failed"].append("No support zone data available")

    # ── Condition 4: Volume signature suggests accumulation not panic ──────────
    avg_5m_vol = signals["vol_h1"] / 12 if signals["vol_h1"] > 0 else 0
    vol_m5     = signals["vol_m5"]
    if avg_5m_vol > 0:
        vol_ratio = vol_m5 / avg_5m_vol
        if vol_ratio < 0.4:
            signals["conditions_failed"].append(f"Volume too low ({round(vol_ratio,1)}x avg) — no accumulation")
        elif vol_ratio > 3.0 and signals["buy_pct_m5"] < 50:
            signals["conditions_failed"].append(f"High vol + sell pressure ({vol_ratio:.1f}x avg, {bpm5}% buys) — panic")
        else:
            signals["conditions_met"].append(f"Volume healthy ({round(vol_ratio,1)}x avg baseline)")
    else:
        signals["conditions_met"].append("Volume check passed (no baseline)")

    # ── All 4 must pass ────────────────────────────────────────────────────────
    if signals["conditions_failed"]:
        return False, signals
    return True, signals


def _mc_str(mc: float) -> str:
    """Format MC for display."""
    if mc >= 1_000_000:
        return f"${mc/1_000_000:.1f}M"
    return f"${mc/1_000:.0f}K"


async def apex_try_smart_dca(app, uid: int, ud: dict, contract: str, h: dict, info: dict) -> bool:
    """
    Main DCA orchestrator. Called from apex_run_position_manager when
    position is in a dip. Returns True if DCA was executed.
    """
    # ── Pre-check ──────────────────────────────────────────────────────────────
    ok, pre_reason = apex_dca_should_consider(h, info, ud)
    if not ok:
        logger.debug(f"APEX DCA pre-check failed {contract}: {pre_reason}")
        return False

    # ── Bounce confirmation ────────────────────────────────────────────────────
    confirmed, signals = apex_dca_confirm_bounce(h, info)
    if not confirmed:
        failed = " | ".join(signals["conditions_failed"])
        logger.debug(f"APEX DCA not confirmed {contract}: {failed}")
        return False

    # ── Calculate DCA size ─────────────────────────────────────────────────────
    orig_invested = h.get("total_invested", 0)
    dca_size      = round(max(5.0, orig_invested * APEX_DCA_SIZE_MULT), 2)
    dca_size      = min(dca_size, ud.get("balance", 0))

    if dca_size < 2.0:
        return False

    # ── Execute DCA buy ────────────────────────────────────────────────────────
    import time as _t
    result = await do_buy_core(ud, uid, contract, dca_size, planned=True, mood="APEX-DCA")
    if not isinstance(result, tuple):
        logger.warning(f"APEX DCA buy failed: {result}")
        return False

    info_post, _ = result

    # ── Update DCA tracking ────────────────────────────────────────────────────
    h["apex_dca_count"]   = h.get("apex_dca_count", 0) + 1
    h["apex_last_dca_ts"] = _t.time()
    h["sr_buy_pct_dipped"]= False   # reset bounce tracker

    dca_history = h.setdefault("apex_dca_history", [])
    dca_history.append({
        "amount":    dca_size,
        "price":     info_post["price"],
        "mc":        info_post["mc"],
        "ts":        _t.time(),
        "signals":   {
            "buy_pct_m5": signals["buy_pct_m5"],
            "at_support":  signals["at_support"],
            "met":         signals["conditions_met"],
        }
    })

    # ── Notify user ────────────────────────────────────────────────────────────
    price   = info_post["price"]
    avg_new = h.get("avg_price", price)
    cx_new  = price / h.get("avg_price", price) if h.get("avg_price") else 1.0
    met_str = "\n".join(f"  \u2705 {c}" for c in signals["conditions_met"])

    try:
        await app.bot.send_message(
            chat_id=uid, parse_mode="Markdown",
            text=(
                "\U0001f4c9 *APEX SMART DCA EXECUTED*\n\n"
                "*$" + _md(h["symbol"]) + "*\n"
                "Added *" + money(dca_size) + "* at support\n"
                "New avg cost: *" + money(avg_new) + "*\n"
                "New avg MC: *" + _mc_str(h.get("avg_cost_mc", 0)) + "*\n"
                "Position now: *" + str(round(cx_new, 2)) + "x*\n\n"
                "*Why DCA'd:*\n" + met_str + "\n\n"
                "Trail resets at 1.5x from new avg entry\n"
                "Cash: *" + money(ud.get("balance", 0)) + "*"
            ),
            reply_markup=main_menu_kb()
        )
    except Exception as _ne:
        logger.error(f"APEX DCA notify: {_ne}")

    return True


# ══ META ENRICHMENT ══════════════════════════════════════════════════════
# ── Known rug template fingerprints ──────────────────────────────────────────
# These are phrases/patterns that appear repeatedly across serial rug operations.
# NOT penalising meme content — only recycled scam templates.
_RUG_PHRASES = [
    "100x guaranteed",
    "1000x guaranteed",
    "guaranteed returns",
    "risk-free",
    "can't go down",
    "based on sol",  # recycled template phrase
    "next big thing guaranteed",
    "buy now before it's too late",
    "last chance to get in",
    "team is fully doxxed",   # ironically a scam signal when unprovable
    "audit passed 100%",
    "lp locked forever",      # often copy-paste lie
    "no team tokens",         # copy-paste claim
    "renounced and locked",   # template phrase
    "the next pepe",
    "the next shib",
    "the next doge",
    "fairlaunch, no presale",  # recycled template
    "community owned",        # overused filler
    "diamond hands only",
    "to the moon guaranteed",
    "ape in now",
    "buy the dip now",
]

_PRESSURE_WORDS = [
    "hurry", "urgent", "last chance", "dont miss", "don't miss",
    "buy now", "ape now", "fomo", "limited time", "act fast",
    "selling fast", "almost gone",
]


async def enrich_token_meta(info: dict, item: dict) -> dict:
    """
    Analyses token description and metadata for rug fingerprints.
    Does NOT penalise for having no utility — this is memecoin context.
    Looks for: copy-paste templates, identity mismatches, pressure language,
    ghost metadata, suspicious name/description mismatches.

    Returns dict with keys:
      meta_description   : str — the raw description text (truncated)
      meta_flags         : list[str] — specific rug fingerprints detected
      meta_score         : int — 0 to 10 (10 = cleanest, 0 = most suspicious)
      meta_identity_ok   : bool — name/ticker/description tell coherent story
      meta_is_ghost      : bool — description is blank or one word
      meta_pressure_lang : bool — uses scammy urgency language
    """
    result = {
        "meta_description":    "",
        "meta_flags":          [],
        "meta_score":          7,          # default neutral-positive
        "meta_identity_ok":    True,
        "meta_is_ghost":       False,
        "meta_pressure_lang":  False,
    }

    # ── Gather raw text fields ────────────────────────────────────────────────
    desc     = (item.get("_pf_description") or item.get("description") or
                info.get("description") or "").strip()
    name     = (info.get("name") or "").strip().lower()
    symbol   = (info.get("symbol") or "").strip().lower()
    twitter  = info.get("twitter", "")
    telegram = info.get("telegram", "")

    result["meta_description"] = desc[:300] if desc else ""

    desc_lower = desc.lower()
    flags      = []
    score      = 7

    # ── Ghost metadata ────────────────────────────────────────────────────────
    if len(desc) <= 3:
        result["meta_is_ghost"] = True
        flags.append("Ghost description (blank or 1 word)")
        score -= 1   # soft penalty only — many legit memecoins have minimal desc

    # ── Rug template phrases ──────────────────────────────────────────────────
    matched_phrases = [p for p in _RUG_PHRASES if p in desc_lower]
    if matched_phrases:
        flags.append(f"Rug template phrase: '{matched_phrases[0]}'")
        score -= len(matched_phrases) * 2
        if len(matched_phrases) >= 2:
            flags.append(f"{len(matched_phrases)} template phrases (serial rugger pattern)")

    # ── Pressure language ─────────────────────────────────────────────────────
    pressure_hits = [p for p in _PRESSURE_WORDS if p in desc_lower]
    if pressure_hits:
        result["meta_pressure_lang"] = True
        flags.append(f"Pressure language: '{pressure_hits[0]}'")
        score -= 2

    # ── Identity coherence check ──────────────────────────────────────────────
    # Does the name/symbol appear in the description at all?
    # A team that wrote their own description almost always mentions their token.
    if desc and len(desc) > 20:
        name_in_desc   = (name in desc_lower or symbol in desc_lower or
                          symbol.replace("$","") in desc_lower)
        if not name_in_desc:
            # Only flag if description is substantial (not ghost)
            # Could be a meme token that's intentionally abstract
            pass   # soft — don't penalise, just note

    # ── Mismatched Twitter handle ─────────────────────────────────────────────
    if twitter:
        # Extract handle from URL
        handle = twitter.rstrip("/").split("/")[-1].lower().replace("@","")
        # Flag if handle shares no characters with name or symbol
        handle_clean = handle.replace("_","").replace("-","")
        sym_clean    = symbol.replace("$","").lower()
        name_clean   = "".join(c for c in name if c.isalpha())
        # Check for complete mismatch — handle has zero overlap with token identity
        if (len(handle_clean) > 3 and len(sym_clean) > 2 and
            handle_clean not in sym_clean and sym_clean not in handle_clean and
            name_clean not in handle_clean and handle_clean not in name_clean and
            not any(c in handle_clean for c in sym_clean[:3])):
            flags.append(f"Twitter handle mismatch: @{handle} vs ${symbol}")
            result["meta_identity_ok"] = False
            score -= 2

    # ── Suspiciously long or copy-heavy description ───────────────────────────
    if len(desc) > 800:
        # Very long descriptions on memecoins are often plagiarised from utility tokens
        flags.append("Unusually long description for a memecoin (possible copy-paste)")
        score -= 1

    # ── Score floor/ceiling ───────────────────────────────────────────────────
    result["meta_flags"] = flags
    result["meta_score"] = max(0, min(10, score))
    return result


async def enrich_twitter_momentum(info: dict, http_client) -> dict:
    """
    Checks Twitter/X signals for the token without requiring API key.
    Uses public nitter instances and Twitter oEmbed to get:
      - Account creation date (old = better)
      - Follower count
      - Recent tweet count / activity
      - Whether handle matches the token
      - Basic bot pattern detection (all tweets same structure)

    Returns dict:
      tw_followers      : int | None
      tw_account_age_d  : int | None — days old
      tw_recent_tweets  : int | None — tweets in last 7 days
      tw_is_fresh_acct  : bool — created within 7 days of token launch
      tw_momentum_score : int — 0–10
      tw_flags          : list[str]
      tw_verified       : bool — account exists and is accessible
    """
    result = {
        "tw_followers":       None,
        "tw_account_age_d":   None,
        "tw_recent_tweets":   None,
        "tw_is_fresh_acct":   False,
        "tw_momentum_score":  5,
        "tw_flags":           [],
        "tw_verified":        False,
    }

    twitter_url = info.get("twitter", "")
    if not twitter_url:
        result["tw_flags"].append("No Twitter link")
        result["tw_momentum_score"] = 3
        return result

    # Extract handle
    handle = twitter_url.rstrip("/").split("/")[-1].replace("@","").strip()
    if not handle or len(handle) < 2:
        result["tw_flags"].append("Unparseable Twitter URL")
        return result

    flags = []
    score = 5

    # ── Try Twitter oEmbed (public, no API key needed) ─────────────────────
    try:
        # Skip oEmbed if handle looks like a numeric tweet ID (not a username)
        if handle.isdigit() or (handle.startswith("2") and len(handle) > 15):
            result["tw_flags"].append("Numeric tweet ID — not a username")
            return result  # BUG FIX: was returning None, crashing info.update()
        oembed_url = f"https://publish.twitter.com/oembed?url=https://twitter.com/{handle}&omit_script=true"
        r = await http_client.get(oembed_url, timeout=6)
        if r.status_code == 200:
            result["tw_verified"] = True
            score += 1   # account exists
        elif r.status_code == 404:
            flags.append("Twitter account not found / suspended")
            result["tw_momentum_score"] = 0
            result["tw_flags"] = flags
            return result
    except Exception:
        pass   # oEmbed unavailable — don't fail the whole enrichment

    # ── Try nitter for richer data ─────────────────────────────────────────
    nitter_instances = [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.1d4.us",
        "https://nitter.kavin.rocks",
    ]
    profile_html = None
    for nitter in nitter_instances:
        try:
            r = await http_client.get(f"{nitter}/{handle}", timeout=7,
                                      headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and "tweet" in r.text.lower():
                profile_html = r.text
                break
        except Exception:
            continue

    if profile_html:
        import re as _re
        result["tw_verified"] = True

        # Extract follower count
        fol_match = _re.search(r'([\d,]+)\s*Followers', profile_html, _re.IGNORECASE)
        if fol_match:
            try:
                followers = int(fol_match.group(1).replace(",",""))
                result["tw_followers"] = followers
                if followers < 50:
                    flags.append(f"Very low followers: {followers}")
                    score -= 2
                elif followers < 300:
                    flags.append(f"Low followers: {followers}")
                    score -= 1
                elif followers > 5000:
                    score += 2   # real following
                elif followers > 1000:
                    score += 1
            except ValueError:
                pass

        # Account join date
        join_match = _re.search(r'Joined\s+(\w+\s+\d{4})', profile_html, _re.IGNORECASE)
        if join_match:
            try:
                from datetime import datetime as _dt
                joined = _dt.strptime(join_match.group(1), "%B %Y")
                age_d  = (_dt.now() - joined).days
                result["tw_account_age_d"] = age_d
                if age_d < 7:
                    result["tw_is_fresh_acct"] = True
                    flags.append(f"Account created {age_d}d ago — very fresh")
                    score -= 3
                elif age_d < 30:
                    flags.append(f"Account created {age_d}d ago")
                    score -= 1
                elif age_d > 365:
                    score += 2   # established account
            except Exception:
                pass

        # Count recent tweets (rough — count tweet containers in HTML)
        tweet_count = len(_re.findall(r'class="tweet-content"', profile_html))
        result["tw_recent_tweets"] = tweet_count
        if tweet_count == 0:
            flags.append("No recent tweets visible")
            score -= 2
        elif tweet_count >= 5:
            score += 1   # active posting

        # Bot pattern detection — if all tweet texts start identically
        tweet_texts = _re.findall(r'class="tweet-content[^"]*"[^>]*>(.*?)</div>', profile_html, _re.DOTALL)
        if len(tweet_texts) >= 3:
            # Strip HTML tags for comparison
            clean = [_re.sub(r'<[^>]+>', '', t).strip()[:30] for t in tweet_texts]
            # If 80%+ start with same 20 chars = bot pattern
            prefixes = [c[:20] for c in clean if len(c) >= 20]
            if prefixes and len(set(prefixes)) == 1 and len(prefixes) >= 3:
                flags.append("Bot pattern: all tweets identical structure")
                score -= 3

    else:
        # Nitter failed — can't get details, slight uncertainty penalty
        if result["tw_verified"]:
            pass   # account exists but can't get details
        else:
            flags.append("Twitter account unverifiable")
            score -= 1

    result["tw_flags"]           = flags
    result["tw_momentum_score"]  = max(0, min(10, score))
    return result


async def enrich_holder_distribution(contract: str, chain: str, http_client) -> dict:
    """
    Fetches top holder distribution.
    Solana: uses Solscan public API
    EVM:    uses DexScreener token page data

    Returns dict:
      holder_top10_pct     : float | None — % held by top 10 wallets
      holder_top1_pct      : float | None — % held by single largest wallet
      holder_fresh_pct     : float | None — % held by wallets <7 days old
      holder_count         : int | None
      holder_flags         : list[str]
      holder_score         : int — 0–10
      holder_distribution  : list[dict] — [{rank, pct, address_short, age_d}]
    """
    result = {
        "holder_top10_pct":    None,
        "holder_top1_pct":     None,
        "holder_fresh_pct":    None,
        "holder_count":        None,
        "holder_flags":        [],
        "holder_score":        6,
        "holder_distribution": [],
    }

    chain_lower = (chain or "").lower()
    flags = []
    score = 6

    # ── Solana: Solscan public API ────────────────────────────────────────────
    if chain_lower in ("solana", "sol"):
        try:
            # Solscan public API is deprecated and returns 404 for all tokens.
            # Returning empty result dict — holder data comes from Helius maker_pct instead.
            return result  # solscan_dead
            url = f"https://public-api.solscan.io/token/holders?tokenAddress={contract}&limit=20&offset=0"  # noqa: dead
            r = await http_client.get(url, timeout=8,
                                      headers={"accept": "application/json",
                                               "User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                data = r.json()
                holders_raw = data.get("data", [])
                total_supply = data.get("total", 1) or 1

                if holders_raw:
                    top_pcts = []
                    dist     = []
                    for i, h in enumerate(holders_raw[:10]):
                        amt = h.get("amount", 0)
                        pct = round(amt / total_supply * 100, 2) if total_supply > 0 else 0
                        top_pcts.append(pct)
                        addr = h.get("address", "")
                        dist.append({
                            "rank":  i + 1,
                            "pct":   pct,
                            "addr":  addr[:6] + "…" + addr[-4:] if len(addr) > 10 else addr,
                        })

                    result["holder_distribution"] = dist
                    result["holder_top10_pct"]    = round(sum(top_pcts), 2)
                    result["holder_top1_pct"]     = top_pcts[0] if top_pcts else None
                    result["holder_count"]        = data.get("total", None)

                    top10 = result["holder_top10_pct"]
                    top1  = result["holder_top1_pct"] or 0

                    # Score top10 concentration
                    if top10 > 60:
                        flags.append(f"Top 10 wallets hold {top10}% — extreme concentration")
                        score -= 4
                    elif top10 > 40:
                        flags.append(f"Top 10 wallets hold {top10}% — high concentration")
                        score -= 2
                    elif top10 < 20:
                        score += 2   # well distributed

                    # Score top1
                    if top1 > 20:
                        flags.append(f"Single wallet holds {top1}% — whale risk")
                        score -= 3
                    elif top1 > 10:
                        flags.append(f"Largest wallet: {top1}%")
                        score -= 1

                    # Check if top wallets all created around same time (coordinated)
                    # We can detect this if top 5 all have similar addresses (heuristic)
                    if len(dist) >= 3:
                        # If top 3 wallets each hold >8%, likely coordinated
                        top3_each = [d["pct"] for d in dist[:3]]
                        if all(p > 8 for p in top3_each):
                            flags.append("Top 3 wallets each >8% — possible coordinated hold")
                            score -= 2

        except Exception as _se:
            logger.debug(f"Solscan holder fetch failed: {_se}")

    # ── EVM: DexScreener public token API ─────────────────────────────────────
    elif chain_lower in ("ethereum", "eth", "base", "bsc", "arbitrum"):
        try:
            # Use DexScreener public /latest/dex/tokens endpoint which returns
            # pair data including boosts and profile data. Not a true holder list
            # but gives us market structure signals we can use.
            url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"
            r = await http_client.get(url, timeout=8,
                                      headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                data  = r.json()
                pairs = data.get("pairs", [])
                if pairs:
                    pair = pairs[0]
                    # Use txns/volume ratio as a concentration proxy
                    # High volume + very few txns = whale-dominated
                    txns_h1 = pair.get("txns", {}).get("h1", {})
                    buys_h1  = txns_h1.get("buys", 1) or 1
                    sells_h1 = txns_h1.get("sells", 1) or 1
                    vol_h1   = pair.get("volume", {}).get("h1", 0) or 0
                    avg_txn  = vol_h1 / (buys_h1 + sells_h1) if (buys_h1 + sells_h1) > 0 else 0
                    # If avg transaction > $5K, likely whale-dominated
                    if avg_txn > 5000:
                        flags.append(f"Avg txn ${avg_txn:,.0f} — whale-dominated activity")
                        score -= 2
                        result["holder_top1_pct"] = 30.0   # estimate — concentrated
                    elif avg_txn > 1000:
                        flags.append(f"Avg txn ${avg_txn:,.0f} — moderate concentration")
                        score -= 1
                    else:
                        score += 1   # distributed small txns
                    result["holder_count"] = buys_h1 + sells_h1
        except Exception as _evm_e:
            logger.debug(f"EVM holder distribution error: {_evm_e}")

    result["holder_flags"] = flags
    result["holder_score"] = max(0, min(10, score))
    return result


def _build_meta_prompt_block(info: dict) -> str:
    """
    Builds the META INTELLIGENCE section for the AI prompt.
    Only included if enrichment data is present.
    """
    lines = []

    # ── Token description ─────────────────────────────────────────────────────
    desc = info.get("meta_description", "")
    if desc:
        lines.append(f"Description: \"{desc[:200]}\"")
    else:
        lines.append("Description: [BLANK]")

    # ── Metadata flags ────────────────────────────────────────────────────────
    meta_flags = info.get("meta_flags", [])
    meta_score = info.get("meta_score", 7)
    lines.append(f"Metadata integrity score: {meta_score}/10")
    if meta_flags:
        lines.append("Metadata flags: " + " | ".join(meta_flags))
    if info.get("meta_is_ghost"):
        lines.append("⚠️ Ghost metadata — team put no effort into identity")
    if info.get("meta_pressure_lang"):
        lines.append("⚠️ Pressure/scam language detected in description")
    if not info.get("meta_identity_ok", True):
        lines.append("⚠️ Twitter handle doesn't match token identity")

    # ── Twitter momentum ──────────────────────────────────────────────────────
    tw_score    = info.get("tw_momentum_score")
    tw_flags    = info.get("tw_flags", [])
    tw_verified = info.get("tw_verified", False)
    tw_fol      = info.get("tw_followers")
    tw_age      = info.get("tw_account_age_d")
    tw_fresh    = info.get("tw_is_fresh_acct", False)

    if tw_score is not None:
        lines.append(f"\nTwitter momentum score: {tw_score}/10")
        if tw_verified:
            lines.append("Account status: ✅ Verified accessible")
            if tw_fol is not None:
                lines.append(f"Followers: {tw_fol:,}")
            if tw_age is not None:
                lines.append(f"Account age: {tw_age} days old")
            if tw_fresh:
                lines.append("⚠️ Account created same week as token — likely dedicated scam account")
        else:
            lines.append("Account status: ❌ Unverifiable / suspended")
        if tw_flags:
            lines.append("Twitter flags: " + " | ".join(tw_flags))

    # ── Holder distribution ───────────────────────────────────────────────────
    h_top10 = info.get("holder_top10_pct")
    h_top1  = info.get("holder_top1_pct")
    h_count = info.get("holder_count")
    h_score = info.get("holder_score")
    h_flags = info.get("holder_flags", [])
    h_dist  = info.get("holder_distribution", [])

    if h_top10 is not None:
        lines.append(f"\nHolder distribution score: {h_score}/10")
        lines.append(f"Total holders: {h_count:,}" if h_count else "Total holders: unknown")
        lines.append(f"Top 10 wallets control: {h_top10}% of supply")
        if h_top1:
            lines.append(f"Largest single wallet: {h_top1}%")
        if h_dist:
            dist_str = "  ".join(f"#{d['rank']}:{d['pct']}%" for d in h_dist[:5])
            lines.append(f"Top 5: {dist_str}")
        if h_flags:
            lines.append("Holder flags: " + " | ".join(h_flags))
    else:
        lines.append("\nHolder distribution: Not available")

    return "\n".join(lines)


async def do_buy_core(ud: dict, uid: int, contract: str, usd_amount: float, planned: bool = True, mood: str = "", vault_buy: bool = False) -> str | tuple:
    if not check_daily(ud):
        return "Daily limit of " + str(ud["daily_limit"]) + " trades reached."
    mp = ud.get("max_positions")
    if mp and len(ud["holdings"]) >= mp and contract not in ud["holdings"]:
        return "Max positions (" + str(mp) + ") reached. Close a position first."
    rsk = ud.get("risk_pct")
    if rsk:
        hv = sum(h["total_invested"] for h in ud["holdings"].values())
        max_allowed = (ud["balance"] + hv) * rsk / 100
        if usd_amount > max_allowed:
            ud["broken"] += 1
            ud["streak"] = 0
            return "Risk limit! Max " + money(max_allowed) + " per trade (" + str(rsk) + "% rule)."
    if vault_buy:
        # APEX vault trade — check vault balance, not main balance
        if usd_amount > ud.get("apex_vault", 0.0):
            return "Insufficient vault balance. Vault has " + money(ud.get("apex_vault", 0.0)) + "."
    else:
        if usd_amount > ud["balance"]:
            return "Insufficient balance. You have " + money(ud["balance"]) + "."
    info = await get_token(contract)
    if not info:
        return "Token not found on DexScreener."
    tokens = usd_amount / info["price"]

    # ── Simulated trading costs (gas + DEX fee + slippage) ───────────────────
    _fee_cost = 0.0
    if SIM_FEES_ENABLED:
        _fee_cost = round(usd_amount * SIM_TOTAL_PCT + SIM_GAS_USD, 4)
        if vault_buy:
            _avail = ud.get("apex_vault", 0.0) - usd_amount
            _fee_cost = min(_fee_cost, max(0.0, _avail))
            ud["apex_vault"] = round(ud.get("apex_vault", 0.0) - _fee_cost, 4)
        else:
            if ud["balance"] < usd_amount + _fee_cost:
                _fee_cost = max(0.0, ud["balance"] - usd_amount)
            ud["balance"] = round(ud["balance"] - _fee_cost, 4)
        ud["total_fees_paid"] = round(ud.get("total_fees_paid", 0.0) + _fee_cost, 4)

    if vault_buy:
        # Clamp to 0 — floating point arithmetic on repeated operations can
        # produce -0.0001 which would show as negative vault balance.
        ud["apex_vault"] = max(0.0, round(ud.get("apex_vault", 0.0) - usd_amount, 4))
    else:
        ud["balance"] = max(0.0, round(ud["balance"] - usd_amount, 4))
    ud["daily_trades"] += 1
    if contract in ud["holdings"]:
        h = ud["holdings"][contract]
        nt = h["total_invested"] + usd_amount
        na = h["amount"] + tokens
        h["avg_price"]     = nt / na
        h["amount"]        = na
        h["total_invested"] = nt
        # Keep avg_cost_mc as weighted average MC at buy time
        cur_mc  = info.get("mc", 0)
        old_mc  = h.get("avg_cost_mc", cur_mc)
        old_inv = nt - usd_amount
        h["avg_cost_mc"] = ((old_mc * old_inv) + (cur_mc * usd_amount)) / nt if nt > 0 else cur_mc
    else:
        ud["holdings"][contract] = {
            "symbol":         info["symbol"],
            "name":           info["name"],
            "chain":          info["chain"],
            "amount":         tokens,
            "avg_price":      info["price"],
            "total_invested": usd_amount,
            "total_sold":     0.0,
            "avg_cost_mc":    info.get("mc", 0),
            "auto_sells":     [],
            "stop_loss_pct":  None,
            "bought_at":      datetime.now(),
            "liq_at_buy":     info.get("liq", 0),
            "journal":        "",
            "mood":           mood,
            "planned":        planned,
            "followed_plan":  None,
            "peak_price":     info["price"],   # tracks highest price seen for replay
            # ── History lists — appended by checker_job / event handlers ──────
            # price_history : {price, mc, ts}         — every checker cycle (20s)
            # liq_history   : {liq, ts}               — every checker cycle (20s)
            # stop_loss_history : {old, new, source, cx, ts} — every SL change
            # auto_sell_history : {x, pct, price, pnl, ts}   — every TP trigger
            # threat_history    : {from, to, cx, ts}          — APEX only, on change
            "price_history":      [],
            "liq_history":        [],
            "stop_loss_history":  [],
            "auto_sell_history":  [],
            "threat_history":     [],
        }
    if planned:
        ud["planned"] += 1
    else:
        ud["impulse"] += 1

    # Notify copy traders
    for follower_id, follower in list(users.items()):
        if follower.get("copy_trading") == uid and not follower.get("copy_paused"):
            if follower.get("balance", 0) >= usd_amount * 0.5:
                copy_amt = min(usd_amount, follower["balance"] * 0.1)
                copy_tokens = copy_amt / info["price"]
                follower["balance"] = round(follower["balance"] - copy_amt, 4)
                if contract in follower["holdings"]:
                    fh = follower["holdings"][contract]
                    nt = fh["total_invested"] + copy_amt
                    na = fh["amount"] + copy_tokens
                    fh["avg_price"] = nt / na
                    fh["amount"] = na
                    fh["total_invested"] = nt
                else:
                    follower["holdings"][contract] = {
                        "symbol": info["symbol"], "name": info["name"],
                        "chain": info["chain"], "amount": copy_tokens,
                        "avg_price": info["price"], "total_invested": copy_amt,
                        "total_sold": 0.0,
                        "auto_sells": [], "stop_loss_pct": None,
                        "bought_at": datetime.now(), "journal": "Copy trade from " + ud["username"],
                        "mood": "Copy Trade", "planned": True, "followed_plan": None,
                        "peak_price": info["price"],
                        "price_history": [], "liq_history": [],
                        "stop_loss_history": [], "auto_sell_history": [], "threat_history": [],
                    }
                save_user(follower_id, follower)
                try:
                    from telegram.ext import Application as _App
                    _bot = _App.get_current().bot
                    await _bot.send_message(
                        chat_id=follower_id,
                        parse_mode="Markdown",
                        text=(
                            "🔁 *COPY TRADE EXECUTED*\n\n"
                            "Copied @" + ud["username"] + "'s buy\n"
                            "*$" + _md(info["symbol"]) + "*  " + mc_str(info["mc"]) + "\n"
                            "Invested: *" + money(copy_amt) + "*\n"
                            "Price: *" + money(info["price"]) + "*\n"
                            "Cash left: *" + money(follower["balance"]) + "*"
                        ),
                        reply_markup=main_menu_kb()
                    )
                except Exception as _ce:
                    logger.warning(f"Copy trade notify failed for {follower_id}: {_ce}")

    # Overtrading alert
    today = datetime.now().date()
    counts = ud.get("daily_trade_counts", [])
    counts = [c for c in counts if c["date"] >= (today - timedelta(days=30))]
    today_entry = next((c for c in counts if c["date"] == today), None)
    if today_entry:
        today_entry["count"] = ud["daily_trades"]
    else:
        counts.append({"date": today, "count": ud["daily_trades"]})
    ud["daily_trade_counts"] = counts
    if len(counts) >= 3:
        avg = sum(c["count"] for c in counts[:-1]) / len(counts[:-1])
        ud["avg_daily_trades"] = round(avg, 1)

    save_user(uid, ud)
    return info, tokens


async def do_buy_msg(update, ud, uid, contract, amount, mood=""):
    # Risk calculator intercept
    if ud.get("risk_calc", True):
        info_pre = await get_token(contract)
        if info_pre:
            pending[uid] = {"action": "risk_confirm", "contract": contract, "amount": amount, "mood": mood}
            await update.message.reply_text(
                risk_card_text(ud, info_pre["symbol"], info_pre["mc"], amount),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(ud, "confirm_buy"), callback_data="rc_yes"),
                     InlineKeyboardButton(t(ud, "cancel"),      callback_data="rc_no")],
                ])
            )
            return
    msg = await update.message.reply_text("Executing buy...")
    result = await do_buy_core(ud, uid, contract, amount, mood=mood)
    if isinstance(result, str):
        await msg.edit_text(result, reply_markup=main_menu_kb())
        return
    info, tokens = result
    liq_warn = "\n\nWARNING: LOW LIQUIDITY" if info["liq"] < 50_000 else ""
    await msg.edit_text(
        "✅ *BUY EXECUTED*\n\n"
        "*" + info["name"] + " ($" + info["symbol"] + ")*\n"
        "Spent: *" + money(amount) + "*\n"
        "Got: *" + str(round(tokens, 4)) + " " + info["symbol"] + "*\n"
        "Price: *" + money(info["price"]) + "*\n"
        "MC: *" + mc_str(info["mc"]) + "*\n"
        "Liq: *" + money(info["liq"]) + "*\n"
        "Cash left: *" + money(ud["balance"]) + "*" + liq_warn,
        parse_mode="Markdown",
        reply_markup=buy_done_kb(contract)
    )


async def do_buy_query(q, ud, uid, contract, amount, mood=""):
    # Risk calculator intercept
    if ud.get("risk_calc", True):
        info_pre = await get_token(contract)
        if info_pre:
            pending[uid] = {"action": "risk_confirm", "contract": contract, "amount": amount, "mood": mood}
            await q.edit_message_text(
                risk_card_text(ud, info_pre["symbol"], info_pre["mc"], amount),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(ud, "confirm_buy"), callback_data="rc_yes"),
                     InlineKeyboardButton(t(ud, "cancel"),      callback_data="rc_no")],
                ])
            )
            return
    await q.edit_message_text("Executing buy...")
    result = await do_buy_core(ud, uid, contract, amount, mood=mood)
    if isinstance(result, str):
        await q.edit_message_text(result, reply_markup=main_menu_kb())
        return
    info, tokens = result
    liq_warn = "\n\nWARNING: LOW LIQUIDITY" if info["liq"] < 50_000 else ""
    await q.edit_message_text(
        "✅ *BUY EXECUTED*\n\n"
        "*" + info["name"] + " ($" + info["symbol"] + ")*\n"
        "Spent: *" + money(amount) + "*\n"
        "Got: *" + str(round(tokens, 4)) + " " + info["symbol"] + "*\n"
        "Price: *" + money(info["price"]) + "*\n"
        "MC: *" + mc_str(info["mc"]) + "*\n"
        "Liq: *" + money(info["liq"]) + "*\n"
        "Cash left: *" + money(ud["balance"]) + "*" + liq_warn,
        parse_mode="Markdown",
        reply_markup=buy_done_kb(contract)
    )


async def _send_pnl_card(
    reply_target,         # q.message  OR  update.message — anything with .reply_photo
    result: dict,
    ud: dict,
    uid: int,
    contract: str,
    info: dict,
    h: dict,
    cx: float,
    usd_amount: float,
    pct,                  # sell pct (0-1) or None
    done_kb: InlineKeyboardMarkup,
) -> None:
    """
    Shared PnL card sender — used by both do_sell_query and do_sell_msg.
    Generates and sends the trade card image on full close OR partial sell.
    Swallows all errors so a card failure never breaks the sell confirmation.
    """
    if result["closed"]:
        try:
            logs = trade_log.get(uid, [])
            tr = next((t for t in reversed(logs) if t["contract"] == contract), None)
            if tr:
                invested    = tr.get("invested", 0)
                pnl_pct_val = round((tr["realized_pnl"] / invested * 100), 2) if invested > 0 else 0
                card = generate_trade_card(
                    symbol=tr["symbol"], chain=tr.get("chain", "SOL"),
                    pnl_str=money(abs(tr["realized_pnl"])),
                    x_val=str(round(tr.get("x", 0), 2)),
                    held_h=str(tr["hold_h"]) + "h",
                    bought_str=money(invested),
                    position_str=money(tr.get("returned", 0)),
                    username=ud.get("username", "trader"),
                    pnl_pct=str(abs(pnl_pct_val)) + "%",
                    pnl_positive=tr["realized_pnl"] > 0,
                    closed_at=tr.get("closed_at"),
                )
                if card:
                    caption = (("✅ " if tr["realized_pnl"] > 0 else "❌ ") +
                        "$" + _md(tr["symbol"]) + "  " + str(round(tr.get("x", 0), 2)) + "x  " +
                        ("+" if tr["realized_pnl"] > 0 else "") + money(tr["realized_pnl"]))
                    await reply_target.reply_photo(photo=card, caption=caption, reply_markup=done_kb)
        except Exception as _ce:
            logger.warning(f"PnL card: {_ce}")
    else:
        # Partial sell — position still open
        try:
            h_remaining = ud.get("holdings", {}).get(contract)
            realized_so_far = result.get("realized", 0)
            if h_remaining:
                rem_val          = h_remaining["amount"] * info["price"]
                total_invested   = h_remaining.get("total_invested", 0) + usd_amount  # full original stake
                pnl_pct_val      = round((realized_so_far / max(total_invested, 0.01)) * 100, 2)
                card = generate_trade_card(
                    symbol=h.get("symbol", "?"), chain=h.get("chain", "SOL"),
                    pnl_str=money(abs(realized_so_far)),
                    x_val=str(round(cx, 2)),
                    held_h=str(result.get("hold_h", 0)) + "h",
                    bought_str=money(usd_amount),
                    position_str=money(rem_val),
                    username=ud.get("username", "trader"),
                    pnl_pct=str(abs(pnl_pct_val)) + "%",
                    pnl_positive=realized_so_far >= 0,
                    closed_at=None,
                    bought_label="Sold",
                    position_label="Remaining",
                )
                if card:
                    pct_label = str(int((pct or 0) * 100)) + "%"
                    caption   = (("✅ " if realized_so_far >= 0 else "❌ ") +
                        "Sold " + pct_label + " $" + h.get("symbol", "?") +
                        "  " + str(round(cx, 2)) + "x  " +
                        ("+" if realized_so_far >= 0 else "") + money(realized_so_far) +
                        "  |  Remaining: " + money(rem_val))
                    await reply_target.reply_photo(photo=card, caption=caption, reply_markup=done_kb)
        except Exception as _ce:
            logger.warning(f"Partial PnL card: {_ce}")


async def do_sell_query(q, ud, uid, contract, pct=None, usd=None):
    if contract not in ud["holdings"]:
        await q.edit_message_text("Position not found.", reply_markup=back_main())
        return
    info = await get_token(contract)
    if not info:
        await q.edit_message_text("Price unavailable.", reply_markup=back_main())
        return
    h = ud["holdings"][contract]
    cv         = h["amount"] * info["price"]
    usd_amount = cv * pct if pct is not None else min(usd, cv)
    usd_amount = min(usd_amount, cv)
    if usd_amount <= 0:
        await q.edit_message_text("Invalid sell amount.", reply_markup=back_main())
        return
    pending_targets = [t for t in h.get("auto_sells", []) if not t.get("triggered")]
    if pending_targets:
        ud["broken"]         += 1
        ud["streak"]          = 0
        h["followed_plan"]    = False
    else:
        ud["followed"]       += 1
        ud["streak"]         += 1
        ud["best_streak"]     = max(ud["best_streak"], ud["streak"])
    ud["daily_trades"] += 1
    avg    = h.get("avg_price", info["price"])
    result = sell_core(ud, uid, contract, usd_amount, info["price"])
    cx     = info["price"] / avg if avg > 0 else 0
    warn      = "\n\nSold before auto-sell targets - rule broken" if pending_targets else ""
    save_line = "\nAuto-saved: " + money(result["auto_saved"]) if result["auto_saved"] > 0 else ""
    share_kb  = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share This Trade", callback_data="share_" + contract)],
        [InlineKeyboardButton("🏠 Main Menu",        callback_data="mm")],
    ])
    await q.edit_message_text(
        "✅ *SELL EXECUTED*\n\n"
        "Received: *" + money(result["received"]) + "*\n"
        "Price: *" + money(info["price"]) + "*  |  *" + str(round(cx, 2)) + "x*\n"
        "Held: *" + str(result["hold_h"]) + "h*\n"
        "PnL: *" + pstr(result["realized"]) + "*\n"
        "Cash: *" + money(ud["balance"]) + "*" + save_line + warn,
        parse_mode="Markdown",
        reply_markup=share_kb
    )
    await _send_pnl_card(q.message, result, ud, uid, contract, info, h, cx, usd_amount, pct, share_kb)
    # ── Challenge check: only fires on manual sells (not APEX mood) ───────────
    if h.get("mood") not in ("APEX", "AI-Sniper", "APEX-DCA") and result.get("closed"):
        try:
            await _check_challenge(q.message.get_bot(), uid, ud)
        except Exception:
            pass


async def do_sell_msg(update, ud, uid, contract, pct=None, usd=None):
    if contract not in ud["holdings"]:
        await update.message.reply_text("Position not found.", reply_markup=back_main())
        return
    info = await get_token(contract)
    if not info:
        await update.message.reply_text("Price unavailable.", reply_markup=back_main())
        return
    h = ud["holdings"][contract]
    cv         = h["amount"] * info["price"]
    usd_amount = cv * pct if pct is not None else min(usd, cv)
    usd_amount = min(usd_amount, cv)
    pending_targets = [t for t in h.get("auto_sells", []) if not t.get("triggered")]
    if pending_targets:
        ud["broken"]         += 1
        ud["streak"]          = 0
        h["followed_plan"]    = False   # bug fix: was missing in do_sell_msg
    else:
        ud["followed"]       += 1
        ud["streak"]         += 1
        ud["best_streak"]     = max(ud["best_streak"], ud["streak"])
    ud["daily_trades"] += 1
    avg    = h.get("avg_price", info["price"])
    result = sell_core(ud, uid, contract, usd_amount, info["price"])
    cx     = info["price"] / avg if avg > 0 else 0
    save_line = "\nAuto-saved: " + money(result["auto_saved"]) if result["auto_saved"] > 0 else ""
    await update.message.reply_text(
        "✅ *SELL EXECUTED*\n\n"
        "Received: *" + money(result["received"]) + "*\n"
        "Price: *" + money(info["price"]) + "*  |  *" + str(round(cx, 2)) + "x*\n"
        "PnL: *" + pstr(result["realized"]) + "*\n"
        "Cash: *" + money(ud["balance"]) + "*" + save_line,
        parse_mode="Markdown",
        reply_markup=main_menu_kb()
    )
    await _send_pnl_card(update.message, result, ud, uid, contract, info, h, cx, usd_amount, pct, main_menu_kb())
    # ── Challenge check: only fires on manual sells ───────────────────────────
    if h.get("mood") not in ("APEX", "AI-Sniper", "APEX-DCA") and result.get("closed"):
        try:
            await _check_challenge(update.get_bot(), uid, ud)
        except Exception:
            pass


async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u:
        return  # Ignore channel posts — no user attached
    if users.get(u.id, {}).get("admin_banned"):
        await update.message.reply_text("⛔ Your account has been suspended. Contact support.")
        return
    if ACCESS_CONTROL_ENABLED and not is_admin(u.id):
        if not users.get(u.id, {}).get("access_approved"):
            await update.message.reply_text(
                "🔒 Access required. Contact @" + ACCESS_ADMIN_USERNAME + " to request access."
            )
            return
    ud = get_user(u.id, u.username or u.first_name)
    message = update.message          # shorthand used throughout handler
    text = message.text.strip()
    p = pending.get(u.id)

    async def _clean(keep_prompt_id: int | None = None):
        """Delete user's input + bot's prompt to keep chat clean."""
        try:
            await message.delete()
        except Exception:
            pass
        try:
            # Check keep_prompt_id first, then pending dict, then ud fallback
            _p = pending.get(u.id, {})
            prompt_id = keep_prompt_id or _p.get("_prompt_msg_id") or ud.get("_last_prompt_msg_id")
            if prompt_id:
                await ctx.bot.delete_message(chat_id=u.id, message_id=prompt_id)
                ud.pop("_last_prompt_msg_id", None)
        except Exception:
            pass

    if p:
        action = p.get("action", "")
        if not action:
            # Pending exists but no action — clear it and treat as CA
            pending.pop(u.id, None)
            p = None
        elif len(text) > 30 and action not in ("set_balance", "comp_bet", "comp_join", "acc_new", "sniper_channel_input", "qb_custom_input"):
            # Looks like a CA pasted while in a non-CA pending state — clear and scan
            pending.pop(u.id, None)
            p = None
            # Fall through to CA scanner below (skip the pending action block)

    if p and p.get("action", ""):
        action = p.get("action", "")
        if action == "set_balance":
            try:
                amt = float(text.replace("$", "").replace(",", ""))
                assert MIN_BALANCE <= amt <= MAX_BALANCE
                ud["balance"] = amt
                ud["starting_balance"] = amt
                ud["peak_equity"] = amt
                pending.pop(u.id, None)
                await update.message.reply_text(
                    "✅ Starting balance: *" + money(amt) + "*\n\nPaste any contract address to start trading!",
                    parse_mode="Markdown", reply_markup=main_menu_kb()
                )
            except Exception:
                await update.message.reply_text(
                    "❌ Enter a number between $1 and $10,000\nExample: 5000",
                    reply_markup=cancel_kb()
                )
            return

        elif action == "cfg_buy":
            try:
                amt = float(text.replace("$", ""))
                assert amt > 0
                ud["preset_buy"] = amt
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Default buy: *" + money(amt) + "*", parse_mode="Markdown", reply_markup=settings_kb(ud))
                else:
                    await message.reply_text("✅ Default buy: *" + money(amt) + "*", parse_mode="Markdown", reply_markup=settings_kb(ud))
            except Exception:
                await message.reply_text("❌ Enter a number like 100", reply_markup=cancel_kb())
            return

        elif action == "cfg_sell":
            raw = text.replace("$", "")
            try:
                if raw.endswith("%"):
                    pct = float(raw[:-1])
                    assert 0 < pct <= 100
                    ud["preset_sell"] = str(int(pct)) + "%"
                else:
                    amt = float(raw)
                    assert amt > 0
                    ud["preset_sell"] = amt
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Default sell: *" + text + "*", parse_mode="Markdown", reply_markup=settings_kb(ud))
                else:
                    await message.reply_text("✅ Default sell: *" + text + "*", parse_mode="Markdown", reply_markup=settings_kb(ud))
            except Exception:
                await message.reply_text("❌ Enter 50% or 200", reply_markup=cancel_kb())
            return

        elif action == "cfg_risk":
            try:
                pct = float(text.replace("%", ""))
                assert 0 < pct <= 100
                ud["risk_pct"] = pct
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Max risk: *" + str(pct) + "%* per trade", parse_mode="Markdown", reply_markup=settings_kb(ud))
                else:
                    await message.reply_text("✅ Max risk: *" + str(pct) + "%* per trade", parse_mode="Markdown", reply_markup=settings_kb(ud))
            except Exception:
                await message.reply_text("❌ Enter a number like 10", reply_markup=cancel_kb())
            return

        elif action == "cfg_maxpos":
            try:
                n = int(text)
                assert n > 0
                ud["max_positions"] = n
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Max positions: *" + str(n) + "*", parse_mode="Markdown", reply_markup=settings_kb(ud))
                else:
                    await message.reply_text("✅ Max positions: *" + str(n) + "*", parse_mode="Markdown", reply_markup=settings_kb(ud))
            except Exception:
                await message.reply_text("❌ Enter a number like 5", reply_markup=cancel_kb())
            return

        elif action == "cfg_daily":
            try:
                n = int(text)
                assert n > 0
                ud["daily_limit"] = n
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Daily limit: *" + str(n) + "* trades", parse_mode="Markdown", reply_markup=settings_kb(ud))
                else:
                    await message.reply_text("✅ Daily limit: *" + str(n) + "* trades", parse_mode="Markdown", reply_markup=settings_kb(ud))
            except Exception:
                await message.reply_text("❌ Enter a number like 10", reply_markup=cancel_kb())
            return

        elif action == "cfg_autosave":
            try:
                pct = float(text.replace("%", ""))
                assert 0 < pct <= 100
                ud["auto_save_pct"] = pct
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Auto-save: *" + str(pct) + "%* of profits", parse_mode="Markdown", reply_markup=settings_kb(ud))
                else:
                    await message.reply_text("✅ Auto-save: *" + str(pct) + "%* of profits", parse_mode="Markdown", reply_markup=settings_kb(ud))
            except Exception:
                await message.reply_text("❌ Enter a percentage like 20", reply_markup=cancel_kb())
            return

        elif action == "cfg_target":
            try:
                amt = float(text.replace("$", "").replace(",", ""))
                assert amt > 0
                ud["target_equity"] = amt
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Target equity: *" + money(amt) + "*", parse_mode="Markdown", reply_markup=settings_kb(ud))
                else:
                    await message.reply_text("✅ Target equity: *" + money(amt) + "*", parse_mode="Markdown", reply_markup=settings_kb(ud))
            except Exception:
                await message.reply_text("❌ Enter a number like 10000", reply_markup=cancel_kb())
            return

        elif action == "buy_custom":
            contract = p["contract"]
            try:
                amt = float(text.replace("$", "").replace(",", ""))
                assert amt > 0
                if ud.get("mood_tracking", True):
                    pending[u.id] = {"action": "buy_mood", "contract": contract, "amount": amt}
                    await update.message.reply_text(
                        "🧠 *MOOD CHECK*\n\nWhy are you buying this?\n\n"
                        "1 - Research\n2 - Chart looks good\n3 - Community tip\n4 - FOMO\n5 - Gut feeling\n\nReply with a number:",
                        parse_mode="Markdown", reply_markup=cancel_kb()
                    )
                else:
                    pending.pop(u.id, None)
                    await do_buy_msg(update, ud, u.id, contract, amt)
            except Exception:
                await update.message.reply_text("❌ Enter a number like 200", reply_markup=cancel_kb())
            return

        elif action == "buy_mood":
            contract = p["contract"]
            amount = p["amount"]
            mood_map = {
                "1": "Research",
                "2": "Chart looks good",
                "3": "Community tip",
                "4": "FOMO",
                "5": "Gut feeling",
            }
            mood = mood_map.get(text.strip(), text.strip())
            pending.pop(u.id, None)
            # Overtrading check
            avg = ud.get("avg_daily_trades", 0)
            today_count = ud.get("daily_trades", 0)
            if avg > 0 and today_count >= avg * 1.5:
                await update.message.reply_text(
                    "⚠️ *OVERTRADING ALERT*\n\n"
                    "You have made " + str(today_count) + " trades today.\n"
                    "Your daily average is " + str(ud['avg_daily_trades']) + " trades.\n\n"
                    "Are you sure you want to continue?",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Yes, Continue", callback_data="ot_yes_" + contract + "_" + str(amount) + "_" + mood)],
                        [InlineKeyboardButton("No, Stop", callback_data="mm")],
                    ])
                )
                return
            await do_buy_msg(update, ud, u.id, contract, amount, mood=mood)
            return

        elif action == "sell_custom":
            contract = p["contract"]
            if contract not in ud["holdings"]:
                pending.pop(u.id, None)
                await update.message.reply_text("❌ Position not found", reply_markup=back_main())
                return
            raw = text.replace("$", "")
            try:
                if raw.endswith("%"):
                    pct = float(raw[:-1]) / 100
                    await do_sell_msg(update, ud, u.id, contract, pct=pct)
                else:
                    await do_sell_msg(update, ud, u.id, contract, usd=float(raw))
                pending.pop(u.id, None)
            except Exception:
                await update.message.reply_text("❌ Enter 50% or 200", reply_markup=cancel_kb())
            return

        elif action == "as_custom":
            contract = p["contract"]
            if contract not in ud["holdings"]:
                pending.pop(u.id, None)
                await update.message.reply_text("❌ Position not found", reply_markup=back_main())
                return
            parts = text.split()
            if len(parts) % 2 != 0:
                await update.message.reply_text("❌ Format: 50% 2x 100% 5x", reply_markup=cancel_kb())
                return
            try:
                targets = []
                for i in range(0, len(parts), 2):
                    pct = float(parts[i].replace("%", "")) / 100
                    x = float(parts[i+1].lower().replace("x", ""))
                    assert 0 < pct <= 1 and x > 1
                    targets.append({"pct": pct, "x": x, "triggered": False})
                targets.sort(key=lambda t: t["x"])
                ud["holdings"][contract]["auto_sells"] = targets
                h = ud["holdings"][contract]
                lines = ["✅ *Auto-sells set for $" + h["symbol"] + "*\n"]
                for t in targets:
                    lines.append("  " + str(int(t["pct"]*100)) + "% at " + str(t["x"]) + "x  (~" + money(h["avg_price"] * t["x"]) + ")")
                pending.pop(u.id, None)
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await update.message.reply_text("❌ Format: 50% 2x 100% 5x", reply_markup=cancel_kb())
            return

        elif action == "sl_custom":
            contract = p["contract"]
            try:
                pct = float(text.replace("%", ""))
                assert 0 < pct < 100
                if contract in ud["holdings"]:
                    h = ud["holdings"][contract]
                    import time as _tslc
                    h.setdefault("stop_loss_history", []).append({
                        "old":    h.get("stop_loss_pct"),
                        "new":    pct,
                        "source": "user_custom",
                        "cx":     None,
                        "ts":     _tslc.time(),
                    })
                    h["stop_loss_pct"] = pct
                    trigger = h["avg_price"] * (1 - pct / 100)
                    pending.pop(u.id, None)
                    await update.message.reply_text(
                        "✅ Stop loss: *" + str(pct) + "%* drop → " + money(trigger),
                        parse_mode="Markdown", reply_markup=back_main()
                    )
            except Exception:
                await update.message.reply_text("❌ Enter a number like 50", reply_markup=cancel_kb())
            return

        elif action == "journal":
            contract = p["contract"]
            if contract in ud["holdings"]:
                ud["holdings"][contract]["journal"] = text
                sym = ud["holdings"][contract]["symbol"]
                pending.pop(u.id, None)
                await update.message.reply_text("📝 Journal saved for $" + sym + ":\n\"" + text + "\"", reply_markup=back_main())
            else:
                pending.pop(u.id, None)
                await update.message.reply_text("❌ Position not found", reply_markup=back_main())
            return

        elif action == "limit_buy":
            contract = p["contract"]
            try:
                parts = text.split()
                target_price = float(parts[0].replace("$", ""))
                amount = float(parts[1].replace("$", "")) if len(parts) > 1 else (ud.get("preset_buy") or 0)
                assert target_price > 0 and amount > 0
                ud["limit_orders"].append({
                    "type": "buy", "contract": contract,
                    "symbol": p.get("symbol", "?"),
                    "target_price": target_price, "amount": amount,
                    "created_at": datetime.now(), "triggered": False, "cancelled": False,
                })
                pending.pop(u.id, None)
                await update.message.reply_text(
                    "✅ *Limit Buy Set*\n\nBuy " + money(amount) + " when price hits " + money(target_price),
                    parse_mode="Markdown", reply_markup=back_main()
                )
            except Exception:
                await update.message.reply_text("❌ Format: 0.005 100\n(price amount)", reply_markup=cancel_kb())
            return

        elif action == "limit_sell":
            contract = p["contract"]
            if contract not in ud["holdings"]:
                pending.pop(u.id, None)
                await update.message.reply_text("❌ Position not found", reply_markup=back_main())
                return
            h = ud["holdings"][contract]
            try:
                parts = text.split()
                target_price = float(parts[0].replace("$", ""))
                if len(parts) > 1:
                    raw = parts[1]
                    if raw.endswith("%"):
                        amount = h["total_invested"] * float(raw[:-1]) / 100
                    else:
                        amount = float(raw.replace("$", ""))
                else:
                    amount = h["total_invested"]
                assert target_price > 0 and amount > 0
                ud["limit_orders"].append({
                    "type": "sell", "contract": contract,
                    "symbol": h["symbol"],
                    "target_price": target_price, "amount": amount,
                    "created_at": datetime.now(), "triggered": False, "cancelled": False,
                })
                pending.pop(u.id, None)
                await update.message.reply_text(
                    "✅ *Limit Sell Set*\n\nSell " + money(amount) + " of $" + h["symbol"] + " when price hits " + money(target_price),
                    parse_mode="Markdown", reply_markup=back_main()
                )
            except Exception:
                await update.message.reply_text("❌ Format: 0.012 50%\n(price amount%)", reply_markup=cancel_kb())
            return

        elif action == "price_alert":
            contract = p["contract"]
            try:
                target = float(text.replace("$", ""))
                current = p.get("current_price", 0)
                direction = "above" if target > current else "below"
                symbol = p.get("symbol", "?")
                ud["price_alerts"].append({
                    "contract": contract, "symbol": symbol,
                    "target": target, "direction": direction, "triggered": False,
                })
                pending.pop(u.id, None)
                # Delete user input + old prompt to keep chat clean
                try:
                    await message.delete()
                except Exception:
                    pass
                # Always send a fresh confirmation — don't try to edit deleted prompt
                arrow = "⬆️" if direction == "above" else "⬇️"
                msg = (
                    "🔔 *Price Alert Set!*\n\n"
                    "$" + symbol + "\n"
                    + arrow + " Notify when price goes *" + direction + "* " + money(target)
                )
                await ctx.bot.send_message(
                    chat_id=u.id,
                    text=msg,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀ Back to Token", callback_data="btt_" + contract)
                    ]])
                )
            except (ValueError, KeyError):
                await message.reply_text("❌ Enter a valid price. Example: 0.002648", reply_markup=cancel_kb())
            return

        elif action == "ch_custom_target":
            try:
                target = float(text.replace("$","").replace(",",""))
                assert target > 0
                pending[u.id] = {"action": "ch_custom_capital", "target": target}
                await message.reply_text(
                    "✅ Goal: *" + money(target) + "*\n\n"
                    "Now enter your *starting capital* — this is the amount you're risking.\n\n"
                    "Example: `100` = you're challenging yourself to turn $100 into " + money(target) + "\n"
                    "_Challenge ends if you lose all of this amount._",
                    parse_mode="Markdown",
                    reply_markup=cancel_kb()
                )
            except (ValueError, AssertionError):
                await message.reply_text("❌ Enter a valid number. Example: 10000")
            return

        elif action == "ch_custom_capital":
            try:
                min_cap = float(text.replace("$","").replace(",",""))
                assert min_cap > 0
                pending[u.id] = {"action": "ch_custom_days", "target": pending[u.id].get("target", 10000), "min_capital": min_cap}
                await message.reply_text(
                    "✅ Starting capital: *" + money(min_cap) + "*\n\n"
                    "Now enter the *number of days* for the challenge:\nExample: 30",
                    parse_mode="Markdown",
                    reply_markup=cancel_kb()
                )
            except (ValueError, AssertionError):
                await message.reply_text("❌ Enter a valid number greater than 0. Example: 100")
            return

        elif action == "ch_custom_days":
            try:
                days = int(text.strip())
                target  = pending[u.id].get("target", 10000)
                min_cap = pending[u.id].get("min_capital", 100.0)
                _start_date_c = datetime.now().date()
                _end_date_c   = _start_date_c + timedelta(days=days - 1)
                ud["challenge"] = {
                    "start_eq":    min_cap,   # kept for legacy card generation
                    "target_eq":   target,
                    "min_capital": min_cap,   # = starting capital / risk amount
                    "days":        days,
                    "started":     datetime.now().isoformat(),
                    "end_date":    _end_date_c.isoformat(),
                    "ended":       False,
                }
                del pending[u.id]
                save_user(u.id, ud)
                await message.reply_text(
                    "🎯 *Challenge Started!*\n\n"
                    "🏁 Starting capital: *" + money(min_cap) + "*\n"
                    "🎯 Goal:             *" + money(target) + "*\n"
                    "📅 Duration:         *" + str(days) + " days*\n\n"
                    "_Tracks realized profit from your trades._\n"
                    "_Savings & vault don't count — only what you earn from trading._\n\n"
                    "Good luck! 💪",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🎯 View Challenge", callback_data="v_challenge"),
                        InlineKeyboardButton("🏠 Menu",           callback_data="mm"),
                    ]])
                )
            except (ValueError, KeyError):
                await message.reply_text("❌ Enter a valid number of days. Example: 30")
            return

        elif action == "acc_new":
            name = text.lower().strip().replace(" ", "_")
            if not name:
                await message.reply_text("❌ Please enter a valid name.")
                return
            if not ud.get("accounts"):
                ud["accounts"] = {}
            if name in ud["accounts"]:
                await message.reply_text(f"❌ Account *{name}* already exists!", parse_mode="Markdown", reply_markup=cancel_kb())
                return
            ud["accounts"][name] = {"balance": 5000.0, "holdings": {}, "savings": 0.0}
            del pending[u.id]
            await message.reply_text(
                f"✅ Account *{name}* created!\n\n💰 Balance: $5,000\n\nSwitch to it from Accounts menu.",
                parse_mode="Markdown", reply_markup=main_menu_kb()
            )
            return



        elif action == "sniper_log_ch_input":
            # User pasted dump-log channel ID
            raw = text.strip().replace(" ", "")
            try:
                _lch_id = int(raw)
            except ValueError:
                await message.reply_text(
                    "\u274c Invalid ID. Should be a number like `-1001234567890`.\n\nTry again or tap Cancel.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u25c0 Cancel", callback_data="sniper_log_ch_menu")]])
                )
                return
            await message.reply_text("\u23f3 Testing connection to channel...")
            try:
                await ctx.bot.send_message(
                    chat_id=_lch_id,
                    text="\u2705 *APEX SNIPER BOT — Scan Log connected!*\n\nEvery scan result (SKIPs + PASSes) will stream here.",
                    parse_mode="Markdown"
                )
                try:
                    _lch_info = await ctx.bot.get_chat(_lch_id)
                    _lch_name = _lch_info.title or str(_lch_id)
                except Exception:
                    _lch_name = str(_lch_id)
                ud["sniper_log_channel"]      = _lch_id
                ud["sniper_log_channel_name"] = _lch_name
                ud["sniper_log_channel_on"]   = True
                del pending[u.id]
                save_user(u.id, ud)
                await message.reply_text(
                    "\u2705 *Scan Log Channel connected!*\n\n"
                    "\U0001f4e1 *" + _lch_name + "* will now receive every scan result.\n"
                    "SKIPs stream silently. SNIPEs ping with notification.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("\U0001f4ca Change Channel", callback_data="sniper_log_ch_setup")],
                        [InlineKeyboardButton("\U0001f5d1 Remove Channel",  callback_data="sniper_log_ch_remove")],
                        [InlineKeyboardButton("\u25c0 Back", callback_data="sniper_log_ch_menu")],
                    ])
                )
            except Exception as _lce:
                await message.reply_text(
                    "\u274c *Could not post to that channel.*\n\n"
                    "Make sure the bot is an admin in the channel, then try again.\n\n"
                    "_Error: " + str(_lce)[:100] + "_",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u25c0 Cancel", callback_data="sniper_log_ch_menu")]])
                )
            return

        elif action == "qb_custom_input":
            contract = p.get("contract", "")
            try:
                amt = float(text.replace("$","").replace(",","").strip())
                assert amt > 0
                ud["quick_buy_amount"] = amt
                del pending[u.id]
                try:
                    await message.delete()
                except Exception:
                    pass
                await ctx.bot.send_message(
                    chat_id=u.id,
                    text="✅ Quick Buy set to *$" + str(int(amt)) + "*\n\nTap ⚡ Quick Buy on any token card.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀ Back to Token", callback_data="btt_" + contract)
                    ]]) if contract else back_main()
                )
            except (ValueError, AssertionError):
                await message.reply_text("❌ Enter a valid amount. Example: 75", reply_markup=cancel_kb())
            return

        elif action == "sniper_channel_input":
            # User pasted a channel/group ID
            raw = text.strip().replace(" ", "")
            try:
                ch_id = int(raw)
            except ValueError:
                await message.reply_text(
                    "❌ Invalid ID. It should be a number like `-1001234567890`.\n\nTry again or tap Cancel.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Cancel", callback_data="sniper_adv_menu")]])
                )
                return
            # Test that bot can actually post to the channel
            await message.reply_text("⏳ Testing connection to channel...")
            try:
                test_msg = await ctx.bot.send_message(
                    chat_id=ch_id,
                    text="✅ *APEX SNIPER BOT connected!*\n\nAI Sniper signals will be posted here.",
                    parse_mode="Markdown"
                )
                # Try to get chat name
                try:
                    chat_info = await ctx.bot.get_chat(ch_id)
                    ch_name = chat_info.title or str(ch_id)
                except Exception:
                    ch_name = str(ch_id)
                ud["sniper_broadcast_channel"] = ch_id
                ud["sniper_broadcast_name"] = ch_name
                del pending[u.id]
                save_user(u.id, ud)
                adv_on = ud.get("sniper_advisory", False)
                notify = ud.get("sniper_adv_notify", True)
                await message.reply_text(
                    "✅ *Channel connected!*\n\n"
                    "📡 *" + ch_name + "* will now receive full AI signal cards.\n"
                    "Your DM will get a compact notification only.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(("🔴 Disable Advisory" if adv_on else "🟢 Enable Advisory"), callback_data="sniper_adv_toggle")],
                        [InlineKeyboardButton("📡 Change Channel", callback_data="sniper_channel_setup")],
                        [InlineKeyboardButton("🗑 Remove Channel", callback_data="sniper_channel_remove")],
                        [InlineKeyboardButton("◀ Back", callback_data="v_sniper")],
                    ])
                )
            except Exception as e:
                err = str(e)
                await message.reply_text(
                    "❌ *Could not post to that channel.*\n\n"
                    "Make sure *apex_sniper_bot* is an admin in the channel/group, then try again.\n\n"
                    "_Error: " + err[:100] + "_",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Cancel", callback_data="sniper_adv_menu")]])
                )
            return

        elif action == "wl_waiting":
            # User typed something while waiting for watchlist choice — ignore, remind them
            contract = pending[u.id].get("contract", "")
            await message.reply_text(
                "👇 Please tap a button above to set your alert, or tap Cancel.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Alert by Price",        callback_data="wl_add_price")],
                    [InlineKeyboardButton("Alert by Market Cap",   callback_data="wl_add_mc")],
                    [InlineKeyboardButton("No Alert — Just Watch", callback_data="mm")],
                    [InlineKeyboardButton("◀ Back to Token",       callback_data="btt_" + contract)],
                ])
            )
            return

        elif action == "wl_target_price":
            try:
                target = float(text)
                contract = pending[u.id].get("contract","")
                if contract and ud.get("watchlist", {}).get(contract):
                    ud["watchlist"][contract]["target_price"] = target
                del pending[u.id]
                await message.reply_text(f"✅ Price alert set at ${target:.8g}", reply_markup=main_menu_kb())
            except (ValueError, KeyError):
                await message.reply_text("❌ Enter a valid price. Example: 0.00005")
            return

        elif action == "wl_target_mc":
            try:
                target = float(text)
                contract = pending[u.id].get("contract","")
                if contract and ud.get("watchlist", {}).get(contract):
                    ud["watchlist"][contract]["target_mc"] = target
                del pending[u.id]
                await message.reply_text(f"✅ MC alert set at {mc_str(target)}", reply_markup=main_menu_kb())
            except (ValueError, KeyError):
                await message.reply_text("❌ Enter a valid market cap. Example: 100000")
            return

        elif action == "comp_bet":
            # Step 1: user entered bet amount
            try:
                bet = float(text.replace("$","").replace(",",""))
                if bet < 0:
                    await message.reply_text("❌ Enter 0 for free or a positive amount.", reply_markup=cancel_kb())
                    return
                if bet > 0 and bet > ud["balance"]:
                    await message.reply_text(
                        f"❌ Not enough balance.\nYou have {money(ud['balance'])}\n\nEnter a lower amount or 0 for free:",
                        reply_markup=cancel_kb()
                    )
                    return
                # Move to step 2: ask days — show buttons for common durations
                pending[u.id] = {"action": "comp_days", "bet": bet}
                bet_label = "🆓 Free" if bet == 0 else money(bet) + " per player"
                await message.reply_text(
                    f"✅ Bet set: *{bet_label}*\n\n"
                    f"⏳ *Step 2/2 — How many days?*\n\n"
                    f"Tap a duration or type a number (1–90):",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("3 days",  callback_data=f"comp_days_3_{bet}"),
                         InlineKeyboardButton("7 days",  callback_data=f"comp_days_7_{bet}"),
                         InlineKeyboardButton("14 days", callback_data=f"comp_days_14_{bet}")],
                        [InlineKeyboardButton("30 days", callback_data=f"comp_days_30_{bet}"),
                         InlineKeyboardButton("60 days", callback_data=f"comp_days_60_{bet}"),
                         InlineKeyboardButton("90 days", callback_data=f"comp_days_90_{bet}")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="mm")],
                    ])
                )
            except ValueError:
                await message.reply_text("❌ Enter a number. Example: 500 or 0 for free.", reply_markup=cancel_kb())
            return

        elif action == "comp_days":
            # Step 2: user entered days → create competition
            try:
                days = int(text.strip())
                if days < 1 or days > 90:
                    await message.reply_text("❌ Enter between 1 and 90 days.", reply_markup=cancel_kb())
                    return
                bet = pending[u.id].get("bet", 0)
                import random, string
                code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
                _joined_at = datetime.now().isoformat()
                comp = {
                    "code":       code,
                    "creator_id": str(u.id),
                    "bet":        bet,
                    "pot":        bet,
                    "days":       days,
                    "end_time":   (datetime.now() + timedelta(days=days)).isoformat(),
                    "ended":      False,
                    "winner_paid": False,
                    "members":    {
                        str(u.id): {
                            "username":  ud["username"],
                            "joined_at": _joined_at,
                        }
                    }
                }
                if bet > 0:
                    ud["balance"] -= bet
                _competitions[code] = comp
                if not ud.get("competitions"):
                    ud["competitions"] = {}
                ud["competitions"][code] = True
                # Persist immediately so competition survives restart
                ud["_persisted_competitions"] = {
                    k: v for k, v in _competitions.items()
                    if k in ud.get("competitions", {})
                }
                save_user(u.id, ud)
                del pending[u.id]
                end_str = (datetime.now() + timedelta(days=days)).strftime("%b %d, %Y")
                pot_line = f"💰 Bet: {money(bet)} per player | Pot: {money(bet)}" if bet > 0 else "🆓 Free to join"
                await message.reply_text(
                    f"🏁 *COMPETITION CREATED!*\n\n"
                    f"📋 Code: `{code}`\n"
                    f"⏳ Duration: {days} days\n"
                    f"🏁 Ends: {end_str}\n"
                    f"{pot_line}\n\n"
                    f"Share code *{code}* with friends!\n"
                    f"Winner takes the entire pot 🏆",
                    parse_mode="Markdown", reply_markup=main_menu_kb()
                )
            except (ValueError, TypeError):
                # If they pasted a CA or something else, clear pending and scan it
                if len(text) > 20:
                    pending.pop(u.id, None)
                    await message.reply_text("⚠️ Competition cancelled. Scanning token...")
                    # Fall through to CA scanner below
                    p = None
                else:
                    await message.reply_text("❌ Enter a whole number of days. Example: 7", reply_markup=cancel_kb())
                    return

        elif action == "comp_join":
            # User entered competition code
            code = text.strip().upper()
            _comps = _competitions
            if code not in _comps:
                await message.reply_text(
                    "❌ Competition not found.\nCheck the code and try again.",
                    reply_markup=cancel_kb()
                )
                return
            comp = _comps[code]
            # Check already joined
            if str(u.id) in comp.get("members", {}):
                await message.reply_text("❌ You already joined this competition!", reply_markup=main_menu_kb())
                del pending[u.id]
                return
            # Check ended
            end_dt = datetime.fromisoformat(comp["end_time"])
            if datetime.now() > end_dt:
                await message.reply_text("❌ This competition has already ended.", reply_markup=main_menu_kb())
                del pending[u.id]
                return
            bet = comp.get("bet", 0)
            if bet > 0 and bet > ud["balance"]:
                await message.reply_text(
                    f"❌ Need {money(bet)} to join. Your balance: {money(ud['balance'])}",
                    reply_markup=cancel_kb()
                )
                return
            if bet > 0:
                ud["balance"] -= bet
                comp["pot"] = comp.get("pot", 0) + bet
            comp["members"][str(u.id)] = {
                "username":  ud["username"],
                "joined_at": datetime.now().isoformat(),
            }
            ud.setdefault("competitions", {})[code] = True
            # Persist competition to joiner's ud immediately
            ud["_persisted_competitions"] = {
                k: v for k, v in _competitions.items()
                if k in ud.get("competitions", {})
            }
            save_user(u.id, ud)
            # Also update creator's persisted copy so they see the new member
            creator_id = int(comp.get("creator_id", 0))
            if creator_id and creator_id in users:
                creator_ud = users[creator_id]
                creator_ud["_persisted_competitions"] = {
                    k: v for k, v in _competitions.items()
                    if k in creator_ud.get("competitions", {})
                }
                save_user(creator_id, creator_ud)
            del pending[u.id]
            days_left = max(0, (end_dt - datetime.now()).days)
            pot_line = f"💰 Pot: {money(comp.get('pot', 0))}" if bet > 0 else "🆓 Free competition"
            await message.reply_text(
                f"✅ *Joined Competition!*\n\n"
                f"📋 Code: `{code}`\n"
                f"{pot_line}\n"
                f"👥 Players: {len(comp['members'])}\n"
                f"⏳ Days left: {days_left}\n\n"
                f"Trade hard! Winner takes all 🏆",
                parse_mode="Markdown", reply_markup=main_menu_kb()
            )
            return

        elif action == "sav_deposit":
            try:
                amt = float(text.replace("$", "").replace(",", ""))
                assert 0 < amt <= ud["balance"]
                ud["balance"] -= amt
                ud["savings"] += amt
                pending.pop(u.id, None)
                await update.message.reply_text(
                    "✅ *" + money(amt) + "* moved to savings\n\nTrading: " + money(ud["balance"]) + "\nSavings: " + money(ud["savings"]),
                    parse_mode="Markdown", reply_markup=back_main()
                )
            except Exception:
                await update.message.reply_text("❌ Max you can save: " + money(ud["balance"]), reply_markup=cancel_kb())
            return

        elif action == "sav_withdraw":
            try:
                amt = float(text.replace("$", "").replace(",", ""))
                assert 0 < amt <= ud["savings"]
                ud["savings"] -= amt
                ud["balance"] += amt
                pending.pop(u.id, None)
                await update.message.reply_text(
                    "✅ *" + money(amt) + "* moved to trading\n\nTrading: " + money(ud["balance"]) + "\nSavings: " + money(ud["savings"]),
                    parse_mode="Markdown", reply_markup=back_main()
                )
            except Exception:
                await update.message.reply_text("❌ Max you can withdraw: " + money(ud["savings"]), reply_markup=cancel_kb())
            return

        # ── SNIPER CONFIG INPUTS ───────────────────────────────────────────────
        elif action == "sniper_score":
            try:
                val = int(text)
                assert 0 <= val <= 100
                ud.setdefault("sniper_filters", {})["min_score"] = val
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Min score set to *" + str(val) + "/100*", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_filters_menu")]]))
                else:
                    await message.reply_text("✅ Min score set to *" + str(val) + "/100*", parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await message.reply_text("❌ Enter a number between 0 and 100", reply_markup=cancel_kb())
            return

        elif action == "sniper_liq":
            try:
                val = float(text.replace("$", "").replace(",", ""))
                assert val >= 0
                ud.setdefault("sniper_filters", {})["min_liq"] = val
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Min liquidity set to *" + money(val) + "*", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_filters_menu")]]))
                else:
                    await message.reply_text("✅ Min liquidity set to *" + money(val) + "*", parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await message.reply_text("❌ Enter a number like 15000", reply_markup=cancel_kb())
            return

        elif action == "sniper_mc":
            try:
                parts = text.replace("$", "").replace(",", "").split()
                assert len(parts) == 2
                min_mc, max_mc = float(parts[0]), float(parts[1])
                assert 0 < min_mc < max_mc
                ud.setdefault("sniper_filters", {})["min_mc"] = min_mc
                ud["sniper_filters"]["max_mc"] = max_mc
                save_user(u.id, ud)
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ MC range: *" + mc_str(min_mc) + "* → *" + mc_str(max_mc) + "*", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_filters_menu")]]))
                else:
                    await message.reply_text("✅ MC range: *" + mc_str(min_mc) + "* → *" + mc_str(max_mc) + "*", parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await message.reply_text("❌ Format: min max\nExample: 20000 1000000", reply_markup=cancel_kb())
            return

        elif action == "sniper_age":
            try:
                val = float(text)
                assert val > 0
                ud.setdefault("sniper_filters", {})["max_age_h"] = val
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Max age: *" + str(val) + "h*", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_filters_menu")]]))
                else:
                    await message.reply_text("✅ Max age: *" + str(val) + "h*", parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await message.reply_text("❌ Enter hours like 6", reply_markup=cancel_kb())
            return

        elif action == "sniper_amt":
            try:
                val = float(text.replace("$", "").replace(",", ""))
                assert val > 0
                ud.setdefault("sniper_filters", {})["buy_amount"] = val
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Buy amount: *" + money(val) + "* per snipe", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_filters_menu")]]))
                else:
                    await message.reply_text("✅ Buy amount: *" + money(val) + "* per snipe", parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await message.reply_text("❌ Enter a number like 100", reply_markup=cancel_kb())
            return

        elif action == "sniper_sl_pct":
            try:
                val = float(text.replace("%", ""))
                assert 5 <= val <= 95
                ud["sniper_auto_sl_pct"] = val
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Auto SL set to *" + str(val) + "%* drop", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_auto_menu")]]))
                else:
                    await message.reply_text("✅ Auto SL set to *" + str(val) + "%* drop", parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await message.reply_text("❌ Enter a % between 5 and 95", reply_markup=cancel_kb())
            return

        elif action == "sniper_tp_x":
            try:
                parts = text.replace("x","").split()
                xs = [float(p2) for p2 in parts]
                assert all(x > 1 for x in xs) and len(xs) >= 1
                xs.sort()
                ud["sniper_auto_tp_x"] = xs
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                tp_str = "  |  ".join(str(x) + "x" for x in xs)
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Take profit targets: *" + tp_str + "*", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_auto_menu")]]))
                else:
                    await message.reply_text("✅ Take profit targets: *" + tp_str + "*", parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await message.reply_text("❌ Format: 2 5 10", reply_markup=cancel_kb())
            return

        elif action == "kol_add_wallet":
            _clean(u.id)
            parts   = text.strip().split(None, 1)
            address = parts[0]
            label   = parts[1] if len(parts) > 1 else address[:8] + "..."
            if not _re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', address):
                await update.message.reply_text(
                    "⚠️ Invalid Solana address. Must be 32-44 base58 characters.\nTry again or tap Cancel.",
                    parse_mode="Markdown"
                )
                pending[u.id] = {"action": "kol_add_wallet"}
                return
            wallets = ud.setdefault("kol_wallets", [])
            if any(w.get("address") == address for w in wallets):
                await update.message.reply_text(
                    "⚠️ That wallet is already being tracked.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ KOL Menu", callback_data="kol_menu")]])
                )
                return
            wallets.append({"address": address, "label": label, "chain": "solana"})
            await update.message.reply_text(
                "✅ *" + label + "* added to KOL tracker!\n\n"
                "`" + address + "`\n\n"
                "You'll get alerted next time this wallet buys a new token.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👀 KOL Menu", callback_data="kol_menu")]])
            )
            return

        elif action == "copy_ext_wallet":
            # User typed a Solana wallet address to copy externally
            parts   = text.strip().split(None, 1)
            address = parts[0]
            label   = parts[1] if len(parts) > 1 else address[:8] + "..."
            if not _re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', address):
                await update.message.reply_text(
                    "⚠️ Invalid Solana address. Must be 32-44 base58 characters.\n"
                    "Format: `<address> <optional label>`\nTry again or tap Cancel.",
                    parse_mode="Markdown",
                    reply_markup=cancel_kb()
                )
                pending[u.id] = {"action": "copy_ext_wallet"}
                return
            # Store as external copy wallet — separate from kol_wallets
            ud["copy_ext_wallet"] = {"address": address, "label": label, "chain": "solana"}
            ud["copy_trading"]    = None   # clear any internal copy target
            ud["copy_paused"]     = False
            del pending[u.id]
            await update.message.reply_text(
                "✅ *External Copy Wallet Set!*\n\n"
                "🏷 Label: *" + _md(label) + "*\n"
                "`" + address + "`\n\n"
                "Whenever this wallet buys a token on Solana, "
                "the bot will automatically mirror the trade up to your set copy amount.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Copy Trading Menu", callback_data="v_copy")]])
            )
            return

        elif action == "copy_ext_amount":
            # User typed their desired copy amount
            try:
                val = float(text.strip().replace("$", "").replace(",", ""))
                assert 1 <= val <= 10000
                ud["copy_ext_amount"] = val
                del pending[u.id]
                await update.message.reply_text(
                    "✅ Copy amount set to *" + money(val) + "* per trade.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Copy Trading Menu", callback_data="v_copy")]])
                )
            except Exception:
                await update.message.reply_text(
                    "❌ Enter a valid amount (e.g. `50` or `100`).",
                    parse_mode="Markdown",
                    reply_markup=cancel_kb()
                )
            return

        elif action == "sniper_buys_h1":
            try:
                val = int(text.strip())
                assert 0 <= val <= 500
                ud.setdefault("sniper_filters", {})["min_buys_h1"] = val
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                reply_text = "✅ Min Buys/1h set to *" + str(val) + "*"
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text=reply_text, parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Filters", callback_data="sniper_filters_menu")]]))
                else:
                    await message.reply_text(reply_text, parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await update.message.reply_text("❌ Enter a whole number (e.g. 20)", reply_markup=cancel_kb())
            return

        elif action == "sniper_buy_pct":
            try:
                val = int(text.strip().replace("%",""))
                assert 0 <= val <= 100
                ud.setdefault("sniper_filters", {})["min_buy_pct"] = val
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                reply_text = "✅ Min Buy% set to *" + str(val) + "%*"
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text=reply_text, parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Filters", callback_data="sniper_filters_menu")]]))
                else:
                    await message.reply_text(reply_text, parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await update.message.reply_text("❌ Enter 40–80 (e.g. 55)", reply_markup=cancel_kb())
            return

        elif action == "sniper_vol_mc":
            try:
                val = float(text.strip().replace("x",""))
                assert 0.5 <= val <= 50
                ud.setdefault("sniper_filters", {})["max_vol_mc_ratio"] = val
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                reply_text = "✅ Vol/MC cap set to *" + str(val) + "x*"
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text=reply_text, parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Filters", callback_data="sniper_filters_menu")]]))
                else:
                    await message.reply_text(reply_text, parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await update.message.reply_text("❌ Enter a decimal (e.g. 6.0)", reply_markup=cancel_kb())
            return

        elif action == "sniper_budget":
            try:
                val = float(text.replace("$", "").replace(",", ""))
                assert val > 0
                ud["sniper_daily_budget"] = val
                prompt_id = p.get("_prompt_msg_id")
                pending.pop(u.id, None)
                await _clean()
                if prompt_id:
                    await ctx.bot.edit_message_text(chat_id=u.id, message_id=prompt_id,
                        text="✅ Daily sniper budget: *" + money(val) + "*", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="v_sniper")]]))
                else:
                    await message.reply_text("✅ Daily sniper budget: *" + money(val) + "*", parse_mode="Markdown", reply_markup=back_main())
            except Exception:
                await update.message.reply_text("❌ Enter a number like 300", reply_markup=cancel_kb())
            return

        # ── DCA STEP-BY-STEP INPUT ─────────────────────────────────────────────
        elif action == "dca_mc_input":
            contract = p["contract"]
            try:
                mc_val = float(text.replace("$","").replace(",","").replace("k","e3").replace("K","e3").replace("m","e6").replace("M","e6"))
                assert mc_val > 0
                pending[u.id]["pending_mc"] = mc_val
                pending[u.id]["action"]     = "dca_amt_input"
                await update.message.reply_text(
                    "💵 *SET BUY AMOUNT*\n\n"
                    "MC trigger: *" + mc_str(mc_val) + "*\n\n"
                    "How much USD to buy?",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("$50",   callback_data="dca_amt_quick_50_"  + contract),
                         InlineKeyboardButton("$100",  callback_data="dca_amt_quick_100_" + contract),
                         InlineKeyboardButton("$250",  callback_data="dca_amt_quick_250_" + contract)],
                        [InlineKeyboardButton("$500",  callback_data="dca_amt_quick_500_" + contract),
                         InlineKeyboardButton("Custom ↩", callback_data="dca_setamt_" + contract)],
                    ])
                )
            except Exception:
                await update.message.reply_text(
                    "❌ Enter a valid MC number\nExamples:  `500000`  `1000000`  `5000000`",
                    parse_mode="Markdown", reply_markup=cancel_kb()
                )
            return

        elif action == "dca_amt_input":
            contract = p["contract"]
            mc_val   = p.get("pending_mc", 0)
            try:
                amt = float(text.replace("$","").replace(",",""))
                assert amt > 0
                targets = p.get("targets", [])
                targets.append({"mc": mc_val, "amount": amt, "triggered": False})
                pending[u.id]["targets"] = targets
                pending[u.id].pop("pending_mc", None)
                pending[u.id]["action"] = "dca_build"

                # Build a fake query-like object to call helper
                class _FakeQ:
                    async def edit_message_text(self, *a, **kw):
                        await update.message.reply_text(*a, **kw)
                await _dca_show_plan(_FakeQ(), contract, pending[u.id])
            except Exception:
                await update.message.reply_text(
                    "❌ Enter a valid amount\nExamples:  `50`  `100`  `250`",
                    parse_mode="Markdown", reply_markup=cancel_kb()
                )
            return

        elif action == "apex_vault_withdraw_amt":
            vault = ud.get("apex_vault", 0.0)
            raw   = text.strip().upper().replace("$", "").replace(",", "")
            try:
                if raw == "MAX":
                    amt = vault
                else:
                    amt = float(raw)
                assert amt > 0, "must be positive"
                assert amt <= vault + 0.001, "exceeds vault balance"
                amt = min(amt, vault)
                ud["apex_vault"] -= amt
                ud["balance"]    += amt
                ud.pop("_apex_vault_low_notified", None)
                # Auto-disable APEX if vault is now too low to cover a trade
                _min_trade = float(ud.get("sniper_filters", {}).get("buy_amount", 50.0)) * 0.5
                _vault_after = ud.get("apex_vault", 0.0)
                _apex_was_on = ud.get("apex_mode", False)
                if _apex_was_on and _vault_after < _min_trade:
                    ud["apex_mode"] = False
                save_user(u.id, ud)
                pending.pop(u.id, None)
                await _clean()
                _disabled_note = (
                    "\n\n\U0001f534 *APEX auto-disabled* — vault too low to trade.\n"
                    "_Fund the vault and re-enable APEX to resume._"
                ) if _apex_was_on and _vault_after < _min_trade else ""
                await message.reply_text(
                    "\u2705 *VAULT WITHDRAWAL CONFIRMED*\n\n"
                    "Withdrawn: *" + money(amt) + "*\n"
                    "Trading Balance: *" + money(ud["balance"]) + "*\n"
                    "Vault Remaining: *" + money(ud["apex_vault"]) + "*"
                    + _disabled_note,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("\U0001f3e6 Vault", callback_data="apex_vault_menu"),
                        InlineKeyboardButton("\U0001f3e0 Menu",  callback_data="mm"),
                    ]])
                )
            except AssertionError:
                await _clean()
                await message.reply_text(
                    "\u274c Invalid amount. Vault balance: *" + money(vault) + "*\n"
                    "Enter a number up to " + money(vault) + " or type MAX.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u274c Cancel", callback_data="apex_vault_menu")]])
                )
                pending[u.id] = {"action": "apex_vault_withdraw_amt"}
            except Exception:
                await _clean()
                await message.reply_text(
                    "\u274c Invalid input. Enter a number (e.g. 50) or MAX.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u274c Cancel", callback_data="apex_vault_menu")]])
                )
                pending[u.id] = {"action": "apex_vault_withdraw_amt"}
            return

        elif action == "apex_auto_custom_amt":
            contract = p.get("contract", "")
            vault    = ud.get("apex_vault", 0.0)
            try:
                amt = float(text.strip().replace("$", "").replace(",", ""))
                assert 1 <= amt <= vault, "exceeds vault"
                pending.pop(u.id, None)
                await _clean()
                # Trigger the same flow as apex_auto_amt_
                result = await do_buy_core(ud, u.id, contract, amt, planned=True,
                                           mood="APEX", vault_buy=True)
                if isinstance(result, str):
                    await message.reply_text("❌ " + result, reply_markup=main_menu_kb())
                    return
                info_post2, _ = result
                h2 = ud["holdings"].get(contract, {})
                if h2:
                    import time as _aact
                    _rug2   = "MEDIUM"
                    _sl2    = 18.0
                    _ca2    = _sniper_analysis_cache.get(u.id, {}).get(contract, {})
                    if _ca2:
                        _rug2 = _ca2.get("ai", {}).get("rug_risk", "MEDIUM")
                        _sl2  = {"LOW": 22.0, "MEDIUM": 18.0, "HIGH": 12.0}.get(_rug2, 18.0)
                    h2["stop_loss_pct"]         = _sl2
                    h2["apex_peak_price"]        = info_post2["price"]
                    h2["apex_trail_stop"]        = None
                    h2["apex_trail_pct"]         = APEX_TRAIL_PCT_EARLY
                    h2["apex_threat"]            = "CLEAR"
                    h2["apex_vault_locked"]      = {}
                    h2["apex_profile_at_entry"]  = "default"
                    h2["apex_hunter_floor"]      = 0.0
                    h2["apex_entry_rug"]         = _rug2
                    h2["liq_at_buy"]             = info_post2.get("liq", 0)
                    h2["pair_addr"]              = info_post2.get("pair_addr", "")
                    h2["sr_history"]             = []
                    h2["apex_dca_count"]         = 0
                    h2["apex_last_dca_ts"]       = 0.0
                    h2["apex_ladder_sold_1_3x"]  = False
                    h2["apex_ladder_sold_2x"]    = False
                    save_user(u.id, ud)
                await message.reply_text(
                    "🤖 *AUTOMATE ACTIVE*\n\n"
                    "*$" + _md(info_post2["symbol"]) + "*\n"
                    "Bought: *" + money(amt) + "* from vault\n"
                    "SL: *" + str(_sl2) + "%*  |  Rug: *" + _rug2 + "*\n"
                    "🏦 Vault: *" + money(ud.get("apex_vault", 0)) + "*\n\n"
                    "_APEX is now managing this position._",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📊 View Position", callback_data="btt_" + contract),
                        InlineKeyboardButton("🏠 Menu",          callback_data="mm"),
                    ]])
                )
            except AssertionError:
                await message.reply_text(
                    "❌ Enter an amount between $1 and " + money(vault),
                    reply_markup=cancel_kb()
                )
                pending[u.id] = {"action": "apex_auto_custom_amt", "contract": contract}
            except Exception:
                await message.reply_text("❌ Invalid amount. Enter a number like 75.", reply_markup=cancel_kb())
                pending[u.id] = {"action": "apex_auto_custom_amt", "contract": contract}
            return

        elif action == "apex_vault_fund_amt":
            balance = ud.get("balance", 0.0)
            raw     = text.strip().upper().replace("$", "").replace(",", "")
            try:
                if raw == "ALL":
                    amt = balance
                else:
                    amt = float(raw)
                assert amt > 0, "must be positive"
                assert amt <= balance + 0.001, "exceeds balance"
                amt = min(amt, balance)
                ud["balance"]    = round(ud["balance"] - amt, 4)
                ud["apex_vault"] = round(ud.get("apex_vault", 0.0) + amt, 4)
                # Clear the low-vault flag so APEX can notify again if it runs low
                ud.pop("_apex_vault_low_notified", None)
                save_user(u.id, ud)
                pending.pop(u.id, None)
                await _clean()
                buy_est  = ud.get("sniper_filters", {}).get("buy_amount", 20.0)
                trades_l = int(ud["apex_vault"] / buy_est) if buy_est > 0 else 0
                await message.reply_text(
                    "\u2705 *VAULT FUNDED*\n\n"
                    "Transferred: *" + money(amt) + "* \u2192 Vault\n\n"
                    "\U0001f3e6 Vault: *" + money(ud["apex_vault"]) + "*\n"
                    "\U0001f4b5 Main Balance: *" + money(ud["balance"]) + "*\n"
                    "\U0001f3af Est. trades: *" + str(trades_l) + "* at " + money(buy_est) + "/trade\n\n"
                    "\u26a1 APEX will now auto-trade from the vault.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("\U0001f3e6 Vault", callback_data="apex_vault_menu"),
                        InlineKeyboardButton("\u26a1 APEX",  callback_data="apex_menu"),
                    ]])
                )
            except AssertionError:
                await _clean()
                await message.reply_text(
                    "\u274c Invalid amount. Main balance: *" + money(balance) + "*\n"
                    "Enter a number up to " + money(balance) + " or type ALL.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="apex_vault_menu")]])
                )
                pending[u.id] = {"action": "apex_vault_fund_amt"}
            except Exception:
                await _clean()
                await message.reply_text(
                    "❌ Invalid input. Enter a number (e.g. 100) or ALL.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="apex_vault_menu")]])
                )
                pending[u.id] = {"action": "apex_vault_fund_amt"}
            return

        elif action == "apex_confidence_input":
            try:
                val = int(text.strip())
                assert 1 <= val <= 10, "Must be 1–10"
                ud["apex_learn_threshold"] = val
                save_user(u.id, ud)
                del pending[u.id]
                await message.reply_text(
                    "✅ Min confidence set to *" + str(val) + "/10*",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Back to Settings", callback_data="apex_settings_menu")]])
                )
            except (AssertionError, ValueError) as _e:
                await message.reply_text("❌ Enter a number between 1 and 10", reply_markup=cancel_kb())
            return

        elif action == "apex_score_input":
            try:
                val = int(text.strip())
                assert 20 <= val <= 80, "Must be 20–80"
                ud["apex_learn_score_min"] = val
                save_user(u.id, ud)
                del pending[u.id]
                await message.reply_text(
                    "✅ Min score set to *" + str(val) + "/100*",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Back to Settings", callback_data="apex_settings_menu")]])
                )
            except (AssertionError, ValueError):
                await message.reply_text("❌ Enter a number between 20 and 80", reply_markup=cancel_kb())
            return


        elif action == "apex_trail_x_input":
            try:
                val = float(text.strip().replace("x", "").replace("X", ""))
                assert 1.2 <= val <= 4.0, "Must be between 1.2 and 4.0"
                val = round(val, 1)
                ud["apex_trail_activate_x_learned"] = val
                save_user(u.id, ud)
                del pending[u.id]
                await message.reply_text(
                    "✅ *Trail activation set to " + str(val) + "x*\n\n"
                    "APEX will activate trailing stop once position reaches *" + str(val) + "x*.\n\n"
                    "_The self-learning engine may suggest changes after daily trades_\n"
                    "_but will NOT apply them automatically._",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Back to Settings", callback_data="apex_settings_menu")]])
                )
                return
            except AssertionError as _te:
                await message.reply_text(
                    "❌ " + str(_te) + "\nEnter a number e.g. *1.5* or *2.0*",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="apex_settings_menu")]])
                )
                pending[u.id] = {"action": "apex_trail_x_input"}
                return
            except Exception:
                await message.reply_text(
                    "❌ Invalid. Enter a number between 1.2 and 4.0",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="apex_settings_menu")]])
                )
                pending[u.id] = {"action": "apex_trail_x_input"}
                return

        elif action == "apex_sl_input":
            try:
                parts = text.strip().split()
                assert len(parts) == 3, "Enter exactly 3 numbers"
                sl_low, sl_med, sl_high = float(parts[0]), float(parts[1]), float(parts[2])
                assert 10.0 <= sl_low  <= 35.0, "LOW must be 10–35"
                assert  8.0 <= sl_med  <= 28.0, "MEDIUM must be 8–28"
                assert  6.0 <= sl_high <= 20.0, "HIGH must be 6–20"
                assert sl_low >= sl_med >= sl_high, "LOW must be ≥ MEDIUM ≥ HIGH"
                ud["apex_sl_learned_low"]  = round(sl_low, 1)
                ud["apex_sl_learned_med"]  = round(sl_med, 1)
                ud["apex_sl_learned_high"] = round(sl_high, 1)
                save_user(u.id, ud)
                del pending[u.id]
                await message.reply_text(
                    "✅ *Stop Loss levels updated*\n\n"
                    "LOW risk:    *" + str(round(sl_low, 1)) + "%*\n"
                    "MEDIUM risk: *" + str(round(sl_med, 1)) + "%*\n"
                    "HIGH risk:   *" + str(round(sl_high, 1)) + "%*\n\n"
                    "_These apply to new positions. Self-learning may suggest_\n"
                    "_adjustments after daily trades but will NOT auto-apply._",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Back to Settings", callback_data="apex_settings_menu")]])
                )
                return
            except AssertionError as _sle:
                await message.reply_text(
                    "❌ " + str(_sle) + "\n\nFormat: *LOW MEDIUM HIGH* e.g. *22 18 12*\n"
                    "LOW 10–35 | MEDIUM 8–28 | HIGH 6–20",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="apex_settings_menu")]])
                )
                pending[u.id] = {"action": "apex_sl_input"}
                return
            except Exception:
                await message.reply_text(
                    "❌ Invalid. Enter three numbers e.g. *22 18 12*",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="apex_settings_menu")]])
                )
                pending[u.id] = {"action": "apex_sl_input"}
                return

    # No pending (or pending was cleared for CA) — treat as CA
    if ud.get("balance") is None:
        await update.message.reply_text("Use /start to set up your account first!")
        return

    # ── Extract valid CA from text ────────────────────────────────────────────
    # Handles truncated CAs (ends in ...) and CAs embedded in longer messages.
    # Telegram truncates quoted/forwarded message previews with "..." which
    # breaks direct lookup. Use regex to extract the longest valid-looking CA.
    _raw = text.strip()
    _sol_re = _re.search(r'\b([1-9A-HJ-NP-Za-km-z]{32,44})\b', _raw)
    _evm_re = _re.search(r'\b(0x[0-9a-fA-F]{40})\b', _raw)
    if _evm_re:
        contract = _evm_re.group(1)
    elif _sol_re:
        contract = _sol_re.group(1)
    else:
        contract = _raw.rstrip(".").strip()

    # ── GROUP MODE: public card + deep-link buttons, no user data exposed ─────
    if _is_group(update):
        # Silently ignore group messages that don't look like a valid CA
        if len(contract) < 32:
            return
        msg = await update.message.reply_text("🔍 Scanning token…")
        info = await get_token(contract)
        try:
            await msg.delete()
        except Exception:
            pass
        if not info:
            # Don't flood group chat with error messages for non-CA messages
            return
        # Register first scanner for group scans too
        import time as _gfst_reg
        if contract not in _first_scanner:
            _first_scanner[contract] = {
                "uid":        u.id,
                "username":   ud.get("username", u.username or "anon"),
                "price":      info.get("price", 0),
                "mc":         info.get("mc", 0),
                "scanned_at": _gfst_reg.time(),
            }

        card_txt = group_token_card(info, contract)
        _g_header = info.get("header_image", "")
        _g_use    = bool(_g_header and (
            info.get("_header_confirmed")
            or not _g_header.startswith("https://dd.dexscreener.com/ds-data/")
        ))
        _g_txt = ("[\u200b](" + _g_header + ")\n" if _g_use else "") + card_txt
        await update.message.reply_text(
            _g_txt,
            parse_mode="Markdown",
            reply_markup=group_buy_kb(contract),
            disable_web_page_preview=not _g_use,
        )
        return

    msg = await update.message.reply_text("🔍 Scanning…")
    # Fire token fetch immediately — if already cached this returns in <1ms
    info = await get_token(contract)
    if not info:
        await msg.edit_text("❌ Token not found. Check the contract address and try again.", reply_markup=back_main())
        return
    sc = score_token(info)
    ud["last_chain"] = info.get("chain", "solana")

    # ── Register first scanner ────────────────────────────────────────────────
    import time as _fst_reg
    if contract not in _first_scanner:
        _first_scanner[contract] = {
            "uid":        u.id,
            "username":   ud.get("username", u.username or "anon"),
            "price":      info.get("price", 0),
            "mc":         info.get("mc", 0),
            "scanned_at": _fst_reg.time(),
        }

    # Delete scanning stub then send card
    try:
        await msg.delete()
    except Exception:
        pass
    try:
        await send_token_card(update.message, info, contract, ud, sc, ctx, is_query=False)
    except Exception as card_err:
        logger.error(f"Token card error: {card_err}")
        await update.message.reply_text(f"❌ Error loading token: {card_err}", reply_markup=back_main())


async def export_csv(bot, uid: int, ud: dict):
    """Generate and send trade history as a CSV file."""
    import csv, io as _io
    logs = trade_log.get(uid, [])
    if not logs:
        await bot.send_message(chat_id=uid, text="No trade history to export yet.", reply_markup=back_main())
        return
    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Symbol", "Contract", "Chain", "Invested", "Returned",
                     "PnL", "X", "Hold(h)", "Reason", "Mood", "Journal", "Planned"])
    for tr in sorted(logs, key=lambda x: _safe_dt(x.get("closed_at"))):
        writer.writerow([
            tr.get("closed_at", "").strftime("%Y-%m-%d %H:%M") if hasattr(tr.get("closed_at"), "strftime") else "",
            tr.get("symbol", ""),
            tr.get("contract", ""),
            tr.get("chain", ""),
            round(tr.get("invested", 0), 4),
            round(tr.get("returned", 0), 4),
            round(tr.get("realized_pnl", 0), 4),
            round(tr.get("x", 0), 4),
            tr.get("hold_h", ""),
            tr.get("reason", ""),
            tr.get("mood", ""),
            tr.get("journal", ""),
            tr.get("planned", ""),
        ])
    buf.seek(0)
    filename = "apex_sniper_trades_" + datetime.now().strftime("%Y%m%d") + ".csv"
    await bot.send_document(
        chat_id=uid,
        document=_io.BytesIO(buf.getvalue().encode("utf-8")),
        filename=filename,
        caption="📁 *Your full trade history*\n" + str(len(logs)) + " trades exported.",
        parse_mode="Markdown",
    )


async def btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = update.effective_user
    if not u:
        return  # Ignore channel posts
    ud = get_user(u.id, u.username or u.first_name)
    cb = q.data

    # ── ACCESS CONTROL APPROVE / DENY (admin only) ────────────────────────────
    if cb.startswith("access_approve_") or cb.startswith("access_deny_"):
        if not is_admin(u.id):
            return
        target_uid = int(cb.split("_")[-1])
        action     = "approve" if cb.startswith("access_approve_") else "deny"
        info       = _pending_access.pop(target_uid, {})
        name       = info.get("name", str(target_uid))

        if action == "approve":
            # Mark approved in their user data
            target_ud = users.get(target_uid)
            if target_ud is None:
                target_ud = get_user(target_uid, name)
            target_ud["access_approved"] = True
            save_user(target_uid, target_ud)  # persist to Supabase immediately
            # Notify the user
            try:
                await ctx.bot.send_message(
                    chat_id=target_uid,
                    parse_mode="Markdown",
                    text=(
                        "✅ *Access Granted!*\n\n"
                        "Welcome to *APEX SNIPER BOT*.\n"
                        "Type /start to begin."
                    )
                )
            except Exception:
                pass
            await q.edit_message_text(
                "✅ Access approved for *" + name + "* (`" + str(target_uid) + "`)",
                parse_mode="Markdown"
            )
        else:
            # Notify the user they were denied
            try:
                await ctx.bot.send_message(
                    chat_id=target_uid,
                    text="❌ Your access request was not approved.\n\nContact @" + ACCESS_ADMIN_USERNAME + " for more info."
                )
            except Exception:
                pass
            await q.edit_message_text(
                "❌ Access denied for *" + name + "* (`" + str(target_uid) + "`)",
                parse_mode="Markdown"
            )
        return
    # ── END ACCESS CALLBACKS ──────────────────────────────────────────────────

    # ── GROUP MODE: Rescan button (grp_rf_) — safe, only public token data ─────
    if cb.startswith("grp_rf_"):
        contract = cb[7:]
        info = await get_token(contract, force=True)
        if not info:
            await q.answer("Token unavailable.", show_alert=True)
            return
        card_txt = group_token_card(info, contract)
        try:
            await q.edit_message_text(
                card_txt,
                parse_mode="Markdown",
                reply_markup=group_buy_kb(contract),
            )
        except Exception:
            pass
        return

    if cb == "mm":
        pending.pop(u.id, None)
        # ── Delete any floating chart image from the last token card view ─────
        _prev_chart = chart_msg_ids.pop(u.id, None)
        if _prev_chart:
            try:
                await ctx.bot.delete_message(chat_id=q.message.chat_id, message_id=_prev_chart)
            except Exception:
                pass
        if ud.get("balance") is None:
            await cmd_start(update, ctx)
            return
        _mm_text = (
            "⚡ *APEX SNIPER BOT*\n\nWelcome back, *" + ud["username"] + "*!\n"
            "💰 Balance: *" + money(ud["balance"]) + "*\n"
            "💎 Savings: *" + money(ud["savings"]) + "*\n"
            "🏦 Vault: *" + money(ud.get("apex_vault", 0.0)) + "*\n\n"
            "Paste any CA to trade 👇"
        )
        # ── Photo messages (token card with banner) — delete and send fresh text ─
        # Main menu is always text, can't be a caption on the token banner.
        if q.message.photo or q.message.document:
            try:
                await q.message.delete()
            except Exception:
                pass
            await ctx.bot.send_message(
                chat_id=q.message.chat_id,
                text=_mm_text,
                parse_mode="Markdown",
                reply_markup=main_menu_kb()
            )
        else:
            await q.edit_message_text(
                _mm_text,
                parse_mode="Markdown",
                reply_markup=main_menu_kb()
            )

    elif cb == "v_trade":
        await q.edit_message_text(
            "⚡ *BUY and SELL NOW*\n\nPaste any Solana, ETH, BSC or Base contract address in the chat to get started.",
            parse_mode="Markdown", reply_markup=back_main()
        )

    elif cb == "v_pos":
        # ── Delete any floating chart image from the last token card view ─────
        _prev_chart = chart_msg_ids.pop(u.id, None)
        if _prev_chart:
            try:
                await ctx.bot.delete_message(chat_id=q.message.chat_id, message_id=_prev_chart)
            except Exception:
                pass
        if not ud["holdings"]:
            await q.edit_message_text(
                "📊 *POSITIONS*\n\nNo open positions.\nPaste a CA to start trading.",
                parse_mode="Markdown", reply_markup=back_main()
            )
            return
        lines = ["📊 *OPEN POSITIONS*\n"]
        _pos_contracts = list(ud["holdings"].keys())
        _pos_infos = await _asyncio.gather(*[get_token(c) for c in _pos_contracts])
        for contract, info in zip(_pos_contracts, _pos_infos):
            h = ud["holdings"][contract]
            if info:
                cv    = h["amount"] * info["price"]
                cx    = info["price"] / h["avg_price"] if h["avg_price"] > 0 else 0
                ppnl  = cv - h["total_invested"]
                sl    = h.get("stop_loss_pct")
                targets = [t for t in h.get("auto_sells", []) if not t.get("triggered")]
                # ── Hold time
                held_h   = (datetime.now() - h.get("bought_at", datetime.now())).total_seconds() / 3600
                held_txt = "  ⏱" + age_str(held_h)
                # ── Stop loss / auto-sell indicators
                sl_txt = "  🛑" + str(sl) + "%" if sl else ""
                as_txt = "  🎯" + str(len(targets)) + " TP" if targets else ""
                # ── APEX threat badge (only shown when not CLEAR)
                threat     = h.get("apex_threat", "")
                threat_txt = ""
                if threat == "RED":    threat_txt = "  🔴"
                elif threat == "ORANGE": threat_txt = "  🟠"
                elif threat == "YELLOW": threat_txt = "  🟡"
                # ── History line: sparkline + peak x + low x
                hist_line = _position_history_line(h, info["price"])
                # ── Mood badge for APEX/Sniper positions
                mood = h.get("mood", "")
                mood_txt = "  ⚡APEX" if mood in ("APEX", "APEX-DCA") else ("  🎯Sniper" if mood == "AI-Sniper" else "")
                # ── Build the position block
                lines.append(
                    "*$" + _md(h["symbol"]) + "*" + mood_txt + "  " + str(round(cx, 2)) + "x" + threat_txt + "\n"
                    "  " + money(cv) + "  " + pstr(ppnl) + held_txt + sl_txt + as_txt + "\n"
                    + (hist_line + "\n" if hist_line else "")
                )
        buttons = []
        for contract, h in ud["holdings"].items():
            buttons.append([InlineKeyboardButton("Open $" + h["symbol"], callback_data="btt_" + contract)])
        buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="mm")])
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif cb == "v_orders":
        orders = ud.get("limit_orders", [])
        alerts = ud.get("price_alerts", [])
        if not orders and not alerts:
            await q.edit_message_text(
                "🕐 *ACTIVE ORDERS & ALERTS*\n\nNone active.\nOpen a token to set limit orders or price alerts.",
                parse_mode="Markdown", reply_markup=back_main())
            return

        lines = ["🕐 *ACTIVE ORDERS & ALERTS*\n"]
        buttons = []

        if orders:
            lines.append("📋 *Limit Orders:*")
            for i, o in enumerate(orders):
                otype = "BUY" if o["type"] == "buy" else "SELL"
                lines.append("  " + otype + " $" + o["symbol"] + " @ " + money(o["target_price"]) + "  (" + money(o["amount"]) + ")")
                buttons.append([InlineKeyboardButton(
                    "🗑 Cancel " + otype + " $" + o["symbol"],
                    callback_data="co_" + str(i)
                )])

        if alerts:
            lines.append("\n🔔 *Price Alerts:*")
            for i, a in enumerate(alerts):
                lines.append("  $" + a["symbol"] + " → " + a["direction"] + " " + money(a["target"]))
                buttons.append([InlineKeyboardButton(
                    "🗑 Cancel Alert $" + a["symbol"],
                    callback_data="al_del_" + str(i)
                )])

        buttons.append([InlineKeyboardButton("🗑 Cancel ALL", callback_data="co_all")])
        buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="mm")])
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif cb.startswith("al_del_"):
        # Cancel individual price alert
        try:
            idx = int(cb[7:])
            alerts = ud.get("price_alerts", [])
            if 0 <= idx < len(alerts):
                sym = alerts[idx].get("symbol", "?")
                alerts.pop(idx)
                ud["price_alerts"] = alerts
                # Refresh orders view
                orders = ud.get("limit_orders", [])
                if not orders and not alerts:
                    await q.edit_message_text("🗑 Alert for *$" + sym + "* cancelled.\n\nNo more active orders.", parse_mode="Markdown", reply_markup=back_main())
                else:
                    await q.edit_message_text("🗑 Alert for *$" + sym + "* cancelled.", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back to Orders", callback_data="v_orders")]]))
            else:
                await q.edit_message_text("Alert not found.", reply_markup=back_main())
        except Exception:
            await q.edit_message_text("Could not cancel alert.", reply_markup=back_main())

    elif cb.startswith("co_"):
        rest = cb[3:]
        if rest == "all":
            ud["limit_orders"] = []
            ud["price_alerts"] = []
            await q.edit_message_text("🗑 All orders and alerts cancelled.", reply_markup=back_main())
        else:
            try:
                idx = int(rest)
                if 0 <= idx < len(ud["limit_orders"]):
                    cancelled_sym = ud["limit_orders"][idx].get("symbol", "?")
                    ud["limit_orders"].pop(idx)
                    orders = ud.get("limit_orders", [])
                    alerts = ud.get("price_alerts", [])
                    if not orders and not alerts:
                        await q.edit_message_text("🗑 Order for *$" + cancelled_sym + "* cancelled.\n\nNo more active orders.", parse_mode="Markdown", reply_markup=back_main())
                    else:
                        await q.edit_message_text("🗑 Order for *$" + cancelled_sym + "* cancelled.", parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back to Orders", callback_data="v_orders")]]))
                else:
                    await q.edit_message_text("Order not found.", reply_markup=back_main())
            except Exception:
                await q.edit_message_text("Could not cancel order.", reply_markup=back_main())

    elif cb.startswith("ot_yes_"):
        rest = cb[7:]
        parts = rest.split("_", 2)
        contract = parts[0]
        amount = float(parts[1])
        mood = parts[2] if len(parts) > 2 else ""
        await do_buy_query(q, ud, u.id, contract, amount, mood=mood)

    elif cb == "cfg_mood":
        ud["mood_tracking"] = not ud.get("mood_tracking", True)
        status = "ON" if ud["mood_tracking"] else "OFF"
        await q.edit_message_text(
            "🧠 Mood tracking turned *" + status + "*",
            parse_mode="Markdown", reply_markup=settings_kb(ud)
        )

    elif cb == "v_history":
        logs = trade_log.get(u.id, [])
        if not logs:
            await q.edit_message_text("📜 *TRADE HISTORY*\n\nNo closed trades yet.", parse_mode="Markdown", reply_markup=back_more())
            return
        try:
            recent = sorted(logs, key=lambda x: _safe_dt(x.get("closed_at")), reverse=True)[:10]
            lines = ["📜 TRADE HISTORY\n"]
            for t in recent:
                pnl        = t.get("realized_pnl") or 0
                x_val      = t.get("x") or 0
                hold_h     = t.get("hold_h") or 0
                symbol     = t.get("symbol", "?")
                icon       = "🟢" if pnl > 0 else "🔴"
                reason_short = (t.get("reason") or "manual").replace("apex_trail_exit", "trail exit").replace("apex_threat_red", "threat red").replace("apex_momentum_decay", "momentum dec").replace("apex_threat_orange", "threat orange").replace("stop_loss", "stop loss").replace("_", " ")
                j = "\n  \"" + str(t.get("journal", ""))[:40] + "\"" if t.get("journal") else ""
                lines.append(
                    icon + " $" + symbol + "  " + str(round(x_val, 2)) + "x  " + pstr(pnl) + "\n"
                    "  Held: " + str(hold_h) + "h  |  " + reason_short + j
                )
            _btn_rows = []
            _row = []
            seen_contracts = set()
            for t in recent:
                contract_t = t.get("contract", "")
                if not contract_t or contract_t in seen_contracts:
                    continue
                seen_contracts.add(contract_t)
                pnl_icon = "✅" if (t.get("realized_pnl") or 0) > 0 else "❌"
                _row.append(InlineKeyboardButton(
                    pnl_icon + " $" + t.get("symbol", "?"),
                    callback_data="btt_" + contract_t
                ))
                if len(_row) == 2:
                    _btn_rows.append(_row)
                    _row = []
            if _row:
                _btn_rows.append(_row)
            _btn_rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="mm"),
                              InlineKeyboardButton("↩ Back",       callback_data="v_more")])
            await q.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(_btn_rows)
            )
        except Exception as _ve:
            logger.warning(f"v_history error: {_ve}")
            await q.edit_message_text("📜 *TRADE HISTORY*\n\nCould not load history. Please try again.", parse_mode="Markdown", reply_markup=back_more())

    elif cb == "v_wallet":
        bal    = ud.get("balance", 0)
        sav    = ud.get("savings", 0)
        vault  = ud.get("apex_vault", 0.0)
        asp    = str(ud["auto_save_pct"]) + "% of profits" if ud.get("auto_save_pct") else "not set"
        total  = bal + sav + vault
        # In-position reserved (vault milestones not yet closed)
        reserved = sum(
            h.get("apex_vault_reserved", 0)
            for h in ud.get("holdings", {}).values()
        )
        await q.edit_message_text(
            "👛 *WALLET*\n\n"
            "💵 Trading Balance:  *" + money(bal)   + "*\n"
            "💰 Savings:          *" + money(sav)   + "*\n"
            "🏦 APEX Vault:       *" + money(vault) + "*\n"
            + ("🔒 Pending vault:    *" + money(reserved) + "*\n" if reserved > 0 else "")
            + "─────────────────────────\n"
            "📊 Total:            *" + money(total) + "*\n\n"
            "Auto-save: *" + asp + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Savings",          callback_data="v_savings"),
                 InlineKeyboardButton("🏦 APEX Vault",       callback_data="apex_vault_menu")],
                [InlineKeyboardButton("🏠 Main Menu",        callback_data="mm")],
            ])
        )

    elif cb == "v_savings":
        asp = str(ud["auto_save_pct"]) + "% of profits" if ud.get("auto_save_pct") else "not set"
        await q.edit_message_text(
            "💰 *SAVINGS WALLET*\n\n"
            "Savings: *" + money(ud["savings"]) + "*\n"
            "Trading: *" + money(ud["balance"]) + "*\n"
            "Auto-save: *" + asp + "*\n\n"
            "Savings are protected from trading.\nTransfer manually when needed.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Deposit to Savings",      callback_data="sav_dep")],
                [InlineKeyboardButton("Withdraw to Trading",     callback_data="sav_wit")],
                [InlineKeyboardButton("Set Auto-Save %",         callback_data="cfg_autosave")],
                [InlineKeyboardButton("🏠 Main Menu",            callback_data="mm")],
            ])
        )

    elif cb == "sav_dep":
        pending[u.id] = {"action": "sav_deposit"}
        await q.edit_message_text(
            "Enter amount to move to savings:\nMax: " + money(ud["balance"]),
            reply_markup=cancel_kb()
        )

    elif cb == "sav_wit":
        if ud["savings"] <= 0:
            await q.edit_message_text("No savings to withdraw.", reply_markup=back_main())
            return
        pending[u.id] = {"action": "sav_withdraw"}
        await q.edit_message_text(
            "Enter amount to move to trading:\nMax: " + money(ud["savings"]),
            reply_markup=cancel_kb()
        )

    elif cb == "v_stats":
        logs = trade_log.get(u.id, [])
        if not logs:
            await q.edit_message_text("📈 *STATS*\n\nNo closed trades yet.", parse_mode="Markdown", reply_markup=back_more())
            return
        wins   = [t for t in logs if (t.get("realized_pnl") or 0) > 0]
        losses = [t for t in logs if (t.get("realized_pnl") or 0) <= 0]
        total  = len(logs)
        wr     = round(len(wins) / total * 100, 1)
        aw     = sum(t.get("realized_pnl") or 0 for t in wins) / len(wins) if wins else 0
        al     = sum(t.get("realized_pnl") or 0 for t in losses) / len(losses) if losses else 0
        ah     = sum(t.get("hold_h") or 0 for t in logs) / total
        tpnl   = sum(t.get("realized_pnl") or 0 for t in logs)
        best   = max(logs, key=lambda t: t.get("realized_pnl") or 0)
        worst  = min(logs, key=lambda t: t.get("realized_pnl") or 0)
        bestx  = max(logs, key=lambda t: t.get("x") or 0)
        rf, rb = ud.get("followed", 0), ud.get("broken", 0)
        dr     = round(rf / (rf + rb) * 100) if (rf + rb) > 0 else 0
        sb = ud.get("starting_balance", 5000)
        eq = ud["balance"] + sum(h["total_invested"] for h in ud["holdings"].values()) + ud["savings"]
        growth = round((eq - sb) / sb * 100, 1) if sb > 0 else 0

        best_hour = ""
        if ud.get("trade_hours"):
            bh = max(ud["trade_hours"].items(), key=lambda x: x[1].get("pnl", 0))
            best_hour = "\nBest Hour: " + str(bh[0]) + ":00  (" + pstr(bh[1]["pnl"]) + ")"

        mood_txt = ""
        if ud.get("mood_stats"):
            mood_txt = "\n\n🧠 *MOOD BREAKDOWN*\n"
            for mood, ms in sorted(ud["mood_stats"].items(), key=lambda x: x[1]["pnl"], reverse=True):
                wr_m = round(ms["wins"] / ms["trades"] * 100) if ms["trades"] > 0 else 0
                mood_txt += mood + ": " + str(ms["trades"]) + " trades  WR:" + str(wr_m) + "%  " + pstr(ms["pnl"]) + "\n"

        ot_txt = ""
        if ud.get("avg_daily_trades", 0) > 0:
            ot_txt = "\nAvg Daily Trades: " + str(ud["avg_daily_trades"])

        target_line = ""
        if ud.get("target_equity"):
            pct_done = round(min((eq / ud["target_equity"]) * 100, 100), 1)
            target_line = "\nTarget Progress: " + str(pct_done) + "% of " + money(ud["target_equity"])

        await q.edit_message_text(
            "📈 *STATS*\n\n"
            "Trades: " + str(total) + "  (" + str(len(wins)) + "W / " + str(len(losses)) + "L)\n"
            "Win Rate: " + str(wr) + "%\n"
            "Avg Win: " + money(aw) + "\n"
            "Avg Loss: " + money(al) + "\n"
            "Total PnL: " + pstr(tpnl) + "\n\n"
            "Best: " + pstr(best.get("realized_pnl") or 0) + " ($" + _md(best.get("symbol","?")) + ")\n"
            "Worst: " + pstr(worst.get("realized_pnl") or 0) + " ($" + _md(worst.get("symbol","?")) + ")\n"
            "Best X: " + str(round(bestx.get("x") or 0, 2)) + "x ($" + _md(bestx.get("symbol","?")) + ")\n\n"
            "Avg Hold: " + str(round(ah, 1)) + "h\n"
            "Rules Followed: " + str(rf) + "  |  Broken: " + str(rb) + "\n"
            "Discipline: " + str(dr) + "%\n"
            "Best Streak: " + str(ud.get("best_streak", 0)) + "\n"
            "Current Streak: " + str(ud.get("streak", 0)) + "\n"
            "Max Drawdown: " + str(round(ud.get("max_drawdown", 0), 1)) + "%\n"
            "Account Growth: " + str(growth) + "%" + best_hour + target_line + ot_txt + mood_txt,
            parse_mode="Markdown", reply_markup=back_more()
        )

    elif cb == "v_review":
        logs = trade_log.get(u.id, [])
        week_ago = datetime.now() - timedelta(days=7)
        weekly = [t for t in logs if _safe_dt(t.get("closed_at")) >= week_ago]
        if not weekly:
            await q.edit_message_text("📅 No closed trades in last 7 days.", reply_markup=back_main())
            return
        wins = [t for t in weekly if t["realized_pnl"] > 0]
        tpnl = sum(t["realized_pnl"] for t in weekly)
        wr = round(len(wins) / len(weekly) * 100)
        lines = ["📅 *WEEKLY REVIEW*\n\n" + str(len(weekly)) + " trades  |  WR: " + str(wr) + "%  |  " + pstr(tpnl) + "\n"]
        for t in sorted(weekly, key=lambda x: _safe_dt(x.get("closed_at")), reverse=True):
            fp = " [followed plan]" if t.get("followed_plan") else (" [sold early]" if t.get("followed_plan") is False else "")
            j = "\n  \"" + t["journal"][:40] + "\"" if t.get("journal") else ""
            mood = "  Mood: " + t["mood"] if t.get("mood") else ""
            lines.append("$" + _md(t["symbol"]) + "  " + pstr(t["realized_pnl"]) + "  " + str(round(t.get("x", 0), 2)) + "x  " + str(t["hold_h"]) + "h" + fp + mood + j)
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=back_main())

    elif cb == "v_leader":
        if not users:
            await q.edit_message_text("No traders yet.", reply_markup=back_more())
            return
        scores = []
        for uid2, d in users.items():
            if d.get("balance") is None:
                continue
            hv = sum(h["total_invested"] for h in d["holdings"].values())
            eq = d["balance"] + hv + d["savings"]
            logs2 = trade_log.get(uid2, [])
            wr2 = round(len([t for t in logs2 if t["realized_pnl"] > 0]) / len(logs2) * 100) if logs2 else 0
            sb = d.get("starting_balance", 5000)
            growth = round((eq - sb) / sb * 100, 1) if sb > 0 else 0
            scores.append((d["username"], eq, eq - sb, wr2, growth))
        scores.sort(key=lambda x: x[1], reverse=True)
        places = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th"]
        lines = ["🏆 *LEADERBOARD*\n"]
        for i, (name, eq, ppnl, wr2, growth) in enumerate(scores[:10]):
            lines.append(places[i] + "  *" + name + "*\n     " + money(eq) + "  " + pstr(ppnl) + "  WR:" + str(wr2) + "%  Growth:" + str(growth) + "%")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=back_more())

    elif cb == "v_alerts":
        alerts = ud.get("price_alerts", [])
        if not alerts:
            await q.edit_message_text("🔔 *PRICE ALERTS*\n\nNo active alerts.\nOpen a token and set a price alert.", parse_mode="Markdown", reply_markup=back_more())
            return
        lines = ["🔔 *PRICE ALERTS*\n"]
        for a in alerts:
            lines.append("$" + _md(a["symbol"]) + " when price goes " + a["direction"] + " " + money(a["target"]))
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Clear All Alerts", callback_data="clear_alerts")],
                [InlineKeyboardButton("◀ Back to More",  callback_data="v_more")],
                [InlineKeyboardButton("🏠 Main Menu",    callback_data="mm")],
            ])
        )

    elif cb == "clear_alerts":
        ud["price_alerts"] = []
        await q.edit_message_text("All price alerts cleared.", reply_markup=back_more())

    elif cb == "v_settings":
        await q.edit_message_text("⚙️ *SETTINGS*\n\nTap any setting to change:", parse_mode="Markdown", reply_markup=settings_kb(ud))

    elif cb in ("cfg_buy", "cfg_sell", "cfg_risk", "cfg_maxpos", "cfg_daily", "cfg_autosave", "cfg_target"):
        prompts = {
            "cfg_buy":      "Enter default buy amount in USD (e.g. 100):",
            "cfg_sell":     "Enter default sell - 50% or fixed like 200:",
            "cfg_risk":     "Enter max risk per trade as % (e.g. 10):",
            "cfg_maxpos":   "Enter max open positions (e.g. 5):",
            "cfg_daily":    "Enter max trades per day (e.g. 10):",
            "cfg_autosave": "Enter auto-save % of profits (e.g. 20):",
            "cfg_target":   "Enter target equity goal (e.g. 10000):",
        }
        pending[u.id] = {"action": cb, "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(prompts[cb], reply_markup=cancel_kb())

    elif cb == "v_profile":
        sb = ud.get("starting_balance", 0)
        hv = sum(h["total_invested"] for h in ud["holdings"].values())
        eq = ud["balance"] + hv + ud["savings"]
        growth = round((eq - sb) / sb * 100, 1) if sb > 0 else 0
        logs2 = trade_log.get(u.id, [])
        wr2 = round(len([t for t in logs2 if t["realized_pnl"] > 0]) / len(logs2) * 100) if logs2 else 0
        joined = ud.get("joined_at", datetime.now()).strftime("%b %d %Y")
        _followed = ud.get("followed", 0)
        _broken   = ud.get("broken", 0)
        _disc_total = _followed + _broken
        _disc_rate  = str(round(_followed / _disc_total * 100)) + "%" if _disc_total > 0 else "N/A"
        await q.edit_message_text(
            "👤 *PROFILE*\n\n"
            "Name: *" + ud["username"] + "*\n"
            "Joined: " + joined + "\n\n"
            "Starting Balance: " + money(sb) + "\n"
            "Current Equity: " + money(eq) + "\n"
            "Account Growth: " + str(growth) + "%\n\n"
            "Total Trades: " + str(len(logs2)) + "\n"
            "Win Rate: " + str(wr2) + "%\n"
            "Best Streak: " + str(ud.get("best_streak", 0)) + "\n"
            "Discipline Rate: " + _disc_rate,
            parse_mode="Markdown", reply_markup=back_more()
        )


    elif cb.startswith("share_"):
        contract = cb[6:]
        logs = trade_log.get(u.id, [])
        trade = next((t for t in reversed(logs) if t["contract"] == contract), None)
        if not trade:
            await q.edit_message_text("Trade not found.", reply_markup=back_main())
            return
        pnl_positive = trade["realized_pnl"] > 0
        invested   = trade.get("invested", 0)
        returned   = trade.get("returned", 0)
        pnl_pct_val = round((trade["realized_pnl"] / invested * 100), 2) if invested > 0 else 0
        card = generate_trade_card(
            symbol       = trade["symbol"],
            chain        = trade.get("chain", "SOL"),
            pnl_str      = money(abs(trade["realized_pnl"])),
            x_val        = str(round(trade.get("x", 0), 2)),
            held_h       = str(trade["hold_h"]) + "h",
            bought_str   = money(invested),
            position_str = money(returned),
            username     = ud.get("username", "trader"),
            pnl_pct      = str(abs(pnl_pct_val)) + "%",
            pnl_positive = pnl_positive,
            closed_at    = trade.get("closed_at"),
        )
        caption = (
            "APEX SNIPER BOT TRADE\n"
            "$" + _md(trade["symbol"]) + "  " + str(round(trade.get("x", 0), 2)) + "x\n"
            + ("+" if pnl_positive else "") + money(trade["realized_pnl"]) + "\n"
            "Held: " + str(trade["hold_h"]) + "h\n"
            "Paper Trading | APEX SNIPER BOT"
        )
        if card:
            await q.message.reply_photo(
                photo=card,
                caption=caption,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="mm")]])
            )
            # Auto post to channel if connected
            ch_id = ud.get("channel_id")
            if ch_id:
                try:
                    card.seek(0)
                    await ctx.bot.send_photo(chat_id=ch_id, photo=card, caption=caption)
                except Exception:
                    pass
            await q.answer()
        else:
            await q.edit_message_text(
                "📤 *SHARE THIS TRADE*\n\n" + caption,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="mm")]])
            )

    elif cb == "v_copy":
        ct      = ud.get("copy_trading")        # internal bot-user copy target (uid)
        ext_w   = ud.get("copy_ext_wallet")     # external wallet dict {address, label}
        paused  = ud.get("copy_paused", False)
        ext_amt = ud.get("copy_ext_amount", 50.0)

        # ── Status line ───────────────────────────────────────────────────────
        status_txt = ""
        if ext_w:
            status_txt = (
                "\n\n📡 *Copying wallet:* `" + ext_w["address"][:12] + "...`"
                "\n🏷 Label: *" + _md(ext_w["label"]) + "*"
                "\n💰 Copy amount: *" + money(ext_amt) + "* per trade"
            )
            if paused:
                status_txt += " *(PAUSED)*"
        elif ct:
            trader_name = users[ct]["username"] if ct in users else "Unknown"
            status_txt = "\n\nCurrently copying: *" + _md(trader_name) + "*"
            if paused:
                status_txt += " *(PAUSED)*"

        # ── Top bot traders ───────────────────────────────────────────────────
        scores = []
        for uid2, d in users.items():
            if uid2 == u.id or d.get("balance") is None:
                continue
            logs2 = trade_log.get(uid2, [])
            if len(logs2) < 3:
                continue
            wins2 = [t for t in logs2 if t["realized_pnl"] > 0]
            wr2   = round(len(wins2) / len(logs2) * 100) if logs2 else 0
            tpnl2 = sum(t["realized_pnl"] for t in logs2)
            hv2   = sum(h["total_invested"] for h in d["holdings"].values())
            eq2   = d["balance"] + hv2
            scores.append((uid2, d["username"], wr2, tpnl2, eq2, len(logs2)))
        scores.sort(key=lambda x: x[4], reverse=True)

        buttons = []
        # External wallet section
        if ext_w:
            pause_lbl = "▶️ Resume Copying" if paused else "⏸ Pause Copying"
            buttons.append([InlineKeyboardButton(pause_lbl, callback_data="copy_pause")])
            buttons.append([InlineKeyboardButton("✏️ Change Wallet",   callback_data="copy_ext_set")])
            buttons.append([InlineKeyboardButton("💰 Set Copy Amount", callback_data="copy_ext_setamt")])
            buttons.append([InlineKeyboardButton("🗑 Remove External Wallet", callback_data="copy_ext_remove")])
        else:
            buttons.append([InlineKeyboardButton("➕ Copy External Wallet", callback_data="copy_ext_set")])
            # Show top internal traders
            for uid2, uname2, wr2, tpnl2, eq2, ntrades in scores[:5]:
                lbl = _md(uname2) + "  WR:" + str(wr2) + "%  " + str(ntrades) + " trades"
                buttons.append([InlineKeyboardButton(lbl, callback_data="copy_sel_" + str(uid2))])
            if ct:
                pause_lbl = "▶️ Resume Copy Trading" if paused else "⏸ Pause Copy Trading"
                buttons.append([InlineKeyboardButton(pause_lbl, callback_data="copy_pause")])
                buttons.append([InlineKeyboardButton("🗑 Stop Copy Trading", callback_data="copy_stop")])

        buttons.append([InlineKeyboardButton("📽 Trade Replay",   callback_data="copy_replay"),
                        InlineKeyboardButton("⚙️ Settings",       callback_data="copy_settings")])
        buttons.append([InlineKeyboardButton("◀ Back to More", callback_data="v_more")])
        buttons.append([InlineKeyboardButton("🏠 Main Menu",    callback_data="mm")])

        await q.edit_message_text(
            "🔁 *COPY TRADING*\n\n"
            "Mirror trades from any trader.\n"
            "• *External Wallet* — paste any Solana wallet address to auto-copy their on-chain buys.\n"
            "• *Top Bot Traders* — copy top performers in this bot."
            + status_txt,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif cb.startswith("copy_sel_"):
        target_id = int(cb[9:])
        if target_id not in users:
            await q.edit_message_text("Trader not found.", reply_markup=back_main())
            return
        target = users[target_id]
        logs2 = trade_log.get(target_id, [])
        wins2 = [t for t in logs2 if t["realized_pnl"] > 0]
        wr2 = round(len(wins2) / len(logs2) * 100) if logs2 else 0
        tpnl2 = sum(t["realized_pnl"] for t in logs2)
        hv2 = sum(h["total_invested"] for h in target["holdings"].values())
        eq2 = target["balance"] + hv2
        sb2 = target.get("starting_balance", 5000)
        growth2 = round((eq2 - sb2) / sb2 * 100, 1) if sb2 > 0 else 0
        await q.edit_message_text(
            "🔁 *COPY TRADER PROFILE*\n\n"
            "Trader: *" + target["username"] + "*\n"
            "Equity: *" + money(eq2) + "*\n"
            "Growth: *" + str(growth2) + "%*\n"
            "Win Rate: *" + str(wr2) + "%*\n"
            "Total Trades: *" + str(len(logs2)) + "*\n"
            "Total PnL: *" + pstr(tpnl2) + "*\n\n"
            "Copy this trader? Their future buys will be mirrored to your account at 10% of your balance per trade.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Yes, Copy This Trader", callback_data="copy_confirm_" + str(target_id))],
                [InlineKeyboardButton("Back", callback_data="v_copy")],
            ])
        )

    elif cb.startswith("copy_confirm_"):
        target_id = int(cb[13:])
        if target_id not in users:
            await q.edit_message_text("Trader not found.", reply_markup=back_main())
            return
        ud["copy_trading"] = target_id
        ud["copy_paused"] = False
        trader_name = users[target_id]["username"]
        await q.edit_message_text(
            "✅ *Copy Trading Active!*\n\n"
            "Now copying: *" + trader_name + "*\n\n"
            "Every time they buy a token, you will automatically buy up to 10% of your balance.\n"
            "You can pause or stop anytime from the Copy Trading menu.",
            parse_mode="Markdown",
            reply_markup=back_main()
        )

    elif cb == "copy_pause":
        ud["copy_paused"] = not ud.get("copy_paused", False)
        status = "PAUSED" if ud["copy_paused"] else "RESUMED"
        await q.edit_message_text(
            "Copy trading *" + status + "*.",
            parse_mode="Markdown",
            reply_markup=back_main()
        )

    elif cb == "copy_stop":
        ud["copy_trading"] = None
        ud["copy_paused"] = False
        await q.edit_message_text(
            "Copy trading stopped.",
            reply_markup=back_main()
        )

    elif cb == "copy_ext_set":
        pending[u.id] = {"action": "copy_ext_wallet"}
        await q.edit_message_text(
            "📡 *COPY EXTERNAL WALLET*\n\n"
            "Send the Solana wallet address you want to copy.\n\n"
            "Format: `<address>` or `<address> <label>`\n"
            "Example: `ABC123...XYZ my_trader`\n\n"
            "Whenever that wallet buys a token, the bot auto-mirrors the trade "
            "using your set copy amount.",
            parse_mode="Markdown",
            reply_markup=cancel_kb()
        )

    elif cb == "copy_ext_setamt":
        pending[u.id] = {"action": "copy_ext_amount"}
        cur = money(ud.get("copy_ext_amount", 50.0))
        await q.edit_message_text(
            "💰 *SET COPY AMOUNT*\n\n"
            "Current: *" + cur + "* per trade.\n\n"
            "Enter the dollar amount to use per copied trade:\nExample: `50`",
            parse_mode="Markdown",
            reply_markup=cancel_kb()
        )

    elif cb == "copy_ext_remove":
        ud["copy_ext_wallet"] = None
        ud["copy_paused"]     = False
        await q.edit_message_text(
            "🗑 External copy wallet removed.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Copy Trading Menu", callback_data="v_copy")]])
        )

    # ── WATCHLIST ──────────────────────────────────────────────────────────
    elif cb == "v_watchlist":
        wl = ud.get("watchlist", {})
        if not wl:
            await q.edit_message_text(
                "👁 *WATCHLIST*\n\nNo tokens being watched.\nPaste a CA then use the Watchlist button to add.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Back", callback_data="mm")],
                ]))
        else:
            txt = "👁 *WATCHLIST* — " + str(len(wl)) + " token" + ("s" if len(wl) != 1 else "") + "\n\nTap to view  |  🗑 to remove:\n"
            buttons = []
            for ca, w in list(wl.items()):
                sym      = w.get("symbol", "?")
                added_mc = mc_str(w.get("added_mc", 0))
                tp       = w.get("target_price")
                tm       = w.get("target_mc")
                alert_tag = " 🔔" if (tp or tm) else ""
                buttons.append([
                    InlineKeyboardButton("🪙 $" + sym + "  |  " + added_mc + alert_tag, callback_data="btt_" + ca),
                    InlineKeyboardButton("🗑", callback_data="wl_del_" + ca),
                ])
            buttons.append([InlineKeyboardButton("🗑 Clear All", callback_data="wl_del_all")])
            buttons.append([InlineKeyboardButton("◀ Back", callback_data="mm")])
            await q.edit_message_text(txt, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons))

    elif cb.startswith("wl_del_"):
        rest = cb[7:]
        if rest == "all":
            ud["watchlist"] = {}
            await q.edit_message_text("🗑 Watchlist cleared.", reply_markup=back_main())
        else:
            contract = rest
            wl = ud.get("watchlist", {})
            sym = wl.get(contract, {}).get("symbol", contract[:8])
            wl.pop(contract, None)
            ud["watchlist"] = wl
            # Refresh watchlist view
            if not wl:
                await q.edit_message_text("🗑 *$" + sym + "* removed.\n\nWatchlist is now empty.", parse_mode="Markdown", reply_markup=back_main())
            else:
                txt = "🗑 *$" + sym + "* removed.\n\n👁 *WATCHLIST* — " + str(len(wl)) + " token" + ("s" if len(wl) != 1 else "") + "\n\nTap to view  |  🗑 to remove:\n"
                buttons = []
                for ca, w in list(wl.items()):
                    s2       = w.get("symbol", "?")
                    added_mc = mc_str(w.get("added_mc", 0))
                    alert_tag = " 🔔" if (w.get("target_price") or w.get("target_mc")) else ""
                    buttons.append([
                        InlineKeyboardButton("🪙 $" + s2 + "  |  " + added_mc + alert_tag, callback_data="btt_" + ca),
                        InlineKeyboardButton("🗑", callback_data="wl_del_" + ca),
                    ])
                buttons.append([InlineKeyboardButton("🗑 Clear All", callback_data="wl_del_all")])
                buttons.append([InlineKeyboardButton("◀ Back", callback_data="mm")])
                await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    # ── WHALE ALERTS ───────────────────────────────────────────────────────
    elif cb == "v_whale":
        status = "ON 🟢" if ud.get("whale_alerts", True) else "OFF 🔴"
        await q.edit_message_text(
            f"🐋 *WHALE ALERTS*\n\n"
            f"Status: *{status}*\n\n"
            f"Detects sudden volume spikes on tokens you're holding or watching.\n"
            f"Fires when 5-minute volume is *4x the hourly average* AND at least $20K — "
            f"a strong signal that a large buyer has entered.\n\n"
            f"Toggle below:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Toggle Whale Alerts", callback_data="whale_toggle")],
                [InlineKeyboardButton("◀ Back to More",      callback_data="v_more")],
                [InlineKeyboardButton("🏠 Main Menu",        callback_data="mm")],
            ]))

    elif cb == "whale_toggle":
        ud["whale_alerts"] = not ud.get("whale_alerts", True)
        status = "ON 🟢" if ud["whale_alerts"] else "OFF 🔴"
        await q.edit_message_text(
            f"🐋 Whale alerts turned *{status}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back to More", callback_data="v_more")],
                [InlineKeyboardButton("🏠 Main Menu",   callback_data="mm")],
            ])
        )

    # ── PORTFOLIO CHART ────────────────────────────────────────────────────
    elif cb == "v_chart":
        logs = trade_log.get(u.id, [])
        if len(logs) < 2:
            await q.edit_message_text("📊 *PORTFOLIO CHART*\n\nNot enough trades yet to generate a chart.\nMake at least 2 trades first!",
                parse_mode="Markdown", reply_markup=back_main())
            return
        # Build equity curve
        eq = ud.get("starting_balance", 5000)
        points = [eq]
        for t in sorted(logs, key=lambda x: x.get("closed_at", "")):
            eq += t["realized_pnl"]
            points.append(round(eq, 2))
        # Text chart
        mn, mx = min(points), max(points)
        rows = 8
        chart_lines = []
        for row in range(rows, -1, -1):
            threshold = mn + (mx - mn) * row / rows
            line = f"{mc_str(threshold):>8} │"
            for p in points[-20:]:
                line += "█" if p >= threshold else " "
            chart_lines.append(line)
        chart_lines.append("         └" + "─" * min(len(points), 20))
        chart_txt = "\n".join(chart_lines)
        start_eq = points[0]
        end_eq   = points[-1]
        growth   = round((end_eq - start_eq) / start_eq * 100, 1) if start_eq > 0 else 0
        await q.edit_message_text(
            f"📊 *PORTFOLIO CHART*\n\n"
            f"```\n{chart_txt}\n```\n\n"
            f"Start: *{money(start_eq)}*  →  Now: *{money(end_eq)}*\n"
            f"Growth: *{'+' if growth >= 0 else ''}{growth}%*\n"
            f"Trades: *{len(logs)}*",
            parse_mode="Markdown", reply_markup=back_main())

    # ── CHALLENGE MODE ─────────────────────────────────────────────────────
    elif cb == "v_challenge":
        ch = ud.get("challenge")
        if ch and not ch.get("ended"):
            # ── Active challenge view ─────────────────────────────────────────
            from datetime import date as _date_vc
            days_total  = ch.get("days", 30)
            target_eq   = ch.get("target_eq", 0)
            start_capital = ch.get("min_capital", ch.get("start_capital", 100.0))
            today_vc    = _date_vc.today()

            # Resolve end_date
            if ch.get("end_date"):
                end_date_vc = _date_vc.fromisoformat(ch["end_date"])
            else:
                try:
                    _sd = datetime.fromisoformat(ch["started"]).date()
                except Exception:
                    _sd = today_vc
                end_date_vc = _sd + timedelta(days=days_total - 1)
                ch["end_date"] = end_date_vc.isoformat()
                save_user(u.id, ud)

            days_elapsed = (today_vc - (end_date_vc - timedelta(days=days_total - 1))).days
            days_elapsed = max(0, days_elapsed)
            remaining   = (end_date_vc - today_vc).days

            # PnL-based progress — matches _check_challenge logic exactly
            try:
                _cstart_dt = datetime.fromisoformat(ch["started"])
            except Exception:
                _cstart_dt = datetime.min
            challenge_pnl = round(sum(
                t.get("realized_pnl", 0)
                for t in trade_log.get(u.id, [])
                if t.get("mood") not in ("APEX", "AI-Sniper", "APEX-DCA")
                and _safe_dt(t.get("closed_at")) >= _cstart_dt
            ), 4)
            challenge_trades_count = sum(
                1 for t in trade_log.get(u.id, [])
                if t.get("mood") not in ("APEX", "AI-Sniper", "APEX-DCA")
                and _safe_dt(t.get("closed_at")) >= _cstart_dt
            )
            challenge_equity = round(start_capital + challenge_pnl, 4)
            profit_needed = max(target_eq - start_capital, 1)
            progress_pct  = round(challenge_pnl / profit_needed * 100, 1)
            progress_pct  = max(0.0, min(100.0, progress_pct))
            bar_filled    = int(progress_pct / 10)
            progress_bar  = "█" * bar_filled + "░" * (10 - bar_filled)
            color = "🟢" if challenge_pnl >= 0 else "🔴"

            # Check expiry on open
            if remaining < 0:
                await _check_challenge(ctx.bot, u.id, ud)
                ch = ud.get("challenge", {})
                if ch.get("ended"):
                    await q.edit_message_text(
                        "⏰ Your challenge has ended. Start a new one!",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🎯 New Challenge", callback_data="v_challenge")
                        ]])
                    )
                    return
            await q.edit_message_text(
                "🎯 *ACTIVE CHALLENGE*\n\n"
                "`" + progress_bar + "` *" + str(progress_pct) + "%*\n\n"
                "🏁 Capital:       *" + money(start_capital) + "*\n"
                "🎯 Goal:          *" + money(target_eq) + "*\n"
                + color + " Profit so far: *" + pstr(challenge_pnl) + "*\n"
                "💰 Capital now:   *" + money(challenge_equity) + "*\n"
                "📊 Trades made:   *" + str(challenge_trades_count) + "*\n\n"
                "⏰ " + str(max(0, remaining)) + " days left  |  Day " + str(days_elapsed + 1) + "/" + str(days_total) + "\n\n"
                "_ℹ️ Tracks realized profit from your trades. Savings & vault excluded._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Abandon Challenge", callback_data="ch_abandon")],
                    [InlineKeyboardButton("◀ Back to More",      callback_data="v_more")],
                    [InlineKeyboardButton("🏠 Main Menu",        callback_data="mm")],
                ]))
        else:
            current_eq = _manual_equity(ud)
            await q.edit_message_text(
                "🎯 *CHALLENGE MODE*\n\n"
                "Set your starting capital and target.\n"
                "The bot stops automatically if your capital is exhausted.\n\n"
                "_Only manual trades count — APEX gains are excluded._\n\n"
                "💰 Your current equity: *" + money(current_eq) + "*\n\n"
                "Choose a preset or set custom amounts:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💵 $100 → $1K  (14d)", callback_data="ch_p1"),
                     InlineKeyboardButton("💵 $500 → $5K  (30d)", callback_data="ch_p2")],
                    [InlineKeyboardButton("💵 $1K → $10K  (30d)", callback_data="ch_p3"),
                     InlineKeyboardButton("💵 $1K → $5K   (60d)", callback_data="ch_p4")],
                    [InlineKeyboardButton("✏️ Custom Challenge",   callback_data="ch_custom")],
                    [InlineKeyboardButton("◀ Back to More",        callback_data="v_more")],
                    [InlineKeyboardButton("🏠 Main Menu",          callback_data="mm")],
                ]))

    elif cb.startswith("ch_p") and cb[3:].isdigit():
        # ── Preset challenges ─────────────────────────────────────────────────
        presets = {
            "ch_p1": (100,  1000,  14),
            "ch_p2": (500,  5000,  30),
            "ch_p3": (1000, 10000, 30),
            "ch_p4": (1000, 5000,  60),
        }
        if cb in presets:
            min_cap, target, days = presets[cb]
            _start_date = datetime.now().date()
            _end_date   = _start_date + timedelta(days=days - 1)
            ud["challenge"] = {
                "start_eq":    min_cap,   # kept for legacy card generation
                "target_eq":   target,
                "min_capital": min_cap,   # = starting capital / risk amount
                "days":        days,
                "started":     datetime.now().isoformat(),
                "end_date":    _end_date.isoformat(),
                "ended":       False,
            }
            save_user(u.id, ud)
            await q.edit_message_text(
                "🎯 *CHALLENGE STARTED!*\n\n"
                "🏁 Starting capital: *" + money(min_cap) + "*\n"
                "🎯 Goal:             *" + money(target) + "*\n"
                "📅 Duration:         *" + str(days) + " days*\n\n"
                "_Tracks realized profit from your trades._\n"
                "_Savings & vault don't count — only what you earn from trading._\n\n"
                "Good luck! 💪",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎯 View Challenge", callback_data="v_challenge"),
                    InlineKeyboardButton("🏠 Menu",           callback_data="mm"),
                ]])
            )

    elif cb == "ch_abandon":
        ud["challenge"] = None
        await q.edit_message_text("Challenge abandoned.", reply_markup=back_main())

    elif cb == "ch_custom":
        pending[u.id] = {"action": "ch_custom_target"}
        await q.edit_message_text("🎯 Enter your target amount\n\nExample: 10000", reply_markup=cancel_kb())

    # ── MULTI ACCOUNT ──────────────────────────────────────────────────────
    elif cb == "v_accounts":
        accounts = ud.get("accounts", {})
        active   = ud.get("active_account", "main")
        txt = f"👥 *MULTI ACCOUNT*\n\nActive: *{active}*\n\nYour accounts:\n"
        txt += f"  • main — {money(ud['balance'])}\n"
        for name, acc in accounts.items():
            txt += f"  • {name} — {money(acc.get('balance', 5000))}\n"
        await q.edit_message_text(txt, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ New Account",    callback_data="acc_new")],
                [InlineKeyboardButton("🔄 Switch Account", callback_data="acc_switch")],
                [InlineKeyboardButton("🏠 Main Menu",      callback_data="mm")],
            ]))

    elif cb == "acc_new":
        pending[u.id] = {"action": "acc_new"}
        await q.edit_message_text("👥 Enter a name for your new account:\n\nExample: scalping, degen, safe", reply_markup=cancel_kb())

    elif cb == "acc_switch":
        accounts = ud.get("accounts", {})
        if not accounts:
            await q.edit_message_text("No extra accounts yet. Create one first.", reply_markup=back_main())
            return
        buttons = [[InlineKeyboardButton(f"main", callback_data="acc_use_main")]]
        for name in accounts:
            buttons.append([InlineKeyboardButton(name, callback_data=f"acc_use_{name}")])
        buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="mm")])
        await q.edit_message_text("👥 Switch to which account?", reply_markup=InlineKeyboardMarkup(buttons))

    elif cb.startswith("acc_use_"):
        name = cb[8:]
        ud["active_account"] = name
        await q.edit_message_text(f"✅ Switched to account: *{name}*", parse_mode="Markdown", reply_markup=back_main())

    # ── REFERRALS ──────────────────────────────────────────────────────────
    elif cb == "v_referrals":
        refs = ud.get("referrals", [])
        ref_link = f"https://t.me/apex_sniper_bot?start=ref_{u.id}"
        await q.edit_message_text(
            f"🔗 *REFERRAL SYSTEM*\n\n"
            f"Invite friends and earn rewards!\n\n"
            f"Your referral link:\n`{ref_link}`\n\n"
            f"Friends referred: *{len(refs)}*\n"
            f"Bonus earned: *{money(len(refs) * 100)}*\n\n"
            f"Every friend who joins gives you *$100* added to your balance!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="mm")]]))

    # ── CHANNEL SETUP ──────────────────────────────────────────────────────


    elif cb == "ch_disconnect":
        ud["channel_id"] = None
        await q.edit_message_text("Channel disconnected.", reply_markup=back_main())

    # ── HELP & DOCS ────────────────────────────────────────────────────────
    elif cb == "v_more":
        await q.edit_message_text(
            "📋 *MORE FEATURES*\n\nSelect a feature:",
            parse_mode="Markdown",
            reply_markup=more_menu_kb()
        )

    elif cb == "v_help":
        await q.edit_message_text(
            "📖 *APEX SNIPER BOT HELP*\n\n"
            "Welcome to APEX SNIPER BOT — your paper trading terminal!\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "🚀 *GETTING STARTED*\n"
            "1. Paste any Solana/ETH/BSC/Base contract address\n"
            "2. Review the token score and info\n"
            "3. Tap Buy to paper trade\n"
            "4. Monitor in Portfolio\n"
            "5. Sell when ready\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📊 *KEY FEATURES*\n"
            "• Auto stop loss & take profit\n"
            "• Mood & psychology tracking\n"
            "• Streak rewards system\n"
            "• Copy trading from top traders\n"
            "• Group competitions with bets\n"
            "• Trade sharing cards\n"
            "• Savings wallet\n"
            "• Watchlist & price alerts\n"
            "• Portfolio chart\n"
            "• Multi account\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📖 Tap below for the full documentation!\n"
            "💬 *Support:* @apex_sniper_support",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 Full Docs", url="https://docs.google.com/document/d/apex-sniper-bot-help")],
                [InlineKeyboardButton("◀ Back to More", callback_data="v_more")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="mm")]
            ]))

    # ── COMPETITION BETS UPDATE ────────────────────────────────────────────
    elif cb == "v_compete":
        await q.edit_message_text(
            "🏁 *GROUP COMPETITIONS*\n\n"
            "Challenge friends to see who grows their balance the most!\n\n"
            "• Create a competition & share the code\n"
            "• Friends join with the code\n"
            "• Winner takes the entire pot 🏆",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Create Competition", callback_data="comp_create")],
                [InlineKeyboardButton("🔗 Join Competition",  callback_data="comp_join")],
                [InlineKeyboardButton("📊 My Competitions",   callback_data="comp_track")],
                [InlineKeyboardButton("◀ Back to More",       callback_data="v_more")],
                [InlineKeyboardButton("🏠 Main Menu",         callback_data="mm")],
            ])
        )

    elif cb == "comp_create":
        # Check if user already has an active competition
        _comps = _competitions
        my_active = [
            c for c in ud.get("competitions", {})
            if c in _comps and datetime.now() < datetime.fromisoformat(_comps[c]["end_time"])
        ]
        if my_active:
            code = my_active[0]
            comp = _comps[code]
            end_dt = datetime.fromisoformat(comp["end_time"])
            days_left = max(0, (end_dt - datetime.now()).days)
            await q.edit_message_text(
                "❌ *You already have an active competition!*\n\n"
                f"📋 Code: `{code}`\n"
                f"⏳ {days_left} days left\n\n"
                "You can only create or join *one competition at a time*.\n"
                "Wait for it to end before creating a new one.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 View My Competition", callback_data="comp_track")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="mm")],
                ])
            )
            return
        pending[u.id] = {"action": "comp_bet"}
        await q.edit_message_text(
            "🏁 *CREATE COMPETITION*\n\n"
            "Step 1/2 — Enter the bet amount per player:\n\n"
            "• Enter *0* for a free competition\n"
            "• Enter an amount (e.g. *500*) to deduct from each player's balance\n\n"
            "The winner takes the entire pot!",
            parse_mode="Markdown",
            reply_markup=cancel_kb()
        )

    elif cb == "comp_join":
        # Check if user already in an active competition
        _comps = _competitions
        my_active = [
            c for c in ud.get("competitions", {})
            if c in _comps and datetime.now() < datetime.fromisoformat(_comps[c]["end_time"])
        ]
        if my_active:
            code = my_active[0]
            comp = _comps[code]
            end_dt = datetime.fromisoformat(comp["end_time"])
            days_left = max(0, (end_dt - datetime.now()).days)
            await q.edit_message_text(
                "❌ *You already have an active competition!*\n\n"
                f"📋 Code: `{code}`\n"
                f"⏳ {days_left} days left\n\n"
                "You can only be in *one competition at a time*.\n"
                "Wait for it to end before joining another.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 View My Competition", callback_data="comp_track")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="mm")],
                ])
            )
            return
        pending[u.id] = {"action": "comp_join"}
        await q.edit_message_text(
            "🔗 *JOIN COMPETITION*\n\n"
            "Enter the 6-character competition code:\n\n"
            "Example: *AB1C2D*",
            parse_mode="Markdown",
            reply_markup=cancel_kb()
        )

    elif cb.startswith("comp_days_"):
        # comp_days_7_500 → days=7, bet=500
        parts = cb.split("_")
        try:
            days = int(parts[2])
            bet  = float(parts[3])
        except Exception:
            await q.edit_message_text("❌ Error. Please try again.", reply_markup=back_main())
            return
        import random, string
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        _joined_at = datetime.now().isoformat()
        comp = {
            "code":        code,
            "creator_id":  str(u.id),
            "bet":         bet,
            "pot":         bet,
            "days":        days,
            "end_time":    (datetime.now() + timedelta(days=days)).isoformat(),
            "ended":       False,
            "winner_paid": False,
            "members":     {
                str(u.id): {
                    "username":  ud["username"],
                    "joined_at": _joined_at,
                }
            }
        }
        if bet > 0:
            ud["balance"] -= bet
        _competitions[code] = comp
        ud.setdefault("competitions", {})[code] = True
        # Persist immediately
        ud["_persisted_competitions"] = {
            k: v for k, v in _competitions.items()
            if k in ud.get("competitions", {})
        }
        save_user(u.id, ud)
        pending.pop(u.id, None)
        end_str = (datetime.now() + timedelta(days=days)).strftime("%b %d, %Y")
        pot_line = f"💰 Bet: {money(bet)} per player  |  Pot: {money(bet)}" if bet > 0 else "🆓 Free to join"
        await q.edit_message_text(
            f"🏁 *COMPETITION CREATED!*\n\n"
            f"📋 Code: `{code}`\n"
            f"⏳ Duration: {days} days\n"
            f"📅 Ends: {end_str}\n"
            f"{pot_line}\n\n"
            f"Share code *{code}* with friends to join!\n"
            f"Winner takes the entire pot 🏆",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Track Competition", callback_data="comp_track")],
                [InlineKeyboardButton("🏠 Main Menu",         callback_data="mm")],
            ])
        )

    elif cb == "comp_track":
        # ── Rebuild competitions from all users if not in memory (post-restart) ──
        for _ruid, _rud in users.items():
            for _rcode, _rcomp in _rud.get("_persisted_competitions", {}).items():
                if _rcode not in _competitions:
                    _competitions[_rcode] = _rcomp

        _comps   = _competitions
        my_codes = [c for c in ud.get("competitions", {}) if c in _comps]

        # ── Safe send helper — edit in-place, fall back to new message ────────
        async def send_comp_msg(txt, kb):
            try:
                await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb)
            except Exception:
                try:
                    await ctx.bot.send_message(
                        chat_id=q.message.chat_id,
                        text=txt,
                        parse_mode="Markdown",
                        reply_markup=kb,
                    )
                except Exception as _scm_err:
                    logger.error(f"send_comp_msg failed: {_scm_err}")

        if not my_codes:
            await send_comp_msg(
                "📊 *MY COMPETITIONS*\n\nYou have no active or recent competitions.\n\nCreate or join one!",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Create", callback_data="comp_create"),
                     InlineKeyboardButton("🔗 Join",   callback_data="comp_join")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="mm")],
                ])
            )
            return

        # ── Build full competition card for each competition ──────────────────
        txt = ""
        for code in my_codes:
            try:
                comp    = _comps[code]
                pot     = comp.get("pot", 0)
                members = comp.get("members", {})
                bet     = comp.get("bet", 0)
                try:
                    end_dt = datetime.fromisoformat(comp["end_time"])
                except Exception:
                    continue
                now_dt    = datetime.now()
                is_ended  = now_dt > end_dt or comp.get("ended", False)
                days_left = max(0, (end_dt - now_dt).days)
                end_str   = end_dt.strftime("%b %d, %Y")

                # ── Leaderboard: ONLY manual trades (no APEX / no copy trades) ──
                rankings = []
                for uid_str, m in members.items():
                    try:
                        m_uid = int(uid_str)
                        m_ud  = users.get(m_uid)
                        # ── KEY FIX: escape username to prevent Markdown parse errors
                        # Underscores in names like "my_trader_99" break Telegram Markdown
                        uname_raw = m.get("username", "?") or "?"
                        uname     = _md(uname_raw)

                        try:
                            joined_dt = datetime.fromisoformat(
                                m.get("joined_at", "2000-01-01T00:00:00")
                            )
                        except Exception:
                            joined_dt = datetime(2000, 1, 1)

                        if m_ud:
                            logs = trade_log.get(m_uid, [])
                            # Exclude APEX, AI-Sniper, APEX-DCA, Copy Trade moods
                            comp_trades = [
                                t for t in logs
                                if t.get("mood") not in (
                                    "APEX", "AI-Sniper", "APEX-DCA", "Copy Trade", "DCA"
                                )
                                and _safe_dt(t.get("closed_at")) >= joined_dt
                            ]
                            total_trades = len(comp_trades)
                            wins         = sum(1 for t in comp_trades if t.get("realized_pnl", 0) > 0)
                            total_pnl    = round(
                                sum(t.get("realized_pnl", 0) for t in comp_trades), 2
                            )
                            win_rate = (
                                round(wins / total_trades * 100, 1) if total_trades > 0 else 0.0
                            )
                        else:
                            total_trades, wins, total_pnl, win_rate = 0, 0, 0.0, 0.0

                        rankings.append({
                            "uid":    m_uid,
                            "uname":  uname,         # already MD-escaped
                            "pnl":    total_pnl,
                            "trades": total_trades,
                            "wins":   wins,
                            "wr":     win_rate,
                        })
                    except Exception as _re:
                        logger.debug(f"comp_track ranking error uid={uid_str}: {_re}")
                        continue

                # Primary sort: Win Rate desc | tiebreaker: PnL desc
                rankings.sort(key=lambda x: (-x["wr"], -x["pnl"]))

                # ── Auto-pay winner when competition ends (runs once per end) ──
                if is_ended and not comp.get("winner_paid"):
                    # Only players who made at least 1 trade are eligible to win
                    eligible = [r for r in rankings if r["trades"] > 0]
                    if eligible and pot > 0:
                        winner    = eligible[0]
                        winner_ud = users.get(winner["uid"])
                        if winner_ud:
                            # Credit the full pot to the winner's balance
                            winner_ud["balance"] = round(
                                winner_ud.get("balance", 0) + pot, 4
                            )
                            save_user(winner["uid"], winner_ud)
                            comp["winner_paid"] = True
                            comp["winner_uid"]  = str(winner["uid"])
                            comp["ended"]       = True
                            # Persist + notify all members
                            for uid_str2 in members:
                                try:
                                    m_uid2 = int(uid_str2)
                                    m_ud2  = users.get(m_uid2)
                                    if m_ud2:
                                        m_ud2["_persisted_competitions"] = {
                                            k: v for k, v in _competitions.items()
                                            if k in m_ud2.get("competitions", {})
                                        }
                                        save_user(m_uid2, m_ud2)
                                    await ctx.bot.send_message(
                                        chat_id=m_uid2,
                                        parse_mode="Markdown",
                                        text=(
                                            "🏆 *COMPETITION ENDED!*\n\n"
                                            f"📋 Code: `{code}`\n\n"
                                            f"🥇 *Winner: @{winner['uname']}*\n"
                                            f"💰 Prize: *{money(pot)}* credited to their balance!\n"
                                            f"🎯 Win Rate: *{winner['wr']}%*"
                                            f"  ({winner['wins']}/{winner['trades']} trades)\n"
                                            f"📈 PnL: *{pstr(winner['pnl'])}*\n\n"
                                            "_Only manual trades were counted._"
                                        ),
                                    )
                                except Exception:
                                    pass
                    else:
                        # No eligible trades — end without paying out
                        comp["winner_paid"] = True
                        comp["ended"]       = True
                        # Refund bets to all members
                        if bet > 0:
                            for uid_str2, m2 in members.items():
                                try:
                                    m_uid2 = int(uid_str2)
                                    m_ud2  = users.get(m_uid2)
                                    if m_ud2:
                                        m_ud2["balance"] = round(
                                            m_ud2.get("balance", 0) + bet, 4
                                        )
                                        save_user(m_uid2, m_ud2)
                                        await ctx.bot.send_message(
                                            chat_id=m_uid2,
                                            text=(
                                                f"⏰ Competition `{code}` ended with no eligible winner "
                                                f"(no manual trades). Your bet of {money(bet)} has been refunded."
                                            ),
                                        )
                                except Exception:
                                    pass

                # ── Render card ───────────────────────────────────────────────
                medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
                status_str = "🔴 *ENDED*" if is_ended else f"🟢 *{days_left} days left*"
                pot_str    = f"💰 Pot: *{money(pot)}*" if pot > 0 else "🆓 Free"
                winner_line = ""
                if is_ended and comp.get("winner_uid"):
                    w_uid_str = comp["winner_uid"]
                    w_name_raw = members.get(w_uid_str, {}).get("username", "?") or "?"
                    w_name = _md(w_name_raw)   # ← escape here too
                    w_rank = next(
                        (r for r in rankings if str(r["uid"]) == w_uid_str), None
                    )
                    w_wr = f"{w_rank['wr']}% WR" if w_rank else ""
                    winner_line = f"\n🏆 *Winner: @{w_name}*  {w_wr}\n"

                txt += (
                    f"📋 Code: `{code}`\n"
                    f"{status_str}  ·  {pot_str}  ·  👥 {len(members)} player"
                    f"{'s' if len(members) != 1 else ''}\n"
                    f"📅 Ends: {end_str}\n"
                    + winner_line
                    + "\n🏆 *Leaderboard (Win Rate — manual trades only):*\n"
                )
                if not rankings:
                    txt += "  _No data yet_\n"
                else:
                    for i, r in enumerate(rankings[:5]):
                        medal  = medals[i] if i < len(medals) else f"{i+1}."
                        is_me  = r["uid"] == u.id
                        me_tag = "  ← *you*" if is_me else ""
                        trades_str = (
                            f"{r['trades']}T  {pstr(r['pnl'])}"
                            if r["trades"] > 0
                            else "_no trades yet_"
                        )
                        txt += f"  {medal} @{r['uname']}{me_tag}  *{r['wr']}% WR*\n"
                        txt += f"      {trades_str}\n"
                txt += "\n"

            except Exception as _ce:
                logger.error(f"comp_track card error code={code}: {_ce}", exc_info=True)
                txt += f"⚠️ Could not load competition `{code}`\n\n"
                continue

        if not txt.strip():
            await send_comp_msg(
                "📊 *MY COMPETITIONS*\n\nNo competition data found.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="mm")]])
            )
            return

        await send_comp_msg(
            "📊 *MY COMPETITIONS*\n\n" + txt.strip(),
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="comp_track")],
                [InlineKeyboardButton("➕ Create",  callback_data="comp_create"),
                 InlineKeyboardButton("🔗 Join",    callback_data="comp_join")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="mm")],
            ])
        )

    elif cb == "rst_prompt":
        await q.edit_message_text(
            "RESET ACCOUNT\n\nThis wipes all holdings, history and savings.\n\nAre you sure?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Yes, Reset", callback_data="rst_confirm_" + str(u.id)),
                 InlineKeyboardButton("Cancel",     callback_data="mm")],
            ])
        )

    elif cb == "rst_confirm_" + str(u.id):
        pending.pop(u.id, None)
        ud.update({
            "balance": None, "starting_balance": None, "savings": 0.0,
            "holdings": {}, "realized_pnl": 0.0, "limit_orders": [], "price_alerts": [],
            "preset_buy": None, "preset_sell": None, "risk_pct": None,
            "max_positions": None, "daily_limit": None, "daily_trades": 0,
            "last_day": None, "planned": 0, "impulse": 0, "followed": 0,
            "broken": 0, "streak": 0, "best_streak": 0, "target_equity": None,
            "peak_equity": 0.0, "max_drawdown": 0.0, "consec_losses": 0,
            "trade_hours": {}, "auto_save_pct": None, "joined_at": datetime.now(),
        })
        trade_log[u.id] = []
        await cmd_start(update, ctx)

    elif cb.startswith("rf_"):
        contract = cb[3:]
        # ── Dedup: drop concurrent refresh taps for the same user ─────────────
        if u.id not in _rf_locks:
            _rf_locks[u.id] = _asyncio.Lock()
        if _rf_locks[u.id].locked():
            # Already refreshing — silently ignore the extra tap
            return
        async with _rf_locks[u.id]:
            info = await get_token(contract)
            if not info:
                await q.edit_message_text("Token unavailable.", reply_markup=back_main())
                return
            sc = score_token(info)
            await send_token_card(q, info, contract, ud, sc, ctx, is_query=True)

    elif cb.startswith("btt_"):
        contract = cb[4:]
        info = await get_token(contract)
        if not info:
            await q.edit_message_text("Token unavailable.", reply_markup=back_main())
            return
        sc = score_token(info)
        await send_token_card(q, info, contract, ud, sc, ctx, is_query=True)

    elif cb.startswith("bts_"):
        # Buy submenu — show amount picker
        contract = cb[4:]
        info = await get_token(contract)
        sym  = info["symbol"] if info else contract[:8]
        price_line = ("Price: *$" + str(info["price"]) + "*  |  MC: *" + mc_str(info["mc"]) + "*") if info else ""
        await q.edit_message_text(
            "⚡ *BUY $" + sym + "*\n\n" + price_line + "\n\nSelect amount:",
            parse_mode="Markdown",
            reply_markup=buy_sub_kb(contract, ud)
        )

    elif cb.startswith("sts_"):
        # Sell submenu — show % picker
        contract = cb[4:]
        if contract not in ud["holdings"]:
            await q.edit_message_text("You don't hold this token.", reply_markup=back_main())
            return
        h    = ud["holdings"][contract]
        info = await get_token(contract)
        price = info["price"] if info else h["avg_price"]
        cv    = h["amount"] * price
        cx    = price / h["avg_price"] if h.get("avg_price", 0) > 0 else 0
        ppnl  = cv - h["total_invested"]
        await q.edit_message_text(
            "🔴 *SELL $" + h["symbol"] + "*\n\n"
            "Value: *" + money(cv) + "*  |  *" + str(round(cx, 2)) + "x*\n"
            "PnL: " + pstr(ppnl) + "\n\nHow much to sell?",
            parse_mode="Markdown",
            reply_markup=sell_sub_kb(contract)
        )

    elif cb.startswith("gos_"):
        contract = cb[4:]
        if contract not in ud["holdings"]:
            await q.edit_message_text("Position not found.", reply_markup=back_main())
            return
        h = ud["holdings"][contract]
        info = await get_token(contract)
        price = info["price"] if info else h["avg_price"]
        cv = h["amount"] * price
        cx = price / h["avg_price"] if h["avg_price"] > 0 else 0
        ppnl = cv - h["total_invested"]
        await q.edit_message_text(
            "🔴 *SELL $" + h["symbol"] + "*\n\n"
            "Value: *" + money(cv) + "*\n"
            "Current: *" + str(round(cx, 2)) + "x*\n"
            "PnL: " + pstr(ppnl) + "\n\nHow much to sell?",
            parse_mode="Markdown", reply_markup=sell_kb(contract)
        )

    elif cb.startswith("bp_"):
        contract = cb[3:]
        pb = ud.get("preset_buy")
        if not pb:
            await q.edit_message_text("No preset buy set. Go to Settings first.", reply_markup=back_main())
            return
        if ud.get("mood_tracking", True):
            pending[u.id] = {"action": "buy_mood", "contract": contract, "amount": pb}
            await q.edit_message_text(
                "🧠 *MOOD CHECK*\n\nWhy are you buying this?\n\n"
                "1 - Research\n2 - Chart looks good\n3 - Community tip\n4 - FOMO\n5 - Gut feeling\n\nReply with a number:",
                parse_mode="Markdown", reply_markup=cancel_kb()
            )
        else:
            await do_buy_query(q, ud, u.id, contract, pb)

    elif cb.startswith("ba_"):
        rest = cb[3:]
        amt_str, contract = rest.split("_", 1)
        amount = float(amt_str)
        if ud.get("mood_tracking", True):
            pending[u.id] = {"action": "buy_mood", "contract": contract, "amount": amount}
            await q.edit_message_text(
                "🧠 *MOOD CHECK*\n\nWhy are you buying this?\n\n"
                "1 - Research\n2 - Chart looks good\n3 - Community tip\n4 - FOMO\n5 - Gut feeling\n\nReply with a number:",
                parse_mode="Markdown", reply_markup=cancel_kb()
            )
        else:
            await do_buy_query(q, ud, u.id, contract, amount)

    elif cb.startswith("bc_"):
        contract = cb[3:]
        pending[u.id] = {"action": "buy_custom", "contract": contract, "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text("Enter buy amount in USD:", reply_markup=cancel_kb())

    elif cb.startswith("sp_"):
        rest = cb[3:]
        pct_str, contract = rest.split("_", 1)
        await do_sell_query(q, ud, u.id, contract, pct=float(pct_str)/100)

    elif cb.startswith("sca_"):
        contract = cb[4:]
        pending[u.id] = {"action": "sell_custom", "contract": contract, "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text("Enter amount to sell (e.g. 200 or 50%):", reply_markup=cancel_kb())

    elif cb.startswith("tks_"):
        contract = cb[4:]
        info = await get_token(contract)
        if not info:
            await q.edit_message_text("Token unavailable.", reply_markup=back_main())
            return
        sc = score_token(info)
        strengths_txt = "\nStrengths:\n" + "\n".join(["  + " + s for s in sc["strengths"]]) if sc["strengths"] else ""
        warnings_txt  = "\nWarnings:\n"  + "\n".join(["  ! " + w for w in sc["warnings"]])  if sc["warnings"] else ""
        await q.edit_message_text(
            "📊 *APEX SCORE*\n\n"
            "*$" + _md(info["symbol"]) + "*\n\n"
            "Score: *" + str(sc["score"]) + "/100*\n"
            "Verdict: *" + sc["verdict"] + "*\n\n"
            "MC: " + mc_str(info["mc"]) + "\n"
            "Liq: " + money(info["liq"]) + " (" + str(info["liq_pct"]) + "%)\n"
            "Buys: " + str(info["buy_pct"]) + "%  |  Sells: " + str(100 - info["buy_pct"]) + "%"
            + strengths_txt + warnings_txt,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Token", callback_data="btt_" + contract)]])
        )

    elif cb.startswith("th_"):
        # ── Token card — Position History toggle ──────────────────────────────
        contract = cb[3:]
        h        = ud.get("holdings", {}).get(contract)

        if not h:
            await q.edit_message_text(
                "📜 *POSITION HISTORY*\n\n"
                "You don't hold this token yet.\n"
                "History starts recording from the moment you buy.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="btt_" + contract)]])
            )
            return

        try:
            sym  = h.get("symbol", "?")
            avg  = h.get("avg_price", 0) or 0
            lines = ["📜 *POSITION HISTORY — $" + sym + "*\n"]

            # ── 1. Price path ─────────────────────────────────────────────────
            ph = h.get("sr_history") or h.get("price_history") or []
            # Filter out any snapshots missing the price key
            ph = [s for s in ph if s.get("price") is not None]
            if ph:
                try:
                    spark     = _position_sparkline(h)
                    lo_p      = min(s.get("price", 0) for s in ph)
                    hi_p      = max(s.get("price", 0) for s in ph)
                    lo_x      = round(lo_p / avg, 2) if avg > 0 else 0
                    hi_x      = round(hi_p / avg, 2) if avg > 0 else 0
                    elapsed_m = round((ph[-1].get("ts", 0) - ph[0].get("ts", 0)) / 60) if len(ph) > 1 else 0
                    elapsed_txt = (str(elapsed_m) + "m") if elapsed_m < 60 else (str(round(elapsed_m / 60, 1)) + "h")
                    lines.append(
                        "📈 *Price Path*\n"
                        + ("`" + spark + "`\n" if spark else "")
                        + "  Peak: *" + str(hi_x) + "x*   Low: *" + str(lo_x) + "x*\n"
                        + "  Tracking: *" + str(len(ph)) + " snapshots · " + elapsed_txt + "*\n"
                    )
                except Exception:
                    pass

            # ── 2. Liquidity path ─────────────────────────────────────────────
            lh = h.get("liq_history", [])
            lh = [s for s in lh if s.get("liq") is not None]
            if len(lh) >= 3:
                try:
                    BLOCKS   = "▁▂▃▄▅▆▇█"
                    liq_vals = [s.get("liq", 0) for s in lh]
                    n        = len(liq_vals)
                    step     = max(1, n // 10)
                    samp     = liq_vals[::step][-10:]
                    lo_l, hi_l = min(samp), max(samp)
                    if hi_l > lo_l:
                        liq_spark = "".join(BLOCKS[min(7, int((v - lo_l) / (hi_l - lo_l) * 7))] for v in samp)
                    else:
                        liq_spark = "▄" * len(samp)
                    liq_entry = h.get("liq_at_buy", lh[0].get("liq", 0))
                    liq_now   = lh[-1].get("liq", 0)
                    liq_chg   = round((liq_now - liq_entry) / liq_entry * 100, 1) if liq_entry > 0 else 0
                    liq_icon  = "🚨" if liq_chg <= -20 else ("⚠️" if liq_chg <= -10 else "✅")
                    lines.append(
                        "💧 *Liquidity Path*\n"
                        + "`" + liq_spark + "`\n"
                        + "  Entry: *" + money(liq_entry) + "*   Now: *" + money(liq_now) + "*\n"
                        + "  Change: " + liq_icon + " *" + ("+" if liq_chg >= 0 else "") + str(liq_chg) + "%*\n"
                    )
                except Exception:
                    pass

            # ── 3. Stop loss changes ──────────────────────────────────────────
            slh = h.get("stop_loss_history", [])
            if slh:
                try:
                    _source_labels = {
                        "apex_entry":      "APEX entry",
                        "user_button":     "you (button)",
                        "user_custom":     "you (custom)",
                        "user_removed":    "you (removed)",
                        "user_cancel_all": "you (cancel all)",
                    }
                    sl_lines = []
                    for s in slh[-5:]:
                        old_v   = s.get("old")
                        new_v   = s.get("new")
                        old_txt = str(old_v) + "%" if old_v is not None else "none"
                        new_txt = str(new_v) + "%" if new_v is not None else "removed"
                        who     = _source_labels.get(s.get("source", ""), s.get("source", "?"))
                        cx_txt  = ("  · " + str(s["cx"]) + "x") if s.get("cx") else ""
                        sl_lines.append("  " + old_txt + " → *" + new_txt + "*  · " + who + cx_txt)
                    lines.append(
                        "🛑 *Stop Loss History* (" + str(len(slh)) + ")\n"
                        + "\n".join(sl_lines) + "\n"
                    )
                except Exception:
                    pass

            # ── 4. Auto-sell hits ─────────────────────────────────────────────
            ash = h.get("auto_sell_history", [])
            if ash:
                try:
                    as_lines = []
                    for s in ash:
                        as_lines.append(
                            "  " + str(s.get("x", "?")) + "x target · sold "
                            + str(int((s.get("pct") or 0) * 100)) + "% · "
                            + money(s.get("price", 0)) + " · PnL " + pstr(s.get("pnl", 0))
                        )
                    lines.append(
                        "🎯 *Auto-Sell Hits* (" + str(len(ash)) + ")\n"
                        + "\n".join(as_lines) + "\n"
                    )
                except Exception:
                    pass

            # ── 5. Threat history (APEX only) ─────────────────────────────────
            thr = h.get("threat_history", [])
            if thr:
                try:
                    _threat_icons = {"CLEAR": "🟢", "YELLOW": "🟡", "ORANGE": "🟠", "RED": "🔴"}
                    thr_lines = []
                    for s in thr[-6:]:
                        frm = s.get("from", "?")
                        to  = s.get("to",   "?")
                        fi  = _threat_icons.get(frm, "⚪")
                        ti  = _threat_icons.get(to,  "⚪")
                        thr_lines.append(
                            "  " + fi + " " + frm + " → " + ti + " " + to
                            + "  · " + str(s.get("cx", "?")) + "x · " + money(s.get("price", 0))
                        )
                    lines.append(
                        "🔴 *Threat History* (" + str(len(thr)) + ")\n"
                        + "\n".join(thr_lines) + "\n"
                    )
                except Exception:
                    pass

            # ── 6. DCA history (APEX only) ────────────────────────────────────
            dca = h.get("apex_dca_history", [])
            if dca:
                try:
                    dca_lines = []
                    for s in dca:
                        dca_lines.append(
                            "  +" + money(s.get("amount", 0)) + " @ " + money(s.get("price", 0))
                            + " · MC " + mc_str(s.get("mc", 0))
                        )
                    lines.append(
                        "⚡ *DCA History* (" + str(len(dca)) + ")\n"
                        + "\n".join(dca_lines) + "\n"
                    )
                except Exception:
                    pass

            # ── No history at all yet ─────────────────────────────────────────
            if len(lines) == 1:
                lines.append("No history recorded yet.\nData starts accumulating once the checker runs.")

            text = "\n".join(lines)
            if len(text) > 4096:
                text = text[:4092] + "…"

            await q.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back to Token", callback_data="btt_" + contract)]])
            )

        except Exception as _the:
            logger.warning(f"th_ history handler error: {_the}")
            await q.edit_message_text(
                "📜 *POSITION HISTORY*\n\nUnable to load history data.\nThis can happen with older positions.\nNew data will appear once the checker runs.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="btt_" + contract)]])
            )

    elif cb.startswith("lbo_"):
        contract = cb[4:]
        info = await get_token(contract)
        sym = info["symbol"] if info else "?"
        price = info["price"] if info else 0
        pending[u.id] = {"action": "limit_buy", "contract": contract, "symbol": sym, "current_price": price, "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "🎯 *LIMIT BUY*\n\nCurrent price: " + money(price) + "\n\n"
            "Enter target price and amount:\nFormat: price amount\nExample: 0.005 100",
            parse_mode="Markdown", reply_markup=cancel_kb()
        )

    elif cb == "wl_add_price":
        contract = pending.get(u.id, {}).get("contract", "")
        pending[u.id] = {"action": "wl_target_price", "contract": contract, "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "👁 Enter target *PRICE* to alert:\nExample: `0.00005`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back", callback_data="wl_" + contract)],
                [InlineKeyboardButton("Cancel",  callback_data="mm")],
            ])
        )

    elif cb == "wl_add_mc":
        contract = pending.get(u.id, {}).get("contract", "")
        pending[u.id] = {"action": "wl_target_mc", "contract": contract, "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "👁 Enter target *MARKET CAP* to alert:\nExample: `100000` (=$100K)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back", callback_data="wl_" + contract)],
                [InlineKeyboardButton("Cancel",  callback_data="mm")],
            ])
        )

    elif cb.startswith("lso_"):
        contract = cb[4:]
        if contract not in ud["holdings"]:
            await q.edit_message_text("Position not found.", reply_markup=back_main())
            return
        h = ud["holdings"][contract]
        info = await get_token(contract)
        price = info["price"] if info else h["avg_price"]
        pending[u.id] = {"action": "limit_sell", "contract": contract, "symbol": h["symbol"], "current_price": price, "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "🎯 *LIMIT SELL*\n\nCurrent price: " + money(price) + "\n\n"
            "Enter target price and amount:\nFormat: price amount%\nExample: 0.012 50%",
            parse_mode="Markdown", reply_markup=cancel_kb()
        )

    elif cb.startswith("wl_") and len(cb) > 10:
        contract = cb[3:]
        info = await get_token(contract)
        if not info:
            await q.edit_message_text("Token not found.", reply_markup=back_main())
            return
        if not ud.get("watchlist"):
            ud["watchlist"] = {}
        ud["watchlist"][contract] = {
            "symbol": info["symbol"], "name": info["name"],
            "added_price": info["price"], "added_mc": info["mc"],
            "target_price": None, "target_mc": None,
        }
        pending[u.id] = {"action": "wl_waiting", "contract": contract}
        _wl_text = (
            f"👁 *${_md(info['symbol'])}* added to watchlist!\n\nSet an alert target (optional):"
        )
        _wl_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Alert by Price",        callback_data="wl_add_price")],
            [InlineKeyboardButton("Alert by Market Cap",   callback_data="wl_add_mc")],
            [InlineKeyboardButton("No Alert — Just Watch", callback_data="mm")],
            [InlineKeyboardButton("◀ Back to Token",       callback_data="btt_" + contract)],
        ])
        # Token cards with charts are photo messages — edit_message_text fails on
        # those silently, leaving the user with no confirmation. Use send_message
        # when the current message has a photo, otherwise edit in-place.
        if q.message and q.message.photo:
            await ctx.bot.send_message(
                chat_id=q.message.chat_id,
                text=_wl_text,
                parse_mode="Markdown",
                reply_markup=_wl_kb,
            )
        else:
            try:
                await q.edit_message_text(_wl_text, parse_mode="Markdown", reply_markup=_wl_kb)
            except Exception:
                await ctx.bot.send_message(
                    chat_id=q.message.chat_id,
                    text=_wl_text,
                    parse_mode="Markdown",
                    reply_markup=_wl_kb,
                )

    elif cb.startswith("pal_"):
        contract = cb[4:]
        info = await get_token(contract)
        sym = info["symbol"] if info else "?"
        price = info["price"] if info else 0
        pending[u.id] = {"action": "price_alert", "contract": contract, "symbol": sym, "current_price": price, "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "🔔 *PRICE ALERT*\n\nCurrent price: " + money(price) + "\n\nEnter target price:",
            parse_mode="Markdown", reply_markup=cancel_kb()
        )

    elif cb.startswith("al_cancel_ca_"):
        # Cancel alert for this specific token directly from the token card
        contract = cb[13:]
        alerts = ud.get("price_alerts", [])
        removed = [a for a in alerts if a.get("contract") == contract]
        ud["price_alerts"] = [a for a in alerts if a.get("contract") != contract]
        if removed:
            sym = removed[0].get("symbol", "?")
            await q.edit_message_text(
                "🗑 Price alert for *$" + sym + "* cancelled.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back to Token", callback_data="btt_" + contract)]])
            )
        else:
            await q.edit_message_text("No alert found for this token.", reply_markup=back_main())

    elif cb.startswith("asm_"):
        contract = cb[4:]
        if contract not in ud["holdings"]:
            await q.edit_message_text("Position not found.", reply_markup=back_main())
            return
        h = ud["holdings"][contract]
        avg = h["avg_price"]
        await q.edit_message_text(
            "🎯 *AUTO-SELL  $" + h["symbol"] + "*\n\nBuy price: " + money(avg) + "\n\nChoose a preset:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("50% at 2x + 100% at 5x",          callback_data="asq_2_5_"   + contract)],
                [InlineKeyboardButton("50% at 3x + 100% at 10x",         callback_data="asq_3_10_"  + contract)],
                [InlineKeyboardButton("25% at 2x + 25% at 5x + 50% at 10x", callback_data="asq_2_5_10_" + contract)],
                [InlineKeyboardButton("100% at 2x",                      callback_data="asq_2_"     + contract)],
                [InlineKeyboardButton("Custom Targets",                  callback_data="ascus_"     + contract)],
                [InlineKeyboardButton("Back",                            callback_data="btt_"       + contract)],
            ])
        )

    elif cb.startswith("asq_2_5_10_"):
        contract = cb[len("asq_2_5_10_"):]
        if contract in ud["holdings"]:
            ud["holdings"][contract]["auto_sells"] = [
                {"pct": 0.25, "x": 2.0, "triggered": False},
                {"pct": 0.25, "x": 5.0, "triggered": False},
                {"pct": 0.50, "x": 10.0, "triggered": False},
            ]
            avg = ud["holdings"][contract]["avg_price"]
            sym = ud["holdings"][contract]["symbol"]
            await q.edit_message_text(
                "✅ Auto-sells set for $" + sym + ":\n"
                "  25% at 2x (~" + money(avg*2) + ")\n"
                "  25% at 5x (~" + money(avg*5) + ")\n"
                "  50% at 10x (~" + money(avg*10) + ")",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="btt_" + contract)]])
            )

    elif cb.startswith("asq_2_"):
        contract = cb[len("asq_2_"):]
        if contract in ud["holdings"]:
            ud["holdings"][contract]["auto_sells"] = [{"pct": 1.0, "x": 2.0, "triggered": False}]
            avg = ud["holdings"][contract]["avg_price"]
            sym = ud["holdings"][contract]["symbol"]
            await q.edit_message_text(
                "✅ Auto-sell set for $" + sym + ":\n  100% at 2x (~" + money(avg*2) + ")",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="btt_" + contract)]])
            )

    elif cb.startswith("asq_"):
        rest = cb[4:]
        parts = rest.split("_", 2)
        x1, x2, contract = int(parts[0]), int(parts[1]), parts[2]
        if contract in ud["holdings"]:
            ud["holdings"][contract]["auto_sells"] = [
                {"pct": 0.5, "x": float(x1), "triggered": False},
                {"pct": 1.0, "x": float(x2), "triggered": False},
            ]
            avg = ud["holdings"][contract]["avg_price"]
            sym = ud["holdings"][contract]["symbol"]
            await q.edit_message_text(
                "✅ Auto-sells set for $" + sym + ":\n"
                "  50% at " + str(x1) + "x (~" + money(avg*x1) + ")\n"
                "  100% at " + str(x2) + "x (~" + money(avg*x2) + ")",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="btt_" + contract)]])
            )

    elif cb.startswith("ascus_"):
        contract = cb[6:]
        pending[u.id] = {"action": "as_custom", "contract": contract}
        await q.edit_message_text(
            "Enter targets:\nFormat: 50% 2x 100% 5x",
            reply_markup=cancel_kb()
        )

    elif cb.startswith("vtg_"):
        contract = cb[4:]
        if contract not in ud["holdings"]:
            await q.edit_message_text("Position not found.", reply_markup=back_main())
            return
        h = ud["holdings"][contract]
        targets = h.get("auto_sells", [])
        sl = h.get("stop_loss_pct")
        avg = h["avg_price"]
        sym = h["symbol"]

        lines = ["🎯 *TARGETS  $" + sym + "*\n"]
        buttons = []

        if targets:
            for i, t in enumerate(targets):
                status = "✅ DONE" if t.get("triggered") else "⏳ WAITING"
                line = status + "  Sell " + str(int(t["pct"]*100)) + "% at " + str(t["x"]) + "x  (~" + money(avg * t["x"]) + ")"
                lines.append(line)
                if not t.get("triggered"):
                    buttons.append([InlineKeyboardButton(
                        "🗑 Cancel " + str(int(t["pct"]*100)) + "% @ " + str(t["x"]) + "x",
                        callback_data="as_del_" + str(i) + "_" + contract
                    )])
        else:
            lines.append("No auto-sell targets set.")

        lines.append("")
        if sl:
            lines.append("🛑 Stop Loss: " + str(sl) + "% drop  (~" + money(avg * (1 - sl/100)) + ")")
            buttons.append([InlineKeyboardButton("🗑 Cancel Stop Loss", callback_data="sl_del_" + contract)])
        else:
            lines.append("No stop loss set.")

        buttons.append([InlineKeyboardButton("🗑 Cancel ALL Targets", callback_data="cat_" + contract)])
        buttons.append([InlineKeyboardButton("◀ Back", callback_data="btt_" + contract)])

        await q.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif cb.startswith("as_del_"):
        # Cancel individual auto-sell target: as_del_{index}_{contract}
        rest = cb[7:]
        idx_str, contract = rest.split("_", 1)
        if contract in ud["holdings"]:
            h = ud["holdings"][contract]
            targets = h.get("auto_sells", [])
            idx = int(idx_str)
            if 0 <= idx < len(targets):
                removed = targets.pop(idx)
                sym = h["symbol"]
                await q.edit_message_text(
                    "🗑 Auto-sell *" + str(int(removed["pct"]*100)) + "% @ " + str(removed["x"]) + "x* cancelled for $" + sym,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back to Targets", callback_data="vtg_" + contract)]])
                )
            else:
                await q.edit_message_text("Target not found.", reply_markup=back_main())
        else:
            await q.edit_message_text("Position not found.", reply_markup=back_main())

    elif cb.startswith("sl_del_"):
        contract = cb[7:]
        if contract in ud["holdings"]:
            h = ud["holdings"][contract]
            import time as _tsld
            h.setdefault("stop_loss_history", []).append({
                "old":    h.get("stop_loss_pct"),
                "new":    None,
                "source": "user_removed",
                "cx":     None,
                "ts":     _tsld.time(),
            })
            h["stop_loss_pct"] = None
            sym = h["symbol"]
            await q.edit_message_text(
                "🗑 Stop loss cancelled for *$" + sym + "*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back to Targets", callback_data="vtg_" + contract)]])
            )
        else:
            await q.edit_message_text("Position not found.", reply_markup=back_main())

    elif cb.startswith("cat_"):
        contract = cb[4:]
        if contract in ud["holdings"]:
            h = ud["holdings"][contract]
            import time as _tcat
            if h.get("stop_loss_pct") is not None:
                h.setdefault("stop_loss_history", []).append({
                    "old":    h["stop_loss_pct"],
                    "new":    None,
                    "source": "user_cancel_all",
                    "cx":     None,
                    "ts":     _tcat.time(),
                })
            h["auto_sells"] = []
            h["stop_loss_pct"] = None
            sym = h["symbol"]
            await q.edit_message_text(
                "🗑 All targets & stop loss cancelled for *$" + sym + "*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="btt_" + contract)]])
            )

    elif cb.startswith("slm_"):
        contract = cb[4:]
        if contract not in ud["holdings"]:
            await q.edit_message_text("Position not found.", reply_markup=back_main())
            return
        h = ud["holdings"][contract]
        sl = h.get("stop_loss_pct")
        sl_info = "  Current: " + str(sl) + "%" if sl else ""
        await q.edit_message_text(
            "🛑 *STOP LOSS  $" + h["symbol"] + "*" + sl_info + "\n\nSell ALL if price drops by:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("25%", callback_data="sls_25_" + contract),
                 InlineKeyboardButton("50%", callback_data="sls_50_" + contract),
                 InlineKeyboardButton("75%", callback_data="sls_75_" + contract)],
                [InlineKeyboardButton("Custom %",  callback_data="slc_" + contract)],
                [InlineKeyboardButton("Remove SL", callback_data="slr_" + contract)],
                [InlineKeyboardButton("Back",      callback_data="btt_" + contract)],
            ])
        )

    elif cb.startswith("sls_"):
        rest = cb[4:]
        pct_str, contract = rest.split("_", 1)
        pct = float(pct_str)
        if contract in ud["holdings"]:
            h = ud["holdings"][contract]
            import time as _tsls
            h.setdefault("stop_loss_history", []).append({
                "old":    h.get("stop_loss_pct"),
                "new":    pct,
                "source": "user_button",
                "cx":     round((h.get("avg_price", 1) and 1), 3),
                "ts":     _tsls.time(),
            })
            h["stop_loss_pct"] = pct
            trigger = h["avg_price"] * (1 - pct / 100)
            await q.edit_message_text(
                "✅ Stop loss set: " + str(int(pct)) + "% drop → " + money(trigger),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="btt_" + contract)]])
            )

    elif cb.startswith("slc_"):
        contract = cb[4:]
        pending[u.id] = {"action": "sl_custom", "contract": contract}
        await q.edit_message_text("Enter stop loss % drop (e.g. 60):", reply_markup=cancel_kb())

    elif cb.startswith("slr_"):
        contract = cb[4:]
        if contract in ud["holdings"]:
            h = ud["holdings"][contract]
            import time as _tslr
            h.setdefault("stop_loss_history", []).append({
                "old":    h.get("stop_loss_pct"),
                "new":    None,
                "source": "user_removed",
                "cx":     None,
                "ts":     _tslr.time(),
            })
            h["stop_loss_pct"] = None
            sym = h["symbol"]
            await q.edit_message_text(
                "Stop loss removed for $" + sym,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="btt_" + contract)]])
            )

    elif cb.startswith("jnl_"):
        contract = cb[4:]
        pending[u.id] = {"action": "journal", "contract": contract}
        h = ud["holdings"].get(contract, {})
        existing = "  Current: \"" + h.get("journal", "") + "\"" if h.get("journal") else ""
        await q.edit_message_text(
            "📝 Journal for $" + h.get("symbol", "?") + existing + "\n\nEnter your trade thesis:",
            reply_markup=cancel_kb()
        )

    # ── RISK CALCULATOR ────────────────────────────────────────────────────────
    elif cb == "cfg_riskcalc":
        ud["risk_calc"] = not ud.get("risk_calc", True)
        status = "ON ✅" if ud["risk_calc"] else "OFF ❌"
        await q.edit_message_text(
            "🧮 *RISK CALCULATOR*\n\nShows you the full risk/reward breakdown before every buy.\n\nStatus: *" + status + "*",
            parse_mode="Markdown", reply_markup=settings_kb(ud)
        )

    elif cb == "rc_yes":
        p = pending.get(u.id, {})
        if p.get("action") == "risk_confirm":
            pending.pop(u.id, None)
            contract = p["contract"]
            amount   = p["amount"]
            mood     = p.get("mood", "")
            await q.edit_message_text("Executing buy...")
            result = await do_buy_core(ud, u.id, contract, amount, mood=mood)
            if isinstance(result, str):
                await q.edit_message_text(result, reply_markup=main_menu_kb())
                return
            info, tokens = result
            liq_warn = "\n\nWARNING: LOW LIQUIDITY" if info["liq"] < 50_000 else ""
            await q.edit_message_text(
                t(ud, "buy_exec",
                  name=info["name"], symbol=info["symbol"],
                  spent=money(amount), tokens=str(round(tokens, 4)),
                  price=money(info["price"]), mc=mc_str(info["mc"]),
                  liq=money(info["liq"]), cash=money(ud["balance"])
                ) + liq_warn,
                parse_mode="Markdown", reply_markup=buy_done_kb(contract)
            )
        else:
            await q.edit_message_text("No pending buy found.", reply_markup=back_main())

    elif cb == "rc_no":
        pending.pop(u.id, None)
        await q.edit_message_text("❌ Buy cancelled.", reply_markup=back_main())

    # ── LEADERBOARD ────────────────────────────────────────────────────────────
    elif cb == "v_leader":
        scores = []
        for uid2, d in users.items():
            if d.get("balance") is None:
                continue
            logs2 = trade_log.get(uid2, [])
            if not logs2:
                continue
            wins2  = [tr for tr in logs2 if tr["realized_pnl"] > 0]
            wr2    = round(len(wins2) / len(logs2) * 100, 1) if logs2 else 0
            tpnl2  = sum(tr["realized_pnl"] for tr in logs2)
            hv2    = sum(h["total_invested"] for h in d["holdings"].values())
            eq2    = d["balance"] + hv2 + d.get("savings", 0)
            sb2    = d.get("starting_balance", 5000) or 5000
            growth = round((eq2 - sb2) / sb2 * 100, 1) if sb2 > 0 else 0
            scores.append({
                "uid": uid2, "username": d["username"],
                "eq": eq2, "pnl": tpnl2, "wr": wr2,
                "trades": len(logs2), "growth": growth,
                "streak": d.get("best_streak", 0),
            })

        if not scores:
            await q.edit_message_text("🏆 *LEADERBOARD*\n\nNo traders with history yet.", parse_mode="Markdown", reply_markup=back_main())
            return

        scores.sort(key=lambda x: x["eq"], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines  = ["🏆 *GLOBAL LEADERBOARD*\n_(Ranked by Equity)_\n"]
        for i, s in enumerate(scores[:10]):
            medal  = medals[i] if i < 3 else str(i + 1) + "."
            me_tag = "  ← *YOU*" if s["uid"] == u.id else ""
            lines.append(
                medal + " *@" + s["username"] + "*" + me_tag + "\n"
                "  💰 " + money(s["eq"]) + "  |  " + str(s["growth"]) + "% growth\n"
                "  📊 " + str(s["trades"]) + " trades  WR:" + str(s["wr"]) + "%  🔥" + str(s["streak"]) + " streak\n"
            )
        my_rank = next((i + 1 for i, s in enumerate(scores) if s["uid"] == u.id), None)
        if my_rank and my_rank > 10:
            lines.append("\n📍 Your rank: #" + str(my_rank) + " of " + str(len(scores)))
        await q.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 By PnL",      callback_data="lb_pnl"),
                 InlineKeyboardButton("🎯 By Win Rate",  callback_data="lb_wr")],
                [InlineKeyboardButton("🏠 Main Menu",   callback_data="mm")],
            ])
        )

    elif cb in ("lb_pnl", "lb_wr"):
        scores = []
        for uid2, d in users.items():
            if d.get("balance") is None:
                continue
            logs2 = trade_log.get(uid2, [])
            if not logs2:
                continue
            wins2 = [tr for tr in logs2 if tr["realized_pnl"] > 0]
            wr2   = round(len(wins2) / len(logs2) * 100, 1) if logs2 else 0
            tpnl2 = sum(tr["realized_pnl"] for tr in logs2)
            hv2   = sum(h["total_invested"] for h in d["holdings"].values())
            eq2   = d["balance"] + hv2 + d.get("savings", 0)
            scores.append({"uid": uid2, "username": d["username"], "eq": eq2, "pnl": tpnl2, "wr": wr2, "trades": len(logs2)})
        sort_key = "pnl" if cb == "lb_pnl" else "wr"
        label    = "PnL" if cb == "lb_pnl" else "Win Rate"
        scores.sort(key=lambda x: x[sort_key], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines  = ["🏆 *LEADERBOARD — by " + label + "*\n"]
        for i, s in enumerate(scores[:10]):
            medal  = medals[i] if i < 3 else str(i + 1) + "."
            val    = pstr(s["pnl"]) if cb == "lb_pnl" else str(s["wr"]) + "% WR"
            me_tag = "  ← *YOU*" if s["uid"] == u.id else ""
            lines.append(medal + " *@" + s["username"] + "*" + me_tag + "  " + val + "  (" + str(s["trades"]) + " trades)\n")
        await q.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 By Equity",   callback_data="v_leader"),
                 InlineKeyboardButton("📊 By PnL",      callback_data="lb_pnl"),
                 InlineKeyboardButton("🎯 By Win Rate", callback_data="lb_wr")],
                [InlineKeyboardButton("🏠 Main Menu",   callback_data="mm")],
            ])
        )

    # ── SNIPER MODE v2 ─────────────────────────────────────────────────────────
    elif cb == "v_sniper":
        auto_on  = ud.get("sniper_auto", False)
        adv_on   = ud.get("sniper_advisory", False)
        apex_on  = ud.get("apex_mode", False)
        budget   = ud.get("sniper_daily_budget", 500.0)
        spent    = ud.get("sniper_daily_spent", 0.0)
        sf       = ud.get("sniper_filters", {})
        chains   = ud.get("sniper_chains", {})
        chain_str = "  ".join(
            ("✅" if v else "❌") + " " + k.upper()[:3]
            for k, v in chains.items()
        )
        log      = ud.get("sniper_log", [])
        bought_n = sum(1 for e in log if e.get("bought"))
        skip_n   = len(log) - bought_n

        # ── Skip reason breakdown ─────────────────────────────────────────────
        skip_counts = ud.get("sniper_skip_counts", {})
        _skip_icons = {
            "hard_flag":    "🚩",
            "score":        "📉",
            "liquidity":    "💧",
            "mc_range":     "📊",
            "age":          "⏰",
            "low_activity": "😴",
            "sell_pressure":"📛",
            "wash_trade":   "🔄",
            "no_socials":   "👻",
            "few_holders":  "👥",
            "other":        "❓",
        }
        _skip_labels = {
            "hard_flag":    "Hard flag",
            "score":        "Score too low",
            "liquidity":    "Low liquidity",
            "mc_range":     "MC out of range",
            "age":          "Too old",
            "low_activity": "Low activity",
            "sell_pressure":"Sell pressure",
            "wash_trade":   "Wash trading",
            "no_socials":   "No socials (−10 pts)",
            "few_holders":  "Few holders",
            "other":        "Other",
        }
        if skip_counts:
            top_skips = sorted(skip_counts.items(), key=lambda x: -x[1])[:4]
            skip_detail = "  " + "  ·  ".join(
                _skip_icons.get(k, "❓") + " " + _skip_labels.get(k, k) + ": " + str(v)
                for k, v in top_skips
            )
        else:
            skip_detail = "  _No skip data yet_"
        if apex_on:
            mode_line = "⚡ *APEX ENGINE — ACTIVE*"
        elif auto_on:
            mode_line = "🟢 *AUTO MODE — ACTIVE*  _(legacy)_"
        elif adv_on:
            mode_line = "🧠 *ADVISORY MODE — ACTIVE*"
        else:
            mode_line = "🔴 *SNIPER OFF*"

        await q.edit_message_text(
            "🎯 *AI SNIPER*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            + mode_line + "\n\n"
            "⛓️ Chains: " + chain_str + "\n"
            "💰 Budget: *" + money(budget) + "*  (spent: " + money(spent) + ")\n"
            "📊 Session: *" + str(bought_n) + " bought*  ·  " + str(skip_n) + " skipped\n"
            + (skip_detail + "\n" if skip_counts else "") +
            "\n"
            "⚡ *APEX* — Full autonomous engine. Trailing exits, threat\n"
            "  detection, vault locking, self-calibration. _Recommended._\n\n"
            "🧠 *Advisory* — AI flags signals to your DM or channel.\n"
            "  You confirm every trade manually.\n\n"
            "🔧 *Active Filters:*\n"
            "  Score ≥ *" + str(sf.get("min_score", 35)) + "*  ·  "
            "Liq ≥ *" + money(sf.get("min_liq", 5_000)) + "*  ·  "
            "MC *" + mc_str(sf.get("min_mc", 10_000)) + "–" + mc_str(sf.get("max_mc", 500_000)) + "*\n"
            "  Buy amt: *" + money(sf.get("buy_amount", 20)) + "*  ·  "
            "Age ≤ *" + str(sf.get("max_age_h", 6.0)) + "h*  ·  "
            "Buy% ≥ *" + str(sf.get("min_buy_pct", 45)) + "%*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⚡ APEX Engine" + (" ✅" if apex_on else " — Tap to configure"),
                    callback_data="apex_menu"
                )],
                [InlineKeyboardButton("🧠 Advisory Mode", callback_data="sniper_adv_menu")],
                [InlineKeyboardButton("⛓️ Chains",  callback_data="sniper_chains_menu"),
                 InlineKeyboardButton("⚙️ Filters", callback_data="sniper_filters_menu")],
                [InlineKeyboardButton("📋 Sniper Log",  callback_data="sniper_log_view"),
                 InlineKeyboardButton("💰 Budget",      callback_data="sniper_budget_cfg")],
                [InlineKeyboardButton("📊 Scan Log Channel", callback_data="sniper_log_ch_menu"),
                 InlineKeyboardButton("💡 Narrative History", callback_data="sniper_narrative_hist")],
                [InlineKeyboardButton("👀 KOL Tracker", callback_data="kol_menu")],
                [InlineKeyboardButton("🔩 Manual / Legacy Mode", callback_data="sniper_auto_menu")],
                [InlineKeyboardButton("◀ Back", callback_data="mm")],
            ])
        )

    elif cb == "sniper_auto_menu":
        auto_on  = ud.get("sniper_auto", False)
        notify   = ud.get("sniper_auto_notify", True)
        sl_on    = ud.get("sniper_auto_sl", True)
        tp_on    = ud.get("sniper_auto_tp", True)
        sl_pct   = ud.get("sniper_auto_sl_pct", 40.0)
        tp_xs    = ud.get("sniper_auto_tp_x", [2.0, 5.0])
        tp_str   = " + ".join(str(x) + "x" for x in tp_xs)
        await q.edit_message_text(
            "🔩 *MANUAL / LEGACY MODE*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⚠️ _This mode is superseded by APEX._\n"
            "_APEX has smarter exits, trailing stops, vault locking,_\n"
            "_and self-calibration. Use APEX instead._\n\n"
            "Status: *" + ("🟢 ON" if auto_on else "🔴 OFF") + "*\n\n"
            "Buys automatically on SNIPE verdict with fixed SL/TP.\n"
            "No active position management after entry.\n\n"
            "Stop Loss: *" + ("ON — " + str(sl_pct) + "%" if sl_on else "OFF") + "*\n"
            "Take Profit: *" + ("ON — " + tp_str if tp_on else "OFF") + "*\n"
            "Notifications: *" + ("ON 🔔" if notify else "OFF 🔕") + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚡ Switch to APEX Instead", callback_data="apex_menu")],
                [InlineKeyboardButton(("🔴 Disable" if auto_on else "🟢 Enable (Legacy)"), callback_data="sniper_auto_toggle")],
                [InlineKeyboardButton(("✅ Stop Loss ON" if sl_on else "❌ Stop Loss OFF"), callback_data="sniper_sl_toggle"),
                 InlineKeyboardButton("⚙️ SL %", callback_data="sniper_sl_pct_cfg")],
                [InlineKeyboardButton(("✅ Take Profit ON" if tp_on else "❌ Take Profit OFF"), callback_data="sniper_tp_toggle"),
                 InlineKeyboardButton("⚙️ TP Targets", callback_data="sniper_tp_cfg")],
                [InlineKeyboardButton(("🔕 Mute Notifs" if notify else "🔔 Unmute Notifs"), callback_data="sniper_auto_notif")],
                [InlineKeyboardButton("◀ Back", callback_data="v_sniper")],
            ])
        )

    elif cb == "sniper_adv_menu":
        adv_on   = ud.get("sniper_advisory", False)
        notify   = ud.get("sniper_adv_notify", True)
        ch_id    = ud.get("sniper_broadcast_channel")
        ch_name  = ud.get("sniper_broadcast_name", "")
        ch_line  = ("📡 Broadcast: *" + ch_name + "*") if ch_id else "📡 Broadcast: *Not set*"
        await q.edit_message_text(
            "🧠 *AI ADVISORY MODE*\n\n"
            "AI analyzes each token and sends a compact notification to your DM.\n"
            "Tap 👁 View Analysis to see the full report.\n"
            "You confirm or skip — full control stays with you.\n\n"
            "Status: *" + ("🟢 ON" if adv_on else "🔴 OFF") + "*\n\n"
            "📬 *Notification Mode:*\n"
            + ("🔔 *DM Mode* — signals sent to YOUR DM only\n   Channel is silent." if notify else
               "📡 *Channel Mode* — signals sent to CHANNEL only\n   Your DM receives nothing.") + "\n\n"
            + ch_line,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(("🔴 Disable Advisory" if adv_on else "🟢 Enable Advisory"), callback_data="sniper_adv_toggle")],
                [InlineKeyboardButton(("🔕 Mute DM Notifs" if notify else "🔔 Unmute DM Notifs"), callback_data="sniper_adv_notif")],
                [InlineKeyboardButton(("📡 Change Channel" if ch_id else "📡 Set Broadcast Channel"), callback_data="sniper_channel_setup")],
                [InlineKeyboardButton("🗑 Remove Channel", callback_data="sniper_channel_remove")] if ch_id else [],
                [InlineKeyboardButton("◀ Back", callback_data="v_sniper")],
            ])
        )

    elif cb == "sniper_auto_toggle":
        turning_on = not ud.get("sniper_auto", False)
        # Mutual exclusion: if enabling Auto, Advisory must be OFF
        if turning_on and ud.get("sniper_advisory", False):
            ud["sniper_advisory"] = False
            switch_note = "\n⚠️ *Advisory Mode was switched OFF automatically.*"
        else:
            switch_note = ""
        ud["sniper_auto"] = turning_on
        auto_on = ud["sniper_auto"]
        notify  = ud.get("sniper_auto_notify", True)
        sl_on   = ud.get("sniper_auto_sl", True)
        tp_on   = ud.get("sniper_auto_tp", True)
        sl_pct  = ud.get("sniper_auto_sl_pct", 40.0)
        tp_xs   = ud.get("sniper_auto_tp_x", [2.0, 5.0])
        tp_str  = " + ".join(str(x) + "x" for x in tp_xs)
        await q.edit_message_text(
            "🤖 *AUTO SNIPER MODE*\n\n"
            "AI analyzes every token. If it says SNIPE, the bot buys automatically,\n"
            "sets stop loss and take profit, and exits on dump detection.\n\n"
            "Status: *" + ("🟢 ON" if auto_on else "🔴 OFF") + "*\n"
            "Notifications: *" + ("ON 🔔" if notify else "OFF 🔕") + "*\n"
            "Auto Stop Loss: *" + ("ON — " + str(sl_pct) + "%" if sl_on else "OFF") + "*\n"
            "Auto Take Profit: *" + ("ON — " + tp_str if tp_on else "OFF") + "*"
            + switch_note,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(("🔴 Disable Auto" if auto_on else "🟢 Enable Auto"), callback_data="sniper_auto_toggle")],
                [InlineKeyboardButton(("🔕 Mute Notifications" if notify else "🔔 Unmute Notifications"), callback_data="sniper_auto_notif")],
                [InlineKeyboardButton(("✅ Stop Loss ON" if sl_on else "❌ Stop Loss OFF"), callback_data="sniper_sl_toggle"),
                 InlineKeyboardButton("⚙️ SL %", callback_data="sniper_sl_pct_cfg")],
                [InlineKeyboardButton(("✅ Take Profit ON" if tp_on else "❌ Take Profit OFF"), callback_data="sniper_tp_toggle"),
                 InlineKeyboardButton("⚙️ TP Targets", callback_data="sniper_tp_cfg")],
                [InlineKeyboardButton("◀ Back", callback_data="v_sniper")],
            ])
        )

    elif cb == "sniper_adv_toggle":
        turning_on = not ud.get("sniper_advisory", False)
        # Mutual exclusion: if enabling Advisory, Auto must be OFF
        if turning_on and ud.get("sniper_auto", False):
            ud["sniper_auto"] = False   # auto-disable Auto mode
            switch_note = "\n\n⚠️ *Auto Mode was switched OFF automatically.*"
        else:
            switch_note = ""
        ud["sniper_advisory"] = turning_on
        adv_on = ud["sniper_advisory"]
        notify = ud.get("sniper_adv_notify", True)
        await q.edit_message_text(
            "🧠 *AI ADVISORY MODE*\n\n"
            "AI analyzes each token and sends you a full report with verdict, thesis,\n"
            "red flags, green flags and a suggested entry amount.\n"
            "You confirm or skip — full control stays with you.\n\n"
            "Status: *" + ("🟢 ON" if adv_on else "🔴 OFF") + "*\n"
            "Notifications: *" + ("ON 🔔" if notify else "OFF 🔕") + "*"
            + switch_note,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(("🔴 Disable Advisory" if adv_on else "🟢 Enable Advisory"), callback_data="sniper_adv_toggle")],
                [InlineKeyboardButton(("📡 Switch to Channel Mode" if notify else "🔔 Switch to DM Mode"), callback_data="sniper_adv_notif")],
                [InlineKeyboardButton("◀ Back", callback_data="v_sniper")],
            ])
        )

    elif cb == "sniper_auto_notif":
        ud["sniper_auto_notify"] = not ud.get("sniper_auto_notify", True)
        auto_on = ud.get("sniper_auto", False)
        notify  = ud["sniper_auto_notify"]
        sl_on   = ud.get("sniper_auto_sl", True)
        tp_on   = ud.get("sniper_auto_tp", True)
        sl_pct  = ud.get("sniper_auto_sl_pct", 40.0)
        tp_xs   = ud.get("sniper_auto_tp_x", [2.0, 5.0])
        tp_str  = " + ".join(str(x) + "x" for x in tp_xs)
        await q.edit_message_text(
            "🤖 *AUTO SNIPER MODE*\n\n"
            "AI analyzes every token. If it says SNIPE, the bot buys automatically,\n"
            "sets stop loss and take profit, and exits on dump detection.\n\n"
            "Status: *" + ("🟢 ON" if auto_on else "🔴 OFF") + "*\n"
            "Notifications: *" + ("ON 🔔" if notify else "OFF 🔕") + "*\n"
            "Auto Stop Loss: *" + ("ON — " + str(sl_pct) + "%" if sl_on else "OFF") + "*\n"
            "Auto Take Profit: *" + ("ON — " + tp_str if tp_on else "OFF") + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(("🔴 Disable Auto" if auto_on else "🟢 Enable Auto"), callback_data="sniper_auto_toggle")],
                [InlineKeyboardButton(("🔕 Mute Notifications" if notify else "🔔 Unmute Notifications"), callback_data="sniper_auto_notif")],
                [InlineKeyboardButton(("✅ Stop Loss ON" if sl_on else "❌ Stop Loss OFF"), callback_data="sniper_sl_toggle"),
                 InlineKeyboardButton("⚙️ SL %", callback_data="sniper_sl_pct_cfg")],
                [InlineKeyboardButton(("✅ Take Profit ON" if tp_on else "❌ Take Profit OFF"), callback_data="sniper_tp_toggle"),
                 InlineKeyboardButton("⚙️ TP Targets", callback_data="sniper_tp_cfg")],
                [InlineKeyboardButton("◀ Back", callback_data="v_sniper")],
            ])
        )

    elif cb == "sniper_adv_notif":
        ud["sniper_adv_notify"] = not ud.get("sniper_adv_notify", True)
        adv_on = ud.get("sniper_advisory", False)
        notify = ud["sniper_adv_notify"]
        await q.edit_message_text(
            "🧠 *AI ADVISORY MODE*\n\n"
            "AI analyzes each token and sends you a full report with verdict, thesis,\n"
            "red flags, green flags and a suggested entry amount.\n"
            "You confirm or skip — full control stays with you.\n\n"
            "Status: *" + ("🟢 ON" if adv_on else "🔴 OFF") + "*\n"
            "Notifications: *" + ("ON 🔔" if notify else "OFF 🔕") + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(("🔴 Disable Advisory" if adv_on else "🟢 Enable Advisory"), callback_data="sniper_adv_toggle")],
                [InlineKeyboardButton(("📡 Switch to Channel Mode" if notify else "🔔 Switch to DM Mode"), callback_data="sniper_adv_notif")],
                [InlineKeyboardButton("◀ Back", callback_data="v_sniper")],
            ])
        )

    elif cb == "sniper_channel_setup":
        # Ask user to paste their channel/group ID
        pending[u.id] = {"action": "sniper_channel_input"}
        await q.edit_message_text(
            "📡 *SET BROADCAST CHANNEL*\n\n"
            "The bot will post full AI signal cards to your channel or group.\n\n"
            "*How to get your channel/group ID:*\n"
            "1️⃣ Add @userinfobot to your channel/group\n"
            "2️⃣ It will reply with the ID (e.g. `-1001234567890`)\n"
            "3️⃣ Also make sure *apex_sniper_bot* is an admin in the channel/group\n\n"
            "Then paste the ID below 👇\n\n"
            "_Example: -1001234567890_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Cancel", callback_data="sniper_adv_menu")]
            ])
        )

    elif cb == "sniper_channel_remove":
        ud["sniper_broadcast_channel"] = None
        ud["sniper_broadcast_name"] = ""
        ch_id  = None
        adv_on = ud.get("sniper_advisory", False)
        notify = ud.get("sniper_adv_notify", True)
        await q.edit_message_text(
            "🧠 *AI ADVISORY MODE*\n\n"
            "AI analyzes each token and sends a compact notification to your DM.\n"
            "Tap 👁 View Analysis to see the full report.\n"
            "You confirm or skip — full control stays with you.\n\n"
            "Status: *" + ("🟢 ON" if adv_on else "🔴 OFF") + "*\n"
            "DM Notifications: *" + ("ON 🔔" if notify else "OFF 🔕") + "*\n"
            "📡 Broadcast: *Removed ✅*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(("🔴 Disable Advisory" if adv_on else "🟢 Enable Advisory"), callback_data="sniper_adv_toggle")],
                [InlineKeyboardButton(("🔕 Mute DM Notifs" if notify else "🔔 Unmute DM Notifs"), callback_data="sniper_adv_notif")],
                [InlineKeyboardButton("📡 Set Broadcast Channel", callback_data="sniper_channel_setup")],
                [InlineKeyboardButton("◀ Back", callback_data="v_sniper")],
            ])
        )

    elif cb == "sniper_narrative_hist":
        # Show last 20 narrative detections from _narrative_history
        import time as _nht
        _nh = _narrative_history[-20:] if _narrative_history else []
        if not _nh:
            _nh_text = (
                "\U0001f4a1 *NARRATIVE HISTORY*\n\n"
                "No narratives detected yet.\n\n"
                "Narratives fire when 5+ tokens share the same keyword in a single scan cycle.\n"
                "They appear here silently \u2014 no more DM spam."
            )
        else:
            _now_nh = _nht.time()
            _lines = []
            for _n in reversed(_nh):
                _age_m = int((_now_nh - _n["ts"]) / 60)
                _age_str = f"{_age_m}m ago" if _age_m < 60 else f"{_age_m//60}h ago"
                _lines.append(f"\u2022 *{_n['kw'].upper()}* \u2014 {_n['count']} tokens \u2014 _{_age_str}_")
            _nh_text = (
                "\U0001f4a1 *NARRATIVE HISTORY*\n\n"
                "Recent keyword clusters detected across scan cycles.\n"
                "Score threshold is lowered 5pts for matching tokens.\n\n"
                + "\n".join(_lines)
            )
        await q.edit_message_text(
            _nh_text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25c0 Back", callback_data="v_sniper")
            ]])
        )

    elif cb == "sniper_log_ch_menu":
        _lch    = ud.get("sniper_log_channel")
        _lname  = ud.get("sniper_log_channel_name", "")
        _lon    = ud.get("sniper_log_channel_on", True)
        _lline  = ("\U0001f4e1 Channel: *" + _lname + "*  " + ("\U0001f7e2 ON" if _lon else "\U0001f534 OFF")) if _lch else "\U0001f4e1 Channel: *Not set*"
        await q.edit_message_text(
            "\U0001f4ca *SCAN LOG CHANNEL*\n\n"
            "Every token the sniper evaluates gets posted here — skips AND passes.\n\n"
            "SKIPs stream silently (no notification ping).\n"
            "SNIPEs ping with notification so you never miss a real signal.\n\n"
            "Format:\n"
            "\U0001f534 SKIP  $TOKEN  SOL  42/100  $67K\n"
            "\u2514 Hard flag: Bundle sniped\n\n"
            "\U0001f7e2 SNIPE  $TOKEN  SOL  73/100  $28K  conf:8/10\n"
            "\u2514 Thesis preview...\n\n"
            "\u26a0\ufe0fRC badge shows when RugCheck was rate-limited.\n\n"
            + _lline,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(
                        ("\U0001f534 Disable Log" if _lon else "\U0001f7e2 Enable Log"),
                        callback_data="sniper_log_ch_toggle"
                    )] if _lch else [],
                    [InlineKeyboardButton(
                        ("\U0001f4e1 Change Channel" if _lch else "\U0001f4e1 Set Log Channel"),
                        callback_data="sniper_log_ch_setup"
                    )],
                ] + (
                    [[InlineKeyboardButton("\U0001f5d1 Remove Channel", callback_data="sniper_log_ch_remove")]]
                    if _lch else []
                ) + [
                    [InlineKeyboardButton("\u25c0 Back", callback_data="v_sniper")],
                ]
            )
        )

    elif cb == "sniper_log_ch_setup":
        pending[u.id] = {"action": "sniper_log_ch_input"}
        await q.edit_message_text(
            "\U0001f4ca *SET SCAN LOG CHANNEL*\n\n"
            "The bot will post every scan result (skips + passes) to this channel.\n\n"
            "*How to get your channel/group ID:*\n"
            "1. Add @userinfobot to your channel/group\n"
            "2. It will reply with the ID (e.g. `-1001234567890`)\n"
            "3. Make sure the bot is an admin in the channel\n\n"
            "Then paste the ID below:\n\n"
            "_Example: -1001234567890_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\u25c0 Cancel", callback_data="sniper_log_ch_menu")]
            ])
        )

    elif cb == "sniper_log_ch_toggle":
        ud["sniper_log_channel_on"] = not ud.get("sniper_log_channel_on", True)
        _lon   = ud["sniper_log_channel_on"]
        _lname = ud.get("sniper_log_channel_name", "")
        await q.edit_message_text(
            "\U0001f4ca Scan Log Channel *" + ("enabled \u2705" if _lon else "paused \u23f8") + "*\n\n"
            + ("Scan results will stream to *" + _lname + "*" if _lon
               else "Channel is paused \u2014 no messages will be sent."),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25c0 Back", callback_data="sniper_log_ch_menu")
            ]])
        )

    elif cb == "sniper_log_ch_remove":
        ud["sniper_log_channel"]      = None
        ud["sniper_log_channel_name"] = ""
        ud["sniper_log_channel_on"]   = True
        await q.edit_message_text(
            "\U0001f4ca *SCAN LOG CHANNEL*\n\n"
            "Channel removed \u2705\n\n"
            "Scan results will no longer be posted anywhere.\n"
            "The in-bot sniper log still keeps the last 200 entries.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f4e1 Set New Channel", callback_data="sniper_log_ch_setup")],
                [InlineKeyboardButton("\u25c0 Back", callback_data="v_sniper")],
            ])
        )

    elif cb == "sniper_sl_toggle":
        ud["sniper_auto_sl"] = not ud.get("sniper_auto_sl", True)
        auto_on = ud.get("sniper_auto", False)
        notify  = ud.get("sniper_auto_notify", True)
        sl_on   = ud["sniper_auto_sl"]
        tp_on   = ud.get("sniper_auto_tp", True)
        sl_pct  = ud.get("sniper_auto_sl_pct", 40.0)
        tp_xs   = ud.get("sniper_auto_tp_x", [2.0, 5.0])
        tp_str  = " + ".join(str(x) + "x" for x in tp_xs)
        await q.edit_message_text(
            "🤖 *AUTO SNIPER MODE*\n\n"
            "AI analyzes every token. If it says SNIPE, the bot buys automatically,\n"
            "sets stop loss and take profit, and exits on dump detection.\n\n"
            "Status: *" + ("🟢 ON" if auto_on else "🔴 OFF") + "*\n"
            "Notifications: *" + ("ON 🔔" if notify else "OFF 🔕") + "*\n"
            "Auto Stop Loss: *" + ("ON — " + str(sl_pct) + "%" if sl_on else "OFF") + "*\n"
            "Auto Take Profit: *" + ("ON — " + tp_str if tp_on else "OFF") + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(("🔴 Disable Auto" if auto_on else "🟢 Enable Auto"), callback_data="sniper_auto_toggle")],
                [InlineKeyboardButton(("🔕 Mute Notifications" if notify else "🔔 Unmute Notifications"), callback_data="sniper_auto_notif")],
                [InlineKeyboardButton(("✅ Stop Loss ON" if sl_on else "❌ Stop Loss OFF"), callback_data="sniper_sl_toggle"),
                 InlineKeyboardButton("⚙️ SL %", callback_data="sniper_sl_pct_cfg")],
                [InlineKeyboardButton(("✅ Take Profit ON" if tp_on else "❌ Take Profit OFF"), callback_data="sniper_tp_toggle"),
                 InlineKeyboardButton("⚙️ TP Targets", callback_data="sniper_tp_cfg")],
                [InlineKeyboardButton("◀ Back", callback_data="v_sniper")],
            ])
        )

    elif cb == "sniper_tp_toggle":
        ud["sniper_auto_tp"] = not ud.get("sniper_auto_tp", True)
        auto_on = ud.get("sniper_auto", False)
        notify  = ud.get("sniper_auto_notify", True)
        sl_on   = ud.get("sniper_auto_sl", True)
        tp_on   = ud["sniper_auto_tp"]
        sl_pct  = ud.get("sniper_auto_sl_pct", 40.0)
        tp_xs   = ud.get("sniper_auto_tp_x", [2.0, 5.0])
        tp_str  = " + ".join(str(x) + "x" for x in tp_xs)
        await q.edit_message_text(
            "🤖 *AUTO SNIPER MODE*\n\n"
            "AI analyzes every token. If it says SNIPE, the bot buys automatically,\n"
            "sets stop loss and take profit, and exits on dump detection.\n\n"
            "Status: *" + ("🟢 ON" if auto_on else "🔴 OFF") + "*\n"
            "Notifications: *" + ("ON 🔔" if notify else "OFF 🔕") + "*\n"
            "Auto Stop Loss: *" + ("ON — " + str(sl_pct) + "%" if sl_on else "OFF") + "*\n"
            "Auto Take Profit: *" + ("ON — " + tp_str if tp_on else "OFF") + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(("🔴 Disable Auto" if auto_on else "🟢 Enable Auto"), callback_data="sniper_auto_toggle")],
                [InlineKeyboardButton(("🔕 Mute Notifications" if notify else "🔔 Unmute Notifications"), callback_data="sniper_auto_notif")],
                [InlineKeyboardButton(("✅ Stop Loss ON" if sl_on else "❌ Stop Loss OFF"), callback_data="sniper_sl_toggle"),
                 InlineKeyboardButton("⚙️ SL %", callback_data="sniper_sl_pct_cfg")],
                [InlineKeyboardButton(("✅ Take Profit ON" if tp_on else "❌ Take Profit OFF"), callback_data="sniper_tp_toggle"),
                 InlineKeyboardButton("⚙️ TP Targets", callback_data="sniper_tp_cfg")],
                [InlineKeyboardButton("◀ Back", callback_data="v_sniper")],
            ])
        )

    elif cb == "sniper_sl_pct_cfg":
        pending[u.id] = {"action": "sniper_sl_pct"}
        await q.edit_message_text(
            "🛑 Enter auto stop loss %\nExample: 35\n\nNote: AI tightens this to 20% if rug risk is HIGH.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_auto_menu")]])
        )

    elif cb == "sniper_tp_cfg":
        pending[u.id] = {"action": "sniper_tp_x"}
        await q.edit_message_text(
            "🎯 Enter take profit targets as X multiples:\nFormat: x1 x2 x3\nExample: 2 5 10\n\n"
            "Bot sells equal portions at each target.\n2 5 = 50% at 2x, 50% at 5x\n2 5 10 = 33% at each",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_auto_menu")]])
        )

    elif cb == "sniper_cfg_buys_h1":
        pending[u.id] = {"action": "sniper_buys_h1", "_prompt_msg_id": None}
        m = await q.edit_message_text(
            "📊 *MIN BUYS PER HOUR*\n\n"
            "Minimum number of buy transactions in the last hour.\n"
            "Current: *" + str(ud.get("sniper_filters", {}).get("min_buys_h1", 30)) + "*\n\n"
            "Enter a number (e.g. 20):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Cancel", callback_data="sniper_filters_menu")]]),
        )
        pending[u.id]["_prompt_msg_id"] = m.message_id

    elif cb == "sniper_cfg_buy_pct":
        pending[u.id] = {"action": "sniper_buy_pct", "_prompt_msg_id": None}
        m = await q.edit_message_text(
            "📉 *MIN BUY PRESSURE %*\n\n"
            "Minimum % of transactions that must be buys (H1 window).\n"
            "Current: *" + str(ud.get("sniper_filters", {}).get("min_buy_pct", 52)) + "%*\n\n"
            "Enter a number 40-80 (e.g. 55):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Cancel", callback_data="sniper_filters_menu")]]),
        )
        pending[u.id]["_prompt_msg_id"] = m.message_id

    elif cb == "sniper_cfg_vol_mc":
        pending[u.id] = {"action": "sniper_vol_mc", "_prompt_msg_id": None}
        m = await q.edit_message_text(
            "🚿 *VOL/MC RATIO CAP*\n\n"
            "Max allowed Volume/MC ratio. Above this = wash trading.\n"
            "Current: *" + str(ud.get("sniper_filters", {}).get("max_vol_mc_ratio", 10.0)) + "x*\n\n"
            "Enter a number (e.g. 6.0):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Cancel", callback_data="sniper_filters_menu")]]),
        )
        pending[u.id]["_prompt_msg_id"] = m.message_id

    elif cb == "sniper_budget_cfg":
        pending[u.id] = {"action": "sniper_budget", "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "💰 Enter daily sniper budget in USD:\nExample: 300\n\nSniper stops buying once this is spent in a day.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="v_sniper")]])
        )

    elif cb == "sniper_cfg_score":
        pending[u.id] = {"action": "sniper_score", "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "📊 Enter minimum sniper score (0–100):\nExample: 45\n\nTokens below this score are skipped.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_filters_menu")]])
        )

    elif cb == "sniper_cfg_liq":
        pending[u.id] = {"action": "sniper_liq", "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "💧 Enter minimum liquidity in USD:\nExample: 15000",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_filters_menu")]])
        )

    elif cb == "sniper_cfg_mc":
        pending[u.id] = {"action": "sniper_mc", "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "📈 Enter MC range:\nFormat: min max\nExample: 20000 1000000",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_filters_menu")]])
        )

    elif cb == "sniper_cfg_age":
        pending[u.id] = {"action": "sniper_age", "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "⏰ Enter max token age in hours:\nExample: 6\n\nRecommended: 3–6h for fresh launches.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_filters_menu")]])
        )

    elif cb == "sniper_cfg_amt":
        pending[u.id] = {"action": "sniper_amt", "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "💵 Enter buy amount per snipe in USD:\nExample: 100",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="sniper_filters_menu")]])
        )

    elif cb == "sniper_chains_menu":
        chains = ud.get("sniper_chains", {
            "solana": True, "ethereum": True, "base": True, "bsc": True, "arbitrum": True
        })
        chain_icons = {"solana":"🟣","ethereum":"🔷","base":"🔵","bsc":"🟡","arbitrum":"🔶"}
        buttons = []
        for chain, enabled in chains.items():
            icon  = chain_icons.get(chain, "⚪")
            label = icon + " " + chain.upper()[:3] + " " + ("✅" if enabled else "❌")
            buttons.append([InlineKeyboardButton(label, callback_data="sniper_chain_" + chain)])
        buttons.append([InlineKeyboardButton("◀ Back", callback_data="v_sniper")])
        await q.edit_message_text(
            "⛓️ *CHAIN SELECTOR*\n\nToggle each chain on/off for the sniper.\nOnly tokens from active chains will be analyzed.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif cb.startswith("sniper_chain_"):
        chain  = cb[13:]
        chains = ud.setdefault("sniper_chains", {
            "solana": True, "ethereum": True, "base": True, "bsc": True, "arbitrum": True
        })
        chains[chain] = not chains.get(chain, True)
        # Refresh chain selector in-place
        chain_icons = {"solana":"🟣","ethereum":"🔷","base":"🔵","bsc":"🟡","arbitrum":"🔶"}
        buttons = []
        for c, enabled in chains.items():
            icon  = chain_icons.get(c, "⚪")
            label = icon + " " + c.upper()[:3] + " " + ("✅" if enabled else "❌")
            buttons.append([InlineKeyboardButton(label, callback_data="sniper_chain_" + c)])
        buttons.append([InlineKeyboardButton("◀ Back", callback_data="v_sniper")])
        await q.edit_message_text(
            "⛓️ *CHAIN SELECTOR*\n\nToggle each chain on/off for the sniper.\nOnly tokens from active chains will be analyzed.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif cb == "sniper_filters_menu":
        sf = ud.get("sniper_filters", {})
        skip_counts = ud.get("sniper_skip_counts", {})
        _skip_icons = {"hard_flag":"🚩","score":"📉","liquidity":"💧","mc_range":"📊",
                       "age":"⏰","low_activity":"😴","sell_pressure":"📛",
                       "wash_trade":"🔄","no_socials":"👻","few_holders":"👥","other":"❓"}
        _skip_labels = {"hard_flag":"Hard flag","score":"Score","liquidity":"Liq",
                        "mc_range":"MC range","age":"Too old","low_activity":"Activity",
                        "sell_pressure":"Buy%","wash_trade":"Wash trade",
                        "no_socials":"No socials","few_holders":"Holders","other":"Other"}
        if skip_counts:
            top = sorted(skip_counts.items(), key=lambda x: -x[1])[:5]
            skip_txt = "\n\n🔍 *Why tokens are being skipped:*\n" + "\n".join(
                "  " + _skip_icons.get(k,"❓") + " " + _skip_labels.get(k,k) + ": *" + str(v) + "*"
                for k, v in top
            )
        else:
            skip_txt = ""
        await q.edit_message_text(
            "⚙️ *SNIPER FILTERS*\n\n"
            "Tokens must pass ALL filters before AI analyzes them.\n"
            "_No socials = −10 score penalty (not a hard skip)_\n\n"
            "Min Score: *"    + str(sf.get("min_score",   35))         + "/100*\n"
            "Min Liq: *"      + money(sf.get("min_liq",        5_000))  + "*\n"
            "MC Range: *"     + mc_str(sf.get("min_mc",        10_000)) + "* → *" + mc_str(sf.get("max_mc", 500_000)) + "*\n"
            "Max Age: *"      + str(sf.get("max_age_h",  6.0))          + "h*\n"
            "Min Buys/1h: *"  + str(sf.get("min_buys_h1", 10))         + "*\n"
            "Min Buy%: *"     + str(sf.get("min_buy_pct",   45))        + "%*\n"
            "Vol/MC Cap: *"   + str(sf.get("max_vol_mc_ratio", 10.0))   + "x*\n"
            "Buy Amount: *"   + money(sf.get("buy_amount", 20))         + "*"
            + skip_txt,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Min Score",     callback_data="sniper_cfg_score"),
                 InlineKeyboardButton("💧 Min Liq",       callback_data="sniper_cfg_liq")],
                [InlineKeyboardButton("📈 MC Range",      callback_data="sniper_cfg_mc"),
                 InlineKeyboardButton("⏰ Max Age",       callback_data="sniper_cfg_age")],
                [InlineKeyboardButton("📊 Min Buys/1h",   callback_data="sniper_cfg_buys_h1"),
                 InlineKeyboardButton("📉 Min Buy%",      callback_data="sniper_cfg_buy_pct")],
                [InlineKeyboardButton("🚿 Vol/MC Cap",    callback_data="sniper_cfg_vol_mc"),
                 InlineKeyboardButton("💵 Buy Amount",    callback_data="sniper_cfg_amt")],
                [InlineKeyboardButton("🔄 Reset to Recommended", callback_data="sniper_filters_reset")],
                [InlineKeyboardButton("◀ Back",           callback_data="v_sniper")],
            ])
        )

    elif cb.startswith("apex_auto_buy_"):
        # ── AUTOMATE: buy this token from vault and hand it to APEX engine ────
        contract = cb[14:]
        vault    = ud.get("apex_vault", 0.0)
        if vault < 1.0:
            await q.edit_message_text(
                "🤖 *AUTOMATE*\n\n"
                "Your APEX vault is empty.\n\n"
                "Fund it first: *APEX → Vault → Fund Vault*\n\n"
                "_Once funded, AUTOMATE buys from the vault and APEX manages\n"
                "the exit automatically (trailing stop, threat detection, etc.)._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏦 Fund Vault", callback_data="apex_vault_fund")],
                    [InlineKeyboardButton("◀ Back",        callback_data="btt_" + contract)],
                ])
            )
            return
        # Show amount picker — reuse sniper buy amounts capped to vault
        _sf_amt = float(ud.get("sniper_filters", {}).get("buy_amount", 20.0))
        _options = [20, 50, 100, 200]
        _btn_rows_auto = []
        _row_auto = []
        for _opt in _options:
            if _opt <= vault:
                _row_auto.append(InlineKeyboardButton(
                    "$" + str(_opt), callback_data="apex_auto_amt_" + str(_opt) + "_" + contract
                ))
        if _row_auto:
            _btn_rows_auto.append(_row_auto)
        _btn_rows_auto.append([
            InlineKeyboardButton("✏️ Custom", callback_data="apex_auto_custom_" + contract),
        ])
        _btn_rows_auto.append([InlineKeyboardButton("◀ Back", callback_data="btt_" + contract)])
        await q.edit_message_text(
            "🤖 *AUTOMATE*\n\n"
            "Buy this token from your APEX vault and let APEX manage the exit.\n\n"
            "🏦 Vault available: *" + money(vault) + "*\n\n"
            "APEX will apply:\n"
            "  • Stop loss (rug-risk tier)\n"
            "  • Trailing stop (activates at 1.5x)\n"
            "  • Threat detection (RED/ORANGE alerts)\n"
            "  • Profit split to main balance on exit\n\n"
            "Choose buy amount:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_btn_rows_auto)
        )

    elif cb.startswith("apex_auto_amt_"):
        # ── Execute AUTOMATE buy ───────────────────────────────────────────────
        rest     = cb[14:]
        amt_str, contract = rest.split("_", 1)
        buy_amt  = float(amt_str)
        vault    = ud.get("apex_vault", 0.0)
        if vault < buy_amt:
            await q.edit_message_text(
                "❌ Insufficient vault balance (" + money(vault) + ").",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀ Back", callback_data="btt_" + contract)
                ]])
            )
            return
        # Execute buy from vault with APEX mood
        result = await do_buy_core(ud, u.id, contract, buy_amt, planned=True,
                                   mood="APEX", vault_buy=True)
        if isinstance(result, str):
            await q.edit_message_text("❌ " + result, reply_markup=main_menu_kb())
            return
        info_post, _ = result
        h = ud["holdings"].get(contract, {})
        if not h:
            await q.edit_message_text("❌ Buy executed but position not found.", reply_markup=main_menu_kb())
            return
        # ── Set up APEX position fields ───────────────────────────────────────
        _rug_map = {"LOW": 22.0, "MEDIUM": 18.0, "HIGH": 12.0}
        # Try to get rug risk from sniper analysis cache, fallback to MEDIUM
        _cached_ai  = _sniper_analysis_cache.get(u.id, {}).get(contract, {})
        _rug_risk   = _cached_ai.get("ai", {}).get("rug_risk", "MEDIUM") if _cached_ai else "MEDIUM"
        _sl_pct     = _rug_map.get(_rug_risk, 18.0)
        import time as _aat
        h["stop_loss_pct"]        = _sl_pct
        h["apex_peak_price"]      = info_post["price"]
        h["apex_trail_stop"]      = None
        h["apex_trail_pct"]       = APEX_TRAIL_PCT_EARLY
        h["apex_threat"]          = "CLEAR"
        h["apex_vault_locked"]    = {}
        h["apex_profile_at_entry"]= "default"
        h["apex_hunter_floor"]    = 0.0
        h["apex_entry_score"]     = _cached_ai.get("sc", {}).get("score", 0) if _cached_ai else 0
        h["apex_entry_conf"]      = _cached_ai.get("ai", {}).get("confidence", 0) if _cached_ai else 0
        h["liq_at_buy"]           = info_post.get("liq", 0)
        h["pair_addr"]            = info_post.get("pair_addr", "")
        h["apex_entry_rug"]       = _rug_risk
        h["apex_entry_age_h"]     = info_post.get("age_h") or 0
        h["apex_entry_buy_pct"]   = info_post.get("buy_pct_m5", info_post.get("buy_pct", 50))
        h["apex_entry_pos_count"] = apex_count_positions(ud)
        h["apex_token_type"]      = "organic"
        h["sr_history"]           = []
        h["sr_peak_vol"]          = 0.0
        h["apex_dca_count"]       = 0
        h["apex_last_dca_ts"]     = 0.0
        h["apex_ladder_sold_1_3x"]= False
        h["apex_ladder_sold_2x"]  = False
        h.setdefault("stop_loss_history", []).append({
            "old": None, "new": _sl_pct, "source": "automate", "cx": 1.0, "ts": _aat.time()
        })
        save_user(u.id, ud)
        split_pct = ud.get("apex_vault_profit_split", 0.50)
        await q.edit_message_text(
            "🤖 *AUTOMATE ACTIVE*\n\n"
            "*$" + _md(info_post["symbol"]) + "*  " + info_post.get("chain", "").upper() + "\n"
            "Bought: *" + money(buy_amt) + "* from vault\n"
            "Entry MC: *" + mc_str(info_post["mc"]) + "*\n"
            "Rug risk: *" + _rug_risk + "*  |  SL: *" + str(_sl_pct) + "%*\n\n"
            "🏦 Vault: *" + money(ud.get("apex_vault", 0)) + "*\n\n"
            "APEX is now managing this position.\n"
            "_Trail activates at 1.5x. " + str(int(split_pct*100)) + "% of profit → main balance on exit._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 View Position", callback_data="btt_" + contract)],
                [InlineKeyboardButton("🏠 Main Menu",     callback_data="mm")],
            ])
        )

    elif cb.startswith("apex_auto_custom_"):
        contract = cb[17:]
        pending[u.id] = {"action": "apex_auto_custom_amt", "contract": contract,
                         "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "✏️ Enter custom AUTOMATE amount in USD:\n\n"
            "Vault: *" + money(ud.get("apex_vault", 0)) + "*\n\n"
            "Example: 75",
            parse_mode="Markdown",
            reply_markup=cancel_kb()
        )

    elif cb.startswith("tc_"):
        # KOL alert "Trade on APEX Sniper" button — show token card
        contract = cb[3:]
        if contract:
            info = await get_token(contract)
            if info:
                sc = score_token(info)
                await send_token_card(q, info, contract, ud, sc, ctx, is_query=True)
            else:
                await q.edit_message_text("Token unavailable.", reply_markup=back_main())
        else:
            await q.answer("Invalid token address.")

    elif cb == "sniper_filters_reset":
        ud["sniper_filters"] = {
            "min_score":        35,
            "min_liq":          5_000,
            "min_mc":           10_000,
            "max_mc":           100_000,   # tightened from 500K — APEX focuses on $20K-$100K for 5-10x
            "max_age_h":        6.0,
            "buy_amount":       20,
            "min_buys_h1":      10,
            "min_buy_pct":      45,
            "max_vol_mc_ratio": 10.0,
            "min_liq_pct":      3,
            "max_top10_pct":    28,
            "min_lp_burn":      50,
        }
        ud["sniper_skip_counts"] = {}   # reset skip counters too
        save_user(u.id, ud)
        await q.answer("✅ Filters reset to recommended defaults!", show_alert=True)
        # Re-render filters menu
        sf = ud["sniper_filters"]
        await q.edit_message_text(
            "⚙️ *SNIPER FILTERS — RESET DONE*\n\n"
            "Filters restored to recommended defaults.\n"
            "_No socials = −10 score penalty (not a hard skip)_\n\n"
            "Min Score: *35/100*\n"
            "Min Liq: *$5,000*\n"
            "MC Range: *$10K* → *$500K*\n"
            "Max Age: *6.0h*\n"
            "Min Buys/1h: *10*\n"
            "Min Buy%: *45%*\n"
            "Vol/MC Cap: *10.0x*\n"
            "Buy Amount: *$20*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Min Score",     callback_data="sniper_cfg_score"),
                 InlineKeyboardButton("💧 Min Liq",       callback_data="sniper_cfg_liq")],
                [InlineKeyboardButton("📈 MC Range",      callback_data="sniper_cfg_mc"),
                 InlineKeyboardButton("⏰ Max Age",       callback_data="sniper_cfg_age")],
                [InlineKeyboardButton("📊 Min Buys/1h",   callback_data="sniper_cfg_buys_h1"),
                 InlineKeyboardButton("📉 Min Buy%",      callback_data="sniper_cfg_buy_pct")],
                [InlineKeyboardButton("🚿 Vol/MC Cap",    callback_data="sniper_cfg_vol_mc"),
                 InlineKeyboardButton("💵 Buy Amount",    callback_data="sniper_cfg_amt")],
                [InlineKeyboardButton("🔄 Reset to Recommended", callback_data="sniper_filters_reset")],
                [InlineKeyboardButton("◀ Back",           callback_data="v_sniper")],
            ])
        )

    elif cb == "sniper_log_view":
        log = ud.get("sniper_log", [])
        if not log:
            await q.edit_message_text("📋 *SNIPER LOG*\n\nNo activity yet.", parse_mode="Markdown", reply_markup=back_main())
            return
        bought    = [e for e in log if e.get("bought")]
        skipped   = [e for e in log if not e.get("bought")]
        sniper_trades = [tr for tr in trade_log.get(u.id, []) if tr.get("mood") in ("AI-Sniper","Sniper")]
        b_wr = 0
        if sniper_trades:
            s_wins = [tr for tr in sniper_trades if tr["realized_pnl"] > 0]
            b_wr   = round(len(s_wins) / len(sniper_trades) * 100)

        # Show newest 10 first
        recent = list(reversed(log))[:10]
        verdict_emoji = {"SNIPE":"🟢","SKIP":"🔴","WAIT":"🟡"}
        buttons = []
        for i, e in enumerate(recent):
            ve     = verdict_emoji.get(e.get("verdict","?"), "⚪")
            bought_tag  = " 💵" if e.get("bought") else ""
            rc_tag      = " ⚠️RC" if e.get("rc_rate_limited") else ""
            conf   = e.get("confidence", 0)
            label  = ve + " $" + e.get("symbol","?") + "  conf:" + str(conf) + "/10  " + e.get("chain","?").upper()[:3] + bought_tag + rc_tag
            # Index in the full log (from end)
            full_idx = len(log) - 1 - list(reversed(log)).index(e)
            buttons.append([InlineKeyboardButton(label, callback_data="snp_log_detail_" + str(full_idx))])

        buttons.append([
            InlineKeyboardButton("🗑 Clear Log",    callback_data="sniper_log_clear"),
            InlineKeyboardButton("🔄 Reset Memory", callback_data="sniper_reset_memory"),
        ])
        buttons.append([InlineKeyboardButton("◀ Back", callback_data="v_sniper")])

        skip_counts = ud.get("sniper_skip_counts", {})
        _skip_icons = {"hard_flag":"🚩","score":"📉","liquidity":"💧","mc_range":"📊",
                       "age":"⏰","low_activity":"😴","sell_pressure":"📛",
                       "wash_trade":"🔄","no_socials":"👻","few_holders":"👥","other":"❓"}
        _skip_labels_short = {"hard_flag":"Flag","score":"Score","liquidity":"Liq",
                              "mc_range":"MC","age":"Age","low_activity":"Activity",
                              "sell_pressure":"Buy%","wash_trade":"Wash",
                              "no_socials":"Socials","few_holders":"Holders","other":"Other"}
        if skip_counts:
            top_skips = sorted(skip_counts.items(), key=lambda x: -x[1])[:5]
            skip_breakdown = "\n🔍 *Skip reasons:*  " + "  ".join(
                _skip_icons.get(k,"❓") + _skip_labels_short.get(k,k) + ":" + str(v)
                for k, v in top_skips
            ) + "\n"
        else:
            skip_breakdown = ""

        await q.edit_message_text(
            "📋 *SNIPER LOG*\n\n"
            "Analyzed: *" + str(len(log)) + "*  |  Bought: *" + str(len(bought)) + "*  |  Skipped: *" + str(len(skipped)) + "*\n"
            "Sniper Win Rate: *" + str(b_wr) + "%*\n"
            + skip_breakdown +
            "\nTap any token for full AI breakdown 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif cb.startswith("snp_log_detail_"):
        idx = int(cb[15:])
        log = ud.get("sniper_log", [])
        if idx < 0 or idx >= len(log):
            await q.edit_message_text("Entry not found.", reply_markup=back_main())
            return
        e = log[idx]
        verdict_emoji = {"SNIPE":"🟢","SKIP":"🔴","WAIT":"🟡"}.get(e.get("verdict","?"),"⚪")
        rug_emoji     = {"LOW":"✅","MEDIUM":"⚠️","HIGH":"🚨","UNKNOWN":"❓"}.get(e.get("rug_risk","?"),"❓")
        mom_emoji     = {"STRONG":"🚀","MODERATE":"📈","WEAK":"📉","NEGATIVE":"💀","UNKNOWN":"❓"}.get(e.get("momentum","?"),"❓")
        soc_emoji     = {"GOOD":"✅","PARTIAL":"⚠️","NONE":"🚨","UNKNOWN":"❓"}.get(e.get("social","?"),"❓")
        conf          = e.get("confidence", 0)
        conf_bar      = "█" * conf + "░" * (10 - conf)
        red_flags     = "\n".join("  🚨 " + f for f in e.get("red_flags", [])) or "  None"
        green_flags   = "\n".join("  ✅ " + f for f in e.get("green_flags", [])) or "  None"
        hard_flags    = "\n".join("  🚨 " + f for f in e.get("hard_flags", [])) or ""
        ts            = e.get("timestamp","")[:16].replace("T"," ")
        bought_line   = "\n💵 *BOUGHT: " + money(e.get("amount", 0)) + "*" if e.get("bought") else ""
        skip_stage    = e.get("skip_stage", "")
        stage_line    = "\n⛔ *Filtered at:* " + skip_stage if skip_stage else ""
        rc_limit_line = "\n⚠️ *RugCheck rate limited* — security data was unavailable at scan time" if e.get("rc_rate_limited") else ""

        hard_flags_block = ("\n🚨 *Hard Flags:*\n" + hard_flags + "\n") if hard_flags else ""

        txt = (
            "🔍 *AI SNIPER DETAIL*\n"
            "━━━━━━━━━━━━━━━━\n"
            "*$" + e.get("symbol","?") + "*  " + e.get("chain","?").upper() + "  " + ts + "\n"
            "MC: *" + mc_str(e.get("mc",0)) + "*  |  Liq: *" + money(e.get("liq",0)) + "*\n"
            "🧠 Sniper Score: *" + str(e.get("score",0)) + "/100*" + stage_line + rc_limit_line + "\n\n"
            + verdict_emoji + " *Verdict: " + e.get("verdict","?") + "*" + bought_line + "\n"
            "Confidence: *" + str(conf) + "/10*  `" + conf_bar + "`\n\n"
            "📝 *Why:*\n" + (e.get("thesis","No analysis available.") or "No analysis available.") + "\n\n"
            "━━━━━━━━━━━━━━━━\n"
            + hard_flags_block
            + rug_emoji + " Rug Risk: *" + e.get("rug_risk","?") + "*\n"
            + mom_emoji + " Momentum: *" + e.get("momentum","?") + "*\n"
            + soc_emoji + " Socials: *" + e.get("social","?") + "*\n\n"
            "🚩 *Red Flags:*\n" + red_flags + "\n\n"
            "💚 *Green Flags:*\n" + green_flags
        )
        contract = e.get("contract","")
        kb_rows = []
        if contract:
            kb_rows.append([InlineKeyboardButton("🔎 View Token Live", callback_data="btt_" + contract)])
        kb_rows.append([InlineKeyboardButton("◀ Back to Log", callback_data="sniper_log_view")])
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb_rows))


    # ── KOL / SMART WALLET TRACKER UI ─────────────────────────────────────────
    elif cb == "kol_menu":
        wallets    = ud.get("kol_wallets", [])
        alerts_on  = ud.get("kol_alerts_on", True)
        helius_set = bool(os.environ.get("HELIUS_API_KEY", ""))
        helius_line = "✅ Helius connected" if helius_set else "⚠️ *HELIUS_API_KEY not set* — add it in Railway Variables"

        wallet_lines = ""
        if wallets:
            wallet_lines = "\n\n*Tracked Wallets:*\n" + "\n".join(
                "  " + str(i+1) + ". *" + w.get("label", "Unnamed") + "*\n"
                "     `" + w.get("address","")[:20] + "...`  " + w.get("chain","sol").upper()
                for i, w in enumerate(wallets)
            )
        else:
            wallet_lines = "\n\n_No wallets tracked yet. Add one below._"

        btns = [
            [InlineKeyboardButton("➕ Add Wallet",      callback_data="kol_add"),
             InlineKeyboardButton("🗑 Remove Wallet",   callback_data="kol_remove_menu")],
            [InlineKeyboardButton(("🔕 Mute Alerts" if alerts_on else "🔔 Unmute Alerts"), callback_data="kol_toggle_alerts")],
            [InlineKeyboardButton("◀ Back",             callback_data="v_sniper")],
        ]
        await q.edit_message_text(
            "👀 *KOL / SMART WALLET TRACKER*\n\n"
            + helius_line + "\n\n"
            "Track up to *10 Solana wallets*. Get instant alerts when they ape into a new token.\n"
            "Alerts include token score, MC, liquidity, and one-tap trade button.\n\n"
            "Alerts: *" + ("🔔 ON" if alerts_on else "🔕 OFF") + "*"
            + wallet_lines,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(btns)
        )

    elif cb == "kol_toggle_alerts":
        ud["kol_alerts_on"] = not ud.get("kol_alerts_on", True)
        alerts_on  = ud["kol_alerts_on"]
        wallets    = ud.get("kol_wallets", [])
        helius_set = bool(os.environ.get("HELIUS_API_KEY", ""))
        helius_line = "✅ Helius connected" if helius_set else "⚠️ *HELIUS_API_KEY not set* — add it in Railway Variables"
        wallet_lines = ""
        if wallets:
            wallet_lines = "\n\n*Tracked Wallets:*\n" + "\n".join(
                "  " + str(i+1) + ". *" + w.get("label", "Unnamed") + "*\n"
                "     `" + w.get("address","")[:20] + "...`  " + w.get("chain","sol").upper()
                for i, w in enumerate(wallets)
            )
        else:
            wallet_lines = "\n\n_No wallets tracked yet. Add one below._"
        btns = [
            [InlineKeyboardButton("➕ Add Wallet",      callback_data="kol_add"),
             InlineKeyboardButton("🗑 Remove Wallet",   callback_data="kol_remove_menu")],
            [InlineKeyboardButton(("🔕 Mute Alerts" if alerts_on else "🔔 Unmute Alerts"), callback_data="kol_toggle_alerts")],
            [InlineKeyboardButton("◀ Back",             callback_data="v_sniper")],
        ]
        await q.edit_message_text(
            "👀 *KOL / SMART WALLET TRACKER*\n\n"
            + helius_line + "\n\n"
            "Track up to *10 Solana wallets*. Get instant alerts when they ape into a new token.\n"
            "Alerts include token score, MC, liquidity, and one-tap trade button.\n\n"
            "Alerts: *" + ("🔔 ON" if alerts_on else "🔕 OFF") + "*"
            + wallet_lines,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(btns)
        )

    elif cb == "kol_add":
        if len(ud.get("kol_wallets", [])) >= 10:
            await q.edit_message_text(
                "⚠️ *Max 10 wallets reached.*\nRemove one before adding another.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="kol_menu")]])
            )
            return
        pending[u.id] = {"action": "kol_add_wallet"}
        await q.edit_message_text(
            "👀 *ADD KOL WALLET*\n\n"
            "Send the wallet address you want to track.\n\n"
            "Format: `<address>` or `<address> <label>`\n\n"
            "Examples:\n"
            "`7xKXtg... ` ← address only\n"
            "`7xKXtg... CryptoWhale` ← with label\n\n"
            "_Solana wallets only (requires Helius API key)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Cancel", callback_data="kol_menu")]])
        )

    elif cb == "kol_remove_menu":
        wallets = ud.get("kol_wallets", [])
        if not wallets:
            await q.answer("No wallets to remove.")
            return
        btns = [
            [InlineKeyboardButton("🗑 " + w.get("label", w.get("address","")[:10]+"..."), callback_data="kol_del_" + str(i))]
            for i, w in enumerate(wallets)
        ]
        btns.append([InlineKeyboardButton("◀ Back", callback_data="kol_menu")])
        await q.edit_message_text(
            "🗑 *REMOVE KOL WALLET*\n\nTap a wallet to remove it:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(btns)
        )

    elif cb.startswith("kol_del_"):
        idx = int(cb[8:])
        wallets = ud.get("kol_wallets", [])
        if 0 <= idx < len(wallets):
            removed = wallets.pop(idx)
            label   = removed.get("label", removed.get("address","?")[:10])
            # Clear cached last sig
            _kol_last_sig.get(u.id, {}).pop(removed.get("address",""), None)
            await q.edit_message_text(
                "✅ *" + label + "* removed from KOL tracker.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="kol_menu")]])
            )
        else:
            await q.answer("Wallet not found.")

    elif cb == "sniper_log_clear":
        ud["sniper_log"] = []
        # NOTE: sniper_seen is intentionally NOT cleared here.
        # That would cause all previously seen tokens to flood back.
        # Use "Reset Memory" in sniper settings to also clear seen.
        await q.edit_message_text(
            "✅ *Log cleared.*\n\n"
            "_Note: Token memory is preserved — previously seen tokens won't flood back._\n"
            "Use *Reset Memory* in Sniper Settings to start completely fresh.",
            parse_mode="Markdown",
            reply_markup=back_main()
        )

    elif cb == "sniper_reset_memory":
        ud["sniper_log"]  = []
        ud["sniper_seen"] = {}
        await q.edit_message_text(
            "🔄 *Full reset done.*\n\n"
            "Log cleared + token memory wiped.\n"
            "The sniper will re-evaluate all tokens it sees next run.",
            parse_mode="Markdown",
            reply_markup=back_main()
        )

    # Advisory confirm / skip
    elif cb.startswith("snp_confirm_"):
        rest    = cb[12:]
        parts   = rest.rsplit("_", 1)
        contract = parts[0]
        amount   = float(parts[1]) if len(parts) > 1 else float(ud.get("sniper_filters",{}).get("buy_amount",100))
        sniper_bought = ud.setdefault("sniper_bought", [])
        if contract in sniper_bought:
            await q.edit_message_text("Already bought this token.", reply_markup=back_main())
            return
        sniper_bought.append(contract)
        if len(sniper_bought) > 500:
            ud["sniper_bought"] = sniper_bought[-500:]
        _sniper_daily_reset(ud)
        ud["sniper_daily_spent"] = ud.get("sniper_daily_spent", 0) + amount
        result = await do_buy_core(ud, u.id, contract, amount, planned=True, mood="AI-Sniper")
        if isinstance(result, str):
            await q.edit_message_text(result, reply_markup=main_menu_kb())
        else:
            info2, tokens = result
            await q.edit_message_text(
                "✅ *ADVISORY BUY CONFIRMED*\n\n"
                "*$" + _md(info2["symbol"]) + "*\n"
                "Bought: *" + money(amount) + "*\n"
                "Price: *" + money(info2["price"]) + "*\n"
                "Cash left: *" + money(ud["balance"]) + "*",
                parse_mode="Markdown",
                reply_markup=buy_done_kb(contract)
            )

    elif cb.startswith("snp_view_"):
        # User tapped "👁 View Analysis" on compact pill — show full AI report
        contract = cb[9:]
        cached = _sniper_analysis_cache.get(u.id, {}).get(contract)
        if not cached:
            # Cache expired — fetch fresh data
            info2 = await get_token(contract)
            if not info2:
                await q.edit_message_text("⚠️ Token data expired. Paste the CA to view it live.", reply_markup=back_main())
                return
            await q.edit_message_text("⚠️ AI analysis expired. Tap the token CA to re-scan.", reply_markup=back_main())
            return
        info2, sc2, ai2 = cached["info"], cached["sc"], cached["ai"]
        report = _ai_report_text(info2, sc2, ai2, contract=contract)
        kb_rows = []
        # Always show View Token Card first
        kb_rows.append([InlineKeyboardButton("🪙 View Token Card", callback_data="btt_" + contract)])
        if ai2["verdict"] == "SNIPE":
            kb_rows.append([
                InlineKeyboardButton(
                    "✅ Buy " + money(ai2["suggested_amount"]),
                    callback_data="snp_confirm_" + contract + "_" + str(round(ai2["suggested_amount"], 2))
                ),
                InlineKeyboardButton("❌ Skip", callback_data="snp_skip_" + contract),
            ])
        else:
            kb_rows.append([InlineKeyboardButton("❌ Dismiss", callback_data="snp_skip_" + contract)])
        kb_rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="mm")])
        await q.edit_message_text(
            report, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb_rows)
        )

    elif cb.startswith("snp_skip_"):
        contract = cb[9:]
        await q.edit_message_text("❌ Token skipped.", reply_markup=back_main())

    # ── DCA BY MARKET CAP ──────────────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════════
    # QUICK BUY
    # ══════════════════════════════════════════════════════════════════════
    elif cb.startswith("qb_") and not cb.startswith("qb_set_") and not cb.startswith("qb_amt_"):
        contract = cb[3:]
        qb_amt   = ud.get("quick_buy_amount", 100.0)
        if qb_amt > ud.get("balance", 0):
            await q.edit_message_text(
                "❌ Insufficient balance for Quick Buy of " + money(qb_amt) + "\n"
                "Balance: " + money(ud.get("balance", 0)),
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Back to Token", callback_data="btt_" + contract)]
                ])
            )
            return
        if ud.get("mood_tracking", True):
            pending[u.id] = {"action": "buy_mood", "contract": contract, "amount": qb_amt}
            await q.edit_message_text(
                "🧠 *MOOD CHECK*\n\nWhy are you buying this?\n\n"
                "1 - Research\n2 - Chart looks good\n3 - Community tip\n4 - FOMO\n5 - Gut feeling\n\nReply with a number:",
                parse_mode="Markdown", reply_markup=cancel_kb()
            )
        else:
            await do_buy_query(q, ud, u.id, contract, qb_amt)

    elif cb.startswith("qb_set_"):
        # Show quick-buy amount picker (from within buy submenu)
        contract = cb[7:]
        qb_amt   = ud.get("quick_buy_amount", 100.0)
        await q.edit_message_text(
            "⚡ *SET QUICK BUY AMOUNT*\n\n"
            "Current: *$" + str(int(qb_amt)) + "*\n\n"
            "One tap on the token card will instantly buy this amount.\n"
            "Choose a new default:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("$25",   callback_data="qb_amt_25_"   + contract),
                 InlineKeyboardButton("$50",   callback_data="qb_amt_50_"   + contract),
                 InlineKeyboardButton("$100",  callback_data="qb_amt_100_"  + contract),
                 InlineKeyboardButton("$250",  callback_data="qb_amt_250_"  + contract)],
                [InlineKeyboardButton("$500",  callback_data="qb_amt_500_"  + contract),
                 InlineKeyboardButton("$1000", callback_data="qb_amt_1000_" + contract),
                 InlineKeyboardButton("✏️ Custom", callback_data="qb_custom_" + contract)],
                [InlineKeyboardButton("◀ Back", callback_data="bts_" + contract)],
            ])
        )

    elif cb.startswith("qb_amt_"):
        rest     = cb[7:]
        amt_str, contract = rest.split("_", 1)
        ud["quick_buy_amount"] = float(amt_str)
        await q.edit_message_text(
            "✅ Quick Buy set to *$" + amt_str + "*\n\n"
            "Tap ⚡ Quick Buy $" + amt_str + " on any token card to instantly buy.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back to Token", callback_data="btt_" + contract)]
            ])
        )

    elif cb.startswith("qb_custom_"):
        contract = cb[10:]
        pending[u.id] = {"action": "qb_custom_input", "contract": contract, "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text("Enter your custom Quick Buy amount in USD:", reply_markup=cancel_kb())


    elif cb == "wl_settings":
        milestone_on = ud.get("milestone_notif", True)
        await q.edit_message_text(
            "⚙️ *WATCHLIST SETTINGS*\n\n"
            "🔔 *Watchlist Milestones*\n"
            "Get notified when a watched token hits 2× 3× 5× 10× from\n"
            "the MC it had when you added it.\n\n"
            "Status: *" + ("🟢 ON" if milestone_on else "🔴 OFF") + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    ("🔴 Disable Watchlist Milestones" if milestone_on else "🟢 Enable Watchlist Milestones"),
                    callback_data="milestone_wl_toggle"
                )],
                [InlineKeyboardButton("◀ Back to Watchlist", callback_data="v_watchlist")],
            ])
        )

    elif cb == "milestone_wl_toggle":
        ud["milestone_notif"] = not ud.get("milestone_notif", True)
        on = ud["milestone_notif"]
        await q.edit_message_text(
            "🔔 Watchlist Milestones *" + ("enabled ✅" if on else "disabled ❌") + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back", callback_data="wl_settings")]
            ])
        )

    # ══════════════════════════════════════════════════════════════════════
    # MILESTONE NOTIFICATIONS (More menu)
    # ══════════════════════════════════════════════════════════════════════
    elif cb == "v_milestone_notif":
        ms_on   = ud.get("milestone_notif", True)
        dump_on = ud.get("milestone_notif_dump", True)
        await q.edit_message_text(
            "🚀 *MILESTONE NOTIFICATIONS*\n\n"
            "Get notified each time a holding hits a new multiplier.\n"
            "Each level fires *once* per position — no repeat spam.\n\n"
            "🚀 *Holdings Milestones:*  2× · 3× · 5× · 10× · 20× · 50×\n"
            "Status: *" + ("🟢 ON" if ms_on else "🔴 OFF") + "*\n\n"
            "🚨 *Dump Alert:*  fires once at –50%\n"
            "Status: *" + ("🟢 ON" if dump_on else "🔴 OFF") + "*\n\n"
            "👁 *Watchlist Milestones:* 2× 3× 5× 10× from add-time MC\n"
            "_(toggle in Watchlist → ⚙️ Settings)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    ("🔴 Disable Holdings Milestones" if ms_on else "🟢 Enable Holdings Milestones"),
                    callback_data="milestone_toggle"
                )],
                [InlineKeyboardButton(
                    ("🔴 Disable Dump Alert" if dump_on else "🟢 Enable Dump Alert"),
                    callback_data="milestone_dump_toggle"
                )],
                [InlineKeyboardButton("◀ Back to More", callback_data="v_more")],
            ])
        )

    elif cb == "milestone_toggle":
        ud["milestone_notif"] = not ud.get("milestone_notif", True)
        on = ud["milestone_notif"]
        await q.edit_message_text(
            "🚀 Holdings Milestones *" + ("enabled ✅" if on else "disabled ❌") + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="v_milestone_notif")]])
        )

    elif cb == "milestone_dump_toggle":
        ud["milestone_notif_dump"] = not ud.get("milestone_notif_dump", True)
        on = ud["milestone_notif_dump"]
        await q.edit_message_text(
            "🚨 Dump Alert *" + ("enabled ✅" if on else "disabled ❌") + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="v_milestone_notif")]])
        )

    # ══════════════════════════════════════════════════════════════════════
    # RUG PULL WARNING (More menu — OFF by default)
    # ══════════════════════════════════════════════════════════════════════
    elif cb == "v_rug_warn":
        rw_on  = ud.get("rug_warn_enabled", False)
        thresh = ud.get("rug_warn_threshold", 30)
        await q.edit_message_text(
            "🔥 *RUG PULL EARLY WARNING*\n\n"
            "Monitors liquidity on every token you hold.\n"
            "If LP drops *–" + str(thresh) + "% or more* within one scan cycle,\n"
            "you get an instant alert with a one-tap Sell Everything button.\n\n"
            "Status: *" + ("🟢 ON" if rw_on else "🔴 OFF") + "* (global OFF by default)\n"
            "Trigger threshold: *–" + str(thresh) + "% liq drop*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    ("🔴 Disable Rug Warning" if rw_on else "🟢 Enable Rug Warning"),
                    callback_data="rug_warn_toggle"
                )],
                [InlineKeyboardButton("Threshold: –20%", callback_data="rug_thresh_20"),
                 InlineKeyboardButton("Threshold: –30%", callback_data="rug_thresh_30"),
                 InlineKeyboardButton("Threshold: –50%", callback_data="rug_thresh_50")],
                [InlineKeyboardButton("◀ Back to More", callback_data="v_more")],
            ])
        )

    elif cb == "rug_warn_toggle":
        ud["rug_warn_enabled"] = not ud.get("rug_warn_enabled", False)
        on = ud["rug_warn_enabled"]
        await q.edit_message_text(
            "🔥 Rug Pull Warning *" + ("enabled ✅" if on else "disabled ❌") + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="v_rug_warn")]])
        )

    elif cb.startswith("rug_thresh_"):
        ud["rug_warn_threshold"] = int(cb[11:])
        thresh = ud["rug_warn_threshold"]
        await q.edit_message_text(
            "✅ Rug Warning threshold set to *–" + str(thresh) + "%*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="v_rug_warn")]])
        )

    # ══════════════════════════════════════════════════════════════════════
    # COPY TRADE REPLAY + SETTINGS
    # ══════════════════════════════════════════════════════════════════════
    elif cb == "copy_replay":
        logs = trade_log.get(u.id, [])
        closed = [t for t in logs if t.get("exit_price")]
        if not closed:
            await q.edit_message_text(
                "📽 *TRADE REPLAY*\n\nNo closed trades with replay data yet.\nClose a position to see its full timeline.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="v_copy")]])
            )
            return
        buttons = []
        for i, t2 in enumerate(reversed(closed[-10:])):
            real_i = len(closed) - 1 - i
            pnl_tag = "✅" if t2["realized_pnl"] >= 0 else "❌"
            lbl = pnl_tag + " $" + t2["symbol"] + "  " + str(round(t2.get("x",0),1)) + "x  " + t2["reason"]
            buttons.append([InlineKeyboardButton(lbl, callback_data="copy_replay_" + str(real_i))])
        buttons.append([InlineKeyboardButton("◀ Back", callback_data="v_copy")])
        await q.edit_message_text(
            "📽 *TRADE REPLAY*\n\nSelect a trade to review its full timeline:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif cb.startswith("copy_replay_") and cb[12:].isdigit():
        idx  = int(cb[12:])
        logs = trade_log.get(u.id, [])
        closed = [t2 for t2 in logs if t2.get("exit_price")]
        if idx >= len(closed):
            await q.edit_message_text("Trade not found.", reply_markup=back_main())
            return
        t2 = closed[idx]
        sym    = t2["symbol"]
        inv    = t2["invested"]
        ret    = t2["returned"]
        pnl    = t2["realized_pnl"]
        x_val  = t2.get("x", 0)
        reason = t2.get("reason", "manual")
        ep     = t2.get("exit_price", 0)
        ap     = t2.get("avg_price", 0)
        pp     = t2.get("peak_price", ep)
        hold_h = t2.get("hold_h", 0)
        bought_at = t2.get("bought_at", datetime.now())
        peak_x = round(pp / ap, 2) if ap > 0 else 0
        left_on_table = round((pp / ep - 1) * ret, 2) if ep > 0 and pp > ep else 0
        closed_at = t2.get("closed_at", datetime.now())
        entry_time = bought_at.strftime("%b %d %H:%M") if hasattr(bought_at,"strftime") else "?"
        exit_time  = closed_at.strftime("%b %d %H:%M") if hasattr(closed_at,"strftime") else "?"
        pnl_sign = "+" if pnl >= 0 else ""
        peak_note = ""
        if left_on_table > 1:
            peak_note = "\n_(left " + money(left_on_table) + " on the table — peak was " + str(peak_x) + "×)_"
        await q.edit_message_text(
            "📽 *TRADE REPLAY — $" + sym + "*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "🟦 *ENTRY*  ·  " + entry_time + "\n"
            "  Invested: *" + money(inv) + "*  @  *" + money(ap) + "*\n\n"
            "⭐ *PEAK PRICE*\n"
            "  *" + money(pp) + "*  (" + str(peak_x) + "×)\n\n"
            "🔴 *EXIT*  ·  " + exit_time + "  ·  " + reason + "\n"
            "  Received: *" + money(ret) + "*  @  *" + money(ep) + "*\n\n"
            "📋 *RESULT*  ·  held *" + str(hold_h) + "h*\n"
            "  " + pnl_sign + money(pnl) + "  ·  *" + str(round(x_val,2)) + "×*"
            + peak_note,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back to Replay", callback_data="copy_replay")],
                [InlineKeyboardButton("🏠 Main Menu",     callback_data="mm")],
            ])
        )

    elif cb == "copy_settings":
        await q.edit_message_text(
            "⚙️ *COPY TRADE SETTINGS*\n\n"
            "📽 *Trade Replay* — view full timeline of any closed trade.\n"
            "  Access: Copy Trading → 📽 Replay\n\n"
            "⭐ *Peak vs Exit* — see how much you left on the table.\n\n"
            "All features are always on for closed trades.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📽 View Replays", callback_data="copy_replay")],
                [InlineKeyboardButton("◀ Back",          callback_data="v_copy")],
            ])
        )

    elif cb.startswith("dca_") and not cb.startswith("dca_cancel_") and not cb.startswith("dca_confirm_") and not cb.startswith("dca_addmore_") and not cb.startswith("dca_setmc_") and not cb.startswith("dca_setamt_") and not cb.startswith("dca_amt_quick_") and cb != "v_dca":
        contract = cb[4:]
        info = await get_token(contract)
        sym    = info["symbol"] if info else "?"
        cur_mc = mc_str(info["mc"]) if info else "unknown"
        existing = [d for d in ud.get("dca_orders", []) if d["contract"] == contract and not d.get("cancelled")]

        # Start fresh DCA session in pending
        pending[u.id] = {
            "action":   "dca_build",
            "contract": contract,
            "symbol":   sym,
            "targets":  [],  # list of {mc, amount} being built
        }

        ex_txt = ""
        if existing:
            ex_txt = "\n*Current plan:*\n"
            for tgt in existing[0].get("mc_targets", []):
                status = "✅" if tgt.get("triggered") else "⏳"
                ex_txt += status + " " + mc_str(tgt["mc"]) + " → buy " + money(tgt["amount"]) + "\n"

        await q.edit_message_text(
            "📉 *DCA BY MARKET CAP*\n\n"
            "*$" + sym + "*  |  Current MC: *" + cur_mc + "*\n" + ex_txt + "\n"
            "Build your DCA plan step by step.\n"
            "Tap *Set MC Target* to add your first trigger:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📈 Set MC Target", callback_data="dca_setmc_" + contract)],
                [InlineKeyboardButton("❌ Cancel Existing", callback_data="dca_cancel_" + contract)],
                [InlineKeyboardButton("◀ Back",            callback_data="btt_" + contract)],
            ])
        )

    elif cb.startswith("dca_setmc_"):
        contract = cb[10:]
        p = pending.get(u.id, {})
        if p.get("action") not in ("dca_build", "dca_addmore"):
            # Re-init if pending was lost
            info = await get_token(contract)
            pending[u.id] = {"action": "dca_build", "contract": contract,
                             "symbol": info["symbol"] if info else "?", "targets": []}
            p = pending[u.id]
        pending[u.id]["action"] = "dca_mc_input"
        targets = p.get("targets", [])
        step = len(targets) + 1
        await q.edit_message_text(
            "📈 *DCA TARGET " + str(step) + " — SET MC*\n\n"
            "Enter the market cap that should trigger this buy.\n\n"
            "Examples:\n"
            "  `500000`  = $500K\n"
            "  `1000000` = $1M\n"
            "  `5000000` = $5M",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back", callback_data="dca_" + contract)],
            ])
        )

    elif cb.startswith("dca_setamt_"):
        contract = cb[11:]
        p = pending.get(u.id, {})
        mc_val = p.get("pending_mc")
        if not mc_val:
            await q.edit_message_text("Session expired. Please restart DCA.", reply_markup=back_main())
            return
        pending[u.id]["action"] = "dca_amt_input"
        await q.edit_message_text(
            "💵 *DCA TARGET — SET BUY AMOUNT*\n\n"
            "MC trigger: *" + mc_str(mc_val) + "*\n\n"
            "How much USD to buy when this MC is hit?\n\n"
            "Examples:  `50`  `100`  `250`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("$50",  callback_data="dca_amt_quick_50_"  + contract),
                 InlineKeyboardButton("$100", callback_data="dca_amt_quick_100_" + contract),
                 InlineKeyboardButton("$250", callback_data="dca_amt_quick_250_" + contract)],
                [InlineKeyboardButton("$500", callback_data="dca_amt_quick_500_" + contract),
                 InlineKeyboardButton("◀ Back", callback_data="dca_setmc_" + contract)],
            ])
        )

    elif cb.startswith("dca_amt_quick_"):
        rest     = cb[14:]          # e.g. "100_<contract>"
        amt_str, contract = rest.split("_", 1)
        amt      = float(amt_str)
        p        = pending.get(u.id, {})
        mc_val   = p.get("pending_mc", 0)
        targets  = p.get("targets", [])
        targets.append({"mc": mc_val, "amount": amt, "triggered": False})
        pending[u.id]["targets"] = targets
        pending[u.id].pop("pending_mc", None)
        pending[u.id]["action"] = "dca_build"
        await _dca_show_plan(q, contract, pending[u.id])

    elif cb.startswith("dca_addmore_"):
        contract = cb[12:]
        p = pending.get(u.id, {})
        if not p.get("targets"):
            await q.edit_message_text("Session expired.", reply_markup=back_main())
            return
        pending[u.id]["action"] = "dca_mc_input"
        step = len(p["targets"]) + 1
        await q.edit_message_text(
            "📈 *DCA TARGET " + str(step) + " — SET MC*\n\n"
            "Enter the next MC trigger:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back", callback_data="dca_confirm_" + contract)],
            ])
        )

    elif cb.startswith("dca_confirm_"):
        contract = cb[12:]
        p        = pending.get(u.id, {})
        targets  = p.get("targets", [])
        sym      = p.get("symbol", "?")
        if not targets:
            await q.edit_message_text("No targets set.", reply_markup=back_main())
            return
        targets.sort(key=lambda x: x["mc"])
        ud["dca_orders"] = [d for d in ud.get("dca_orders", []) if d["contract"] != contract]
        ud["dca_orders"].append({
            "contract":   contract,
            "symbol":     sym,
            "mc_targets": targets,
            "created_at": datetime.now().isoformat(),
            "cancelled":  False,
        })
        pending.pop(u.id, None)
        lines = "\n".join(
            "  📍 Buy *" + money(tgt["amount"]) + "* at *" + mc_str(tgt["mc"]) + "* MC"
            for tgt in targets
        )
        await q.edit_message_text(
            "✅ *DCA PLAN SET — $" + sym + "*\n\n" + lines + "\n\n"
            "The bot will auto-buy at each MC milestone.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back to Token", callback_data="btt_" + contract)],
                [InlineKeyboardButton("🏠 Main Menu",    callback_data="mm")],
            ])
        )

    elif cb.startswith("dca_cancel_"):
        contract = cb[11:]
        before = len(ud.get("dca_orders", []))
        # Find symbol from dca_orders BEFORE removing it
        sym = contract[:8] + "..."
        for d in ud.get("dca_orders", []):
            if d["contract"] == contract and d.get("symbol"):
                sym = d["symbol"]
                break
        ud["dca_orders"] = [d for d in ud.get("dca_orders", []) if d["contract"] != contract]
        await q.edit_message_text(
            "✅ DCA orders for $" + sym + " cancelled.",
            reply_markup=back_main()
        )

    elif cb == "v_dca":
        orders = ud.get("dca_orders", [])
        if not orders:
            await q.edit_message_text("📉 *DCA ORDERS*\n\nNo active DCA orders.\nOpen a token and use the 📉 DCA by MC button.", parse_mode="Markdown", reply_markup=back_main())
            return
        lines = ["📉 *ACTIVE DCA ORDERS*\n"]
        for dca in orders:
            lines.append("*$" + _md(dca["symbol"]) + "*")
            for tgt in dca.get("mc_targets", []):
                status = "✅" if tgt.get("triggered") else "⏳"
                lines.append("  " + status + " " + mc_str(tgt["mc"]) + " → " + money(tgt["amount"]))
            lines.append("")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=back_main())

    # ── CSV EXPORT ─────────────────────────────────────────────────────────────
    elif cb == "v_export":
        await q.edit_message_text("📁 Generating your trade history CSV...", reply_markup=back_more())
        await export_csv(ctx.bot, u.id, ud)

    # ── LANGUAGE SELECTOR ──────────────────────────────────────────────────────
    elif cb == "cfg_lang":
        await q.edit_message_text(
            "🌐 *SELECT LANGUAGE*\n\nChoose your preferred language:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🇬🇧 English",    callback_data="lang_en"),
                 InlineKeyboardButton("🇪🇸 Español",   callback_data="lang_es")],
                [InlineKeyboardButton("🇧🇷 Português", callback_data="lang_pt"),
                 InlineKeyboardButton("🇫🇷 Français",  callback_data="lang_fr")],
                [InlineKeyboardButton("🇨🇳 中文",       callback_data="lang_zh")],
                [InlineKeyboardButton("Back",           callback_data="v_settings")],
            ])
        )

    elif cb.startswith("lang_"):
        lang = cb[5:]
        if lang in TRANSLATIONS:
            ud["language"] = lang
            await q.edit_message_text(
                t(ud, "lang_set"), parse_mode="Markdown", reply_markup=settings_kb(ud)
            )
        else:
            await q.edit_message_text("Language not found.", reply_markup=back_main())

    # ── APEX VAULT MENU ───────────────────────────────────────────────────────
    elif cb == "apex_menu":
        apex_on  = ud.get("apex_mode", False)
        vault    = ud.get("apex_vault", 0.0)
        balance  = ud.get("balance", 0.0)
        try:    heat = round(apex_capital_heat(ud) * 100, 1)
        except: heat = 0.0
        try:    positions = apex_count_positions(ud)
        except: positions = 0
        try:    paused = apex_is_paused(u.id)
        except: paused = False
        try:    halted = apex_is_daily_loss_halted(ud)
        except: halted = False
        daily_pnl  = ud.get("apex_daily_pnl", 0.0)
        total_tr   = ud.get("apex_total_trades", 0)
        total_wins = ud.get("apex_total_wins", 0)
        wr         = round(total_wins / total_tr * 100) if total_tr > 0 else 0
        wl_count   = len(_apex_watchlist.get(u.id, {}))
        split_pct  = ud.get("apex_vault_profit_split", 0.50)

        if apex_on and paused:
            state_line = "\u23f8\ufe0f *PAUSED* (cooling down after 3 losses)"
        elif apex_on and halted:
            state_line = "\U0001f6d1 *HALTED* (daily loss limit reached)"
        elif apex_on:
            state_line = "\u26a1 *ON*"
        else:
            state_line = "\U0001f534 *OFF*"

        heat_bar  = "\u2593" * int(heat/10) + "\u2591" * (10 - int(heat/10))
        heat_icon = "\U0001f7e2" if heat < 40 else ("\U0001f7e1" if heat < 65 else "\U0001f534")
        toggle_label = "\U0001f534 Disable APEX" if apex_on else "\u26a1 Enable APEX"
        wl_label  = "\U0001f441 Watchlist" + (" (" + str(wl_count) + ")" if wl_count > 0 else "")

        kb = [
            [InlineKeyboardButton(toggle_label,             callback_data="apex_toggle")],
            [InlineKeyboardButton("\U0001f3e6 Vault \u2014 " + money(vault),
                                                            callback_data="apex_vault_menu")],
            [InlineKeyboardButton("\U0001f4ca APEX Stats",  callback_data="apex_stats"),
             InlineKeyboardButton("\U0001f4cb APEX Log",    callback_data="apex_log_view")],
            [InlineKeyboardButton(wl_label,                  callback_data="apex_watchlist_view")],
            [InlineKeyboardButton("\u2699\ufe0f APEX Settings", callback_data="apex_settings_menu")],
            [InlineKeyboardButton("\u25c0 Back",            callback_data="v_sniper")],
        ]
        vault_note = (
            "\n\u26a0\ufe0f _Vault empty \u2014 fund it to enable auto-trading_"
            if vault < 1.0 else
            "\n_" + str(int(split_pct*100)) + "% of each profit \u2192 main balance automatically_"
        )
        await q.edit_message_text(
            "\u26a1 *APEX MODE*\n"
            "_Autonomous Profit & Exit eXecution_\n\n"
            "Status: " + state_line + "\n\n"
            "\U0001f4ca *Live Stats*\n"
            "  Open positions: *" + str(positions) + "/\u221e*\n"
            "  " + heat_icon + " Capital heat: *" + heat_bar + "* " + str(heat) + "%\n"
            "  Today PnL: *" + pstr(daily_pnl) + "*\n"
            "  All-time: *" + str(total_tr) + " trades*  *" + str(wr) + "% WR*\n\n"
            "\U0001f3e6 *Vault:* " + money(vault) + "  \U0001f4b5 *Balance:* " + money(balance)
            + vault_note,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif cb == "apex_toggle":
        apex_on = ud.get("apex_mode", False)
        if not apex_on:
            apex_reset_daily(ud)
            ud["apex_mode"]             = True
            ud["apex_total_trades"]     = ud.get("apex_total_trades", 0)
            ud["apex_total_wins"]       = ud.get("apex_total_wins", 0)
            ud["apex_consec_losses"]    = ud.get("apex_consec_losses", 0)
            ud["apex_daily_pnl"]        = ud.get("apex_daily_pnl", 0.0)
            ud["apex_session_start_bal"] = ud.get("apex_vault", 0.0)
            save_user(u.id, ud)
            vault = ud.get("apex_vault", 0.0)
            split_pct = ud.get("apex_vault_profit_split", 0.50)
            msg = (
                "\u26a1 *APEX MODE ENABLED*\n\n"
                "APEX will now autonomously:\n"
                "  \u2022 Monitor all AI-sniped positions\n"
                "  \u2022 Apply trailing stops (activates at 1.5x)\n"
                "  \u2022 Emergency exit on rug/dump signals\n"
                "  \u2022 DCA into support on winning positions\n\n"
                "\U0001f3e6 *Vault:* " + money(vault) + "\n"
                "\U0001f501 *Profit split:* " + str(int(split_pct*100)) + "% \u2192 main balance on each exit\n\n"
                + ("\u26a0\ufe0f _Vault is empty \u2014 fund it from the Vault menu before APEX can trade._"
                   if vault < 1.0 else
                   "_APEX will trade from vault. " + str(int(split_pct*100)) + "% of each profit \u2192 main balance._")
            )
        else:
            ud["apex_mode"] = False
            save_user(u.id, ud)
            msg = (
                "\U0001f534 *APEX MODE DISABLED*\n\n"
                "Open APEX positions will continue to be managed until they close.\n\n"
                "Switch to Advisory Mode from the Sniper menu if needed."
            )
        await q.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\u26a1 APEX Menu",  callback_data="apex_menu")],
                [InlineKeyboardButton("\U0001f3af Sniper", callback_data="v_sniper")],
                [InlineKeyboardButton("\U0001f3e0 Menu",   callback_data="mm")],
            ])
        )

    elif cb == "apex_stats":
        total_tr   = ud.get("apex_total_trades", 0)
        total_wins = ud.get("apex_total_wins", 0)
        wr         = round(total_wins / total_tr * 100) if total_tr > 0 else 0
        vault      = ud.get("apex_vault", 0.0)
        daily_pnl  = ud.get("apex_daily_pnl", 0.0)
        conf_thr   = ud.get("apex_learn_threshold", 5)
        score_min  = ud.get("apex_learn_score_min", 45)
        consec_l   = ud.get("apex_consec_losses", 0)
        fees_paid  = ud.get("total_fees_paid", 0.0)
        profit_sent= ud.get("apex_vault_total_profit_sent", 0.0)
        try:    paused = apex_is_paused(u.id)
        except: paused = False
        try:    halted = apex_is_daily_loss_halted(ud)
        except: halted = False

        apex_logs = [t for t in trade_log.get(u.id, [])
                     if t.get("mood") in ("APEX", "APEX-DCA", "AI-Sniper")]
        exit_counts: dict = {}
        for t in apex_logs:
            r = (t.get("reason") or "manual").replace("apex_", "").replace("_", " ")
            exit_counts[r] = exit_counts.get(r, 0) + 1
        reason_str = "  " + "\n  ".join(
            r + ": " + str(n) for r, n in sorted(exit_counts.items(), key=lambda x: -x[1])
        ) if exit_counts else "  No closed trades yet"

        status_str = "\u23f8\ufe0f Paused" if paused else ("\U0001f6d1 Halted" if halted else "\u2705 Active")
        await q.edit_message_text(
            "\U0001f4ca *APEX LIFETIME STATS*\n\n"
            "Trades: *" + str(total_tr) + "*  Wins: *" + str(total_wins) + "*  WR: *" + str(wr) + "%*\n"
            "Today PnL: *" + pstr(daily_pnl) + "*\n"
            "Consecutive losses: *" + str(consec_l) + "*\n"
            "Status: " + status_str + "\n\n"
            "\U0001f3e6 *Vault:* " + money(vault) + "\n"
            + ("\U0001f4b8 *Sent to main balance:* " + money(profit_sent) + "\n" if profit_sent > 0 else "")
            + ("\U0001f4b8 *Total fees paid:* " + money(fees_paid) + " _(simulated)_\n" if SIM_FEES_ENABLED else "")
            + "\n\U0001f9e0 *Self-learned thresholds:*\n"
            "  Min confidence: *" + str(conf_thr) + "/10*\n"
            "  Min score: *" + str(score_min) + "/100*\n\n"
            "\U0001f4e4 *Exit breakdown:*\n" + reason_str,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="apex_menu")]])
        )

    elif cb == "apex_log_view":
        logs = [t for t in trade_log.get(u.id, [])
                if t.get("mood") in ("APEX", "APEX-DCA", "AI-Sniper")][-10:]
        if not logs:
            await q.edit_message_text(
                "\U0001f4cb *APEX LOG*\n\nNo closed trades yet.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="apex_menu")]])
            )
            return
        lines = ["\U0001f4cb *APEX TRADE LOG* (last 10)\n"]
        for t in reversed(logs):
            sym  = _md(t.get("symbol", "?"))
            x    = round(t.get("x") or 0, 2)
            pnl  = t.get("realized_pnl") or 0
            rsn  = (t.get("reason") or "manual").replace("apex_","").replace("_"," ")[:12]
            icon = "\U0001f7e2" if pnl > 0 else "\U0001f534"
            lines.append(icon + " *$" + sym + "*  " + str(x) + "x  " + pstr(pnl) + "  _" + rsn + "_")
        await q.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="apex_menu")]])
        )

    elif cb == "apex_settings_menu":
        conf_thr   = ud.get("apex_learn_threshold", 6)
        score_min  = ud.get("apex_learn_score_min", 45)
        split_pct  = ud.get("apex_vault_profit_split", 0.50)
        profile    = ud.get("apex_risk_profile", "default")
        _hp_label  = {"default": "⚖️ Default", "hunter": "🎯 Hunter"}.get(profile, "⚖️ Default")
        _halt_str  = "25%" if profile == "hunter" else "20%"
        _halt_off  = ud.get("apex_daily_loss_halt_disabled", False)
        _halt_display = "~~" + _halt_str + "~~ _(off)_" if _halt_off else "*" + _halt_str + "*"
        _halt_btn_lbl = "🟢 Enable Loss Halt" if _halt_off else "🔴 Disable Loss Halt"
        _trail_x   = ud.get("apex_trail_activate_x_learned", APEX_TRAIL_ACTIVATE_X)
        _sl_low    = ud.get("apex_sl_learned_low",  22.0)
        _sl_med    = ud.get("apex_sl_learned_med",  18.0)
        _sl_high   = ud.get("apex_sl_learned_high", 12.0)
        _lad_x     = ud.get("apex_ladder_trigger_x", 2.0)
        _lad_pct   = int(ud.get("apex_ladder_sell_pct", 0.50) * 100)
        await q.edit_message_text(
            "⚙️ *APEX SETTINGS*\n\n"
            "Min confidence: *" + str(conf_thr) + "/10*\n"
            "Min score: *" + str(score_min) + "/100*\n"
            "Daily loss halt: " + _halt_display + "\n"
            "Trail activates at: *" + str(round(_trail_x, 1)) + "x*\n"
            "Stop Loss — Low: *" + str(round(_sl_low, 1)) + "%*  "
            "Med: *" + str(round(_sl_med, 1)) + "%*  "
            "High: *" + str(round(_sl_high, 1)) + "%*\n"
            "Ladder sell: *" + str(_lad_pct) + "%* at *" + str(_lad_x) + "x*\n"
            "Vault profit split: *" + str(int(split_pct*100)) + "%* → main balance\n\n"
            "🎛 *Risk Profile:* " + _hp_label + "\n"
            "_Self-learning engine suggests but never overrides your settings._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎛 Change Risk Profile",   callback_data="apex_profile_menu")],
                [InlineKeyboardButton(_halt_btn_lbl,              callback_data="apex_halt_toggle")],
                [InlineKeyboardButton("🎯 Min Confidence",        callback_data="apex_set_confidence"),
                 InlineKeyboardButton("📊 Min Score",             callback_data="apex_set_score")],
                [InlineKeyboardButton("📈 Set Trail Activation",  callback_data="apex_set_trail_x")],
                [InlineKeyboardButton("🛑 Set Stop Loss %",       callback_data="apex_set_sl")],
                [InlineKeyboardButton("🪜 Set Ladder",            callback_data="apex_set_ladder")],
                [InlineKeyboardButton("⬅️ Back",                 callback_data="apex_menu")],
            ])
        )

    elif cb == "apex_set_trail_x":
        _cur_trail = ud.get("apex_trail_activate_x_learned", APEX_TRAIL_ACTIVATE_X)
        pending[u.id] = {"action": "apex_trail_x_input"}
        await q.edit_message_text(
            "📈 *SET TRAIL ACTIVATION*\n\n"
            "Current: *" + str(round(_cur_trail, 1)) + "x*\n\n"
            "Enter the multiplier at which trailing stop activates.\n"
            "Example: *1.5* = trail starts once position is up 50%.\n\n"
            "Range: 1.2 – 4.0\n"
            "_Lower = tighter protection. Higher = lets winners run longer._\n\n"
            "_The self-learning engine may suggest changes after daily trades_\n"
            "_but will NOT apply them automatically._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="apex_settings_menu")]])
        )

    elif cb == "apex_set_sl":
        _sl_low  = ud.get("apex_sl_learned_low",  22.0)
        _sl_med  = ud.get("apex_sl_learned_med",  18.0)
        _sl_high = ud.get("apex_sl_learned_high", 12.0)
        pending[u.id] = {"action": "apex_sl_input"}
        await q.edit_message_text(
            "🛑 *SET STOP LOSS %*\n\n"
            "Current:\n"
            "  LOW risk:    *" + str(round(_sl_low, 1)) + "%*\n"
            "  MEDIUM risk: *" + str(round(_sl_med, 1)) + "%*\n"
            "  HIGH risk:   *" + str(round(_sl_high, 1)) + "%*\n\n"
            "Enter three values: *LOW MEDIUM HIGH*\n"
            "Example: *22 18 12*\n\n"
            "Ranges: LOW 10–35 | MEDIUM 8–28 | HIGH 6–20\n"
            "LOW must be ≥ MEDIUM ≥ HIGH\n\n"
            "_Self-learning suggests changes after daily trades_\n"
            "_but will NOT apply them automatically._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="apex_settings_menu")]])
        )

    elif cb == "apex_halt_toggle":
        _currently_off = ud.get("apex_daily_loss_halt_disabled", False)
        ud["apex_daily_loss_halt_disabled"] = not _currently_off
        save_user(u.id, ud)
        _now_off = ud["apex_daily_loss_halt_disabled"]
        await q.answer(
            "Loss halt " + ("DISABLED — APEX trades through losses." if _now_off else "ENABLED — APEX pauses at daily loss limit."),
            show_alert=True
        )
        conf_thr  = ud.get("apex_learn_threshold", 5)
        score_min = ud.get("apex_learn_score_min", 45)
        split_pct = ud.get("apex_vault_profit_split", 0.50)
        profile   = ud.get("apex_risk_profile", "default")
        _hp_lbl2  = {"default": "⚖️ Default", "hunter": "🎯 Hunter"}.get(profile, "⚖️ Default")
        _halt_str2 = "25%" if profile == "hunter" else "20%"
        _halt_disp2 = "~~" + _halt_str2 + "~~ _(off)_" if _now_off else "*" + _halt_str2 + "*"
        _halt_btn2  = "🟢 Enable Loss Halt" if _now_off else "🔴 Disable Loss Halt"
        _trail_x2   = ud.get("apex_trail_activate_x_learned", APEX_TRAIL_ACTIVATE_X)
        _sl_low2    = ud.get("apex_sl_learned_low",  22.0)
        _sl_med2    = ud.get("apex_sl_learned_med",  18.0)
        _sl_high2   = ud.get("apex_sl_learned_high", 12.0)
        _lad_x2   = ud.get("apex_ladder_trigger_x", 2.0)
        _lad_pct2 = int(ud.get("apex_ladder_sell_pct", 0.50) * 100)
        await q.edit_message_text(
            "⚙️ *APEX SETTINGS*\n\n"
            "Min confidence: *" + str(conf_thr) + "/10*\n"
            "Min score: *" + str(score_min) + "/100*\n"
            "Daily loss halt: " + _halt_disp2 + "\n"
            "Trail activates at: *" + str(round(_trail_x2, 1)) + "x*\n"
            "Stop Loss — Low: *" + str(round(_sl_low2, 1)) + "%*  "
            "Med: *" + str(round(_sl_med2, 1)) + "%*  "
            "High: *" + str(round(_sl_high2, 1)) + "%*\n"
            "Ladder sell: *" + str(_lad_pct2) + "%* at *" + str(_lad_x2) + "x*\n"
            "Vault profit split: *" + str(int(split_pct*100)) + "%* → main balance\n\n"
            "🎛 *Risk Profile:* " + _hp_lbl2 + "\n"
            "_Self-learning engine suggests but never overrides your settings._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎛 Change Risk Profile",   callback_data="apex_profile_menu")],
                [InlineKeyboardButton(_halt_btn2,                 callback_data="apex_halt_toggle")],
                [InlineKeyboardButton("🎯 Min Confidence",        callback_data="apex_set_confidence"),
                 InlineKeyboardButton("📊 Min Score",             callback_data="apex_set_score")],
                [InlineKeyboardButton("📈 Set Trail Activation",  callback_data="apex_set_trail_x")],
                [InlineKeyboardButton("🛑 Set Stop Loss %",       callback_data="apex_set_sl")],
                [InlineKeyboardButton("🪜 Set Ladder",            callback_data="apex_set_ladder")],
                [InlineKeyboardButton("⬅️ Back",                 callback_data="apex_menu")],
            ])
        )

    elif cb == "apex_set_confidence":
        _cur_conf = ud.get("apex_learn_threshold", 6)
        pending[u.id] = {"action": "apex_confidence_input"}
        await q.edit_message_text(
            "🎯 *SET MIN CONFIDENCE*\n\n"
            "Current: *" + str(_cur_conf) + "/10*\n\n"
            "APEX only enters trades where the AI confidence meets or exceeds this.\n"
            "Higher = fewer but better quality entries.\n\n"
            "Range: 1 – 10\n"
            "_Recommended: 6–8_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("4", callback_data="apex_conf_quick_4"),
                 InlineKeyboardButton("5", callback_data="apex_conf_quick_5"),
                 InlineKeyboardButton("6", callback_data="apex_conf_quick_6"),
                 InlineKeyboardButton("7", callback_data="apex_conf_quick_7"),
                 InlineKeyboardButton("8", callback_data="apex_conf_quick_8")],
                [InlineKeyboardButton("❌ Cancel", callback_data="apex_settings_menu")],
            ])
        )

    elif cb.startswith("apex_conf_quick_"):
        val = int(cb.replace("apex_conf_quick_", ""))
        ud["apex_learn_threshold"] = val
        save_user(u.id, ud)
        pending.pop(u.id, None)
        await q.answer("✅ Min confidence set to " + str(val) + "/10", show_alert=True)
        # Re-render settings menu directly
        conf_thr  = ud.get("apex_learn_threshold", 6)
        score_min = ud.get("apex_learn_score_min", 45)
        split_pct = ud.get("apex_vault_profit_split", 0.50)
        profile   = ud.get("apex_risk_profile", "default")
        _hp_label = {"default": "⚖️ Default", "hunter": "🎯 Hunter"}.get(profile, "⚖️ Default")
        _halt_str  = "25%" if profile == "hunter" else "20%"
        _halt_off  = ud.get("apex_daily_loss_halt_disabled", False)
        _halt_display = "~~" + _halt_str + "~~ _(off)_" if _halt_off else "*" + _halt_str + "*"
        _halt_btn_lbl = "🟢 Enable Loss Halt" if _halt_off else "🔴 Disable Loss Halt"
        _trail_x  = ud.get("apex_trail_activate_x_learned", APEX_TRAIL_ACTIVATE_X)
        _sl_low   = ud.get("apex_sl_learned_low",  22.0)
        _sl_med   = ud.get("apex_sl_learned_med",  18.0)
        _sl_high  = ud.get("apex_sl_learned_high", 12.0)
        _lad_x    = ud.get("apex_ladder_trigger_x", 2.0)
        _lad_pct  = int(ud.get("apex_ladder_sell_pct", 0.50) * 100)
        await q.edit_message_text(
            "⚙️ *APEX SETTINGS*\n\n"
            "Min confidence: *" + str(conf_thr) + "/10*\n"
            "Min score: *" + str(score_min) + "/100*\n"
            "Daily loss halt: " + _halt_display + "\n"
            "Trail activates at: *" + str(round(_trail_x, 1)) + "x*\n"
            "Stop Loss — Low: *" + str(round(_sl_low, 1)) + "%*  "
            "Med: *" + str(round(_sl_med, 1)) + "%*  "
            "High: *" + str(round(_sl_high, 1)) + "%*\n"
            "Ladder sell: *" + str(_lad_pct) + "%* at *" + str(_lad_x) + "x*\n"
            "Vault profit split: *" + str(int(split_pct*100)) + "%* → main balance\n\n"
            "🎛 *Risk Profile:* " + _hp_label + "\n"
            "_Self-learning engine suggests but never overrides your settings._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎛 Change Risk Profile",   callback_data="apex_profile_menu")],
                [InlineKeyboardButton(_halt_btn_lbl,              callback_data="apex_halt_toggle")],
                [InlineKeyboardButton("🎯 Min Confidence",        callback_data="apex_set_confidence"),
                 InlineKeyboardButton("📊 Min Score",             callback_data="apex_set_score")],
                [InlineKeyboardButton("📈 Set Trail Activation",  callback_data="apex_set_trail_x")],
                [InlineKeyboardButton("🛑 Set Stop Loss %",       callback_data="apex_set_sl")],
                [InlineKeyboardButton("🪜 Set Ladder",            callback_data="apex_set_ladder")],
                [InlineKeyboardButton("⬅️ Back",                 callback_data="apex_menu")],
            ])
        )

    elif cb == "apex_set_score":
        _cur_score = ud.get("apex_learn_score_min", 45)
        pending[u.id] = {"action": "apex_score_input"}
        await q.edit_message_text(
            "📊 *SET MIN SCORE*\n\n"
            "Current: *" + str(_cur_score) + "/100*\n\n"
            "APEX only enters trades where the sniper score meets or exceeds this.\n"
            "Higher = stricter token quality filter.\n\n"
            "Range: 20 – 80\n"
            "_Recommended: 45–60_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("35", callback_data="apex_score_quick_35"),
                 InlineKeyboardButton("45", callback_data="apex_score_quick_45"),
                 InlineKeyboardButton("55", callback_data="apex_score_quick_55"),
                 InlineKeyboardButton("65", callback_data="apex_score_quick_65")],
                [InlineKeyboardButton("✏️ Custom", callback_data="apex_score_custom")],
                [InlineKeyboardButton("❌ Cancel",  callback_data="apex_settings_menu")],
            ])
        )

    elif cb.startswith("apex_score_quick_"):
        val = int(cb.replace("apex_score_quick_", ""))
        ud["apex_learn_score_min"] = val
        save_user(u.id, ud)
        pending.pop(u.id, None)
        await q.answer("✅ Min score set to " + str(val) + "/100", show_alert=True)
        # Re-render settings menu directly
        conf_thr  = ud.get("apex_learn_threshold", 6)
        score_min = ud.get("apex_learn_score_min", 45)
        split_pct = ud.get("apex_vault_profit_split", 0.50)
        profile   = ud.get("apex_risk_profile", "default")
        _hp_label = {"default": "⚖️ Default", "hunter": "🎯 Hunter"}.get(profile, "⚖️ Default")
        _halt_str  = "25%" if profile == "hunter" else "20%"
        _halt_off  = ud.get("apex_daily_loss_halt_disabled", False)
        _halt_display = "~~" + _halt_str + "~~ _(off)_" if _halt_off else "*" + _halt_str + "*"
        _halt_btn_lbl = "🟢 Enable Loss Halt" if _halt_off else "🔴 Disable Loss Halt"
        _trail_x  = ud.get("apex_trail_activate_x_learned", APEX_TRAIL_ACTIVATE_X)
        _sl_low   = ud.get("apex_sl_learned_low",  22.0)
        _sl_med   = ud.get("apex_sl_learned_med",  18.0)
        _sl_high  = ud.get("apex_sl_learned_high", 12.0)
        _lad_x    = ud.get("apex_ladder_trigger_x", 2.0)
        _lad_pct  = int(ud.get("apex_ladder_sell_pct", 0.50) * 100)
        await q.edit_message_text(
            "⚙️ *APEX SETTINGS*\n\n"
            "Min confidence: *" + str(conf_thr) + "/10*\n"
            "Min score: *" + str(score_min) + "/100*\n"
            "Daily loss halt: " + _halt_display + "\n"
            "Trail activates at: *" + str(round(_trail_x, 1)) + "x*\n"
            "Stop Loss — Low: *" + str(round(_sl_low, 1)) + "%*  "
            "Med: *" + str(round(_sl_med, 1)) + "%*  "
            "High: *" + str(round(_sl_high, 1)) + "%*\n"
            "Ladder sell: *" + str(_lad_pct) + "%* at *" + str(_lad_x) + "x*\n"
            "Vault profit split: *" + str(int(split_pct*100)) + "%* → main balance\n\n"
            "🎛 *Risk Profile:* " + _hp_label + "\n"
            "_Self-learning engine suggests but never overrides your settings._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎛 Change Risk Profile",   callback_data="apex_profile_menu")],
                [InlineKeyboardButton(_halt_btn_lbl,              callback_data="apex_halt_toggle")],
                [InlineKeyboardButton("🎯 Min Confidence",        callback_data="apex_set_confidence"),
                 InlineKeyboardButton("📊 Min Score",             callback_data="apex_set_score")],
                [InlineKeyboardButton("📈 Set Trail Activation",  callback_data="apex_set_trail_x")],
                [InlineKeyboardButton("🛑 Set Stop Loss %",       callback_data="apex_set_sl")],
                [InlineKeyboardButton("🪜 Set Ladder",            callback_data="apex_set_ladder")],
                [InlineKeyboardButton("⬅️ Back",                 callback_data="apex_menu")],
            ])
        )

    elif cb == "apex_score_custom":
        pending[u.id] = {"action": "apex_score_input"}
        await q.edit_message_text(
            "📊 Enter min score (20–80):\nExample: 50",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="apex_settings_menu")]])
        )

    elif cb == "apex_set_ladder":
        _lad_x   = ud.get("apex_ladder_trigger_x", 2.0)
        _lad_pct = int(ud.get("apex_ladder_sell_pct", 0.50) * 100)
        await q.edit_message_text(
            "🪜 *SET LADDER SELL*\n\n"
            "Current: Sell *" + str(_lad_pct) + "%* when position hits *" + str(_lad_x) + "x*\n\n"
            "The ladder sells a portion of your position at a set multiplier\n"
            "to lock real profit. The rest continues trailing.\n\n"
            "*Step 1 — Choose trigger multiplier:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1.5x", callback_data="apex_lad_x_1.5"),
                 InlineKeyboardButton("2x",   callback_data="apex_lad_x_2.0"),
                 InlineKeyboardButton("2.5x", callback_data="apex_lad_x_2.5"),
                 InlineKeyboardButton("3x",   callback_data="apex_lad_x_3.0")],
                [InlineKeyboardButton("❌ Cancel", callback_data="apex_settings_menu")],
            ])
        )

    elif cb.startswith("apex_lad_x_"):
        _x_val = float(cb.replace("apex_lad_x_", ""))
        ud["apex_ladder_trigger_x"] = _x_val
        save_user(u.id, ud)
        _lad_pct = int(ud.get("apex_ladder_sell_pct", 0.50) * 100)
        await q.edit_message_text(
            "🪜 *SET LADDER SELL*\n\n"
            "Trigger set to: *" + str(_x_val) + "x*\n\n"
            "*Step 2 — Choose sell %:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("25%", callback_data="apex_lad_pct_25"),
                 InlineKeyboardButton("33%", callback_data="apex_lad_pct_33"),
                 InlineKeyboardButton("50%", callback_data="apex_lad_pct_50"),
                 InlineKeyboardButton("75%", callback_data="apex_lad_pct_75")],
                [InlineKeyboardButton("❌ Cancel", callback_data="apex_settings_menu")],
            ])
        )

    elif cb.startswith("apex_lad_pct_"):
        _pct_val = int(cb.replace("apex_lad_pct_", ""))
        ud["apex_ladder_sell_pct"] = round(_pct_val / 100, 2)
        save_user(u.id, ud)
        _lad_x = ud.get("apex_ladder_trigger_x", 2.0)
        await q.answer(
            "✅ Ladder set: sell " + str(_pct_val) + "% at " + str(_lad_x) + "x",
            show_alert=True
        )
        # Re-render settings menu directly
        conf_thr  = ud.get("apex_learn_threshold", 6)
        score_min = ud.get("apex_learn_score_min", 45)
        split_pct = ud.get("apex_vault_profit_split", 0.50)
        profile   = ud.get("apex_risk_profile", "default")
        _hp_label = {"default": "⚖️ Default", "hunter": "🎯 Hunter"}.get(profile, "⚖️ Default")
        _halt_str  = "25%" if profile == "hunter" else "20%"
        _halt_off  = ud.get("apex_daily_loss_halt_disabled", False)
        _halt_display = "~~" + _halt_str + "~~ _(off)_" if _halt_off else "*" + _halt_str + "*"
        _halt_btn_lbl = "🟢 Enable Loss Halt" if _halt_off else "🔴 Disable Loss Halt"
        _trail_x  = ud.get("apex_trail_activate_x_learned", APEX_TRAIL_ACTIVATE_X)
        _sl_low   = ud.get("apex_sl_learned_low",  22.0)
        _sl_med   = ud.get("apex_sl_learned_med",  18.0)
        _sl_high  = ud.get("apex_sl_learned_high", 12.0)
        _lad_pct  = int(ud.get("apex_ladder_sell_pct", 0.50) * 100)
        await q.edit_message_text(
            "⚙️ *APEX SETTINGS*\n\n"
            "Min confidence: *" + str(conf_thr) + "/10*\n"
            "Min score: *" + str(score_min) + "/100*\n"
            "Daily loss halt: " + _halt_display + "\n"
            "Trail activates at: *" + str(round(_trail_x, 1)) + "x*\n"
            "Stop Loss — Low: *" + str(round(_sl_low, 1)) + "%*  "
            "Med: *" + str(round(_sl_med, 1)) + "%*  "
            "High: *" + str(round(_sl_high, 1)) + "%*\n"
            "Ladder sell: *" + str(_lad_pct) + "%* at *" + str(_lad_x) + "x*\n"
            "Vault profit split: *" + str(int(split_pct*100)) + "%* → main balance\n\n"
            "🎛 *Risk Profile:* " + _hp_label + "\n"
            "_Self-learning engine suggests but never overrides your settings._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎛 Change Risk Profile",   callback_data="apex_profile_menu")],
                [InlineKeyboardButton(_halt_btn_lbl,              callback_data="apex_halt_toggle")],
                [InlineKeyboardButton("🎯 Min Confidence",        callback_data="apex_set_confidence"),
                 InlineKeyboardButton("📊 Min Score",             callback_data="apex_set_score")],
                [InlineKeyboardButton("📈 Set Trail Activation",  callback_data="apex_set_trail_x")],
                [InlineKeyboardButton("🛑 Set Stop Loss %",       callback_data="apex_set_sl")],
                [InlineKeyboardButton("🪜 Set Ladder",            callback_data="apex_set_ladder")],
                [InlineKeyboardButton("⬅️ Back",                 callback_data="apex_menu")],
            ])
        )

    elif cb == "apex_profile_menu":
        profile = ud.get("apex_risk_profile", "default")
        def _tick(name): return "✅ " if profile == name else "      "
        await q.edit_message_text(
            "🎛 *APEX RISK PROFILE*\n\n"
            "⚖️ *Default*\n"
            "Balanced exits. Tight SL (18–22%). Trail from 1.5x.\n"
            "Best for consistent capital protection.\n\n"
            "🎯 *Hunter*\n"
            "Ratchet model — tight entry protection that STEPS UP as you profit.\n"
            "• Entry: -18% SL (same as Default — no wide stops on losers)\n"
            "• At 1.25x: floor moves to -5% from entry\n"
            "• At 1.50x: floor locks to break-even — *can no longer lose*\n"
            "• Trail: 22–28% wide once in profit zone\n"
            "• After 1.25x: needs 2× RED signal to exit (no wick-outs)\n"
            "Best for 10x–100x targets. Requires confidence 6+/10.\n\n"
            "_Active profile shown with_ ✅",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(_tick("default") + "⚖️ Default", callback_data="apex_profile_set_default")],
                [InlineKeyboardButton(_tick("hunter")  + "🎯 Hunter",  callback_data="apex_profile_set_hunter")],
                [InlineKeyboardButton("◀ Back", callback_data="apex_settings_menu")],
            ])
        )

    elif cb.startswith("apex_profile_set_"):
        new_profile = cb.replace("apex_profile_set_", "")
        if new_profile not in APEX_PROFILES:
            new_profile = "default"
        old_profile = ud.get("apex_risk_profile", "default")
        ud["apex_risk_profile"] = new_profile
        save_user(u.id, ud)
        _labels = {"default": "⚖️ Default", "hunter": "🎯 Hunter"}
        if old_profile == new_profile:
            _msg = "No change — already on *" + _labels.get(new_profile, new_profile) + "*."
        else:
            _msg = (
                "Switched to *" + _labels.get(new_profile, new_profile) + "*\n\n"
                + ("🎯 *Hunter active.*\n"
                   "Tight -20% SL protects entry.\n"
                   "Floor ratchets up at 1.3x and locks break-even at 1.6x.\n"
                   "Wide 30–38% trail runs once you're in profit."
                   if new_profile == "hunter" else
                   "⚖️ *Default active.* Standard balanced exits restored.")
            )
        await q.edit_message_text(
            "✅ *Profile Updated*\n\n" + _msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Back to Settings", callback_data="apex_settings_menu")],
                [InlineKeyboardButton("⚡ APEX Menu",        callback_data="apex_menu")],
            ])
        )

    elif cb == "apex_watchlist_view":
        watchlist = _apex_watchlist.get(u.id, {})
        import time as _wlts
        now_ts = _wlts.time()
        if not watchlist:
            await q.edit_message_text(
                "\U0001f441 *APEX WATCHLIST*\n\n"
                "No tokens being watched right now.\n\n"
                "Whenever APEX exits a position the token is automatically added here "
                "and monitored for reversal signals for up to 12 hours.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u25c0 Back", callback_data="apex_menu")]])
            )
            return
        lines = ["\U0001f441 *APEX WATCHLIST*\n", "Monitoring *" + str(len(watchlist)) + "* token(s) for reversal\n", "\u2500\u2500\u2500\u2500\u2500\u2500\n"]
        status_icons = {"watching": "\U0001f535", "reversed": "\u2705", "dead": "\U0001f480", "expired": "\u23f0"}
        for contract, rec in list(watchlist.items())[:8]:
            age_h     = round((now_ts - rec.get("exit_at", now_ts)) / 3600, 1)
            expires_h = round(max(0, (WATCHLIST_EXPIRY_S - (now_ts - rec.get("exit_at", now_ts))) / 3600), 1)
            icon      = status_icons.get(rec.get("status","watching"), "\U0001f535")
            lines.append(icon + " *$" + rec.get("symbol","?") + "*  Exit: *" + str(rec.get("exit_x","?")) + "x*  (" + rec.get("exit_reason","?").replace("apex_","").replace("_"," ") + ")\n  Age: *" + str(age_h) + "h*  \u00b7  Expires in: *" + str(expires_h) + "h*\n")
        await q.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f5d1 Clear Watchlist", callback_data="apex_watchlist_clear")],
                [InlineKeyboardButton("\u25c0 Back",                callback_data="apex_menu")],
            ])
        )

    elif cb == "apex_watchlist_clear":
        _apex_watchlist.pop(u.id, None)
        await q.answer("\u2705 Watchlist cleared", show_alert=True)
        await q.edit_message_text(
            "\U0001f441 *APEX WATCHLIST*\n\nWatchlist cleared.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u25c0 Back", callback_data="apex_menu")]])
        )

    elif cb == "apex_vault_menu":
        vault        = ud.get("apex_vault", 0.0)
        vault_peak   = ud.get("apex_vault_peak", 0.0)
        vault_pnl    = ud.get("apex_vault_pnl", 0.0)
        profit_sent  = ud.get("apex_vault_total_profit_sent", 0.0)
        split_pct    = ud.get("apex_vault_profit_split", 0.50)
        balance      = ud.get("balance", 0.0)
        buy_amt_est  = ud.get("sniper_filters", {}).get("buy_amount", 20.0)
        trades_left  = int(vault / buy_amt_est) if buy_amt_est > 0 and vault > 0 else 0
        open_val     = sum(
            h.get("total_invested", 0) for h in ud.get("holdings", {}).values()
            if h.get("mood") in ("APEX", "AI-Sniper", "APEX-DCA")
        )
        vault_status = "\U0001f7e2 Active" if vault >= buy_amt_est else "\U0001f534 Insufficient \u2014 fund to resume"
        kb = [
            [InlineKeyboardButton("\U0001f4b0 Fund Vault from Balance", callback_data="apex_vault_fund")],
            [InlineKeyboardButton("\U0001f4b8 Withdraw to Balance",     callback_data="apex_vault_withdraw")],
            [InlineKeyboardButton("\U0001f4b0 Withdraw All",            callback_data="apex_vault_withdraw_all")],
            [InlineKeyboardButton("\u2699\ufe0f Set Profit Split %",   callback_data="apex_vault_split_set")],
            [InlineKeyboardButton("\u2b05\ufe0f Back",                 callback_data="apex_menu")],
        ]
        await q.edit_message_text(
            "\U0001f3e6 *APEX VAULT*\n"
            "_APEX\'s dedicated trading capital_\n\n"
            "\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\n"
            "\U0001f4b0 *Vault Balance:* " + money(vault) + "\n"
            "\U0001f4ca *In open positions:* " + money(open_val) + "\n"
            "\U0001f3c6 *Vault peak:* " + money(vault_peak) + "\n\n"
            "\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\n"
            "\U0001f4c8 *All-time vault PnL:* " + pstr(vault_pnl) + "\n"
            "\U0001f4b8 *Sent to main balance:* " + money(profit_sent) + "\n"
            "\U0001f501 *Profit split:* *" + str(int(split_pct * 100)) + "%* to main  |  *" + str(int((1 - split_pct) * 100)) + "%* stays in vault\n\n"
            "\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\n"
            "\U0001f4b5 *Main balance:* " + money(balance) + "\n"
            "\U0001f3af *Est. trades remaining:* *" + str(trades_left) + "* at " + money(buy_amt_est) + "/trade\n"
            "Status: " + vault_status + "\n\n"
            + ("_\U0001f7e2 Vault funded \u2014 APEX will trade when sniper finds a qualifying token._\n" if vault >= buy_amt_est else "_Fund vault from your main balance to start APEX trading._\n")
            + "_On every profitable exit: " + str(int(split_pct*100)) + "% of profit \u2192 main balance._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    elif cb == "apex_vault_fund":
        balance = ud.get("balance", 0.0)
        if balance < 1.0:
            await q.edit_message_text(
                "\U0001f3e6 *FUND VAULT*\n\nYour main balance is empty \u2014 nothing to transfer.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="apex_vault_menu")]])
            )
            return
        pending[u.id] = {"action": "apex_vault_fund_amt", "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "\U0001f3e6 *FUND APEX VAULT*\n\n"
            "Main balance: *" + money(balance) + "*\n"
            "Current vault: *" + money(ud.get("apex_vault", 0.0)) + "*\n\n"
            "Enter amount to transfer from main balance \u2192 vault:\n"
            "_(e.g. 50, 200, or type ALL for everything)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u274c Cancel", callback_data="apex_vault_menu")]])
        )
    elif cb == "apex_vault_split_set":
        cur_split = ud.get("apex_vault_profit_split", 0.50)
        await q.edit_message_text(
            "\u2699\ufe0f *SET PROFIT SPLIT*\n\n"
            "Current: *" + str(int(cur_split * 100)) + "%* of each trade\'s profit \u2192 main balance\n\n"
            "Choose what % of each trade\'s profit goes to your main balance.\n"
            "The rest stays in vault to compound.\n\n"
            "Examples:\n"
            "  25% \u2192 $10 profit = $2.50 to balance, $7.50 stays in vault\n"
            "  50% \u2192 $10 profit = $5.00 to balance, $5.00 stays in vault\n"
            "  75% \u2192 $10 profit = $7.50 to balance, $2.50 stays in vault",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("25% to balance", callback_data="apex_vault_split_25"),
                 InlineKeyboardButton("50% to balance", callback_data="apex_vault_split_50")],
                [InlineKeyboardButton("75% to balance", callback_data="apex_vault_split_75"),
                 InlineKeyboardButton("100% to balance",callback_data="apex_vault_split_100")],
                [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="apex_vault_menu")],
            ])
        )
    elif cb.startswith("apex_vault_split_"):
        pct_str = cb.replace("apex_vault_split_", "")
        try:
            pct = int(pct_str) / 100
            ud["apex_vault_profit_split"] = pct
            save_user(u.id, ud)
            await q.edit_message_text(
                "\u2705 *PROFIT SPLIT UPDATED*\n\n"
                "On every profitable APEX exit:\n"
                "  *" + pct_str + "%* of profit \u2192 Main Balance\n"
                "  *" + str(100 - int(pct_str)) + "%* of profit \u2192 stays in Vault\n\n"
                "_Takes effect on the next closed position._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="apex_vault_menu")]])
            )
        except Exception:
            await q.answer("Invalid split value", show_alert=True)
    elif cb == "apex_vault_withdraw":
        vault = ud.get("apex_vault", 0.0)
        if vault < 0.01:
            await q.edit_message_text(
                "\U0001f3e6 *APEX VAULT*\n\nVault is empty.\n\nFund it from your main balance to start APEX trading.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="apex_vault_menu")]])
            )
            return
        pending[u.id] = {"action": "apex_vault_withdraw_amt", "_prompt_msg_id": q.message.message_id}
        await q.edit_message_text(
            "\U0001f3e6 *VAULT WITHDRAWAL*\n\n"
            "Vault balance: *" + money(vault) + "*\n\n"
            "Enter amount to withdraw to your main balance:\n"
            "_(e.g. 50, 200.50, or type MAX for everything)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u274c Cancel", callback_data="apex_vault_menu")]])
        )
    elif cb == "apex_vault_withdraw_all":
        vault = ud.get("apex_vault", 0.0)
        if vault < 0.01:
            await q.edit_message_text(
                "\U0001f3e6 *APEX VAULT*\n\nVault is empty.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="apex_vault_menu")]])
            )
            return
        ud["balance"]    = round(ud.get("balance", 0.0) + vault, 4)
        ud["apex_vault"] = 0.0
        ud.pop("_apex_vault_low_notified", None)
        # Vault is now empty — auto-disable APEX
        _apex_was_on2 = ud.get("apex_mode", False)
        if _apex_was_on2:
            ud["apex_mode"] = False
        save_user(u.id, ud)
        await q.edit_message_text(
            "\u2705 *VAULT WITHDRAWN*\n\n"
            "Moved *" + money(vault) + "* from vault \u2192 main balance.\n\n"
            "\U0001f4b5 Main Balance: *" + money(ud["balance"]) + "*\n"
            "\U0001f3e6 Vault: *$0.00*\n\n"
            + ("\U0001f534 *APEX auto-disabled* — vault is empty.\n"
               "_Fund the vault and re-enable APEX to resume._"
               if _apex_was_on2 else
               "\u26a0\ufe0f APEX will not auto-trade until you fund the vault again."),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="apex_vault_menu")]])
        )


async def apex_checker_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Adaptive APEX position checker — parallel execution with per-user timeout.
    CLEAR threat  → 15s interval (saves credits)
    YELLOW threat → 8s interval
    ORANGE/RED    → 4s interval (maximum urgency)

    FIX: Users checked in parallel via asyncio.gather. Each user has a 7.5s
    hard timeout (up from 6s — position manager was consistently timing out).
    """
    import time as _t
    now = _t.time()
    try:
        # ── Build list of users that actually need checking this cycle ────────
        active_users = []
        for uid, ud in list(users.items()):
            holdings = ud.get("holdings", {})
            apex_holdings = {
                c: h for c, h in holdings.items()
                if h.get("mood") in ("APEX", "AI-Sniper", "APEX-DCA")
                and h.get("amount", 0) > 0
            }
            if not apex_holdings:
                continue

            user_checks = _apex_last_check.setdefault(uid, {})
            positions_due = []
            for contract, h in apex_holdings.items():
                threat   = h.get("apex_threat", "CLEAR")
                last_chk = user_checks.get(contract, 0)
                elapsed  = now - last_chk
                if threat in ("RED", "ORANGE"):   interval = 4
                elif threat == "YELLOW":          interval = 8
                else:                             interval = 15
                if elapsed >= interval:
                    positions_due.append(contract)

            if positions_due:
                active_users.append((uid, ud, positions_due))

        # ── Run ALL users in parallel with a hard 7.5s timeout per user ──────
        async def _check_user(uid, ud, positions_due):
            try:
                await _asyncio.wait_for(
                    apex_run_position_manager(ctx.application, uid, ud,
                                             positions_due=positions_due),
                    timeout=7.5
                )
                for contract in positions_due:
                    _apex_last_check[uid][contract] = now
            except _asyncio.TimeoutError:
                logger.warning(f"apex_checker_job: uid={uid} timed out — positions retry next cycle")
            except Exception as _ue:
                logger.warning(f"apex_checker_job: uid={uid} error: {_ue}")

        if active_users:
            await _asyncio.gather(*[
                _check_user(uid, ud, positions_due)
                for uid, ud, positions_due in active_users
            ])

        # ── Re-entry watchlist checker ─────────────────────────────────────────
        try:
            await _asyncio.wait_for(apex_watchlist_checker(ctx.application), timeout=3.0)
        except Exception as _wlce:
            logger.debug(f"Watchlist checker error: {_wlce}")

        # ── Post-exit snapshot collector ──────────────────────────────────────
        for _uid2, _ud2 in list(users.items()):
            if _apex_post_exit.get(_uid2):
                try:
                    await _asyncio.wait_for(
                        apex_post_exit_tracker_run(ctx.application, _uid2),
                        timeout=3.0
                    )
                except Exception as _pete:
                    logger.debug(f"Post-exit tracker error {_uid2}: {_pete}")

        # ── APEX entry confirmation queue ─────────────────────────────────────
        for _uid2, _ud2 in list(users.items()):
            if _uid2 in _apex_entry_queue:
                try:
                    await _asyncio.wait_for(
                        apex_process_entry_queue(ctx.application, _uid2, _ud2),
                        timeout=3.0
                    )
                except Exception as _qe:
                    logger.error(f"APEX entry queue error {_uid2}: {_qe}", exc_info=True)

    except Exception as e:
        logger.error(f"apex_checker_job crashed: {e}", exc_info=True)



# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL — inserted before main()
# ══════════════════════════════════════════════════════════════════════════════

_admin_sniper_paused = False


def _admin_guard(uid):
    return uid in ADMIN_IDS


def _fmt_vault(ud):
    return round(ud.get("apex_vault", 0.0) or 0.0, 2)


def _fmt_bal(ud):
    return round(ud.get("balance", 0.0) or 0.0, 2)


def _fmt_pnl(ud):
    return round(ud.get("apex_daily_pnl", 0.0) or 0.0, 2)


def _admin_pos_count(ud):
    return sum(
        1 for h in ud.get("holdings", {}).values()
        if h.get("mood") in ("APEX", "AI-Sniper", "APEX-DCA")
    )


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global APEX_HUNTER_SUSPENDED, _admin_sniper_paused
    u   = update.effective_user
    uid = u.id
    if not _admin_guard(uid):
        await update.message.reply_text("Unauthorised.")
        return

    args = ctx.args or []
    sub  = args[0].lower() if args else ""

    # /admin  — dashboard
    if not sub:
        tu  = len(users)
        au  = sum(1 for ud in users.values() if ud.get("apex_mode") or ud.get("sniper_mode"))
        tp  = sum(_admin_pos_count(ud) for ud in users.values())
        tv  = sum(_fmt_vault(ud) for ud in users.values())
        hs  = "SUSPENDED" if APEX_HUNTER_SUSPENDED else "ACTIVE"
        ss  = "PAUSED" if _admin_sniper_paused else "RUNNING"
        msg = (
            "*APEX ADMIN PANEL*\n\n"
            "Users: *" + str(tu) + "* (" + str(au) + " active)\n"
            "Open positions: *" + str(tp) + "*\n"
            "Total vault: *$" + str(tv) + "*\n"
            "Hunter: *" + hs + "*\n"
            "Sniper: *" + ss + "*\n\n"
            "*Commands:*\n"
            "/admin stats\n"
            "/admin users\n"
            "/admin user <uid>\n"
            "/admin credit <uid> <amt>\n"
            "/admin debit <uid> <amt>\n"
            "/admin ban <uid>\n"
            "/admin unban <uid>\n"
            "/admin broadcast <msg>\n"
            "/admin pause sniper\n"
            "/admin resume sniper\n"
            "/admin hunter on|off\n"
            "/admin resetsl <uid> <ca>"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # /admin stats
    if sub == "stats":
        tu  = len(users)
        au  = sum(1 for ud in users.values() if ud.get("apex_mode"))
        su  = sum(1 for ud in users.values() if ud.get("sniper_mode"))
        tp  = sum(_admin_pos_count(ud) for ud in users.values())
        ti  = sum(
            sum(h.get("total_invested", 0) or 0 for h in ud.get("holdings", {}).values())
            for ud in users.values()
        )
        tv  = sum(_fmt_vault(ud) for ud in users.values())
        tpnl = sum(_fmt_pnl(ud) for ud in users.values())
        hs  = "SUSPENDED" if APEX_HUNTER_SUSPENDED else "ACTIVE"
        ss  = "PAUSED" if _admin_sniper_paused else "RUNNING"
        msg = (
            "*LIVE BOT STATS*\n\n"
            "Total users: *" + str(tu) + "*\n"
            "APEX users: *" + str(au) + "*\n"
            "Sniper users: *" + str(su) + "*\n"
            "Open positions: *" + str(tp) + "*\n"
            "Total invested: *$" + str(round(ti, 2)) + "*\n"
            "Total vault: *$" + str(round(tv, 2)) + "*\n"
            "Today PnL: *$" + str(round(tpnl, 2)) + "*\n"
            "Hunter: *" + hs + "*\n"
            "Sniper: *" + ss + "*"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # /admin users
    if sub == "users":
        lines_out = []
        for uid2, ud2 in sorted(users.items()):
            uname  = ud2.get("username", str(uid2))
            vault  = _fmt_vault(ud2)
            bal    = _fmt_bal(ud2)
            pnl    = _fmt_pnl(ud2)
            pos    = _admin_pos_count(ud2)
            banned = "BANNED " if ud2.get("admin_banned") else ""
            lines_out.append(
                banned + "*" + str(uname) + "* (`" + str(uid2) + "`)"
                + "  Bal:$" + str(bal)
                + " Vault:$" + str(vault)
                + " PnL:$" + str(pnl)
                + " Pos:" + str(pos)
            )
        if not lines_out:
            await update.message.reply_text("No users.")
            return
        chunk = ""
        for line in lines_out:
            if len(chunk) + len(line) > 3800:
                await update.message.reply_text(chunk, parse_mode="Markdown")
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        return

    # /admin user <uid>
    if sub == "user":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Usage: /admin user <uid>")
            return
        target = int(args[1])
        ud2 = users.get(target)
        if not ud2:
            await update.message.reply_text("User not found: " + str(target))
            return
        pos_lines = ""
        for c, h in ud2.get("holdings", {}).items():
            if h.get("mood") in ("APEX", "AI-Sniper", "APEX-DCA"):
                pos_lines += (
                    "  $" + str(h.get("symbol", "?"))
                    + " SL:" + str(h.get("stop_loss_pct", "?")) + "%"
                    + " Inv:$" + str(round(h.get("total_invested", 0) or 0, 2)) + "\n"
                )
        msg = (
            "*User: " + str(ud2.get("username", "?")) + "* (`" + str(target) + "`)\n\n"
            "Balance: *$" + str(_fmt_bal(ud2)) + "*\n"
            "Vault: *$" + str(_fmt_vault(ud2)) + "*\n"
            "Daily PnL: *$" + str(_fmt_pnl(ud2)) + "*\n"
            "All-time PnL: *$" + str(round(ud2.get("realized_pnl", 0) or 0, 2)) + "*\n"
            "APEX: *" + ("ON" if ud2.get("apex_mode") else "OFF") + "*\n"
            "Sniper: *" + ("ON" if ud2.get("sniper_mode") else "OFF") + "*\n"
            "Profile: *" + str(ud2.get("apex_risk_profile", "default")) + "*\n"
            "Banned: *" + ("YES" if ud2.get("admin_banned") else "no") + "*\n"
            "Positions:\n" + (pos_lines if pos_lines else "  none")
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # /admin credit <uid> <amount>
    if sub == "credit":
        if len(args) < 3 or not args[1].isdigit():
            await update.message.reply_text("Usage: /admin credit <uid> <amount>")
            return
        target = int(args[1])
        try:
            amount = float(args[2])
        except ValueError:
            await update.message.reply_text("Amount must be a number.")
            return
        ud2 = users.get(target)
        if not ud2:
            await update.message.reply_text("User not found: " + str(target))
            return
        old_v = _fmt_vault(ud2)
        ud2["apex_vault"] = round(old_v + amount, 4)
        save_user(target, ud2)
        await update.message.reply_text(
            "Credited $" + str(amount) + " to " + str(target)
            + "\nVault: $" + str(old_v) + " -> $" + str(ud2["apex_vault"])
        )
        try:
            await ctx.bot.send_message(
                chat_id=target,
                text="$" + str(amount) + " credits added to your APEX vault!\nNew vault: $" + str(ud2["apex_vault"]),
                reply_markup=main_menu_kb()
            )
        except Exception:
            pass
        return

    # /admin debit <uid> <amount>
    if sub == "debit":
        if len(args) < 3 or not args[1].isdigit():
            await update.message.reply_text("Usage: /admin debit <uid> <amount>")
            return
        target = int(args[1])
        try:
            amount = float(args[2])
        except ValueError:
            await update.message.reply_text("Amount must be a number.")
            return
        ud2 = users.get(target)
        if not ud2:
            await update.message.reply_text("User not found: " + str(target))
            return
        old_v = _fmt_vault(ud2)
        ud2["apex_vault"] = round(max(0.0, old_v - amount), 4)
        save_user(target, ud2)
        await update.message.reply_text(
            "Debited $" + str(amount) + " from " + str(target)
            + "\nVault: $" + str(old_v) + " -> $" + str(ud2["apex_vault"])
        )
        return

    # /admin ban <uid>
    if sub == "ban":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Usage: /admin ban <uid>")
            return
        target = int(args[1])
        ud2 = users.get(target)
        if not ud2:
            await update.message.reply_text("User not found: " + str(target))
            return
        ud2["admin_banned"] = True
        ud2["apex_mode"]    = False
        ud2["sniper_mode"]  = False
        save_user(target, ud2)
        await update.message.reply_text("Banned user " + str(target) + ". APEX and sniper disabled.")
        return

    # /admin unban <uid>
    if sub == "unban":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Usage: /admin unban <uid>")
            return
        target = int(args[1])
        ud2 = users.get(target)
        if not ud2:
            await update.message.reply_text("User not found: " + str(target))
            return
        ud2["admin_banned"] = False
        save_user(target, ud2)
        await update.message.reply_text("Unbanned user " + str(target) + ".")
        return

    # /admin approve <uid>
    if sub == "approve":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Usage: /admin approve <uid>")
            return
        target = int(args[1])
        ud2 = users.get(target)
        if not ud2:
            ud2 = get_user(target, str(target))
        ud2["access_approved"] = True
        save_user(target, ud2)
        _pending_access.pop(target, None)
        try:
            await ctx.bot.send_message(
                chat_id=target, parse_mode="Markdown",
                text="✅ *Access Granted!*\n\nWelcome to *APEX SNIPER BOT*.\nType /start to begin."
            )
        except Exception:
            pass
        await update.message.reply_text("✅ Approved user " + str(target) + ".")
        return

    # /admin pending — list users waiting for approval
    if sub == "pending":
        if not _pending_access:
            await update.message.reply_text("No pending access requests.")
            return
        lines_p = ["*PENDING ACCESS REQUESTS*\n"]
        for pid, info in _pending_access.items():
            lines_p.append(
                "• " + info.get("name","?") + " " + info.get("username","") +
                " — `" + str(pid) + "`\n  /admin approve " + str(pid)
            )
        await update.message.reply_text("\n".join(lines_p), parse_mode="Markdown")
        return


    if sub == "broadcast":
        if len(args) < 2:
            await update.message.reply_text("Usage: /admin broadcast <message>")
            return
        msg_text = " ".join(args[1:])
        sent = 0
        failed = 0
        for uid2 in list(users.keys()):
            try:
                await ctx.bot.send_message(
                    chat_id=uid2,
                    text="APEX Announcement:\n\n" + msg_text,
                    reply_markup=main_menu_kb()
                )
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(
            "Broadcast sent to " + str(sent) + " users. Failed: " + str(failed)
        )
        return

    # /admin pause sniper
    if sub == "pause":
        if len(args) > 1 and args[1].lower() == "sniper":
            _admin_sniper_paused = True
            await update.message.reply_text("Sniper scanning PAUSED globally.")
        else:
            await update.message.reply_text("Usage: /admin pause sniper")
        return

    # /admin resume sniper
    if sub == "resume":
        if len(args) > 1 and args[1].lower() == "sniper":
            _admin_sniper_paused = False
            await update.message.reply_text("Sniper scanning RESUMED.")
        else:
            await update.message.reply_text("Usage: /admin resume sniper")
        return

    # /admin hunter on|off
    if sub == "hunter":
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            await update.message.reply_text("Usage: /admin hunter on|off")
            return
        APEX_HUNTER_SUSPENDED = (args[1].lower() == "off")
        status = "SUSPENDED" if APEX_HUNTER_SUSPENDED else "ACTIVE"
        await update.message.reply_text("Hunter mode: " + status)
        return

    # /admin resetsl <uid> <contract>
    if sub == "resetsl":
        if len(args) < 3 or not args[1].isdigit():
            await update.message.reply_text("Usage: /admin resetsl <uid> <contract>")
            return
        target   = int(args[1])
        contract = args[2]
        ud2 = users.get(target)
        if not ud2:
            await update.message.reply_text("User not found: " + str(target))
            return
        h = ud2.get("holdings", {}).get(contract)
        if not h:
            await update.message.reply_text("Contract not found in holdings.")
            return
        rug    = h.get("apex_entry_rug", "LOW")
        new_sl = {"LOW": 22.0, "MEDIUM": 18.0, "HIGH": 12.0}.get(rug, 20.0)
        h["stop_loss_pct"]     = new_sl
        h["apex_hunter_floor"] = 0.0
        h["apex_peak_price"]   = h.get("avg_price", 0)
        h["apex_trail_stop"]   = None
        h["apex_threat"]       = "CLEAR"
        save_user(target, ud2)
        await update.message.reply_text(
            "Reset SL on " + contract[:20] + "\nNew SL: " + str(new_sl) + "% | Threat: CLEAR"
        )
        return

    await update.message.reply_text("Unknown command: " + sub + ". Type /admin for help.")



async def dump_log_drain_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Drains the dump log queue at DL_BATCH_SIZE messages per user every DL_DRAIN_EVERY seconds.
    Rate: 3 messages / 4s = ~45/min — safely under Telegram's ~20/min channel limit.
    A 100-token cycle delivers in ~2.5 minutes.

    Flood control handling:
    - On 429 flood: reads retry_after from error, pauses that user's drain until delay expires
    - Flood errors do NOT count toward the permanent-disable threshold
    - Only genuine send failures (wrong channel, bot banned, etc.) count
    """
    if not _dl_queue:
        return
    now_ts = _time.time()
    for uid, queue in list(_dl_queue.items()):
        if not queue:
            _dl_queue.pop(uid, None)
            continue
        # Skip this user if still in flood-control cooldown
        if now_ts < _dl_flood_until.get(uid, 0):
            continue
        ud = users.get(uid)
        sent = 0
        # Use ud to persist fail count across drain cycles so 3 failures
        # across separate cycles correctly trigger the disable logic.
        if ud is not None:
            real_fails = ud.get("_dl_fail_count", 0)
        else:
            real_fails = 0
        while queue and sent < DL_BATCH_SIZE:
            msg = queue[0]   # peek — only pop after success
            try:
                await ctx.bot.send_message(
                    chat_id=msg["chat_id"],
                    text=msg["text"],
                    disable_notification=msg.get("disable_notification", False),
                )
                queue.popleft()
                sent += 1
                real_fails = 0
                if ud:
                    ud.pop("_dl_fail_count", None)
            except Exception as _drain_err:
                err_str = str(_drain_err)
                # ── Flood control: Telegram says wait N seconds ──────────────
                # Parse retry delay from error message e.g. "Retry in 38 seconds"
                import re as _re_flood
                _retry_match = _re_flood.search(r'[Rr]etry in (\d+)', err_str)
                if _retry_match or "flood" in err_str.lower() or "429" in err_str:
                    _wait = int(_retry_match.group(1)) + 2 if _retry_match else 45
                    _dl_flood_until[uid] = now_ts + _wait
                    logger.debug(f"Dump log flood control (uid={uid}): pausing {_wait}s, {len(queue)} queued")
                    break   # stop this cycle, retry after delay
                # ── Timeout: transient, retry next cycle ─────────────────────
                if "timed out" in err_str.lower() or "timeout" in err_str.lower():
                    logger.debug(f"Dump log timeout (uid={uid}): will retry next cycle")
                    break
                # ── Real failure: wrong channel ID, bot banned, etc. ─────────
                real_fails += 1
                if ud:
                    ud["_dl_fail_count"] = real_fails
                    save_user(uid, ud)   # persist after each failure so counter survives restarts
                logger.warning(f"Dump log send failed (uid={uid}, ch={msg.get('chat_id')}): {err_str}")
                if real_fails >= 3:
                    # Genuine channel problem — disable and notify user
                    queue.clear()
                    _dl_queue.pop(uid, None)
                    _dl_flood_until.pop(uid, None)
                    if ud:
                        ud["sniper_log_channel_on"] = False
                        ud.pop("_dl_fail_count", None)
                        save_user(uid, ud)
                        try:
                            await ctx.bot.send_message(
                                chat_id=uid, parse_mode="Markdown",
                                text=(
                                    "\u26a0\ufe0f *Scan Log Channel Disabled*\n\n"
                                    "The bot failed to post to your dump log channel 3 times.\n\n"
                                    "*Most common cause:* The bot is not an admin in the channel.\n\n"
                                    "Fix: Go to your channel \u2192 Add Admin \u2192 add this bot "
                                    "\u2192 enable *Post Messages*.\n\n"
                                    "Then re-connect in *Sniper \u2192 Scan Log Channel*.\n\n"
                                    "_Error: " + err_str[:120] + "_"
                                ),
                                reply_markup=main_menu_kb()
                            )
                        except Exception:
                            pass
                    break
                else:
                    break   # stop this cycle, retry next
        if queue:
            logger.debug(f"Dump log drain: {sent} sent for uid={uid}, {len(queue)} remaining")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.job_queue.run_repeating(checker_job,           interval=PRICE_CHECK_INTERVAL,       first=10)
    app.job_queue.run_repeating(apex_checker_job,      interval=APEX_PRICE_CHECK_INTERVAL,  first=15)
    app.job_queue.run_repeating(sniper_job,            interval=300,  first=60)
    app.job_queue.run_repeating(kol_tracker_job,       interval=300,  first=90)
    app.job_queue.run_repeating(channel_milestone_job, interval=300,  first=120)
    app.job_queue.run_repeating(autosave_job,          interval=120,  first=120)
    # dump_log_drain_job removed — scan log channel feature disabled
    app.job_queue.run_daily(daily_summary_job,           time=__import__("datetime").time(23, 59))
    app.job_queue.run_daily(monthly_report_job,          time=__import__("datetime").time(8,  0))
    app.job_queue.run_daily(weekly_filter_analysis_job,       time=__import__("datetime").time(9,  0))
    app.job_queue.run_daily(apex_auto_apply_suggestions_job,   time=__import__("datetime").time(0, 10))
    app.job_queue.run_repeating(rejected_token_outcome_job,    interval=3600, first=300)

    # APEX midnight self-calibration
    async def _apex_midnight_calibrate(ctx2):
        for _uid2, _ud2 in list(users.items()):
            if _ud2.get("apex_mode"):
                apex_self_calibrate(_ud2, _uid2, suggest_only=False)
    app.job_queue.run_daily(_apex_midnight_calibrate, time=__import__("datetime").time(0, 5))

    # Load all persisted user data before starting
    load_all(users, trade_log)

    # Warm APEX in-memory learning from persisted data
    _migrated_users = 0
    for _uid, _ud in users.items():
        _mem = _ud.get("apex_memory")
        if _mem:
            _apex_learn_memory[_uid] = list(_mem)
        # ── Restore in-memory dicts from ud so they survive restarts ──────────
        # _apex_watchlist, _apex_post_exit and _channel_calls are runtime state
        # that must survive Railway redeploys. They are saved into ud by the
        # autosave/exit paths and restored here on boot.
        _wl = _ud.get("_persisted_watchlist")
        if _wl:
            _apex_watchlist[_uid] = _wl
        # Restore rejected token tracker
        _rj = _ud.get("_persisted_rejected")
        if _rj:
            _apex_rejected[_uid] = _rj
        _pe = _ud.get("_persisted_post_exit")
        if _pe:
            _apex_post_exit[_uid] = _pe
        _cc = _ud.get("_persisted_channel_calls")
        if _cc:
            # milestones_hit was serialised as a list by _BotEncoder — restore to set
            for _contract, _cdata in _cc.items():
                if isinstance(_cdata.get("milestones_hit"), list):
                    _cdata["milestones_hit"] = set(_cdata["milestones_hit"])
            _channel_calls[_uid] = _cc
        # Restore pause timer — if it hasn't expired yet, re-apply it
        _pp = _ud.get("_persisted_paused_until")
        if _pp and isinstance(_pp, datetime) and _pp > datetime.now():
            _apex_paused_until[_uid] = _pp
        # Restore competitions from persisted data
        _pc = _ud.get("_persisted_competitions", {})
        for _cc3, _cdata in _pc.items():
            if _cc3 not in _competitions:
                _competitions[_cc3] = _cdata

        # ── One-time migration: fix old users with sub-standard APEX thresholds ──
        # Old accounts were created when APEX_MIN_CONFIDENCE=3 and score_min=30/35.
        # Those values are now stored in Supabase and override the corrected defaults.
        # This block forces them up to the correct minimums on every bot startup.
        # It is safe to run repeatedly — it only raises values, never lowers them.
        _changed = False
        if _ud.get("apex_learn_threshold", 6) < 6:
            _ud["apex_learn_threshold"] = 6
            _changed = True
        if _ud.get("apex_learn_score_min", 45) < 45:
            _ud["apex_learn_score_min"] = 45
            _changed = True
        # Also fix sniper max_mc if it was stored at the old 500K default
        _sf = _ud.get("sniper_filters", {})
        if _sf.get("max_mc", 0) > 100_000 and _sf.get("max_mc", 0) >= 500_000:
            _sf["max_mc"] = 100_000
            _changed = True
        if _changed:
            save_user(_uid, _ud)
            _migrated_users += 1

    if _migrated_users:
        logger.info(f"Startup migration: upgraded {_migrated_users} users to min confidence=6, score=45, max_mc=100K")

    # ── Initialize asyncio objects that require a running event loop ─────────
    async def _on_startup(application):
        global _rugcheck_semaphore
        _rugcheck_semaphore = _asyncio.Semaphore(2)
        logger.info("RugCheck semaphore initialized (max 2 concurrent).")
    app.post_init = _on_startup

    # Graceful HTTP client shutdown
    async def _on_shutdown(application):
        global _http
        if _http and not _http.is_closed:
            await _http.aclose()
            logger.info("HTTP client closed cleanly.")
    app.post_shutdown = _on_shutdown

    # ── Global error handler — logs all uncaught handler exceptions to Railway ──
    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Uncaught exception in handler:", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message and update.effective_user:
            try:
                await update.effective_message.reply_text(
                    "⚠️ Something went wrong. Please try again.",
                    reply_markup=main_menu_kb()
                )
            except Exception:
                pass
    app.add_error_handler(_error_handler)

    logger.info("APEX SNIPER BOT running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
