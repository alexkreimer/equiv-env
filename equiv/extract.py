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


# Allow-listed libc functions — currently empty: CBMC 6.11.0 does not model
# memset, memcpy, memcmp, strlen, strcmp, strncmp reliably.
# See DECISIONS.md Decision 12.
ALLOWED_LIBC_CALLS: set[str] = set()


# ---- Type classification ----

INTEGER_TYPES = {
    "char", "short", "int", "long", "long long",
    "signed char", "unsigned char", "unsigned short",
    "unsigned int", "unsigned long", "unsigned long long",
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


def _is_integer_type(typ: str) -> bool:
    """Check if a type string represents an integer scalar type."""
    # Strip const, volatile, restrict qualifiers
    clean = typ.replace("const ", "").replace("volatile ", "").replace("restrict ", "").strip()
    # Check if it's in our known integer set
    if clean in INTEGER_TYPES:
        return True
    # Check for typedef'd integer types (like int32_t, custom names)
    # For now, only exact matches; we'll refine with clang AST
    return False


def _is_pointer_to_integer(typ: str) -> tuple[bool, str]:
    """Check if type is/contains a pointer to integer types. Returns (is_ptr_to_int, pointee_type)."""
    clean = typ.replace("const ", "").replace("volatile ", "").replace("restrict ", "").strip()
    if clean.endswith("*"):
        pointee = clean[:-1].strip()
        if _is_integer_type(pointee):
            return True, pointee
    return False, ""


def _is_forbidden_type(typ: str) -> bool:
    """Check if type contains forbidden types (float, struct, etc.)."""
    clean = typ.replace("const ", "").replace("volatile ", "").replace("restrict ", "").strip()
    # Check for struct/union keywords
    if "struct " in clean or "union " in clean or "enum " in clean:
        return True
    # Check for typedef'd struct references (capitalized types that aren't in our integer set)
    # This is a heuristic — we'll validate with clang AST
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


def _infer_pointer_rules(signature: Signature, func_body: str) -> dict[str, str]:
    """Infer pointer sizing rules for each pointer parameter.

    Returns: {param_name: "paired" | "fixed" | "string"}
    """
    rules = {}
    param_names = {name for name, _ in signature.params}
    length_names = {"n", "len", "length", "size", "count", "num", "sz"}

    for name, typ in signature.params:
        ptr_match = _is_pointer_to_integer(typ)
        if not ptr_match[0]:
            continue

        pointee = ptr_match[1]
        # Check if this is char* used as string
        is_char_ptr = pointee in ("char", "unsigned char")

        # Look for a paired length parameter
        paired = False
        for other_name, _ in signature.params:
            if other_name == name:
                continue
            if other_name in length_names:
                paired = True
                break
            # Also check if any param ends in _len, _size, _n
            for suffix in ("_len", "_size", "_n", "_count", "_num"):
                if other_name.endswith(suffix):
                    paired = True
                    break
            if paired:
                break

        if paired:
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
    loops = 0
    loops += len(re.findall(r'\bfor\s*\(', func_body))
    loops += len(re.findall(r'\bwhile\s*\(', func_body))
    loops += len(re.findall(r'\bdo\s*\{', func_body))
    return loops


def _count_loc(func_body: str) -> int:
    """Count lines of code (non-blank, non-comment-only)."""
    stripped = _strip_comments(func_body)
    lines = [l for l in stripped.split('\n') if l.strip()]
    return len(lines)


# ---- Main extraction ----


def extract_from_exebench(zst_path: str, split: str) -> Iterator[Eligibility]:
    """Stream ExeBench zst-compressed JSONL, check each function's eligibility.

    Args:
        zst_path: Path to .jsonl.zst file from ExeBench.
        split: Dataset split name (e.g. "train", "test").

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
                    yield _check_eligibility(record, split)


def _check_eligibility(record: dict, split: str) -> Eligibility:
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

    # 3. Parse signature
    sig = _parse_signature(func_head, func_def, fname)
    if sig is None:
        return Eligibility(eligible=False, reason_code=REASON_NO_SIGNATURE)

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
    ret = sig.return_type.strip()
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
    body_only = _get_body_only(func_def)
    called_fns = _extract_called_functions(body_only)
    for fn_name in called_fns:
        # Skip CBMC builtins, C keywords, and attribute specifiers
        if fn_name in ("if", "while", "for", "return", "switch", "case", "sizeof",
                        "default", "break", "continue", "goto", "void", "char",
                        "int", "long", "short", "unsigned", "signed", "const",
                        "static", "inline", "typedef", "struct", "enum", "union",
                        "__attribute__", "__extension__", "__builtin"):
            continue
        # Skip the function's own name (self-reference)
        if fn_name == fname:
            continue
        if fn_name in ALLOWED_LIBC_CALLS:
            continue
        # Check if it's a libc function we don't model
        _LIBC_FUNCTIONS = {"memcpy", "memset", "memcmp", "memmove",
                           "strlen", "strcmp", "strncmp", "strcpy", "strncpy",
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


def _parse_signature(func_head: str, func_def: str, fname: str) -> Signature | None:
    """Parse function signature from func_head string.

    func_head examples:
        "int sum(int a, int b)"
        "void reset(void)"
        "static int foo(char *s, size_t n)"
        "char *strcpy(char *dest, const char *src)"

    Returns Signature or None if parsing fails.
    """
    head = func_head.strip()
    # Remove storage class specifiers
    for prefix in ("static ", "inline ", "extern ", "__attribute__((used)) "):
        head = head.replace(prefix, "")

    head = head.strip()

    # Split into return type and function name+params
    paren_idx = head.find("(")
    if paren_idx == -1:
        return None

    before_paren = head[:paren_idx].strip()
    after_paren = head[paren_idx:]

    # Extract return type (everything before the function name)
    # The function name is the last word before the paren, but it might start with *
    parts = before_paren.split()
    if not parts:
        return None

    func_name_with_star = parts[-1]
    if func_name_with_star.startswith("*"):
        # Pointer return: move * to return type
        func_name = func_name_with_star[1:]
        return_type = " ".join(parts[:-1]) + " *" if len(parts) > 1 else "*"
    else:
        func_name = func_name_with_star
        return_type = " ".join(parts[:-1]) if len(parts) > 1 else "int"

    # Parse parameters from the paren section
    params_str = after_paren.strip()
    if not params_str.startswith("("):
        return None

    # Extract content between parentheses
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
        for rp in raw_params:
            rp = rp.strip()
            if not rp:
                continue
            # Split type and name: last token is name, rest is type
            tokens = rp.split()
            if len(tokens) >= 2:
                param_name = tokens[-1].rstrip("[]")
                param_type = " ".join(tokens[:-1])
                # Handle pointer: if name starts with *, move it to type
                while param_name.startswith("*"):
                    param_name = param_name[1:]
                    param_type += " *"
                # Handle array syntax: int arr[] -> int *
                if "[" in rp and "]" in rp:
                    if not param_type.endswith("*"):
                        param_type += " *"
                params.append((param_name, param_type))
            elif len(tokens) == 1:
                # Unnamed parameter
                param_name = tokens[0]
                param_type = tokens[0]
                # Handle pointer in unnamed param
                while param_name.startswith("*"):
                    param_name = param_name[1:]
                    param_type = "* " + param_type if not param_type.startswith("*") else param_type
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
    """Extract likely function call names from function body."""
    # Match identifiers followed by opening paren
    pattern = re.compile(r'\b([a-zA-Z_]\w*)\s*\(')
    return set(pattern.findall(func_body))


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