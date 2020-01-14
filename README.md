# Gentoo Portage Configuration

Jason Lynch <jason@calindora.com>

## Introduction

This is the configuration I use for my desktop Gentoo machines. It attempts to
enable every feature that might be conceivably useful on a modern GNOME desktop,
while eschewing any outdated or rarely used items.

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
to go nearly far enough. Funtoo appears to be in the midst of developing at
least some sort of partial solution, but that doesn't help the Gentoo world
much.

To summarize, Gentoo remains my favorite Linux distribution because it provides
an excellent development environment, has clean tracking of manually vs.
automatically installed packages, and has a rolling release cycle. However, I
have become horribly disenchanted with USE flags and want to ensure I have to
look at them as little as possible, even when maintaining multiple Gentoo
installations.

## Usage

This configuration assumes you have a machine running an AMD64 multilib profile
(which should be the case for the vast majority of users), and enables 32-bit
for every package that supports it.

This repository should be cloned directly into `/etc/portage`, replacing any
pre-existing configuration.

You should ensure the `/var/db/repos` directory exists, as this configuration
makes use of overlays installed there.

You should create a `local` file in the `/etc/portage/make.conf` directory
containing any make.conf configuration you want to do. Most machines should at
least set an appropriate `MAKEOPTS` value here.

You should simlarly create a `local` file in the `/etc/portage/package.use`
directory to provide machine-specific USE flags, such as CPU flags, video cards
or input drivers. If you have a CUDA-capable video card, you may wish to
enable the cuda flag there as well.

You may also wish to add `@base` to your `/var/lib/portage/world_sets` file in
order to use the provided world packages. This is completely optional, and will
probably install a bunch of programs you don't want. However, it does make it
easy for me to install the same packages on all my machines.

## Suggestions

If you have any suggestions for flags to enable or disable, please let me know.
Again, my intention is to provide a fully-functional GNOME desktop with all
potentially useful features enabled. I believe I have probably gone slightly too
far to the other side, but it's hard to know without extensive research about
each package. Some of the flags I have enabled are probably better suited to
server systems, but it serves my purposes well enough for the time being.
