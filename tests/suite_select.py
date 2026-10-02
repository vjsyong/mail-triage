"""Impact-aware section selection for tests/mock_e2e.py.

The suite is one long sequence of sections, each tagged with the domains it
covers, e.g. ``section("T12 RAG: indexer, chunking, folder exclusions", "rag")``.
A full run executes every section. A partial run keeps only the sections whose
groups were selected (plus any earlier section whose variables they read) and
blanks the rest of the section bodies before re-executing the suite.

Selection:
    python tests/mock_e2e.py                 # auto (dirty tree) / full (clean tree)
    python tests/mock_e2e.py --all           # force the full suite
    python tests/mock_e2e.py --only core,rag # only these domains (+ prerequisites)
    python tests/mock_e2e.py --skip ui       # everything except these domains
    python tests/mock_e2e.py --list          # show sections and their groups

Auto mode looks at the files changed since the branch point with master
(committed and uncommitted, untracked included). ``app.py`` hunks that only
touch a ``*_TMPL`` string count as ui; route/CLI hunks fall back to a full run.
Unknown paths fall back to a full run.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess

GROUPS = ("base", "core", "ui", "assistant", "rag", "learning", "plugins",
          "proxy", "bench")
TAIL_MARKER = "==== suite tail"

# State prerequisites that variable-flow analysis cannot see: sections build
# on mailbox/store/plugin fixtures created by earlier sections. Selecting a
# group also runs its prerequisites (transitively). `base` always runs.
GROUP_PREREQS = {
    "core": (),
    "ui": (),
    "assistant": ("rag",),
    "rag": (),
    "learning": ("core",),
    "plugins": ("core", "rag"),
    "bench": ("plugins",),
    "proxy": (),
}

# Paths that never need the mock suite (docs and deploy plumbing).
NO_TEST_PATHS = {
    "Dockerfile", "docker-compose.yml", ".dockerignore", ".env.example",
    ".gitignore", "LICENSE",
}


class Section:
    __slots__ = ("name", "groups", "line", "end_line", "stmts")

    def __init__(self, name, groups, line, end_line):
        self.name = name
        self.groups = tuple(groups)
        self.line = line
        self.end_line = end_line
        self.stmts = []


def _is_section_call(node):
    return (isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "section"
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
            and isinstance(node.value.args[0].value, str))


def parse_suite(source):
    """Split mock_e2e.py into (lines, sections, setup_stmts, tail_stmts, tail_line)."""
    lines = source.splitlines(keepends=True)
    tail_line = None
    for i, line in enumerate(lines, start=1):
        if TAIL_MARKER in line:
            tail_line = i
            break

    tree = ast.parse(source)
    main_fn = next((n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main_fn is None:
        return lines, [], [], [], tail_line

    setup, sections, tail = [], [], []
    current = None
    for node in main_fn.body:
        if _is_section_call(node):
            args = node.value.args
            name = ast.literal_eval(args[0])
            groups = [ast.literal_eval(a) for a in args[1:]
                      if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            current = Section(name, groups, node.lineno,
                              node.end_lineno or node.lineno)
            sections.append(current)
        elif tail_line is not None and node.lineno >= tail_line:
            current = None
            tail.append(node)
        elif current is not None:
            current.stmts.append(node)
        else:
            setup.append(node)
    return lines, sections, setup, tail, tail_line


def _assigned_lines(node):
    """Map section-visible bound names to the line of their first binding."""
    out = {}

    def record(target):
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
                out.setdefault(sub.id, sub.lineno)

    def walk(n):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                record(t)
            walk(n.value)
        elif isinstance(n, ast.AnnAssign):
            if n.value:
                record(n.target)
                walk(n.value)
        elif isinstance(n, ast.AugAssign):
            record(n.target)
            walk(n.value)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            record(n.target)
            walk(n.iter)
            for s in list(n.body) + list(n.orelse):
                walk(s)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars:
                    record(item.optional_vars)
                walk(item.context_expr)
            for s in n.body:
                walk(s)
        elif isinstance(n, ast.Import):
            for a in n.names:
                out.setdefault(a.asname or a.name.split(".")[0], n.lineno)
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                out.setdefault(a.asname or a.name, n.lineno)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.setdefault(n.name, n.lineno)
        elif isinstance(n, ast.ExceptHandler):
            if n.name:
                out.setdefault(n.name, n.lineno)
            for s in n.body:
                walk(s)
        elif isinstance(n, ast.NamedExpr):
            record(n.target)
            walk(n.value)
        elif isinstance(n, ast.Try):
            for s in list(n.body) + list(n.orelse) + list(n.finalbody):
                walk(s)
            for h in n.handlers:
                walk(h)
        elif isinstance(n, ast.If):
            for s in list(n.body) + list(n.orelse):
                walk(s)
        else:
            for child in ast.iter_child_nodes(n):
                walk(child)

    walk(node)
    return out


def _block_bound_names(node):
    """Names bound only for a statement-local scope (loops, with, except,
    comprehensions, lambdas) - not leaking bindings like for-loop targets."""
    names = set()

    def walk(n):
        if isinstance(n, (ast.For, ast.AsyncFor)):
            for sub in ast.walk(n.target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
            walk(n.iter)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars:
                    for sub in ast.walk(item.optional_vars):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
                walk(item.context_expr)
        elif isinstance(n, ast.ExceptHandler):
            if n.name:
                names.add(n.name)
        elif isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp,
                            ast.GeneratorExp)):
            for gen in n.generators:
                for sub in ast.walk(gen.target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(n, ast.Lambda):
            args = n.args
            for a in (list(args.posonlyargs) + list(args.args)
                      + list(args.kwonlyargs)):
                names.add(a.arg)
            if args.vararg:
                names.add(args.vararg.arg)
            if args.kwarg:
                names.add(args.kwarg.arg)
        for child in ast.iter_child_nodes(n):
            walk(child)

    walk(node)
    return names


def _used_located(node):
    return [(n.id, n.lineno) for n in ast.walk(node)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)]


def section_dependencies(sections, setup):
    """Map section name -> earlier sections whose variables it reads.

    A use counts as external when the name is read before this section binds
    it. Bindings local to a statement (loop/with/comprehension/lambda) don't
    count as uses of an earlier section's variable.
    """
    _MISSING = object()
    defined = {}
    for st in setup:
        for name in _assigned_lines(st):
            defined[name] = None
    deps = {}
    for sec in sections:
        local = set()
        needed = set()
        for st in sec.stmts:
            assigned = _assigned_lines(st)
            block = _block_bound_names(st)
            for name, use_line in _used_located(st):
                if name in local or name in block:
                    continue
                assign_line = assigned.get(name)
                if assign_line is not None and assign_line < use_line:
                    continue
                origin = defined.get(name, _MISSING)
                if origin is not _MISSING and origin is not None:
                    needed.add(origin)
            for name in assigned:
                local.add(name)
                defined[name] = sec.name
        deps[sec.name] = needed
    return deps


def _closure(selected, deps):
    out = set()
    stack = list(selected)
    while stack:
        name = stack.pop()
        if name in out:
            continue
        out.add(name)
        stack.extend(deps.get(name, ()))
    return out


def _expand_groups(groups):
    out = set()
    stack = list(groups)
    while stack:
        group = stack.pop()
        if group in out:
            continue
        out.add(group)
        stack.extend(GROUP_PREREQS.get(group, ()))
    return out


def filter_source(lines, sections, selected, tail_line):
    """Blank the bodies of unselected sections, keeping line numbers stable."""
    keep = set(selected)
    for i, sec in enumerate(sections):
        if sec.name in keep:
            continue
        start = sec.end_line + 1
        if i + 1 < len(sections):
            end = sections[i + 1].line - 1
        else:
            end = (tail_line - 1) if tail_line else len(lines)
        for ln in range(start, end + 1):
            lines[ln - 1] = "\n"
    return "".join(lines)


# --------------------------------------------------------------- git impact

def _git(args, cwd):
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, text=True,
                           capture_output=True, timeout=20)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    return p.stdout


def _merge_base(cwd):
    for ref in ("master", "origin/master"):
        out = _git(["merge-base", "HEAD", ref], cwd)
        if out and out.strip():
            return out.strip()
    return "HEAD"


def changed_paths(project):
    base = _merge_base(project)
    out = _git(["diff", "--name-only", base], project)
    if out is None:
        return None
    paths = {p for p in out.splitlines() if p}
    untracked = _git(["ls-files", "--others", "--exclude-standard"], project)
    if untracked:
        paths |= {p for p in untracked.splitlines() if p}
    return sorted(paths)


def _changed_lines(project, base, path):
    out = _git(["diff", "-U0", base, "--", path], project)
    if out is None:
        return None
    lines = set()
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", out, re.M):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        if count == 0:
            return None  # deletion-only hunk: cannot classify, be conservative
        lines.update(range(start, start + count))
    return lines or None


def _app_template_ranges(app_path):
    try:
        tree = ast.parse(open(app_path, encoding="utf-8").read())
    except Exception:
        return None
    ranges = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name)
                    and (target.id == "BASE_TMPL" or target.id.endswith("_TMPL"))):
                continue
            if isinstance(node.value, (ast.Constant, ast.JoinedStr)):
                ranges.append((node.value.lineno,
                               node.value.end_lineno or node.value.lineno))
    return ranges or None


def _app_groups(project, base):
    """Template-string-only app.py hunks are ui; route/CLI hunks touch every
    domain the web layer serves, so they force a full run."""
    lines = _changed_lines(project, base, "app.py")
    ranges = _app_template_ranges(os.path.join(project, "app.py"))
    if lines and ranges and all(any(a <= ln <= b for a, b in ranges)
                                for ln in lines):
        return {"ui"}
    return set(GROUPS)


def groups_for_paths(paths, project=None, base=None):
    """Map changed files to suite groups. Empty set = no test-relevant change."""
    groups = set()
    for path in paths:
        p = path.replace("\\", "/")
        if p == "app.py" and project:
            groups |= _app_groups(project, base)
        elif p == "app.py":
            groups = set(GROUPS)
        elif p == "engine.py":
            groups |= {"core", "assistant"}
        elif p == "store.py":
            groups = set(GROUPS)
        elif p == "heuristics.py":
            groups |= {"core", "learning"}
        elif p == "learning.py":
            groups |= {"learning"}
        elif p in ("rag.py", "rag_lite.py") or p.startswith("embed/"):
            groups |= {"rag"}
        elif p in ("plugins.py", "plugin_rt.py", "plugin_worker.py") or p.startswith(
                ("plugins/", "sdk/", "schemas/")):
            groups |= {"plugins"}
        elif p == "proxy.py":
            groups |= {"proxy"}
        elif p.startswith(("static/", "icons/", "fonts/")):
            groups |= {"ui"}
        elif p.startswith("benchmarks/"):
            groups |= {"bench"}
        elif p in ("config.py",) or p.startswith("tests/"):
            groups = set(GROUPS)
        elif p.endswith(".md") or p.startswith("docs/") or p in NO_TEST_PATHS:
            continue
        else:
            groups = set(GROUPS)  # unknown -> be safe, run everything
    return groups


# ------------------------------------------------------------------- CLI

def _parse_args(argv):
    p = argparse.ArgumentParser(
        prog="tests/mock_e2e.py",
        description="Mock E2E suite. Full by default on a clean tree; a dirty "
                    "tree runs only the domains its changed files touch.")
    p.add_argument("--all", action="store_true", help="force the full suite")
    p.add_argument("--auto", action="store_true",
                   help="pick groups from changed files (the default when dirty)")
    p.add_argument("--only", metavar="GROUPS",
                   help="run only these comma-separated groups")
    p.add_argument("--skip", metavar="GROUPS",
                   help="run everything except these comma-separated groups")
    p.add_argument("--list", action="store_true",
                   help="list sections and their groups, then exit")
    ns = p.parse_args(argv)
    if ns.only and ns.skip:
        p.error("--only and --skip are mutually exclusive")
    if ns.skip:
        _group_list(ns.skip, p, allow_base=False)
    return p, ns


def _group_list(value, parser, allow_base=True):
    wanted = [g.strip() for g in value.split(",") if g.strip()]
    if not wanted:
        parser.error("no groups given")
    bad = [g for g in wanted if g not in GROUPS or (g == "base" and not allow_base)]
    if bad:
        parser.error("unknown group(s): %s (valid: %s; base always runs)"
                     % (", ".join(bad), ", ".join(g for g in GROUPS if g != "base")))
    return set(wanted)


def _print_list(sections):
    print("groups: %s" % " ".join(GROUPS))
    counts = {g: 0 for g in GROUPS}
    for sec in sections:
        for g in sec.groups:
            counts[g] = counts.get(g, 0) + 1
        print("  %-60s [%s]" % (sec.name, " ".join(sec.groups)))
    print("\nsections: %d; per group: %s"
          % (len(sections), ", ".join("%s=%d" % (g, counts[g]) for g in GROUPS)))


def _resolve(parser, ns, source_file):
    """Return ('full', reason) | ('nothing', reason) | ('groups', groups, reason)."""
    if ns.only:
        return "groups", _group_list(ns.only, parser), "--only"
    if ns.skip:
        skipped = _group_list(ns.skip, parser)
        return "groups", set(GROUPS) - skipped, "--skip"
    if ns.all:
        return "full", "--all"

    project = os.path.dirname(os.path.dirname(os.path.abspath(source_file)))
    paths = changed_paths(project)
    if paths is None:
        return "full", "not a git checkout"
    if not paths:
        return "full", "clean tree"
    base = _merge_base(project)
    groups = groups_for_paths(paths, project, base)
    if not groups:
        return "nothing", "changed files are docs/deploy only: %s" % ", ".join(paths)
    if groups == set(GROUPS):
        return "full", "changed files: %s" % ", ".join(paths)
    return "groups", groups, "auto from %s" % ", ".join(paths)


def partial_plan(argv, source_file):
    """Return None for a full run, else the filtered source plus a report."""
    source = open(source_file, encoding="utf-8").read()
    lines, sections, setup, _tail, tail_line = parse_suite(source)
    if len(sections) < 2 or tail_line is None:
        return None

    parser, ns = _parse_args(argv)
    if ns.list:
        _print_list(sections)
        raise SystemExit(0)

    kind, *rest = _resolve(parser, ns, source_file)
    if kind == "full":
        return None
    if kind == "nothing":
        print("== mock E2E: %s ==" % rest[0])
        print("nothing to run (full suite with --all)")
        raise SystemExit(0)

    groups, reason = rest
    groups = _expand_groups(groups)
    base = {sec.name for sec in sections if "base" in sec.groups}
    selected = {sec.name for sec in sections if set(sec.groups) & groups} | base
    deps = section_dependencies(sections, setup)
    selected = _closure(selected, deps)
    if len(selected) >= len(sections):
        return None

    filtered = filter_source(lines, sections, selected, tail_line)
    skipped = [sec.name for sec in sections if sec.name not in selected]
    note = ("[partial run] %s: groups=%s; running %d/%d sections"
            % (reason, ",".join(sorted(groups)), len(selected), len(sections)))
    if skipped:
        note += "\n             skipped: %s" % ", ".join(
            s.split(":")[0][:28] for s in skipped)
    note += "\n             full suite: add --all"
    return {"source": filtered, "skipped": set(skipped), "note": note}
