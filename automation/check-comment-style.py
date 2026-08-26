"""check-comment-style.py: flags oversized comment blocks in executable source files."""
from __future__ import annotations

import ast
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INCLUDED_ROOTS = {
    ".github/workflows": (".yaml", ".yml"),
    "envs": (".tf", ".yaml", ".yml"),
    "automation": (".py", ".sh"),
    "helm": (".yaml", ".yml", ".tpl"),
    "monitoring": (".py",),
}

EXCLUDED_PATH_SUBSTRINGS = (
    "/__pycache__/", "/.git/", "/node_modules/",
    "/helm/argocd/charts/",
)

DIRECTIVE_PATTERNS = (
    re.compile(r"^#!"),
    re.compile(r"^#\s*-\*-\s*coding"),
    re.compile(r"^#\s*noqa\b"),
    re.compile(r"^#\s*type:\s*ignore\b"),
    re.compile(r"^#\s*pylint:"),
    re.compile(r"^#\s*flake8:"),
    re.compile(r"^#\s*pragma\b"),
    re.compile(r"^#\s*nosec\b"),
    re.compile(r"^#\s*shellcheck\b"),
    re.compile(r"^#\s*syntax="),
    re.compile(r"^#\s*escape="),
    re.compile(r"^#\s*SPDX-License-Identifier"),
    re.compile(r"^#\s*Copyright\b", re.IGNORECASE),
)


def _is_directive(stripped_line):
    return any(p.match(stripped_line) for p in DIRECTIVE_PATTERNS)


YAML_BLOCK_SCALAR_EXTENSIONS = (".yaml", ".yml", ".tpl")

# A line ending in a YAML block-scalar indicator: optional "- " marker, optional "key:", then |/> with an optional chomp/indent modifier.
BLOCK_SCALAR_START_RE = re.compile(r"^([ \t]*)(?:-[ \t]*)?(?:[\w.\-]+:)?[ \t]*[|>][+\-]?[0-9]*[ \t]*$")

# A line that is entirely a Helm/Go-template action; "-" trim markers make its own indentation unreliable, so never treat it as a block-scalar dedent.
GO_TEMPLATE_ONLY_RE = re.compile(r"^[ \t]*\{\{-?.*-?\}\}[ \t]*$")


def compute_block_scalar_mask(lines):
    """Per-line bool: True when the line is DATA inside a YAML block scalar, never a source comment."""
    mask = [False] * len(lines)
    i = 0
    n = len(lines)
    while i < n:
        stripped_end = lines[i].rstrip()
        match = BLOCK_SCALAR_START_RE.match(stripped_end)
        if not match:
            i += 1
            continue
        start_indent = len(match.group(1))

        content_indent = None
        j = i + 1
        while j < n:
            candidate = lines[j]
            if candidate.strip() == "" or GO_TEMPLATE_ONLY_RE.match(candidate):
                j += 1
                continue
            candidate_indent = len(candidate) - len(candidate.lstrip(" \t"))
            if candidate_indent <= start_indent:
                break
            content_indent = candidate_indent
            break

        if content_indent is None:
            i += 1
            continue

        k = i + 1
        while k < n:
            line_k = lines[k]
            if line_k.strip() == "" or GO_TEMPLATE_ONLY_RE.match(line_k):
                mask[k] = True
                k += 1
                continue
            indent_k = len(line_k) - len(line_k.lstrip(" \t"))
            if indent_k < content_indent:
                break
            mask[k] = True
            k += 1
        i = k
    return mask


def find_source_files():
    files = []
    for rel_root, extensions in INCLUDED_ROOTS.items():
        abs_root = os.path.join(REPO_ROOT, rel_root)
        if not os.path.isdir(abs_root):
            continue
        for dirpath, _dirnames, filenames in os.walk(abs_root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                if any(s in path for s in EXCLUDED_PATH_SUBSTRINGS):
                    continue
                if os.path.splitext(name)[1] in extensions:
                    files.append(path)

    dockerfile_roots = ("monitoring",)
    for rel_root in dockerfile_roots:
        abs_root = os.path.join(REPO_ROOT, rel_root)
        if not os.path.isdir(abs_root):
            continue
        for dirpath, _dirnames, filenames in os.walk(abs_root):
            for name in filenames:
                if name == "Dockerfile" or name.startswith("Dockerfile."):
                    path = os.path.join(dirpath, name)
                    if not any(s in path for s in EXCLUDED_PATH_SUBSTRINGS):
                        files.append(path)
    return sorted(set(files))


def check_line_comment_blocks(path, lines):
    block_scalar_mask = (
        compute_block_scalar_mask(lines)
        if os.path.splitext(path)[1] in YAML_BLOCK_SCALAR_EXTENSIONS
        else [False] * len(lines)
    )

    violations = []
    run = []
    for idx, raw_line in enumerate(lines, start=1):
        if block_scalar_mask[idx - 1]:
            if run and len(run) > 1:
                violations.append((path, run[0], f"consecutive standalone comment block ({len(run)} lines)"))
            run = []
            continue
        stripped = raw_line.strip()
        if stripped.startswith("#") and not _is_directive(stripped):
            run.append(idx)
            continue
        if run:
            if len(run) > 1:
                violations.append((path, run[0], f"consecutive standalone comment block ({len(run)} lines)"))
            run = []
    if len(run) > 1:
        violations.append((path, run[0], f"consecutive standalone comment block ({len(run)} lines)"))
    return violations


def check_python_docstrings(path, source):
    violations = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return violations

    nodes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    for node in nodes:
        doc = ast.get_docstring(node, clean=False)
        if doc and "\n" in doc.strip("\n"):
            lineno = getattr(node, "lineno", 1) if node is not tree else 1
            violations.append((path, lineno, "multi-line docstring (narrative-length)"))
    return violations


def check_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    lines = text.splitlines()

    violations = check_line_comment_blocks(path, lines)
    if path.endswith(".py"):
        violations += check_python_docstrings(path, text)
    return violations


def main():
    all_violations = []
    for path in find_source_files():
        all_violations.extend(check_file(path))

    if not all_violations:
        print("OK: no oversized comment blocks found.")
        return 0

    for path, lineno, reason in sorted(all_violations):
        rel = os.path.relpath(path, REPO_ROOT)
        print(f"{rel}:{lineno}: {reason}")
    print(f"\nFAIL: {len(all_violations)} oversized comment block(s) found.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
