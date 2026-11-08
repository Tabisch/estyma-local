#!/usr/bin/env python3
"""Match cloud values (cloud_recorder.py) to raw registers (proxy logs).

The proxy logs hold what the controller reports in ``ZAPISZ_ODCZYT``
(``reg,idx,value;...``). The cloud recorder logs what the cloud web API shows
for the same moments. This script lines the two up in time and, for every
numeric cloud field, looks for registers whose values explain it under a
simple transform (as is, /10, /100, low byte, ...). It prints the best
candidates per field.

Usage:
    python3 compare/compare_cloud_proxy.py [LOG_DIR_OR_FILES...]     (default: logs)
    python3 compare/compare_cloud_proxy.py logs --csv aligned.csv --json mapping.json

Files named ``exchanges-*.jsonl`` are read as proxy logs and ``cloud-*.jsonl``
as cloud logs; other names are detected from their content.

Standard library only.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

# name, function(register value) -> expected cloud value, tolerance
TRANSFORMS = [
    ("x1", lambda v: v, 0.0),
    ("/10", lambda v: v / 10, 0.051),
    ("/10~", lambda v: v / 10, 0.55),        # cloud shows a rounded value
    ("/100", lambda v: v / 100, 0.0051),
    ("x10", lambda v: v * 10, 0.0),
    ("low byte", lambda v: v & 0xFF, 0.0),   # flags seen as 0x100 + 0/1
    ("bit0", lambda v: v & 1, 0.0),
    ("/10 trunc", lambda v: int(v / 10), 0.0),  # wrapper drops the last digit of counters
]
# Cloud bookkeeping, not device values.
SKIP_FIELDS = ("online.", "_execution_time", "device_id", "dataUpToDate")
SIMPLICITY = {name: i for i, (name, _, _) in enumerate(TRANSFORMS)}


def parse_time(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def list_files(paths):
    for p in paths:
        if os.path.isdir(p):
            yield from sorted(glob.glob(os.path.join(p, "*.jsonl")))
        else:
            yield p


def iter_lines(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def loose_form(text: str) -> dict:
    """The controller's form body: k=v&..., NUL padded, values not encoded."""
    out = {}
    for part in text.rstrip("\0").split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v
    return out


def parse_registers(value: str) -> dict:
    regs = {}
    for item in value.split(";"):
        bits = item.strip().split(",")
        if len(bits) != 3:
            continue
        try:
            regs[(int(bits[0]), int(bits[1]))] = int(bits[2])
        except ValueError:
            continue
    return regs


def proxy_sample(rec: dict):
    req = rec.get("request") or {}
    if not str(req.get("path", "")).endswith("/device/connect"):
        return None
    body = req.get("body") or {}
    field = None
    if "text" in body:
        field = loose_form(body["text"]).get("ZAPISZ_ODCZYT")
    if field is None:
        field = dict(body.get("form") or []).get("ZAPISZ_ODCZYT")
    if not field:
        return None
    regs = parse_registers(field)
    return (parse_time(rec["time"]), regs) if regs else None


def numeric_fields(rec: dict, use_raw: bool, use_values: bool) -> dict:
    out = {}
    for src, prefix in (("raw", "raw:"), ("values", "")):
        if (src == "raw" and not use_raw) or (src == "values" and not use_values):
            continue
        for k, v in (rec.get(src) or {}).items():
            if k.startswith(SKIP_FIELDS):
                continue
            if isinstance(v, bool):
                v = int(v)
            if isinstance(v, (int, float)):
                out[prefix + k] = v
    return out


def load(paths, device, use_raw, use_values):
    proxy, cloud = [], []
    for f in list_files(paths):
        for rec in iter_lines(f):
            if "request" in rec:
                s = proxy_sample(rec)
                if s:
                    proxy.append(s)
            elif "device_id" in rec and "time" in rec and rec.get("type") != "settings":
                if device and str(rec["device_id"]) != str(device):
                    continue
                fields = numeric_fields(rec, use_raw, use_values)
                if fields:
                    cloud.append((parse_time(rec["time"]), rec.get("data_age_s"), fields))
    proxy.sort(key=lambda s: s[0])
    cloud.sort(key=lambda s: s[0])
    return proxy, cloud


def align(proxy, cloud, window, slack, max_age):
    """For each cloud sample, the proxy samples that could have produced it.

    The cloud shows data the controller sent at about ``time - data_age_s``.
    Every proxy sample in ``[est - window, est + slack]`` is a candidate.
    """
    times = [t for t, _ in proxy]
    pairs = []
    for t, age, fields in cloud:
        est = t - age if isinstance(age, (int, float)) and 0 <= age <= max_age else t
        lo = bisect.bisect_left(times, est - window)
        hi = bisect.bisect_right(times, est + slack)
        cands = [proxy[i][1] for i in range(lo, hi)]
        if cands:
            pairs.append((t, est, fields, cands))
    return pairs


def _bucket(value, tol):
    return value if tol == 0 else int(value // tol)


def score(pairs, min_pairs, min_coverage=0.5):
    """Per cloud field and (register, transform): how often it matches."""
    hits = defaultdict(lambda: defaultdict(int))        # field -> (key, tf) -> hits
    distinct = defaultdict(lambda: defaultdict(set))    # cloud values that matched
    combos = defaultdict(int)                           # (fields, keys) -> pairs
    n_field = defaultdict(int)
    cloud_values = defaultdict(set)

    for _, _, fields, cands in pairs:
        keys = frozenset().union(*(c.keys() for c in cands))
        combos[(frozenset(fields), keys)] += 1
        # transform -> bucket -> [(transformed value, key)]
        index = []
        for tf_name, fn, tol in TRANSFORMS:
            buckets = defaultdict(list)
            for c in cands:
                for key, v in c.items():
                    tv = fn(v)
                    buckets[_bucket(tv, tol)].append((tv, key))
            index.append((tf_name, tol, buckets))
        for field, cv in fields.items():
            n_field[field] += 1
            cloud_values[field].add(cv)
            for tf_name, tol, buckets in index:
                if tol == 0:
                    found = {key for _, key in buckets.get(cv, ())}
                else:
                    b = _bucket(cv, tol)
                    found = {key for i in (b - 1, b, b + 1) for tv, key in buckets.get(i, ())
                             if abs(tv - cv) <= tol}
                for key in found:
                    hits[field][(key, tf_name)] += 1
                    distinct[field][(key, tf_name)].add(cv)

    # How many pairs had both the field and the register present.
    seen = defaultdict(lambda: defaultdict(int))
    for (fset, kset), n in combos.items():
        for field in fset:
            for key in kset:
                seen[field][key] += n

    results = {}
    for field, n in n_field.items():
        if n < min_pairs:
            continue
        cands = []
        for (key, tf), h in hits[field].items():
            # A register that only appeared in a few samples can reach 100 %
            # by chance; require it to cover a good part of the field's samples.
            if seen[field][key] < min_coverage * n:
                continue
            rate = h / seen[field][key]
            cands.append({
                "register": f"{key[0]},{key[1]}",
                "transform": tf,
                "match_rate": round(rate, 3),
                "matched": h,
                "pairs": seen[field][key],
                "distinct_values": len(distinct[field][(key, tf)]),
            })
        cands.sort(key=lambda c: (-c["match_rate"], -c["distinct_values"],
                                  SIMPLICITY[c["transform"]], c["register"]))
        # Keep only the best transform per register.
        best, used = [], set()
        for c in cands:
            if c["register"] not in used:
                used.add(c["register"])
                best.append(c)
        results[field] = {
            "samples": n,
            "cloud_distinct": len(cloud_values[field]),
            "cloud_min": min(cloud_values[field]),
            "cloud_max": max(cloud_values[field]),
            "candidates": best,
        }
    return results


def verdict(info, threshold):
    c = [x for x in info["candidates"] if x["match_rate"] >= threshold]
    if not c:
        return "no match"
    top = c[0]
    ties = [x for x in c if x["match_rate"] == top["match_rate"]
            and x["distinct_values"] == top["distinct_values"]]
    if info["cloud_distinct"] <= 1:
        if len(c) == 1:
            return "likely (but cloud value never changed)"
        return f"ambiguous (cloud value never changed, {len(c)} registers fit)"
    if len(ties) > 1:
        return f"ambiguous ({len(ties)} registers fit equally)"
    return "likely"


def write_csv(path, pairs, results, top):
    fields = sorted(results)
    regs = sorted({tuple(map(int, c["register"].split(",")))
                   for f in fields for c in results[f]["candidates"][:top]})
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["cloud_time", "estimated_data_time", "proxy_samples"] + fields
                   + [f"reg {r},{i}" for r, i in regs])
        for t, est, f_vals, cands in pairs:
            last = cands[-1]
            w.writerow([datetime.fromtimestamp(t).isoformat(timespec="seconds"),
                        datetime.fromtimestamp(est).isoformat(timespec="seconds"), len(cands)]
                       + [f_vals.get(f, "") for f in fields]
                       + [last.get(r, "") for r in regs])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Match cloud API values to proxy registers.")
    p.add_argument("paths", nargs="*", default=["logs"], help="log dirs or files (default: logs)")
    p.add_argument("--device", help="only use this cloud device id")
    p.add_argument("--window", type=float, default=90,
                   help="seconds before the estimated data time to search proxy samples (default 90)")
    p.add_argument("--slack", type=float, default=10,
                   help="seconds after the estimated data time to accept (default 10)")
    p.add_argument("--max-age", type=float, default=600,
                   help="ignore cloud data_age_s above this (cloud offline) (default 600)")
    p.add_argument("--min-pairs", type=int, default=3, help="skip fields with fewer samples")
    p.add_argument("--min-coverage", type=float, default=0.5,
                   help="ignore registers present in less than this share of a field's samples")
    p.add_argument("--threshold", type=float, default=0.9, help="match rate to count as a fit")
    p.add_argument("--top", type=int, default=3, help="candidates to print per field")
    p.add_argument("--only-raw", action="store_true", help="only Polish raw fields")
    p.add_argument("--only-values", action="store_true", help="only the wrapper's English fields")
    p.add_argument("--all", action="store_true", help="also print fields without a match")
    p.add_argument("--json", help="write all results to this file")
    p.add_argument("--csv", help="write the time-aligned table to this file")
    args = p.parse_args(argv)

    proxy, cloud = load(args.paths, args.device,
                        use_raw=not args.only_values, use_values=not args.only_raw)
    print(f"proxy samples: {len(proxy)}, cloud samples: {len(cloud)}")
    if not proxy or not cloud:
        print("need both proxy (exchanges-*.jsonl) and cloud (cloud-*.jsonl) logs")
        return 1
    print(f"proxy {datetime.fromtimestamp(proxy[0][0])} .. {datetime.fromtimestamp(proxy[-1][0])}")
    print(f"cloud {datetime.fromtimestamp(cloud[0][0])} .. {datetime.fromtimestamp(cloud[-1][0])}")

    pairs = align(proxy, cloud, args.window, args.slack, args.max_age)
    print(f"cloud samples with proxy data in range: {len(pairs)}\n")
    if not pairs:
        print("no overlap in time; run the proxy and the recorder at the same time")
        return 1

    results = score(pairs, args.min_pairs, args.min_coverage)
    unmatched = []
    for field in sorted(results):
        info = results[field]
        v = verdict(info, args.threshold)
        if v == "no match":
            unmatched.append(field)
            if not args.all:
                continue
        info["verdict"] = v
        rng = (f"{info['cloud_min']}" if info["cloud_distinct"] == 1
               else f"{info['cloud_min']}..{info['cloud_max']}, {info['cloud_distinct']} values")
        print(f"{field}  [{rng}]  -> {v}")
        for c in info["candidates"][: args.top]:
            print(f"    {c['register']:>9} {c['transform']:<9} {c['match_rate']:6.1%} "
                  f"({c['matched']}/{c['pairs']}, {c['distinct_values']} distinct)")

    regs_used = {c["register"] for f in results.values() for c in f["candidates"]
                 if c["match_rate"] >= args.threshold}
    all_regs = sorted({k for _, r in proxy for k in r})
    unused = [f"{r},{i}" for r, i in all_regs if f"{r},{i}" not in regs_used]
    print(f"\ncloud fields without a matching register ({len(unmatched)}): "
          + (", ".join(unmatched) if unmatched else "-"))
    print(f"registers not explained by any cloud field ({len(unused)}): "
          + (", ".join(unused) if unused else "-"))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"fields": results, "unmatched_fields": unmatched,
                       "unexplained_registers": unused}, fh, indent=2, ensure_ascii=False)
        print(f"wrote {args.json}")
    if args.csv:
        write_csv(args.csv, pairs, results, args.top)
        print(f"wrote {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
