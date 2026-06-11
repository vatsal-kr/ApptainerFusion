#!/usr/bin/env python3
"""Sweep client-side concurrency against a live sandbox server.

Replays the verl reward-loop access pattern (same /run_code payload shape,
same 30s client timeout) at increasing in-flight levels and reports
throughput / latency / timeout counts per level, so the training-side
`reward.sandbox_fusion.max_concurrent` can be chosen from data instead of
guesswork.

Usage (against a running server):
    python scripts/bench_concurrency.py --url http://localhost:8080/run_code \
        --levels 8,16,24,32,40,48,64,96

The verl training client runs 8 RewardLoopWorkers, each holding its own
`max_concurrent` semaphore, so total in-flight = 8 * max_concurrent.  The
recommendation at the end is already divided by 8.
"""

import argparse
import json
import random
import statistics
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

API_TIMEOUT = 10  # matches verl's buffer on top of compile+run timeouts
REWARD_WORKERS = 8  # RewardLoopWorker actors in the verl trainer


def make_workload(n, run_timeout, seed):
    """RL-like request mix. Weights approximate a mid-training batch:
    mostly fast correct/incorrect solutions, some CPU-bound ones, a tail of
    infinite loops (TLE) and runtime errors, plus big-stdin cases that
    exercise the historical drain path."""
    rng = random.Random(seed)
    items = []

    def salt():
        return "".join(rng.choices(string.ascii_lowercase, k=8))

    for _ in range(n):
        r = rng.random()
        if r < 0.50:  # fast, correct
            code = f"# {salt()}\nimport sys\nprint(sum(map(int, sys.stdin.read().split())))"
            items.append(("fast_ok", code, "1 2 3 4\n"))
        elif r < 0.70:  # CPU-bound ~1s on an uncontended core
            code = (
                f"# {salt()}\n"
                "n = 0\n"
                "for i in range(2, 60000):\n"
                "    if all(i % d for d in range(2, int(i ** 0.5) + 1)):\n"
                "        n += 1\n"
                "print(n)"
            )
            items.append(("cpu_1s", code, ""))
        elif r < 0.80:  # runtime error, fast
            code = f"# {salt()}\nimport sys\nx = int(sys.stdin.read())\nprint(x / 0)"
            items.append(("error", code, "7\n"))
        elif r < 0.90:  # infinite loop -> TLE, occupies a slot for run_timeout
            code = f"# {salt()}\nwhile True:\n    pass"
            items.append(("tle", code, ""))
        else:  # big stdin (256 KiB), exercises the stdin drain path
            code = f"# {salt()}\nimport sys\nprint(len(sys.stdin.read()))"
            items.append(("big_stdin", code, "x" * (256 * 1024)))
    return items


def one_request(url, code, stdin, run_timeout):
    payload = json.dumps(
        {
            "compile_timeout": run_timeout,
            "run_timeout": run_timeout,
            "code": code,
            "stdin": stdin,
            "memory_limit_MB": 1024,
            "language": "python",
            "files": {},
            "fetch_files": [],
        }
    )
    t0 = time.monotonic()
    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            data=payload,
            timeout=2 * run_timeout + API_TIMEOUT,
        )
        latency = time.monotonic() - t0
        if resp.status_code != 200:
            return latency, f"http_{resp.status_code}"
        return latency, resp.json().get("status", "NoStatus")
    except requests.exceptions.Timeout:
        return time.monotonic() - t0, "CLIENT_TIMEOUT"
    except requests.exceptions.ConnectionError:
        return time.monotonic() - t0, "CONN_ERROR"


def pct(values, p):
    if not values:
        return float("nan")
    values = sorted(values)
    k = min(len(values) - 1, max(0, round(p / 100 * (len(values) - 1))))
    return values[k]


def run_level(url, level, items, run_timeout):
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=level) as pool:
        results = list(
            pool.map(lambda it: one_request(url, it[1], it[2], run_timeout), items)
        )
    wall = time.monotonic() - t0
    lat = [r[0] for r in results]
    statuses = {}
    for _, s in results:
        statuses[s] = statuses.get(s, 0) + 1
    return {
        "level": level,
        "n": len(items),
        "wall": wall,
        "rps": len(items) / wall,
        "p50": pct(lat, 50),
        "p90": pct(lat, 90),
        "p99": pct(lat, 99),
        "max": max(lat),
        "timeouts": statuses.get("CLIENT_TIMEOUT", 0) + statuses.get("CONN_ERROR", 0),
        "sandbox_errors": statuses.get("SandboxError", 0),
        "statuses": statuses,
    }


def drain(url, run_timeout):
    """Wait until a trivial probe answers fast, so levels don't bleed into
    each other through residual queued work (TLE stragglers)."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        _, status = one_request(url, "print(42)", "", run_timeout)
        if status == "Success" and time.monotonic() - t0 < 2.0:
            return
        time.sleep(2)
    print("WARNING: server still draining after 120s; results may bleed", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080/run_code")
    ap.add_argument("--levels", default="8,16,24,32,40,48,64,96")
    ap.add_argument("--run-timeout", type=int, default=10, help="compile/run timeout, as in training")
    ap.add_argument("--requests-per-level", type=int, default=0, help="0 = auto (max(240, 5*level))")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    levels = [int(x) for x in args.levels.split(",")]

    # Warm-up: first executions pay one-time runtime cache costs.
    print("warming up...", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: one_request(args.url, "print(1)", "", args.run_timeout), range(16)))

    results = []
    for level in levels:
        n = args.requests_per_level or max(240, 5 * level)
        items = make_workload(n, args.run_timeout, args.seed + level)
        print(f"level {level:3d}: {n} requests ...", flush=True)
        res = run_level(args.url, level, items, args.run_timeout)
        results.append(res)
        bad = {k: v for k, v in res["statuses"].items() if k not in ("Success", "Failed")}
        print(
            f"  -> {res['rps']:6.2f} req/s  p50 {res['p50']:5.2f}s  p90 {res['p90']:5.2f}s  "
            f"p99 {res['p99']:5.2f}s  max {res['max']:5.2f}s  timeouts {res['timeouts']}"
            + (f"  bad={bad}" if bad else ""),
            flush=True,
        )
        drain(args.url, args.run_timeout)

    print("\n| in-flight | per-worker (/8) | req/s | p50 | p90 | p99 | max | timeouts | sandbox_errors |")
    print("|-----------|-----------------|-------|-----|-----|-----|-----|----------|----------------|")
    for r in results:
        print(
            f"| {r['level']} | {r['level'] / REWARD_WORKERS:.1f} | {r['rps']:.2f} | {r['p50']:.2f}s "
            f"| {r['p90']:.2f}s | {r['p99']:.2f}s | {r['max']:.2f}s | {r['timeouts']} | {r['sandbox_errors']} |"
        )

    # Recommend: highest throughput among levels that stayed clean.  p99 must
    # clear the client timeout with margin so production (which adds network
    # hops and bigger payloads) doesn't sit on the edge.
    budget = 2 * args.run_timeout + API_TIMEOUT
    clean = [r for r in results if r["timeouts"] == 0 and r["sandbox_errors"] == 0 and r["p99"] < 0.8 * budget]
    if clean:
        best = max(clean, key=lambda r: r["rps"])
        print(
            f"\nRecommended: total in-flight {best['level']} "
            f"-> SANDBOX_MAX_CONCURRENT={max(1, round(best['level'] / REWARD_WORKERS))} "
            f"({best['rps']:.2f} req/s, p99 {best['p99']:.2f}s)"
        )
    else:
        print("\nNo level stayed clean; the server is undersized for every tested level.")


if __name__ == "__main__":
    main()
