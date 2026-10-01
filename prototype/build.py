#!/usr/bin/env python3
"""Build the RAG-Lite prototype store from the live mailbox (read-only source).

    python prototype/build.py load                          # copy messages + clean
    python prototype/build.py chunks                        # raw + clean chunk sets
    python prototype/build.py vec --embed 06b --sets raw,clean
    python prototype/build.py vec --embed 4b --sets raw     # live TEI control
    python prototype/build.py stats
    python prototype/build.py all                           # load + chunks + 06b + stats
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import raglite as R  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["load", "chunks", "vec", "stats", "all"])
    ap.add_argument("--embed", default="06b", choices=["06b", "06btei", "4b"])
    ap.add_argument("--sets", default="raw,clean")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    sets = tuple(s.strip() for s in args.sets.split(",") if s.strip())

    R.init_db()
    if args.step in ("load", "all"):
        n = R.load_messages()
        with R.db(vec=False) as c:
            q = c.execute("SELECT COUNT(*) FROM messages_light WHERE quoted != ''").fetchone()[0]
            chars = c.execute("SELECT SUM(LENGTH(quoted)) FROM messages_light").fetchone()[0]
        print(json.dumps({"loaded": n, "with_quoted_history": q,
                          "quoted_chars_removed": int(chars or 0)}, indent=1))
    if args.step in ("chunks", "all"):
        print(json.dumps(R.build_chunks(sets=("raw", "clean") if args.step == "all" else sets,
                                        limit=args.limit), indent=1))
    if args.step in ("vec", "all"):
        if args.step == "all":
            print("vec 06b:", json.dumps(R.build_vectors("06b", ("raw", "clean"))))
        else:
            print("vec %s:" % args.embed, json.dumps(R.build_vectors(args.embed, sets)))
    if args.step in ("stats", "all"):
        print("stats:", json.dumps(R.index_stats(), indent=1))
        print("load times:", R.load_time_report())


if __name__ == "__main__":
    main()
