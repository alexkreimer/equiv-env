"""
harness.py — Generate CBMC equivalence harness for a (original, candidate) pair.

Given a Task (from extract.py) and candidate source code, produces a complete C file
that CBMC can check for equivalence. Uses symbolic inputs, identical buffer copies,
and asserts equality of return values and post-call memory.

Usage:
    from equiv.harness import generate_harness
    harness_src = generate_harness(task, candidate_src)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .extract import Task, Signature


# ---- CBMC primitive declarations ----

NONDET_DECLS = """
/* CBMC primitive: returns any value of the given type */
int nondet_int(void);
unsigned int nondet_uint(void);
char nondet_char(void);
unsigned char nondet_uchar(void);
short nondet_short(void);
unsigned short nondet_ushort(void);
long nondet_long(void);
unsigned long nondet_ulong(void);
long long nondet_llong(void);
unsigned long long nondet_ullong(void);
size_t nondet_size_t(void);
_Bool nondet_bool(void);

/* Stub for non-CBMC compilation (gcc/clang) */
#ifndef __CPROVER__
int nondet_int(void) { return 0; }
unsigned int nondet_uint(void) { return 0; }
char nondet_char(void) { return 'x'; }
unsigned char nondet_uchar(void) { return 0; }
short nondet_short(void) { return 0; }
unsigned short nondet_ushort(void) { return 0; }
long nondet_long(void) { return 0; }
unsigned long nondet_ulong(void) { return 0; }
long long nondet_llong(void) { return 0; }
unsigned long long nondet_ullong(void) { return 0; }
size_t nondet_size_t(void) { return 0; }
_Bool nondet_bool(void) { return 0; }
#endif
"""

HEADER = """
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <string.h>
#include <stdlib.h>
"""


# ---- Type-to-nondet mapping ----

def _nondet_for_type(c_type: str) -> str:
    """Map a C type to the appropriate nondet_* function."""
    clean = c_type.replace("const ", "").replace("volatile ", "").replace("restrict ", "").strip()

    # Handle pointers — for nondet, we don't generate symbolic pointers directly;
    # instead we generate buffers in the harness
    if clean.endswith("*"):
        return None  # handled by buffer generation

    mapping = {
        "int": "nondet_int()",
        "unsigned int": "nondet_uint()",
        "char": "nondet_char()",
        "unsigned char": "nondet_uchar()",
        "short": "nondet_short()",
        "unsigned short": "nondet_ushort()",
        "long": "nondet_long()",
        "unsigned long": "nondet_ulong()",
        "long long": "nondet_llong()",
        "unsigned long long": "nondet_ullong()",
        "size_t": "nondet_size_t()",
        "ssize_t": "nondet_long()",  # ssize_t is long on most platforms
        "ptrdiff_t": "nondet_long()",
        "bool": "nondet_bool()",
        "_Bool": "nondet_bool()",
        # Fixed-width types
        "int8_t": "(int8_t)nondet_char()",
        "uint8_t": "(uint8_t)nondet_uchar()",
        "int16_t": "(int16_t)nondet_short()",
        "uint16_t": "(uint16_t)nondet_ushort()",
        "int32_t": "nondet_int()",
        "uint32_t": "nondet_uint()",
        "int64_t": "nondet_llong()",
        "uint64_t": "nondet_ullong()",
        "intptr_t": "nondet_long()",
        "uintptr_t": "nondet_ulong()",
        "intmax_t": "nondet_llong()",
        "uintmax_t": "nondet_ullong()",
        "int_fast8_t": "nondet_int()",
        "uint_fast8_t": "nondet_uint()",
        "int_fast16_t": "nondet_int()",
        "uint_fast16_t": "nondet_uint()",
        "int_fast32_t": "nondet_int()",
        "uint_fast32_t": "nondet_uint()",
        "int_fast64_t": "nondet_llong()",
        "uint_fast64_t": "nondet_ullong()",
        "int_least8_t": "nondet_int()",
        "uint_least8_t": "nondet_uint()",
        "int_least16_t": "nondet_int()",
        "uint_least16_t": "nondet_uint()",
        "int_least32_t": "nondet_int()",
        "uint_least32_t": "nondet_uint()",
        "int_least64_t": "nondet_llong()",
        "uint_least64_t": "nondet_ullong()",
    }
    return mapping.get(clean, None)


def _scalar_type_width(c_type: str) -> str:
    """Get the underlying width category for a scalar type (for buffer indexing)."""
    clean = c_type.replace("const ", "").replace("volatile ", "").replace("restrict ", "").strip()
    if clean.endswith("*"):
        clean = clean[:-1].strip()

    small_types = {"char", "unsigned char", "int8_t", "uint8_t", "_Bool", "bool"}
    medium_types = {"short", "unsigned short", "int16_t", "uint16_t"}
    large_types = {"int", "unsigned int", "int32_t", "uint32_t", "int_fast32_t", "uint_fast32_t"}
    xl_types = {"long", "unsigned long", "long long", "unsigned long long",
                "int64_t", "uint64_t", "size_t", "ssize_t", "ptrdiff_t",
                "intptr_t", "uintptr_t", "intmax_t", "uintmax_t"}

    if clean in small_types:
        return "char"
    elif clean in medium_types:
        return "short"
    elif clean in large_types:
        return "int"
    elif clean in xl_types:
        return "long long"
    else:
        return "int"  # default fallback


def _pointee_type(ptr_type: str) -> str:
    """Extract the pointee type from a pointer type."""
    return ptr_type.replace("const ", "").replace("volatile ", "").replace("restrict ", "").replace("*", "").strip()


def _filter_deps(deps: str) -> str:
    """Filter dependency text: strip #include lines, keep typedefs/structs/defines."""
    lines = deps.split("\n")
    filtered = []
    for line in lines:
        stripped = line.strip()
        # Skip #include lines
        if stripped.startswith("#include"):
            continue
        # Skip empty/whitespace-only lines
        if not stripped:
            continue
        filtered.append(line)
    return "\n".join(filtered)


# ---- Harness generation ----

@dataclass
class HarnessResult:
    """Result of harness generation."""
    harness_src: str
    original_func_name: str  # orig__NAME
    candidate_func_name: str  # cand__NAME
    maxn: int
    unwind: int


def generate_harness(task: Task, candidate_src: str, maxn: int = 8, unwind: int | None = None) -> HarnessResult:
    """Generate a CBMC equivalence harness C file.

    Args:
        task: The eligible Task from extract.py.
        candidate_src: The candidate function source code.
        maxn: Maximum buffer size for pointer parameters.
        unwind: Loop unwind bound (default: maxn + 2).

    Returns:
        HarnessResult with the complete harness source.
    """
    if unwind is None:
        unwind = maxn + 2

    parts = []

    # 1. Header includes
    parts.append(HEADER)

    # 2. Dependency declarations (type definitions, macros)
    if task.deps_src:
        # Filter: only include typedefs, structs, and #defines; skip #includes
        filtered_deps = _filter_deps(task.deps_src)
        if filtered_deps:
            parts.append("/* Type definitions and macros */")
            parts.append(filtered_deps)

    # 3. MAXN definition
    parts.append(f"#define MAXN {maxn}")

    # 4. Nondet declarations + stubs
    parts.append(NONDET_DECLS)

    # 5. Rename original function
    original_renamed = _rename_function(task.func_src, task.func_name, f"orig__{task.func_name}")
    if original_renamed is None:
        # Can't rename safely — inline it
        parts.append(f"/* Original function (inline) */")
        parts.append(task.func_src)
        original_renamed = task.func_src
    else:
        parts.append(f"/* Original function (renamed to orig__{task.func_name}) */")
        parts.append(original_renamed)

    # 5. Rename candidate function
    # Extract the candidate's function name from its source
    cand_name = _extract_function_name(candidate_src)
    if cand_name is None:
        cand_name = task.func_name  # assume same name
    candidate_renamed = _rename_function(candidate_src, cand_name, f"cand__{task.func_name}")
    if candidate_renamed is None:
        parts.append(f"/* Candidate function (inline) */")
        parts.append(candidate_src)
        candidate_renamed = candidate_src
    else:
        parts.append(f"/* Candidate function (renamed to cand__{task.func_name}) */")
        parts.append(candidate_renamed)

    # 6. Harness function
    parts.append(f"/* Equivalence harness */")
    parts.append(f"void harness(void) {{")

    harness_body = _generate_harness_body(task, maxn)
    parts.append(harness_body)

    parts.append("}")

    return HarnessResult(
        harness_src="\n".join(parts),
        original_func_name=f"orig__{task.func_name}",
        candidate_func_name=f"cand__{task.func_name}",
        maxn=maxn,
        unwind=unwind,
    )


def _generate_harness_body(task: Task, maxn: int) -> str:
    """Generate the body of the harness function."""
    lines = []
    sig = task.signature
    pointer_rules = task.pointer_rules

    scalar_params = []
    pointer_params = []

    for name, c_type in sig.params:
        if c_type.endswith("*"):
            pointer_params.append((name, c_type))
        else:
            scalar_params.append((name, c_type))

    # Step 1: Symbolic scalars — generate nondet values with __CPROVER_assume bounds
    if scalar_params:
        lines.append("  /* 1. Symbolic scalar inputs */")
    for name, c_type in scalar_params:
        nondet_call = _nondet_for_type(c_type)
        if nondet_call:
            lines.append(f"  {c_type} {name} = {nondet_call};")
        else:
            # Fallback for unknown types — try to use nondet_int
            lines.append(f"  {c_type} {name} = ({c_type})nondet_int();")

    # Assume bounds for integer scalars to prevent overflow/UB
    # Use generous but finite bounds
    if scalar_params:
        lines.append("")
        for name, c_type in scalar_params:
            clean = c_type.replace("const ", "").strip()
            # Bound large values to prevent overflow UB
            if clean in ("int", "long", "long long", "int32_t", "int64_t",
                          "ssize_t", "ptrdiff_t", "intptr_t", "intmax_t"):
                lines.append(f"  __CPROVER_assume({name} > -1073741824 && {name} < 1073741824);")

    # Step 2: Symbolic buffer arrays + identical copies
    if pointer_params:
        lines.append("")
        lines.append("  /* 2. Symbolic buffers (separate for original and candidate) */")

    # Track buffer declarations for the post-call comparison
    buffer_vars_orig = []
    buffer_vars_cand = []

    for buf_idx, (name, c_type) in enumerate(pointer_params):
        pointee = _pointee_type(c_type)
        rule = pointer_rules.get(name, "fixed")

        if rule == "string":
            # String: char array with null terminator at symbolic position
            lines.append(f"  {pointee} {name}_o[MAXN];")
            lines.append(f"  {pointee} {name}_c[MAXN];")
            lines.append(f"  for (int __i = 0; __i < MAXN; __i++) {{")
            lines.append(f"    {name}_o[__i] = nondet_char();")
            lines.append(f"    {name}_c[__i] = {name}_o[__i];")
            lines.append(f"  }}")
            # Force null termination somewhere, allowing empty string at position 0
            lines.append(f"  int __null_{buf_idx} = nondet_int();")
            lines.append(f"  __CPROVER_assume(__null_{buf_idx} >= 0 && __null_{buf_idx} < MAXN);")
            lines.append(f"  {name}_o[__null_{buf_idx}] = '\\0';")
            lines.append(f"  {name}_c[__null_{buf_idx}] = '\\0';")
        else:
            # Fixed or paired: integer-typed buffer
            width = _scalar_type_width(pointee)
            lines.append(f"  {pointee} {name}_o[MAXN];")
            lines.append(f"  {pointee} {name}_c[MAXN];")
            lines.append(f"  for (int __i = 0; __i < MAXN; __i++) {{")
            if width == "char":
                lines.append(f"    {name}_o[__i] = ({pointee})nondet_uchar();")
            elif width == "short":
                lines.append(f"    {name}_o[__i] = ({pointee})nondet_ushort();")
            elif width == "int":
                lines.append(f"    {name}_o[__i] = ({pointee})nondet_int();")
            else:
                lines.append(f"    {name}_o[__i] = ({pointee})nondet_llong();")
            lines.append(f"    {name}_c[__i] = {name}_o[__i];")
            lines.append(f"  }}")

            # For paired pointers, constrain the size parameter
            if rule == "paired":
                # Find the paired size parameter and constrain it
                lines.append(f"  /* {name} is paired with a size parameter */")

        buffer_vars_orig.append(f"{name}_o")
        buffer_vars_cand.append(f"{name}_c")

    # Step 3: Call both functions
    lines.append("")
    lines.append("  /* 3. Call original and candidate */")

    # Build call argument lists
    orig_args = []
    cand_args = []
    for name, c_type in sig.params:
        if c_type.endswith("*"):
            orig_args.append(f"{name}_o")
            cand_args.append(f"{name}_c")
        else:
            orig_args.append(name)
            cand_args.append(name)

    orig_call = f"orig__{task.func_name}({', '.join(orig_args)});"
    cand_call = f"cand__{task.func_name}({', '.join(cand_args)});"

    ret_is_void = sig.return_type == "void"

    if not ret_is_void:
        lines.append(f"  {sig.return_type} __r_o = {orig_call}")
        lines.append(f"  {sig.return_type} __r_c = {cand_call}")
    else:
        lines.append(f"  {orig_call}")
        lines.append(f"  {cand_call}")

    # Step 4: Assert equivalence
    lines.append("")
    lines.append("  /* 4. Assert equivalence */")

    if not ret_is_void:
        lines.append(f"  assert(__r_o == __r_c);")

    # Assert buffer equality post-call
    for buf_idx, (name, _) in enumerate(pointer_params):
        lines.append(f"  for (int __i = 0; __i < MAXN; __i++) {{")
        lines.append(f"    assert({name}_o[__i] == {name}_c[__i]);")
        lines.append(f"  }}")

    return "\n".join(lines)


def _rename_function(src: str, old_name: str, new_name: str) -> str | None:
    """Rename a function definition by replacing its identifier at the definition site.

    Only renames the function at its DEFINITION (the name immediately before
    the parameter list), not inside attributes, comments, or calls.
    """
    # Find the function definition: old_name followed by optional whitespace and '('
    # but NOT preceded by '(' or word chars that make it part of a longer name.
    # We match the name that appears right before the opening paren of the parameter list.

    # Strategy: use a regex that finds old_name at the start of the function definition.
    # A function definition looks like: [return type] [*]name(params)
    # We match: word boundary + old_name + optional whitespace + '('
    # But we need to be careful not to match inside __attribute__((...))

    # Simple approach: find the FIRST occurrence of old_name( that is NOT inside double parens
    # (i.e., not inside __attribute__((...)))

    lines = src.split('\n')
    new_lines = []
    renamed = False
    for line in lines:
        if not renamed:
            # Work on a copy of the line, stripping __attribute__ blocks for matching
            search_line = re.sub(r'__attribute__\s*\(\([^)]*\)\)', '', line)
            # Try to find old_name followed by whitespace and ( on this line
            pattern = re.compile(
                r'(?<![a-zA-Z_])'  # not preceded by word char
                + re.escape(old_name)
                + r'\s*\('          # followed by optional whitespace and (
            )
            match = pattern.search(search_line)
            if match:
                # Find the position of old_name in the ORIGINAL line
                # (it may be offset if __attribute__ was stripped)
                name_pos = line.find(old_name, match.start())
                if name_pos == -1:
                    # Try matching in the original line directly
                    match_orig = pattern.search(line)
                    if match_orig:
                        name_pos = match_orig.start()
                    else:
                        new_lines.append(line)
                        continue

                name_end = name_pos + len(old_name)
                # Find the '(' after the name
                paren_pos = line.find('(', name_end)
                if paren_pos == -1:
                    new_lines.append(line)
                    continue

                # Replace just old_name, keep everything else
                line = line[:name_pos] + new_name + line[name_end:]
                renamed = True

        new_lines.append(line)

    if renamed:
        return '\n'.join(new_lines)
    return None


def _extract_function_name(src: str) -> str | None:
    """Extract the function name from C source code."""
    stripped = src.strip()

    # Remove leading decorators and attributes
    # First strip common storage class prefixes
    for prefix in ("static ", "inline ", "extern "):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]

    # Strip __attribute__((...)) specifiers (any content inside the double parens)
    stripped = re.sub(r'__attribute__\s*\(\([^)]*(?:\)\))?', '', stripped).strip()

    # Split on first '(' to find the function name
    paren_idx = stripped.find("(")
    if paren_idx == -1:
        return None

    before_paren = stripped[:paren_idx].strip().split()
    if not before_paren:
        return None

    candidate = before_paren[-1]
    # Remove leading *
    if candidate.startswith("*"):
        candidate = candidate[1:]

    return candidate if re.match(r'^[a-zA-Z_]\w*$', candidate) else None


def generate_self_harness(task: Task, maxn: int = 8, unwind: int | None = None) -> HarnessResult:
    """Generate a self-equivalence harness (original vs identical copy).

    Used for the self-equivalence check during dataset building.
    """
    return generate_harness(task, task.func_src, maxn=maxn, unwind=unwind)