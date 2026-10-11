
import re
def _read(p):
    try:
        return open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return ""
ok = False
t = _read("pyproject.toml")
for sec in (r"\[project\.scripts\]", r"\[tool\.poetry\.scripts\]"):
    m = re.search(sec + r"(.*?)(\n\[|\Z)", t, re.S)
    if m and re.search(r"(?m)^\s*[\"']?yuyao[\"']?\s*[=:]", m.group(1)):
        ok = True
if re.search(r"(?m)^\s*yuyao\s*=", _read("setup.cfg")):
    ok = True
for p in ("setup.py", "install.sh"):
    s = _read(p)
    if re.search(r"(console_scripts.*yuyao|/bin/yuyao|yuyao[\"']?\s*=)", s, re.S):
        ok = True
print("YUYAO_STATIC_OK" if ok else "YUYAO_STATIC_MISSING")
