"""
abstraction.py — Make arithmetic-heavy equivalence checks tractable for CBMC.

Why: CBMC bit-blasts arithmetic into gates. A SAT solver cannot see that `+`
is associative/commutative, so a correct rewrite that reorders a sum, or any
loop multiplying two variables, times out (see DECISIONS.md, Decision 16).

Fast-prove = two CBMC runs, both of which must succeed:

  1. Equivalence under abstraction. Every `x * y` with two non-constant operands
     becomes an uninterpreted function call MUL_T(min(x,y), max(x,y)) in BOTH
     functions (the min/max argument order makes it commutative). Signed
     overflow checks are off, so arithmetic wraps mod 2^w. Proving equality for
     ALL interpretations of MUL_T proves it for real (wrapped) multiplication.
  2. Safety of the candidate alone, with real arithmetic and all checks on
     (no overflow, no out-of-bounds) over the harness inputs.

Soundness: the original is UB-free on the harness inputs (dataset filter) and
run 2 shows the candidate is too, so their C semantics equal the wrap-around
semantics run 1 compared. Incomplete by design: a rewrite that changes the
arithmetic itself (x*2 -> x<<1 is kept exact since constants are not
abstracted, but (a+b)*c -> a*c+b*c) fails run 1. A run-1 failure is never
reported as a counterexample; the caller falls back to the exact check.
"""

from __future__ import annotations

import os
import re
import tempfile

from .extract import Task, clang_ast_dump

UF_PREFIX = "__CPROVER_uninterpreted_mul_"
HELPER_PREFIX = "__absmul_"

_INT_TYPE_RE = re.compile(
    r"^(?:(?:unsigned|signed)\s+)?(?:char|short|int|long|long\s+long|long\s+int|short\s+int|long\s+long\s+int)$"
    r"|^unsigned$|^u?int(?:8|16|32|64)_t$|^size_t$|^ssize_t$|^ptrdiff_t$"
)


def _ident(t: str) -> str:
    return re.sub(r"\W+", "_", t.strip())


def helper_block(types: set[str]) -> str:
    """C definitions of the commutative uninterpreted multiply, one per type."""
    out = ["/* Fast-prove: uninterpreted, commutative multiplication */"]
    for t in sorted(types):
        i = _ident(t)
        out.append(f"{t} {UF_PREFIX}{i}({t}, {t});")
        out.append(f"static {t} {HELPER_PREFIX}{i}({t} x, {t} y) "
                   f"{{ return x < y ? {UF_PREFIX}{i}(x, y) : {UF_PREFIX}{i}(y, x); }}")
    return "\n".join(out) + "\n"


def _strip(node: dict) -> dict:
    while node.get("kind") in ("ImplicitCastExpr", "ParenExpr", "ConstantExpr") and node.get("inner"):
        node = node["inner"][0]
    return node


def _is_constant(node: dict) -> bool:
    n = _strip(node)
    k = n.get("kind")
    if k in ("IntegerLiteral", "CharacterLiteral", "UnaryExprOrTypeTraitExpr"):
        return True
    if k == "UnaryOperator" and n.get("opcode") in ("-", "+", "~") and n.get("inner"):
        return _is_constant(n["inner"][0])
    return False


def _span(node: dict) -> tuple[int, int] | None:
    r = node.get("range") or {}
    b, e = r.get("begin", {}), r.get("end", {})
    if "spellingLoc" in b or "expansionLoc" in b or "spellingLoc" in e or "expansionLoc" in e:
        return None  # inside a macro expansion: offsets are not source text
    if "offset" not in b or "offset" not in e or "tokLen" not in e:
        return None
    return b["offset"], e["offset"] + e["tokLen"]


class _GiveUp(Exception):
    pass


def abstract_mul(task: Task, func_src: str) -> tuple[str, set[str]] | None:
    """Rewrite var*var multiplications in `func_src` to commutative UF calls.

    Returns (new_src, types_used), (func_src, set()) if there is nothing to
    abstract, or None if the rewrite cannot be done safely (macros, `*=` on
    something other than a plain variable, unknown types).
    """
    from .harness import _filter_deps, _header_for_task

    prefix = _header_for_task(task) + "\n"
    if task.deps_src:
        deps = _filter_deps(task.deps_src)
        if deps:
            prefix += deps + "\n"
    tu = prefix + func_src + "\n"
    base = len(prefix.encode())
    src_b = func_src.encode()

    with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
        f.write(tu)
        path = f.name
    try:
        ast = clang_ast_dump(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if ast is None:
        return None

    # Top-level decls that come from func_src (offset at/after `base`).
    roots = []
    for d in ast.get("inner", []):
        sp = _span(d) if d.get("range") else None
        if sp and sp[0] >= base:
            roots.append(d)
    if not roots:
        return None

    muls: list[dict] = []

    def walk(n: dict):
        k = n.get("kind")
        if k == "CompoundAssignOperator" and n.get("opcode") == "*=":
            a, b = n["inner"][0], n["inner"][1]
            if not _is_constant(b):
                # `v *= e` -> `v = MUL(v, e)`; only for a plain variable (no side effects).
                t = (n.get("type") or {}).get("qualType", "")
                if _strip(a).get("kind") != "DeclRefExpr" or not _INT_TYPE_RE.match(t.strip()):
                    raise _GiveUp("*= on a non-variable or non-integer")
                n["_compound"] = True
                muls.append(n)
        if k == "BinaryOperator" and n.get("opcode") == "*":
            a, b = n["inner"][0], n["inner"][1]
            if not (_is_constant(a) or _is_constant(b)):
                t = (n.get("type") or {}).get("qualType", "")
                if not _INT_TYPE_RE.match(t.strip()):
                    raise _GiveUp(f"non-integer multiply type {t!r}")
                muls.append(n)
        for c in n.get("inner", []) or []:
            if isinstance(c, dict):
                walk(c)

    try:
        for r in roots:
            walk(r)
    except _GiveUp:
        return None
    if not muls:
        return func_src, set()

    spans = {}
    for m in muls:
        s = [_span(m), _span(m["inner"][0]), _span(m["inner"][1])]
        if any(x is None for x in s):
            return None
        spans[id(m)] = [(a - base, b - base) for a, b in s]

    types: set[str] = set()

    def render(lo: int, hi: int) -> str:
        """Source bytes [lo, hi) with every abstracted multiply inside rewritten."""
        inside = [m for m in muls if lo <= spans[id(m)][0][0] and spans[id(m)][0][1] <= hi]
        # keep only outermost
        top = [m for m in inside if not any(
            o is not m and spans[id(o)][0][0] <= spans[id(m)][0][0] and spans[id(m)][0][1] <= spans[id(o)][0][1]
            for o in inside)]
        top.sort(key=lambda m: spans[id(m)][0][0])
        out, pos = [], lo
        for m in top:
            (s, e), (ls, le), (rs, re_) = spans[id(m)]
            out.append(src_b[pos:s].decode())
            t = m["type"]["qualType"].strip()
            types.add(t)
            if m.get("_compound"):
                lhs = src_b[ls:le].decode()
                out.append(f"{lhs} = {HELPER_PREFIX}{_ident(t)}(({lhs}), ({render(rs, re_)}))")
            else:
                out.append(f"{HELPER_PREFIX}{_ident(t)}(({render(ls, le)}), ({render(rs, re_)}))")
            pos = e
        out.append(src_b[pos:hi].decode())
        return "".join(out)

    return render(0, len(src_b)), types


def inject_helpers(harness_src: str, types: set[str]) -> str:
    """Insert the UF helper definitions right after the nondet declarations."""
    if not types:
        return harness_src
    from .harness import NONDET_DECLS
    block = helper_block(types)
    if NONDET_DECLS in harness_src:
        return harness_src.replace(NONDET_DECLS, NONDET_DECLS + "\n" + block, 1)
    return block + harness_src
