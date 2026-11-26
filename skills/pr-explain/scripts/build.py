#!/usr/bin/env python3
"""Validate explain.json against pr.json and build the PR Explain page.

Usage:
  build.py <workdir> [--previous <old explain.json>]

Reads <workdir>/pr.json and <workdir>/explain.json. Writes into <workdir>/site/:
  index.html   the page (loads data.json beside it)  -> publish this with data.json
  data.json    pr + explain + meta
  preview.html the same page with the data inline (open locally)

Exits 1 and lists every problem if explain.json doesn't fit the PR.
"""
import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "assets", "template.html")

SEV = {"critical", "major", "minor", "trivial", "info"}
TYPES = {"potential_issue", "refactor", "nitpick"}
CATS = {"functional", "security", "data", "performance", "stability", "maintainability"}
BANDS = {"blocker", "high", "pending", "advisory"}
READY = {"ready", "caution", "risky", "blocked"}
CONF = {"high", "medium", "low"}
CPLX = {"low", "medium", "high"}
DIAG = {"sequence", "flowchart", "class", "state", "er"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--previous", help="explain.json from an earlier snapshot, to mark findings addressed")
    a = ap.parse_args()
    pr = json.load(open(os.path.join(a.workdir, "pr.json")))
    ex = json.load(open(os.path.join(a.workdir, "explain.json")))
    errs, warns = [], []

    files = {f["path"]: f for f in pr["files"]}

    def nlines(path, side):
        f = files[path]
        c = f["newContent"] if side == "new" else f["oldContent"]
        if c is None:
            # fall back to the furthest line the diff shows
            m = 0
            for h in f["hunks"]:
                for l in h["lines"]:
                    m = max(m, l.get("n" if side == "new" else "o") or 0)
            return m
        return len(c.split("\n"))

    def in_diff(path, side, line):
        key = "n" if side == "new" else "o"
        return any(l.get(key) == line for h in files[path]["hunks"] for l in h["lines"])

    # --- layers
    layers = ex.get("layers") or []
    if not layers:
        errs.append("No layers.")
    seen = {}
    for i, l in enumerate(layers, 1):
        for k in ("title", "summary", "files"):
            if not l.get(k):
                errs.append(f"Layer {i} has no {k}.")
        for p in l.get("files", []):
            if p not in files:
                errs.append(f"Layer {i} lists {p}, which the PR doesn't change.")
            elif p in seen:
                errs.append(f"{p} is in layer {seen[p]} and layer {i}. Put each file in one layer.")
            else:
                seen[p] = i
        for d in l.get("dependsOn", []) or []:
            if not isinstance(d, int) or d < 1 or d >= i:
                errs.append(f"Layer {i} dependsOn {d}: must be an earlier layer number.")
        dg = l.get("diagram")
        if dg:
            if dg.get("kind") not in DIAG:
                errs.append(f"Layer {i} diagram kind '{dg.get('kind')}' must be one of {sorted(DIAG)}.")
            if not dg.get("mermaid"):
                errs.append(f"Layer {i} diagram has no mermaid source.")
    excluded = set(ex.get("excluded") or [])
    for p in files:
        if p not in seen and p not in excluded:
            errs.append(f"{p} is in no layer. Add it to one, or to 'excluded' if it's generated or vendored.")

    # --- ranges
    for j, r in enumerate(ex.get("ranges") or []):
        where = f"Range {j} ({r.get('file')}:{r.get('start')})"
        if r.get("file") not in files:
            errs.append(f"{where}: file not in PR."); continue
        side = r.get("side", "new")
        if side not in ("new", "old"):
            errs.append(f"{where}: side must be new or old.")
        if not (isinstance(r.get("start"), int) and isinstance(r.get("end"), int) and 1 <= r["start"] <= r["end"]):
            errs.append(f"{where}: start/end must be ints with start <= end."); continue
        if r["end"] > nlines(r["file"], side):
            errs.append(f"{where}: end {r['end']} is past the end of the file.")
        if not in_diff(r["file"], side, r["start"]):
            warns.append(f"{where}: start line isn't in the diff; the viewer will expand context to show it.")
        if r.get("complexity", "low") not in CPLX:
            errs.append(f"{where}: complexity must be low, medium or high.")
        if not r.get("summary"):
            errs.append(f"{where}: no summary.")

    # --- findings
    ids = set()
    for j, f in enumerate(ex.get("findings") or []):
        fid = f.get("id") or f"F{j+1}"
        f["id"] = fid
        where = f"Finding {fid}"
        if fid in ids:
            errs.append(f"{where}: duplicate id.")
        ids.add(fid)
        if f.get("file") not in files:
            errs.append(f"{where}: file {f.get('file')} not in PR."); continue
        side = f.get("side", "new")
        if not (isinstance(f.get("start"), int) and isinstance(f.get("end"), int) and 1 <= f["start"] <= f["end"]):
            errs.append(f"{where}: start/end must be ints with start <= end."); continue
        if f["end"] > nlines(f["file"], side):
            errs.append(f"{where}: end {f['end']} is past the end of the file.")
        if f.get("severity") not in SEV:
            errs.append(f"{where}: severity must be one of {sorted(SEV)}.")
        if f.get("type") not in TYPES:
            errs.append(f"{where}: type must be one of {sorted(TYPES)}.")
        if f.get("category") and f["category"] not in CATS:
            errs.append(f"{where}: category must be one of {sorted(CATS)}.")
        if not f.get("title") or not f.get("body"):
            errs.append(f"{where}: needs a title and a body.")
        if f.get("suggestion") is not None and side == "old":
            errs.append(f"{where}: suggestions must anchor to new-side lines.")
        for k, e in enumerate(f.get("existing") or []):
            if not (isinstance(e, dict) and e.get("file") and (e.get("line") is None or isinstance(e["line"], int))):
                errs.append(f"{where}: existing[{k}] needs a file and an int line.")
        f.setdefault("status", "open")

    # --- attention, readiness
    for j, t in enumerate(ex.get("attention") or []):
        if t.get("band") not in BANDS:
            errs.append(f"Attention item {j}: band must be one of {sorted(BANDS)}.")
        if t.get("finding") and t["finding"] not in ids:
            errs.append(f"Attention item {j}: finding {t['finding']} doesn't exist.")
        if t.get("url") and not str(t["url"]).startswith("https://"):
            errs.append(f"Attention item {j}: url must start with https://.")
    rd = ex.get("readiness") or {}
    if rd.get("band") not in READY:
        errs.append(f"readiness.band must be one of {sorted(READY)}.")
    if rd.get("confidence") not in CONF:
        errs.append(f"readiness.confidence must be one of {sorted(CONF)}.")

    # --- blast radius labels
    BR_HOW, BR_CONF = {"index", "rails", "cross-repo", "search"}, {"high", "medium", "low"}
    for j, it in enumerate((ex.get("blastRadius") or {}).get("items") or []):
        if it.get("confidence") and it["confidence"] not in BR_CONF:
            errs.append(f"blastRadius item {j}: confidence must be high, medium or low.")
        for w in it.get("where") or []:
            if w.get("how") and w["how"] not in BR_HOW:
                errs.append(f"blastRadius item {j}: where.how must be one of {sorted(BR_HOW)}.")
            if w.get("confidence") and w["confidence"] not in BR_CONF:
                errs.append(f"blastRadius item {j}: where.confidence must be high, medium or low.")

    # --- compare with an earlier snapshot
    if a.previous and os.path.exists(a.previous):
        old = json.load(open(a.previous))
        cur_keys = {(f["file"], f["title"].strip().lower()) for f in ex.get("findings") or []}
        carried = []
        for of in old.get("findings") or []:
            if (of["file"], of["title"].strip().lower()) not in cur_keys and of.get("status", "open") == "open":
                of = dict(of, status="addressed", id="P-" + of.get("id", ""))
                if of["file"] in files:
                    carried.append(of)
        ex.setdefault("findings", []).extend(carried)
        if carried:
            print(f"{len(carried)} finding(s) from the previous snapshot no longer apply; shown as addressed.")

    for w in warns:
        print("warn:", w)
    if errs:
        print("\n".join("error: " + e for e in errs))
        sys.exit(1)

    # One trace per PR: blast.json for the main PR, blast-<repo>.json for the others.
    blasts = []
    for i, meta in enumerate(pr.get("prs") or [{"name": pr["repo"].split("/")[1]}]):
        path = os.path.join(a.workdir, "blast.json" if i == 0 else f"blast-{meta['name']}.json")
        if os.path.exists(path):
            blasts.append({"pr": i, "trace": json.load(open(path))})
    blast = blasts[0]["trace"] if blasts and blasts[0]["pr"] == 0 else None
    data = {"pr": pr, "explain": ex, "blast": blast, "blasts": blasts,
            "meta": {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                     "generator": "pr-explain"}}
    site = os.path.join(a.workdir, "site")
    os.makedirs(site, exist_ok=True)
    tpl = open(TEMPLATE).read()
    title = f"PR {pr['number']} Explained"
    if "__TITLE__" in tpl:
        tpl = tpl.replace("__TITLE__", title.replace("<", "&lt;"), 1)
    blob = json.dumps(data, separators=(",", ":"))
    with open(os.path.join(site, "data.json"), "w") as fh:
        fh.write(blob)
    with open(os.path.join(site, "index.html"), "w") as fh:
        fh.write(tpl)  # keeps the __DATA__ placeholder, so it fetches data.json
    with open(os.path.join(site, "preview.html"), "w") as fh:
        fh.write(tpl.replace("__DATA__", blob.replace("<", "\\u003c"), 1))
    print(json.dumps({"ok": True, "site": os.path.abspath(site), "layers": len(layers),
                      "findings": len(ex.get("findings") or []), "ranges": len(ex.get("ranges") or []),
                      "dataBytes": len(blob), "warnings": len(warns)}))


if __name__ == "__main__":
    main()
