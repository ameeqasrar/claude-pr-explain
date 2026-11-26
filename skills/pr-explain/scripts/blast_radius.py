#!/usr/bin/env python3
"""Trace the blast radius of a PR: who calls what it changed, here and in sibling repos.

Usage:
  blast_radius.py <workdir> --repo <local checkout of the PR's repo>
                  [--search-root <folder holding sibling repos>] [--pr N] [--max-refs 40]

Reads <workdir>/pr.json (from fetch_pr.py). Writes <workdir>/blast.json and prints a summary.

Three sources, each reference labelled with how it was found and how sure it is:

  index      A built-in lexer for Ruby, JS and TS. It tells code from comments and strings,
             knows which class and method every line sits in, finds the definitions the
             PR changed, and finds references to them at the PR's exact base commit (head
             content for files the PR changes).
  rails      Rails conventions a call graph can't see: routes to controller actions,
             callbacks and other :symbol references, alias_method / delegate, concerns
             and their includers, string job dispatch like
             Executioner.perform_async('Site', 'delete_site', id).
  cross-repo Sibling git repos under --search-root, searched (at their checked-out HEAD)
             for the changed actions' URL paths, string literals and env names the diff
             adds or removes, and distinctive changed method or class names.

Confidence: high = the reference can only mean this definition (unique name, qualified
receiver, class+method dispatch pair, an exact route); medium = right name but more than
one definition shares it, or a convention-based link; low = a text match only.

Needs only git and python3. Reads repos; never changes them.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

CODE_EXT = {".rb", ".rake", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue"}
TEMPLATE_EXT = {".erb", ".haml", ".slim", ".graphql", ".gql", ".yml", ".yaml"}
EXCLUDES = [":(glob,exclude)**/node_modules/**", ":(glob,exclude)**/dist/**", ":(glob,exclude)**/build/**",
            ":(glob,exclude)**/vendor/**", ":(glob,exclude)**/public/**", ":(glob,exclude)**/*.min.js",
            ":(glob,exclude)**/*.map", ":(glob,exclude)**/package-lock.json", ":(glob,exclude)**/yarn.lock",
            ":(glob,exclude)**/coverage/**", ":(glob,exclude)**/*.snap", ":(glob,exclude)**/tmp/**",
            ":(glob,exclude)**/log/**"]
TEST_RE = re.compile(r"(^|/)(spec|test|tests|__tests__|cypress|e2e)/|[._](spec|test)\.[jt]sx?$|_spec\.rb$|_test\.rb$")
COMMON = {"call", "new", "id", "name", "to_s", "to_h", "each", "map", "user", "users", "data", "value", "type",
          "index", "show", "create", "update", "destroy", "edit", "initialize", "perform", "run", "get", "set",
          "params", "render", "save", "find", "where", "status", "constructor", "default", "process", "current"}
MAX_LINE = 400


def lang_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".rb", ".rake") or path.endswith("Gemfile"):
        return "ruby"
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue"):
        return "js"
    if ext in TEMPLATE_EXT:
        return "text"
    return None


# --------------------------------------------------------------------------- lexer
PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">"}


def lex(text, lang):
    """Return (code_lines, string_spans). Comments become spaces in code; string interiors
    become spaces too (quotes kept). string_spans[i] = [(start, end, value), ...] for line i."""
    lines = text.split("\n")
    if lang not in ("ruby", "js"):
        return lines, [[] for _ in lines]
    code, spans = [], []
    st = None            # None | dict(kind, close, open, interp, depth, start, buf)
    heredocs = []        # pending ruby heredoc terminators
    in_heredoc = None    # (terminator, squiggly)
    block_comment = False
    stack = []           # saved string states while inside #{...} / ${...}
    brace_depth = 0
    for raw in lines:
        out = list(raw)
        sp = []
        if in_heredoc:
            term = in_heredoc
            if raw.strip() == term:
                in_heredoc = heredocs.pop(0) if heredocs else None
            else:
                sp.append((0, len(raw), raw.strip()))
                out = [" "] * len(raw)
            code.append("".join(out)); spans.append(sp)
            continue
        if lang == "ruby" and block_comment:
            if raw.startswith("=end"):
                block_comment = False
            code.append(" " * len(raw)); spans.append([])
            continue
        if lang == "ruby" and raw.startswith("=begin"):
            block_comment = True
            code.append(" " * len(raw)); spans.append([])
            continue
        i, n = 0, len(raw)
        if st is not None:  # string continuing from the previous line
            st["start"] = 0
        while i < n:
            c = raw[i]
            if st is not None:
                if c == "\\":
                    out[i] = " "
                    if i + 1 < n:
                        out[i + 1] = " "; st["buf"] += raw[i:i + 2]
                    i += 2; continue
                if st["interp"] and ((lang == "ruby" and raw.startswith("#{", i)) or (lang == "js" and raw.startswith("${", i))):
                    stack.append((st, brace_depth)); st = None; brace_depth = 0
                    i += 2; continue
                if st["open"] and c == st["open"]:
                    st["depth"] += 1
                elif c == st["close"]:
                    if st["depth"] > 0:
                        st["depth"] -= 1
                    else:
                        sp.append((st["start"], i + 1, st["buf"]))
                        st = None; i += 1; continue
                st["buf"] += c; out[i] = " "; i += 1
                continue
            # --- code
            if stack and c == "}" and brace_depth == 0:
                st, brace_depth = stack.pop(); i += 1; continue
            if c == "{":
                brace_depth += 1
            elif c == "}":
                brace_depth -= 1
            if lang == "ruby" and c == "#":
                for k in range(i, n): out[k] = " "
                break
            if lang == "js" and raw.startswith("//", i):
                for k in range(i, n): out[k] = " "
                break
            if lang == "js" and raw.startswith("/*", i):
                j = raw.find("*/", i + 2)
                end = n if j < 0 else j + 2
                for k in range(i, end): out[k] = " "
                if j < 0:
                    block_comment = True
                i = end; continue
            if c in "'\"" or (lang == "js" and c == "`"):
                st = {"close": c, "open": None, "interp": c != "'", "depth": 0, "start": i, "buf": ""}
                i += 1; continue
            if lang == "ruby" and c == "%" and i + 2 < n and re.match(r"%[qQwWiI]?[\(\[\{<|!/]", raw[i:i + 3]):
                j = i + 1
                kind = raw[j] if raw[j].isalpha() else "Q"
                if raw[j].isalpha():
                    j += 1
                o = raw[j]
                st = {"close": PAIRS.get(o, o), "open": o if o in PAIRS else None, "interp": kind in "QWI",
                      "depth": 0, "start": i, "buf": ""}
                i = j + 1; continue
            if lang == "ruby" and raw.startswith("<<", i):
                m = re.match(r"<<([~-]?)(['\"]?)([A-Z_][A-Z0-9_]*)\2", raw[i:])
                if m:
                    t = m.group(3)
                    if in_heredoc is None:
                        in_heredoc = t
                    else:
                        heredocs.append(t)
                    i += m.end(); continue
            i += 1
        if lang == "js" and block_comment and st is None:
            # carry /* */ across lines
            pass
        code.append("".join(out)); spans.append(sp)
        if st is not None and lang == "js" and st["close"] in "'\"":
            st = None  # unterminated quote in JS: drop it at line end
    if lang == "js":
        # second pass for multi-line /* */ comments
        inside = False
        for idx, l in enumerate(code):
            s = l
            if inside:
                j = lines[idx].find("*/")
                if j < 0:
                    code[idx] = " " * len(l); continue
                code[idx] = " " * (j + 2) + l[j + 2:]
                inside = False
            k = lines[idx].find("/*")
            if k >= 0 and lines[idx].find("*/", k) < 0 and code[idx][k:k + 2] == "/*":
                code[idx] = code[idx][:k] + " " * (len(l) - k)
                inside = True
    return code, spans


# --------------------------------------------------------------------------- definitions
RB_DEF = re.compile(r"^(\s*)(?:(?:private|protected|public|module_function)\s+)?def\s+(self\.)?([A-Za-z_][\w]*[?!=]?|\[\]=?|[+\-*/<>=!~%&|^]+)")
RB_CLASS = re.compile(r"^(\s*)(class|module)\s+(?!<<)([A-Z][\w:]*)")
RB_CONST = re.compile(r"^(\s*)([A-Z][A-Z0-9_]{2,})\s*=(?!=)")
RB_ATTR = re.compile(r"^\s*(scope|attr_reader|attr_accessor|attr_writer|delegate|alias_method|has_many|has_one|belongs_to)\s+:(\w+[?!]?)")
JS_DEFS = [
    re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"^(\s*)(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"),
    # an arrow function whose parameters start on the next line: `const f = (` or `const f = async (`
    re.compile(r"^(\s*)(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?\(\s*$"),
    re.compile(r"^(\s*)(?:export\s+)?class\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^(\s*)(?:static\s+|async\s+|get\s+|set\s+)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{\s*$"),
    re.compile(r"^(\s*)([A-Za-z_$][\w$]*)\s*:\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"),
    re.compile(r"^(\s*)(?:[A-Za-z_$][\w$]*\.)+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>)"),
]
JS_CLASS = next(rx for rx in JS_DEFS if "class\\s+" in rx.pattern)
JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "constructor", "else"}


def ruby_defs(code):
    defs = []
    for i, l in enumerate(code):
        m = RB_CLASS.match(l)
        if m:
            defs.append({"kind": m.group(2), "name": m.group(3).split("::")[-1], "full": m.group(3),
                         "indent": m.group(1), "line": i + 1}); continue
        m = RB_DEF.match(l)
        if m:
            one = bool(re.search(r"\)\s*=\s*\S|def\s+[\w.?!]+\s*=\s*\S|;\s*end\s*$", l))
            defs.append({"kind": "method", "name": m.group(3), "self": bool(m.group(2)),
                         "indent": m.group(1), "line": i + 1, "one": one}); continue
        m = RB_CONST.match(l)
        if m:
            defs.append({"kind": "constant", "name": m.group(2), "indent": m.group(1), "line": i + 1, "one": True})
    for d in defs:
        if d.get("one"):
            d["end"] = d["line"]; continue
        end = len(code)
        pat = re.compile("^" + re.escape(d["indent"]) + r"end\b")
        for j in range(d["line"], len(code)):
            if pat.match(code[j]):
                end = j + 1; break
        d["end"] = end
    return defs


def js_defs(code):
    defs = []
    for i, l in enumerate(code):
        for k, rx in enumerate(JS_DEFS):
            m = rx.match(l)
            if m and m.group(2) not in JS_KEYWORDS:
                defs.append({"kind": "class" if rx is JS_CLASS else "function", "name": m.group(2), "indent": m.group(1), "line": i + 1})
                break
    for d in defs:  # span by brace matching from the definition line
        depth, started, end = 0, False, d["line"]
        for j in range(d["line"] - 1, min(len(code), d["line"] + 3000)):
            for ch in code[j]:
                if ch == "{":
                    depth += 1; started = True
                elif ch == "}":
                    depth -= 1
            end = j + 1
            if started and depth <= 0:
                break
            if not started and j > d["line"] + 1:
                end = d["line"]; break
        d["end"] = end
    return defs


def definitions(text, lang):
    code, spans = lex(text, lang)
    defs = ruby_defs(code) if lang == "ruby" else js_defs(code) if lang == "js" else []
    # owners: innermost enclosing class/module (ruby) or class (js)
    containers = [d for d in defs if d["kind"] in ("class", "module")]
    for d in defs:
        chain = [c for c in containers if c is not d and c["line"] <= d["line"] <= c["end"]]
        chain.sort(key=lambda c: c["line"])
        d["owner"] = "::".join(c.get("full", c["name"]) if i == 0 else c["name"] for i, c in enumerate(chain)) or None
    return code, spans, defs


def enclosing(defs, line):
    best = None
    for d in defs:
        if d["kind"] in ("method", "function") and d["line"] <= line <= d["end"]:
            if best is None or d["line"] >= best["line"]:
                best = d
    cls = None
    for d in defs:
        if d["kind"] in ("class", "module") and d["line"] <= line <= d["end"]:
            if cls is None or d["line"] >= cls["line"]:
                cls = d
    owner = (best or {}).get("owner") or (cls and (cls.get("owner") + "::" + cls["name"] if cls.get("owner") else cls.get("full", cls["name"])))
    if best:
        return f"{owner}#{best['name']}" if owner else best["name"]
    return owner


# --------------------------------------------------------------------------- git helpers
class Repo:
    def __init__(self, path, ref):
        self.path, self.ref, self.cache = path, ref, {}
        self.name = os.path.basename(os.path.abspath(path))

    def git(self, *args):
        r = subprocess.run(["git", "-C", self.path] + list(args), capture_output=True, text=True, errors="replace")
        return r.stdout if r.returncode in (0, 1) else ""

    def grep(self, term, word=False):
        args = ["grep", "-n", "-I", "-F", "--no-color"]
        if word:
            args.append("-w")
        args += ["-e", term, self.ref, "--", "."] + EXCLUDES
        hits = []
        for line in self.git(*args).splitlines():
            parts = line.split(":", 3)  # ref:path:line:text
            if len(parts) == 4 and parts[2].isdigit():
                hits.append((parts[1], int(parts[2]), parts[3]))
        return hits

    def show(self, path, overlay=None):
        if overlay and path in overlay:
            return overlay[path]
        if path not in self.cache:
            self.cache[path] = self.git("show", f"{self.ref}:{path}")
        return self.cache[path]


# --------------------------------------------------------------------------- routes (Rails)
VERB = r"(get|post|put|patch|delete|match)"


def strip_ruby_comment(line):
    q = None
    for i, c in enumerate(line):
        if q:
            if c == "\\":
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == "#":
            return line[:i]
    return line


def parse_routes(text):
    """Very small Rails routes reader: returns [(verb, path, controller, action, line)]."""
    out, ns = [], []  # ns: [(indent, module, path)]
    resource_stack = []
    member_stack = []  # (indent, "member" | "collection")
    for i, raw in enumerate(text.split("\n"), 1):
        l = strip_ruby_comment(raw)
        ind = len(raw) - len(raw.lstrip())
        while ns and ind <= ns[-1][0] and raw.strip().startswith("end"):
            ns.pop(); break
        if raw.strip().startswith("end"):
            if member_stack and ind <= member_stack[-1][0]:
                member_stack.pop(); continue
            if resource_stack and ind <= resource_stack[-1][0]:
                resource_stack.pop(); continue
        mb = re.match(r"\s*(member|collection)\s+do\b", l)
        if mb and resource_stack:
            member_stack.append((ind, mb.group(1))); continue
        m = re.match(r"\s*namespace\s+:(\w+)", l)
        if m and l.rstrip().endswith("do"):
            ns.append((ind, m.group(1), m.group(1))); continue
        m = re.match(r"\s*scope\s+(?:module:\s*['\":]+(\w+)|['\"]/?([\w/]+)['\"])", l)
        if m and l.rstrip().endswith("do"):
            ns.append((ind, m.group(1) or "", m.group(2) or "")); continue
        mod = "/".join(x[1] for x in ns if x[1])
        pre = "/".join(x[2] for x in ns if x[2])
        m = re.match(r"\s*resources?\s+:(\w+)(.*)", l)
        if m:
            name, rest = m.group(1), m.group(2)
            ctrl = (mod + "/" if mod else "") + name
            base = "/" + "/".join(p for p in [pre, name] if p)
            only = re.search(r"only:\s*\[([^\]]*)\]", rest)
            acts = re.findall(r":(\w+)", only.group(1)) if only else ["index", "show", "new", "create", "edit", "update", "destroy"]
            vb = {"index": "GET", "show": "GET", "new": "GET", "edit": "GET", "create": "POST", "update": "PATCH", "destroy": "DELETE"}
            for a in acts:
                out.append((vb.get(a, "GET"), base + ("/:id" if a in ("show", "update", "destroy", "edit") else ""), ctrl, a, i))
            if l.rstrip().endswith("do"):
                resource_stack.append((ind, ctrl, base))
            continue
        m = re.match(r"\s*" + VERB + r"\s+['\"]([^'\"]+)['\"]\s*(?:=>|,\s*to:)\s*['\"]([\w/]+)#(\w+)['\"](.*)", l)
        if m:
            verb = m.group(1).upper()
            if verb == "MATCH":
                v = re.search(r"via:\s*\[?\s*:(\w+)", m.group(5))
                verb = v.group(1).upper() if v else "ANY"
            ctrl = m.group(3) if "/" in m.group(3) or not mod else mod + "/" + m.group(3)
            p = m.group(2) if m.group(2).startswith("/") else "/" + "/".join(x for x in [pre, m.group(2)] if x)
            out.append((verb, p, ctrl, m.group(4), i)); continue
        m = re.match(r"\s*" + VERB + r"\s+:(\w+)", l)
        if m and resource_stack:
            _, ctrl, base = resource_stack[-1]
            on_coll = member_stack and member_stack[-1][1] == "collection"
            on_flag = re.search(r"on:\s*:collection", l)
            out.append((m.group(1).upper(), base + ("/" if (on_coll or on_flag) else "/:id/") + m.group(2), ctrl, m.group(2), i))
    return out


# --------------------------------------------------------------------------- main analysis
def changed_symbols(pr):
    """Definitions in non-test code files whose span holds an added or deleted line."""
    syms = []
    for f in pr["files"]:
        path, lang = f["path"], lang_of(f["path"])
        if lang not in ("ruby", "js") or TEST_RE.search(path):
            continue
        added = {l["n"] for h in f["hunks"] for l in h["lines"] if l["t"] == "add"}
        deleted = {l["o"] for h in f["hunks"] for l in h["lines"] if l["t"] == "del"}
        seen = set()
        for side, content, touched, status in (("new", f.get("newContent"), added, "modified"),
                                               ("old", f.get("oldContent"), deleted, "removed")):
            if not content or not touched:
                continue
            _, _, defs = definitions(content, lang)
            leaf = [d for d in defs if d["kind"] in ("method", "function", "constant")]
            for d in leaf + [d for d in defs if d["kind"] in ("class", "module")]:
                span = set(range(d["line"], d["end"] + 1))
                hit = span & touched
                if not hit:
                    continue
                # a class counts as changed only if a change falls outside all its methods
                if d["kind"] in ("class", "module"):
                    inner = set()
                    for x in leaf:
                        if d["line"] <= x["line"] <= d["end"]:
                            inner |= set(range(x["line"], x["end"] + 1))
                    if not (hit - inner) and f["status"] != "added":
                        continue
                key = (d["name"], d.get("owner"))
                if key in seen:
                    continue
                seen.add(key)
                change = "added" if (f["status"] == "added" or (side == "new" and set(range(d["line"], d["line"] + 1)) <= added)) else status
                syms.append({"name": d["name"], "kind": d["kind"], "owner": d.get("owner"), "file": path,
                             "line": d["line"], "end": d["end"], "side": side, "change": change,
                             "self": d.get("self", False), "lang": lang})
    return syms


def classify(path, lang, code_line, raw_line, col, term, spans_line, owner_short, repo_is_rails):
    """Return (kind, confidence_hint), or None for comments, definitions, keys and prose."""
    end = col + len(term)
    is_test = bool(TEST_RE.search(path))
    if lang == "text":
        if path.endswith((".erb", ".haml", ".slim")):
            regions = [(m.start(1), m.end(1)) for m in re.finditer(r"<%=?-?(.*?)-?%>", raw_line)]
            if path.endswith((".haml", ".slim")) and re.match(r"\s*[-=~]", raw_line):
                regions.append((0, len(raw_line)))
            reg = next(((a, b) for a, b in regions if a <= col < b), None)
            if not reg:
                return None  # page text, not code
            seg = raw_line[reg[0]:col]
            if seg.count("'") % 2 or seg.count('"') % 2:
                return None  # a string inside the template code
            if col > 0 and raw_line[col - 1] == ":" and raw_line[col - 2:col] != "::":
                return None  # a symbol, e.g. f.label :name
            return ("test" if is_test else "template", None)
        return None if is_test else ("config", "low")
    in_string = next((s for s in spans_line if s[0] <= col < s[1]), None)
    if in_string is None and code_line[col:end] != term:
        return None  # inside a comment
    before = code_line[:col]
    kind = None
    if in_string:
        strs = [s[2] for s in spans_line]
        if owner_short and owner_short in strs:
            kind = ("dispatch", "high")
        elif re.search(r"perform_(async|in|at|later)|enqueue|send\(|public_send|try\(|method\(", code_line):
            kind = ("dispatch", "medium")
        else:
            kind = ("string", "low")
    elif lang == "ruby" and re.search(r"\bdef\s+(self\.)?$", before):
        return None
    elif lang == "js" and re.search(r"\bfunction\s*\*?\s*$|\b(const|let|var|class)\s+$", before):
        return None
    elif before.endswith(":") and not before.endswith("::"):
        if re.search(r"\b(before|after|around|skip_before|skip_after|skip_around|prepend_before|prepend_after)_(action|filter|save|create|update|destroy|validation|commit|initialize|find|touch)\b|\bvalidate\b", code_line):
            kind = ("callback", "medium")
        elif re.search(r"\b(alias_method|alias|delegate|helper_method)\b", code_line):
            kind = ("alias", "high")
        else:
            kind = ("symbol", "medium")
    else:
        after = code_line[end:end + 1]
        if lang == "ruby" and after == ":" and code_line[end:end + 2] != "::":
            return None  # a hash key / keyword arg named like the method
        if term[:1].isupper() and lang == "ruby" and not re.match(r"(\.|::|\(|\s*[<=]|\s*\)|,|\s+do\b|\]|$)", code_line[end:]):
            return None
        kind = ("call", None)
    if is_test:
        # specs count only when they call it (or dispatch it); their symbols and strings are labels
        return ("test", None) if kind[0] in ("call", "dispatch") else None
    return kind


def ref_regex(term):
    return re.compile(r"(?<![A-Za-z0-9_@$])" + re.escape(term) + r"(?![A-Za-z0-9_?!])")


def trace(repo, term, owner_short, overlay, max_refs, def_names_count, repo_is_rails, how="index"):
    rx = ref_regex(term)
    hits = repo.grep(term)
    files = defaultdict(list)
    for path, ln, _ in hits:
        files[path].append(ln)
    for path in overlay:  # files the PR changes: search their head content instead
        files.pop(path, None)
        if term in overlay[path]:
            for i, l in enumerate(overlay[path].split("\n"), 1):
                if term in l:
                    files[path].append(i)
    if sum(len(v) for v in files.values()) > 600:
        return {"tooCommon": True, "candidates": sum(len(v) for v in files.values())}, 0
    refs, defs_found = [], 0
    for path, lns in files.items():
        lang = lang_of(path)
        if lang is None:
            continue
        text = repo.show(path, overlay)
        code, spans, defs = definitions(text, lang) if lang in ("ruby", "js") else (text.split("\n"), [[] for _ in text.split("\n")], [])
        raw_lines = text.split("\n")
        for d in defs:
            if d["name"] == term:
                defs_found += 1
        for ln in sorted(set(lns)):
            if ln - 1 >= len(raw_lines):
                continue
            raw = raw_lines[ln - 1]
            if len(raw) > MAX_LINE:
                continue
            for m in rx.finditer(raw):
                c = classify(path, lang, code[ln - 1], raw, m.start(), term, spans[ln - 1], owner_short, repo_is_rails)
                if not c:
                    continue
                kind, conf = c
                refs.append({"file": path, "line": ln, "kind": kind, "how": "rails" if kind in ("callback", "alias", "dispatch", "symbol") and lang == "ruby" else how,
                             "confidenceHint": conf, "caller": enclosing(defs, ln) if defs else None,
                             "text": raw.strip()[:200], "inPR": path in overlay})
                break
    return {"refs": refs}, defs_found


def select_pr(pr, idx):
    """One PR's view of a multi-PR workspace: its files at their own repo paths, its base commit.
    Returns (pr, output suffix)."""
    prs = pr.get("prs") or []
    if not prs:
        return pr, ""
    if idx >= len(prs):
        sys.exit(f"--pr {idx}: the workspace has {len(prs)} PRs.")
    meta = prs[idx]
    sub = dict(pr)
    sub.update(repo=meta["repo"], number=meta["number"], baseSha=meta["baseSha"], base=meta["base"], headSha=meta["headSha"])
    sub["files"] = [dict(f, path=f.get("repoPath", f["path"])) for f in pr["files"] if f.get("pr", 0) == idx]
    return sub, ("" if idx == 0 else "-" + meta["name"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--repo", required=True, help="local checkout of the PR's repo")
    ap.add_argument("--search-root", help="folder whose git repos are searched as siblings")
    ap.add_argument("--max-refs", type=int, default=40)
    ap.add_argument("--pr", type=int, default=0, help="which PR of a multi-PR workspace (0 = main)")
    a = ap.parse_args()
    pr, suffix = select_pr(json.load(open(os.path.join(a.workdir, "pr.json"))), a.pr)

    # Pin to the PR's base commit when the checkout has it.
    probe = subprocess.run(["git", "-C", a.repo, "cat-file", "-e", pr["baseSha"] + "^{commit}"], capture_output=True)
    if probe.returncode == 0:
        ref, ref_note = pr["baseSha"], f"PR base commit {pr['baseSha'][:7]}"
    else:
        ref = f"origin/{pr['base']}"
        date = subprocess.run(["git", "-C", a.repo, "log", "-1", "--format=%cs", ref], capture_output=True, text=True).stdout.strip()
        ref_note = f"{ref} as last fetched ({date or 'unknown date'}); the PR's base commit isn't in this checkout, run `git fetch` for an exact match"
    repo = Repo(a.repo, ref)
    overlay = {f["path"]: f["newContent"] for f in pr["files"] if f.get("newContent") is not None}
    is_rails = subprocess.run(["git", "-C", a.repo, "cat-file", "-e", f"{ref}:config/routes.rb"], capture_output=True).returncode == 0

    syms = changed_symbols(pr)
    out_syms, unique_owner = [], {}

    # Rails: routes for changed controller actions
    routes_out = []
    routes = parse_routes(repo.show("config/routes.rb", overlay)) if is_rails else []
    for s in syms:
        m = re.match(r"app/controllers/(.+)_controller\.rb$", s["file"])
        if m and s["kind"] == "method":
            ctrl = m.group(1)
            for verb, path, c, act, ln in routes:
                if c == ctrl and act == s["name"]:
                    routes_out.append({"symbol": f"{s['owner'] or ctrl}#{s['name']}", "verb": verb, "path": path,
                                       "file": "config/routes.rb", "line": ln, "how": "rails", "confidence": "high"})

    def routes_for_action(ctrl_path, action):
        return [r for r in routes if r[2] == ctrl_path and r[3] == action]

    for s in syms:
        term = s["name"]
        entry = dict(s)
        if s["kind"] == "method" and term in COMMON:
            entry.update(refs=[], note="Name too common to trace by text."); out_syms.append(entry); continue
        if s["file"].endswith("_controller.rb") and s["kind"] == "method":
            entry["routes"] = [r for r in routes_out if r["symbol"].endswith("#" + term)]
        owner_short = (s.get("owner") or "").split("::")[-1] or None
        if s["kind"] in ("class", "module") and s["change"] != "added":
            entry.update(refs=[], total=0, truncated=0, note="Class body changed; see its methods.")
            if s["kind"] == "module":
                inc = []
                for path, ln, text in repo.grep(s["name"], word=True):
                    if re.search(r"\b(include|extend|prepend|helper)\s+" + re.escape(s["name"]) + r"\b", text):
                        inc.append({"file": path, "line": ln, "kind": "include", "how": "rails", "confidence": "high",
                                    "caller": None, "text": text.strip()[:200], "inPR": path in overlay})
                entry.update(refs=inc[: a.max_refs], total=len(inc), truncated=max(0, len(inc) - a.max_refs),
                             note=f"Module body changed; included by {len(inc)} classes.")
            out_syms.append(entry); continue
        res, ndefs = trace(repo, term, owner_short, overlay, a.max_refs, None, is_rails)
        if res.get("tooCommon"):
            entry.update(refs=[], note=f"{res['candidates']} text matches: too common to trace reliably.")
            out_syms.append(entry); continue
        refs = res["refs"]
        # Drop the definition's own lines and references from inside its own body.
        refs = [r for r in refs if not (r["file"] == s["file"] and s["line"] <= r["line"] <= s["end"] and r["kind"] != "test")]
        entry["definitions"] = ndefs
        for r in refs:
            hint = r.pop("confidenceHint")
            if hint:
                conf = hint
            elif r["kind"] == "test":
                conf = "high" if ndefs <= 1 else "medium"
            elif r["kind"] == "call":
                qualified = owner_short and re.search(re.escape(owner_short) + r"(\.|::)" + re.escape(term), r["text"])
                conf = "high" if (ndefs <= 1 or qualified) else "medium"
            else:
                conf = "low" if r["kind"] in ("template", "config") and ndefs > 1 else "medium"
            r["confidence"] = conf
        # Ruby concerns: their includers are consumers too.
        if s["kind"] in ("module",) or ("/concerns/" in s["file"] and s["kind"] == "method"):
            mod = owner_short if s["kind"] == "method" else s["name"]
            if mod:
                for path, ln, text in repo.grep(mod, word=True):
                    if re.search(r"\b(include|extend|prepend|helper)\s+" + re.escape(mod) + r"\b", text):
                        refs.append({"file": path, "line": ln, "kind": "include", "how": "rails", "confidence": "high",
                                     "caller": None, "text": text.strip()[:200], "inPR": path in overlay})
        # Aliases: trace the alias names one level deep.
        for r in [r for r in refs if r["kind"] == "alias"]:
            m = re.search(r"alias_method\s+:(\w+[?!]?)\s*,\s*:" + re.escape(term), r["text"]) or re.search(r"alias\s+:?(\w+[?!]?)\s+:?" + re.escape(term), r["text"])
            if m and m.group(1) != term and m.group(1) not in COMMON:
                sub, _ = trace(repo, m.group(1), owner_short, overlay, a.max_refs, None, is_rails)
                for x in sub.get("refs", []):
                    x.pop("confidenceHint", None)
                    if x["kind"] not in ("call", "test", "template"):
                        continue  # a role or key spelled like the alias, not a call
                    x.update(how="rails", confidence="medium", via=m.group(1))
                    refs.append(x)
        # Controller helpers: follow callers in controllers to the action (up to 3 hops inside the
        # controller), then to its route.
        hop_routes = []
        for r in refs:
            cm = re.match(r"app/controllers/(.+)_controller\.rb$", r["file"])
            if not (cm and is_rails and r.get("caller") and "#" in r["caller"]):
                continue
            if r["kind"] not in ("call", "dispatch", "callback") or r.get("confidence") == "low":
                continue
            ctrl = cm.group(1)
            text = repo.show(r["file"], overlay)
            c2, _, d2 = definitions(text, "ruby")
            frontier, seen_m, found = [r["caller"].split("#")[-1]], set(), []
            for _ in range(3):
                nxt = []
                for meth in frontier:
                    if meth in seen_m:
                        continue
                    seen_m.add(meth)
                    hit = routes_for_action(ctrl, meth)
                    if hit:
                        found += hit; continue
                    rx = ref_regex(meth)
                    for i, l in enumerate(c2, 1):
                        if rx.search(l) and not RB_DEF.match(l):
                            up = enclosing(d2, i)
                            if up and "#" in up:
                                nxt.append(up.split("#")[-1])
                frontier = nxt
            for verb, path, c, a2, ln in found:
                hop_routes.append({"symbol": f"{c}#{a2}", "verb": verb, "path": path, "file": "config/routes.rb",
                                   "line": ln, "how": "rails", "confidence": "high", "via": term})
        if hop_routes:
            uniq = {(h["verb"], h["path"]): h for h in hop_routes}
            entry["routes"] = list(uniq.values())
            for h in uniq.values():
                ex = next((x for x in routes_out if (x["verb"], x["path"]) == (h["verb"], h["path"])), None)
                if ex:
                    ex.setdefault("vias", [ex.get("via")])
                    if term not in ex["vias"]:
                        ex["vias"].append(term)
                else:
                    routes_out.append(dict(h, vias=[term]))
        order = {"high": 0, "medium": 1, "low": 2}
        kind_order = {"call": 0, "dispatch": 1, "callback": 2, "include": 3, "alias": 4, "symbol": 5, "template": 6, "config": 7, "string": 8, "test": 9}
        refs.sort(key=lambda r: (r["inPR"], order[r["confidence"]], kind_order.get(r["kind"], 9), r["file"], r["line"]))
        entry["total"] = len(refs)
        entry["refs"] = refs[: a.max_refs]
        entry["truncated"] = max(0, len(refs) - a.max_refs)
        out_syms.append(entry)

    # ------------------------------------------------------------------ cross-repo
    cross = []
    if a.search_root:
        pr_remote = repo.git("remote", "get-url", "origin").strip()
        siblings, seen_remote = [], {pr_remote}
        for d in sorted(os.listdir(a.search_root), key=len):
            p = os.path.join(a.search_root, d)
            if not os.path.isdir(os.path.join(p, ".git")) or os.path.abspath(p) == os.path.abspath(a.repo):
                continue
            rem = subprocess.run(["git", "-C", p, "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
            if rem in seen_remote:
                continue
            seen_remote.add(rem)
            head = subprocess.run(["git", "-C", p, "log", "-1", "--format=%h %cs"], capture_output=True, text=True).stdout.strip()
            branch = subprocess.run(["git", "-C", p, "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
            sha = subprocess.run(["git", "-C", p, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", rem)
            sib = Repo(p, "HEAD"); sib.url = f"https://github.com/{m.group(1)}" if m else None; sib.sha = sha
            siblings.append((sib, head, branch))

        terms = []  # (term, why, word)
        for r in routes_out:
            static = [seg for seg in r["path"].split("/") if seg and not seg.startswith(":")]
            path = "/" + "/".join(static)
            if len(static) >= 2 or (len(static) == 1 and len(static[0]) >= 10):
                terms.append((path, f"URL of {r['symbol']}", False))
        lits = set()
        ident = re.compile(r"^[A-Za-z_][\w.:-]{5,79}$")
        title = re.compile(r"^(?:[A-Z][\w]*)(?: [A-Z][\w]*){1,5}$")
        for f in pr["files"]:
            lang = lang_of(f["path"])
            if TEST_RE.search(f["path"]) or lang not in ("ruby", "js"):
                continue
            for side, content in (("n", f.get("newContent")), ("o", f.get("oldContent"))):
                if not content:
                    continue
                touched = {l[side] for h in f["hunks"] for l in h["lines"] if l["t"] == ("add" if side == "n" else "del")}
                code_l, spans_l = lex(content, lang)
                for ln in touched:
                    if ln - 1 >= len(code_l):
                        continue
                    for _, _, v in spans_l[ln - 1]:
                        v = v.strip()
                        if (ident.match(v) and ("_" in v or v[0].isupper())) or title.match(v):
                            lits.add(v)
                    for m in re.finditer(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]{3,})\b", code_l[ln - 1]):
                        lits.add(m.group(1))
        for v in sorted(lits):
            terms.append((v, "string or name the diff adds or removes", False))
        for s in syms:
            if s["kind"] in ("method", "function") and len(s["name"]) >= 12 and "_" in s["name"]:
                terms.append((s["name"].rstrip("?!"), f"changed {s['kind']} name", True))
            if s["kind"] in ("class", "module") and len(s["name"]) >= 8:
                terms.append((s["name"], f"changed {s['kind']}", True))
        seen_terms = set()
        for term, why, word in terms:
            if term in seen_terms:
                continue
            seen_terms.add(term)
            hits = []
            for sib, head, branch in siblings:
                for path, ln, text in sib.grep(term, word=word):
                    if len(text) > MAX_LINE:
                        continue
                    hits.append({"repo": sib.name, "file": path, "line": ln, "text": text.strip()[:200],
                                 "test": bool(TEST_RE.search(path)), "how": "cross-repo",
                                 "confidence": "medium" if (why.startswith("URL") or "string" in why) else "low"})
            if hits:
                hits.sort(key=lambda h: (h["test"], h["repo"], h["file"], h["line"]))
                cross.append({"term": term, "why": why, "total": len(hits), "hits": hits[:25]})
        searched = [{"repo": s.name, "commit": h, "branch": b, "url": s.url, "sha": s.sha} for s, h, b in siblings]
    else:
        searched = []

    result = {"repo": pr["repo"], "ref": ref, "refNote": ref_note, "rails": is_rails,
              "symbols": out_syms, "routes": routes_out, "crossRepo": cross, "searchedRepos": searched}
    with open(os.path.join(a.workdir, f"blast{suffix}.json"), "w") as fh:
        json.dump(result, fh, indent=1)

    # summary for the analyst
    print(f"Searched {pr['repo']} at {ref_note}; {len(searched)} sibling repos.")
    for s in out_syms:
        by = defaultdict(int)
        for r in s.get("refs", []):
            by[r["confidence"]] += 1
        own = (s.get("owner") + ("." if s.get("self") else "::" if s["kind"] == "constant" else "#")) if s.get("owner") else ""
        tail = s.get("note") or f"{s.get('total', 0)} refs (high {by['high']}, medium {by['medium']}, low {by['low']})" + (f", {s.get('definitions')} defs share the name" if s.get("definitions", 0) > 1 else "")
        print(f"  {s['change']:<8} {own}{s['name']}  [{s['file']}:{s['line']}]  {tail}")
    for r in routes_out:
        print(f"  route    {r['verb']} {r['path']} -> {r['symbol']}  (routes.rb:{r['line']})")
    for c in cross:
        repos = sorted({h['repo'] for h in c['hits']})
        print(f"  x-repo   {c['term']!r} ({c['why']}): {c['total']} hits in {', '.join(repos)}")
    print(f"Wrote {os.path.join(a.workdir, 'blast' + suffix + '.json')}")


if __name__ == "__main__":
    main()
