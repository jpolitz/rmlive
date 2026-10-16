"""Summarize an rmlive --prof session.

Usage: python3 tools/analyze_prof.py DIR
DIR holds rmlive-prof.jsonl (tablet: ticks, sends, pen) and
rmlive-client.jsonl (viewer: per-message timings), as pulled by `./run prof`.
Times are ms; pN = Nth percentile.
"""
import json
import sys


def pct(xs, ps=(50, 90, 99, 100)):
    xs = sorted(xs)
    if not xs:
        return "-"
    return " ".join(f"p{p}={xs[min(len(xs) - 1, int(len(xs) * p / 100))]:.1f}" for p in ps)


def main(d):
    ticks, sends, pens = [], [], []
    for line in open(f"{d}/rmlive-prof.jsonl"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue  # last line may be half-flushed
        {"tick": ticks, "send": sends, "pen": pens}[r["k"]].append(r)
    try:
        client = [r for line in open(f"{d}/rmlive-client.jsonl") for r in json.loads(line)]
    except FileNotFoundError:
        client = []
    if not ticks:
        print("no ticks logged (no viewer connected?)")
        return

    span = (ticks[-1]["ms"] - ticks[0]["ms"]) / 1000
    print(f"{len(ticks)} ticks over {span:.0f}s, {len(sends)} sends, "
          f"{len(pens)} pen up/down, {len(client)} viewer messages")

    print("\nTABLET, per poll tick")
    print("  read+hash  ", pct([t["read_us"] / 1000 for t in ticks]))
    print("  spacing    ", pct([b["ms"] - a["ms"] for a, b in zip(ticks, ticks[1:])]))
    changed = [t for t in ticks if t["dirty"]]
    print(f"  {len(changed)} ticks saw changes; tiles changed", pct([t["dirty"] for t in changed]))

    print("\nTABLET, per send (change seen -> written to socket)")
    big = [s for s in sends if s["tiles"] > 200]
    small = [s for s in sends if s["tiles"] <= 20]
    print(f"  small ({len(small)}): age", pct([s["age_us"] / 1000 for s in small]),
          "| encode", pct([s["enc_us"] / 1000 for s in small]))
    print(f"  big   ({len(big)}): age", pct([s["age_us"] / 1000 for s in big]),
          "| encode", pct([s["enc_us"] / 1000 for s in big]),
          "| KB", pct([s["z_b"] / 1024 for s in big]))
    print("  write      ", pct([s["write_us"] / 1000 for s in sends]))

    if client:
        print("\nVIEWER, per message")
        for k in ("lag_ms", "inflate_ms", "apply_ms", "paint_ms"):
            print(f"  {k[:-3]:10} ", pct([r[k] for r in client if k in r]))
        print("  backlog KB ", pct([r["backlog_b"] / 1024 for r in client]))

    lat = []
    for p in pens:
        if p["down"]:
            nxt = next((t for t in ticks if t["ms"] >= p["ms"] and t["dirty"]), None)
            if nxt:
                lat.append(nxt["ms"] - p["ms"])
    print("\nPEN: pen-down -> first tick that saw a change", pct(lat))


if __name__ == "__main__":
    main(sys.argv[1])
