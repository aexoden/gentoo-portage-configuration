Gentoo Portage Configuration
============================

Jason Lynch <jason@calindora.com>

This is the configuration I use for my desktop Gentoo machines. It attempts to
enable every feature that might be conceivably useful on a modern GNOME
desktop, while eschewing any outdated or rarely used items.

However, because Gentoo suffers from an overabundance of useless USE flags,
with often exceedingly poor documentation on how said flags affect individual
packages, this goal may not be entirely met.

This configuration assumes a machine running an AMD64 multilib profile and
enables 32-bit for every package that supports it. This is probably overkill,
so you may wish to create a file named "local" (or any other name lower than
"base" sorted alphabetically) and modify this. Said file should also contain
values for other variables such as CPU_FLAGS_X86, VIDEO_CARDS and
INPUT_DEVICES, which are necessarily machine specific.

In summary, I like Gentoo because it provides an excellent development
environment, has clean tracking of manually vs. automatically installed
packages, and because of the rolling release cycle, but I seriously hate USE
flags and wish they would largely go away.
