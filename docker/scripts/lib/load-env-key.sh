# Source from bash: load_env_key KEY /path/to/.env
# Loads a single KEY=value from .env without sourcing the whole file.
load_env_key() {
  local key="$1" file="$2" line val
  line="$(grep -E "^${key}=" "$file" 2>/dev/null | tail -1 || true)"
  [[ -n "$line" ]] || return 0
  val="${line#*=}"
  val="${val%\"}"; val="${val#\"}"
  val="${val%\'}"; val="${val#\'}"
  export "$key=$val"
}
