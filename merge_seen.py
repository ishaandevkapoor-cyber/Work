#!/usr/bin/env python3
"""Git merge driver for seen.json: union of both sides (never lose a remembered posting).

Configured by the workflow:  git config merge.jsonunion.driver "python3 merge_seen.py %O %A %B"
Usage: merge_seen.py <base> <ours> <theirs>   (writes the result to <ours>, exits 0)
"""
import json
import sys


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main(argv):
    base, ours, theirs = argv[1:4]
    merged = dict(load(theirs))
    merged.update(load(ours))  # both are unions of base; ours wins on the same key
    with open(ours, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
