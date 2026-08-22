#!/bin/bash
#
# Installer for the yandex-disk-downloads-sort agent skill.
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/akinfold/yandex-disk-downloads-sort-skill/HEAD/install.sh)"
#
# It downloads the published skill archive from the latest GitHub release, checks it
# against the SHA-256 the release ships, links the skill into the places Claude Code,
# Codex, Cursor and other Agent Skills clients look, asks for a Yandex Disk OAuth token,
# stores it in ~/.yandex-disk-token with owner-only permissions, and points Claude Code
# at that file. No git, no sudo, nothing installed system-wide.
#
# Every step says what it is about to do, refuses to clobber anything it did not create,
# and can be re-run safely.
#
# Environment variables (all optional):
#   YADISK_SKILL_DIR    where to keep the skill (default: ~/.local/share/yandex-disk-downloads-sort)
#   YADISK_VERSION      install a specific release tag, e.g. v1.0.0 (default: the latest)
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

# Any temp file that touches the token is registered here and removed however the script
# ends — normally, on error, or on Ctrl-C.
TMPFILES=()
cleanup() {
  # bash 3.2 treats "${empty[@]}" as an unbound variable under `set -u`, so check first.
  [ "${#TMPFILES[@]}" -gt 0 ] || return 0
  rm -rf "${TMPFILES[@]}"
  TMPFILES=()
}
install_traps() {
  trap cleanup EXIT
  trap 'cleanup; exit 130' INT
  trap 'cleanup; exit 143' TERM
}
install_traps

# The whole installer lives in main(), invoked on the last line. If the download is cut
# short, bash never reaches that line, so a partial script cannot half-install anything.
main() {
  REPO_SLUG="akinfold/yandex-disk-downloads-sort-skill"
  SKILL_NAME="yandex-disk-downloads-sort"
  ARCHIVE="${SKILL_NAME}.tar.gz"
  VERSION_WANTED="${YADISK_VERSION:-}"
  if [ -n "${VERSION_WANTED}" ]; then
    RELEASE_BASE="https://github.com/${REPO_SLUG}/releases/download/${VERSION_WANTED}"
  else
    RELEASE_BASE="https://github.com/${REPO_SLUG}/releases/latest/download"
  fi
  POLIGON_URL="https://yandex.ru/dev/disk/poligon/"
  REVOKE_URL="https://id.yandex.ru/personal/data-access"
  API_URL="https://cloud-api.yandex.net/v1/disk"

  SKILL_DIR="${YADISK_SKILL_DIR:-${HOME}/.local/share/yandex-disk-downloads-sort}"
  INSTALLED_VERSION="unknown"
  TOKEN_FILE="${YADISK_TOKEN_FILE:-${HOME}/.yandex-disk-token}"
  SETTINGS_FILE="${HOME}/.claude/settings.json"
  NONINTERACTIVE="${NONINTERACTIVE:-}"
  [ -n "${CI:-}" ] && NONINTERACTIVE=1

  setup_output
  if [ -z "${HOME:-}" ] || [ ! -d "${HOME}" ]; then
    abort "HOME is empty or not a directory; nothing can be installed."
  fi
  parse_args "$@"
  SKILL_DIR=$(absolute_path "${SKILL_DIR}")
  TOKEN_FILE=$(absolute_path "${TOKEN_FILE}")

  cat <<EOF

  ${BOLD}yandex-disk-downloads-sort${RESET} — analyze and sort your Yandex Disk Downloads folder

  This installer will:
    1. download the ${VERSION_WANTED:-latest} release of ${REPO_SLUG} into ${SKILL_DIR}
       and check it against the SHA-256 published with it
    2. link the skill into ~/.claude/skills and ~/.agents/skills
    3. ask you for a Yandex Disk OAuth token and save it to ${TOKEN_FILE} (mode 600)
    4. point Claude Code at that file via ~/.claude/settings.json

  It needs no sudo, writes only inside your home directory (plus one short-lived temp file
  while it checks the token), and is safe to re-run.

EOF

  check_prerequisites
  fetch_release
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
  --dir PATH      keep the skill at PATH (default ~/.local/share/yandex-disk-downloads-sort)

Environment: YANDEX_DISK_TOKEN, YADISK_TOKEN_FILE, YADISK_SKILL_DIR, YADISK_VERSION,
             NONINTERACTIVE=1

Through the one-liner, options go after an empty argument, which bash assigns to \$0:

  /bin/bash -c "\$(curl -fsSL https://raw.githubusercontent.com/${REPO_SLUG}/HEAD/install.sh)" '' --no-token

See https://github.com/${REPO_SLUG} for what the skill does.
EOF
}

parse_args() {
  SKIP_TOKEN=""
  SKIP_SETTINGS=""
  [ -z "${YADISK_NO_SETTINGS:-}" ] || SKIP_SETTINGS=1
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

# Symlinks are resolved from the directory that holds them, so a relative target would
# dangle once it is written into ~/.claude/skills.
absolute_path() {
  # The "~/" pattern is quoted on purpose: it matches a literal tilde that arrived inside a
  # variable (--dir '~/skills'). Unquoted, bash would expand it and the branch could never
  # match anything.
  # shellcheck disable=SC2088
  case "$1" in
    /*) printf '%s' "$1" ;;
    "~/"*) printf '%s' "${HOME}/${1#"~/"}" ;;
    *) printf '%s' "${PWD}/$1" ;;
  esac
}

# -- prerequisites ------------------------------------------------------------

check_prerequisites() {
  ohai "Checking prerequisites"

  # On macOS /usr/bin/git is a stub that only prompts to install the Command Line Tools, so
  # ask git to actually do something rather than trusting that the path exists.
  if ! curl --version >/dev/null 2>&1; then
    abort "curl is required, and the curl on your PATH does not run."
  fi
  ok "curl"

  if ! tar --version >/dev/null 2>&1 && ! tar --help >/dev/null 2>&1; then
    abort "tar is required, and the tar on your PATH does not run."
  fi
  ok "tar"

  if have sha256sum; then
    SHA_TOOL="sha256sum"
  elif have shasum; then
    SHA_TOOL="shasum -a 256"
  else
    SHA_TOOL=""
    warn "no sha256sum or shasum found; the download cannot be checked against its published hash"
  fi
  [ -z "${SHA_TOOL}" ] || ok "checksums via ${SHA_TOOL}"

  if have python3 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    ok "python3 $(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null)"
  else
    warn "python3 3.9+ was not found. The skill's scripts need it at run time; the install will continue."
  fi
}

# -- the skill itself ---------------------------------------------------------

fetch_release() {
  ohai "Fetching the skill"

  archive_url="${RELEASE_BASE}/${ARCHIVE}"
  work=$(mktemp -d "${TMPDIR:-/tmp}/yadisk-install.XXXXXXXX") || abort "cannot create a temp directory"
  TMPFILES+=("${work}")

  info "downloading ${archive_url}"
  if ! curl -fsSL --retry 2 --retry-delay 2 --max-time 120 -o "${work}/${ARCHIVE}" "${archive_url}"; then
    abort "could not download ${archive_url}
    If the URL 404s, this repository may have no published release yet; see
    https://github.com/${REPO_SLUG}/releases"
  fi
  ok "downloaded $(archive_size "${work}/${ARCHIVE}")"

  verify_archive "${work}"
  unpack_archive "${work}"

  SKILL_SOURCE="${SKILL_DIR}/${SKILL_NAME}"
  [ -f "${SKILL_SOURCE}/SKILL.md" ] || abort "${SKILL_SOURCE}/SKILL.md is missing — the archive looks wrong."
  INSTALLED_VERSION=$(cat "${SKILL_SOURCE}/VERSION" 2>/dev/null || echo "unknown")
  ok "installed version ${INSTALLED_VERSION} into ${SKILL_DIR}"
}

archive_size() {
  size=$(wc -c < "$1" 2>/dev/null | tr -d ' ')
  case "${size}" in
    ''|*[!0-9]*) printf 'the archive' ;;
    *) printf '%s KB' "$((size / 1024))" ;;
  esac
}

# A tarball fetched over the network gets unpacked into the user's home directory, so check
# it against the hash published beside it rather than trusting the transfer.
verify_archive() {
  work="$1"
  if [ -z "${SHA_TOOL}" ]; then
    warn "skipping the checksum: no sha256 tool on this machine"
    return 0
  fi
  if ! curl -fsSL --max-time 30 -o "${work}/${ARCHIVE}.sha256" "${RELEASE_BASE}/${ARCHIVE}.sha256"; then
    warn "the release publishes no ${ARCHIVE}.sha256; continuing without verifying"
    return 0
  fi
  published=$(awk '{print $1; exit}' "${work}/${ARCHIVE}.sha256")
  actual=$(${SHA_TOOL} "${work}/${ARCHIVE}" | awk '{print $1; exit}')
  if [ -z "${published}" ] || [ "${published}" != "${actual}" ]; then
    abort "checksum mismatch for ${ARCHIVE}
    published: ${published:-(none)}
    actual:    ${actual}
    Nothing was installed. Try again; if it keeps failing, report it at
    https://github.com/${REPO_SLUG}/issues"
  fi
  ok "checksum matches (${actual})"
}

# Unpack beside the target and swap, so an interrupted extraction cannot leave a half-written
# skill where the agent will look for one.
unpack_archive() {
  work="$1"
  staged="${work}/unpacked"
  mkdir -p "${staged}" || abort "cannot use ${staged}"
  tar -xzf "${work}/${ARCHIVE}" -C "${staged}" || abort "could not unpack ${ARCHIVE}"
  [ -d "${staged}/${SKILL_NAME}" ] || abort "the archive does not contain ${SKILL_NAME}/"

  mkdir -p "${SKILL_DIR}" || abort "cannot create ${SKILL_DIR}"
  target="${SKILL_DIR}/${SKILL_NAME}"
  if [ -e "${target}" ]; then
    previous=$(cat "${target}/VERSION" 2>/dev/null || echo "unknown")
    info "replacing the copy already in ${SKILL_DIR} (version ${previous})"
    retired="${work}/previous"
    mv "${target}" "${retired}" || abort "cannot move the old copy out of ${target}"
  fi
  mv "${staged}/${SKILL_NAME}" "${target}" || abort "cannot move the new copy into ${target}"
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
  unset YANDEX_DISK_TOKEN  # do not pass the secret on to git, curl, python or the skill

  if [ -z "${token}" ] && [ -s "${TOKEN_FILE}" ]; then
    if [ -n "${NONINTERACTIVE}" ] || ! can_prompt; then
      ok "${TOKEN_FILE} already has a token; keeping it"
      tighten_token_file
      return 0
    fi
    printf "    %s already contains a token. Replace it? [y/N] " "${TOKEN_FILE}" > /dev/tty
    IFS= read -r answer < /dev/tty || answer=""
    case "${answer}" in
      y|Y|yes|YES) : ;;
      *) ok "keeping the existing token"; tighten_token_file; return 0 ;;
    esac
  fi

  if [ -z "${token}" ]; then
    if [ -n "${NONINTERACTIVE}" ] || ! can_prompt; then
      warn "no terminal to ask for a token"
      info "get one as described at ${POLIGON_URL}, then run this, which reads the token"
      info "from the keyboard and so keeps it out of your shell history:"
      info "  (umask 077; read -rs t && printf '%s' \"\$t\" > ${TOKEN_FILE}); unset t"
      info "or re-run this installer from a terminal"
      return 0
    fi
    print_token_instructions
    printf "  Paste the token and press RETURN (it stays hidden), or press RETURN to skip:\n  > " > /dev/tty
    # read -s turns echo off; a Ctrl-C in the middle would otherwise leave the terminal
    # silent for every command the user types afterwards.
    stty_state=$(stty -g < /dev/tty 2>/dev/null || true)
    trap 'restore_tty "${stty_state}"; cleanup; printf "\n" > /dev/tty 2>/dev/null; exit 130' INT TERM
    IFS= read -rs token < /dev/tty || token=""
    install_traps  # back to the plain cleanup handlers, not to bash's defaults
    restore_tty "${stty_state}"
    printf "\n" > /dev/tty
  fi

  # Strip a stray carriage return or surrounding whitespace from the paste.
  token=$(printf '%s' "${token}" | tr -d '\r\n\t ' )

  if [ -z "${token}" ]; then
    warn "no token entered; the skill is installed but cannot talk to Yandex yet"
    info "when you have one, re-run this installer, or paste it without leaving it in history:"
    info "  (umask 077; read -rs t && printf '%s' \"\$t\" > ${TOKEN_FILE}); unset t"
    return 0
  fi

  if can_prompt; then printf "    got %s\n" "$(mask_token "${token}")" > /dev/tty; else info "got $(mask_token "${token}")"; fi
  check_token_shape "${token}"

  verify_token "${token}"
  verdict=$?
  if [ "${verdict}" -eq 1 ] && [ -s "${TOKEN_FILE}" ]; then
    # Only an outright rejection blocks the write: replacing a token that works with one
    # the server refused would be a downgrade. A network failure proves nothing.
    warn "keeping the token already in ${TOKEN_FILE}; Yandex refused the new one"
    info "to store it anyway, run this installer again when the token is right"
    tighten_token_file
  else
    write_token "${token}"
    [ "${verdict}" -eq 0 ] || info "saved without a successful check"
  fi
  unset token
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
  # The token reaches curl through a file, not on its command line, where any user on the
  # machine could read it out of `ps`.
  headers=$(mktemp "${TMPDIR:-/tmp}/yadisk-install.XXXXXXXX") || {
    warn "cannot create a temp file; skipping the check"
    return 0
  }
  TMPFILES+=("${headers}")
  chmod 600 "${headers}" 2>/dev/null
  printf 'header = "Authorization: OAuth %s"\n' "${t}" > "${headers}"

  body=$(curl -fsS --max-time 20 --config "${headers}" "${API_URL}" 2>/dev/null)
  status=$?
  cleanup

  if [ "${status}" -ne 0 ]; then
    # curl exits 22 for an HTTP error (with -f) and 6/7/28/35 for DNS, connection and TLS
    # trouble — worth telling apart, because the remedies are nothing alike.
    case "${status}" in
      22)
        warn "Yandex refused the token: it is wrong, expired or revoked. Get a fresh one at ${POLIGON_URL}"
        return 1 ;;
      *)
        warn "could not check the token (curl exit ${status}); the token itself may be fine"
        return 2 ;;  # unreachable, not refused
    esac
  fi

  login=$(printf '%s' "${body}" | extract_json_field "login")
  free=$(printf '%s' "${body}" | free_space)
  if [ -n "${login}" ]; then
    ok "token works — signed in as ${BOLD}${login}${RESET}${free}"
  else
    ok "token works"
  fi
  return 0
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

# A token file left readable by others is worth fixing even when we did not write it.
tighten_token_file() {
  [ -f "${TOKEN_FILE}" ] || return 0
  mode=$(file_mode "${TOKEN_FILE}")
  case "${mode}" in
    600|400) : ;;
    *) chmod 600 "${TOKEN_FILE}" 2>/dev/null && info "tightened ${TOKEN_FILE} from mode ${mode} to 600" ;;
  esac
}

write_token() {
  # Remove first: umask applies to creation only, so writing into an existing 0644 file
  # would put the token there at 0644 and only tighten it afterwards.
  rm -f "${TOKEN_FILE}"
  ( umask 077; printf '%s' "$1" > "${TOKEN_FILE}" ) || abort "cannot write ${TOKEN_FILE}"
  chmod 600 "${TOKEN_FILE}" 2>/dev/null
  ok "token saved to ${TOKEN_FILE} (mode $(file_mode "${TOKEN_FILE}"))"
}

file_mode() {
  # -L follows symlinks: the mode that matters is the file's, not the link's (links are
  # 0777 on Linux and 0755 on macOS, and copying that onto a settings file would be bad).
  stat -L -c '%a' "$1" 2>/dev/null ||
    stat -L -f '%Lp' "$1" 2>/dev/null ||
    printf '600'
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
import json, os, shutil, stat, sys, tempfile, time

path, token_file = sys.argv[1], sys.argv[2]
path = os.path.realpath(path)  # write through a symlink, never replace it
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
    # NamedTemporaryFile is 0600; carry over whatever mode the user had on the original.
    os.chmod(handle.name, stat.S_IMODE(os.stat(path).st_mode))
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
    if [ "$(jq -r --arg f "${TOKEN_FILE}" '.env.YANDEX_DISK_TOKEN_FILE == $f' "$(resolve_symlink "${SETTINGS_FILE}")" 2>/dev/null)" = "true" ]; then
      ok "already set in ${SETTINGS_FILE}"
      return 0
    fi
    # Rename onto the real file, never onto a symlink: a settings.json managed by stow or
    # chezmoi is a link, and mv would replace the link instead of updating its target. The
    # temp file lives beside that real file so the rename stays on one filesystem, and so
    # stays atomic.
    settings_real=$(resolve_symlink "${SETTINGS_FILE}")
    tmp="${settings_real}.tmp.$$"
    if ( umask 077; jq --arg f "${TOKEN_FILE}" '.env = ((.env // {}) + {YANDEX_DISK_TOKEN_FILE: $f})' \
        "${settings_real}" > "${tmp}" ) 2>/dev/null; then
      copy_mode "${settings_real}" "${tmp}"
      cp -p "${settings_real}" "${settings_real}.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null
      mv "${tmp}" "${settings_real}" && ok "added env.YANDEX_DISK_TOKEN_FILE to ${SETTINGS_FILE}"
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

# readlink -f is GNU-only and arrived late on macOS, so walk the chain by hand.
resolve_symlink() {
  _f="$1"
  _n=0
  while [ -L "${_f}" ] && [ "${_n}" -lt 32 ]; do
    _t=$(readlink "${_f}")
    case "${_t}" in
      /*) _f="${_t}" ;;
      *) _f="$(dirname "${_f}")/${_t}" ;;
    esac
    _n=$((_n + 1))
  done
  printf '%s' "${_f}"
}

copy_mode() {
  mode=$(file_mode "$1")
  [ -n "${mode}" ] && chmod "${mode}" "$2" 2>/dev/null
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

  In ${BOLD}Claude Code${RESET} the token is wired up already — just ask:

      ${BOLD}"What's in my Downloads folder on Yandex Disk?"${RESET}

  In ${BOLD}Codex, Cursor${RESET} and other clients, put this in your shell profile first, so their
  scripts can find the token:

      export YANDEX_DISK_TOKEN_FILE="${TOKEN_FILE}"

  To run it yourself, from any shell:

      YANDEX_DISK_TOKEN_FILE="${TOKEN_FILE}" python3 ${SKILL_SOURCE}/scripts/downloads_sort.py check

  The skill shows you a plan before it touches anything, moves files only after you agree,
  never overwrites a file, and journals every move so it can be undone. It deletes nothing —
  the one exception is "undo --remove-empty-folders", which sends folders it created itself,
  and only if they are empty, to the Disk's trash.

  Installed: ${SKILL_NAME} ${INSTALLED_VERSION} in ${SKILL_DIR}
  Docs:      https://github.com/${REPO_SLUG}
  Update:    re-run this installer; it always fetches the latest release

EOF
}

main "$@"
