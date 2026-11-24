# USE flag review policy

This file is written for whoever (or whatever) is making the call on a flag,
using the output of `use-review explain`. It restates the goal from the
repository README in a form that can be applied flag by flag, and it doubles
as a prompt for an LLM reviewer.

## Goal

A fully functional KDE Plasma + GNOME desktop: every feature that could
plausibly be useful is enabled. Outdated, rarely used, or purely
developer-facing functionality is left off. Hardware-specific flags live in
`package.use/50-local.conf`, not the shared base configuration.

## How to decide

Base the decision on what the flag *actually does*, using the evidence in the
explain output, in this order of trust:

1. Upstream build files (`Declared in`, `Used in build files`, `What it gates`).
2. The ebuild excerpt (the mapping from flag to build option, any patches or
   install steps).
3. The Gentoo description. It is often vague or out of date. Where it
   disagrees with 1 or 2, say so.

Watch for these cases specifically:

- **Hidden scope.** One flag that maps to an option gating much more than the
  description says, such as a whole backend, library, or subdirectory. Or
  several flags that collapse into one option (for example, any one of
  `mysql`/`postgres`/`sqlite` turning on `WITH_SQL`).
- **Gentoo-only options.** The option is introduced by a patch in `files/`.
  Behaviour differs from what upstream documents.
- **Runtime-only flags.** These only add packages to RDEPEND. Enabling one
  costs disk space but carries no build risk. Enable it if the feature is
  wanted.
- **Implementation choices** (allocators, TLS/crypto backends, toolkit
  selection). Don't enable these just because they exist. Recommend one only
  if upstream documents a concrete benefit for a desktop, and note any
  conflicts. Allocator flags (jemalloc/tcmalloc/mimalloc) should stay at the
  default unless upstream recommends otherwise.
- **Automagic dependencies.** The flag only adds DEPEND but no build option.
  The build may pick up the library whether or not the flag is set.
- **Server, legacy, debug, or experimental functionality.** Off unless there is a
  desktop use case.

## Output format (one block per flag)

```
cat/pkg flag: ENABLE | LEAVE DEFAULT | DISABLE | NEEDS HUMAN
  what it really does: <one or two sentences, citing file:line from the evidence>
  cost/risk: <new dependencies, conflicts, build risk>
  confidence: high | medium | low
```

Use `NEEDS HUMAN` whenever the evidence is missing or contradictory. Never guess.

Decisions become either a line in `package.use/00-base.conf` (with a comment
when it is not obvious) or, for "leave default", a ledger entry:

```
tools/use-review/use_review.py mark cat/pkg flag -m "reason"
```
