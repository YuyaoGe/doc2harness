
import ast, os, re
root = "{f0}"
GROUPS = [
    ["/undo"], ["/redo"], ["/rewind","/checkpoint"], ["/compact","/compress"], ["/plan"], ["/ask"],
    ["/debug"], ["/mode"], ["/role"], ["/diff"], ["/review"], ["/security-review"], ["/run"], ["/copy"],
    ["/focus"], ["/tui"], ["/config","/settings"], ["/background","/bg"], ["/init"], ["/memory"], ["/tools"],
    ["/recap"], ["/context"], ["/model"], ["/new"], ["/clear","/reset"], ["/sessions","/resume"],
    ["/title","/rename"], ["/status"], ["/export"], ["/add-dir"], ["/cd"], ["/permissions"], ["/hooks"],
    ["/mcp"], ["/agents"], ["/help"], ["/exit","/quit"],
    ["/goal"], ["/loop"], ["/fork"], ["/branch"], ["/btw","/side"], ["/verify"], ["/batch"],
    ["/workflows","/workflow"], ["/skills","/skill"], ["/flow:"], ["/queue"], ["/tasks","/bashes"],
    ["/think"], ["/show-thinking"], ["/stream"], ["/usage"],
]
SKIP = {".d2a_spec", "__pycache__", ".git", "node_modules", ".venv", "minisweagent", ".pytest_cache", ".mypy_cache"}

# Two views of the same sources.
#   code_blob   -- comments and docstrings removed, so a command named only in
#                  prose does not count as implemented.  This is the load-bearing
#                  one: scanning raw text would let a product whose entire content
#                  is a help-text docstring listing every command score zero
#                  missing, and `all_commands_present` is one of the checks the
#                  basic gate requires.
#   prose_blob  -- the removed material, reported separately.  A command that
#                  appears only there is named but not dispatched; saying so is
#                  more useful than a bare "missing".
code_chunks, prose_chunks = [], []


def split_code_and_prose(text):
    """Return (code, prose) for one module: prose is comments plus docstrings.

    Falls back to treating everything as code when the file does not parse -- a
    syntax error is caught by source_compiles, and guessing here could only
    manufacture a false missing.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text, ""

    prose = []
    # Docstrings: the string expression leading a module, class or function body.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            prose.append(first.value.value)

    lines = text.splitlines(keepends=True)
    # Blank out docstring spans and comments, leaving line structure intact so a
    # later regex cannot accidentally join two unrelated lines.
    blanked = list(lines)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            continue
        start = getattr(first, "lineno", None)
        end = getattr(first, "end_lineno", None)
        if start is None or end is None:
            continue
        for index in range(start - 1, min(end, len(blanked))):
            blanked[index] = "\n"

    code = []
    for line in blanked:
        # Strip trailing comments, but only when the '#' is not inside a string.
        # Counting unescaped quotes is enough here: the goal is to avoid cutting a
        # literal such as "/tag#1", not to reimplement the tokenizer.
        cut = None
        in_single = in_double = False
        index = 0
        while index < len(line):
            char = line[index]
            if char == "\\":
                index += 2
                continue
            if char == "'" and not in_double:
                in_single = not in_single
            elif char == '"' and not in_single:
                in_double = not in_double
            elif char == "#" and not in_single and not in_double:
                cut = index
                break
            index += 1
        if cut is not None:
            prose.append(line[cut:])
            line = line[:cut] + "\n"
        code.append(line)
    return "".join(code), "\n".join(prose)


for dp, dns, fns in os.walk(root):
    dns[:] = [d for d in dns if d not in SKIP]
    for fn in fns:
        if fn.endswith(".py"):
            try:
                with open(os.path.join(dp, fn), encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except Exception:
                continue
            code, prose = split_code_and_prose(text)
            code_chunks.append(code)
            prose_chunks.append(prose)

blob = "\n".join(code_chunks)
prose_blob = "\n".join(prose_chunks)
missing = []
prose_only = []
for g in GROUPS:
    present = False
    named_in_prose = False
    for tok in g:
        # A prefix command such as `/flow:` is written `/flow:<name>` in real
        # use, so the token is followed by a word character rather than
        # terminated.  Applying the "not followed by a word character" lookahead
        # to it asks for the one form that never appears in working code.
        prefix_form = tok.endswith(":")
        literal = re.escape(tok) if prefix_form else re.escape(tok) + r"(?![\w-])"

        # (a) Literal `/cmd`: enumerated implementations (help text held in a data
        #     structure, elif cmd == "/x", handler-table keys that include the slash).
        if re.search(literal, blob):
            present = True
            break
        # (b) A `cmd_<name>` handler definition or reference: name-dispatched
        #     implementations strip the leading '/' and build `cmd_<name>`, so the
        #     literal `/cmd` may never appear in the source, yet `def cmd_recap`
        #     means the command is genuinely reachable.
        #     name = drop the leading '/', drop a trailing ':', map '-' to '_'.
        #     Strong signal with near-zero false positives: the `cmd_` prefix is a
        #     command-only convention.
        #     The leading boundary must also admit `_cmd_flow`: `\b` does not match
        #     between '_' and 'c' because '_' is a word character, so a private
        #     handler -- an extremely common way to write these -- would go undetected.
        name = tok[1:].rstrip(":").replace("-", "_")
        if name and re.search(r"(?:\b|_)cmd_" + re.escape(name) + r"\b", blob):
            present = True
            break
        # (c) Prefix commands are also dispatched by testing the bare prefix
        #     against the command name, with the slash already stripped:
        #     `if name.startswith("flow:")`.  That is a real implementation and
        #     carries no slash, so neither (a) nor (b) can see it.
        if prefix_form and re.search(
                r"""["']""" + re.escape(tok[1:]) + r"""["']""", blob):
            present = True
            break
        if re.search(literal, prose_blob):
            named_in_prose = True
    if not present:
        missing.append(g[0])
        if named_in_prose:
            prose_only.append(g[0])
print("MISSING_COUNT=%d" % len(missing))
for m in missing:
    print("MISSING::" + m)
# Reported so a build's hint can say "documented but not dispatched" rather than
# "missing", which is a different and more actionable defect.
for m in prose_only:
    print("PROSE_ONLY::" + m)
