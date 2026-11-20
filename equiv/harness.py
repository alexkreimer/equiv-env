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

from .extract import Task, Signature, get_paired_length_params, _clean_type_str


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
/* No stdbool.h — ExeBench deps often typedef int bool;
   stdbool.h would #define bool _Bool, turning the typedef
   into "typedef int _Bool" which is illegal (C keyword). */
#include <string.h>
#include <stdlib.h>
"""


# ---- Type-to-nondet mapping ----

def _nondet_for_type(c_type: str) -> str | None:
    """Map a C type to the appropriate nondet_* function."""
    clean = _clean_type_str(c_type)

    # Handle pointers — for nondet, we don't generate symbolic pointers directly;
    # instead we generate buffers in the harness
    if clean.endswith("*"):
        return None  # handled by buffer generation

    mapping = {
        "int": "nondet_int()",
        "signed": "nondet_int()",
        "signed int": "nondet_int()",
        "unsigned": "nondet_uint()",
        "unsigned int": "nondet_uint()",
        "char": "nondet_char()",
        "signed char": "nondet_char()",
        "unsigned char": "nondet_uchar()",
        "short": "nondet_short()",
        "short int": "nondet_short()",
        "signed short": "nondet_short()",
        "signed short int": "nondet_short()",
        "unsigned short": "nondet_ushort()",
        "unsigned short int": "nondet_ushort()",
        "long": "nondet_long()",
        "long int": "nondet_long()",
        "signed long": "nondet_long()",
        "signed long int": "nondet_long()",
        "unsigned long": "nondet_ulong()",
        "unsigned long int": "nondet_ulong()",
        "long long": "nondet_llong()",
        "long long int": "nondet_llong()",
        "signed long long": "nondet_llong()",
        "signed long long int": "nondet_llong()",
        "unsigned long long": "nondet_ullong()",
        "unsigned long long int": "nondet_ullong()",
        "size_t": "nondet_size_t()",
        "ssize_t": "nondet_long()",  # ssize_t is long on x86-64 LP64
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
        "int_fast8_t": "(int_fast8_t)nondet_char()",
        "uint_fast8_t": "(uint_fast8_t)nondet_uchar()",
        "int_fast16_t": "(int_fast16_t)nondet_short()",
        "uint_fast16_t": "(uint_fast16_t)nondet_ushort()",
        "int_fast32_t": "nondet_int()",
        "uint_fast32_t": "nondet_uint()",
        "int_fast64_t": "nondet_llong()",
        "uint_fast64_t": "nondet_ullong()",
        "int_least8_t": "(int_least8_t)nondet_char()",
        "uint_least8_t": "(uint_least8_t)nondet_uchar()",
        "int_least16_t": "(int_least16_t)nondet_short()",
        "uint_least16_t": "(uint_least16_t)nondet_ushort()",
        "int_least32_t": "nondet_int()",
        "uint_least32_t": "nondet_uint()",
        "int_least64_t": "nondet_llong()",
        "uint_least64_t": "nondet_ullong()",
    }
    return mapping.get(clean, None)


def _type_bounds(c_type: str) -> tuple[int, int, bool]:
    """Return (min_val, max_val, is_unsigned) for an x86-64 LP64 integer C type."""
    clean = _clean_type_str(c_type)
    if clean.endswith("*"):
        clean = clean[:-1].strip()

    if clean in ("bool", "_Bool"):
        return (0, 1, True)
    if clean in ("char", "signed char", "int8_t", "int_least8_t", "int_fast8_t"):
        return (-128, 127, False)
    if clean in ("unsigned char", "uint8_t", "uint_least8_t", "uint_fast8_t"):
        return (0, 255, True)
    if clean in ("short", "short int", "signed short", "signed short int", "int16_t", "int_least16_t"):
        return (-32768, 32767, False)
    if clean in ("unsigned short", "unsigned short int", "uint16_t", "uint_least16_t"):
        return (0, 65535, True)
    if clean in ("int", "signed", "signed int", "int32_t", "int_least32_t", "int_fast16_t", "int_fast32_t"):
        return (-2147483648, 2147483647, False)
    if clean in ("unsigned", "unsigned int", "uint32_t", "uint_least32_t", "uint_fast16_t", "uint_fast32_t"):
        return (0, 4294967295, True)
    if clean in (
        "long", "long int", "signed long", "signed long int",
        "long long", "long long int", "signed long long", "signed long long int",
        "int64_t", "int_least64_t", "int_fast64_t", "ssize_t", "ptrdiff_t",
        "intptr_t", "intmax_t",
    ):
        return (-(1 << 63), (1 << 63) - 1, False)
    if clean in (
        "unsigned long", "unsigned long int",
        "unsigned long long", "unsigned long long int",
        "uint64_t", "uint_least64_t", "uint_fast64_t",
        "size_t", "uintptr_t", "uintmax_t",
    ):
        return (0, (1 << 64) - 1, True)
    # Conservative signed 32-bit fallback
    return (-2147483648, 2147483647, False)


def _emit_range_assume(var_expr: str, c_type: str, lo: int, hi: int) -> str | None:
    """Emit a C-type-safe __CPROVER_assume bounding var_expr to [lo, hi].

    Clamps [lo, hi] to the C type's representable range so we NEVER emit
    unsatisfiable unsigned-vs-negative comparisons like `(unsigned)x >= -32768`.
    Returns None if the clamped range already covers the entire type.
    """
    tmin, tmax, is_unsigned = _type_bounds(c_type)
    clo = max(lo, tmin)
    chi = min(hi, tmax)
    if clo > chi:
        return None
    if clo <= tmin and chi >= tmax:
        return None
    if is_unsigned:
        if clo <= 0:
            return f"__CPROVER_assume({var_expr} <= {chi}ULL);"
        return f"__CPROVER_assume({var_expr} >= {clo}ULL && {var_expr} <= {chi}ULL);"
    return f"__CPROVER_assume({var_expr} >= {clo} && {var_expr} <= {chi});"


def _scalar_type_width(c_type: str) -> str:
    """Get the underlying width category for a scalar type (for buffer indexing)."""
    clean = _clean_type_str(c_type)
    if clean.endswith("*"):
        clean = clean[:-1].strip()

    small_types = {"char", "signed char", "unsigned char", "int8_t", "uint8_t", "_Bool", "bool"}
    medium_types = {
        "short", "short int", "signed short", "signed short int",
        "unsigned short", "unsigned short int", "int16_t", "uint16_t",
    }
    large_types = {
        "int", "signed", "signed int", "unsigned", "unsigned int",
        "int32_t", "uint32_t", "int_fast32_t", "uint_fast32_t",
    }
    xl_types = {
        "long", "long int", "signed long", "signed long int",
        "unsigned long", "unsigned long int",
        "long long", "long long int", "signed long long", "signed long long int",
        "unsigned long long", "unsigned long long int",
        "int64_t", "uint64_t", "size_t", "ssize_t", "ptrdiff_t",
        "intptr_t", "uintptr_t", "intmax_t", "uintmax_t",
    }

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
    """Extract the clean pointee type from a pointer type."""
    return _clean_type_str(ptr_type.replace("*", ""))


def _filter_deps(deps: str) -> str:
    """Filter dependency text: strip #include lines and redefinitions of types
    already provided by our standard headers (<stdint.h>, <stddef.h>, <string.h>)."""
    # Types already defined by the headers we include — strip their redefinitions
    REDEFINED_TYPES = {
        "size_t", "ssize_t", "intptr_t", "uintptr_t", "ptrdiff_t",
        "int8_t", "uint8_t", "int16_t", "uint16_t", "int32_t", "uint32_t",
        "int64_t", "uint64_t", "intmax_t", "uintmax_t",
        "int_fast8_t", "uint_fast8_t", "int_fast16_t", "uint_fast16_t",
        "int_fast32_t", "uint_fast32_t", "int_fast64_t", "uint_fast64_t",
        "int_least8_t", "uint_least8_t", "int_least16_t", "uint_least16_t",
        "int_least32_t", "uint_least32_t", "int_least64_t", "uint_least64_t",
        "NULL",
    }
    # Function prototypes already declared by <string.h> / <stdlib.h>
    REDEFINED_FUNCS = {
        "strlen", "strcmp", "strncmp", "strcpy", "strncpy", "strcat", "strncat",
        "memcmp", "memcpy", "memmove", "memset", "memchr",
        "malloc", "free", "calloc", "realloc",
    }

    import re as _re
    lines = deps.split("\n")
    filtered = []
    for line in lines:
        stripped = line.strip()
        # Skip #include lines and stray preprocessor line markers like `# 1 `
        if stripped.startswith("#include") or _re.match(r'^#\s*\d+\b', stripped):
            continue
        if not stripped:
            continue
        # Skip typedef redefinitions of types we already have
        m = _re.match(r'typedef\s+.+\s+(\w+)\s*;', stripped)
        if m and m.group(1) in REDEFINED_TYPES:
            continue
        # Skip function declarations of functions we already have
        m = _re.match(r'(?:extern\s+)?[\w\s*]+\s+(\w+)\s*\(', stripped)
        if m and m.group(1) in REDEFINED_FUNCS:
            continue
        filtered.append(line)
    return "\n".join(filtered)


def _header_for_task(task: Task) -> str:
    """Return standard includes, adding <stdbool.h> unless deps_src defines `bool`."""
    deps = task.deps_src or ""
    defines_bool = bool(
        re.search(r'\btypedef\b[^;]*\bbool\s*;', deps)
        or re.search(r'^\s*#\s*define\s+bool\b', deps, flags=re.MULTILINE)
        or re.search(r'\benum\b[^;]*\bbool\b', deps)
    )
    if defines_bool:
        return HEADER
    return HEADER + "#include <stdbool.h>\n"


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
    parts.append(_header_for_task(task))

    # 2. Dependency declarations (type definitions, macros)
    if task.deps_src:
        # Filter: only include typedefs, structs, and #defines; skip #includes
        filtered_deps = _filter_deps(task.deps_src)
        if filtered_deps:
            parts.append("/* Type definitions and macros */")
            parts.append(filtered_deps)

    # 3. MAXN definition
    parts.append(f"#define MAXN {maxn}")
    parts.append("")

    # Platform: x86-64 target with signed char
    parts.append("/* Platform assumption: x86-64, char is signed */")
    parts.append("/* Compile with: -fsigned-char, CBMC: --arch x86_64 */")
    parts.append("")

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


def _emit_scalar_inits(
    task: Task,
    maxn: int,
    *,
    use_narrow_ranges: bool = False,
) -> list[str]:
    """Emit symbolic scalar declarations and __CPROVER_assume bounds.

    Shared by main equivalence harness, overlap harness, and original-only UB
    harness so all three see the exact same input domain.
    """
    lines: list[str] = []
    sig = task.signature
    scalar_params = [(n, t) for n, t in sig.params if not t.strip().endswith("*")]
    if not scalar_params:
        return lines

    paired_lengths = get_paired_length_params(sig, task.pointer_rules)
    input_ranges = task.input_ranges or {}
    narrowed = use_narrow_ranges or bool(input_ranges.get("_narrowed"))

    lines.append("  /* 1. Symbolic scalar inputs */")
    for name, c_type in scalar_params:
        decl_type = _clean_type_str(c_type)
        nondet_call = _nondet_for_type(decl_type)
        if nondet_call:
            lines.append(f"  {decl_type} {name} = {nondet_call};")
        else:
            lines.append(f"  {decl_type} {name} = ({decl_type})nondet_int();")

    lines.append("")
    for name, c_type in scalar_params:
        decl_type = _clean_type_str(c_type)
        _, _, is_unsigned = _type_bounds(decl_type)

        # Paired length parameters MUST stay within [0, MAXN]
        if name in paired_lengths:
            if is_unsigned:
                lines.append(f"  __CPROVER_assume({name} <= MAXN);")
            else:
                lines.append(f"  __CPROVER_assume({name} >= 0 && {name} <= MAXN);")
            continue

        # Explicit per-parameter range from task.input_ranges
        if name in input_ranges and isinstance(input_ranges[name], (list, tuple)) and len(input_ranges[name]) == 2:
            lo, hi = int(input_ranges[name][0]), int(input_ranges[name][1])
            assume = _emit_range_assume(name, decl_type, lo, hi)
            if assume:
                lines.append(f"  {assume}")
            continue

        # Uniform narrowed range [-2^15, 2^15 - 1] (clamped safely to type bounds)
        if narrowed:
            assume = _emit_range_assume(name, decl_type, -32768, 32767)
            if assume:
                lines.append(f"  {assume}")
            continue

        # Default generous finite bound for wide signed scalars (avoids INT_MIN corner UB)
        if decl_type in (
            "int", "signed", "signed int",
            "long", "long int", "signed long", "signed long int",
            "long long", "long long int", "signed long long", "signed long long int",
            "int32_t", "int64_t", "ssize_t", "ptrdiff_t", "intptr_t", "intmax_t",
        ):
            lines.append(f"  __CPROVER_assume({name} > -1073741824 && {name} < 1073741824);")

    return lines


def _emit_buffer_inits(
    task: Task,
    maxn: int,
    *,
    dual: bool = True,
    use_narrow_ranges: bool = False,
) -> list[str]:
    """Emit symbolic buffer arrays (and optional identical copies for candidate).

    Handles `string` (null terminator at symbolic position < MAXN), `paired`,
    and `fixed` buffer rules, plus narrowed element bounds when requested.
    """
    lines: list[str] = []
    sig = task.signature
    pointer_rules = task.pointer_rules or {}
    pointer_params = [(n, t) for n, t in sig.params if t.strip().endswith("*")]
    if not pointer_params:
        return lines

    input_ranges = task.input_ranges or {}
    narrowed = use_narrow_ranges or bool(input_ranges.get("_narrowed"))
    buf_range = input_ranges.get("_buf_range", (-32768, 32767))

    lines.append("")
    if dual:
        lines.append("  /* 2. Symbolic buffers (separate for original and candidate) */")
    else:
        lines.append("  /* 2. Symbolic buffers */")

    for buf_idx, (name, c_type) in enumerate(pointer_params):
        pointee = _pointee_type(c_type)
        rule = pointer_rules.get(name, "fixed")
        nondet_elem = _nondet_for_type(pointee) or f"({pointee})nondet_int()"

        if rule == "string":
            # [R1] Every char* without a length param gets a '\0' at symbolic pos < MAXN
            lines.append(f"  {pointee} {name}_o[MAXN];")
            if dual:
                lines.append(f"  {pointee} {name}_c[MAXN];")
            lines.append(f"  for (int __i = 0; __i < MAXN; __i++) {{")
            lines.append(f"    {name}_o[__i] = {nondet_elem};")
            if dual:
                lines.append(f"    {name}_c[__i] = {name}_o[__i];")
            lines.append(f"  }}")
            lines.append(f"  int __null_{buf_idx} = nondet_int();")
            lines.append(f"  __CPROVER_assume(__null_{buf_idx} >= 0 && __null_{buf_idx} < MAXN);")
            lines.append(f"  {name}_o[__null_{buf_idx}] = '\\0';")
            if dual:
                lines.append(f"  {name}_c[__null_{buf_idx}] = '\\0';")
        else:
            lines.append(f"  {pointee} {name}_o[MAXN];")
            if dual:
                lines.append(f"  {pointee} {name}_c[MAXN];")
            lines.append(f"  for (int __i = 0; __i < MAXN; __i++) {{")
            lines.append(f"    {name}_o[__i] = {nondet_elem};")
            if narrowed:
                assume = _emit_range_assume(f"{name}_o[__i]", pointee, int(buf_range[0]), int(buf_range[1]))
                if assume:
                    lines.append(f"    {assume}")
            if dual:
                lines.append(f"    {name}_c[__i] = {name}_o[__i];")
            lines.append(f"  }}")

    return lines


def _emit_return_assert(sig: Signature, pointer_params: list[tuple[str, str]], buf_suffix_o: str = "_o", buf_suffix_c: str = "_c") -> list[str]:
    """Emit return-value equivalence assertion (handling pointer returns safely)."""
    lines: list[str] = []
    ret_clean = _clean_type_str(sig.return_type)
    if ret_clean == "void":
        return lines

    if ret_clean.endswith("*") and pointer_params:
        # Compare pointer returns by same-object + offset (never compare addresses of distinct arrays)
        lines.append("  if (__r_o == NULL) {")
        lines.append("    assert(__r_c == NULL);")
        for name, _ in pointer_params:
            o_buf = f"{name}{buf_suffix_o}"
            c_buf = f"{name}{buf_suffix_c}"
            lines.append("#ifdef __CPROVER__")
            lines.append(f"  }} else if (__CPROVER_same_object(__r_o, {o_buf})) {{")
            lines.append(f"    assert(__CPROVER_same_object(__r_c, {c_buf}) && (__r_o - {o_buf}) == (__r_c - {c_buf}));")
            lines.append("#endif")
        lines.append("  } else {")
        lines.append("    assert(__r_o == __r_c);")
        lines.append("  }")
    else:
        lines.append("  assert(__r_o == __r_c);")
    return lines


def _generate_harness_body(task: Task, maxn: int) -> str:
    """Generate the body of the equivalence harness function."""
    lines: list[str] = []
    sig = task.signature
    pointer_params = [(n, t) for n, t in sig.params if t.strip().endswith("*")]

    # Step 1: Symbolic scalars + bounds (paired lengths, input_ranges, etc.)
    lines.extend(_emit_scalar_inits(task, maxn))

    # Step 2: Symbolic buffer arrays + identical copies
    lines.extend(_emit_buffer_inits(task, maxn, dual=True))

    # Step 3: Call both functions
    lines.append("")
    lines.append("  /* 3. Call original and candidate */")

    orig_args = []
    cand_args = []
    for name, c_type in sig.params:
        if c_type.strip().endswith("*"):
            orig_args.append(f"{name}_o")
            cand_args.append(f"{name}_c")
        else:
            orig_args.append(name)
            cand_args.append(name)

    orig_call = f"orig__{task.func_name}({', '.join(orig_args)});"
    cand_call = f"cand__{task.func_name}({', '.join(cand_args)});"

    ret_clean = _clean_type_str(sig.return_type)
    ret_is_void = ret_clean == "void"

    if not ret_is_void:
        lines.append(f"  {ret_clean} __r_o = {orig_call}")
        lines.append(f"  {ret_clean} __r_c = {cand_call}")
    else:
        lines.append(f"  {orig_call}")
        lines.append(f"  {cand_call}")

    # Step 4: Assert equivalence
    lines.append("")
    lines.append("  /* 4. Assert equivalence */")
    lines.extend(_emit_return_assert(sig, pointer_params))

    # Assert buffer equality post-call
    for name, _ in pointer_params:
        lines.append(f"  for (int __i = 0; __i < MAXN; __i++) {{")
        lines.append(f"    assert({name}_o[__i] == {name}_c[__i]);")
        lines.append(f"  }}")

    return "\n".join(lines)


def generate_ub_harness(
    task: Task,
    maxn: int = 8,
    unwind: int | None = None,
    use_narrow_ranges: bool = False,
) -> HarnessResult:
    """Generate an original-only UB check harness using the exact same input
    setup (headers, deps, string terminators, paired length bounds, ranges)
    as the main equivalence harness."""
    if unwind is None:
        unwind = maxn + 2

    parts: list[str] = []
    parts.append(_header_for_task(task))

    if task.deps_src:
        filtered_deps = _filter_deps(task.deps_src)
        if filtered_deps:
            parts.append("/* Type definitions and macros */")
            parts.append(filtered_deps)

    parts.append(f"#define MAXN {maxn}")
    parts.append("")
    parts.append(NONDET_DECLS)

    orig_renamed = _rename_function(task.func_src, task.func_name, f"orig__{task.func_name}")
    if orig_renamed is None:
        orig_renamed = task.func_src
        call_name = task.func_name
    else:
        call_name = f"orig__{task.func_name}"
    parts.append(orig_renamed)

    parts.append("")
    parts.append("/* Original-only UB check harness */")
    parts.append("void harness(void) {")
    parts.extend(_emit_scalar_inits(task, maxn, use_narrow_ranges=use_narrow_ranges))
    parts.extend(_emit_buffer_inits(task, maxn, dual=False, use_narrow_ranges=use_narrow_ranges))

    sig = task.signature
    args = [f"{name}_o" if c_type.strip().endswith("*") else name for name, c_type in sig.params]
    ret_clean = _clean_type_str(sig.return_type)
    parts.append("")
    if ret_clean != "void":
        parts.append(f"  {ret_clean} __r = {call_name}({', '.join(args)});")
        parts.append("  (void)__r;")
    else:
        parts.append(f"  {call_name}({', '.join(args)});")
    parts.append("}")

    return HarnessResult(
        harness_src="\n".join(parts),
        original_func_name=call_name,
        candidate_func_name=call_name,
        maxn=maxn,
        unwind=unwind,
    )


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


def generate_overlap_harness(task: Task, candidate_src: str,
                              maxn: int = 8, unwind: int | None = None) -> HarnessResult | None:
    """Generate an overlap-sensitivity harness.

    For tasks with >=2 pointer params of compatible type and no restrict,
    creates a harness where those pointers share one buffer at symbolic offsets.
    Returns None if the task doesn't need an overlap check.

    R1 spec Step 3 item 5.
    """
    if unwind is None:
        unwind = maxn + 2

    sig = task.signature
    pointer_params = [(name, c_type) for name, c_type in sig.params if c_type.endswith("*")]

    if len(pointer_params) < 2:
        return None

    # Check if the original declares restrict — if so, no overlap check needed
    if "restrict" in task.func_src:
        return None

    # Check compatible types (same pointee)
    pointees = [_pointee_type(t) for _, t in pointer_params]
    if len(set(pointees)) > 1:
        # Different pointee types — overlap doesn't make sense
        return None

    pointee = pointees[0]

    parts = []
    parts.append(_header_for_task(task))
    parts.append(f"#define MAXN {maxn}")
    parts.append(f"/* Overlap harness — all {len(pointer_params)} pointers share one buffer */")

    if task.deps_src:
        filtered = _filter_deps(task.deps_src)
        if filtered:
            parts.append(filtered)

    parts.append(NONDET_DECLS)

    # Rename functions
    orig_renamed = _rename_function(task.func_src, task.func_name, f"orig__{task.func_name}")
    if orig_renamed is None:
        orig_renamed = task.func_src
    parts.append(orig_renamed)

    cand_name = _extract_function_name(candidate_src) or task.func_name
    cand_renamed = _rename_function(candidate_src, cand_name, f"cand__{task.func_name}")
    if cand_renamed is None:
        cand_renamed = candidate_src
    parts.append(cand_renamed)

    # Harness
    parts.append("void harness(void) {")

    # Scalar params (with paired length bounds and input_ranges)
    parts.extend(_emit_scalar_inits(task, maxn))

    # Shared buffer: double the size to accommodate offsets
    nondet_elem = _nondet_for_type(pointee) or f"({pointee})nondet_int()"
    input_ranges = task.input_ranges or {}
    narrowed = bool(input_ranges.get("_narrowed"))
    buf_range = input_ranges.get("_buf_range", (-32768, 32767))
    pointer_rules = task.pointer_rules or {}
    any_string = any(pointer_rules.get(name) == "string" for name, _ in pointer_params)

    parts.append("")
    parts.append("  /* Shared buffer: all pointers alias into one array */")
    parts.append(f"  {pointee} shared_o[MAXN * 2];")
    parts.append(f"  {pointee} shared_c[MAXN * 2];")
    parts.append("  for (int __i = 0; __i < MAXN * 2; __i++) {")
    parts.append(f"    shared_o[__i] = {nondet_elem};")
    if narrowed and not any_string:
        assume = _emit_range_assume("shared_o[__i]", pointee, int(buf_range[0]), int(buf_range[1]))
        if assume:
            parts.append(f"    {assume}")
    parts.append("    shared_c[__i] = shared_o[__i];")
    parts.append("  }")

    # Symbolic offsets for each pointer into the shared buffer
    pointer_names = [name for name, _ in pointer_params]
    for idx, name in enumerate(pointer_names):
        if idx == 0:
            parts.append(f"  {pointee} *{name}_o = shared_o;")
            parts.append(f"  {pointee} *{name}_c = shared_c;")
        else:
            parts.append(f"  int __off_{idx} = nondet_int();")
            parts.append(f"  __CPROVER_assume(__off_{idx} >= 0 && __off_{idx} <= MAXN);")
            parts.append(f"  {pointee} *{name}_o = shared_o + __off_{idx};")
            parts.append(f"  {pointee} *{name}_c = shared_c + __off_{idx};")

        if pointer_rules.get(name) == "string":
            parts.append(f"  int __null_{idx} = nondet_int();")
            parts.append(f"  __CPROVER_assume(__null_{idx} >= 0 && __null_{idx} < MAXN);")
            parts.append(f"  {name}_o[__null_{idx}] = '\\0';")
            parts.append(f"  {name}_c[__null_{idx}] = '\\0';")

    # Call both
    parts.append("")
    parts.append("  /* Call original and candidate on shared buffers */")
    orig_args = []
    cand_args = []
    for name, c_type in sig.params:
        if c_type.strip().endswith("*"):
            orig_args.append(f"{name}_o")
            cand_args.append(f"{name}_c")
        else:
            orig_args.append(name)
            cand_args.append(name)

    ret_clean = _clean_type_str(sig.return_type)
    ret_void = ret_clean == "void"
    if not ret_void:
        parts.append(f"  {ret_clean} __r_o = orig__{task.func_name}({', '.join(orig_args)});")
        parts.append(f"  {ret_clean} __r_c = cand__{task.func_name}({', '.join(cand_args)});")
        if ret_clean.endswith("*"):
            parts.append("  if (__r_o == NULL) {")
            parts.append("    assert(__r_c == NULL);")
            parts.append("#ifdef __CPROVER__")
            parts.append("  } else if (__CPROVER_same_object(__r_o, shared_o)) {")
            parts.append("    assert(__CPROVER_same_object(__r_c, shared_c) && (__r_o - shared_o) == (__r_c - shared_c));")
            parts.append("#endif")
            parts.append("  } else {")
            parts.append("    assert(__r_o == __r_c);")
            parts.append("  }")
        else:
            parts.append("  assert(__r_o == __r_c);")
    else:
        parts.append(f"  orig__{task.func_name}({', '.join(orig_args)});")
        parts.append(f"  cand__{task.func_name}({', '.join(cand_args)});")

    # Compare shared buffers
    parts.append("  for (int __i = 0; __i < MAXN * 2; __i++) {")
    parts.append("    assert(shared_o[__i] == shared_c[__i]);")
    parts.append("  }")

    parts.append("}")

    return HarnessResult(
        harness_src="\n".join(parts),
        original_func_name=f"orig__{task.func_name}",
        candidate_func_name=f"cand__{task.func_name}",
        maxn=maxn,
        unwind=unwind,
    )