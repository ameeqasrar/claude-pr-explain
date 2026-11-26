---
name: pr-explain
description: Turn a GitHub pull request, or every PR for one ticket across repos, into one interactive "explain" workspace. Groups the change into numbered layers in reading order, with split/unified diffs, range summaries, a Mermaid diagram per layer, labelled findings with suggestions, a "needs your attention" list, merge readiness, blast radius, architecture impact, activity, search and keyboard navigation. Published as a private Artifact. Use when the user says /pr-explain, "explain this PR", "walk me through PR <n>", "explain ABC-123", or pastes a PR link and wants it explained or reviewed visually.
---

# PR Explain

Builds an interactive review workspace for a GitHub PR, or every PR for one ticket, and publishes it as a private Artifact.

`<skill-dir>` in the commands below is this skill's folder: the "Base directory for this skill" shown when it loads.

You do three things: **fetch** (script), **analyse** (you, by reading the code), **build and publish** (script + Artifact tool). The analysis is the product. The viewer only displays what you write, so spend your effort there.

## 0. Input

Accept a PR URL, `owner/repo#123`, a bare number (resolved against the current directory's git remote), several of these, or a ticket key like `ABC-123`. If the user names a repo by folder, `cd` there first. Needs `gh` signed in.

**One change, one page.** A ticket often ships as several PRs across repos (back end, API, front end). Explain them together, never just the biggest: the risks usually sit where they meet. For a ticket key, find every PR that names it, open or merged, in all of the org's repos:

```bash
gh search prs "<KEY>" --owner <org> --limit 50 --json repository,number,title,state,url
```

Keep the PRs whose title or branch carries the key, and drop old ones that a later PR replaced. Put the PR with the core logic first: it's the main PR, and the page's title, branch and snapshot come from it.

Work folder: `<scratchpad>/pr-<repo>-<number>/` of the main PR (create it). If the folder already has an `explain.json` from an earlier run, move it to `explain.prev.json` before writing a new one: the build marks findings that disappeared as addressed.

## 1. Fetch

```bash
python3 <skill-dir>/scripts/fetch_pr.py <main-pr-ref> [<other-pr-ref> ...] --out <workdir>
```

With several PRs, the others' files are prefixed with their repo's name (`front-end-netlify/src/App.tsx`). Use those prefixed paths everywhere in `explain.json`. `diff.txt` holds every PR, each under its own header.

Writes `pr.json` (metadata, files with parsed hunks and full old/new content, review threads, checks, timeline) and `diff.txt` (the diff with explicit old/new line numbers). Read **all** of `diff.txt`. Every line number you write later comes from it.

## 2. Trace the blast radius

```bash
python3 <skill-dir>/scripts/blast_radius.py <workdir> --repo <local checkout of the PR repo> [--search-root <folder of sibling repos>]
```

With several PRs, run it once per PR, adding `--pr 1`, `--pr 2` and so on with that PR's checkout as `--repo`; those runs write `blast-<repo>.json`. The build puts every trace on the page: the map draws them all, labelled by repo, and the reference list has a section per PR. Skip PRs that only add tests (a Cypress spec has no callers); the map lists them as not traced. Needs a local checkout; it reads it and never changes it. It searches at the PR's exact base commit when the checkout has it, and says so if it has to fall back to `origin/<base>`. Pass `--search-root` as the folder that holds the product's other repos (for example `~/projects/<product>` when each repo is a folder in it); sibling repos are searched at their checked-out commit. Takes about 20 seconds on a Rails monolith.

It writes `blast.json` and prints a summary. Three sources, each reference labelled with how it was found (`how`) and how sure it is (`confidence`):

- **Code index** (`index`): a built-in Ruby/JS/TS lexer that tells code from comments and strings and knows the enclosing class and method. It finds the definitions the PR changed, then real references to them. `high` when the name has one definition or the call names the class; `medium` when several definitions share the name.
- **Rails rules** (`rails`): routes to controller actions (following private helpers up to three hops to the public action), callbacks and other `:symbol` references, `alias_method` and `delegate` (aliases traced one level), concerns and the classes that include them, and string job dispatch like `Executioner.perform_async('Site', 'delete_site', id)`.
- **Other repos** (`cross-repo`): the URLs of routes the change reaches, string literals and env-style names the diff adds or removes, and distinctive changed method or class names, searched in every sibling repo.

The URL matches are usually the most valuable: they show which front ends and services call the Rails endpoints the PR changed, which is where deploy-order problems and silent breakages hide. **Open every high-confidence cross-repo hit that isn't a test, and read the call site**: which token does it send, which folder or record does it touch, would the new rule refuse it? That's where uploads, webhooks and other callers that a new rule would refuse usually turn up.

The script misses metaprogramming it can't see (`send` with a computed name, `method_missing`, `define_method`) and anything outside the searched repos. Say so when it matters.

## 2b. Look for code that already exists

```bash
python3 <skill-dir>/scripts/reuse.py <workdir> --repo <local checkout of the PR repo>
```

Takes under 10 seconds. With several PRs, run it once per PR (`--pr N`, that PR's checkout as `--repo`). For every function, method or component the PR adds, it lists existing definitions at the base commit with the **same name** (and says when the new one **overrides a method from an included concern**), a **similar name**, or a **similar body** (renamed copies too). It also lists regexes, URLs and long messages the PR adds that already appear elsewhere. Writes `reuse.json`.

Duplicated logic is one of the most common problems in AI-written code, and reviewers rarely catch it because the diff looks clean. The script finds candidates; you decide. Open both sides:
- **Same job, same inputs and outputs?** Then the PR should call the existing code, or replace the existing copies with its new shared version. That's a finding.
- **Overrides an included concern's method?** Check it's on purpose and calls `super` if it should. A silent override changes behaviour for every caller of the concern's version.
- **Look alike but differ on purpose** (another model, other side effects, a deliberate fork)? Not a finding. At most an `info` note when the next person will be confused.

Also check by hand what the script can't see: helpers in `utils`, `lib`, `concerns`, `hooks` and shared components the PR could have used; and code that rebuilds what Rails, ActiveSupport, React, lodash, a gem or an npm package already in the project provides.

## 3. Analyse (the real work)

Read the diff, then read around it. Treat it like a staff engineer's review, not a summary of the diff.

- Open the changed files' surroundings at the PR head (content is in `pr.json`, or `gh api repos/O/R/contents/<path>?ref=<headSha>`). Use a local checkout if one exists: `git grep <symbol> origin/<base>` (read-only; never check out or modify the user's branch).
- Start from `blast.json`: who calls the changed symbols, which routes they reach, and who calls those routes from other repos. Then add what it can't find: PRs named in the description, feature flags, env vars, data the change depends on.
- Before you call anything a bug, confirm it: read the method, the caller, the schema or config. If it's already handled, it isn't a finding. Fewer, true findings beat many speculative ones.
- Check reuse (step 2b): did the PR write something the codebase already has? Name the existing code in the finding.
- Check rollout: does the code deploy on merge? Does it need another PR, a migration, an env var or a backfill first? Deploy-order problems are usually the real blocker.

Then write `<workdir>/explain.json` following `references/schema.md`. The key rules:

**Layers.** 2 to 7 numbered layers in the order you'd explain the change to a teammate: the contract or data first, then the logic that uses it, then the entry points, then tests. Group by meaning, not folder: a contract, its implementation and its tests may share a layer. Every changed file goes in exactly one layer, or in `excluded` if it's generated, vendored or a lockfile. `dependsOn` lists earlier layer numbers that must be read first.

With several PRs, layers cut across repos by meaning: the API field and the screen that reads it can share a layer, and the reading order usually follows the data from back end to front end. Every PR's files must be in a layer. Add a deploy-order item to Needs your attention whenever one PR must ship before another.

**Diagrams.** One per layer, only when it shows something the code doesn't make obvious. Pick the type by what changed: `class` for schema or type changes, `sequence` for request/response or cross-service calls, `flowchart` for decisions, validation and data movement, `state` for lifecycle transitions, `er` for persistence. No diagram for tests, config, docs or cosmetic edits. Keep it under about 10 nodes, labels under 5 words (use `\n` to wrap), and use `flowchart TD` for decision chains (LR gets too wide to read).

**Ranges.** Summaries attached to line spans, not whole files: one per meaningful hunk or group of hunks. Title names what the code does; summary says why it matters, in one or two sentences. `complexity` says what kind of code the span is, and the page shows it as a label: `low` shows as **Routine** (mechanical: renames, config, imports, tests that mirror the code), `medium` as **Needs care** (changed conditions, moved steps, new parameters callers depend on), `high` as **Core logic** (what decides the change's behaviour). It describes the code, not the engineer's effort or a risk; risks are findings. Lines are on the `new` side unless the span is only deleted lines.

**Findings.** Each has four independent labels:
- `type`: `potential_issue` (possible defect), `refactor` (better structure), `nitpick` (optional polish)
- `severity`: `critical` > `major` > `minor` > `trivial`; `info` for guidance that isn't a problem
- `category`: `functional`, `security`, `data`, `performance`, `stability`, `maintainability`
- `effort` + `reward` (`low`/`high` each): low/high is a quick win, high/high a heavy lift, low/low a low-value fix, high/low a poor trade-off

Body: what goes wrong, for whom, and the fix. For duplicated code, use `type: refactor`, `category: maintainability` (severity `minor`, or `major` when the copies cover money, permissions or sending and will drift apart), and list the existing code in `existing` so the card links to it. Say what drifts: "a fix to one copy won't reach the other". Add `suggestion` (the full replacement for lines `start` to `end`, new side only) when the fix is local and certain.

**Needs your attention.** The overview's triage list, in four bands: `blocker` (prevents a safe merge), `high` (do before or right after merge), `pending` (waiting on reviews, checks or another PR), `advisory` (optional). Priority `p1` to `p4` sets reading order. Link a `finding` id or an https `url`.

**Merge readiness.** `band`: `ready` / `caution` / `risky` / `blocked`, with `confidence` (`high`/`medium`/`low`) and `drivers` (each with severity and status `open` / `addressed` / `superseded` / `not relevant`). This is your judgement, separate from GitHub's mergeability, which the page shows beside it.

**Blast radius and architecture impact.** Blast radius is your curated list: the consumers, dependencies and tests that matter, each `where` entry with `file:line`, plus `repo` for other repos, and the `how` and `confidence` from `blast.json` (use `search` for ones you found by hand). Lead with the routes and outside callers that change behaviour. The page also shows every reference `blast.json` found, with filters, under the curated list, so don't copy it all. Architecture impact is one system-level diagram plus deltas between systems. Leave either out when the change stays inside one component.

**Writing.** Plain English, short sentences, product consequences first ("partner staff lose X"), code second. No em dashes. Name things by what users recognise. Don't repeat the PR description back; explain what it means.

## 4. Build

```bash
python3 <skill-dir>/scripts/build.py <workdir> [--previous <workdir>/explain.prev.json]
```

It validates `explain.json` against the PR (every file layered, lines inside files, enums, ids) and prints every problem. Fix them all and re-run until it prints `"ok": true`. A warning that a range starts outside the diff is fine when intended: the viewer expands context to show it.

It embeds every trace in the work folder (`blast.json` and each `blast-<repo>.json`). Output: `<workdir>/site/index.html` + `data.json` (publish these), and `preview.html` (data inline).

## 5. Publish and check

Publish with the Artifact tool:
- `file_path`: `<workdir>/site/index.html`
- `files`: `{"data.json": "<workdir>/site/data.json"}`
- No `capabilities`: the page is read-only and needs none.
- `icon`: `code`; `description`: one sentence naming the PR
- On a re-run for the same PR in the same conversation, publish the same `file_path` to keep the URL.

Then open the URL in the built-in browser and look at it once: the overview, one layer with findings, one diagram. Fix what's visibly wrong (bad line anchors, unreadable diagram) and republish. Don't loop.

## 6. Report

Give the link, then in plain English: what the PR does for users, the merge-readiness verdict and why, and the top findings in one line each. Say what you couldn't verify. The artifact is private until the user shares it.

## What the page does (so you can describe it)

- **Top bar:** state, title, branch, snapshot commit, search and view settings. The page is read-only: it never posts to GitHub.
- **Blast radius:** a map of how far the change reaches (changed code, the files that call it, the routes they sit behind, other repos), drawn from `blast.json`: top 8 per column picked right to left so paths to the outside survive, solid links for high confidence, dashed for medium, low left out; blue borders mark code a PR changed and grey borders code no PR touches (callers left unchanged are the real blast radius); boxes named in the curated list carry a small risk tag; repeated file names show their folder; hover lights a path; clicking a box filters the reference list below to that item (a changed function, a caller file, a route or a repo), clicking a column header filters it to that column, and "Show all" clears it. The Changed column covers code the PRs added, modified and removed. Below it, the curated list with source and confidence labels and links to each line (in the PR, or on GitHub for other repos), then every reference found, filterable by confidence, source and tests, with the routes the change reaches.
- **Left rail:** Overview, Blast radius, Architecture impact, Activity; then layers (Layers mode) or a file tree (Files mode), with viewed progress.
- **Overview:** layer, file and finding tiles; summary; needs-your-attention bands; merge readiness with drivers, GitHub mergeability and checks; all findings.
- **Layer:** Files and Diagram tabs; split or unified diff with syntax highlighting; range summaries labelled Routine, Needs care or Core logic (Routine collapsed by default); inline findings with suggestions you can copy and links to existing code they duplicate; existing GitHub review threads, read-only; expand 20 lines of context; click a line number (shift-click to extend) to highlight lines; mark viewed; whitespace filter.
- **Right panel:** Summaries and Findings (findings plus existing GitHub threads, filtered by severity, resolved, outdated and other bots).
- **Merge:** builds a `gh pr merge --match-head-commit` command to copy, so nobody merges code they didn't see.
- **Keys:** J/K layer, L/H file, Shift+J/K summary or finding, F/D tabs, U split/unified, [ and ] panels, Z focus mode (layers only: hides both panels so the diff gets the full width; an Exit focus button shows while it is on), O open on GitHub, / search, ⌘K or ? commands, Esc clear.
- Search covers file paths, changed code, summaries and comments. Links address views by hash (`#overview`, `#layer-2`, `#layer-2-diagram`, `#blast-radius`, `#architecture`, `#activity`).

## Limits to state honestly

- The analysis is a snapshot of one head commit. Re-run after new pushes.
- Blast radius combines a lexer-based code index, Rails rules and cross-repo search. It's not a type-checked call graph: dynamic dispatch with computed names and repos outside `--search-root` are invisible to it.
- The page doesn't comment, review or chat. Merging happens through the `gh` command it builds, run by the user.
- Files over 200,000 characters can't expand context; their hunks still show.
