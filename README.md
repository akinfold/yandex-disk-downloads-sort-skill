# yandex-disk-downloads-sort

[![CI](https://github.com/akinfold/yandex-disk-downloads-sort-skill/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/akinfold/yandex-disk-downloads-sort-skill/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An agent skill that **analyzes and sorts the Downloads folder on Yandex Disk** through the
official REST API. It works in Claude (Claude Code, Cowork, claude.ai) and in OpenAI's tools
(Codex CLI/IDE, ChatGPT desktop), and ships a Custom GPT action for ChatGPT on the web.

> "What's in my Downloads on Yandex Disk?"
> "Sort my Загрузки into folders, put screenshots separately, and show me duplicates first."
> "Only the PDFs and spreadsheets, and keep installers where they are."

The agent inventories every file in the folder, reports what is there (by type, size, age;
exact duplicates; partial downloads; keys and certificates; largest and oldest files), proposes
a plan of moves into category folders, executes it only after you confirm, and keeps a
journal so the whole thing can be undone.

## What it does, and what it never does

- Finds the real Downloads folder through the API (`disk:/Загрузки`, `disk:/Downloads`, …),
  whatever the account language.
- Classifies files by extension, by name patterns (screenshots, invoices, private keys) and by
  Yandex's own `media_type`, into Documents, Spreadsheets, Presentations, Images, Screenshots,
  Videos, Audio, Archives, Installers, Disk images, Code and data, Books, Fonts, Torrents,
  Certificates, Other. Folder names come out Russian on a Russian Disk and English otherwise.
- Detects exact duplicates (same `md5` and size), keeps the cleanest-named copy and parks the
  rest in `_Duplicates` / `_Дубликаты`; reports look-alike names with different content.
- **Never deletes, never overwrites** (name clashes get a ` (2)` suffix), never enters
  subfolders, skips partial downloads and files modified minutes ago, re-checks each file's
  identity right before moving it, and writes every move to an append-only journal before
  sending it, so `undo` works even after an interrupted run.

The REST API is the only thing it talks to: no third-party service sees your files or token.

## Repository layout

```
skills/yandex-disk-downloads-sort/   the skill (Agent Skills format: SKILL.md + scripts + references)
  scripts/downloads_sort.py          check · analyze · plan · apply · undo
  scripts/yadisk_api.py              stdlib REST client (+ tiny CLI: info, ls, mkdir, move, exists)
  scripts/classify.py                rules engine
  assets/rules.default.json          categories, extensions, patterns — copy and edit to customize
  references/oauth-token.md          how to get a token (Poligon or your own app)
  references/yandex-disk-api.md      API cheat sheet and curl recipes
  references/sorting-rules.md        how classification works, how to write your own rules
chatgpt/                             Custom GPT: openapi.yaml (action) + instructions.md + README
tests/                               unit and end-to-end tests against an in-memory fake of the API
.claude-plugin/, .codex-plugin/      plugin manifests (+ marketplace catalog) for Claude Code and Codex/ChatGPT
```

## 1. Get a Yandex Disk OAuth token

The scripts talk to the Yandex Disk REST API with your own OAuth token, sent as
`Authorization: OAuth <token>`. Nothing is proxied through a third party. Two ways to get one;
the first needs no registration and takes about two minutes.

### Option A: the Poligon page (quickest)

[Poligon](https://yandex.ru/dev/disk/poligon/) is Yandex's own interactive console for the Disk
API. Its "get token" button runs a normal OAuth authorization for a Yandex-registered
application, and the token it issues is an ordinary OAuth token that works from any script.

1. Sign in to the Yandex account whose Disk you want to sort
   ([passport.yandex.ru](https://passport.yandex.ru)). On a shared computer use a private window.
2. Open **https://yandex.ru/dev/disk/poligon/** (English mirror:
   https://yandex.com/dev/disk/poligon/) and dismiss the cookie banner if it appears.
3. At the top of the console there is a field **"Ваш OAuth-токен"** ("Your OAuth token") and a
   yellow button **"Получить OAuth-токен"** ("Get OAuth token"). Click the button.
4. Log in if asked, then click **"Разрешить"** ("Allow") on the consent screen. If you
   authorized Poligon before and that token has not expired, the screen is skipped.
5. You are redirected back and the token appears in the **"Ваш OAuth-токен"** field. Copy it.
   A token looks like `y0_AgAAAAA…`: it starts with `y`, a digit and `_`, is about 58 characters
   long, and contains only letters, digits, `_` and `-`.

### Option B: your own OAuth application

Choose this to control the token independently of Poligon, pick exact scopes, or get refresh
tokens.

1. Open https://oauth.yandex.ru/client/new/ and pick **"Для доступа к API или отладки"**
   (for API access or debugging). The type cannot be changed later.
2. Fill in **"Название вашего сервиса"** (service name) and **"Контактная почта"** (contact email).
3. Under permissions, type "Диск" and add:
   - `cloud_api:disk.read` — read the whole Disk
   - `cloud_api:disk.write` — write anywhere on the Disk
   - `cloud_api:disk.info` — quota and system folders

   `cloud_api:disk.app_folder` alone confines the app to its own folder and is **not** enough
   to sort Downloads.
4. The Redirect URI is fixed to `https://oauth.yandex.ru/verification_code`. Click
   **"Создать приложение"** and copy the **ClientID**.
5. Open `https://oauth.yandex.ru/authorize?response_type=token&client_id=<ClientID>`, click
   **"Разрешить"**, and copy `access_token` from the resulting
   `…/verification_code#access_token=<token>&expires_in=<seconds>` URL.

### Give the token to the skill

The scripts look for it in this order: `--token-file <path>`, `YANDEX_DISK_TOKEN`,
`YANDEX_DISK_OAUTH_TOKEN`, then the file named by `YANDEX_DISK_TOKEN_FILE`.

```bash
# current shell session
export YANDEX_DISK_TOKEN='y0_your_token_here'
```

```bash
# or a file, so agents can read it without the token living in your environment
printf '%s' 'y0_your_token_here' > ~/.yandex-disk-token && chmod 600 ~/.yandex-disk-token
export YANDEX_DISK_TOKEN_FILE=~/.yandex-disk-token   # or pass --token-file ~/.yandex-disk-token
```

For Claude Code, Codex and similar, put the `export` in your shell profile or in the `.env`
your launcher loads — never in a file inside a repository. For the ChatGPT action, paste
`OAuth y0_…` as the API key (see [chatgpt/README.md](chatgpt/README.md)). The scripts never
print the token.

Verify it works:

```bash
curl -sS -H "Authorization: OAuth $YANDEX_DISK_TOKEN" https://cloud-api.yandex.net/v1/disk
```

A `200` with JSON (`total_space`, `system_folders`, …) means you are set. A
`401 UnauthorizedError` means the token is wrong, expired or revoked — note that Yandex
requires the `OAuth` scheme, so `Bearer` will not work.

**Lifetime and revocation.** Yandex tokens are typically valid up to a year (the exact value is
`expires_in` in the redirect URL); when one expires, repeat the steps. Revoke a token at
[id.yandex.ru/personal/data-access](https://id.yandex.ru/personal/data-access) by removing the
application. Changing your password, toggling two-factor authentication, or "log out
everywhere" revokes every token.

The longer guide (device flow, refresh tokens, error table) is in
[references/oauth-token.md](skills/yandex-disk-downloads-sort/references/oauth-token.md).

## 2. Install the skill

Everything lives in one folder, `skills/yandex-disk-downloads-sort`. Clone the repository once,
then point your tool at that folder — all of these tools follow symlinks, so one checkout can
serve every one of them:

```bash
git clone https://github.com/akinfold/yandex-disk-downloads-sort-skill.git
cd yandex-disk-downloads-sort-skill
```

**Claude Code** — as a personal skill:

```bash
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/yandex-disk-downloads-sort" ~/.claude/skills/yandex-disk-downloads-sort
```

**Claude Code** — as a plugin (the repository ships a marketplace catalog):

```
/plugin marketplace add akinfold/yandex-disk-downloads-sort-skill
/plugin install yandex-disk-downloads-sort@akinfold-skills
```

For a local checkout without the marketplace: `claude --plugin-dir .`

**OpenAI Codex** (CLI, IDE extension, ChatGPT desktop app), **Cursor**, **GitHub Copilot**,
**Hermes Agent** and other Agent-Skills clients:

```bash
mkdir -p ~/.agents/skills
ln -s "$PWD/skills/yandex-disk-downloads-sort" ~/.agents/skills/yandex-disk-downloads-sort
```

Project-scoped installs work too: copy or symlink the folder into `.claude/skills/` or
`.agents/skills/` inside the project.

**Claude.ai / Claude Cowork** — upload a zip whose root is the skill folder:

```bash
cd skills && zip -r ../yandex-disk-downloads-sort.zip yandex-disk-downloads-sort && cd ..
```

In claude.ai enable *Code execution and file creation* under Settings → Capabilities, then go
to Customize → Skills and create a skill from that zip.

**ChatGPT on the web** cannot run the scripts (its code interpreter has no network), so use the
Custom GPT action instead: [chatgpt/README.md](chatgpt/README.md).

### Check the install

```bash
python3 skills/yandex-disk-downloads-sort/scripts/downloads_sort.py check
```

This prints the detected Downloads folder, the file count and your quota. Then ask your agent:
*"Check my Yandex Disk Downloads folder"*. The skill triggers on mentions of Yandex Disk
together with Downloads/Загрузки, cleaning up, sorting, duplicates or free space.

Requirements: Python 3.9+ and nothing else — the scripts use only the standard library.

### Where the scripts can actually reach Yandex

The scripts need outbound HTTPS to `cloud-api.yandex.net`. On your own machine (Claude Code,
Codex CLI, Cursor) that is a given. In claude.ai, Cowork and ChatGPT Work the code sandbox's
network access is a plan/organization setting (in claude.ai: Settings → Capabilities; Team and
Enterprise admins can restrict egress to package registries or an allowlist). The Claude API's
skill sandbox has no network at all. When the host is unreachable the skill says so clearly;
fall back to Claude Code/Codex on a local machine or to the ChatGPT action.

## 3. Use it

The agent runs these for you; you can also run them directly:

```bash
cd skills/yandex-disk-downloads-sort
python3 scripts/downloads_sort.py check            # token OK? where is Downloads? how big?
python3 scripts/downloads_sort.py analyze          # report + inventory.json
python3 scripts/downloads_sort.py plan             # plan.json + summary table (offline)
python3 scripts/downloads_sort.py plan --by-date year --exclude installers
python3 scripts/downloads_sort.py apply --yes      # create folders, move files, write a journal
python3 scripts/downloads_sort.py undo --yes       # move everything back
```

All commands take `--token-file`, `--path` (another folder than Downloads), `--workdir` (where
inventory/plan/journal go), `--names en|ru` and `--rules my-rules.json` before the subcommand.
Run with `--help` for the full list. The skill's own instructions for the agent are in
[SKILL.md](skills/yandex-disk-downloads-sort/SKILL.md).

### Customizing the sorting

Copy `assets/rules.default.json`, edit categories, extensions, folder names or name patterns,
and pass `--rules`. [references/sorting-rules.md](skills/yandex-disk-downloads-sort/references/sorting-rules.md)
explains the format and the resolution order.

## Development

```bash
python3 -m unittest discover -s tests -v        # no network needed: an in-memory fake of the API
python3 tests/check_openapi_limits.py           # GPT action schema within the editor's limits (needs PyYAML)
```

CI runs the tests on Python 3.9–3.13 and validates `chatgpt/openapi.yaml` with
`openapi-spec-validator`. The scripts were also exercised end-to-end against a real Yandex
Disk account (create folders, move, rename on clash, deferred operations, undo).

Contributions are welcome: new screenshot patterns, extensions and folder-name translations
belong in `assets/rules.default.json`; protocol quirks in `references/yandex-disk-api.md`.

## License

MIT. Not affiliated with Yandex, Anthropic or OpenAI.
