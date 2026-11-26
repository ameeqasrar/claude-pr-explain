#!/usr/bin/env python3
"""Fetch everything the PR Explain viewer needs about one GitHub pull request.

Usage:
  fetch_pr.py <pr-ref> [<pr-ref> ...] --out <workdir>

<pr-ref> is a PR URL (https://github.com/owner/repo/pull/123), "owner/repo#123",
or a bare number (resolved against the git remote of the current directory).

Several refs make one workspace for a change that spans PRs (one ticket, several repos).
The first is the main PR: the page's title, branch and snapshot come from it. Files from
the others are prefixed with their repo's name ("front-end/src/App.tsx") and carry
`pr` (index into `prs`) and `repoPath` (the path inside their own repo).

Writes <workdir>/pr.json with: metadata, files (patch + parsed hunks + old/new
content when small enough), existing review threads, issue comments, reviews,
commits, checks, and a timeline. Also writes <workdir>/diff.txt (the plain
unified diff, one block per file with line numbers) for the analyst to read.

Needs: the `gh` CLI, authenticated.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys

MAX_CONTENT_CHARS = 200_000   # larger files show their hunks but can't expand context
MAX_FILES = 300


def gh(args, parse=True, check=True):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed:\n{r.stderr}")
    if not parse:
        return r.stdout
    return json.loads(r.stdout) if r.stdout.strip() else None


def resolve_ref(ref):
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", ref)
    if m:
        return f"{m[1]}/{m[2]}", int(m[3])
    m = re.match(r"([\w.-]+/[\w.-]+)#(\d+)$", ref)
    if m:
        return m[1], int(m[2])
    if ref.isdigit():
        repo = gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], parse=False).strip()
        return repo, int(ref)
    sys.exit(f"Can't read PR reference: {ref}")


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_patch(patch):
    """Turn a GitHub file patch into hunks of typed lines with old/new numbers."""
    hunks = []
    if not patch:
        return hunks
    cur = None
    o = n = 0
    for raw in patch.split("\n"):
        m = HUNK_RE.match(raw)
        if m:
            o, n = int(m[1]), int(m[3])
            cur = {"oldStart": o, "oldLines": int(m[2] or 1), "newStart": n,
                   "newLines": int(m[4] or 1), "header": m[5].strip(), "lines": []}
            hunks.append(cur)
            continue
        if cur is None or raw.startswith("\\"):
            continue
        t = raw[:1]
        text = raw[1:]
        if t == "+":
            cur["lines"].append({"t": "add", "n": n, "s": text}); n += 1
        elif t == "-":
            cur["lines"].append({"t": "del", "o": o, "s": text}); o += 1
        else:
            cur["lines"].append({"t": "ctx", "o": o, "n": n, "s": text}); o += 1; n += 1
    return hunks


def get_content(repo, path, ref):
    r = subprocess.run(["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}",
                        "-H", "Accept: application/vnd.github.raw"],
                       capture_output=True)
    if r.returncode != 0:
        return None
    try:
        s = r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return s if len(s) <= MAX_CONTENT_CHARS else None


THREADS_Q = """
query($owner:String!,$name:String!,$num:Int!,$cursor:String){
  repository(owner:$owner,name:$name){ pullRequest(number:$num){
    reviewThreads(first:100, after:$cursor){
      pageInfo{hasNextPage endCursor}
      nodes{ id isResolved isOutdated path line originalLine startLine diffSide
        comments(first:50){ nodes{ id databaseId body createdAt url
          author{login __typename} } } } } } } }
"""


def fetch_threads(repo, num):
    owner, name = repo.split("/")
    out, cursor = [], None
    while True:
        args = ["api", "graphql", "-f", f"query={THREADS_Q}", "-F", f"owner={owner}",
                "-F", f"name={name}", "-F", f"num={num}"]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        d = gh(args, check=False)
        if not d or "data" not in d:
            break
        rt = d["data"]["repository"]["pullRequest"]["reviewThreads"]
        for t in rt["nodes"]:
            out.append({
                "id": t["id"], "resolved": t["isResolved"], "outdated": t["isOutdated"],
                "path": t["path"], "line": t["line"] or t["originalLine"],
                "startLine": t["startLine"], "side": "old" if t["diffSide"] == "LEFT" else "new",
                "comments": [{
                    "id": c["databaseId"], "body": c["body"], "at": c["createdAt"], "url": c["url"],
                    "author": (c["author"] or {}).get("login", "ghost"),
                    "bot": (c["author"] or {}).get("__typename") == "Bot"
                           or (c["author"] or {}).get("login", "").endswith("[bot]"),
                } for c in t["comments"]["nodes"]],
            })
        if not rt["pageInfo"]["hasNextPage"]:
            break
        cursor = rt["pageInfo"]["endCursor"]
    return out


def fetch_one(repo, num):
    fields = ("number,title,body,url,state,isDraft,author,headRefName,baseRefName,headRefOid,"
              "baseRefOid,additions,deletions,changedFiles,createdAt,updatedAt,mergeable,"
              "mergeStateStatus,reviewDecision,statusCheckRollup,reviews,comments,commits,labels")
    pr = gh(["pr", "view", str(num), "-R", repo, "--json", fields])

    files = gh(["api", f"repos/{repo}/pulls/{num}/files?per_page=100", "--paginate"])
    if isinstance(files, list) and files and isinstance(files[0], list):
        files = [f for page in files for f in page]
    files = files[:MAX_FILES]

    from concurrent.futures import ThreadPoolExecutor

    def contents(f):
        status, path, prev = f["status"], f["filename"], f.get("previous_filename")
        new_c = None if status == "removed" else get_content(repo, path, pr["headRefOid"])
        old_c = None if status == "added" else get_content(repo, prev or path, pr["baseRefOid"])
        return old_c, new_c

    with ThreadPoolExecutor(max_workers=8) as ex:
        all_contents = list(ex.map(contents, files))

    out_files = []
    for f, (old_c, new_c) in zip(files, all_contents):
        out_files.append({
            "path": f["filename"], "previousPath": f.get("previous_filename"), "status": f["status"],
            "additions": f["additions"], "deletions": f["deletions"],
            "binary": f.get("patch") is None and f["changes"] > 0,
            "hunks": parse_patch(f.get("patch")), "oldContent": old_c, "newContent": new_c,
        })

    checks = []
    for c in pr.get("statusCheckRollup") or []:
        checks.append({"name": c.get("name") or c.get("context"),
                       "status": (c.get("conclusion") or c.get("state") or c.get("status") or "").lower(),
                       "url": c.get("detailsUrl") or c.get("targetUrl")})

    timeline = []
    for c in pr.get("commits") or []:
        timeline.append({"kind": "commit", "at": c.get("committedDate") or c.get("authoredDate"),
                         "who": ((c.get("authors") or [{}])[0]).get("login") or ((c.get("authors") or [{}])[0]).get("name"),
                         "text": c.get("messageHeadline"), "sha": c.get("oid", "")[:7]})
    for r in pr.get("reviews") or []:
        timeline.append({"kind": "review", "at": r.get("submittedAt"), "who": (r.get("author") or {}).get("login"),
                         "text": (r.get("state") or "").replace("_", " ").lower(), "body": r.get("body")})
    for c in pr.get("comments") or []:
        timeline.append({"kind": "comment", "at": c.get("createdAt"), "who": (c.get("author") or {}).get("login"),
                         "text": (c.get("body") or "")[:400], "url": c.get("url")})
    timeline.append({"kind": "opened", "at": pr["createdAt"], "who": pr["author"]["login"], "text": "opened the pull request"})
    timeline.sort(key=lambda e: e.get("at") or "")

    return {
        "repo": repo, "number": num, "url": pr["url"], "title": pr["title"], "body": pr["body"] or "",
        "state": "draft" if pr["isDraft"] and pr["state"] == "OPEN" else pr["state"].lower(),
        "author": pr["author"]["login"], "head": pr["headRefName"], "base": pr["baseRefName"],
        "headSha": pr["headRefOid"], "baseSha": pr["baseRefOid"],
        "additions": pr["additions"], "deletions": pr["deletions"], "changedFiles": pr["changedFiles"],
        "createdAt": pr["createdAt"], "updatedAt": pr["updatedAt"],
        "mergeable": pr["mergeable"], "mergeState": pr["mergeStateStatus"], "reviewDecision": pr["reviewDecision"],
        "labels": [l["name"] for l in pr.get("labels") or []],
        "checks": checks, "threads": fetch_threads(repo, num), "timeline": timeline,
        "files": out_files,
    }


SUMMARY_KEYS = ("repo", "number", "url", "title", "state", "author", "head", "base", "headSha", "baseSha",
                "additions", "deletions", "changedFiles", "mergeable", "mergeState", "reviewDecision", "checks")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("refs", nargs="+", help="PR refs; the first is the main PR")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    prs = [fetch_one(*resolve_ref(r)) for r in a.refs]
    # The workspace starts as a copy of the main PR; its lists get every PR's entries.
    data = dict(prs[0], files=list(prs[0]["files"]), threads=list(prs[0]["threads"]),
                timeline=list(prs[0]["timeline"]))
    data["prs"] = []
    for i, p in enumerate(prs):
        name = p["repo"].split("/")[1]
        label = f"{name}#{p['number']}"
        summary = {k: p[k] for k in SUMMARY_KEYS}
        summary.update(name=name, label=label, prefix="" if i == 0 else name + "/")
        data["prs"].append(summary)
        for f in p["files"]:
            f["pr"], f["repoPath"] = i, f["path"]
            if i:
                f["path"] = name + "/" + f["path"]
                if f.get("previousPath"):
                    f["previousPath"] = name + "/" + f["previousPath"]
        for t in p["threads"]:
            t["pr"] = i
            if i:
                t["path"] = name + "/" + t["path"]
        if len(prs) > 1:
            for e in p["timeline"]:
                e["pr"] = label
        if i:
            data["files"] += p["files"]
            data["threads"] += p["threads"]
            data["timeline"] += p["timeline"]
    data["timeline"].sort(key=lambda e: e.get("at") or "")

    with open(os.path.join(a.out, "pr.json"), "w") as fh:
        json.dump(data, fh)

    # A readable diff with explicit line numbers, for the analyst.
    with open(os.path.join(a.out, "diff.txt"), "w") as fh:
        for i, p in enumerate(prs):
            fh.write(f"# {p['repo']}#{p['number']}: {p['title']}\n# {p['head']} -> {p['base']} @ {p['headSha'][:7]}"
                     + ("  (main PR)" if i == 0 and len(prs) > 1 else "") + "\n\n")
            fh.write("## PR description\n" + (p["body"] or "(none)") + "\n\n")
            for f in p["files"]:
                fh.write(f"=== {f['path']} ({f['status']}, +{f['additions']}/-{f['deletions']})\n")
                for h in f["hunks"]:
                    fh.write(f"@@ old {h['oldStart']} new {h['newStart']} @@ {h['header']}\n")
                    for l in h["lines"]:
                        if l["t"] == "add":
                            fh.write(f"      {l['n']:>5} + {l['s']}\n")
                        elif l["t"] == "del":
                            fh.write(f"{l['o']:>5}       - {l['s']}\n")
                        else:
                            fh.write(f"{l['o']:>5} {l['n']:>5}   {l['s']}\n")
                fh.write("\n")

    print(json.dumps([{"repo": p["repo"], "number": p["number"], "files": len(p["files"]),
                       "additions": p["additions"], "deletions": p["deletions"],
                       "threads": len(p["threads"]), "headSha": p["headSha"][:7]} for p in prs]
                     + [{"out": os.path.abspath(a.out)}]))


if __name__ == "__main__":
    main()
