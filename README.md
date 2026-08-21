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

Requirements: Python 3.9+ (standard library only) and outbound HTTPS to `cloud-api.yandex.net`.

## 1. Get a Yandex Disk OAuth token (2 minutes)

1. Sign in to Yandex, open the Poligon: https://yandex.ru/dev/disk/poligon/
2. Click **"Получить OAuth-токен"** ("Get OAuth token") next to the **"Ваш OAuth-токен"** field,
   allow access, and the token appears in that field. Copy it (`y0_…`).
3. Give it to the skill:

   ```bash
   export YANDEX_DISK_TOKEN='y0_…'
   ```

   or save it to a file and pass `--token-file ~/.yandex-disk-token`.

The full guide, including registering your own OAuth app, token lifetime and revocation, is in
[references/oauth-token.md](skills/yandex-disk-downloads-sort/references/oauth-token.md).

## 2. Install the skill

Everything lives in one folder, `skills/yandex-disk-downloads-sort`. Clone the repository and
point your tool at that folder (symlinks are followed by all of them):

```bash
git clone https://github.com/akinfold/yandex-disk-downloads-sort-skill.git
```

| Tool | Where the folder goes |
|---|---|
| **Claude Code** (personal) | `ln -s "$PWD/yandex-disk-downloads-sort-skill/skills/yandex-disk-downloads-sort" ~/.claude/skills/yandex-disk-downloads-sort` |
| **Claude Code** (one project) | `.claude/skills/yandex-disk-downloads-sort/` inside the project |
| **Claude Code** (as a plugin) | `/plugin marketplace add akinfold/yandex-disk-downloads-sort-skill`, then `/plugin install yandex-disk-downloads-sort@akinfold-skills`; or for a local checkout `claude --plugin-dir ./yandex-disk-downloads-sort-skill` |
| **OpenAI Codex** CLI / IDE, **ChatGPT desktop app** | `ln -s … ~/.agents/skills/yandex-disk-downloads-sort` (project-level `.agents/skills/` also works; the older `~/.codex/skills/` still loads) |
| **Cursor, GitHub Copilot, Hermes Agent** and other Agent-Skills clients | `.agents/skills/yandex-disk-downloads-sort/` |
| **Claude.ai / Claude Cowork** | zip the folder so that the folder is the zip root (`cd skills && zip -r ../yandex-disk-downloads-sort.zip yandex-disk-downloads-sort`); in claude.ai enable *Code execution and file creation* under Settings → Capabilities, then Customize → Skills → create/upload a skill from the zip. See the network note below. |
| **ChatGPT on the web** | a Custom GPT with an action: [chatgpt/README.md](chatgpt/README.md) |

Then ask: *"Check my Yandex Disk Downloads folder"*. The skill triggers on mentions of Yandex
Disk together with Downloads/Загрузки, cleaning up, sorting, duplicates or free space.

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
