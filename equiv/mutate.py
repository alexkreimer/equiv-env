"""
mutate.py — Mutation operators for validation (mutation study, §7.2).

Generates mutants of C functions using simple source-level transformations:
- Swap relational operators (< ↔ <=, == ↔ !=)
- Swap + / -
- Change constants by ±1
- Delete one statement
- Swap loop bounds

Usage:
    from equiv.mutate import mutate, MUTATION_OPERATORS
    mutants = mutate(func_src)
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass


@dataclass
class Mutant:
    src: str
    operator: str
    description: str


# Mutation operators
MUTATION_OPERATORS = [
    "swap_relational",
    "swap_add_sub",
    "change_constant",
    "delete_statement",
    "swap_loop_bound",
]


def mutate(src: str, n: int = 1, operators: list[str] | None = None) -> list[Mutant]:
    """Generate n random mutants of a C function.

    Args:
        src: The function source code.
        n: Number of mutants to generate.
        operators: Which operators to use (default: all).

    Returns:
        List of Mutant objects.
    """
    if operators is None:
        operators = list(MUTATION_OPERATORS)

    mutants = []
    for _ in range(n):
        op = random.choice(operators)
        mutant = _apply_operator(src, op)
        if mutant:
            mutants.append(mutant)

    return mutants


def _apply_operator(src: str, operator: str) -> Mutant | None:
    """Apply a single mutation operator. Returns None if no applicable site found."""
    if operator == "swap_relational":
        return _swap_relational(src)
    elif operator == "swap_add_sub":
        return _swap_add_sub(src)
    elif operator == "change_constant":
        return _change_constant(src)
    elif operator == "delete_statement":
        return _delete_statement(src)
    elif operator == "swap_loop_bound":
        return _swap_loop_bound(src)
    return None


def _swap_relational(src: str) -> Mutant | None:
    """Swap a relational operator: < ↔ <=, == ↔ !=, > ↔ >=."""
    swaps = [
        ("<=", "<"), ("<", "<="),
        (">=", ">"), (">", ">="),
        ("==", "!="), ("!=", "=="),
    ]
    for old, new in swaps:
        # Find an occurrence (not inside comments)
        pattern = re.compile(r'(?<![=!<>])' + re.escape(old) + r'(?!=)')
        match = pattern.search(src)
        if match:
            new_src = src[:match.start()] + new + src[match.end():]
            return Mutant(new_src, "swap_relational", f"Changed {old} to {new}")


def _swap_add_sub(src: str) -> Mutant | None:
    """Swap + and - operators."""
    for old, new in [("+", "-"), ("-", "+")]:
        # Find a + or - that's not part of ++, --, +=, -=
        pattern = re.compile(r'(?<![+\-])' + re.escape(old) + r'(?![+\-=])')
        match = pattern.search(src)
        if match:
            new_src = src[:match.start()] + new + src[match.end():]
            return Mutant(new_src, "swap_add_sub", f"Changed {old} to {new}")


def _change_constant(src: str) -> Mutant | None:
    """Change a numeric constant by ±1."""
    # Find numeric literals
    pattern = re.compile(r'(?<!\w)(\d+)(?!\w)')
    matches = list(pattern.finditer(src))
    if not matches:
        return None

    match = random.choice(matches)
    value = int(match.group(1))
    direction = random.choice([-1, 1])
    new_value = max(0, value + direction)

    new_src = src[:match.start()] + str(new_value) + src[match.end():]
    return Mutant(new_src, "change_constant", f"Changed {value} to {new_value}")


def _delete_statement(src: str) -> Mutant | None:
    """Delete one statement from the function body."""
    # Split into lines, find statement lines inside the function body
    lines = src.split("\n")
    body_start = 0
    for i, line in enumerate(lines):
        if "{" in line:
            body_start = i + 1
            break

    if body_start >= len(lines):
        return None

    # Find non-blank, non-brace lines in the body
    stmt_indices = []
    for i in range(body_start, len(lines) - 1):  # -1 to preserve closing brace
        stripped = lines[i].strip()
        if stripped and stripped != "{" and stripped != "}" and not stripped.startswith("//"):
            stmt_indices.append(i)

    if not stmt_indices:
        return None

    idx = random.choice(stmt_indices)
    new_lines = lines[:idx] + lines[idx + 1:]
    return Mutant("\n".join(new_lines), "delete_statement",
                  f"Deleted line {idx}: {lines[idx].strip()[:50]}")


def _swap_loop_bound(src: str) -> Mutant | None:
    """Swap a loop bound (change < to <= or vice versa, or change bound variable)."""
    return _swap_relational(src)  # Reuse relational swap for loop conditions