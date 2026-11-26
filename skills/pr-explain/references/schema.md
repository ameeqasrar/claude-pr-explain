# explain.json schema

`build.py` checks all of this and lists every problem. `example-explain.json` is a small finished example for a made-up PR (customers cancelling an order in a shop app): two layers, seven summaries, three findings, and a blocked verdict.

```jsonc
{
  "headline": "Plain-English sentence: what changes for users",   // overview title
  "whatChanged": "Markdown. 2-4 short paragraphs: before, after, who's affected. Ends with a lead-in to the layer list.",
  "excluded": ["yarn.lock"],                // optional: changed files left out of layers (generated, vendored)

  "layers": [{
    "title": "Record how each request signed in",          // verb phrase, under ~7 words
    "summary": "One line shown in the layer header.",
    "detail": "Markdown, optional. What to know before reading the diff.",
    "files": ["app/models/user_token.rb", "..."],          // order = reading order inside the layer
    "dependsOn": [1],                                       // earlier layer numbers (1-based), optional
    "diagram": {                                            // optional; omit for tests/config/docs
      "kind": "sequence | flowchart | class | state | er",
      "title": "What the diagram shows",
      "mermaid": "sequenceDiagram\n  ...",                // Mermaid 10 syntax
      "caption": "Markdown, optional, one line"
    }
  }],

  "ranges": [{                                              // summaries attached to line spans
    "file": "app/models/user.rb",
    "side": "new",                                          // "old" only for spans of deleted lines
    "start": 1036, "end": 1057,                             // inclusive, from diff.txt numbering
    "title": "portal_powers_off? and its log line",
    "summary": "Markdown inline: why it matters, 1-2 sentences.",
    "complexity": "low | medium | high"                     // shown as Routine / Needs care / Core logic
  }],

  "findings": [{
    "id": "F1",                                             // unique; referenced by attention items
    "file": "app/models/user.rb", "side": "new", "start": 1040, "end": 1048,   // card shows after `end`
    "type": "potential_issue | refactor | nitpick",
    "severity": "critical | major | minor | trivial | info",
    "category": "functional | security | data | performance | stability | maintainability",
    "effort": "low | high", "reward": "low | high",
    "title": "Markdown inline, one line: the problem",
    "body": "Markdown: what goes wrong, for whom, and the fix.",
    "suggestion": "Full replacement text for lines start..end (new side only), or omit",
    "existing": [{"file": "app/helpers/money_helper.rb", "line": 12,        // optional: code this duplicates or overrides
                  "symbol": "MoneyHelper#format_price", "repo": "omit for this repo", "note": "optional"}],
    "status": "open"                                        // build sets "addressed" on findings carried from --previous
  }],

  "attention": [{
    "band": "blocker | high | pending | advisory",
    "priority": "p1 | p2 | p3 | p4",                        // reading order inside the list
    "severity": "critical | major | minor | trivial",       // optional chip
    "title": "What to do", "body": "Markdown, 1-3 sentences",
    "owner": "Short tag, optional (e.g. Rollout, Needs deploy order)",
    "finding": "F1",                                        // optional: adds "Go to code"
    "url": "https://...", "urlLabel": "Open #1008"          // optional: https only
  }],

  "readiness": {
    "band": "ready | caution | risky | blocked",
    "confidence": "high | medium | low",
    "summary": "Markdown, 1-2 sentences: the verdict and the reason",
    "drivers": [{"text": "...", "severity": "critical | major | minor", "status": "open | addressed | superseded | not relevant"}]
  },

  "blastRadius": {                                          // optional
    "summary": "Markdown",
    "items": [{
      "kind": "consumer | dependency | test",
      "symbol": "can_manage_partner_settings?",
      "file": "path in this PR (makes it a link), optional",
      "riskLevel": "critical | major | minor | info", "risk": "Markdown, optional", "note": "Markdown, optional",
      "confidence": "high | medium | low",                  // optional
      "where": [{"repo": "other repo's folder name, omit for this repo",
                 "file": "app/controllers/sending_ips_controller.rb", "line": 8, "note": "optional",
                 "how": "index | rails | cross-repo | search",   // from blast.json; search = found by hand
                 "confidence": "high | medium | low"}]
    }]
  },

  "architecture": {                                         // optional
    "summary": "Markdown",
    "mermaid": "flowchart LR\n  ...",
    "deltas": [{"system": "Rails", "kind": "added | changed | removed | unchanged", "change": "One line"}]
  }
}
```

## Several PRs

When `fetch_pr.py` gets several PRs, files from every PR but the first are prefixed with their repo's name: `front-end-netlify/src/App.tsx`. Use the prefixed path in `layers.files`, `ranges`, `findings` and `excluded`. In `blastRadius.where` and `existing`, give `repo` and the plain path; the page links to the file in the workspace when it's part of a PR.

## Line numbers

Take them from `diff.txt`: the left column is the old file, the right column the new file. Ranges and findings may point at unchanged lines outside a hunk; the viewer expands context to show them (build warns, which is fine).

## Mermaid tips

- Quote labels with punctuation: `A["Signed in by\nthis request?"]`.
- `flowchart TD` for decision chains, `LR` only for 3-6 boxes in a row.
- Sequence diagrams: short participant aliases (`participant AC as ApplicationController`).
- Test syntax mentally: one statement per line, no trailing semicolons, no `&` in node ids.
