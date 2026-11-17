#!/usr/bin/env python3
"""Summarize captured exchanges: which endpoints exist and which parameters they use.

Usage: python3 -m estyma_local.analysis.summarize_logs [LOG_DIR_OR_FILES...]   (default: ./logs)
"""

from __future__ import annotations

import glob
import os
import sys
from collections import Counter, defaultdict

from ..protocol import loose_form
from ..raw_logs import read_records


def iter_records(paths):
    files = []
    for p in paths:
        files += sorted(glob.glob(os.path.join(p, "*.jsonl")) + glob.glob(os.path.join(p, "*.jsonl.gz"))) \
            if os.path.isdir(p) else [p]
    for f in files:
        yield from read_records(f)


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
        body = req.get("body") or {}
        form = loose_form(body["text"]).items() if "=" in body.get("text", "") else []
        for k, _ in form:
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
