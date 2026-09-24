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


def http_get(url, timeout=20, attempts=3):
    """GET with retries for transient network errors (connection resets etc.)."""
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-daily-post/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # 4xx (except 429) are permanent - don't retry.
            if e.code not in (429,) and 400 <= e.code < 500:
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


def get_prices():
    """Return (bitcoin, ethereum) market dicts with 24h and 7d change in USD."""
    d = json.loads(http_get(COINGECKO))
    by_id = {c["id"]: c for c in d}
    return by_id["bitcoin"], by_id["ethereum"]


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


def get_news_headlines(limit=5):
    """Pull top headlines from the first working crypto RSS feed.

    Tries several feeds so a single dead/changed feed can't blank the news
    section (which is what happened when CoinDesk retired its RSS URL).
    """
    import xml.etree.ElementTree as ET

    for url in NEWS_FEEDS:
        try:
            xml = http_get(url, timeout=20)
            titles = ET.fromstring(xml).iter("item")
            out = []
            for it in titles:
                t = it.find("title")
                if t is not None and t.text and t.text.strip():
                    out.append(t.text.strip())
            if out:
                return out[:limit]
        except Exception:
            continue
    return []


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
        f"happening and why it matters. Use the most recent "
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


def main():
    # Post only at 07:00 UK time. The workflow fires at 06:00 + 07:00 UTC;
    # whichever lands on a 07:00 UK hour does the send, so it's DST-safe.
    allow_any = os.environ.get("ALLOW_ANY_HOUR") == "1"
    if ZoneInfo is not None and not allow_any:
        uk_hour = datetime.now(ZoneInfo("Europe/London")).hour
        if uk_hour != 7:
            print(f"Not 7 AM UK (hour={uk_hour}); skipping.")
            return
    fng_val, fng_label = get_fng()
    btc, eth = get_prices()
    headlines = get_news_headlines(8)
    if not headlines:
        headlines = ["No fresh headline available; keep the story section general but honest."]

    market = {
        "fng": {"value": fng_val},
        "prices": (
            f"BTC ${btc['current_price']:,.0f} "
            f"({btc['price_change_percentage_24h_in_currency']:+.1f}% 24h, "
            f"{btc['price_change_percentage_7d_in_currency']:+.1f}% 7d), "
            f"ETH ${eth['current_price']:,.0f} "
            f"({eth['price_change_percentage_24h_in_currency']:+.1f}% 24h, "
            f"{eth['price_change_percentage_7d_in_currency']:+.1f}% 7d)"
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
    header = (
        "\U0001F680 <b>Today's Crypto News: Plain &amp; Simple</b> \U0001F4C8\n"
        "Your daily update on the digital markets\n"
        "Date: " + uk_date
    )
    post = "====\n" + header + "\n\n" + body + "\n===="
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN - not sending. Rendered post:\n")
        print(post)
        return
    msg_id = send_telegram(post)
    print(f"OK sent, message_id={msg_id}")



if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)