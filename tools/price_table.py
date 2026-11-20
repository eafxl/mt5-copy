"""Dump the real-price comparison from a trace file as a flat table.

WHY
    "The offset deviates too much" is unanswerable from a single number. Every
    measurement pairs two prices at one instant, and the question is always
    which of the two moved, when, and whether either side was alive at the
    time. That is a table, one row per cycle, with both prices, the instant
    they claim to describe, and the liveness of each feed beside them.

USAGE
    python3 tools/price_table.py traces/traces.json            # summary + tail
    python3 tools/price_table.py traces/traces.json --csv out.csv
    python3 tools/price_table.py traces/traces.json --from 21:00 --to 22:20
"""
import argparse
import csv
import datetime
import json
import statistics
import sys

COLUMNS = [
    "wall_utc",        # when the cycle ran
    "instant_local",   # the shared instant both prices are for
    "lookback_s",      # now - instant, the delay actually in use
    "tv_price",        # TradingView's value AT the instant
    "master_close",    # master M1 close AT the instant
    "raw",             # master_close - tv_price, this cycle
    "diff",            # the smoothed offset the UI shows
    "master_bid",      # master live bid, informational only
    "master_ask",
    "tv_age_s",        # age of the TV value used
    "master_age_s",    # age of the master value used
    "age_gap_s",       # the two above must agree; anything else is a fault
    "tv_pub_age_s",    # how long since TradingView published anything
    "mt_tick_age_s",   # how long since the master ticked at all
    "state",           # ok / warming / tv_missing / master_missing / stale...
]


def _rows(path):
    for line in open(path, errors="replace"):
        line = line.strip()
        if not line or '"price_calc"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("operation") == "price_calc":
            yield rec


def _state(t):
    flags = []
    if t.get("tv_warming"):
        flags.append("warming")
    elif t.get("tv_missing"):
        flags.append("tv_missing")
    if t.get("master_stale"):
        flags.append("master_shut")
    elif t.get("master_missing"):
        flags.append("master_missing")
    if t.get("stale") and not flags:
        flags.append("cached")
    return "+".join(flags) or "ok"


def build(path, start=None, end=None):
    out = []
    for rec in _rows(path):
        hhmm = rec["ts"][11:16]
        if start and hhmm < start:
            continue
        if end and hhmm > end:
            continue
        t = rec["tags"]
        at = t.get("common_at") or 0.0
        out.append({
            "wall_utc": rec["ts"][11:19],
            "instant_local": (
                datetime.datetime.fromtimestamp(at).strftime("%H:%M:%S")
                if at else ""
            ),
            "lookback_s": round(t.get("lookback_s", 0.0)),
            "tv_price": t.get("tv_price", 0.0),
            "master_close": t.get("master_at", 0.0),
            "raw": round(t.get("raw", 0.0), 2),
            "diff": round(t.get("diff", 0.0), 2),
            "master_bid": t.get("master_bid", 0.0),
            "master_ask": t.get("master_ask", 0.0),
            "tv_age_s": round(t.get("tv_age_s", 0.0)),
            "master_age_s": round(t.get("master_age_s", 0.0)),
            "age_gap_s": round(t.get("age_gap_s", 0.0)),
            "tv_pub_age_s": round(t.get("tv_published_age_s", 0.0)),
            "mt_tick_age_s": round(t.get("master_quote_age_s", 0.0)),
            "state": _state(t),
        })
    return out


def summarize(rows):
    ok = [r for r in rows if r["state"] == "ok"]
    print(f"rows {len(rows)}   measured {len(ok)}   "
          f"skipped {len(rows) - len(ok)}")
    if ok:
        raw = [r["raw"] for r in ok]
        print(f"raw offset: mean {statistics.mean(raw):.2f}  "
              f"sd {statistics.pstdev(raw):.2f}  "
              f"min {min(raw):.2f}  max {max(raw):.2f}")
    states = {}
    for r in rows:
        states[r["state"]] = states.get(r["state"], 0) + 1
    print("states: " + ", ".join(f"{k}={v}" for k, v in sorted(states.items())))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("trace", nargs="?", default="traces/traces.json")
    ap.add_argument("--csv", help="write the full table here")
    ap.add_argument("--from", dest="start", help="UTC HH:MM lower bound")
    ap.add_argument("--to", dest="end", help="UTC HH:MM upper bound")
    ap.add_argument("--tail", type=int, default=20, help="rows to print")
    args = ap.parse_args(argv)

    rows = build(args.trace, args.start, args.end)
    if not rows:
        print("no price_calc records in range", file=sys.stderr)
        return 1

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows)} rows to {args.csv}")

    summarize(rows)
    print()
    head = f"{'wall':<9}{'instant':<10}{'TV':>10}{'MT5':>10}{'raw':>8}{'bid now':>10}{'tvage':>7}{'mtage':>7}{'tick':>6}  state"
    print(head)
    print("-" * len(head))
    for r in rows[-args.tail:]:
        print(f"{r['wall_utc']:<9}{r['instant_local']:<10}{r['tv_price']:>10.2f}"
              f"{r['master_close']:>10.2f}{r['raw']:>8.2f}{r['master_bid']:>10.2f}"
              f"{r['tv_age_s']:>7}{r['master_age_s']:>7}{r['mt_tick_age_s']:>6}  {r['state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
