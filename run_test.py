#!/usr/bin/env python3
"""Run post.py end-to-end for a real test (sends a DM to Chris's Telegram)."""
import os, re, importlib.util

# Load secrets from Chris's local .env (values never printed)
env_path = os.path.expanduser("~/.hermes/.env")
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))

os.environ["TELEGRAM_CHAT_ID"] = "1841368859"
os.environ["ALLOW_ANY_HOUR"] = "1"

spec = importlib.util.spec_from_file_location("post", "/tmp/crypto-daily-post/post.py")
post = importlib.util.module_from_spec(spec)
spec.loader.exec_module(post)

post.main()
print("DONE")