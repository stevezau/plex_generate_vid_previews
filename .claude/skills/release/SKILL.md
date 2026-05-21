---
name: release
description: Use when the user wants to cut a release of media_preview_generator — merges dev into main, drafts plain-English release notes from the commits since the last tag, suggests the next semver tag, and publishes a GitHub release after explicit confirmation.
---

# Release

Cuts a release: fast-forwards `main` to `dev`, then publishes a GitHub release
(which creates the version tag atomically). The git tag drives the package
version — `pyproject.toml` uses setuptools-scm with `write_to = _version.py` —
so the tag must be bare semver with **no `v` prefix** (`4.1.0`, not `v4.1.0`).

Run only when the user explicitly asks to release. Per project policy, "finish"
or "do it all" does NOT authorize a release on its own.

## Guardrails

- Never force-push. Never delete tags or releases.
- Never touch `/data` (read-only Plex media).
- The `plugin-v*` tags belong to a separate component — ignore them entirely
  when finding the last release or computing the next version.
- Stop and report if any preflight check fails. Do not "fix" a dirty tree or
  divergence automatically.

## Procedure

### 1. Preflight

```bash
git rev-parse --abbrev-ref HEAD          # must be "dev"
git status --porcelain                   # must be empty (clean tree)
git fetch origin --quiet
git rev-list --left-right --count origin/main...dev   # left count must be 0
```

- Branch is not `dev` → stop, tell the user to switch to `dev`.
- Tree is dirty → stop, list the uncommitted files.
- Left count > 0 (main is ahead of local dev) → stop. main has diverged; a
  fast-forward is unsafe. Report and let the user resolve it.

Compare against **local** `dev`, not `origin/dev` — local `dev` is the source of
truth and may have unpushed commits. Everything downstream (notes, tag, push)
must describe the same local `dev` state.

### 2. Find the last release and draft notes

```bash
LAST_TAG=$(git tag --list --sort=-v:refname | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
git log "$LAST_TAG"..dev --pretty='%s'   # commit subjects since last release
git diff "$LAST_TAG"..dev --stat         # files touched, for context
```

If `$LAST_TAG` is empty (no semver tag found) → stop and report. Do not run the
`git log`/`git diff` with an empty left side; `..dev` would dump the entire
history and silently mis-scope the notes.

Write release notes for **end users**, not commit-speak:

- **Title**: short, plain English, names the headline change
  (e.g. "Stale-mount diagnostics & job-log polish"). No version number.
- **Description**: tight bullets grouped as **New** (from `feat:`) and
  **Fixes** (from `fix:`). Translate each into what a user notices. Drop
  pure-internal commits (`test:`, `chore:`, refactors) unless user-visible.

### 3. Suggest the next version

Parse the commit subjects from step 2 against `$LAST_TAG` (MAJOR.MINOR.PATCH):

- Any `feat:` since the last tag → bump **MINOR**, reset PATCH (`4.0.1` → `4.1.0`).
- Otherwise → bump **PATCH** (`4.0.1` → `4.0.2`).
- Breaking changes (`feat!:`, `BREAKING CHANGE`) → confirm a MAJOR bump with the user.

Propose the computed version and **ask the user to confirm or type a different
tag**. Always wait for their answer — the tag is theirs to decide.

### 4. Confirmation gate

Show the user one block: final **tag**, **title**, and **description**. State
plainly that approving runs **push dev → advance main → publish release**, and
that advancing `main` is the point of no return (it triggers any CI/release
pipeline). Wait for an explicit OK. Do not proceed on silence or ambiguity.

### 5. Publish (only after OK)

```bash
COMMIT=$(git rev-parse dev)               # pin the reviewed commit
git push origin dev                       # publish local dev
git push origin dev:main                  # fast-forward main to dev
gh release create "<TAG>" --target "$COMMIT" --title "<TITLE>" --notes "<NOTES>"
```

Pass the captured `$COMMIT` SHA as `--target`, not `main` — this tags the exact
commit the notes were drafted from, even if `main` advances concurrently.
`gh release create` creates the tag and publishes the release in one step.
Report the release URL it prints.

## Failure handling

- `git push origin dev:main` rejected (non-fast-forward) → main moved since
  preflight. Re-fetch, re-check divergence, do not force.
- `gh release create` fails because the tag already exists → stop and report;
  do not overwrite. The user picks a new tag or deletes the bad one themselves.
