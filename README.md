# Gentoo Portage Configuration

Jason Lynch <jason@calindora.com>

## Introduction

This is the configuration I use for my desktop Gentoo machines. It attempts to
enable every feature that might be conceivably useful on a modern KDE Plasma and
GNOME desktop, while eschewing any outdated or rarely used items. I use both KDE
and GNOME applications, and occasionally switch between desktops.

## Rationale

One of the highly touted features Gentoo offers to its users is the concept of
USE flags: the ability to customize (to some extent) the features a package is
compiled with. This provides an unparalleled level of customization compared to
most other distros.

Unfortunately, for the average user, this system is incredibly flawed. There is
a large number of flags, with many of them filling a package-specific role.
Without individually auditing each package, a user has no way to know which
flags should or shouldn't be enabled in a given environment. At this time,
Gentoo suffers from an overabundance of USE flags, with often exceedingly poor
documentation on how said flags affect individual packages.

If there were one improvement I could make to Gentoo, it would be to find a way
to allow for customization for unique environments without exposing six billion
flags that 99.9% of users will never need to know are even options. (This is
particularly disheartening because many of these largely useless flags are
actually **disabled** by default, leaving the unknowing user with missing
features they may not even know exist.

To some extent, this is mitigated by the desktop profiles, but they don't seem
to go nearly far enough. Funtoo appears to have a partial solution, though I
haven't used or verified how well it works in practice. In additiona, their
refusal to allow systemd makes Funtoo a non-starter in any case. It also doesn't
help Gentoo at all.

To summarize, Gentoo remains my favorite Linux distribution because it provides
an excellent development environment, has clean tracking of manually vs.
automatically installed packages, and has a rolling release cycle. However, I
have become horribly disenchanted with USE flags and want to ensure I have to
look at them as little as possible, even when maintaining multiple Gentoo
installations.

## Usage

This configuration assumes you have a machine running an AMD64 multilib profile
(which should be the case for the vast majority of users), and enables 32-bit
for every package that supports it. (This is becoming less important as time
goes on, and I'm not sure I even use anything that is still 32-bit only.)

This repository should be cloned directly into `/etc/portage`, replacing any
pre-existing configuration.

You should ensure the `/var/db/repos` directory exists, as this configuration
makes use of overlays installed there.

You should copy and modify the sample `local` files in `/etc/portage/make.conf`
and `/etc/portage/package.use`.

You may also wish to add `@aexoden-base`, `@aexoden-desktop` and
`@aexoden-kernel` to install the packages included in this configuration. This
is completely optional, and will probably install a bunch of programs you don't
want.

Bootstrapping the install can be somewhat difficult, as there are a couple of
circular dependencies that will require temporarily USE flag changes. There is
also an issue of bootstrapping GCC with D and Ada support. If you don't use
those languages, it may be easier to just disable those USE flags on GCC.

You may wish to modify the `package.env` or `env` settings. VTK is currently
restricted to 12 threads due to memory issues on a machine with 256GB of RAM. If
you are building VTK with CUDA and have less memory (which is likely on most
desktop machines), you may wish to force a further reduction. The ebuild does
a check of its own, but it failed to prevent the problem in my case.

## Suggestions

If you have any suggestions for flags to enable or disable, please let me know.
I've generally erred on the side of enabling most things, which almost certainly
means I've enabled something that is probably not needed. My intention is to
provide a fully-functional desktop with all potentially useful features enabled.
