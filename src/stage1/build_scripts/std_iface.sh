
cd "{f0}"
ROOT="."
[ -d src ] && ROOT="src"
export PYTHONPATH="$ROOT:${PYTHONPATH:-}"
export D2A_MODEL_BASE_URL="${D2A_MODEL_BASE_URL:-http://127.0.0.1:1}"
export D2A_MODEL_NAME="${D2A_MODEL_NAME:-litellm_proxy/mock}"
export D2A_MODEL_API_KEY="${D2A_MODEL_API_KEY:-sk-x}"
export MSWEA_SILENT_STARTUP=1
python - <<'PYEOF'
import importlib
ok = False; detail = ""
for mn in ("yuyao.agent_api", "yuyao.d2a_api", "yuyao.harness_api", "agent_api"):
    try:
        m = importlib.import_module(mn)
    except Exception as e:
        detail = "import %s: %r" % (mn, e); continue
    f = getattr(m, "create_session", None)
    if not callable(f):
        detail = "%s.create_session not callable" % mn; continue
    try:
        s = f()
    except Exception as e:
        detail = "%s.create_session() raised; must be lazy and not contact the model at construction time: %r" % (mn, e); continue
    miss = []
    for a in ("dispatch", "chat"):
        if not callable(getattr(s, a, None)): miss.append(a)
    try:
        str(s.cwd)
    except Exception:
        miss.append("cwd")
    if not isinstance(getattr(s, "messages", None), list): miss.append("messages")
    if miss:
        detail = "%s session is missing required members: %s" % (mn, ",".join(miss)); continue
    ok = True; break
print("STD_IFACE_OK" if ok else ("STD_IFACE_FAIL " + (detail or "no importable yuyao.agent_api.create_session")[:300]))
PYEOF
