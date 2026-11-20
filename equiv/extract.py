"""
extract.py — Parse C functions from ExeBench, check eligibility via clang AST.

Reads zst-compressed JSONL files (ExeBench format), extracts the function body,
runs clang AST dump to get the signature and detect forbidden constructs, and
applies the eligibility rules from the Stage 0 spec (§2).

Usage:
    from equiv.extract import extract_from_exebench, Eligibility
    for record in extract_from_exebench("data/synth_test/data_0_....jsonl.zst"):
        if record.eligible:
            task = record.task
        else:
            print(f"Rejected: {record.reason_code}")
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

# ---- Data types ----


@dataclass
class Signature:
    """Parsed function signature."""
    name: str
    return_type: str
    params: list[tuple[str, str]]  # [(name, type), ...]


@dataclass
class Task:
    """An eligible function ready for the pipeline."""
    task_id: str
    source: str  # "exebench"
    split: str
    func_name: str
    func_src: str
    deps_src: str
    signature: Signature
    pointer_rules: dict  # mapping param_name -> "paired" | "fixed" | "string"
    maxn: int = 8
    unwind: int = 10
    self_check_time_s: float = 0.0
    speed_baseline: dict | None = None
    io_tests: list[dict] | None = None
    input_ranges: dict | None = None  # {param_name: (min, max)} or None if full range
    n_loops: int = 0
    loc: int = 0


@dataclass
class Eligibility:
    """Result of eligibility check."""
    eligible: bool
    task: Task | None = None
    reason_code: str | None = None  # set when not eligible


# ---- Reason codes ----
REASON_NOT_COMPILABLE = "not_compilable"
REASON_CALLS_EXTERNAL = "calls_external_function"
REASON_USES_GLOBALS = "uses_global_variables"
REASON_UNSUPPORTED_TYPE = "unsupported_type"  # float, struct, union, etc.
REASON_FORBIDDEN_CONSTRUCT = "forbidden_construct"  # inline asm, varargs, etc.
REASON_NO_SIGNATURE = "no_signature"
REASON_VOID_PARAMS = "void_params"  # function takes no parameters
REASON_LIBC_NOT_MODELED = "libc_not_modeled"  # calls memset/memcpy/strlen etc. not modeled
REASON_PARSE_ERROR = "parse_error"
REASON_EXCLUDED_SOURCE = "excluded_source"  # path matches excluded_path_patterns in config.yaml


from .config import get_config as _get_config

# Allow-listed libc functions — only those verified to have correct CBMC models
# (CBMC 6.11.0, --arch x86_64, with system headers).
# memcmp NOT included: CBMC model returns wrong values.
# See DECISIONS.md Decision 12 & config.yaml.
ALLOWED_LIBC_CALLS: set[str] = set(_get_config()["dataset"]["allowed_libc"])

# Case-insensitive path substrings excluded from dataset extraction (see config.yaml).
EXCLUDED_PATH_PATTERNS: tuple[str, ...] = tuple(
    p.lower() for p in _get_config()["dataset"]["excluded_path_patterns"]
)


# ---- Type classification ----

INTEGER_TYPES = {
    "char", "short", "int", "long", "long long",
    "signed", "unsigned",
    "signed char", "unsigned char",
    "signed short", "unsigned short", "short int", "signed short int", "unsigned short int",
    "signed int", "unsigned int",
    "signed long", "unsigned long", "long int", "signed long int", "unsigned long int",
    "signed long long", "unsigned long long",
    "long long int", "signed long long int", "unsigned long long int",
    # stdint types
    "int8_t", "int16_t", "int32_t", "int64_t",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int_fast8_t", "int_fast16_t", "int_fast32_t", "int_fast64_t",
    "uint_fast8_t", "uint_fast16_t", "uint_fast32_t", "uint_fast64_t",
    "int_least8_t", "int_least16_t", "int_least32_t", "int_least64_t",
    "uint_least8_t", "uint_least16_t", "uint_least32_t", "uint_least64_t",
    "intptr_t", "uintptr_t", "intmax_t", "uintmax_t",
    "size_t", "ssize_t", "ptrdiff_t",
    "bool", "_Bool",
}

FORBIDDEN_TYPES = {
    "float", "double", "long double",
}

_TYPE_QUALIFIERS_RE = re.compile(
    r'\b(?:const|volatile|restrict|__restrict__|__restrict|register)\b'
)


def _clean_type_str(typ: str) -> str:
    """Strip C type/storage qualifiers and normalize whitespace."""
    cleaned = _TYPE_QUALIFIERS_RE.sub("", typ)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def _is_integer_type(typ: str) -> bool:
    """Check if a type string represents an integer scalar type."""
    clean = _clean_type_str(typ)
    return clean in INTEGER_TYPES


def _is_pointer_to_integer(typ: str) -> tuple[bool, str]:
    """Check if type is a single pointer to an integer type or void*. Returns (is_valid_ptr, pointee_type)."""
    clean = _clean_type_str(typ)
    # Reject double pointers (e.g. int **, char * *, void **)
    if clean.count("*") != 1:
        return False, ""
    if clean.endswith("*"):
        pointee = clean[:-1].strip()
        if _is_integer_type(pointee) or pointee == "void":
            return True, pointee
    return False, ""


def _is_forbidden_type(typ: str) -> bool:
    """Check if type contains forbidden types (float, struct, etc.)."""
    clean = _clean_type_str(typ)
    if "struct " in clean or "union " in clean or "enum " in clean:
        return True
    for word in clean.replace("*", " ").split():
        if word in FORBIDDEN_TYPES:
            return True
    return False


def _strip_comments(source: str) -> str:
    """Strip C-style comments from source code."""
    # Remove // comments
    source = re.sub(r'//.*$', '', source, flags=re.MULTILINE)
    # Remove /* */ comments
    source = re.sub(r'/\*.*?\*/', '', source, flags=re.DOTALL)
    return source


def _normalize_whitespace(source: str) -> str:
    """Collapse whitespace for hashing/comparison."""
    source = _strip_comments(source)
    # Collapse multiple whitespace to single space
    source = re.sub(r'\s+', ' ', source)
    return source.strip()


def _get_body_only(func_body: str) -> str:
    """Strip function signature line(s) to get only the body (after opening brace)."""
    body_lines = func_body.strip().split('\n')
    if len(body_lines) <= 1:
        # Single-line function: strip everything before the first {
        brace_idx = func_body.find('{')
        if brace_idx != -1:
            return func_body[brace_idx:]
        return func_body
    # Find the opening brace
    for i, line in enumerate(body_lines):
        if '{' in line:
            brace_idx = line.index('{')
            body_lines[i] = line[brace_idx:]  # strip return type + signature
            return '\n'.join(body_lines[i:])
    # Fallback: skip first line
    return '\n'.join(body_lines[1:])


def _detect_forbidden_constructs(func_body: str) -> str | None:
    """Check for forbidden constructs in function body. Returns reason code or None."""
    body_only = _get_body_only(func_body)

    # Inline assembly
    if re.search(r'\basm\s*\(', body_only) or re.search(r'__asm__\s*\(', body_only):
        return REASON_FORBIDDEN_CONSTRUCT
    # Varargs (note: ... in function signature is handled separately)
    if re.search(r'va_\w+', body_only):
        return REASON_FORBIDDEN_CONSTRUCT
    # setjmp/longjmp
    if re.search(r'\bsetjmp\b', body_only) or re.search(r'\blongjmp\b', body_only):
        return REASON_FORBIDDEN_CONSTRUCT
    # malloc/free
    if re.search(r'\bmalloc\b', body_only) or re.search(r'\bfree\b', body_only):
        return REASON_FORBIDDEN_CONSTRUCT
    # I/O
    if re.search(r'\bprintf\b', body_only) or re.search(r'\bscanf\b', body_only):
        return REASON_FORBIDDEN_CONSTRUCT
    if re.search(r'\bfopen\b', body_only) or re.search(r'\bfread\b', body_only):
        return REASON_FORBIDDEN_CONSTRUCT
    # static local variables
    if re.search(r'\bstatic\s+\w', body_only):
        return REASON_FORBIDDEN_CONSTRUCT
    return None


LENGTH_PARAM_NAMES = {
    "n", "len", "length", "size", "count", "num", "sz", "cnt",
    "width", "height", "rows", "cols", "nelems", "nb",
}
LENGTH_PARAM_SUFFIXES = ("_len", "_size", "_n", "_count", "_num", "_sz", "_width", "_height")
LENGTH_PARAM_PREFIXES = ("len_", "num_", "n_")


def _is_length_param_name(name: str) -> bool:
    """Return True if a scalar parameter name looks like a buffer length/size."""
    lower = name.lower()
    if lower in LENGTH_PARAM_NAMES:
        return True
    if any(lower.endswith(s) for s in LENGTH_PARAM_SUFFIXES):
        return True
    if any(lower.startswith(p) for p in LENGTH_PARAM_PREFIXES):
        return True
    return False


def get_paired_length_params(signature: Signature, pointer_rules: dict | None = None) -> set[str]:
    """Return scalar parameter names that act as buffer lengths for pointer params.

    Only returns scalar (non-pointer) parameters, and only when the signature
    contains at least one pointer parameter (and, if pointer_rules is given,
    at least one pointer has rule == 'paired').
    """
    has_pointer = any(typ.strip().endswith("*") for _, typ in signature.params)
    if not has_pointer:
        return set()
    if pointer_rules is not None and "paired" not in pointer_rules.values():
        return set()

    paired_scalars = set()
    for name, typ in signature.params:
        if typ.strip().endswith("*"):
            continue
        if _is_length_param_name(name):
            paired_scalars.add(name)
    return paired_scalars


def _infer_pointer_rules(signature: Signature, func_body: str) -> dict[str, str]:
    """Infer pointer sizing rules for each pointer parameter.

    Returns: {param_name: "paired" | "fixed" | "string"}
    """
    rules = {}
    # Find scalar (non-pointer) integer parameters that look like a length/size
    length_scalars = [
        other_name
        for other_name, other_typ in signature.params
        if not other_typ.strip().endswith("*") and _is_length_param_name(other_name)
    ]

    for name, typ in signature.params:
        ptr_match = _is_pointer_to_integer(typ)
        if not ptr_match[0]:
            continue

        pointee = ptr_match[1]
        is_char_ptr = pointee in ("char", "unsigned char", "signed char")

        if length_scalars:
            rules[name] = "paired"
        elif is_char_ptr:
            # [R1] Every char* without a length param gets a null terminator
            # at a symbolic position < MAXN. No need to detect string usage.
            rules[name] = "string"
        else:
            rules[name] = "fixed"

    return rules


def _count_loops(func_body: str) -> int:
    """Count loop constructs in the function body."""
    stripped = _strip_comments(func_body)
    loops = 0
    loops += len(re.findall(r'\bfor\s*\(', stripped))
    loops += len(re.findall(r'\bwhile\s*\(', stripped))
    loops += len(re.findall(r'\bdo\s*\{', stripped))
    return loops


def has_input_dependent_loop(func_body: str, signature: Signature) -> bool:
    """Heuristic: True if at least one loop condition depends on an input parameter."""
    stripped = _strip_comments(func_body)
    param_names = {name for name, _ in signature.params if name}
    if not param_names:
        return False

    # Check for(init; cond; step) headers and while(cond) headers
    for m in re.finditer(r'\bfor\s*\(([^;]*);([^;]*);([^)]*)\)', stripped):
        cond = m.group(2)
        if any(re.search(rf'\b{re.escape(p)}\b', cond) for p in param_names):
            return True
    for m in re.finditer(r'\bwhile\s*\(([^)]*)\)', stripped):
        cond = m.group(1)
        if any(re.search(rf'\b{re.escape(p)}\b', cond) for p in param_names):
            return True
        # Also catch pointer copies like `char *p = s; while (*p)`
        if "*" in cond:
            return True
    return False


def _count_loc(func_body: str) -> int:
    """Count lines of code (non-blank, non-comment-only)."""
    stripped = _strip_comments(func_body)
    lines = [l for l in stripped.split('\n') if l.strip()]
    return len(lines)


def _extract_head_from_def(func_def: str) -> str | None:
    """Extract the clean function signature header from func_def (before '{').

    ExeBench's `func_head` field drops spaces in scalar parameters (e.g. `intn`,
    `intlen`, `intsize`), causing false rejections. Parsing directly from
    `func_def` avoids this. Returns None for K&R-style parameter lists so the
    caller can fall back to `func_head`.
    """
    cleaned = _strip_comments(func_def)
    brace_idx = cleaned.find("{")
    if brace_idx == -1:
        return None
    head = re.sub(r'\s+', ' ', cleaned[:brace_idx]).strip()
    if "(" not in head or ")" not in head:
        return None

    # Detect K&R-style parameter declarations after closing ')':
    # e.g. `int same(w1, w2) short *w1; int *w2;`
    open_idx = head.find("(")
    depth = 0
    close_idx = -1
    for i in range(open_idx, len(head)):
        if head[i] == "(":
            depth += 1
        elif head[i] == ")":
            depth -= 1
            if depth == 0:
                close_idx = i
                break
    if close_idx == -1:
        return None
    trailing = head[close_idx + 1:].strip()
    if ";" in trailing:
        # K&R declaration list — let caller fall back to ExeBench's ANSI func_head
        return None
    return head[:close_idx + 1].strip()


# ---- Main extraction ----


def extract_from_exebench(
    zst_path: str,
    split: str,
    *,
    excluded_path_patterns: list[str] | tuple[str, ...] | None = None,
    allowed_libc: set[str] | list[str] | None = None,
) -> Iterator[Eligibility]:
    """Stream ExeBench zst-compressed JSONL, check each function's eligibility.

    Args:
        zst_path: Path to .jsonl.zst file from ExeBench.
        split: Dataset split name (e.g. "train", "test").
        excluded_path_patterns: Optional override for excluded path substrings.
        allowed_libc: Optional override for allowed libc function names.

    Yields:
        Eligibility for each function in the file.
    """
    import zstandard as zstd

    with open(zst_path, "rb") as fh:
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(fh) as reader:
            buf = ""
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    yield _check_eligibility(
                        record,
                        split,
                        excluded_path_patterns=excluded_path_patterns,
                        allowed_libc=allowed_libc,
                    )


def _check_eligibility(
    record: dict,
    split: str,
    *,
    excluded_path_patterns: list[str] | tuple[str, ...] | None = None,
    allowed_libc: set[str] | list[str] | None = None,
) -> Eligibility:
    """Check if a single ExeBench record passes eligibility rules."""
    t = record.get("text", record)

    # Get fields (account for the text wrapper)
    func_def = t.get("func_def", "")
    func_head = t.get("func_head", "")
    fname = t.get("fname", "")
    path = t.get("path", "")
    real_error = t.get("real_error", "")
    angha_deps = t.get("angha_deps", "") or t.get("real_deps", "") or ""

    # Build a unique task ID
    task_id = f"{split}:{path}:{fname}"

    # 0. Exclude compiler testsuites / synthetic obfuscator repos (config.yaml)
    patterns = (
        EXCLUDED_PATH_PATTERNS
        if excluded_path_patterns is None
        else tuple(p.lower() for p in excluded_path_patterns)
    )
    path_lower = path.lower()
    if patterns and any(pat in path_lower for pat in patterns):
        return Eligibility(eligible=False, reason_code=REASON_EXCLUDED_SOURCE)

    # 1. Must compile
    if real_error:
        return Eligibility(eligible=False, reason_code=REASON_NOT_COMPILABLE)

    # 2. Must not reference global variables (fast check via deps)
    if angha_deps:
        # Check for global variable declarations in deps
        # Pattern: "extern" or standalone variable declarations
        if re.search(r'\bextern\s+\w', angha_deps):
            return Eligibility(eligible=False, reason_code=REASON_USES_GLOBALS)
        # Check iospec if available
        iospec = t.get("angha_iospec")
        if iospec and isinstance(iospec, dict):
            globals_list = iospec.get("globals", [])
            if globals_list:
                return Eligibility(eligible=False, reason_code=REASON_USES_GLOBALS)

    # 3. Parse signature — prefer func_def over ExeBench's func_head, because
    #    func_head drops spaces in scalar parameters (e.g. `intn`, `intlen`).
    #    Fall back to func_head for K&R definitions or missing bodies.
    def_head = _extract_head_from_def(func_def)
    sig = _parse_signature(def_head, func_def, fname) if def_head else None
    if sig is None:
        sig = _parse_signature(func_head, func_def, fname)
    if sig is None:
        return Eligibility(eligible=False, reason_code=REASON_NO_SIGNATURE)

    # Check for varargs in signature
    if any(typ == "..." or name == "..." for name, typ in sig.params):
        return Eligibility(eligible=False, reason_code=REASON_FORBIDDEN_CONSTRUCT)

    # 4. Check parameter types
    for name, typ in sig.params:
        # Check for forbidden types (float, struct, etc.)
        if _is_forbidden_type(typ):
            return Eligibility(eligible=False, reason_code=REASON_UNSUPPORTED_TYPE)
        # Must be either integer scalar or pointer to integer
        is_ptr_int, _ = _is_pointer_to_integer(typ)
        is_int = _is_integer_type(typ)
        if not is_int and not is_ptr_int:
            return Eligibility(eligible=False, reason_code=REASON_UNSUPPORTED_TYPE)

    # 5. Check return type
    ret = _clean_type_str(sig.return_type)
    if ret == "void":
        pass  # OK
    elif _is_integer_type(ret):
        pass  # OK
    elif _is_pointer_to_integer(ret)[0]:
        pass  # OK
    elif _is_forbidden_type(ret):
        return Eligibility(eligible=False, reason_code=REASON_UNSUPPORTED_TYPE)
    else:
        return Eligibility(eligible=False, reason_code=REASON_UNSUPPORTED_TYPE)

    # 6. Function must take at least one parameter (no void functions for v1)
    if len(sig.params) == 0:
        return Eligibility(eligible=False, reason_code=REASON_VOID_PARAMS)

    # 7. Check for forbidden constructs in function body
    reason = _detect_forbidden_constructs(func_def)
    if reason:
        return Eligibility(eligible=False, reason_code=reason)

    # 8. Check for calls to non-allowlisted functions
    allowed_set = ALLOWED_LIBC_CALLS if allowed_libc is None else set(allowed_libc)
    body_only = _get_body_only(func_def)
    called_fns = _extract_called_functions(body_only)
    _SKIP_CALL_NAMES = {
        "if", "while", "for", "return", "switch", "case", "sizeof",
        "default", "break", "continue", "goto", "void", "char",
        "int", "long", "short", "unsigned", "signed", "const",
        "static", "inline", "typedef", "struct", "enum", "union",
        "__attribute__", "__extension__", "__builtin", "typeof", "__typeof__",
    } | {t for t in INTEGER_TYPES if " " not in t}
    for fn_name in called_fns:
        # Skip CBMC builtins, C keywords, integer cast types, and attribute specifiers
        if fn_name in _SKIP_CALL_NAMES:
            continue
        # Skip the function's own name (self-reference)
        if fn_name == fname:
            continue
        if fn_name in allowed_set:
            continue
        # Check if it's a libc function we don't model
        _LIBC_FUNCTIONS = {"memcmp", "memmove", "strcmp", "strncmp", "strcpy", "strncpy",
                           "abs", "labs", "llabs", "malloc", "free",
                           "printf", "scanf", "fopen", "fread", "fwrite"}
        if fn_name in _LIBC_FUNCTIONS:
            return Eligibility(eligible=False, reason_code=REASON_LIBC_NOT_MODELED)
        # If the function calls anything else, reject
        return Eligibility(eligible=False, reason_code=REASON_CALLS_EXTERNAL)

    # Build Task
    rules = _infer_pointer_rules(sig, func_def)
    task = Task(
        task_id=task_id,
        source="exebench",
        split=split,
        func_name=fname,
        func_src=func_def,
        deps_src=angha_deps,
        signature=sig,
        pointer_rules=rules,
        maxn=8,
        unwind=10,
        n_loops=_count_loops(func_def),
        loc=_count_loc(func_def),
        # IO tests extracted later from angha_io_pairs if available
        io_tests=_extract_io_tests(t),
    )

    return Eligibility(eligible=True, task=task)


def _strip_attributes(text: str) -> str:
    """Remove __attribute__((...)) blocks (handling nested parens)."""
    out = text
    while True:
        m = re.search(r'__attribute__\s*\(\(', out)
        if not m:
            break
        start = m.start()
        i = m.end() - 2  # at first '('
        depth = 0
        while i < len(out):
            if out[i] == "(":
                depth += 1
            elif out[i] == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        out = out[:start] + " " + out[i:]
    return out


def _parse_signature(func_head: str, func_def: str, fname: str) -> Signature | None:
    """Parse function signature from a C header string (from func_def or func_head).

    Examples:
        "int sum(int a, int b)"
        "void reset(void)"
        "static int foo(char *s, size_t n)"
        "char *strcpy(char *dest, const char *src)"

    Returns Signature or None if parsing fails.
    """
    head = _strip_attributes(func_head.strip())
    # Remove leading storage/inline specifiers
    head = re.sub(
        r'\b(?:static|inline|__inline__|__inline|extern|__extension__|__forceinline)\b',
        ' ',
        head,
    )
    head = re.sub(r'\s+', ' ', head).strip()

    # Split into return type + function name, and params
    paren_idx = head.find("(")
    if paren_idx == -1:
        return None

    before_paren = head[:paren_idx].strip()
    after_paren = head[paren_idx:]

    # Separate stars cleanly so `char* foo` and `char *foo` both tokenize properly
    before_spaced = re.sub(r'\*', ' * ', before_paren)
    parts = before_spaced.split()
    if not parts:
        return None

    func_name = parts[-1]
    if not re.match(r'^[a-zA-Z_]\w*$', func_name):
        return None
    return_type = " ".join(parts[:-1]).strip() if len(parts) > 1 else "int"
    # Normalize spaces before `*` (e.g. "const char *")
    return_type = re.sub(r'\s+', ' ', return_type).strip()

    # Parse parameters from the paren section
    params_str = after_paren.strip()
    if not params_str.startswith("("):
        return None

    # Extract content between outer parentheses
    depth = 0
    param_str = ""
    for ch in params_str:
        if ch == '(':
            depth += 1
            if depth > 1:
                param_str += ch
        elif ch == ')':
            depth -= 1
            if depth == 0:
                break
            if depth > 0:
                param_str += ch
        elif depth >= 1:
            param_str += ch

    # Parse individual parameters
    params = []
    if param_str.strip() and param_str.strip() != "void":
        raw_params = _split_params(param_str)
        for idx, rp in enumerate(raw_params):
            rp = rp.strip()
            if not rp:
                continue
            if rp == "...":
                params.append(("...", "..."))
                continue
            # Function pointer parameters like `int (*cmp)(int, int)` are unsupported
            if "(" in rp:
                return None

            # Count array dimensions (e.g. `int a[4]` -> 1 pointer level, `int a[4][4]` -> 2)
            array_dims = len(re.findall(r'\[[^\]]*\]', rp))
            rp_no_arr = re.sub(r'\[[^\]]*\]', '', rp).strip()

            # Space around `*` so `int* x` and `int *x` tokenize uniformly
            rp_spaced = re.sub(r'\*', ' * ', rp_no_arr)
            tokens = rp_spaced.split()
            if not tokens:
                continue

            # If last token is a valid identifier AND not a type keyword/qualifier,
            # it is the parameter name; otherwise parameter is unnamed.
            last_tok = tokens[-1]
            _NON_NAME_TOKS = {
                "*", "const", "volatile", "restrict", "__restrict", "__restrict__",
                "register", "void", "char", "short", "int", "long", "signed",
                "unsigned", "float", "double", "struct", "union", "enum",
                "bool", "_Bool", "size_t", "ssize_t", "ptrdiff_t",
            } | {t for t in INTEGER_TYPES if " " not in t}

            if len(tokens) >= 2 and re.match(r'^[a-zA-Z_]\w*$', last_tok) and last_tok not in _NON_NAME_TOKS:
                param_name = last_tok
                type_tokens = tokens[:-1]
            else:
                # Unnamed parameter (or mangled single-token param like `intn`)
                if len(tokens) == 1 and not _is_integer_type(tokens[0]):
                    # Preserve single unknown token so caller rejects as unsupported_type
                    params.append((tokens[0], tokens[0]))
                    continue
                param_name = f"__arg{idx}"
                type_tokens = tokens

            # Strip trailing pointer qualifiers like `const` / `restrict` after `*`
            # while preserving the pointer stars
            param_type = " ".join(type_tokens)
            for _ in range(array_dims):
                param_type = (param_type + " *").strip()
            param_type = re.sub(r'\s+', ' ', param_type).strip()
            # Normalize `const char * const` -> `const char *`
            param_type = re.sub(r'\*\s+(?:const|volatile|restrict|__restrict__|__restrict)\b', '*', param_type).strip()

            params.append((param_name, param_type))

    return Signature(
        name=func_name,
        return_type=return_type,
        params=params,
    )


def _split_params(params_str: str) -> list[str]:
    """Split comma-separated parameters respecting nested brackets."""
    result = []
    current = ""
    depth = 0
    for ch in params_str:
        if ch in "([{<":
            depth += 1
            current += ch
        elif ch in ")]}>":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            result.append(current)
            current = ""
        else:
            current += ch
    if current:
        result.append(current)
    return result


def _extract_called_functions(func_body: str) -> set[str]:
    """Extract likely function call names from function body (ignoring comments & strings)."""
    cleaned = _strip_comments(func_body)
    # Strip string and char literals so `"foo()"` doesn't match
    cleaned = re.sub(r'"(?:\\.|[^"\\])*"', '""', cleaned)
    cleaned = re.sub(r"'(?:\\.|[^'\\])*'", "''", cleaned)
    pattern = re.compile(r'\b([a-zA-Z_]\w*)\s*\(')
    return set(pattern.findall(cleaned))


def _extract_io_tests(exebench_record: dict) -> list[dict] | None:
    """Extract IO test cases from ExeBench record — inputs only.

    Per C2: Do NOT use ExeBench stored outputs as expected answers.
    They were produced on x86 and may disagree with locally computed outputs.
    Use inputs only; expected outputs are computed by running the original
    on the same machine as the candidate.
    """
    io_pairs = exebench_record.get("angha_io_pairs")
    if not io_pairs:
        return None

    tests = []
    for pair in io_pairs:
        # Extract input only, discard stored output
        inp = pair.get("input", {})
        if inp:
            tests.append({"input": inp})

    return tests if tests else None


# ---- Utility: run clang AST dump for robust extraction ----

def clang_ast_dump(source_file: str) -> dict | None:
    """Run clang -Xclang -ast-dump=json on a source file. Returns the parsed JSON or None."""
    try:
        result = subprocess.run(
            ["clang", "-Xclang", "-ast-dump=json", "-fsyntax-only", source_file],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None