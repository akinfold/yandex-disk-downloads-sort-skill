#!/bin/bash
#
# Installer for the yandex-disk-downloads-sort agent skill.
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/akinfold/yandex-disk-downloads-sort-skill/HEAD/install.sh)"
#
# It clones (or updates) the repository, links the skill into the places Claude Code,
# Codex, Cursor and other Agent Skills clients look, asks for a Yandex Disk OAuth token,
# stores it in ~/.yandex-disk-token with owner-only permissions, and points Claude Code
# at that file. Nothing is installed system-wide and no sudo is used.
#
# Every step says what it is about to do, refuses to clobber anything it did not create,
# and can be re-run safely.
#
# Environment variables (all optional):
#   YADISK_SKILL_DIR    where to keep the checkout (default: ~/.local/share/yandex-disk-downloads-sort-skill)
#   YANDEX_DISK_TOKEN   supply the token instead of being asked (for unattended installs)
#   YADISK_TOKEN_FILE   where to write the token (default: ~/.yandex-disk-token)
#   NONINTERACTIVE=1    never prompt; skip anything that would need an answer
#   YADISK_NO_SETTINGS=1  do not touch ~/.claude/settings.json

set -u

# Defined first, and with single brackets, so that a shell which cannot run the rest of
# this file still reaches a readable message instead of a syntax error.
abort() { printf "%serror:%s %s\n" "${RED:-}${BOLD:-}" "${RESET:-}" "$*" >&2; exit 1; }

if [ -z "${BASH_VERSION:-}" ]; then
  abort "This installer needs bash. Run it as: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/akinfold/yandex-disk-downloads-sort-skill/HEAD/install.sh)\""
fi

if [ -n "${POSIXLY_CORRECT+1}" ]; then
  abort "Bash is in POSIX mode. Unset POSIXLY_CORRECT and try again."
fi

# The whole installer lives in main(), invoked on the last line. If the download is cut
# short, bash never reaches that line, so a partial script cannot half-install anything.
main() {
  REPO_SLUG="akinfold/yandex-disk-downloads-sort-skill"
  REPO_URL="https://github.com/${REPO_SLUG}.git"
  SKILL_NAME="yandex-disk-downloads-sort"
  POLIGON_URL="https://yandex.ru/dev/disk/poligon/"
  REVOKE_URL="https://id.yandex.ru/personal/data-access"
  API_URL="https://cloud-api.yandex.net/v1/disk"

  SKILL_DIR="${YADISK_SKILL_DIR:-${HOME}/.local/share/yandex-disk-downloads-sort-skill}"
  TOKEN_FILE="${YADISK_TOKEN_FILE:-${HOME}/.yandex-disk-token}"
  SETTINGS_FILE="${HOME}/.claude/settings.json"
  NONINTERACTIVE="${NONINTERACTIVE:-}"
  [ -n "${CI:-}" ] && NONINTERACTIVE=1

  setup_output
  parse_args "$@"

  cat <<EOF

  ${BOLD}yandex-disk-downloads-sort${RESET} — analyze and sort your Yandex Disk Downloads folder

  This installer will:
    1. put a checkout of ${REPO_SLUG} in ${SKILL_DIR}
    2. link the skill into ~/.claude/skills and ~/.agents/skills
    3. ask you for a Yandex Disk OAuth token and save it to ${TOKEN_FILE} (mode 600)
    4. point Claude Code at that file via ~/.claude/settings.json

  It needs no sudo, touches nothing outside your home directory, and is safe to re-run.

EOF

  check_prerequisites
  fetch_repo
  link_skill "${HOME}/.claude/skills"
  link_skill "${HOME}/.agents/skills"
  install_token
  wire_settings
  print_next_steps
}

# -- output helpers -----------------------------------------------------------

setup_output() {
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$(printf '\033[1m'); RESET=$(printf '\033[0m')
    BLUE=$(printf '\033[34m'); GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m'); RED=$(printf '\033[31m')
  else
    BOLD=""; RESET=""; BLUE=""; GREEN=""; YELLOW=""; RED=""
  fi
}

ohai() { printf "%s==>%s %s\n" "${BLUE}${BOLD}" "${RESET}" "$*"; }
ok()   { printf "    %sok%s %s\n" "${GREEN}" "${RESET}" "$*"; }
info() { printf "    %s\n" "$*"; }
warn() { printf "%swarning:%s %s\n" "${YELLOW}${BOLD}" "${RESET}" "$*" >&2; }

usage() {
  cat <<EOF
Usage: install.sh [--help] [--no-token] [--no-settings] [--dir PATH]

  --help          show this message
  --no-token      install the skill but do not ask for or write a token
  --no-settings   do not add YANDEX_DISK_TOKEN_FILE to ~/.claude/settings.json
  --dir PATH      keep the checkout at PATH (default ~/.local/share/yandex-disk-downloads-sort-skill)

See https://github.com/${REPO_SLUG} for what the skill does.
EOF
}

parse_args() {
  SKIP_TOKEN=""
  [ -n "${YADISK_NO_SETTINGS:-}" ] && SKIP_SETTINGS=1 || SKIP_SETTINGS=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --help|-h) usage; exit 0 ;;
      --no-token) SKIP_TOKEN=1 ;;
      --no-settings) SKIP_SETTINGS=1 ;;
      --dir) shift; [ "$#" -gt 0 ] || abort "--dir needs a path"; SKILL_DIR="$1" ;;
      *) abort "unknown option: $1 (try --help)" ;;
    esac
    shift
  done
}

have() { command -v "$1" >/dev/null 2>&1; }

# -- prerequisites ------------------------------------------------------------

check_prerequisites() {
  ohai "Checking prerequisites"

  have git || abort "git is required. Install the Xcode Command Line Tools (xcode-select --install) or your distribution's git package."
  ok "git $(git --version 2>/dev/null | awk '{print $3}')"

  have curl || abort "curl is required."
  ok "curl"

  if have python3 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    ok "python3 $(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null)"
  else
    warn "python3 3.9+ was not found. The skill's scripts need it at run time; the install will continue."
  fi
}

# -- repository ---------------------------------------------------------------

fetch_repo() {
  ohai "Fetching the skill"
  if [ -d "${SKILL_DIR}/.git" ]; then
    info "updating ${SKILL_DIR}"
    if git -C "${SKILL_DIR}" pull --ff-only --quiet 2>/dev/null; then
      ok "updated to $(git -C "${SKILL_DIR}" rev-parse --short HEAD)"
    else
      warn "could not fast-forward ${SKILL_DIR} (local changes?); using it as it is"
    fi
  elif [ -e "${SKILL_DIR}" ]; then
    abort "${SKILL_DIR} exists but is not a git checkout. Move it aside, or pass --dir PATH."
  else
    info "cloning ${REPO_URL}"
    mkdir -p "$(dirname "${SKILL_DIR}")" || abort "cannot create $(dirname "${SKILL_DIR}")"
    if ! git clone --quiet --depth 1 "${REPO_URL}" "${SKILL_DIR}"; then
      warn "clone failed; retrying once in 3 seconds"
      rm -rf "${SKILL_DIR}"
      sleep 3
      git clone --quiet --depth 1 "${REPO_URL}" "${SKILL_DIR}" ||
        abort "git clone failed. Check your network, or clone ${REPO_URL} by hand and re-run with --dir PATH."
    fi
    ok "cloned into ${SKILL_DIR}"
  fi

  SKILL_SOURCE="${SKILL_DIR}/skills/${SKILL_NAME}"
  [ -f "${SKILL_SOURCE}/SKILL.md" ] || abort "${SKILL_SOURCE}/SKILL.md is missing — the checkout looks wrong."
}

# -- links --------------------------------------------------------------------

link_skill() {
  parent="$1"
  target="${parent}/${SKILL_NAME}"
  ohai "Linking into ${parent}"

  mkdir -p "${parent}" || { warn "cannot create ${parent}; skipping"; return 0; }

  if [ -L "${target}" ]; then
    current=$(readlink "${target}")
    if [ "${current}" = "${SKILL_SOURCE}" ]; then
      ok "already linked"
    else
      warn "${target} is a link to ${current}; leaving it alone"
    fi
    return 0
  fi

  if [ -e "${target}" ]; then
    warn "${target} already exists and is not a link; leaving it alone"
    return 0
  fi

  if ln -s "${SKILL_SOURCE}" "${target}"; then
    ok "${target} -> ${SKILL_SOURCE}"
  else
    warn "could not create ${target}"
  fi
}

# -- token --------------------------------------------------------------------

print_token_instructions() {
  cat <<EOF

  ${BOLD}How to get a Yandex Disk OAuth token${RESET} (about two minutes)

    1. In your browser, sign in to the Yandex account whose Disk you want to sort,
       then open:

         ${BOLD}${POLIGON_URL}${RESET}

       This is Yandex's own API console. The page is in Russian.

    2. Near the top there is an input field labelled "Ваш OAuth-токен"
       ("Your OAuth token") with a yellow button next to it labelled
       "Получить OAuth-токен" ("Get OAuth token"). ${BOLD}Click that yellow button.${RESET}

    3. Sign in if you are asked to, then click "Разрешить" ("Allow") on the
       consent screen that lists the permissions.

    4. You land back on the console and the token is now filled into the
       "Ваш OAuth-токен" field. Select it and copy it. It looks like
       ${BOLD}y0_AgAAAAA...${RESET} — about 58 characters, letters, digits, "_" and "-".

  The token grants full read and write access to your Disk. This installer stores it
  only on this machine, in ${TOKEN_FILE}, readable by you alone. Revoke it any time at
  ${REVOKE_URL}

EOF
}

install_token() {
  ohai "Setting up the token"

  if [ -n "${SKIP_TOKEN}" ]; then
    info "skipped (--no-token); write the token to ${TOKEN_FILE} yourself when you are ready"
    return 0
  fi

  token="${YANDEX_DISK_TOKEN:-}"

  if [ -z "${token}" ] && [ -s "${TOKEN_FILE}" ]; then
    if [ -n "${NONINTERACTIVE}" ] || ! can_prompt; then
      ok "${TOKEN_FILE} already has a token; keeping it"
      return 0
    fi
    printf "    %s already contains a token. Replace it? [y/N] " "${TOKEN_FILE}" > /dev/tty
    IFS= read -r answer < /dev/tty || answer=""
    case "${answer}" in
      y|Y|yes|YES) : ;;
      *) ok "keeping the existing token"; return 0 ;;
    esac
  fi

  if [ -z "${token}" ]; then
    if [ -n "${NONINTERACTIVE}" ] || ! can_prompt; then
      warn "no terminal to ask for a token"
      info "get one as described at ${POLIGON_URL}, then run:"
      info "  printf '%s' 'y0_your_token' > ${TOKEN_FILE} && chmod 600 ${TOKEN_FILE}"
      info "or re-run this installer with YANDEX_DISK_TOKEN=y0_... in the environment"
      return 0
    fi
    print_token_instructions
    printf "  Paste the token and press RETURN (it stays hidden), or press RETURN to skip:\n  > " > /dev/tty
    # read -s turns echo off; a Ctrl-C in the middle would otherwise leave the terminal
    # silent for every command the user types afterwards.
    stty_state=$(stty -g < /dev/tty 2>/dev/null || true)
    trap 'restore_tty "${stty_state}"; printf "\n" > /dev/tty 2>/dev/null; exit 130' INT TERM
    IFS= read -rs token < /dev/tty || token=""
    trap - INT TERM
    restore_tty "${stty_state}"
    printf "\n" > /dev/tty
  fi

  # Strip a stray carriage return or surrounding whitespace from the paste.
  token=$(printf '%s' "${token}" | tr -d '\r\n\t ' )

  if [ -z "${token}" ]; then
    warn "no token entered; the skill is installed but cannot talk to Yandex yet"
    info "when you have one: printf '%s' 'y0_...' > ${TOKEN_FILE} && chmod 600 ${TOKEN_FILE}"
    return 0
  fi

  info "got $(mask_token "${token}")"
  check_token_shape "${token}"

  verify_token "${token}"
  write_token "${token}"
}

# /dev/tty can exist with the right permissions and still fail to open when the process
# has no controlling terminal (cron, a container without -t, a pipeline in CI), so the
# only reliable test is to open it.
# Enough of the value to recognise a mis-paste, not enough to be worth shoulder-surfing.
mask_token() {
  printf '%s' "$1" | awk '{ n = length($0); if (n <= 8) { print "a " n "-character value" } else { printf "%s...%s (%d characters)\n", substr($0, 1, 4), substr($0, n - 2), n } }'
}

# Glob ranges follow the collation order of the current locale, so the check runs under
# LC_ALL=C where [![:graph:]] means exactly "not a printable ASCII character".
check_token_shape() {
  ( LC_ALL=C
    case "$1" in
      *[![:graph:]]*)
        printf "%swarning:%s the token contains a space or a control character; Yandex tokens do not\n" "${YELLOW}${BOLD}" "${RESET}" >&2 ;;
      y[0-3]_*) : ;;
      *)
        printf "%swarning:%s that does not look like a Yandex OAuth token (they start with y0_); trying it anyway\n" "${YELLOW}${BOLD}" "${RESET}" >&2 ;;
    esac )
}

restore_tty() {
  [ -n "${1:-}" ] || return 0
  stty "$1" < /dev/tty 2>/dev/null || stty echo < /dev/tty 2>/dev/null || true
}

can_prompt() {
  [ -z "${NONINTERACTIVE}" ] || return 1
  { true < /dev/tty; } >/dev/null 2>&1 || return 1
  { true > /dev/tty; } >/dev/null 2>&1 || return 1
  return 0
}

# Verify the token without ever putting it on a command line, where `ps` would show it.
verify_token() {
  t="$1"
  info "checking the token against ${API_URL}"
  headers=$(mktemp) || { warn "cannot create a temp file; skipping the check"; return 0; }
  chmod 600 "${headers}" 2>/dev/null
  printf 'header = "Authorization: OAuth %s"\n' "${t}" > "${headers}"

  body=$(curl -fsS --max-time 20 --config "${headers}" "${API_URL}" 2>/dev/null)
  status=$?
  rm -f "${headers}"

  if [ "${status}" -ne 0 ]; then
    warn "Yandex did not accept the token (or the network is down). It will be saved anyway."
    info "a 401 means the token is wrong, expired or revoked — get a fresh one at ${POLIGON_URL}"
    return 0
  fi

  login=$(printf '%s' "${body}" | extract_json_field "login")
  free=$(printf '%s' "${body}" | free_space)
  if [ -n "${login}" ]; then
    ok "token works — signed in as ${BOLD}${login}${RESET}${free}"
  else
    ok "token works"
  fi
}

extract_json_field() {
  field="$1"
  if have python3; then
    python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
user = data.get("user") or {}
sys.stdout.write(str(user.get(sys.argv[1]) or ""))
' "${field}" 2>/dev/null
  else
    sed -n 's/.*"'"${field}"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
  fi
}

free_space() {
  if have python3; then
    python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
total, used = d.get("total_space"), d.get("used_space")
if isinstance(total, int) and isinstance(used, int):
    sys.stdout.write(", %.1f GB free of %.1f GB" % ((total - used) / 2**30, total / 2**30))
' 2>/dev/null
  fi
}

write_token() {
  # umask makes the file 0600 the moment it is created; chmod afterwards would leave a
  # window in which the token is world-readable.
  ( umask 077; printf '%s' "$1" > "${TOKEN_FILE}" ) || abort "cannot write ${TOKEN_FILE}"
  chmod 600 "${TOKEN_FILE}" 2>/dev/null
  ok "token saved to ${TOKEN_FILE} (mode $(file_mode "${TOKEN_FILE}"))"
}

file_mode() {
  stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1" 2>/dev/null || echo "600"
}

# -- Claude Code settings -----------------------------------------------------

wire_settings() {
  ohai "Pointing Claude Code at the token"

  if [ -n "${SKIP_SETTINGS}" ]; then
    info "skipped (--no-settings)"
    return 0
  fi

  mkdir -p "$(dirname "${SETTINGS_FILE}")" 2>/dev/null
  [ -f "${SETTINGS_FILE}" ] || printf '{}\n' > "${SETTINGS_FILE}"

  if have python3 && python3 -c 'import json' >/dev/null 2>&1; then
    if python3 - "${SETTINGS_FILE}" "${TOKEN_FILE}" <<'PY'
import json, os, shutil, sys, tempfile, time

path, token_file = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as handle:
        settings = json.load(handle)
except (OSError, ValueError):
    sys.exit(2)
if not isinstance(settings, dict):
    sys.exit(2)

env = settings.get("env")
if not isinstance(env, dict):
    env = {}
if env.get("YANDEX_DISK_TOKEN_FILE") == token_file:
    sys.exit(3)  # nothing to change, so nothing to back up either
env["YANDEX_DISK_TOKEN_FILE"] = token_file
settings["env"] = env

shutil.copy2(path, path + time.strftime(".bak-%Y%m%d-%H%M%S"))

# Write beside the original, then rename: a crash cannot leave a truncated settings file.
directory = os.path.dirname(os.path.abspath(path))
handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False)
try:
    json.dump(settings, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
    handle.close()
    os.replace(handle.name, path)
except BaseException:
    os.unlink(handle.name)
    raise
PY
    then
      ok "added env.YANDEX_DISK_TOKEN_FILE to ${SETTINGS_FILE}"
    else
      case "$?" in
        3) ok "already set in ${SETTINGS_FILE}" ;;
        *) warn "could not edit ${SETTINGS_FILE} automatically"; print_settings_snippet ;;
      esac
    fi
  elif have jq; then
    if [ "$(jq -r --arg f "${TOKEN_FILE}" '.env.YANDEX_DISK_TOKEN_FILE == $f' "${SETTINGS_FILE}" 2>/dev/null)" = "true" ]; then
      ok "already set in ${SETTINGS_FILE}"
      return 0
    fi
    tmp="${SETTINGS_FILE}.tmp.$$"
    if jq --arg f "${TOKEN_FILE}" '.env = ((.env // {}) + {YANDEX_DISK_TOKEN_FILE: $f})' \
        "${SETTINGS_FILE}" > "${tmp}" 2>/dev/null; then
      cp -p "${SETTINGS_FILE}" "${SETTINGS_FILE}.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null
      mv "${tmp}" "${SETTINGS_FILE}" && ok "added env.YANDEX_DISK_TOKEN_FILE to ${SETTINGS_FILE}"
    else
      rm -f "${tmp}"
      warn "could not edit ${SETTINGS_FILE} automatically"
      print_settings_snippet
    fi
  else
    warn "neither python3 nor jq is available to edit ${SETTINGS_FILE}"
    print_settings_snippet
  fi
}

print_settings_snippet() {
  cat <<EOF

    Add this to ${SETTINGS_FILE} yourself (keep the other keys):

      {
        "env": {
          "YANDEX_DISK_TOKEN_FILE": "${TOKEN_FILE}"
        }
      }

EOF
}

# -- done ---------------------------------------------------------------------

print_next_steps() {
  cat <<EOF

  ${GREEN}${BOLD}Done.${RESET}

  Try it — in Claude Code, Codex or another Agent Skills client, ask:

      ${BOLD}"What's in my Downloads folder on Yandex Disk?"${RESET}

  or run the scripts directly:

      python3 ${SKILL_SOURCE}/scripts/downloads_sort.py check

  The skill never deletes or overwrites anything: it shows you a plan first, moves files
  only after you say yes, and keeps a journal so every move can be undone.

  Docs:   https://github.com/${REPO_SLUG}
  Update: re-run this installer, or git -C ${SKILL_DIR} pull

EOF
}

main "$@"
