#!/usr/bin/env python3
"""Rebuild header assembly deterministically."""
import os, re, importlib.util

# Just test the header builder + body assembly logic in isolation (no LLM/net)
def build_header(date_str):
    title = "<b>Today's Crypto News: Plain &amp; Simple</b>"
    return (
        "\U0001F680 " + title + " \U0001F4C8\n"
        "Your daily update on the digital markets\n"
        "Date: " + date_str
    )

def assemble(body, date_str):
    header = build_header(date_str)
    body = body.strip("\n").strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = re.sub(r"([^\n])\n([^\n])", r"\1\n\n\2", body)
    return "====\n" + header + "\n\n" + body + "\n===="

sample_body = ("Markets are calm today, a cautious confident feel.\n"
               "Bitcoin is at $76,679.\nEthereum at $2,464.\n"
               "That's today's snapshot.")
out = assemble(sample_body, "Tuesday 15 September 2026")
print("=== FINAL ASSEMBLED ===")
print(out)
print("=== REPR ===")
print(repr(out))