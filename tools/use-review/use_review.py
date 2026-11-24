#!/usr/bin/env python3
"""use-review: make USE flag review tractable.

Subcommands:

  fetch-tree  Create a minimal git checkout of the gentoo-mirror tree holding only
              the revisions you need (no Gentoo machine required).
  diff        Report USE-relevant changes between two tree revisions for the
              packages this configuration cares about.
  audit       List flags on tracked packages that no config line or review
              ledger entry has made a decision about, plus stale config entries.
  explain     For specific package flags, trace what the flag actually does:
              ebuild logic -> build-system option -> (optionally) upstream
              build files and the sources they gate.

Only the Python standard library and git are required.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from functools import cmp_to_key
from pathlib import Path

CONFIG_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "use-review"
DEFAULT_TREE_URL = "https://github.com/gentoo-mirror/gentoo.git"

# Flags with no bearing on what an installed desktop can do. They are still
# shown by audit when --all is given, but are not counted as needing review.
DEV_ONLY_FLAGS = {"test", "doc", "examples", "debug", "static-libs", "static", "test-rust",
                  "test-full", "test-install", "valgrind", "big-endian", "verify-sig",
                  "verify-provenance"}


# --------------------------------------------------------------------------
# Tree access
# --------------------------------------------------------------------------

class GitTree:
    """Read-only view of one revision of a git checkout of an ebuild repository."""

    def __init__(self, repo: Path, rev: str):
        self.repo = Path(repo)
        self.rev = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--verify", rev + "^{commit}"],
                                  check=True, capture_output=True, text=True).stdout.strip()
        self._proc = subprocess.Popen(["git", "-C", str(self.repo), "cat-file", "--batch"],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self._ls_cache: dict[str, list[str]] = {}
        self._cat_cache: dict[str, str | None] = {}

    def cat(self, path: str) -> str | None:
        if path in self._cat_cache:
            return self._cat_cache[path]
        self._proc.stdin.write(f"{self.rev}:{path}\n".encode())
        self._proc.stdin.flush()
        header = self._proc.stdout.readline().decode().split()
        if len(header) < 3 or header[1] != "blob":
            data = None
        else:
            size = int(header[2])
            data = self._proc.stdout.read(size).decode("utf-8", "replace")
            self._proc.stdout.read(1)
        if len(self._cat_cache) < 20000:
            self._cat_cache[path] = data
        return data

    def ls(self, path: str) -> list[str]:
        path = path.rstrip("/")
        if path not in self._ls_cache:
            res = subprocess.run(["git", "-C", str(self.repo), "ls-tree", "--name-only", self.rev, path + "/"],
                                 capture_output=True, text=True)
            self._ls_cache[path] = [p.rsplit("/", 1)[-1] for p in res.stdout.splitlines()]
        return self._ls_cache[path]

    @property
    def short(self) -> str:
        return self.rev[:12]


# --------------------------------------------------------------------------
# Versions (PMS algorithm 3.3)
# --------------------------------------------------------------------------

VERSION_RE = re.compile(r"(\d+)((?:\.\d+)*)([a-z]?)((?:_(?:alpha|beta|pre|rc|p)\d*)*)(?:-r(\d+))?")
SUFFIX_ORDER = {"alpha": 0, "beta": 1, "pre": 2, "rc": 3, "p": 5}
PF_RE = re.compile(r"^(?P<pn>.+?)-(?P<pv>" + VERSION_RE.pattern + r")$")


def vercmp(a: str, b: str) -> int:
    ma, mb = VERSION_RE.fullmatch(a), VERSION_RE.fullmatch(b)
    if not ma or not mb:
        return (a > b) - (a < b)

    def cmp(x, y):
        return (x > y) - (x < y)

    if c := cmp(int(ma[1]), int(mb[1])):
        return c
    ca, cb = ma[2].split(".")[1:], mb[2].split(".")[1:]
    for x, y in zip(ca, cb):
        if x.startswith("0") or y.startswith("0"):
            c = cmp(x.rstrip("0"), y.rstrip("0"))
        else:
            c = cmp(int(x), int(y))
        if c:
            return c
    if c := cmp(len(ca), len(cb)):
        return c
    if c := cmp(ma[3], mb[3]):
        return c

    def suffixes(s):
        return [(SUFFIX_ORDER[m[1]], int(m[2] or 0)) for m in re.finditer(r"_(alpha|beta|pre|rc|p)(\d*)", s)]

    sa, sb = suffixes(ma[4]), suffixes(mb[4])
    for x, y in zip(sa, sb):
        if c := cmp(x, y):
            return c
    if len(sa) != len(sb):
        longer, sign = (sa, 1) if len(sa) > len(sb) else (sb, -1)
        nxt = longer[min(len(sa), len(sb))]
        return sign if nxt[0] == SUFFIX_ORDER["p"] else -sign
    return cmp(int(ma[5] or 0), int(mb[5] or 0))


# --------------------------------------------------------------------------
# Dependency / REQUIRED_USE strings
# --------------------------------------------------------------------------

def parse_depstr(s: str) -> list:
    """Parse a PMS dependency-style string into nested nodes.

    Nodes: ("atom", str) | ("cond", flag, negated, children) | ("group", op, children)
    """
    tokens = s.split()
    pos = 0

    def parse_list():
        nonlocal pos
        out = []
        while pos < len(tokens):
            t = tokens[pos]
            if t == ")":
                pos += 1
                return out
            if t.endswith("?") and pos + 1 < len(tokens) and tokens[pos + 1] == "(":
                pos += 2
                flag = t[:-1]
                out.append(("cond", flag.lstrip("!"), flag.startswith("!"), parse_list()))
            elif t in ("||", "^^", "??") and pos + 1 < len(tokens) and tokens[pos + 1] == "(":
                pos += 2
                out.append(("group", t, parse_list()))
            elif t == "(":
                pos += 1
                out.append(("group", "all", parse_list()))
            else:
                pos += 1
                out.append(("atom", t))
        return out

    return parse_list()


ATOM_RE = re.compile(r"^!{0,2}(?P<op>[<>=~]*)(?P<cp>[A-Za-z0-9+_][A-Za-z0-9+_.-]*/[A-Za-z0-9+_][A-Za-z0-9+_-]*?)"
                     r"(?P<ver>-" + VERSION_RE.pattern + r"\*?)?(?=$|[:\[])")


def atom_cp(atom: str) -> str | None:
    m = ATOM_RE.match(atom)
    if not m:
        return None
    if m["ver"] and not m["op"]:
        # Without an operator, the "version" is part of the package name.
        return None if "/" not in atom else atom.split(":")[0].split("[")[0].lstrip("!")
    return m["cp"]


def atom_has_version(atom: str) -> bool:
    m = ATOM_RE.match(atom)
    return bool(m and m["op"] and m["ver"])


def atom_matches(atom: str, pv: str) -> bool:
    """Does a (possibly versioned) atom match version pv? Slots and USE deps are ignored."""
    m = ATOM_RE.match(atom)
    if not m or not m["op"] or not m["ver"]:
        return True
    op, ver = m["op"], m["ver"][1:]
    if ver.endswith("*"):
        base = ver[:-1]
        return op == "=" and pv.startswith(base)   # PMS: plain string prefix match
    if op == "~":
        strip = lambda v: re.sub(r"-r\d+$", "", v)  # noqa: E731
        return vercmp(strip(pv), strip(ver)) == 0
    c = vercmp(pv, ver)
    return {"=": c == 0, ">=": c >= 0, ">": c > 0, "<=": c <= 0, "<": c < 0}.get(op, False)


def flag_conditional_deps(depstr: str, is_expand) -> dict[str, set[str]]:
    """Map 'flag' / '!flag' -> set of package names pulled in under that condition."""
    out: dict[str, set[str]] = defaultdict(set)

    def walk(nodes, conds):
        for n in nodes:
            if n[0] == "atom":
                cp = atom_cp(n[1])
                if not cp:
                    continue
                for c in conds:
                    out[c].add(("!" if n[1].startswith("!") else "") + cp)
            elif n[0] == "cond":
                nc = conds if is_expand(n[1]) else conds + [("!" if n[2] else "") + n[1]]
                walk(n[3], nc)
            else:
                walk(n[2], conds)

    walk(parse_depstr(depstr), [])
    return out


def render_depstr(nodes, is_expand) -> str:
    """Render nodes, dropping conditionals on USE_EXPAND flags (python targets etc.)."""
    parts = []
    for n in nodes:
        if n[0] == "atom":
            if not is_expand(n[1].lstrip("!")):
                parts.append(n[1])
        elif n[0] == "cond":
            if is_expand(n[1]):
                continue
            inner = render_depstr(n[3], is_expand)
            if inner:
                parts.append(f"{'!' if n[2] else ''}{n[1]}? ( {inner} )")
        else:
            inner = render_depstr(n[2], is_expand)
            if inner:
                parts.append(inner if n[1] == "all" else f"{n[1]} ( {inner} )")
    return " ".join(parts)


def required_use_clauses(s: str, is_expand) -> set[str]:
    return {render_depstr([n], is_expand) for n in parse_depstr(s)} - {""}


# --------------------------------------------------------------------------
# Repository metadata
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Ebuild:
    cp: str
    pv: str
    meta: dict[str, str]

    @property
    def slot(self) -> str:
        return self.meta.get("SLOT", "0").split("/")[0]

    @property
    def iuse(self) -> dict[str, str]:
        """flag -> '+', '-' or '' (default state from IUSE)."""
        out = {}
        for tok in self.meta.get("IUSE", "").split():
            if tok[0] in "+-":
                out[tok[1:]] = tok[0]
            else:
                out[tok] = ""
        return out

    @property
    def ebuild_path(self) -> str:
        return f"{self.cp}/{self.cp.split('/')[1]}-{self.pv}.ebuild"


class Repo:
    def __init__(self, tree: GitTree, arch: str = "amd64"):
        self.tree = tree
        self.arch = arch
        self._expand_prefixes = None
        self._global_desc = None

    # USE_EXPAND ---------------------------------------------------------
    @property
    def expand_prefixes(self) -> tuple[str, ...]:
        if self._expand_prefixes is None:
            names = set()
            md = self.tree.cat("profiles/base/make.defaults") or ""
            for var in ("USE_EXPAND", "USE_EXPAND_HIDDEN"):
                m = re.search(rf'^{var}="([^"]*)"', md, re.M)
                if m:
                    names.update(m[1].split())
            names.update(f[:-5].upper() for f in self.tree.ls("profiles/desc") if f.endswith(".desc"))
            names.update({"KERNEL", "ELIBC", "USERLAND", "ARCH"})
            self._expand_prefixes = tuple(sorted(n.lower() + "_" for n in names))
        return self._expand_prefixes

    def is_expand(self, flag: str) -> bool:
        return flag.startswith(self.expand_prefixes)

    # Descriptions -------------------------------------------------------
    @property
    def global_desc(self) -> dict[str, str]:
        if self._global_desc is None:
            self._global_desc = {}
            for line in (self.tree.cat("profiles/use.desc") or "").splitlines():
                if line and not line.startswith("#") and " - " in line:
                    k, v = line.split(" - ", 1)
                    self._global_desc[k.strip()] = v.strip()
        return self._global_desc

    def local_desc(self, cp: str) -> dict[str, str]:
        xml = self.tree.cat(f"{cp}/metadata.xml")
        out = {}
        if not xml:
            return out
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return out
        for use in root.iter("use"):
            if use.get("lang", "en") != "en":
                continue
            for flag in use.iter("flag"):
                out[flag.get("name")] = " ".join("".join(flag.itertext()).split())
        return out

    def describe(self, cp: str, flag: str, local=None) -> tuple[str, str]:
        local = self.local_desc(cp) if local is None else local
        if flag in local:
            return local[flag], "local"
        if flag in self.global_desc:
            return self.global_desc[flag], "global"
        return "(no description)", "none"

    # Ebuilds ------------------------------------------------------------
    def ebuilds(self, cp: str) -> list[Ebuild]:
        cat, pn = cp.split("/", 1)
        out = []
        for name in self.tree.ls(f"metadata/md5-cache/{cat}"):
            m = PF_RE.match(name)
            if not m or m["pn"] != pn:
                continue
            text = self.tree.cat(f"metadata/md5-cache/{cat}/{name}") or ""
            meta = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            out.append(Ebuild(cp, m["pv"], meta))
        return out

    def visible(self, cp: str, accept_any: bool = False) -> list[Ebuild]:
        """Best keyworded version per slot (live / unkeyworded ebuilds excluded unless **)."""
        best: dict[str, Ebuild] = {}
        for eb in self.ebuilds(cp):
            kws = eb.meta.get("KEYWORDS", "").split()
            if not accept_any and self.arch not in kws and "~" + self.arch not in kws:
                continue
            cur = best.get(eb.slot)
            if cur is None or vercmp(eb.pv, cur.pv) > 0:
                best[eb.slot] = eb
        return sorted(best.values(), key=lambda e: e.slot)

    def eclass(self, name: str) -> str | None:
        return self.tree.cat(f"eclass/{name}.eclass")


class Profile:
    """Flattened USE settings from a profile stack (make.defaults, package.use, *.force/*.mask)."""

    def __init__(self, repo: "Repo", path: str):
        self.repo, self.path = repo, path
        self.use: dict[str, bool] = {}
        self.pkg_use: dict[str, dict[str, bool]] = defaultdict(dict)
        self.forced: set[str] = set()
        self.masked: set[str] = set()
        self.pkg_forced: dict[str, set[str]] = defaultdict(set)
        self.pkg_masked: dict[str, set[str]] = defaultdict(set)
        self.nodes = []
        self._walk(f"profiles/{path}".rstrip("/"), set())
        if not self.nodes:
            raise SystemExit(f"profile {path!r} not found in tree")
        for node in self.nodes:
            self._load(node)

    def _walk(self, node, seen):
        if node in seen:
            return
        seen.add(node)
        names = self.repo.tree.ls(node)
        if not names:
            return
        if "parent" in names:
            for line in (self.repo.tree.cat(f"{node}/parent") or "").splitlines():
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                if ":" in line:
                    line = "profiles/" + line.split(":", 1)[1]
                    target = line
                else:
                    target = os.path.normpath(f"{node}/{line}")
                self._walk(target, seen)
        self.nodes.append(node)

    @staticmethod
    def _incremental(target: dict | set, tokens):
        for t in tokens:
            if t == "-*":
                target.clear()
            elif t.startswith("-"):
                target.pop(t[1:], None) if isinstance(target, dict) else target.discard(t[1:])
            elif isinstance(target, dict):
                target[t] = True
            else:
                target.add(t)

    def _lines(self, node, name):
        for line in (self.repo.tree.cat(f"{node}/{name}") or "").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                yield line.split()

    def _load(self, node):
        md = self.repo.tree.cat(f"{node}/make.defaults") or ""
        for m in re.finditer(r'^USE="([^"]*)"', md, re.M):
            self._incremental(self.use, [t for t in m[1].split() if t not in ("${USE}", "$USE")])
        for toks in self._lines(node, "use.force"):
            self._incremental(self.forced, toks)
        for toks in self._lines(node, "use.mask"):
            self._incremental(self.masked, toks)
        for name, target in (("package.use", None), ("package.use.force", self.pkg_forced),
                             ("package.use.mask", self.pkg_masked)):
            for toks in self._lines(node, name):
                cp = atom_cp(toks[0])
                if not cp:
                    continue
                if target is None:
                    for t in toks[1:]:
                        self.pkg_use[cp][t.lstrip("-")] = not t.startswith("-")
                else:
                    self._incremental(target[cp], toks[1:])


def find_profile(config_root: Path, explicit: str | None) -> str | None:
    if explicit:
        return None if explicit == "none" else explicit
    link = config_root / "make.profile"
    if link.is_symlink():
        target = os.readlink(link)
        if "/profiles/" in target:
            return target.split("/profiles/", 1)[1]
    return "default/linux/amd64/23.0/desktop/plasma/systemd"


@dataclasses.dataclass
class PackageView:
    """The flag-relevant facts about a package at one revision (union over visible slots)."""
    cp: str
    versions: list[str]
    flags: dict[str, str]                 # flag -> IUSE default
    desc: dict[str, tuple[str, str]]      # flag -> (text, source)
    deps: dict[str, set[str]]             # 'flag'/'!flag' -> packages
    required_use: set[str]
    optfeatures: set[str]
    pvs: list[str] = dataclasses.field(default_factory=list)

    @classmethod
    def build(cls, repo: Repo, cp: str, accept_any=False) -> "PackageView | None":
        ebs = repo.visible(cp, accept_any)
        if not ebs:
            return None
        flags, deps, req, opt = {}, defaultdict(set), set(), set()
        local = repo.local_desc(cp)
        for eb in ebs:
            for f, d in eb.iuse.items():
                if not repo.is_expand(f):
                    flags[f] = d if flags.get(f, "") == "" else flags[f]
            for var in ("DEPEND", "RDEPEND", "BDEPEND", "PDEPEND", "IDEPEND"):
                for k, v in flag_conditional_deps(eb.meta.get(var, ""), repo.is_expand).items():
                    deps[k] |= v
            req |= required_use_clauses(eb.meta.get("REQUIRED_USE", ""), repo.is_expand)
            opt |= optfeatures_of(repo.tree.cat(eb.ebuild_path) or "")
        desc = {f: repo.describe(cp, f, local) for f in flags}
        return cls(cp, [f"{e.pv}" + (f":{e.slot}" if len(ebs) > 1 else "") for e in ebs],
                   flags, desc, dict(deps), req, opt, [e.pv for e in ebs])


OPTFEATURE_RE = re.compile(r"""\boptfeature[ \t]+(?:"[^"]*"|'[^']*'|\S+)((?:[ \t]+[^\s\\;|&]+)+)""")


def optfeatures_of(ebuild: str) -> set[str]:
    out = set()
    for m in OPTFEATURE_RE.finditer(ebuild.replace("\\\n", " ")):
        for tok in m[1].split():
            cp = atom_cp(tok.strip("\"'"))
            if cp:
                out.add(cp)
    return out


# --------------------------------------------------------------------------
# This configuration (package.use, sets, ledger)
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Setting:
    enabled: bool
    source: str
    atom: str


class Config:
    def __init__(self, root: Path, ledger: Path | None):
        self.root = root
        self.global_use: dict[str, Setting] = {}
        self.pkg_use: dict[str, dict[str, list[Setting]]] = defaultdict(lambda: defaultdict(list))
        self.current_pv: dict[str, list[str]] = {}     # cp -> visible versions at the revision under review
        self.reviewed_global: dict[str, str] = {}
        self.reviewed_pkg: dict[str, dict[str, str]] = defaultdict(dict)
        self.packages: dict[str, set[str]] = defaultdict(set)   # cp -> where it is referenced
        self.accept_any: set[str] = set()
        self.profile: Profile | None = None

        mc = "\n".join(p.read_text() for p in self._conf_files("make.conf"))
        for m in re.finditer(r'^USE="([^"]*)"', mc, re.M):
            for f in m[1].split():
                self.global_use[f.lstrip("-")] = Setting(not f.startswith("-"), "make.conf USE", "*/*")

        for path, lineno, atom, flags in self._atom_lines("package.use"):
            src = f"{path.relative_to(root)}:{lineno}"
            if atom == "*/*":
                for f in flags:
                    self.global_use[f.lstrip("-")] = Setting(not f.startswith("-"), src, atom)
                continue
            cp = atom_cp(atom)
            if not cp:
                continue
            self.packages[cp].add("package.use")
            for f in flags:
                self.pkg_use[cp][f.lstrip("-")].append(Setting(not f.startswith("-"), src, atom))

        for name in ("package.accept_keywords", "package.env", "package.unmask"):
            for _, _, atom, rest in self._atom_lines(name):
                cp = atom_cp(atom)
                if cp:
                    self.packages[cp].add(name)
                    if name == "package.accept_keywords" and "**" in rest:
                        self.accept_any.add(cp)

        sets = root / "sets"
        for p in sorted(sets.glob("*")) if sets.is_dir() else []:
            for line in p.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if line and (cp := atom_cp(line)):
                    self.packages[cp].add(f"@{p.name}")

        if ledger and ledger.exists():
            for lineno, line in enumerate(ledger.read_text().splitlines(), 1):
                body, _, note = line.partition("#")
                parts = body.split()
                if len(parts) < 2:
                    continue
                atom, flags = parts[0], parts[1:]
                note = note.strip() or f"{ledger.name}:{lineno}"
                if atom == "*/*":
                    for f in flags:
                        self.reviewed_global[f] = note
                elif cp := atom_cp(atom):
                    for f in flags:
                        self.reviewed_pkg[cp][f] = note

    def _conf_files(self, name):
        p = self.root / name
        if p.is_file():
            return [p]
        if p.is_dir():
            return sorted(f for f in p.rglob("*") if f.is_file() and not f.name.endswith((".sample", "~"))
                          and not f.name.startswith("."))
        return []

    def _atom_lines(self, name):
        for path in self._conf_files(name):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                tokens = line.split()
                atom, rest = tokens[0], tokens[1:]
                flags = []
                for t in rest:
                    if t.endswith(":"):
                        break          # USE_EXPAND section; not reviewed by this tool
                    flags.append(t)
                yield path, lineno, atom, flags if name == "package.use" else rest

    def add_world(self, path: Path):
        for line in path.read_text().splitlines():
            if cp := atom_cp(line.strip()):
                self.packages[cp].add("world")

    def state(self, cp: str, flag: str) -> Setting | None:
        """Last package.use entry that applies to a currently visible version, else global."""
        versions = self.current_pv.get(cp)
        for s in reversed(self.pkg_use.get(cp, {}).get(flag, [])):
            if versions is None or any(atom_matches(s.atom, pv) for pv in versions):
                return s
        return self.global_use.get(flag)

    def expired(self, cp: str) -> list[tuple[str, Setting]]:
        """Version-restricted entries that match none of the versions you would upgrade to."""
        versions = self.current_pv.get(cp) or []
        out = []
        for flag, settings in self.pkg_use.get(cp, {}).items():
            for s in settings:
                if s.atom != cp and atom_has_version(s.atom) and not any(atom_matches(s.atom, v) for v in versions):
                    out.append((flag, s))
        return out

    def reviewed(self, cp: str, flag: str) -> str | None:
        if flag in self.reviewed_pkg.get(cp, {}):
            return self.reviewed_pkg[cp][flag]
        return self.reviewed_global.get(flag)


def effective(cfg: Config, cp: str, flag: str, default: str) -> tuple[bool, str]:
    """Resolve a flag in PMS order: IUSE default < profile < make.conf < package.use; mask/force win."""
    prof = cfg.profile
    on, why = default == "+", f"IUSE default ({'on' if default == '+' else 'off'})"
    if prof:
        if flag in prof.use:
            on, why = prof.use[flag], "profile make.defaults"
        if flag in prof.pkg_use.get(cp, {}):
            on = prof.pkg_use[cp][flag]
            why = "profile package.use"
    s = cfg.state(cp, flag)
    if s is not None:
        where = "package.use" if s.atom != "*/*" else "global"
        pinned = f" [versioned: {s.atom}]" if s.atom not in ("*/*", cp) else ""
        on, why = s.enabled, f"{'+' if s.enabled else '-'}{flag} via {where} ({s.source}){pinned}"
    if prof:
        if flag in prof.masked or flag in prof.pkg_masked.get(cp, set()):
            return False, why + "; **masked by profile**"
        if flag in prof.forced or flag in prof.pkg_forced.get(cp, set()):
            return True, why + "; forced by profile"
    return on, why


# --------------------------------------------------------------------------
# Heuristic categorisation
# --------------------------------------------------------------------------

CATEGORY_RULES = [
    ("dev/build-only", re.compile(r"^(test|doc|examples|debug|static-libs|static|valgrind|test-.*|.*-doc|api-?doc|"
                                  r"gtk-doc|man|verify-sig|verify-provenance|lto|pgo|cpu_flags_.*|custom-cflags|"
                                  r"jit)$")),
    ("implementation choice", re.compile(r"^(jemalloc|tcmalloc|mimalloc|openssl|gnutls|nss|libressl|mbedtls|rustls|"
                                         r"wolfssl|botan|gcrypt|nettle|qt[56]|gtk[234]?|ncurses|slang|llvm|clang|"
                                         r"system-.*|bundled-.*|.*-bundled|elogind|systemd|openrc|musl|"
                                         r"pulseaudio|pipewire|jack|alsa|oss|sdl[23]?|wayland|X)$")),
    ("legacy/questionable", re.compile(r"^(berkdb|gdbm|python2|gtk2|qt4|qt5|xinerama|esd|arts|oss|hal|consolekit|"
                                       r"libav|introspection-old|fam|gamin|tcpd|telemetry|experimental|"
                                       r"unsupported|unstable)$")),
    ("hardware-specific", re.compile(r"^(cuda|rocm|nvenc|nvidia|amdgpu|intel|vaapi|vdpau|vulkan|opencl|hip|"
                                     r"level-zero|openvino|nvml|nouveau|radeon|bluetooth|ieee1394|smartcard|"
                                     r"thunderbolt|tpm|uefi|efi|dvb|v4l|lirc|gps|nfc|wifi|libcamera)$")),
    ("server-side", re.compile(r"^(server|daemon|apache2|nginx|fastcgi|cgi|ldap|postgres|mysql|mariadb|"
                               r"odbc|kerberos|sssd|pam_.*|selinux|apparmor|audit)$")),
]


def categorise(flag: str, desc: str) -> str:
    if "internal use only" in desc.lower():
        return "profile-internal"
    for name, rx in CATEGORY_RULES:
        if rx.match(flag):
            return name
    d = desc.lower()
    if any(w in d for w in ("documentation", "unit test", "test suite", "debugging symbols", "debug build")):
        return "dev/build-only"
    if any(w in d for w in ("instead of", "use system", "bundled", "allocator", "backend", "rather than")):
        return "implementation choice"
    if any(w in d for w in ("deprecated", "obsolete", "legacy", "experimental", "unsupported")):
        return "legacy/questionable"
    return "feature"


# --------------------------------------------------------------------------
# Ebuild tracing (what does the flag do in the ebuild?)
# --------------------------------------------------------------------------

# Any helper named like use/usex/use_enable/meson_use/_tc_use_if_iuse/multilib_native_use_with...,
# plus the few common helpers whose names do not contain "use".
USE_FUNCS = (r"(?:\w*_)?use(?:[xve]|_\w+)?|meson(?:_native)?_feature|cargo_feature|in_iuse|qmake_use|"
             r"ecm_use\w*")
OPT_PATTERNS = [
    # (regex: group 1 = flag, build system, option-name extractor)
    (re.compile(r"(?<![A-Za-z])use_enable\s+!?([\w+-]+)(?:[ \t]+([\w.+-]+))?"), "autotools", lambda m: m[2] or m[1]),
    (re.compile(r"(?<![A-Za-z])use_with\s+!?([\w+-]+)(?:[ \t]+([\w.+-]+))?"), "autotools", lambda m: m[2] or m[1]),
    (re.compile(r"\bmeson(?:_native)?_(?:use|feature|use_bool|use_feature)\s+!?([\w+-]+)(?:[ \t]+([\w.+-]+))?"),
     "meson", lambda m: m[2] or m[1]),
    (re.compile(r"\bcmake_use_find_package\s+!?([\w+-]+)\s+([\w.+-]+)"), "cmake-find", lambda m: m[2]),
    (re.compile(r"\bqt_feature\s+!?([\w+-]+)(?:[ \t]+([\w.+-]+))?"), "cmake/meson define",
     lambda m: "QT_FEATURE_" + (m[2] or m[1]).replace("-", "_")),
    (re.compile(r"\bcargo_feature\s+!?([\w+-]+)(?:[ \t]+([\w.+-]+))?"), "cargo feature", lambda m: m[2] or m[1]),
    (re.compile(r"\busev\s+!?([\w+-]+)\s+['\"]?([A-Za-z_][\w.+-]*)['\"]?"), "value", lambda m: m[2]),
    (re.compile(r"\bexport\s+(ac_cv_\w+)=\$\(\w*usex\s+!?([\w+-]+)"), "autoconf cache", lambda m: m[1]),
    # local wrapper: $(helper flag option-name)
    (re.compile(r"\$\((?![\w_]*use)[\w-]+\s+!?([\w+-]+)\s+([\w.+-]+)\)"), "ebuild helper", lambda m: m[2]),
]
DEFINE_RE = re.compile(r"-D\s?([A-Za-z_][\w.-]*)=")
AUTOOPT_RE = re.compile(r"--(?:enable|disable|with|without)-([A-Za-z0-9][\w.+-]*)")
ASSIGN_RE = re.compile(r"^\s*(?:local\s+)?([A-Za-z_]\w*)(\+)?=")


def flag_ref_re(flag: str) -> re.Pattern:
    f = re.escape(flag)
    return re.compile(rf"(?:(?<![\w-])(?:{USE_FUNCS})\s+(?:-?-\w+\s+)*!?{f}(?![\w+-])"   # use foo, usex foo ...
                      rf"|(?<![\w+-])!?{f}\?\s*\("                                     # foo? ( ... )
                      rf"|\$\([\w-]+\s+!?{f}(?:\s[^)]*)?\))")                          # $(local_helper foo)


@dataclasses.dataclass
class Trace:
    flag: str
    dep_refs: dict[str, list[str]]             # DEPEND-var -> lines
    code_refs: list[tuple[str, int, str]]      # (function, lineno, line)
    blocks: list[list[tuple[int, str]]]        # surrounding if-blocks / indirect variable uses
    options: list[tuple[str, str]]             # (build system, option name)
    eclass_refs: list[tuple[str, int, str]]
    passthrough: dict[str, set[str]]           # DEPEND-var -> atoms carrying [flag?] / [flag=]

    @property
    def kind(self) -> str:
        if self.options:
            return "build option"
        if self.code_refs:
            return "ebuild logic (patches/install steps/indirect)"
        if self.passthrough and not self.dep_refs:
            return "passed through to dependencies (enables the same feature in them)"
        if self.dep_refs:
            keys = set(self.dep_refs)
            if keys <= {"RDEPEND", "PDEPEND", "IDEPEND"}:
                return "runtime-only dependency (does not change how this package is built)"
            return "dependency only (may be automagic detection; check upstream)"
        if self.eclass_refs:
            return "handled by eclass"
        return "no references found"


def strip_comment(line: str) -> str:
    """Drop a trailing shell comment (good enough for ebuilds; ignores quoting)."""
    return re.sub(r"(^|\s)#.*$", "", line).rstrip()


def trace_ebuild(text: str, flag: str, eclass_texts: dict[str, str]) -> Trace:
    rx = flag_ref_re(flag)
    lines = text.splitlines()
    dep_refs, code_refs, blocks, options = defaultdict(list), [], [], []
    func, var = None, None
    seen_opts = set()

    def add_opts(s):
        for pat, system, name_fn in OPT_PATTERNS:
            for m in pat.finditer(s):
                fl = m[2] if system == "autoconf cache" else m[1]
                if fl.lstrip("!") == flag:
                    key = (system, name_fn(m))
                    if system == "ebuild helper" and any(n == key[1] for _, n in options):
                        continue
                    if key not in seen_opts:
                        seen_opts.add(key)
                        options.append(key)
        for m in DEFINE_RE.finditer(s):
            key = ("cmake/meson define", m[1])
            if key not in seen_opts:
                seen_opts.add(key)
                options.append(key)
        for m in AUTOOPT_RE.finditer(s):
            key = ("autotools", m[1])
            if key not in seen_opts:
                seen_opts.add(key)
                options.append(key)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if m := re.match(r"^([\w-]+)\s*\(\)\s*\{", line):
            func, var = m[1], None
        elif line.startswith("}"):
            func = None
        if func is None:
            if m := re.match(r"^([A-Z_]+)=", line):
                var = m[1]
            elif m := re.match(r"^([A-Z_]+)\+=", line):
                var = m[1]
        if stripped.startswith("#") or not rx.search(line):
            continue
        if func is None and var and var.endswith("DEPEND"):
            dep_refs[var].append(stripped)
            continue
        if func is None and var in ("IUSE", "REQUIRED_USE", "RESTRICT", "PROPERTIES", "KEYWORDS"):
            continue
        code_refs.append((func or var or "global", i + 1, stripped))
        code = strip_comment(stripped)
        add_opts(code)
        # Capture the conditional block the reference opens, if any.
        if re.match(r"(if|elif)\b", code) or code.endswith(("&& {", "; then")):
            block, depth = [(i + 1, line)], 0
            is_elif = code.startswith("elif")
            if is_elif:
                depth = 1
            for j in range(i, min(len(lines), i + 60)):
                s = strip_comment(lines[j].strip())
                if j > i and is_elif and depth == 1 and re.match(r"(elif|else)\b", s):
                    break      # end of this branch
                depth += len(re.findall(r"\bif\b", s)) - len(re.findall(r"\bfi\b", s))
                if j > i:
                    block.append((j + 1, lines[j]))
                if depth <= 0 and j > i:
                    break
            body = "\n".join(b for _, b in block)
            add_opts(body)
            blocks.append(block)
            # Follow variables assigned in the block to where they are consumed
            # (e.g. `use sqlite && sql=ON` ... `-DWITH_SQL=${sql}`).
            assigned = {m[1] for _, b in block[1:] if (m := ASSIGN_RE.match(b))}
            assigned -= {"mycmakeargs", "myconf", "emesonargs", "myeconfargs", "mymesonargs", "myfeatures"}
            follow = []
            for v in assigned:
                vrx = re.compile(rf"\$\{{?{v}\b")
                for k, other in enumerate(lines):
                    if vrx.search(other) and not other.strip().startswith("#"):
                        follow.append((k + 1, other))
                        add_opts(other)
            if follow:
                blocks.append(follow)
        elif "${" not in line or "usex" in line or "usev" in line:
            m = ASSIGN_RE.match(line)
            if m and m[1] not in ("mycmakeargs", "myconf", "emesonargs", "myeconfargs"):
                vrx = re.compile(rf"\$\{{?{m[1]}\b")
                follow = [(k + 1, o) for k, o in enumerate(lines) if k != i and vrx.search(o)]
                for _, o in follow:
                    add_opts(o)
                if follow:
                    blocks.append([(i + 1, line)] + follow)

    eclass_refs = []
    if not code_refs:
        for name, etext in eclass_texts.items():
            efunc = None
            for k, line in enumerate(etext.splitlines()):
                if m := re.match(r"^([\w-]+)\s*\(\)\s*\{", line):
                    efunc = m[1]
                if not line.strip().startswith("#") and rx.search(line):
                    eclass_refs.append((f"{name}.eclass:{efunc or 'global'}", k + 1, line.strip()))
    return Trace(flag, dict(dep_refs), code_refs, blocks, options, eclass_refs[:12], {})


# --------------------------------------------------------------------------
# Upstream tracing (what does the build option do in the package's build system?)
# --------------------------------------------------------------------------

BUILD_FILE_NAMES = {"meson.build", "meson_options.txt", "meson.options", "CMakeLists.txt", "configure.ac",
                    "configure.in", "Makefile.am", "Cargo.toml", "setup.py", "pyproject.toml", "SConstruct",
                    "wscript", "BUILD.gn", "build.gradle"}
BUILD_FILE_EXT = {".cmake", ".m4", ".am", ".gni"}
SOURCE_EXT = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".in", ".vala", ".rs", ".py", ".m", ".mm",
              ".y", ".l", ".inl", ".ipp"}


def parse_src_uri(s: str) -> list[tuple[str, str]]:
    """Unconditional (url, filename) pairs from SRC_URI."""
    out = []
    tokens = s.split()
    i = 0
    stack = []
    while i < len(tokens):
        t = tokens[i]
        if t.endswith("?") and i + 1 < len(tokens) and tokens[i + 1] == "(":
            stack.append(True)
            i += 2
            continue
        if t == "(":
            stack.append(False)
        elif t == ")" and stack:
            stack.pop()
        elif "://" in t and not any(stack):
            name = t.rsplit("/", 1)[-1]
            if i + 2 < len(tokens) and tokens[i + 1] == "->":
                name = tokens[i + 2]
                i += 2
            out.append((t, name))
        i += 1
    return out


def resolve_mirror(repo: Repo, url: str) -> list[str]:
    if not url.startswith("mirror://"):
        return [url]
    name, _, path = url[len("mirror://"):].partition("/")
    for line in (repo.tree.cat("profiles/thirdpartymirrors") or "").splitlines():
        parts = line.split()
        if parts and parts[0] == name:
            return [p.rstrip("/") + "/" + path for p in parts[1:] if p.startswith("http")]
    return []


def fetch_distfile(repo: Repo, eb: Ebuild, cache: Path, log) -> Path | None:
    manifest = repo.tree.cat(f"{eb.cp}/Manifest") or ""
    dist = {}
    for line in manifest.splitlines():
        parts = line.split()
        if parts and parts[0] == "DIST":
            hashes = dict(zip(parts[3::2], parts[4::2]))
            dist[parts[1]] = (int(parts[2]), hashes)
    candidates = parse_src_uri(eb.meta.get("SRC_URI", ""))
    if not candidates:
        log(f"  no unconditional SRC_URI for {eb.cp}-{eb.pv}")
        return None
    pn = eb.cp.split("/")[1].lower()
    candidates.sort(key=lambda c: (pn not in c[1].lower(), not re.search(r"\.(tar|zip|tgz|tbz)", c[1])))
    url, name = candidates[0]
    dest = cache / "distfiles" / name
    size, hashes = dist.get(name, (None, {}))
    if dest.exists() and (size is None or dest.stat().st_size == size):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    urls = resolve_mirror(repo, url)
    h = hashlib.blake2b(name.encode()).hexdigest()[:2]
    urls.append(f"https://distfiles.gentoo.org/distfiles/{h}/{name}")
    for u in urls:
        try:
            log(f"  fetching {u}")
            req = urllib.request.Request(u, headers={"User-Agent": "use-review/0.1"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest.with_suffix(".part"), "wb") as f:
                shutil.copyfileobj(r, f)
        except Exception as e:  # noqa: BLE001 - try the next mirror
            log(f"    failed: {e}")
            continue
        part = dest.with_suffix(".part")
        data_ok = True
        if "BLAKE2B" in hashes:
            hb = hashlib.blake2b()
            with open(part, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    hb.update(chunk)
            data_ok = hb.hexdigest() == hashes["BLAKE2B"]
        if not data_ok:
            log("    checksum mismatch against Manifest; discarding")
            part.unlink()
            continue
        part.rename(dest)
        return dest
    return None


def extract(archive: Path, dest: Path) -> Path:
    if dest.exists():
        return dest
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    name = archive.name
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                if member.startswith("/") or ".." in Path(member).parts:
                    continue
                z.extract(member, tmp)
    elif re.search(r"\.(tar(\.(gz|bz2|xz))?|tgz|tbz2?|txz)$", name):
        with tarfile.open(archive) as t:
            t.extractall(tmp, filter="data")
    else:
        # .tar.zst, .tar.lz etc.: let GNU tar pick the decompressor.
        subprocess.run(["tar", "-xf", str(archive), "-C", str(tmp), "--no-same-owner", "--no-same-permissions"],
                       check=True)
    tmp.rename(dest)
    return dest


@dataclasses.dataclass
class Hit:
    path: str
    lineno: int
    lines: list[str]
    match: str = ""


class SourceTree:
    def __init__(self, root: Path):
        self.root = root
        self.build_files, self.source_files = [], []
        for p in root.rglob("*"):
            if not p.is_file() or p.stat().st_size > 3_000_000:
                continue
            if p.name in BUILD_FILE_NAMES or p.suffix in BUILD_FILE_EXT or p.name.startswith("CMakeLists"):
                self.build_files.append(p)
            elif p.suffix in SOURCE_EXT:
                self.source_files.append(p)
        self._text: dict[Path, list[str]] = {}

    def lines(self, p: Path) -> list[str]:
        if p not in self._text:
            try:
                self._text[p] = p.read_text(errors="replace").splitlines()
            except OSError:
                self._text[p] = []
        return self._text[p]

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root))

    def grep(self, files, rx: re.Pattern, before=1, after=2, limit=40) -> list[Hit]:
        hits = []
        for p in files:
            ls = self.lines(p)
            for i, line in enumerate(ls):
                if rx.search(line):
                    hits.append(Hit(self.rel(p), i + 1, ls[max(0, i - before): i + after + 1], line))
                    if len(hits) >= limit:
                        return hits
        return hits

    def block(self, p: Path, start: int, until_paren=True, max_lines=12) -> list[str]:
        ls = self.lines(p)
        out, depth = [], 0
        for line in ls[start: start + max_lines]:
            out.append(line)
            depth += line.count("(") - line.count(")")
            if until_paren and depth <= 0:
                break
        return out


def trace_upstream(src: SourceTree, options: list[tuple[str, str]], patches: dict[str, str]) -> list[str]:
    md: list[str] = []
    for system, name in options:
        esc = re.escape(name)
        var = re.escape(name.replace("-", "_"))
        if system == "meson" or (system == "cmake/meson define" and any(
                p.name in ("meson_options.txt", "meson.options") for p in src.build_files)):
            decl_rx = re.compile(rf"(?<!\w)option\(\s*['\"]{esc}['\"]")
            use_rx = re.compile(rf"get_option\(\s*['\"]{esc}['\"]")
        elif system == "autotools":
            decl_rx = re.compile(rf"\w*_ARG_(?:ENABLE|DISABLE|WITH|WITHOUT)\w*\(\s*\[?{esc}\]?\s*[,)\]]")
            use_rx = re.compile(rf"\b(?:enable|with)_{var}\b")
        elif system == "cmake-find":
            decl_rx = re.compile(rf"find_package\(\s*{esc}\b", re.I)
            use_rx = re.compile(rf"\b{esc}_FOUND\b|\b{re.escape(name.upper())}_FOUND\b")
        else:
            decl_rx = re.compile(rf"(?<!\w)(?:option|cmake_dependent_option|set)\s*\(\s*{esc}\b|add_feature_info\(\s*{esc}\b")
            use_rx = re.compile(rf"(?<![\w-]){esc}(?![\w-])")
        md.append(f"\n#### `{name}` ({system})\n")
        decls = src.grep(src.build_files, decl_rx, before=0, after=0, limit=5)
        if decls:
            md.append("Declared in:\n")
            for h in decls:
                p = src.root / h.path
                md.append(f"```\n# {h.path}:{h.lineno}\n" + "\n".join(src.block(p, h.lineno - 1)) + "\n```")
        else:
            md.append("_No declaration found in upstream build files._")
        name_rx = re.compile(rf"(?<![\w-]){esc}(?![\w-])")
        in_patches = [n for n, body in patches.items()
                      if any(name_rx.search(l) for l in body.splitlines()
                             if l[:1] in "+-" and not l.startswith(("+++", "---")))]
        if in_patches:
            md.append(f"\n**Gentoo patches touching `{name}`** (the option may exist only because of these): "
                      + ", ".join(f"`{n}`" for n in in_patches))
            for n in in_patches[:2]:
                hunk = [l for l in patches[n].splitlines() if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
                md.append(f"```diff\n# {n}\n" + "\n".join(hunk[:25]) + "\n```")
        after = 6 if system == "autotools" else 3
        uses = [h for h in src.grep(src.build_files, use_rx, before=1, after=after, limit=30)
                if not decl_rx.search(h.match)]
        # Show real code before tests.
        uses.sort(key=lambda h: ("/test" in h.path or "tests/" in h.path, h.path, h.lineno))
        if uses:
            md.append(f"\nUsed in build files ({len(uses)} sites{' (truncated)' if len(uses) >= 30 else ''}):\n")
            for h in uses[:12]:
                md.append(f"```\n# {h.path}:{h.lineno}\n" + "\n".join(h.lines) + "\n```")
        # Second hop: what do those uses switch on?
        gated = second_hop(src, uses)
        if gated:
            md.append("\nWhat it gates (second hop):\n")
            md.extend(gated)
    return md


MACRO_PATTERNS = [
    re.compile(r"\.set(?:10|_quoted)?\(\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]"),      # meson conf.set('HAVE_X'
    re.compile(r"AC_DEFINE(?:_UNQUOTED)?\(\s*\[?([A-Z][A-Z0-9_]{2,})"),          # autotools
    re.compile(r"AM_CONDITIONAL\(\s*\[?([A-Z][A-Z0-9_]{2,})"),
    re.compile(r"(?:add_definitions|add_compile_definitions|target_compile_definitions)\([^)]*?-?D?([A-Z][A-Z0-9_]{2,})"),
    re.compile(r"['\"]-D([A-Z][A-Z0-9_]{2,})"),
    re.compile(r"#cmakedefine01?\s+([A-Z][A-Z0-9_]{2,})"),
    re.compile(r"^\s*set\(\s*([A-Z][A-Z0-9_]{2,})\s+(?:ON|TRUE|1)\b"),
]
SUBDIR_PATTERNS = [
    re.compile(r"\bsubdir\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\badd_subdirectory\(\s*([^\s)]+)"),
    re.compile(r"\bSUBDIRS\s*\+?=\s*(\S+)"),
]
TARGET_RE = re.compile(r"\b(add_library|add_executable)\s*\(\s*([\w.-]+)|"
                       r"\b(?:shared_library|static_library|library|executable|shared_module)\(\s*['\"]([^'\"]+)['\"]")
MACRO_NOISE = {"TRUE", "FALSE", "ON", "OFF", "CMAKE_BUILD_TYPE", "STATUS", "FATAL_ERROR", "WARNING", "REQUIRED",
               "PUBLIC", "PRIVATE", "INTERFACE"}


def governed_lines(src: SourceTree, h: Hit, max_lines=25) -> list[str]:
    """The match line plus the lines it controls: an opened conditional body, or a following
    `if <var>...` that tests a variable assigned on the match line (meson dependency() idiom)."""
    ls = src.lines(src.root / h.path)
    i = h.lineno - 1
    out = [ls[i]]
    indent = len(ls[i]) - len(ls[i].lstrip())
    code = ls[i].strip()
    assigned = m[1] if (m := re.match(r"(\w+)\s*=", code)) else None
    opens = bool(re.match(r"(if|elif|elseif)\b", code, re.I)) or code.endswith(("then", ":"))
    j = i + 1
    if not opens and assigned and j < len(ls) and re.match(rf"\s*if\b.*\b{assigned}\b", ls[j], re.I):
        out.append(ls[j])
        opens, indent, j = True, len(ls[j]) - len(ls[j].lstrip()), j + 1
    if not opens:
        return out
    while j < len(ls) and len(out) < max_lines:
        line = ls[j]
        s = line.strip()
        if re.match(r"(endif|fi|else|elif|elseif)\b", s, re.I):
            break
        if s and len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line)
        j += 1
    return out


def second_hop(src: SourceTree, uses: list[Hit]) -> list[str]:
    macros, subdirs, targets = set(), set(), set()
    for h in uses:
        governed = governed_lines(src, h)
        body = "\n".join(governed)
        for rx in MACRO_PATTERNS:
            for line in governed:
                for m in rx.finditer(line):
                    if m[1] not in MACRO_NOISE:
                        macros.add(m[1])
        for rx in SUBDIR_PATTERNS:
            for m in rx.finditer(body):
                subdirs.add((Path(h.path).parent / m[1].strip("'\"${}")).as_posix())
        for m in TARGET_RE.finditer(body):
            targets.add(f"{m[2] or m[3]} ({Path(h.path).parent.as_posix()})")
    out = []
    for t in sorted(targets)[:10]:
        out.append(f"- defines build target `{t}`")
    for d in sorted(subdirs)[:10]:
        n = sum(1 for p in src.source_files if src.rel(p).startswith(d.lstrip("./") + "/"))
        out.append(f"- builds subdirectory `{d}` ({n} source files)")
    for mac in sorted(macros)[:12]:
        rx = re.compile(rf"^\s*#\s*(?:if|ifdef|ifndef|elif)\b.*\b{re.escape(mac)}\b|\bif\s+{re.escape(mac)}\b")
        files = defaultdict(int)
        sample = None
        for p in src.source_files + [f for f in src.build_files if f.name == "Makefile.am"]:
            for i, line in enumerate(src.lines(p)):
                if rx.search(line):
                    files[src.rel(p)] += 1
                    sample = sample or f"{src.rel(p)}:{i + 1}: {line.strip()}"
        if files:
            top = ", ".join(f"`{f}`" for f in sorted(files, key=files.get, reverse=True)[:6])
            more = f" (+{len(files) - 6} more)" if len(files) > 6 else ""
            out.append(f"- `{mac}` is tested in {sum(files.values())} places across {len(files)} files: {top}{more}")
        else:
            out.append(f"- `{mac}` is set, but no preprocessor/automake test found in sources")
    return out


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def load_config(args, repo: "Repo") -> Config:
    ledger = Path(args.ledger) if args.ledger else Path(__file__).with_name("reviewed.conf")
    cfg = Config(Path(args.config), ledger)
    for w in args.world or []:
        cfg.add_world(Path(w))
    prof = find_profile(Path(args.config), args.profile)
    if prof:
        cfg.profile = Profile(repo, prof)
    return cfg


def tracked(cfg: Config, args) -> list[str]:
    pkgs = sorted(cfg.packages)
    if args.packages:
        pkgs = [p for p in pkgs if any(fnmatch.fnmatch(p, pat) for pat in args.packages)] or args.packages
    return pkgs


def fmt_flag(cfg, cp, flag, default, desc):
    on, why = effective(cfg, cp, flag, default)
    return f"`{flag}` — {desc[0]}  \n  _{'ON' if on else 'off'}: {why}_"


def cmd_diff(args):
    from_rev = args.from_rev or (Path(args.config) / "gentoo.revision").read_text().split()[0]
    old_repo = Repo(GitTree(Path(args.tree), from_rev), args.arch)
    new_repo = Repo(GitTree(Path(args.tree), args.to_rev), args.arch)
    cfg = load_config(args, new_repo)
    pkgs = tracked(cfg, args)
    out = [f"# USE flag changes: {old_repo.tree.short} → {new_repo.tree.short}\n",
           f"{len(pkgs)} tracked packages (sets, package.use, package.accept_keywords, package.env"
           f"{', world' if args.world else ''}). Effective state uses profile "
           f"`{cfg.profile.path if cfg.profile else '(none)'}` at the new revision.\n"]
    summary = defaultdict(int)
    sections = []
    churn: dict[tuple, list[str]] = defaultdict(list)   # (flag, added deps) -> packages
    queue: list[str] = []
    missing = []
    for cp in pkgs:
        accept_any = cp in cfg.accept_any
        old = PackageView.build(old_repo, cp, accept_any)
        new = PackageView.build(new_repo, cp, accept_any)
        if new is None:
            if old is not None:
                missing.append(f"- `{cp}`: present at old revision, gone (or unkeyworded) now")
            elif not cfg.packages[cp] <= {"package.use"} or args.verbose:
                missing.append(f"- `{cp}`: not in this tree (overlay package?)")
            continue
        old = old or PackageView(cp, [], {}, {}, {}, set(), set())
        cfg.current_pv[cp] = new.pvs
        lines = []
        for f, e in cfg.expired(cp):
            lines.append(f"- [EXPIRED temporary entry] `{e.atom}` `{'' if e.enabled else '-'}{f}` at {e.source} "
                         f"does not match {', '.join(new.pvs)}; the flag reverts to its normal setting — "
                         "check whether the reason still holds, then drop or re-pin it")
            summary["expired versioned entries"] += 1
        added = sorted(set(new.flags) - set(old.flags))
        removed = sorted(set(old.flags) - set(new.flags))
        for f in added:
            on, _ = effective(cfg, cp, f, new.flags[f])
            reviewed = cfg.reviewed(cp, f)
            cfg_set = cfg.state(cp, f)
            if cfg_set and cfg_set.atom == "*/*" and cfg_set.enabled:
                tag = "NEW, auto-enabled by your global USE"
                summary["new flags auto-enabled"] += 1
            elif cfg_set:
                tag = "NEW, already set in your config"
            elif on:
                tag = "NEW, on by default"
                summary["new flags on by default"] += 1
            else:
                if not reviewed and categorise(f, new.desc[f][0]) == "profile-internal":
                    reviewed = "internal flag, set by profile"
                tag = "NEW, **off — needs a decision**" if not reviewed else f"NEW, off (reviewed: {reviewed})"
                if not reviewed:
                    queue.append(f"{cp}:{f}")
                summary["new flags off and undecided" if not reviewed else "new flags off (reviewed)"] += 1
            cat = categorise(f, new.desc[f][0])
            deps = sorted(new.deps.get(f, set()))
            dep_s = f"  \n  pulls in: {', '.join(deps[:8])}{' …' if len(deps) > 8 else ''}" if deps else ""
            lines.append(f"- [{tag}] [{cat}] {fmt_flag(cfg, cp, f, new.flags[f], new.desc[f])}{dep_s}")
        for f in removed:
            s = (cfg.pkg_use.get(cp, {}).get(f) or [None])[-1]
            if s:
                lines.append(f"- [REMOVED, **stale config entry** at {s.source}] `{f}`")
                summary["stale package.use entries"] += 1
            else:
                lines.append(f"- [REMOVED] `{f}` — {old.desc[f][0]}")
                summary["flags removed"] += 1
        for f in sorted(set(new.flags) & set(old.flags)):
            notes = []
            if old.flags[f] != new.flags[f]:
                notes.append(f"IUSE default {old.flags[f] or '(none)'} → {new.flags[f] or '(none)'}")
            if old.desc[f][0] != new.desc[f][0] and args.verbose:
                notes.append(f"description changed (was: {old.desc[f][0]})")
            ndeps, odeps = new.deps.get(f, set()), old.deps.get(f, set())
            if ndeps - odeps and (f not in DEV_ONLY_FLAGS or args.verbose):
                key = (f, tuple(sorted(ndeps - odeps)))
                churn[key].append(cp)
                notes.append("now also pulls in " + ", ".join(key[1]) + f"{{CHURN:{f}:{','.join(key[1])}}}")
            if odeps - ndeps and args.verbose:
                notes.append("no longer pulls in " + ", ".join(sorted(odeps - ndeps)))
            if notes:
                on, why = effective(cfg, cp, f, new.flags[f])
                lines.append(f"- [CHANGED] `{f}` ({'ON' if on else 'off'}: {why}): " + "; ".join(notes))
                summary["changed flags"] += 1
        for clause in sorted(new.required_use - old.required_use):
            if old.versions:
                lines.append(f"- [REQUIRED_USE added] `{clause}`")
                summary["new REQUIRED_USE constraints"] += 1
        new_opt = new.optfeatures - old.optfeatures if old.versions else set()
        for cp_opt in sorted(new_opt):
            where = cfg.packages.get(cp_opt)
            have = f"already in {', '.join(sorted(where))}" if where else "**not in any of your sets**"
            lines.append(f"- [NEW optfeature] `{cp_opt}` ({have})")
            summary["new optfeatures"] += 1
        if lines:
            hdr = f"\n## {cp}  \n{', '.join(old.versions) or '(new)'} → {', '.join(new.versions)}\n"
            sections.append(hdr + "\n".join(lines))
    # Identical dependency changes across many packages are tree-wide churn
    # (e.g. a virtual being introduced); report them once.
    tree_wide = {k for k, v in churn.items() if len(v) >= args.churn}
    cleaned = []
    for sec in sections:
        kept = []
        for line in sec.split("\n"):
            for k in churn:
                tag = f"{{CHURN:{k[0]}:{','.join(k[1])}}}"
                if tag in line:
                    if k in tree_wide:
                        line = line.replace("now also pulls in " + ", ".join(k[1]) + tag, "").rstrip(": ;")
                    else:
                        line = line.replace(tag, "")
            if line.startswith("- [CHANGED]") and line.endswith(")"):
                continue      # nothing left but the header
            kept.append(line)
        if any(l.startswith("- ") for l in kept):
            cleaned.append("\n".join(kept))
    summary["changed flags"] = sum(s.count("- [CHANGED]") for s in cleaned)
    out.append("## Summary\n")
    out.extend(f"- {k}: {v}" for k, v in sorted(summary.items()) if v)
    if tree_wide:
        out.append("\nTree-wide dependency churn (folded out of the per-package list):\n")
        for k in sorted(tree_wide, key=lambda k: -len(churn[k])):
            out.append(f"- `{k[0]}` now pulls in {', '.join(k[1])} in {len(churn[k])} packages")
    out.extend(cleaned)
    if missing:
        out.append("\n## Not resolved in this tree\n")
        out.extend(missing)
    emit(args, "\n".join(out) + "\n")
    if args.queue:
        Path(args.queue).write_text("\n".join(queue) + "\n")
        print(f"wrote {len(queue)} undecided flags to {args.queue} (use: explain @{args.queue})", file=sys.stderr)


def cmd_audit(args):
    repo = Repo(GitTree(Path(args.tree), args.rev), args.arch)
    cfg = load_config(args, repo)
    pkgs = tracked(cfg, args)
    out = [f"# USE flag audit at {repo.tree.short}\n",
           "Flags listed here are neither set in package.use/make.conf nor recorded in the review ledger. "
           f"Flags that are already effectively on, and development-only flags "
           f"({', '.join(sorted(DEV_ONLY_FLAGS))}), are hidden unless --all.\n"]
    stale, counts, queue = [], defaultdict(int), []
    by_cat: dict[str, list[str]] = defaultdict(list)
    for cp in pkgs:
        view = PackageView.build(repo, cp, cp in cfg.accept_any)
        if view is None:
            continue
        cfg.current_pv[cp] = view.pvs
        for f, settings in cfg.pkg_use.get(cp, {}).items():
            for s in settings:
                if f not in view.flags:
                    stale.append(f"- `{cp}` `{'' if s.enabled else '-'}{f}` at {s.source} [{s.atom}]: flag gone")
        for f, s in cfg.expired(cp):
            stale.append(f"- `{s.atom}` `{'' if s.enabled else '-'}{f}` at {s.source}: version-restricted entry "
                         f"does not match {', '.join(view.pvs)}")
        for f, d in sorted(view.flags.items()):
            if cfg.state(cp, f) or cfg.reviewed(cp, f):
                continue
            if f in DEV_ONLY_FLAGS and not args.all:
                continue
            if effective(cfg, cp, f, d)[0] and not args.all:
                continue          # already on via IUSE default or profile
            cat = categorise(f, view.desc[f][0])
            if cat == "profile-internal" and not args.all:
                continue
            queue.append(f"{cp}:{f}")
            counts[cat] += 1
            by_cat[cat].append(f"- `{cp}` {fmt_flag(cfg, cp, f, d, view.desc[f])}")
    out.append("## Undecided flags by category\n")
    out.extend(f"- {k}: {v}" for k, v in sorted(counts.items()))
    for cat in sorted(by_cat):
        out.append(f"\n## {cat}\n")
        out.extend(by_cat[cat])
    if stale:
        out.append("\n## Stale package.use entries\n")
        out.extend(stale)
    emit(args, "\n".join(out) + "\n")
    if args.queue:
        Path(args.queue).write_text("\n".join(queue) + "\n")
        print(f"wrote {len(queue)} undecided flags to {args.queue}", file=sys.stderr)


def cmd_explain(args):
    repo = Repo(GitTree(Path(args.tree), args.rev), args.arch)
    cfg = load_config(args, repo)
    cache = Path(args.cache)
    log = (lambda m: print(m, file=sys.stderr)) if not args.quiet else (lambda m: None)
    md = []
    grouped: dict[str, list[str]] = {}
    for spec in args.targets:
        cp, _, flagspec = spec.strip().partition(":")
        if not cp or cp.startswith("#"):
            continue
        grouped.setdefault(cp, [])
        grouped[cp] += [f for f in flagspec.split(",") if f]
    for cp, flaglist in grouped.items():
        flagspec = ",".join(dict.fromkeys(flaglist))
        ebs = repo.visible(cp, accept_any=cp in cfg.accept_any)
        if not ebs:
            md.append(f"# {cp}\n\nNo visible ebuild at {repo.tree.short}.\n")
            continue
        eb = max(ebs, key=cmp_to_key(lambda a, b: vercmp(a.pv, b.pv)))
        text = repo.tree.cat(eb.ebuild_path) or ""
        cfg.current_pv[cp] = [eb.pv]
        iuse = {f: d for f, d in eb.iuse.items() if not repo.is_expand(f)}
        flags = flagspec.split(",") if flagspec else sorted(iuse)
        local = repo.local_desc(cp)
        eclasses = {n: repo.eclass(n) or "" for n in eb.meta.get("_eclasses_", "").split()[::2]}
        patches = {f"files/{n}": repo.tree.cat(f"{cp}/files/{n}") or ""
                   for n in repo.tree.ls(f"{cp}/files") if re.search(r"\.(patch|diff)$", n)}
        alldeps = {var: flag_conditional_deps(eb.meta.get(var, ""), repo.is_expand)
                   for var in ("DEPEND", "RDEPEND", "BDEPEND", "PDEPEND", "IDEPEND")}
        md.append(f"# {cp}-{eb.pv}\n\n{eb.meta.get('DESCRIPTION', '')}  \n"
                  f"Homepage: {eb.meta.get('HOMEPAGE', '')}  \nInherits: {eb.meta.get('INHERIT', '')}\n")
        traces = []
        for f in flags:
            if f not in iuse:
                md.append(f"\n## `{f}`\n\nNot in IUSE of {cp}-{eb.pv}.\n")
                continue
            t = trace_ebuild(text, f, eclasses)
            urx = re.compile(rf"\[[^\]]*(?<![\w+-])!?{re.escape(f)}[?=][^\]]*\]")
            for var in ("DEPEND", "RDEPEND", "BDEPEND", "PDEPEND"):
                hits = {re.sub(r"\[([^\]]*)\]", lambda m: "[" + ",".join(
                            u for u in m[1].split(",") if not repo.is_expand(u.rstrip("()-+?=!"))) + "]", a)
                        for a in eb.meta.get(var, "").split() if urx.search(a)}
                if hits:
                    t.passthrough[var] = hits
            traces.append(t)
            desc, src = repo.describe(cp, f, local)
            on, why = effective(cfg, cp, f, iuse[f])
            md.append(f"\n## `{f}` — {t.kind}\n")
            md.append(f"- Gentoo description ({src}): {desc}")
            md.append(f"- Your setting: {'ON' if on else 'off'} — {why}")
            if r := cfg.reviewed(cp, f):
                md.append(f"- Ledger: {r}")
            md.append(f"- Heuristic category: {categorise(f, desc)}")
            if t.options:
                md.append("- Maps to build option(s): " + ", ".join(f"`{n}` ({s})" for s, n in t.options))
            for var, dmap in alldeps.items():
                for cond in (f, "!" + f):
                    if dmap.get(cond):
                        pk = sorted(dmap[cond])
                        md.append(f"- {var} under `{cond}?`: " + ", ".join(pk[:10]) + (" …" if len(pk) > 10 else ""))
            for var, atoms in t.passthrough.items():
                md.append(f"- {var} passes the flag to: " + ", ".join(f"`{a}`" for a in sorted(atoms)[:8]))
            if t.code_refs:
                md.append("\nEbuild references:\n```")
                md.extend(f"{ln:>4} [{fn}] {line}" for fn, ln, line in t.code_refs[:20])
                md.append("```")
            for b in t.blocks[:4]:
                md.append("```\n" + "\n".join(f"{ln:>4} {line}" for ln, line in b[:30]) + "\n```")
            if t.eclass_refs:
                md.append("\nEclass references:\n```")
                md.extend(f"{where}:{ln} {line}" for where, ln, line in t.eclass_refs)
                md.append("```")
        if args.upstream:
            opts = [o for t in traces for o in t.options]
            if not opts:
                md.append("\n_No build options to trace upstream._\n")
                continue
            archive = fetch_distfile(repo, eb, cache, log)
            if not archive:
                md.append("\n_Could not fetch the distfile for upstream tracing._\n")
                continue
            root = extract(archive, cache / "src" / f"{cp.replace('/', '_')}-{eb.pv}")
            src = SourceTree(root)
            md.append(f"\n## Upstream build system ({archive.name})\n")
            for t in traces:
                if t.options:
                    md.append(f"\n### flag `{t.flag}`")
                    md.extend(trace_upstream(src, t.options, patches))
    emit(args, "\n".join(md) + "\n")


def cmd_fetch_tree(args):
    repo = Path(args.tree)
    revs = list(args.revs)
    if not revs:
        revs = [(Path(args.config) / "gentoo.revision").read_text().split()[0], "HEAD"]
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", args.url], check=True)
    for rev in revs:
        print(f"fetching {rev} ...", file=sys.stderr)
        subprocess.run(["git", "-C", str(repo), "fetch", "-q", "--depth", "1", "origin", rev], check=True)
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "FETCH_HEAD"], check=True,
                             capture_output=True, text=True).stdout.strip()
        if rev == "HEAD":
            subprocess.run(["git", "-C", str(repo), "update-ref", "refs/heads/upstream", sha], check=True)
            subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/upstream"], check=True)
        print(f"  {rev} = {sha}", file=sys.stderr)


def cmd_mark(args):
    ledger = Path(args.ledger) if args.ledger else Path(__file__).with_name("reviewed.conf")
    atom = args.atom
    if atom != "*/*" and not atom_cp(atom):
        raise SystemExit(f"not a package atom: {atom}")
    line = f"{atom}\t{' '.join(args.flags)}"
    if args.message:
        line += f"\t# {args.message}"
    with open(ledger, "a") as f:
        f.write(line + "\n")
    print(f"{ledger}: {line}", file=sys.stderr)


def emit(args, text):
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=str(CONFIG_ROOT), help="portage config root (default: this repo)")
    common.add_argument("--tree", default=os.environ.get("USE_REVIEW_TREE", "/var/db/repos/gentoo"),
                        help="git checkout of the gentoo-mirror tree (env USE_REVIEW_TREE)")
    common.add_argument("--arch", default="amd64")
    common.add_argument("--profile", help="profile path under profiles/ (default: make.profile symlink, else "
                        "default/linux/amd64/23.0/desktop/plasma/systemd; 'none' to disable)")
    common.add_argument("--ledger", help="review ledger (default: reviewed.conf next to this script)")
    common.add_argument("--world", action="append", help="also track packages from a world file")
    common.add_argument("-o", "--output", help="write the report here instead of stdout")
    common.add_argument("--cache", default=str(DEFAULT_CACHE), help="distfile/source cache for explain --upstream")
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("-q", "--quiet", action="store_true")
    ap = argparse.ArgumentParser(prog="use-review", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=lambda **kw: argparse.ArgumentParser(
        fromfile_prefix_chars="@", **kw))

    p = sub.add_parser("fetch-tree", parents=[common], help="shallow-fetch tree revisions into --tree")
    p.add_argument("revs", nargs="*", help="revisions (default: gentoo.revision and HEAD)")
    p.add_argument("--url", default=DEFAULT_TREE_URL)
    p.set_defaults(func=cmd_fetch_tree)

    p = sub.add_parser("diff", parents=[common], help="flag changes between two tree revisions")
    p.add_argument("--from", dest="from_rev", help="old revision (default: gentoo.revision)")
    p.add_argument("--to", dest="to_rev", default="HEAD")
    p.add_argument("--queue", help="also write undecided new flags (cat/pkg:flag per line) to this file")
    p.add_argument("--churn", type=int, default=5,
                   help="fold identical dependency changes seen in at least N packages (default 5)")
    p.add_argument("packages", nargs="*", help="limit to these packages (globs allowed)")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("mark", parents=[common], help="record flags as reviewed (left at default) in the ledger")
    p.add_argument("atom", help="cat/pkg, or */* for every package")
    p.add_argument("flags", nargs="+")
    p.add_argument("-m", "--message", help="reason, stored as a comment")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("audit", parents=[common], help="undecided flags and stale entries at one revision")
    p.add_argument("--rev", default="HEAD")
    p.add_argument("--all", action="store_true", help="include flags already on, and development-only flags")
    p.add_argument("--queue", help="also write the listed flags (cat/pkg:flag per line) to this file")
    p.add_argument("packages", nargs="*")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("explain", parents=[common], help="trace what flags actually do")
    p.add_argument("--rev", default="HEAD")
    p.add_argument("--upstream", action="store_true", help="download the distfile and trace build options")
    p.add_argument("targets", nargs="+", help="cat/pkg or cat/pkg:flag1,flag2; @FILE reads one per line")
    p.set_defaults(func=cmd_explain)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
