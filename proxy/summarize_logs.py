#!/usr/bin/env python3
"""Summarize captured exchanges: which endpoints exist and which parameters they use.

Usage: python3 summarize_logs.py [LOG_DIR_OR_FILES...]   (default: ./logs)
"""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import Counter, defaultdict


def iter_records(paths):
    files = []
    for p in paths:
        files += sorted(glob.glob(os.path.join(p, "*.jsonl"))) if os.path.isdir(p) else [p]
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)


def main(argv):
    paths = argv or ["logs"]
    endpoints = Counter()
    statuses = defaultdict(Counter)
    query_keys = defaultdict(Counter)
    form_keys = defaultdict(Counter)
    hosts = Counter()
    first = last = None

    for rec in iter_records(paths):
        req = rec["request"]
        key = f"{req['method']} {req['path']}"
        endpoints[key] += 1
        first = first or rec["time"]
        last = rec["time"]
        for name, value in req["headers"]:
            if name.lower() == "host":
                hosts[value] += 1
        for k, _ in req.get("query", []):
            query_keys[key][k] += 1
        for k, _ in req["body"].get("form", []):
            form_keys[key][k] += 1
        statuses[key][rec.get("response", {}).get("status", "error")] += 1

    if not endpoints:
        print("no exchanges found")
        return 1
    print(f"{sum(endpoints.values())} exchanges, {first} .. {last}")
    print("Hosts:", ", ".join(f"{h} ({n})" for h, n in hosts.most_common()))
    print()
    for key, n in endpoints.most_common():
        st = ", ".join(f"{s}x{c}" for s, c in statuses[key].items())
        print(f"{n:6d}  {key}   [{st}]")
        if query_keys[key]:
            print("        query:", ", ".join(sorted(query_keys[key])))
        if form_keys[key]:
            print("        form: ", ", ".join(sorted(form_keys[key])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
