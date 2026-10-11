
import os, re
root = "{f0}"
pat = re.compile(
    r"not\s+(yet\s+)?(fully\s+)?implemented"          # not implemented / not yet implemented / not fully implemented
    r"|notimplementederror|not_implemented|to\s+be\s+implemented"
    r"|would\s+process\s+your\s+request"
    r"|would\s+run\s+in\s+(the\s+)?background"          # stub: "Session ... would run in background"
    r"|would\s+be\s+(run|created|executed|spawned|dispatched)"
    r"|placeholder\s+response|i'm\s+a\s+placeholder|coming\s+soon"
    r"|mock(ed)?\s+(response|data|reply)|fake\s+(response|data|reply)|dummy\s+(response|data|reply)"
    r"|no\s+(critical\s+|serious\s+)?(security\s+)?issues?\s+(detected|found)",  # hardcoded fake review verdict
    re.I)
SKIP = {".d2a_spec", "__pycache__", ".git", "node_modules", ".venv", "minisweagent", ".pytest_cache", ".mypy_cache", "tests", "test"}
hits = []
for dp, dns, fns in os.walk(root):
    dns[:] = [d for d in dns if d not in SKIP]
    for fn in fns:
        if not fn.endswith(".py"):
            continue
        p = os.path.join(dp, fn)
        try:
            for i, line in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                s = line.strip()
                if s.startswith("#"):  # skip pure comment lines to reduce false positives
                    continue
                if pat.search(line):
                    hits.append("%s:%d: %s" % (os.path.relpath(p, root), i, s[:120]))
        except Exception:
            pass
print("STUB_COUNT=%d" % len(hits))
for h in hits[:40]:
    print("STUB::" + h)
