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
   `## [1.2.5.5]` in the changelog ⇢ tag `1.2.5.5`. No drift, ever.
3. Work not yet assigned a version lives under a single `## [Unreleased]`. It
   *becomes* a release the moment we tag — never leave shipped work sitting under
   `[Unreleased]`.

## Per-PR checklist (definition of done)

Before a PR is ready to tag:

- [ ] Work out the next version from the table (highest part the PR touches).
- [ ] In `CHANGELOG.md`, rename `## [Unreleased]` to `## [<version>] — <YYYY-MM-DD>`, and add a fresh empty `## [Unreleased]` above it.
- [ ] File the PR's changes under `### Added / Changed / Fixed / Removed` in that section.
- [ ] The tag you push is that **exact** `<version>`. `release.yml` builds and drafts the GitHub release from it.

If the tag and the top dated CHANGELOG heading match, we are consistent by
construction — nothing to reconcile afterward.

## How Claude assigns the number each PR

When we work a PR together, I will:

1. Read the diff and categorise it (addition / edit / subtraction, and how big).
2. Compute the next version from the last tag using the table above.
3. Write the CHANGELOG heading + entries to that **exact** number.
4. Tell you the **one tag** to push — the same number.

So you never have to work out the number or reconcile it later; you push the tag
I give you, and the changelog already matches it.

## Optional: make drift impossible

A short guard in `release.yml` can **fail the build** if the tag being released
doesn't match the newest dated CHANGELOG heading. With it in place, a mismatched
tag can never ship — the consistency stops depending on anyone remembering. Say
the word and I'll wire it.

## Worked example — this branch

Last release: `1.2.5.4`. This branch is docs corrections + behaviour fixes + a
GUI single-instance fix — no new user-facing feature, nothing breaking →
**PATCH** → `1.2.5.5`. CHANGELOG heading `## [1.2.5.5] — 2026-07-19`, tag
`1.2.5.5`. Matched.
