
set -e
cd "{f0}"
ROOT="."
[ -d src ] && ROOT="src"
# Compile the sources, excluding the injected spec directory and caches: any .py syntax error exits non-zero.
python -m compileall -q -x '(\.d2a_spec|__pycache__|node_modules|\.git)' "$ROOT" >/dev/null
echo "COMPILE_OK"
# Import smoke test: edits must not break package import. The package is `yuyao`, the one
# the contract requires.
PYTHONPATH="$ROOT:${PYTHONPATH:-}" python -c "import yuyao" >/dev/null 2>&1 && echo "IMPORT_OK" || echo "IMPORT_FAIL"
