"""Hand-rolled mutation tester.

Why this exists: mutmut v3.5 fights this project's tooling (xdist, multiprocessing
contexts, sibling-package imports). Cosmic-ray adds another moving part. This
script does the minimum: walk the AST of one production file, generate a finite
set of "obvious" mutations (comparator flips, boolean swaps, +/-1 constants,
return strips), apply them one at a time on disk, run a focused pytest, and
record kill/survive.

Run:
    /home/data/.venv/bin/python tools/manual_mutation_test.py \
        --target media_preview_generator/output/journal.py \
        --tests tests/test_output_journal.py \
        [--max N]
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = "/home/data/.venv/bin/python"


@dataclass
class Mutation:
    line: int
    col: int
    kind: str  # 'comparator', 'boolean', 'constant', 'return', 'boolop', 'aug'
    description: str
    original_src: str
    mutated_src: str
    killed: bool = False
    error: str | None = None


COMPARATOR_FLIPS: dict[type, type] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.LtE: ast.Gt,
    ast.Gt: ast.LtE,
    ast.GtE: ast.Lt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

BOOLOP_FLIPS: dict[type, type] = {
    ast.And: ast.Or,
    ast.Or: ast.And,
}


def _src_for_node(source: str, node: ast.AST) -> str:
    """Return the substring of `source` corresponding to `node`'s extent."""
    return ast.get_source_segment(source, node) or ""


def _replace_segment(source: str, node: ast.AST, new_text: str) -> str | None:
    """Replace the source segment for `node` with `new_text`.

    Uses (line, col) -> (end_line, end_col) extents from the AST.
    Returns the patched full source, or None if the node lacks position info.
    """
    if not all(hasattr(node, attr) for attr in ("lineno", "col_offset", "end_lineno", "end_col_offset")):
        return None

    lines = source.splitlines(keepends=True)
    # Convert to flat-index using line offsets.
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line))

    start = line_starts[node.lineno - 1] + node.col_offset
    end = line_starts[node.end_lineno - 1] + node.end_col_offset
    return source[:start] + new_text + source[end:]


def collect_mutations(source: str) -> list[Mutation]:
    """Walk the AST and produce mutation candidates."""
    tree = ast.parse(source)
    mutations: list[Mutation] = []

    for node in ast.walk(tree):
        # Comparator flips: only single-comparator compares for safety.
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op = node.ops[0]
            flip_cls = COMPARATOR_FLIPS.get(type(op))
            if flip_cls is None:
                continue
            new_op = flip_cls()
            new_compare = ast.Compare(left=node.left, ops=[new_op], comparators=node.comparators)
            ast.copy_location(new_compare, node)
            try:
                new_text = ast.unparse(new_compare)
            except Exception:
                continue
            orig = _src_for_node(source, node)
            mutations.append(
                Mutation(
                    line=node.lineno,
                    col=node.col_offset,
                    kind="comparator",
                    description=f"{type(op).__name__} -> {flip_cls.__name__}",
                    original_src=orig,
                    mutated_src=new_text,
                )
            )

        # BoolOp flips (and <-> or). Only first op (BoolOp can chain).
        elif isinstance(node, ast.BoolOp):
            flip_cls = BOOLOP_FLIPS.get(type(node.op))
            if flip_cls is None:
                continue
            new_node = ast.BoolOp(op=flip_cls(), values=node.values)
            ast.copy_location(new_node, node)
            try:
                new_text = ast.unparse(new_node)
            except Exception:
                continue
            mutations.append(
                Mutation(
                    line=node.lineno,
                    col=node.col_offset,
                    kind="boolop",
                    description=f"{type(node.op).__name__} -> {flip_cls.__name__}",
                    original_src=_src_for_node(source, node),
                    mutated_src=new_text,
                )
            )

        # Constant changes: int +/-1, bool flip, str -> "".
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                new_val = not node.value
                mutations.append(
                    Mutation(
                        line=node.lineno,
                        col=node.col_offset,
                        kind="constant",
                        description=f"{node.value!r} -> {new_val!r}",
                        original_src=repr(node.value),
                        mutated_src=repr(new_val),
                    )
                )
            elif isinstance(node.value, int) and not isinstance(node.value, bool):
                # Skip giant integers / line-noise values.
                if abs(node.value) > 10_000_000:
                    continue
                # Two mutations per int: +1 and -1.
                for delta in (1, -1):
                    new_val = node.value + delta
                    mutations.append(
                        Mutation(
                            line=node.lineno,
                            col=node.col_offset,
                            kind="constant",
                            description=f"{node.value} -> {new_val}",
                            original_src=repr(node.value),
                            mutated_src=repr(new_val),
                        )
                    )

        # Return value strip: `return X` -> `return None` (only if X != None already).
        elif isinstance(node, ast.Return) and node.value is not None:
            try:
                if isinstance(node.value, ast.Constant) and node.value.value is None:
                    continue
            except Exception:
                pass
            mutations.append(
                Mutation(
                    line=node.lineno,
                    col=node.col_offset,
                    kind="return",
                    description="return X -> return None",
                    original_src=_src_for_node(source, node),
                    mutated_src="return None",
                )
            )

    return mutations


def apply_mutation(source: str, m: Mutation) -> str | None:
    """Re-parse source, find the matching node by (line, col, kind, original_src), patch."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not hasattr(node, "lineno") or node.lineno != m.line or node.col_offset != m.col:
            continue
        # Constant mutation
        if m.kind == "constant" and isinstance(node, ast.Constant):
            if repr(node.value) != m.original_src:
                continue
            return _replace_segment(source, node, m.mutated_src)
        if m.kind == "comparator" and isinstance(node, ast.Compare) and len(node.ops) == 1:
            return _replace_segment(source, node, m.mutated_src)
        if m.kind == "boolop" and isinstance(node, ast.BoolOp):
            return _replace_segment(source, node, m.mutated_src)
        if m.kind == "return" and isinstance(node, ast.Return):
            return _replace_segment(source, node, m.mutated_src)
    return None


def run_tests(test_path: str) -> tuple[bool, str]:
    """Run focused pytest. Returns (passed, tail-of-output)."""
    proc = subprocess.run(
        [
            PYTHON,
            "-m",
            "pytest",
            test_path,
            "--no-cov",
            "-q",
            "-x",
            "-o",
            "addopts=",
            "--timeout=15",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    return proc.returncode == 0, "\n".join(tail)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="Production file to mutate (relative to repo root)")
    ap.add_argument("--tests", required=True, help="Test file/path to run for each mutation")
    ap.add_argument("--max", type=int, default=0, help="Cap on mutations (0 = unlimited)")
    ap.add_argument("--out", default="", help="Optional JSON output path")
    args = ap.parse_args()

    target = REPO_ROOT / args.target
    if not target.is_file():
        print(f"ERROR: target not found: {target}", file=sys.stderr)
        return 2

    original = target.read_text()
    mutations = collect_mutations(original)
    if args.max > 0:
        # Stable sample: deterministic stride to spread across the file.
        if len(mutations) > args.max:
            stride = len(mutations) / args.max
            mutations = [mutations[int(i * stride)] for i in range(args.max)]

    print(f"Target: {args.target} ({len(mutations)} mutations)")
    print(f"Tests:  {args.tests}")

    # Baseline must pass.
    print("Baseline test run...")
    ok, tail = run_tests(args.tests)
    if not ok:
        print(f"BASELINE FAILED — refusing to mutate.\n{tail}")
        return 3
    print(f"  {tail}")

    # Backup target.
    backup_dir = Path(tempfile.mkdtemp(prefix="mutation-backup-"))
    backup_path = backup_dir / target.name
    shutil.copy2(target, backup_path)

    results: list[Mutation] = []
    start = time.monotonic()
    try:
        for i, m in enumerate(mutations, 1):
            patched = apply_mutation(original, m)
            if patched is None or patched == original:
                m.error = "could-not-apply"
                results.append(m)
                continue
            target.write_text(patched)
            try:
                ok, tail = run_tests(args.tests)
            except subprocess.TimeoutExpired:
                ok, tail = False, "TIMEOUT"
            m.killed = not ok
            elapsed = time.monotonic() - start
            mark = "K" if m.killed else "S"
            print(f"[{i:>3}/{len(mutations)}] {mark} L{m.line:<3} {m.kind:<10} {m.description:<40} ({elapsed:.1f}s)")
            results.append(m)
    finally:
        # Always restore.
        shutil.copy2(backup_path, target)
        shutil.rmtree(backup_dir, ignore_errors=True)

    # Sanity: re-run baseline to confirm restore.
    ok, tail = run_tests(args.tests)
    if not ok:
        print("ERROR: post-restore baseline failed!")
        print(tail)
        return 4

    killed = sum(1 for m in results if m.killed)
    survived = sum(1 for m in results if not m.killed and m.error is None)
    errored = sum(1 for m in results if m.error is not None)
    total_runnable = killed + survived
    pct = (killed / total_runnable * 100) if total_runnable else 0.0

    print()
    print(f"Killed:    {killed}")
    print(f"Survived:  {survived}")
    print(f"Errored:   {errored}")
    print(f"Kill rate: {pct:.1f}%")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "target": args.target,
                    "tests": args.tests,
                    "killed": killed,
                    "survived": survived,
                    "errored": errored,
                    "kill_rate": pct,
                    "mutations": [
                        {
                            "line": m.line,
                            "col": m.col,
                            "kind": m.kind,
                            "description": m.description,
                            "original": m.original_src,
                            "mutated": m.mutated_src,
                            "killed": m.killed,
                            "error": m.error,
                        }
                        for m in results
                    ],
                },
                indent=2,
            )
        )
        print(f"Wrote: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
