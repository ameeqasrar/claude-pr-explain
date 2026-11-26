#!/usr/bin/env python3
"""Find code the PR adds that may already exist in the codebase.

Usage:
  reuse.py <workdir> --repo <local checkout of the PR's repo> [--pr N] [--max 6]

Reads <workdir>/pr.json. Writes <workdir>/reuse.json and prints a summary.

For every function, method or component the PR adds, it looks at the PR's base commit for
existing definitions that:

  same name     have the same name in another class or file (skipped when many classes share
                the name on purpose, like `perform` or `to_param`)
  similar name  share most of the name's words (`format_price` vs `price_formatter`)
  similar body  have nearly the same code, by overlap of 4-token runs (renamed copies too)

It also searches for long string or regex literals the PR adds that already appear in other
files: the usual sign of a pattern, URL or message being defined twice.

These are candidates, not findings. Open both sides and confirm they do the same job before
calling it duplication. Needs only git and python3. Reads the repo; never changes it.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from blast_radius import (COMMON, EXCLUDES, TEST_RE, changed_symbols, definitions,  # noqa: E402
                          lang_of, lex, select_pr)

SKIP_DIR = re.compile(r"(^|/)(node_modules|dist|build|vendor|public|coverage|tmp|log)/|^db/")
STOP = {"is", "has", "to", "the", "a", "an", "of", "for", "by", "with", "and", "or", "on", "in", "at", "use", "do"}
KEYWORDS = {"end", "def", "do", "if", "else", "elsif", "unless", "return", "const", "let", "var", "function",
            "then", "nil", "null", "true", "false", "self", "this", "new", "await", "async", "export", "import"}
SHINGLE = 4
MIN_TOKENS = 25
MAX_FILE = 300_000


def looks_defined(v, raw_line):
    """A literal worth defining once: a regex, a URL, or a sentence-long message.
    Keys, enum values, field names, CSS classes and import paths are meant to repeat."""
    if len(v) < 12 or v[0] in ".@~" or re.match(r"\s*(import|export|require|from)\b|.*\brequire\(", raw_line):
        return False
    if "w3.org/" in v or "schema.org" in v:
        return False
    if re.search(r"\\[dwsbAzZ.()\[\]/+*?]|\(\?[:=!<]|^\^.*\$$", v):
        return True                        # regex
    if re.match(r"https?://", v):
        return True                        # URL
    return len(v) >= 30 and v.count(" ") >= 4 and not re.search(r"\b(flex|px-|py-|text-|bg-|rounded|w-|h-)", v)


def words(name):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name.strip("?!=$_"))
    return [w for w in re.split(r"[_\W]+", s.lower()) if w and w not in STOP]


def body_tokens(code_lines, start, end, name):
    text = "\n".join(code_lines[start - 1:end])
    toks = re.findall(r"[A-Za-z_$][\w$]*[?!]?|\d+|==|!=|<=|>=|&&|\|\||=>|->|[^\s\w]", text)
    return [t for t in toks if t != name]


def shingles(toks):
    return {tuple(toks[i:i + SHINGLE]) for i in range(len(toks) - SHINGLE + 1)}


def git(repo, *args, inp=None):
    r = subprocess.run(["git", "-C", repo] + list(args), input=inp, capture_output=True)
    return r.stdout


def read_blobs(repo, ref, paths):
    """Contents of many files at ref through one `git cat-file --batch`."""
    out, data = {}, git(repo, "cat-file", "--batch", inp="".join(f"{ref}:{p}\n" for p in paths).encode())
    i = 0
    for p in paths:
        nl = data.index(b"\n", i)
        head = data[i:nl].split()
        i = nl + 1
        if len(head) < 3 or head[1] != b"blob":
            continue
        size = int(head[2])
        out[p] = data[i:i + size].decode("utf-8", "replace")
        i += size + 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--max", type=int, default=6, help="candidates kept per new definition")
    ap.add_argument("--pr", type=int, default=0, help="which PR of a multi-PR workspace (0 = main)")
    a = ap.parse_args()
    pr, suffix = select_pr(json.load(open(os.path.join(a.workdir, "pr.json"))), a.pr)

    probe = subprocess.run(["git", "-C", a.repo, "cat-file", "-e", pr["baseSha"] + "^{commit}"], capture_output=True)
    ref = pr["baseSha"] if probe.returncode == 0 else f"origin/{pr['base']}"

    new_defs = [s for s in changed_symbols(pr) if s["change"] == "added" and s["kind"] in ("method", "function")
                and s["name"] not in COMMON and not SKIP_DIR.search(s["file"])
                and not re.match(r"(handle|on)[A-Z]", s["name"])]
    langs = {s["lang"] for s in new_defs}
    deleted = defaultdict(set)
    for f in pr["files"]:
        for h in f["hunks"]:
            for l in h["lines"]:
                if l["t"] == "del":
                    deleted[f["path"]].add(l["o"])

    # ---------------------------------------------------------------- index the base commit
    index = []
    if new_defs:
        paths = [p for p in git(a.repo, "ls-tree", "-r", "--name-only", ref).decode().splitlines()
                 if lang_of(p) in langs and not TEST_RE.search(p) and not SKIP_DIR.search(p) and not p.endswith(".min.js")]
        blobs = read_blobs(a.repo, ref, paths)
        for p, text in blobs.items():
            if len(text) > MAX_FILE:
                continue
            lang = lang_of(p)
            code, _, defs = definitions(text, lang)
            for d in defs:
                if d["kind"] not in ("method", "function"):
                    continue
                # a definition this PR deletes or rewrites is a move, not a duplicate
                if deleted.get(p) and set(range(d["line"], d["end"] + 1)) & deleted[p]:
                    continue
                toks = body_tokens(code, d["line"], d["end"], d["name"])
                index.append({"file": p, "line": d["line"], "end": d["end"], "name": d["name"], "owner": d.get("owner"),
                              "lang": lang, "words": set(words(d["name"])),
                              "sh": shingles(toks) if len(toks) >= MIN_TOKENS else set(),
                              "first": text.splitlines()[d["line"] - 1].strip()[:160]})
    by_name = defaultdict(list)
    inverted = defaultdict(list)
    for i, d in enumerate(index):
        by_name[d["name"]].append(i)
        for s in d["sh"]:
            inverted[s].append(i)

    # ---------------------------------------------------------------- compare
    files = {f["path"]: f for f in pr["files"]}
    results = []
    for s in new_defs:
        content = files[s["file"]].get("newContent") or ""
        code, _ = lex(content, s["lang"])
        toks = body_tokens(code, s["line"], s["end"], s["name"])
        sh = shingles(toks) if len(toks) >= MIN_TOKENS else set()
        w = set(words(s["name"]))
        # body overlap with every indexed definition that shares a rare 4-token run
        body = {}
        if sh:
            counts = defaultdict(int)
            for x in sh:
                ids = inverted.get(x, ())
                if len(ids) > 60:  # boilerplate run shared by everything
                    continue
                for i in ids:
                    counts[i] += 1
            body = {i: c / len(sh | index[i]["sh"]) for i, c in counts.items()}
        scored = {}
        for i, j in body.items():
            if j >= 0.3:
                scored[i] = (round(j, 2), "similar body")
        same = [i for i in by_name.get(s["name"], []) if index[i]["lang"] == s["lang"]]
        if 0 < len(same) <= 8:
            for i in same:
                # one-word names like `resolve` or `evaluate` collide by chance; keep them only
                # when the bodies overlap too
                if len(w) >= 2 or body.get(i, 0) >= 0.2:
                    b = body.get(i, 0)
                    mod = (index[i]["owner"] or "").split("::")[-1]
                    over = mod and re.search(r"\b(include|prepend|extend)\s+(\w+::)*" + re.escape(mod) + r"\b", content)
                    why = (f"overrides included {mod}" if over else "same name") + (f", body {b:.0%} alike" if b >= 0.2 else "")
                    scored[i] = (1.0, why)
        if len(w) >= 2:
            near = []
            for i, d in enumerate(index):
                if d["lang"] != s["lang"] or i in scored or not d["words"] or d["name"] == s["name"]:
                    continue
                common = w & d["words"]
                if len(common) < 2:
                    continue
                j = len(common) / len(w | d["words"])
                if j >= 0.6:
                    near.append((j, i))
            for j, i in sorted(near, reverse=True)[:3]:
                scored[i] = (round(j, 2), "similar name")

        if not scored:
            continue
        top = sorted(scored.items(), key=lambda kv: -kv[1][0])[: a.max]
        own = (s.get("owner") + "#") if s.get("owner") else ""
        results.append({"symbol": own + s["name"], "file": s["file"], "line": s["line"], "end": s["end"], "lang": s["lang"],
                        "matches": [{"file": index[i]["file"], "line": index[i]["line"], "end": index[i]["end"],
                                     "symbol": ((index[i]["owner"] + "#") if index[i]["owner"] else "") + index[i]["name"],
                                     "score": sc, "why": why, "firstLine": index[i]["first"]} for i, (sc, why) in top]})

    # ---------------------------------------------------------------- repeated literals
    literals = []
    pr_paths = set(files)
    seen = set()
    for f in pr["files"]:
        lang = lang_of(f["path"])
        content = f.get("newContent")
        if lang not in ("ruby", "js") or TEST_RE.search(f["path"]) or not content:
            continue
        added = {l["n"] for h in f["hunks"] for l in h["lines"] if l["t"] == "add"}
        _, spans = lex(content, lang)
        raw = content.split("\n")
        for ln in sorted(added):
            if ln - 1 >= len(spans):
                continue
            for _, _, v in spans[ln - 1]:
                v = v.strip()
                if v in seen or not looks_defined(v, raw[ln - 1]):
                    continue
                seen.add(v)
                out = git(a.repo, "grep", "-n", "-I", "-F", "--no-color", "-e", v, ref, "--", ".", *EXCLUDES).decode("utf-8", "replace")
                hits = []
                for row in out.splitlines():
                    parts = row.split(":", 3)
                    if (len(parts) == 4 and parts[2].isdigit() and parts[1] not in pr_paths and not TEST_RE.search(parts[1])
                            and lang_of(parts[1]) in ("ruby", "js")):
                        hits.append({"file": parts[1], "line": int(parts[2]), "text": parts[3].strip()[:160]})
                if hits:
                    literals.append({"value": v[:120], "file": f["path"], "line": ln, "total": len(hits), "hits": hits[:5]})

    result = {"repo": pr["repo"], "ref": ref, "indexed": len(index), "newDefinitions": len(new_defs),
              "candidates": results, "literals": literals}
    with open(os.path.join(a.workdir, f"reuse{suffix}.json"), "w") as fh:
        json.dump(result, fh, indent=1)

    print(f"Indexed {len(index)} definitions at {ref[:12]}; {len(new_defs)} new definitions in the PR.")
    for r in results:
        print(f"  {r['symbol']}  [{r['file']}:{r['line']}]")
        for m in r["matches"]:
            print(f"      {m['score']:.2f} {m['why']:<22} {m['symbol']}  [{m['file']}:{m['line']}]")
    for l in literals:
        print(f"  literal {l['value']!r} [{l['file']}:{l['line']}] already in {l['total']} places, e.g. {l['hits'][0]['file']}:{l['hits'][0]['line']}")
    if not results and not literals:
        print("  No reuse candidates.")
    print(f"Wrote {os.path.join(a.workdir, 'reuse' + suffix + '.json')}")


if __name__ == "__main__":
    main()
