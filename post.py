#!/usr/bin/env python3
"""Daily Crypto Market Post for Chris Farrell.

Fetches live market data, writes a ready-to-paste post in Chris's voice
using an LLM, and sends it to Chris's Telegram DM. Chris copies it and
publishes from his own account. Runs in GitHub Actions (cloud), so it works
regardless of whether his Mac is on.

Secrets are injected via environment variables by the workflow:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENROUTER_API_KEY
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# The workflow fires at 06:00 and 07:00 UTC each morning; we post only when
# it is 07:00 UK local time, so the post goes out at 7 AM UK year-round (DST-safe).
if hasattr(time, "tzset"):
    os.environ["TZ"] = "Europe/London"
    time.tzset()

FNG_URL = "https://api.alternative.me/fng/"
# Price sources, tried in order. GitHub Actions runners are US-based, so Binance
# (HTTP 451 in the US) is unusable there, and CoinGecko now 403s intermittently.
# Coinbase and Kraken are US-accessible and need no API key.
COINBASE_STATS = "https://api.exchange.coinbase.com/products/{pair}/stats"
COINBASE_CANDLES = ("https://api.exchange.coinbase.com/products/{pair}/candles"
                    "?granularity=86400")
KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker?pair={pair}"
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440"
COINGECKO = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
             "&ids=bitcoin,ethereum&price_change_percentage=24h,7d")
MOVER_URL = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
             "&order=market_cap_desc&per_page=40&page=1&price_change_percentage=24h")
NEWS_FEEDS = [
    "https://watcher.guru/news/feed",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "deepseek/deepseek-v4-flash-0731"

# Rotating greetings - Chris wrote these himself and we cycle through them one
# per day, so the post opens with a different (warmer) hello every morning and
# repeats only after the whole list has been used. Keep his exact wording and
# emojis; never let the AI near these. Add new ones to the END of the list so
# the rotation order already published stays the same.
GREETINGS = [
    "Hello, Champions! \U0001F3C6",
    "Hello, Legends! \U0001F31F",
    "Hello, Superstars! \U00002B50",
    "Hello, Wonderful People!",
    "Hello, Dreamers! \U0001F4AD",
    "Hello, Achievers!",
    "Hello, Winners! \U0001F947",
    "Hello, Rockstars! \U0001F3B8",
    "Hello, Heroes! \U0001F9B8",
    "Hello, Trailblazers!",
    "Hello, Adventurers! \U0001F30D",
    "Hello, Beautiful Humans! \U00002764\U0000FE0F",
    "Hello, Good Eggs! \U0001F95A",
]
SUBLINE = "Here is your daily update - on the digital markets - explained simply!"
# Repo file recording the last day a post went out (guards against double-posting
# now that the send window is wider than one hour).
MARKER_PATH = "state/last_sent.txt"


def http_get(url, timeout=20, attempts=3):
    """GET with retries for transient network errors (connection resets etc.)."""
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-daily-post/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # 403 and 429 are usually rate-limiting, not permanent - retry them.
            # Other 4xx are permanent, so fail fast.
            if e.code not in (403, 429) and 400 <= e.code < 500:
                raise RuntimeError(f"GET {url} -> HTTP {e.code}") from e
            last = e
        except Exception as e:  # URLError, ConnectionResetError, timeout, ...
            last = e
        if i < attempts - 1:
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last}") from last


def get_fng():
    d = json.loads(http_get(FNG_URL))["data"][0]
    return d.get("value"), d.get("value_classification")


def _coinbase_prices():
    """(bitcoin, ethereum) with price, 24h % and 7d % - primary source."""
    out = {}
    for pair, key in (("BTC-USD", "bitcoin"), ("ETH-USD", "ethereum")):
        s = json.loads(http_get(COINBASE_STATS.format(pair=pair)))
        last, op = float(s["last"]), float(s["open"])
        candles = json.loads(http_get(COINBASE_CANDLES.format(pair=pair)))
        base = float(candles[7][4]) if len(candles) >= 8 else 0
        out[key] = {
            "price": last,
            "pct24h": (last / op - 1) * 100 if op else None,
            "pct7d": (last / base - 1) * 100 if base else None,
        }
    return out["bitcoin"], out["ethereum"]


def _kraken_prices():
    """Secondary source - same normalised shape."""
    out = {}
    for pair, key in (("XBTUSD", "bitcoin"), ("ETHUSD", "ethereum")):
        t = json.loads(http_get(KRAKEN_TICKER.format(pair=pair)))["result"]
        row = next(iter(t.values()))
        last, op = float(row["c"][0]), float(row["o"])
        ohlc = json.loads(http_get(KRAKEN_OHLC.format(pair=pair))).get("result") or {}
        series = next((v for k, v in ohlc.items() if k != "last"), [])
        base = float(series[-8][4]) if len(series) >= 8 else 0
        out[key] = {
            "price": last,
            "pct24h": (last / op - 1) * 100 if op else None,
            "pct7d": (last / base - 1) * 100 if base else None,
        }
    return out["bitcoin"], out["ethereum"]


def _coingecko_prices():
    """Last-resort source, same normalised shape."""
    d = json.loads(http_get(COINGECKO))
    by_id = {c["id"]: c for c in d}

    def norm(c):
        return {
            "price": float(c["current_price"]),
            "pct24h": float(c["price_change_percentage_24h_in_currency"]),
            "pct7d": float(c["price_change_percentage_7d_in_currency"]),
        }

    return norm(by_id["bitcoin"]), norm(by_id["ethereum"])


def get_prices():
    """Return (bitcoin, ethereum) dicts: {price, pct24h, pct7d}.

    Tries each source in turn so one provider going down (or geo-blocking the
    GitHub runner) can never kill the post.
    """
    errors = []
    for name, fn in (("Coinbase", _coinbase_prices),
                     ("Kraken", _kraken_prices),
                     ("CoinGecko", _coingecko_prices)):
        try:
            return fn()
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"price source {name} failed ({e})", file=sys.stderr)
    raise RuntimeError("all price sources failed - " + " | ".join(errors))


def get_movers():
    try:
        d = json.loads(http_get(MOVER_URL))
        d = [c for c in d if c.get("price_change_percentage_24h") is not None]
        gain = sorted(d, key=lambda c: -c["price_change_percentage_24h"])[:3]
        lose = sorted(d, key=lambda c: c["price_change_percentage_24h"])[:1]
        g = ", ".join(f"{c['symbol'].upper()} {c['price_change_percentage_24h']:.1f}%" for c in gain)
        l = ", ".join(f"{c['symbol'].upper()} {c['price_change_percentage_24h']:.1f}%" for c in lose)
        return f"Top gainers: {g}. Biggest faller: {l}."
    except Exception:
        return "Movers could not be fetched."


CRYPTO_TERMS = re.compile(
    r"\b(bitcoin|btc|ethereum|ether|eth|crypto|blockchain|stablecoin|altcoin|memecoin|"
    r"defi|web3|token|binance|coinbase|kraken|okx|bybit|solana|sol|xrp|ripple|dogecoin|"
    r"doge|cardano|nft|on-chain|satoshi|halving|mining|wallet|custody|digital asset|"
    r"digital assets|whale|airdrop|layer 2|memecoin)\b",
    re.IGNORECASE,
)


def get_news_headlines(limit=5):
    """Top crypto headlines, merged from every feed.

    Watcher Guru (Chris's preferred, fastest) is general finance, so its headlines
    are filtered to crypto-only - otherwise stock stories (Nvidia, Google) leak in.
    Other feeds are already crypto-only. Never again depends on one live feed.
    """
    import xml.etree.ElementTree as ET

    seen, crypto, fallback = set(), [], []
    for url in NEWS_FEEDS:
        try:
            xml = http_get(url, timeout=20)
            items = list(ET.fromstring(xml).iter("item"))
        except Exception:
            continue
        for it in items:
            t = it.find("title")
            if t is None or not t.text:
                continue
            title = t.text.strip()
            if not title or title in seen:
                continue
            seen.add(title)
            (crypto if CRYPTO_TERMS.search(title) else fallback).append(title)

    out = crypto[:limit]
    # If the crypto feeds gave us nothing at all, don't leave the section blank.
    if not out:
        out = fallback[:limit]
    return out


def call_llm(market_blob, headlines, fng_label, attempts=3):
    """Call OpenRouter and return the post body.

    Retries on transient/empty responses. Some models occasionally return a
    choice whose message has no `content` (reasoning-only or truncated reply),
    which used to crash the whole run - we now retry and fall back to the
    `reasoning` field before giving up.
    """
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    system = (
        "You are a friendly crypto market commentator writing for Chris Farrell's "
        "channel. Write ONLY the BODY paragraphs of a daily crypto update - do NOT "
        "include any title, subheadline, date line, '====' separator lines, or emojis "
        "flanking a title. The editor adds the header and frame separately. "
        "Separate each body paragraph with a blank line so the post breathes. "
        "Body structure, in order: (1) a punchy hook line; (2) the Fear and Greed reading "
        "with a one-line plain-English meaning; (3) Bitcoin and Ethereum price with BOTH their "
        "24-hour and 7-day change - give both figures for each coin, e.g. 'Bitcoin is at $X, "
        "up 2.1% in 24 hours and up 13.4% over the past week' (ONLY these two coins - never "
        "name any other token/coin); (4) the big story - explain what the MARKET did "
        "and WHY: what drove today's move (rallies or pullbacks). Focus on market drivers "
        "(flows, big buyers/sellers, macro, liquidations), never dull product, tech, probe or "
        "lawsuit stories; (5) a brief simple closing line that sums up the overall "
        "market mood, opening with 'That's today's snapshot'. "
        "TIMING - IMPORTANT: this post is published at 7am UK time, so everything you describe "
        "happened YESTERDAY or over the last 24 hours. Always use 'yesterday' or 'in the last 24 "
        "hours' when talking about what the market did or what the news was - NEVER say 'today' "
        "for the market action or the news, because at 7am today has barely started. "
        "(The date line still shows today's date, and the closing line 'That's today's snapshot' "
        "stays as it is.) "
        "PLAIN, RELATABLE ENGLISH - write for an ordinary person who is curious about crypto but "
        "has NO finance background. Never use industry jargon: derivatives, clearinghouse, "
        "custody, institutional flows, liquidity, basis, yield, counterparty, notional, "
        "compliance. If a technical term is truly unavoidable, explain it in everyday words "
        "right there in the same sentence. Always answer the reader's real question - 'what "
        "does this actually mean for me?' Say what the story means in human terms (e.g. not "
        "'CFTC approval of a derivatives clearinghouse', but 'America's market watchdog has "
        "given the green light for big money managers to trade crypto through a regulated US "
        "exchange - which makes crypto look safer and could bring billions of pounds in'). "
        "Prefer sport, shopping, everyday-life comparisons over financial vocabulary. "
        "Plain English, no jargon; explain any unfamiliar term right there (e.g. not just "
        "FOMC, but 'the FOMC, the Fed's rate-setting committee'). Use a healthy but not "
        "overloaded number of fun emojis (roughly 8-12 across the body, on-brand: \U0001F9E9 "
        "\U0001F4C8 \U0001F680 \U0001F440 \U0001F525 \U000026A1 \U0001F30C \U0001F4B0 \U0001F4B9 \U0001F511). "
        "Keep the whole body under ~150 words. NEVER invent numbers - use only the data "
        "given. Stay strictly neutral - never promote, recommend, or push any specific "
        "crypto, and never give buy/sell advice. Do NOT ask questions or invite replies; "
        "this is a pure snapshot. Use plain hyphens (-), never em-dashes, throughout."
    )
    user = (
        f"Today: {today}. Fear & Greed: {market_blob['fng']['value']} "
        f"({fng_label}). Bitcoin and Ethereum: {market_blob['prices']}. "
        f"{market_blob['movers']}\n\n"
        f"Below are today's fresh crypto news headlines. Look FIRST at how the market actually "
        f"moved over the last 24 hours (data above), then pick the headline that best explains "
        f"THAT move. Remember this is published at 7am, so refer to the move as 'yesterday' or "
        f"'in the last 24 hours' - never 'today'. If prices pulled back, explain WHY they pulled "
        f"back; if they rallied, explain what drove the rally. This section must be about the "
        f"MARKET - what it did and "
        f"the reason why - not about products, technology or infrastructure. "
        f"Strong drivers to look for: ETF or institutional flows, big buys or sells, a major "
        f"liquidation, macro news (rates, inflation, the Fed), a regulatory decision that moved "
        f"prices, a big exchange or protocol event, or a well-known name entering the market. "
        f"Do NOT pick abstract product/tech/upgrade stories (new tools, software releases, "
        f"research or engineering news) - they are dull and readers do not care. "
        f"AVOID dry legal/regulatory enforcement stories (probes, investigations, lawsuits, "
        f"sanctions, compliance, tax, court cases) unless they genuinely moved prices. "
        f"If NO headline clearly explains the move, then simply report what the market did and "
        f"give the closest real driver from the headlines - NEVER invent a cause that is not "
        f"in the headlines. "
        f"Explain it in simple English in 2-3 sentences so anyone can understand what is "
        f"happening and why it matters. Make it EXCITING and RELATABLE - everyday words, no "
        f"finance jargon, and say what it actually means for an ordinary person. Use the most recent "
        f"development about it (not an outdated preview - if a vote or event has already "
        f"happened, report its RESULT, not that it is 'coming up'). Do not mention it is 'later today' "
        f"unless the headlines say it is actually happening today.\n\n"
        f"Today's headlines:\n"
        + "\n".join(f"- {h}" for h in headlines)
    )
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.8,
        # Reasoning models spend part of this budget "thinking" before they write.
        # Too low and the answer comes back empty with finish_reason=length.
        "max_tokens": 4000,
    }
    last_err = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                OPENROUTER_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/CGTalent/crypto-daily-post",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read().decode())
            if d.get("error"):
                raise RuntimeError(f"OpenRouter error: {d['error']}")
            choices = d.get("choices") or []
            if not choices:
                raise RuntimeError(f"OpenRouter returned no choices: {str(d)[:200]}")
            msg = choices[0].get("message") or {}
            # Only accept the FINAL answer. The model sometimes returns an empty
            # `content` with the text parked in `reasoning` (its scratchpad) - that
            # is a failed generation, not a post, so we retry instead of using it.
            content = (msg.get("content") or "").strip()
            if not content:
                finish = choices[0].get("finish_reason")
                raise RuntimeError(f"empty content (finish_reason={finish})")
            return content
        except Exception as e:
            last_err = e
            print(f"LLM attempt {i + 1}/{attempts} failed: {e}", file=sys.stderr)
            if i < attempts - 1:
                time.sleep(3 * (i + 1))
    raise RuntimeError(f"LLM call failed after {attempts} attempts: {last_err}") from last_err


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
    }).encode()
    with urllib.request.urlopen(url, data=payload, timeout=30) as r:
        d = json.loads(r.read().decode())
    if not d.get("ok"):
        raise RuntimeError(f"Telegram send failed: {d}")
    return d["result"]["message_id"]


def _gh_headers():
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "crypto-daily-post/1.0"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _read_marker():
    """Read state/last_sent.txt from the repo. Returns (date, sha) or ('', None).

    Fails open: if the read fails we assume 'not sent yet' and post, because a
    duplicate is far better than a missing post.
    """
    import base64
    repo = os.environ.get("GITHUB_REPOSITORY", "CGTalent/crypto-daily-post")
    url = f"https://api.github.com/repos/{repo}/contents/{MARKER_PATH}"
    try:
        req = urllib.request.Request(url, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        return base64.b64decode(d.get("content") or "").decode().strip(), d.get("sha")
    except Exception as e:
        print(f"marker read failed ({e}) - assuming not sent", file=sys.stderr)
        return "", None


def _write_marker(value):
    """Record that today's post went out. Best-effort - never fatal."""
    import base64
    repo = os.environ.get("GITHUB_REPOSITORY", "CGTalent/crypto-daily-post")
    if not os.environ.get("GITHUB_TOKEN"):
        print("no GITHUB_TOKEN - cannot record sent marker", file=sys.stderr)
        return False
    _, sha = _read_marker()
    url = f"https://api.github.com/repos/{repo}/contents/{MARKER_PATH}"
    body = {"message": f"Mark crypto post sent for {value}",
            "content": base64.b64encode(value.encode()).decode()}
    if sha:
        body["sha"] = sha
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=_gh_headers(), method="PUT")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status in (200, 201)
    except Exception as e:
        print(f"marker write failed ({e})", file=sys.stderr)
        return False


def main():
    # Post at 7 AM UK. The workflow fires at 06:00 + 07:00 UTC; whichever lands
    # on 07:00 UK does the send, so it's DST-safe.
    #
    # GitHub sometimes delays scheduled runs by hours. A narrow "must be exactly
    # 07:00" gate then silently skips the day with no error at all (this happened
    # on 2026-10-04: the run fired at 12:22 UK and posted nothing). So instead we
    # accept any run from 07:00-12:59 UK and use a "sent today" marker, committed
    # to the repo, to guarantee exactly one post per day.
    allow_any = os.environ.get("ALLOW_ANY_HOUR") == "1"
    if ZoneInfo is not None:
        now_uk = datetime.now(ZoneInfo("Europe/London"))
    else:
        now_uk = datetime.now()
    today_uk = now_uk.strftime("%Y-%m-%d")
    if ZoneInfo is not None and not allow_any:
        uk_hour = now_uk.hour
        if uk_hour < 7:
            print(f"Too early (hour={uk_hour}); skipping.")
            return
        if uk_hour > 12:
            print(f"Too late (hour={uk_hour}); not sending a stale post.")
            return
        marker, _ = _read_marker()
        if marker == today_uk:
            print(f"Already sent today ({today_uk}); skipping.")
            return
        print(f"In window (hour={uk_hour}), not yet sent today - posting.")
    fng_val, fng_label = get_fng()
    btc, eth = get_prices()
    headlines = get_news_headlines(8)
    if not headlines:
        headlines = ["No fresh headline available; keep the story section general but honest."]

    market = {
        "fng": {"value": fng_val},
        "prices": (
            f"BTC ${btc['price']:,.0f} "
            f"({btc['pct24h']:+.1f}% 24h, {btc['pct7d']:+.1f}% 7d), "
            f"ETH ${eth['price']:,.0f} "
            f"({eth['pct24h']:+.1f}% 24h, {eth['pct7d']:+.1f}% 7d)"
        ),
        "movers": "",
    }
    body = call_llm(market, headlines, fng_label)
    # Guarantee clean spacing: blank line between every paragraph.
    import re
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = re.sub(r"([^\n])\n([^\n])", "\1\n\n\2", body)
    # HARD RULE: only Bitcoin and Ethereum may be mentioned. Drop any line naming
    # another token (movers / gainers / fallers / altcoin symbols).
    ALT_TOKENS = re.compile(
        r"\b(?:top movers|top gainer|biggest faller|biggest mover|UNI|XLM|HBAR|SOL|DOGE|"
        r"ADA|XRP|BNB|AVAX|LINK|MATIC|POLY|DOT|LTC|TRX|TON|SHIB|PEPE|FIL|ATOM|NEAR|APT|"
        r"ARB|OP|INJ|SUI|SEI|TIA|WIF|BONK|RAIN|BTW|WLFI)\b",
        re.IGNORECASE,
    )
    filtered = [ln for ln in body.split("\n") if not (ln.strip() and ALT_TOKENS.search(ln))]
    body = "\n".join(filtered)
    body = re.sub(r"\n{2,}", "\n\n", body)
    # Build the exact header in code (guaranteed correct - cannot be mangled by the AI).
    uk_date = datetime.now(ZoneInfo("Europe/London")).strftime("%A %d %B %Y") if (ZoneInfo is not None) else datetime.now().strftime("%A %d %B %Y")
    # Rotate the greeting by calendar day, so every morning opens differently and
    # the same hello only comes round again once the whole list has been used.
    # Date-based (not random) so a re-run or a manual trigger can't change it.
    from datetime import date
    try:
        day_index = date.fromisoformat(today_uk).toordinal()
    except ValueError:
        day_index = datetime.now(timezone.utc).toordinal()
    greeting = GREETINGS[day_index % len(GREETINGS)]
    # Order Chris asked for: the hello comes FIRST, then the title block.
    header = (
        greeting + "\n"
        + SUBLINE + "\n\n"
        "\U0001F680 <b>Today's Crypto News: Plain &amp; Simple</b> \U0001F4C8\n"
        "Date: " + uk_date
    )
    post = "====\n" + header + "\n\n" + body + "\n===="
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN - not sending. Rendered post:\n")
        print(post)
        return
    msg_id = send_telegram(post)
    print(f"OK sent, message_id={msg_id}")
    # Record the send so a later run today can't post a duplicate.
    if not allow_any:
        _write_marker(today_uk)



if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)