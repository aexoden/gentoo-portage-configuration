#!/usr/bin/env python3
"""Unit tests for the parts of use_review that do not need a tree. Run: python3 -m unittest -v"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import use_review as ur  # noqa: E402


def no_expand(flag):
    return flag.startswith(("python_targets_", "python_single_target_"))


class VersionTests(unittest.TestCase):
    def test_vercmp(self):
        lt = [("1.0_rc1", "1.0"), ("1.0", "1.0_p1"), ("1.0", "1.0-r1"), ("1.01", "1.1"), ("257.9", "262_rc2"),
              ("1.2", "1.10"), ("1.0a", "1.0b"), ("1.0_alpha", "1.0_beta"), ("2", "10")]
        for a, b in lt:
            self.assertLess(ur.vercmp(a, b), 0, (a, b))
            self.assertGreater(ur.vercmp(b, a), 0, (b, a))
        self.assertEqual(ur.vercmp("1.0", "1.0-r0"), 0)

    def test_atom_matches(self):
        cases = [("=net-misc/curl-8.16.0-r1", "8.16.0-r1", True), ("=net-misc/curl-8.16.0-r1", "8.23.0-r1", False),
                 ("=a/b-1.2*", "1.2.3", True), ("~a/b-1.2", "1.2-r3", True), (">=a/b-2", "10", True),
                 ("<a/b-1.0_rc1", "1.0", False), ("a/b", "1", True)]
        for atom, pv, expected in cases:
            self.assertEqual(ur.atom_matches(atom, pv), expected, (atom, pv))

    def test_atom_cp(self):
        self.assertEqual(ur.atom_cp(">=dev-libs/glib-2.68.1:2"), "dev-libs/glib")
        self.assertEqual(ur.atom_cp("!!x11-libs/foo[bar]"), "x11-libs/foo")
        self.assertEqual(ur.atom_cp("sys-libs/libchipcard"), "sys-libs/libchipcard")
        self.assertEqual(ur.atom_cp("=sys-libs/ncurses-6.5_p20250802"), "sys-libs/ncurses")
        self.assertIsNone(ur.atom_cp("*/*"))


class DepTests(unittest.TestCase):
    DEP = ("dev-libs/glib aqbanking? ( >=net-libs/aqbanking-6[ofx?] smartcard? ( sys-libs/libchipcard ) ) "
           "python? ( python_single_target_python3_13? ( dev-lang/python:3.13 ) ) !gtk4? ( x11-libs/gtk+:3 )")

    def test_flag_conditional_deps(self):
        d = ur.flag_conditional_deps(self.DEP, no_expand)
        self.assertEqual(d["aqbanking"], {"net-libs/aqbanking", "sys-libs/libchipcard"})
        self.assertEqual(d["smartcard"], {"sys-libs/libchipcard"})
        self.assertEqual(d["python"], {"dev-lang/python"})
        self.assertEqual(d["!gtk4"], {"x11-libs/gtk+"})

    def test_required_use_filters_expand(self):
        req = "^^ ( python_single_target_python3_12 python_single_target_python3_13 ) examples? ( gui )"
        self.assertEqual(ur.required_use_clauses(req, no_expand), {"examples? ( gui )"})

    def test_src_uri(self):
        uri = "https://x.org/a-1.tar.gz test? ( https://x.org/t.tar.gz ) https://x.org/b -> b-1.tar.xz"
        self.assertEqual(ur.parse_src_uri(uri), [("https://x.org/a-1.tar.gz", "a-1.tar.gz"),
                                                  ("https://x.org/b", "b-1.tar.xz")])


EBUILD = """\
IUSE="mysql postgres sqlite keyring quotes +gui winpr-tools gtk4 motif"
RDEPEND="
	quotes? ( dev-perl/Finance-Quote )
"
src_configure() {
	local sql_on_off="OFF"
	if use mysql || use postgres || use sqlite ; then
		sql_on_off="ON"
	fi
	if use motif; then
		myconf+=( --enable-gui=motif )
	elif use gtk4; then
		myconf+=( --enable-gui=gtk4 --without-x )
	else
		myconf+=( --enable-gui=gtk3 )
	fi
	local mycmakeargs=(
		-DWITH_SQL=${sql_on_off}
		-DWITH_LIBSECRET=$(usex keyring) # no-op if USE=foo is set
		-DWITH_WINPR_TOOLS=$(option winpr-tools)
	)
}
"""


class TraceTests(unittest.TestCase):
    def test_indirect_variable(self):
        t = ur.trace_ebuild(EBUILD, "sqlite", {})
        self.assertIn(("cmake/meson define", "WITH_SQL"), t.options)

    def test_direct_and_comment(self):
        t = ur.trace_ebuild(EBUILD, "keyring", {})
        self.assertEqual(t.options, [("cmake/meson define", "WITH_LIBSECRET")])
        self.assertEqual(t.blocks, [])     # the "if" in the trailing comment must not open a block

    def test_runtime_only(self):
        t = ur.trace_ebuild(EBUILD, "quotes", {})
        self.assertTrue(t.kind.startswith("runtime-only"))

    def test_local_helper(self):
        t = ur.trace_ebuild(EBUILD, "winpr-tools", {})
        self.assertIn(("cmake/meson define", "WITH_WINPR_TOOLS"), t.options)

    def test_elif_branch(self):
        t = ur.trace_ebuild(EBUILD, "gtk4", {})
        names = {n for _, n in t.options}
        self.assertIn("gui", names)
        self.assertNotIn("gtk3", " ".join(line for b in t.blocks for _, line in b))

    def test_eclass_fallback(self):
        t = ur.trace_ebuild("IUSE=algol68\n", "algol68", {"toolchain": "is_algol68() {\n\t_tc_use_if_iuse algol68\n}"})
        self.assertEqual(t.kind, "handled by eclass")


if __name__ == "__main__":
    unittest.main()
