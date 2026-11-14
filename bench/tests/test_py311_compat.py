"""The bench tree must parse on the interpreter CI uses — **Python 3.11**
(``.github/workflows/ci.yml``, the bench job) — while the interpreter most
of us type into is 3.12.

PEP 701 (3.12) relaxed f-strings: a replacement field may now contain the
quote character that delimits the literal, a comment, or a newline. All
three parse happily here and are a ``SyntaxError`` in CI, where the
failure mode is the worst one available: the module cannot be imported,
so pytest aborts during **collection** and not one test runs. That is what
happened to PR #47, whose

    f"({'{{\"instances\": [...]}}' } required): {exc}"

inside a ``"``-delimited f-string is fine on 3.12 — and on ≤ 3.11 the
tokenizer ended the literal at the ``"`` before ``instances``, leaving
``unterminated string``.

:func:`ast.parse` with ``feature_version=(3, 11)`` is NOT the check: that
flag restricts the grammar only, the parser keeps the running
interpreter's f-string tokenizer, and it accepts the line above on 3.12
(verified). So the scan below reimplements the pre-3.11 rule — **the
literal ends at the first unescaped occurrence of its own quote run,
because ``{{`` escapes a brace and hides nothing** — and compares that
extent with where 3.12 says the literal really ended. Two extra rules
cover the other PEP 701 relaxations: a comment, or a newline in a
single-quoted literal, inside a replacement field.

Literals are taken from the running tokenizer, not from a line scan: a
line scan finds ``f"`` inside string literals, and a guard that reports
its own samples is a guard that gets deleted. On 3.11 itself a PEP 701
file never reaches the scan — ``ast.parse`` fails first, and that is
reported as a hit too, so this file says the same useful sentence on both
interpreters.
"""

import ast
import io
import pathlib
import re
import sys
import tokenize

_BENCH = pathlib.Path(__file__).resolve().parent.parent
_WORKFLOW = _BENCH.parent / ".github" / "workflows" / "ci.yml"

_NESTED_QUOTE = ("a quote run of the same kind as the f-string's own "
                 "inside a replacement field")
_COMMENT = "a comment inside a replacement field"
_NEWLINE = "a newline inside a single-quoted replacement field"

#: the line that broke CI on PR #47, as the source text of a module
CI_BREAKER = (
    "def describe(manifest_path, exc):\n"
    "    raise ValueError(\n"
    '        f"{manifest_path!r} is not a pinned instance manifest "\n'
    '        f"({\'{{"instances": [...]}}\' } required): {exc}")\n'
)

#: legal for every interpreter back to 3.8
LEGAL = [
    'x = f"{d[\'k\']}"\n',                        # the ordinary case
    'x = f"{\'{\'!r}"\n',                         # a brace in a nested str
    'x = f"{\'a\' if ok else \'b\'} {n:>3}"\n',
    'x = f"{{literal braces}} {v!r}"\n',
    'x = f"needs \\"quotes\\" {v}"\n',            # the fix that shipped
    'x = f"{f\'{inner}\'}"\n',                    # nested f-string
    'x = "not an f-string at all: {\\"q\\": 1}"\n',
    'x = rf"raw {v} \\ {w}"\n',
    'x = f"""{\n    v\n}"""\n',                   # multiline is fine here
    'x = f"""{"# not a comment"}"""\n',           # inside a nested string
    'x = f"""{"a"} and {b}"""\n',                 # lone quotes in triple
    'x = f"""{d["key"]}"""\n',                   # the pre-3.12 workaround:
    #   the literal ends at the whole run, so a lone " inside a triple-
    #   quoted f-string always worked — which is why the rule compares
    #   runs, not quote characters
]

#: parses on 3.12, is a SyntaxError on 3.11
ILLEGAL_BEFORE_3_12 = [
    CI_BREAKER,
    'x = f"{d["k"]}"\n',
    "x = f'{d['k']}'\n",
    'x = f"{ \'a"b\' }"\n',
    'x = f"""{ """x""" }"""\n',                  # a whole run inside
    'x = f"{x  # a comment\n}"\n',                # 3.12: the field may span
    'x = f"{\nx\n}"\n',
]


def _line_of(src: str, i: int) -> int:
    return src.count("\n", 0, i) + 1


def _literal_end_311(src: str, i: int, quotes: str) -> int:
    """Where a pre-3.11 tokenizer would have ENDED this literal: the
    first unescaped occurrence of its quote run. Braces are irrelevant to
    that scan — ``{{`` escapes a brace, it does not hide a quote — which
    is exactly why ``f"({{'{{'\"inst\"...``` died."""
    n = len(src)
    while i < n:
        if src[i] == "\\":          # an escape consumes the next char
            i += 2
            continue
        if src.startswith(quotes, i):
            return i
        i += 1
    return n


def _fields_312(src: str, i: int, end: int, triple: bool):
    """Yield ``(kind, pos)`` for constructs PEP 701 added inside
    replacement fields: ``"comment"`` / ``"newline"`` (the latter only
    when the literal is single-quoted). ``in_str`` keeps a ``#`` inside a
    nested string from looking like a comment."""
    depth = 0
    in_str = None
    while i < end:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if depth == 0:
            if c == "{":
                if src[i + 1:i + 2] == "{":
                    i += 2
                    continue
                depth = 1
            elif c == "}":
                if src[i + 1:i + 2] == "}":
                    i += 2
            i += 1
            continue
        if in_str:
            if c == "\n":
                return              # a broken nested string: not our rule
            if src.startswith(in_str, i):
                step = len(in_str)
                in_str = None
                i += step
                continue
            i += 1
            continue
        if c in ("'", '"'):
            run = src[i:i + 3]
            in_str = run if run in ('"""', "'''") else c
            i += len(in_str)
            continue
        if c == "#":
            yield ("comment", i)
        elif c == "\n" and not triple:
            yield ("newline", i)
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1


def _fstrings(src: str):
    """``(body, close_at, quote_run)`` per f-string literal — 3.12's
    answer: the offset just past the opening quotes, and where the
    closing quote run starts.
    ``None`` when the tokenizer has no f-string tokens (≤ 3.11), where a
    PEP 701 file cannot parse at all and the scan has nothing to find."""
    start_tok = getattr(tokenize, "FSTRING_START", None)
    end_tok = getattr(tokenize, "FSTRING_END", None)
    if start_tok is None or end_tok is None:
        return None
    lines = src.splitlines(keepends=True)
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln))
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []                     # ast.parse already reported it

    def pos(t):
        return offsets[t.start[0] - 1] + t.start[1]

    out = []
    # a stack: PEP 701 also lets an f-string sit inside a replacement
    # field, and each of those is a literal with an extent of its own
    stack = []
    for t in toks:
        if t.type == start_tok:
            # t.string is the prefix AND the opening run: f" / rf""" —
            # the body starts after both of them
            text = t.string.lstrip("rRfFuUbB")
            stack.append((pos(t) + len(t.string),
                          text[:3] if text[:3] in ('"""', "'''")
                          else text[:1]))
        elif t.type == end_tok and stack:
            # pair with the innermost open literal, and record where its
            # CLOSING run starts: 3.12's own answer to the question the
            # pre-3.11 tokenizer answers differently
            b, quotes = stack.pop()
            out.append((b, pos(t), quotes))
    return out


def pep701_hits(src: str, path: str = "<string>") -> "list[str]":
    """Constructs in ``src`` that a pre-3.12 interpreter cannot parse."""
    try:
        ast.parse(src)
    except SyntaxError as exc:
        where = f"{path}:{exc.lineno}" if exc.lineno else path
        return [f"{where}: {exc.msg}"]
    literals = _fstrings(src)
    if literals is None:
        return []
    hits = []
    for body, close_at, quotes in literals:
        short = _literal_end_311(src, body, quotes)
        if short < close_at:
            hits.append(
                f"{path}:{_line_of(src, short)}: PEP 701 (3.12 only): "
                f"{_NESTED_QUOTE}: {src[short:short + 30]!r}")
            continue
        for kind, at in _fields_312(src, body, close_at,
                                    len(quotes) == 3):
            what = _COMMENT if kind == "comment" else _NEWLINE
            hits.append(f"{path}:{_line_of(src, at)}: PEP 701 "
                        f"(3.12 only): {what}")
    return hits


def _ci_python_version():
    """The pinned version in the workflow (no yaml import: the bench venv
    does not ship PyYAML)."""
    if not _WORKFLOW.is_file():
        return None
    for line in _WORKFLOW.read_text(encoding="utf-8").splitlines():
        m = re.search(r"python-version:\s*[\"']?(\d+)\.(\d+)", line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


# --------------------------------------------------------------------------
# the guard itself
# --------------------------------------------------------------------------

def test_the_guard_catches_what_3_11_cannot_parse():
    """A guard that quietly stopped detecting is worse than none: what it
    prevents is a collection error that hides every other test."""
    for sample in ILLEGAL_BEFORE_3_12:
        assert pep701_hits(sample), f"NOT CAUGHT: {sample!r}"


def test_the_guard_lets_what_3_11_allows_through():
    for sample in LEGAL:
        assert pep701_hits(sample) == [], pep701_hits(sample)


def test_the_guard_finds_the_line_that_broke_ci():
    """End to end, straight out of git when the history still holds it:
    the file that broke CI must be reported, and the file that fixed it
    must not. Skipped elsewhere (a squash merge rewrites these commits)
    — the synthetic breaker above is the part that always runs."""
    import pytest
    breaker = _git_show("HEAD~1:bench/scripts/verify_submission.py")
    if breaker is None or 'f"({' not in breaker:
        pytest.skip("the commit that carried the breaker is not in this "
                    "history")
    hits = pep701_hits(breaker)
    assert any("PEP 701" in h for h in hits), hits
    fixed = _git_show("HEAD:bench/scripts/verify_submission.py")
    if fixed is not None:
        assert pep701_hits(fixed) == []


def _git_show(rev: str):
    import subprocess
    r = subprocess.run(["git", "show", rev], cwd=str(_BENCH.parent),
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def test_every_bench_module_parses_on_the_ci_interpreter():
    bad = []
    for path in sorted(_BENCH.rglob("*.py")):
        bad += pep701_hits(path.read_text(encoding="utf-8"), str(path))
    assert not bad, "\n".join(bad)


def test_the_ci_pin_is_still_this_files_premise():
    """If CI moves to 3.12 the guard's premise changes; say so rather
    than keep asserting a rule nobody needs any more."""
    v = _ci_python_version()
    assert v is not None, "ci.yml no longer pins python-version"
    if v >= (3, 12):
        import pytest
        pytest.skip(f"CI runs python {v}: PEP 701 is available there, "
                    "this guard can be retired")
    assert sys.version_info[:2] >= v, (
        f"running on {sys.version_info[:2]}, CI is {v}: the scan cannot "
        "see what CI sees")
