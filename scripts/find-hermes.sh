#!/usr/bin/env bash
# Locate an installed launcher without invoking it or changing the user's shell.
resolve_hermes() {
  local candidate checkout
  local -a candidates=()
  if [[ -n ${HERMES_BIN:-} ]]; then
    if [[ "$HERMES_BIN" != /* || ! -f "$HERMES_BIN" || ! -x "$HERMES_BIN" ]]; then
      printf 'HERMES_BIN must point to an existing executable using an absolute path: %s\n' \
        "$HERMES_BIN" >&2
      return 1
    fi
    printf '%s\n' "$HERMES_BIN"
    return 0
  fi
  candidate=$(type -P hermes || true)
  [[ -z "$candidate" ]] || candidates+=("$candidate")
  candidates+=("$HOME/.local/bin/hermes")
  if [[ -n ${HERMES_INSTALL_DIR:-} ]]; then
    candidates+=("$HERMES_INSTALL_DIR/.hermes/bin/hermes")
  fi
  for checkout in "${HERMES_HOME:-$HOME/.hermes}/hermes-agent" \
      "$HOME/.hermes/hermes-agent" "$HOME/hermes-agent"; do
    candidates+=("$checkout/.hermes/bin/hermes")
  done
  # Compatibility with older installations; prefer installed wrappers above.
  for checkout in "${HERMES_INSTALL_DIR:-$HOME/.hermes/hermes-agent}" \
      "${HERMES_HOME:-$HOME/.hermes}/hermes-agent" "$HOME/hermes-agent"; do
    candidates+=("$checkout/venv/bin/hermes" "$checkout/.venv/bin/hermes")
  done
  for candidate in "${candidates[@]}"; do
    if [[ "$candidate" == /* && -f "$candidate" && -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  cat >&2 <<'EOF'
Could not locate the Hermes launcher in PATH or the standard install locations.
This does not mean Hermes is uninstalled or setup is incomplete.
For a custom install, specify its installed executable, for example:
  HERMES_BIN=/opt/hermes/.hermes/bin/hermes bash scripts/install-ubuntu.sh
To inspect a command that works in your interactive shell, run: type -a hermes
EOF
  return 1
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  resolve_hermes
fi
