#!/usr/bin/env python3
"""Aggregate a py-spy speedscope profile into a top self-time function breakdown.
Speedscope 'evented' format: frames[] + per-profile open/close events. Self time
= time a frame is on top of the stack. Reports top-N by self %."""
import json, sys
from collections import defaultdict

def main(path, topn=12):
    d = json.load(open(path))
    frames = d["shared"]["frames"]
    self_w = defaultdict(float)
    total = 0.0
    for prof in d["profiles"]:
        if prof.get("type") == "sampled":
            samples = prof["samples"]; weights = prof["weights"]
            for st, w in zip(samples, weights):
                if st: self_w[st[-1]] += w   # leaf = top of stack = self time
                total += w
        else:  # evented
            stack = []; last = prof.get("startValue", 0)
            for ev in prof["events"]:
                at = ev["at"]
                if stack: self_w[stack[-1]] += at - last
                last = at
                if ev["type"] == "O": stack.append(ev["frame"])
                elif ev["type"] == "C" and stack: stack.pop()
            total += prof.get("endValue", last) - prof.get("startValue", 0)
    if total <= 0: total = sum(self_w.values()) or 1
    rows = sorted(self_w.items(), key=lambda kv: -kv[1])[:topn]
    print(f"  top self-time functions ({path.split('/')[-1]}):")
    for fid, w in rows:
        f = frames[fid]
        name = f.get("name", "?")
        file = (f.get("file", "") or "").split("/")[-1]
        print(f"    {100*w/total:5.1f}%  {name}  ({file}:{f.get('line','')})")

if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 12)
