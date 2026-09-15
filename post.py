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
COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"
MOVER_URL = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
             "&order=market_cap_desc&per_page=40&page=1&price_change_percentage=24h")
NEWS_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "deepseek/deepseek-v4-flash-0731"


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-daily-post/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GET {url} -> HTTP {e.code}") from e


def get_fng():
    d = json.loads(http_get(FNG_URL))["data"][0]
    return d.get("value"), d.get("value_classification")


def get_prices():
    d = json.loads(http_get(COINGECKO))
    return d["bitcoin"], d["ethereum"]


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
    """Pull top headlines from CoinDesk RSS (no key needed if parse works)."""
    try:
        import re
        import xml.etree.ElementTree as ET
        xml = http_get(NEWS_URL, timeout=20)
        ns = {"m": "http://purl.org/rss/1.0/modules/content/"}
        titles = ET.fromstring(xml).iter("item")
        out = []
        for it in titles:
            t = it.find("title")
            if t is not None and t.text:
                out.append(t.text.strip())
        return out[:limit]
    except Exception:
        return []


def call_llm(market_blob, headlines, fng_label):
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    system = (
        "You are a friendly crypto market commentator writing for Chris Farrell's "
        "channel. Write ONE post, ready to copy-paste. The post MUST open with a "
                "separator line of four equal signs, then this exact header on three "
                "lines (title line, then subhead line, then date line), the title in "
                "HTML bold and flanked by emojis on both sides:\n"
                "====\n"
                "\U0001F680 <b>Crypto Today, Plain &amp; Simple</b> \U0001F4C8\n"
                "Your daily update on the digital markets\n"
                "Date: [today's date, e.g. Monday 15 September 2026]\n"
                "Follow with a blank line, then the post body. Plain English, no jargon "
                                "(explain terms briefly). Use a healthy but not overloaded number of fun "
                                "emojis (roughly 8-12 across the whole post, on-brand: \U0001F9E9 \U0001F4C8 "
                                "\U0001F680 \U0001F440 \U0001F525 \U000026A1 \U0001F30C \U0001F4B0 \U0001F4B9 \U0001F511), "
                                "with 2-3 emojis placed around the title on the title line so the "
                                "header stands out (one before and after the bold title text). "
                                "Body structure: punchy hook line, "
        "Fear & Greed reading with a one-line plain meaning, BTC and ETH price+change, "
        "one or two movers, ONE interesting recent story (2-3 sentences, concrete), "
        "and a brief, simple closing line that just summarises the state of the market "
        "or the day ahead. End the post with a final separator line of four equal "
        "signs (====) on its own line after the closing sentence. Conversational, feels like a friend. Keep under "
        "~130 words (excluding the header). Never invent numbers; use only the data given. "
        "Fill in today's date in the Date: header line. IMPORTANT — stay strictly "
        "neutral and never sound like you are promoting or recommending any specific "
        "crypto or giving buy/sell advice. Do NOT name a single coin as something to "
        "buy, load up on, or chase. Do NOT ask questions or invite replies anywhere in the "
        "post — this is a pure update/snapshot, not a hook for comments. "
        "Never assume the reader knows jargon or specific terms: if you name anything that "
        "might be unfamiliar (e.g. the Clarity Act, FOMC, a regulation, an index), briefly "
        "explain what it is right there in simple English (e.g. not just FOMC, but 'the "
        "FOMC, the Fed's rate-setting committee'). "
        "Use plain hyphens (-), never em-dashes (—), throughout the entire post."
    )
    user = (
        f"Today: {today}. Fear & Greed: {market_blob['fng']['value']} "
        f"({fng_label}). Bitcoin and Ethereum: {market_blob['prices']}. "
        f"{market_blob['movers']}\n\n"
        f"Recent crypto headlines to draw a story from (quote no specific number unless present):\n"
        + "\n".join(f"- {h}" for h in headlines)
    )
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.8,
    }
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
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode())
    return d["choices"][0]["message"]["content"].strip()


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
    movers = get_movers()
    headlines = get_news_headlines()
    if not headlines:
        headlines = ["No fresh headline available; keep the story section general but honest."]

    market = {
        "fng": {"value": fng_val},
        "prices": (
            f"BTC ${btc['usd']:,.0f} ({btc['usd_24h_change']:+.1f}% 24h), "
            f"ETH ${eth['usd']:,.0f} ({eth['usd_24h_change']:+.1f}% 24h)"
        ),
        "movers": movers,
    }
    post = call_llm(market, headlines, fng_label)
    msg_id = send_telegram(post)
    print(f"OK sent, message_id={msg_id}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)