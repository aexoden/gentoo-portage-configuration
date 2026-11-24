# use-review

Tooling that makes USE flag review a finite task instead of an endless one.
It needs only Python 3.12+ and git. It doesn't need a Gentoo machine, because
it reads the tree straight from a git checkout of
[gentoo-mirror/gentoo](https://github.com/gentoo-mirror/gentoo), the same repo
`repos.conf/gentoo.conf` syncs from.

There are three ideas behind it:

1. **Review changes, not the whole tree.** `gentoo.revision` pins the tree, so
   each bump can be compared with the previous one. You then only look at
   what changed for the packages this configuration uses.
2. **Record every decision.** A flag set in `package.use` (on or off) is a
   decision. A flag deliberately left at its default goes in `reviewed.conf`.
   Anything else is "not yet reviewed" and gets listed.
3. **Don't trust the flag description.** `explain` traces each flag from the
   ebuild to the build-system option it sets. It follows any variables in
   between. With `--upstream` it continues into the upstream build files and
   the source files those options gate.

## Quick start

```bash
# A shallow tree holding just the revisions you need (~160 MB, ~20 s).
tools/use-review/use_review.py fetch-tree --tree ~/src/gentoo-tree
export USE_REVIEW_TREE=~/src/gentoo-tree     # or pass --tree each time
```

On a Gentoo machine you can use `/var/db/repos/gentoo` directly, which is the
default; `sync-type = git` makes it a checkout.

```bash
# What changed between the pinned revision and HEAD, for tracked packages.
# --queue writes the flags that need a decision to a file.
tools/use-review/use_review.py diff -o diff.md --queue queue.txt

# Evidence for each flag in the queue (add --upstream to fetch sources).
tools/use-review/use_review.py explain @queue.txt -o evidence.md

# Look at one package or flag in depth.
tools/use-review/use_review.py explain --upstream app-office/gnucash:sqlite,keyring

# Record "looked at it, leaving it at the default".
tools/use-review/use_review.py mark app-admin/sudo offensive -m "novelty"

# One-off catch-up: every flag that is currently off and undecided, plus stale entries.
tools/use-review/use_review.py audit -o audit.md --queue audit-queue.txt
```

When you're done, write the new revision into `gentoo.revision` as you do today.
The next `diff` then starts from there.

## What gets tracked

These packages are tracked:
- everything in `sets/*`, `package.use`, `package.accept_keywords`,
  `package.env` and `package.unmask`;
- anything in a world file you pass with `--world /var/lib/portage/world`.

Packages that come only from overlays are listed as not found.

Each flag's effective state is resolved roughly the way Portage does it. From
lowest to highest priority: the IUSE default, the profile (`make.defaults`,
`package.use`, plus `use.mask`/`use.force`), `make.conf` `USE`, then
`package.use`. The profile comes from the `make.profile` symlink, or
`--profile`; it defaults to `default/linux/amd64/23.0/desktop/plasma/systemd`.
A version-restricted entry (`=cat/pkg-1.2.3 -flag`) applies only while it
matches the version you'd upgrade to.

## Reading the diff

| Tag | Meaning |
|---|---|
| `NEW, off — needs a decision` | The flag is new and nothing enables it. This is the "missing features" case. It is written to `--queue`. |
| `NEW, auto-enabled by your global USE` | The flag is new and your `*/*` line turns it on. Check that you want this for the package. |
| `NEW, on by default` | The flag is on through IUSE `+flag` or the profile. |
| `REMOVED, stale config entry` | The flag is gone, but `package.use` still mentions it. |
| `EXPIRED temporary entry` | A `=cat/pkg-ver` workaround no longer matches. Check whether the bug is fixed, then drop or re-pin the entry. |
| `CHANGED` | The IUSE default changed, or the flag now pulls in new dependencies. Identical changes across many packages, such as the `virtual/zlib` migration, are folded into one summary line. |
| `REQUIRED_USE added` | A new constraint that might conflict with your settings. |
| `NEW optfeature` | The ebuild now suggests an optional package. It's marked if it's not in any of your sets, which is a candidate for `@aexoden-optfeatures`. |

## Reading `explain`

Every flag gets a one-line classification:

- **build option**: the flag maps to a configure/meson/cmake/cargo option, which
  is shown. The ebuild excerpt is included. When the mapping is indirect, for
  example `use mysql || use sqlite` setting `sql_on_off` and then
  `-DWITH_SQL=${sql_on_off}`, the variable is followed.
- **runtime-only dependency**: the flag only adds RDEPEND or PDEPEND, so it
  doesn't change how the package is built.
- **dependency only**: the flag adds build-time dependencies but no option. The
  upstream build may detect the library automatically whether or not the flag
  is set.
- **passed through to dependencies**: the flag only shows up as `dep[flag?]`.
- **ebuild logic**: patches, install steps, or something the tracer couldn't
  map. Read the excerpt.
- **handled by eclass**: the matching eclass lines are shown.

With `--upstream`, the distfile is downloaded to `~/.cache/use-review` and
checked against the Manifest BLAKE2B hash. For each option the report shows:

- **Declared in**: the option's declaration and help text (`meson_options.txt`,
  CMake `option()`, `AC_ARG_ENABLE`/`AC_ARG_WITH` and wrappers around them).
- **Used in build files**: each place the option is tested, with real code
  listed before tests.
- **What it gates**: the subdirectories, build targets and `HAVE_*`/`ENABLE_*`
  macros those tests switch on, and how many source files check each macro.
- **Gentoo patches**: patches in `files/` that add or change the option. For
  example, GnuCash's `WITH_LIBSECRET` exists only because of a Gentoo patch.

These are heuristics based on regular expressions, not a shell or CMake
interpreter. They tell you where to look; they don't prove what happens.

## Using an LLM for the judgement calls

`POLICY.md` turns the goal in the top-level README ("enable everything
plausibly useful on a desktop, skip legacy, server and developer extras") into
a per-flag rubric. Feeding the `explain` output into an LLM with that rubric
gives a first-pass recommendation for every flag, citing `file:line`. You then
only have to check the ones it marks `NEEDS HUMAN` or low confidence. For
example, with Claude Code:

```bash
tools/use-review/use_review.py explain --upstream @queue.txt -o evidence.md
claude -p "Apply tools/use-review/POLICY.md to every flag in evidence.md" > recommendations.md
```

The evidence comes first so that the model reasons from the build files rather
than from the flag's name. That matters most for flags where the description is
misleading.

## Tests

```bash
cd tools/use-review && python3 -m unittest -v
```

## Known limitations

- Only the gentoo tree is read; overlays aren't.
- Package-level masks, `package.use.stable.*` and per-slot `package.use` are
  approximated. The "visible" version is the best keyworded one per slot for
  `--arch` (stable and `~arch`, matching `ACCEPT_KEYWORDS="~amd64"`).
- The upstream trace only covers the main distfile. Patches shipped as separate
  tarballs and build systems other than meson/CMake/autotools get a generic
  name search.
- REQUIRED_USE changes are listed but not checked against your settings.
