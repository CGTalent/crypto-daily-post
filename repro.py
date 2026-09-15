#!/usr/bin/env python3
"""Reproduce the post generation + filtering to find where the header gets mangled."""
import os, sys, importlib.util, re

env_path = os.path.expanduser("~/.hermes/.env")
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))
os.environ["TELEGRAM_CHAT_ID"] = "1841368859"
os.environ["ALLOW_ANY_HOUR"] = "1"

spec = importlib.util.spec_from_file_location("post", "/tmp/cdp-work/post.py")
post = importlib.util.module_from_spec(spec)
spec.loader.exec_module(post)

# Build market data
fng_val, fng_label = post.get_fng()
btc, eth = post.get_prices()
market = {
    "fng": {"value": fng_val},
    "prices": (f"BTC ${btc['usd']:,.0f} ({btc['usd_24h_change']:+.1f}% 24h), "
               f"ETH ${eth['usd']:,.0f} ({eth['usd_24h_change']:+.1f}% 24h)"),
    "movers": "",
}
headlines = post.get_news_headlines()

raw = post.call_llm(market, headlines, fng_label)
print("===== RAW LLM OUTPUT =====")
print(repr(raw))
print("===== RENDERED =====")
print(raw)
print("===== AFTER FILTER+SPACING =====")
p = raw.strip("\n").strip()
p = re.sub(r"\n{3,}", "\n\n", p)
p = re.sub(r"([^\n])\n([^\n])", r"\1\n\n\2", p)
print(repr(p))
print("----- rendered -----")
print(p)