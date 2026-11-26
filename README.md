# pr-explain

A Claude Code skill that turns a pull request, or every PR for one ticket, into an interactive review page. The page is published as a private Claude artifact.

The page has:
- **Layers:** the change split into numbered steps in reading order, each with its diff and a diagram where it helps.
- **Summaries** on each part of the diff, labelled Routine, Needs care or Core logic.
- **Findings** with severity, suggested fixes, and links to existing code the PR duplicates.
- **Needs your attention** and a **merge readiness** verdict.
- **Blast radius:** a map of the code that calls what changed, the routes it reaches, and other repos that call in, built from a real trace of the codebase.

## Install

In Claude Code:

```
/plugin marketplace add ameeqasrar/claude-pr-explain
/plugin install pr-explain@claude-pr-explain
```

Or from a terminal:

```
claude plugin marketplace add ameeqasrar/claude-pr-explain
claude plugin install pr-explain@claude-pr-explain
```

Restart Claude Code after installing.

## Use

```
/pr-explain ABC-123
/pr-explain https://github.com/<org>/<repo>/pull/123
/pr-explain <org>/<repo>#123
```

A ticket key finds every PR that mentions it across the org's repos and explains them on one page. When it's done, Claude gives you the link to the page. The page is private until you share it.

## What you need

- **Claude Code** with artifacts.
- **`gh`** signed in, with access to the repos.
- **Python 3.** The scripts use only the standard library, so there's nothing to install.
- **Local checkouts** of the repos for the blast radius and reuse checks. Keep each repo as a folder in one parent folder (for example `~/projects/<product>/<repo>`), and Claude will search the others from there. The trace only reads them.

## Limits

- The page is a snapshot of each PR's head commit. Run it again after new pushes.
- The blast radius combines a code index, Rails conventions and cross-repo search. It isn't a type-checked call graph. It can't see calls made with computed names, or repos outside the parent folder.
- The page is read-only: it never comments, approves or merges. Merging uses a `gh pr merge` command it shows you, which you run yourself.
