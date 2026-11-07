"""Duplicate scoring that runs inside Otari's code sandbox.

We send Otari a self-contained, standard-library script with the issue data
embedded as base64, so nothing in an issue can change the code that runs.
"""

from __future__ import annotations

import base64
import json

from .github import Issue

SCRIPT = r'''
import base64, json, math, re
from collections import Counter

DATA = json.loads(base64.b64decode("{payload}").decode("utf-8"))
STOP = set("""a an and are as at be been but by can cannot could did do does doing for from had has have how i if in into is it
its just me my no not of on or our so some than that the their them then there these they this to too use used using was we
were what when where which while who why will with would you your error issue bug problem get got getting fails failed failing
works working try tried trying need want please help thanks hi hello""".split())

def tokens(text):
    return [w for w in re.findall(r"[a-z0-9_]+", text.lower()) if len(w) > 2 and w not in STOP and not w.isdigit()]

def doc(item):
    # Titles carry the most signal, so they count twice.
    return tokens(item["title"]) * 2 + tokens(item.get("body", ""))

target = doc(DATA["target"])
docs = [doc(c) for c in DATA["candidates"]]
n = len(docs) + 1
df = Counter()
for d in docs + [target]:
    df.update(set(d))

def vector(d):
    tf = Counter(d)
    return {w: (1 + math.log(c)) * (math.log((1 + n) / (1 + df[w])) + 1) for w, c in tf.items()}

def cosine(a, b):
    dot = sum(v * b.get(w, 0.0) for w, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0

tv = vector(target)
scored = []
for item, d in zip(DATA["candidates"], docs):
    cv = vector(d)
    shared = sorted(set(tv) & set(cv), key=lambda w: -(tv[w] * cv[w]))[:5]
    scored.append({"number": item["number"], "title": item["title"], "state": item["state"],
                   "score": round(cosine(tv, cv), 3), "shared_terms": shared})
scored.sort(key=lambda s: -s["score"])
print(json.dumps({"compared": len(docs), "top": scored[:8]}))
'''


def build_script(target: Issue, candidates: list[Issue]) -> str:
    payload = {
        "target": {"title": target.title, "body": target.body[:2000]},
        "candidates": [
            {"number": c.number, "title": c.title, "body": c.body[:600], "state": c.state} for c in candidates
        ],
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return SCRIPT.replace("{payload}", encoded).strip() + "\n"


def parse_output(stdout: str) -> dict:
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict) and "top" in value:
                return value
        except ValueError:
            continue
    return {"compared": 0, "top": []}
