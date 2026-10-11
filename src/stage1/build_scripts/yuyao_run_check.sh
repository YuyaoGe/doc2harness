
set +e
cd "{f0}" 2>/dev/null || { echo "YUYAO_RUN_ERR::no_f0"; exit 0; }
export PIP_DEFAULT_TIMEOUT=15 PIP_RETRIES=1 PIP_DISABLE_PIP_VERSION_CHECK=1
if [ -f install.sh ]; then
    timeout 240 bash install.sh >/tmp/_yuyao_install.log 2>&1
fi
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/local/sbin:$PATH"
hash -r 2>/dev/null
loc="$(command -v yuyao 2>/dev/null)"
if [ -n "$loc" ] && [ -x "$loc" ]; then
    echo "YUYAO_RUN_OK::$loc"
else
    echo "YUYAO_RUN_MISSING"
fi
