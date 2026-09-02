# Versioning & release consistency

**One rule to remember:** a release's **git tag** and its **CHANGELOG heading**
are always the *same number*. Everything below just makes that automatic instead
of something we notice and clean up later.

Why it matters here: the version is derived from the git tag by `setuptools-scm`
(nothing is hand-written in code), so the tag is the single source of truth. If
the CHANGELOG says one thing and the tag says another, the shipped build and its
notes disagree — which is exactly the drift that left the whole 1.2 line sitting
under `[Unreleased]` with no headings.

## The number: `MAJOR.MINOR.FEATURE.PATCH`

Four parts, e.g. `1.2.5.5`. A PR bumps **one** of them — the highest one it
touches:

| Part | Bump when a PR… | Example |
|---|---|---|
| **MAJOR** — `1`.x.x.x | breaks an existing install/config, or removes something people rely on | a config format old installs can't read |
| **MINOR** — x.`2`.x.x | ships a milestone: a group of features together | "the Discord remote-control surface" |
| **FEATURE** — x.x.`5`.x | adds **one** new user-facing capability | a new `/command`, a new GUI tab, a new option |
| **PATCH** — x.x.x.`5` | anything else that ships: bug fixes, behaviour corrections, docs, packaging, tests, refactors, removing dead code | the everyday PR |

**Most PRs are a PATCH bump. That's expected — not sloppy.**

### Mapping "additions / edits / subtractions" to a part
- **Addition** of a new capability → **FEATURE** (or **MINOR** if it's a whole grouped milestone shipping at once).
- **Edit / fix** to something that already exists → **PATCH**.
- **Subtraction**: removing a capability users rely on → **MAJOR**; removing cruft, dead code, or docs → **PATCH**.

### Mixed PRs (the common case)
Pick the **highest** part the PR touches, bump it, and reset everything to its
right to `0`:

- feature **and** bug fixes → **FEATURE** bump (PATCH resets to 0)
- only fixes / edits / docs → **PATCH** bump
- something breaking → **MAJOR** bump (all lower parts reset to 0)

Examples: `1.2.5.5` + a feature → `1.2.6.0`; `1.2.6.0` + a fix → `1.2.6.1`;
`1.2.6.1` + a breaking change → `2.0.0.0`.

## The consistency lock (the whole point)

1. The **git tag is the only source of the version** — `setuptools-scm` reads it.
2. Therefore a release's **CHANGELOG heading must equal its tag, exactly**:
   `## [1.2.5.5]` in the changelog ⇢ tag `1.2.5.5`. No drift, ever. The only
   spellings that count as the same number are a leading `v` (`v1.2.5.5`) and
   omitted trailing zeros (`1.2.8` is `1.2.8.0`) — the release gate accepts
   those and nothing else.
3. Work not yet assigned a version lives under a single `## [Unreleased]`. It
   *becomes* a release the moment we tag — never leave shipped work sitting under
   `[Unreleased]`.

## Per-PR checklist (definition of done)

The numbering, the heading and the tag are **automated** — see below. What a PR
still owes:

- [ ] File its changes under `### Added / Changed / Fixed / Removed` inside the
      single `## [Unreleased]` section. Leave the heading alone.
- [ ] If it is not an everyday PATCH, label the PR `bump:feature`, `bump:minor`
      or `bump:major` (highest part it touches, from the table).
- [ ] If it ships nothing users see and should not cut a release at all — a CI
      tweak, a README typo — leave `[Unreleased]` empty, or label it
      `no-release`.

Nothing to reconcile afterward: the tag and the heading are written from the
same number, by the same run.

## How Claude assigns the number each PR

When we work a PR together, I will:

1. Read the diff and categorise it (addition / edit / subtraction, and how big).
2. File the entries under `## [Unreleased]`.
3. Tell you which `bump:` label the PR needs — or that it's a plain PATCH and
   needs none.

The number itself is the automation's job, not mine and not yours: merging the
PR is what cuts the release.

## The automation (what actually cuts a release)

Two workflows, one number.

**`.github/workflows/release-cut.yml` — "Cut release".** Runs on every merge to
`main`:

1. Picks the part from the merged PR's label (`bump:major` / `bump:minor` /
   `bump:feature` / `bump:patch`), defaulting to **PATCH** — most PRs are, and
   that is expected, not sloppy.
2. Runs `scripts/bump_version.py`, which computes the next number from the
   **highest existing git tag** (the tag is the version; the changelog is the
   side that drifts), renames `## [Unreleased]` to it with today's date, and
   opens a fresh empty `## [Unreleased]` above.
3. Commits that to `main`, tags **the same commit** with the same number, and
   starts the Release build for the tag.

It stops early, with no release and no noise, when `[Unreleased]` is empty or
the PR is labelled `no-release`. "Run workflow" cuts one by hand — pick the
part, or tick `dry_run` to see the number it would use without writing
anything. It never publishes: `release.yml` still produces a **draft** GitHub
Release for a human to review.

Because the tag is pushed by CI's own token, which by design does not trigger
other workflows, the cut dispatches `release.yml` explicitly rather than
waiting for a tag-push event that would never arrive.

**`scripts/check_changelog.py` — the gate.** `release.yml` runs it first, before
anything is built, and fails the release if the tag and its CHANGELOG heading
disagree, if the section is empty, if it isn't the top dated one, or if shipped
work is still sitting under `[Unreleased]`. `bump_version.py` runs the same
check against the file it just wrote, so a bad cut fails at the cut instead of
in the release run.

Between them, a mismatched tag can't ship and a release can't be forgotten —
the consistency stopped depending on anyone remembering.

### If you are tagging by hand anyway

Push the exact number in the top dated heading (`1.2.8` and `1.2.8.0` count as
the same number; a leading `v` is fine). Anything else is refused by the gate.

## Worked example — this branch

Last release: `1.2.5.4`. This branch is docs corrections + behaviour fixes + a
GUI single-instance fix — no new user-facing feature, nothing breaking →
**PATCH** → `1.2.5.5`. CHANGELOG heading `## [1.2.5.5] — 2026-07-19`, tag
`1.2.5.5`. Matched.
