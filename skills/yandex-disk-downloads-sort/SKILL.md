---
name: yandex-disk-downloads-sort
description: Analyzes and tidies the Downloads folder ("Загрузки") on Yandex Disk through the Yandex Disk REST API. Inventories every file and subfolder, reports what is there by type, size and age, finds exact duplicates (files and whole folders), partial downloads and stray installers, then sorts files and whole subfolders into category folders (Documents, Images, Screenshots, Archives, Installers, ...) with a reviewable dry-run plan, no overwrites, no deletes, and an undo journal. Use this whenever the user mentions Yandex Disk / Яндекс Диск / Я.Диск together with Downloads / Загрузки, a cluttered cloud folder, cleaning up, sorting, organizing, "what is in my Downloads", duplicates, or freeing space on Yandex Disk, even if they never say "sort". Needs a Yandex OAuth token in YANDEX_DISK_TOKEN (see references/oauth-token.md to get one in two minutes).
license: MIT
compatibility: Python 3.9+ (standard library only) and outbound HTTPS to cloud-api.yandex.net. Works in Claude Code, Claude Cowork, OpenAI Codex and any agent that can run scripts; for ChatGPT use the Custom GPT action in chatgpt/ of the repository instead.
metadata:
  author: Roman Akinfeev
  version: "1.2.0"
  repository: https://github.com/akinfold/yandex-disk-downloads-sort-skill
---

# Yandex Disk: analyze and sort Downloads

The Downloads folder on Yandex Disk (`disk:/Загрузки` on Russian accounts, `disk:/Downloads`
on others) collects browser downloads, mail attachments and "save to Disk" clicks until it
is a few hundred loose files. This skill turns it into a handful of category folders and
tells the user what they have, without ever deleting or overwriting anything.

Everything goes through the official REST API (`https://cloud-api.yandex.net/v1/disk`) with
the user's own OAuth token. No third-party service sees the files or the token.

## Before you start

1. **Token.** The scripts read `YANDEX_DISK_TOKEN` (also `YANDEX_DISK_OAUTH_TOKEN`, or
   `--token-file <path>`). If it is missing, do not guess: point the user to
   `references/oauth-token.md` (the Poligon page issues a token in two minutes) and ask them
   to export it in their shell or put it in a file. Never echo, log or paste the token.
2. **Python 3.9+.** Scripts use only the standard library, so there is nothing to install.
3. **Scratch directory.** Inventory, plan and journals are written to `--workdir` (default:
   `<tmp>/yandex-disk-downloads-sort`). If you have a scratchpad directory, pass it, so the
   user can find the files later.

Run scripts from anywhere with their full path, e.g.
`python <skill>/scripts/downloads_sort.py check`. All commands accept the global options
`--token-file`, `--path`, `--workdir`, `--names` and `--rules` before the subcommand.

## The workflow

The shape is **look → propose → confirm → act → report**. The scripts enforce the first
three: `apply` refuses to move anything without `--yes`, and a plan must exist first.

### 1. `check`: is the token good, where is Downloads, how big is it

```bash
python scripts/downloads_sort.py check
```

Prints the detected Downloads path (taken from `system_folders.downloads` in `GET /disk`, so
it is right regardless of the account language), the file count and size, and the quota.
A 401 here means the token is bad or lacks the `cloud_api:disk.*` scopes; a 404 means the
folder does not exist under that name: ask the user and pass `--path "disk:/Some folder"`.

### 2. `analyze`: inventory and report

```bash
python scripts/downloads_sort.py analyze
```

Lists what sits directly inside Downloads — both files and subfolders — classifies each one
and prints a markdown report: proposed categories with counts and sizes, exact duplicates — both files (same md5
and size) and whole folders (same files, checksums and arrangement) —
look-alike names with different content, partial downloads that will be skipped, sensitive
files (keys, certificates), the largest and oldest files, and extension statistics. It also
writes `inventory.json` and `report.md` to the workdir.

Relay the report to the user in their language as a short narrative, not a raw dump: how
many files, what dominates, what is wasteful (duplicates, old installers, huge videos), and
what the sorting would look like. The tables are there to back that narrative up.

### 3. `plan`: a reviewable list of moves (no network)

```bash
python scripts/downloads_sort.py plan
python scripts/downloads_sort.py plan --by-date year          # Category/2026/...
python scripts/downloads_sort.py plan --only images,screenshots
python scripts/downloads_sort.py plan --exclude installers --keep-other
```

Turns the inventory into `plan.json` plus a summary table (target folder, files, size,
examples). It is cheap and offline, so it is the right place to iterate on the user's
wishes: re-run with different options until they like the result.

| Option | Effect |
|---|---|
| `--names auto\|en\|ru` (global) | Folder names language. `auto` picks Russian when the Downloads folder itself has a Cyrillic name. |
| `--by-date none\|year\|month` | Adds `YYYY` or `YYYY-MM` subfolders inside each category (photos use EXIF date, else `created`). |
| `--duplicates quarantine\|ignore` | `quarantine` (default) keeps the cleanest-named copy and moves the others to `_Duplicates` / `_Дубликаты`. |
| `--keep-other` | Leave unclassifiable files where they are instead of moving them to `Other`. |
| `--only a,b` / `--exclude a,b` | Restrict to, or leave out, category ids (`images`, `documents`, `installers`, ...). |
| `--min-age-minutes N` | Skip files modified less than N minutes ago (default 5; they may still be syncing). |
| `--max-moves N` | Cap the number of moves for a cautious first run. |
| `--rules file.json` (global) | Custom categories: see `references/sorting-rules.md`. |
| `--folders content\|group\|skip` (global) | What to do with subfolders. `content` (default) sends each to the category its files belong to; `group` puts them all in one `Folders`/`Папки`; `skip` leaves them where they are, as versions before 1.1 did. |

Show the user the summary table and the skipped reasons, and ask for an explicit go-ahead.
Mention anything the plan will do that they might not expect: renames on name clashes,
copies going to the duplicates folder, sensitive files being moved.

### 4. `apply --yes`: create folders and move files

```bash
python scripts/downloads_sort.py apply --yes
```

Only after the user confirmed. Re-lists the folder once and checks every planned file is
still the same file (resource id, or md5 and size): anything that changed or vanished since
the plan is reported as `changed` / `missing` and left alone. Creates the target folders
(parents first, existing folders are reused), then moves files one by one with
`overwrite=false`; a name clash at the target becomes `name (2).ext`; five consecutive
failures stop the run. Every move is written to the append-only
`journal-<timestamp>.jsonl` *before* it is sent and its outcome right after, so an
interrupted run is still undoable, and a move whose response was lost in transit is
recognised and recorded rather than reported as missing. Output stays short on big folders
(first 40 moves, then a progress line per 50; renames, recoveries and failures are always
listed; `--verbose` prints everything). Report the final counts, the renames and the journal
path to the user.

### 5. `undo`: put everything back

```bash
python scripts/downloads_sort.py undo --yes [--journal <file>] [--remove-empty-folders]
```

Moves files back to their original paths in reverse order (name clashes get a suffix, never
an overwrite), checking identities the same way `apply` does. With `--remove-empty-folders`,
folders that `apply` created and that ended up empty are sent to the trash; pre-existing
folders are never touched. Without `--journal` the newest journal in the workdir is used; if
it has nothing left to undo, older journals that still do are listed.

## What the user usually wants, and how to answer it

- **"What's in my Downloads?"** → `check` + `analyze`, narrate the report, stop there. Do
  not plan or move unless asked.
- **"Clean up / sort my Downloads."** → full workflow. Default options are right for most
  people; offer `--by-date year` if the folder is large or spans years.
- **"Don't touch my folders."** → `--folders skip`. **"Put all folders in one place."** →
  `--folders group`.
- **"Only the screenshots / only the PDFs."** → `plan --only screenshots` or `--only documents`.
- **"Find duplicates."** → `analyze`. It reports duplicate files, duplicate top-level folders
  (identical contents: the plan keeps one and parks the copies in the duplicates folder), and
  identical folders nested deeper, which are reported but never rearranged — what is inside a
  folder is the user's business. `analyze --deep-duplicates` reads every subfolder in full and
  also reports duplicate files living inside them; it costs a request per 200 files, so offer
  it rather than defaulting to it. Deleting is always the user's call: the scripts never delete
  files.
- **"Free up space."** → `analyze`; point at the largest files, duplicates and old
  installers/disk images. Deletion is the user's call and happens in the Disk UI or with
  `scripts/yadisk_api.py` if they explicitly ask.
- **Custom categories** (e.g. "put invoices in Accounting") → copy
  `assets/rules.default.json`, edit, pass `--rules`. `references/sorting-rules.md` explains the format.
- **A different folder than Downloads** → the same workflow with `--path "disk:/Some/folder"`.

## Safety rules the scripts implement (and why)

- **No deletes, no overwrites.** A sorter that can destroy data must be confirmed twice; one
  that cannot needs only a plan review. `overwrite=false` on every move; clashes get suffixes.
- **Subfolders move as a whole; nothing is reorganized inside them.** A folder joins the
  category holding at least 60% of its files (a folder of photos goes with the images), and
  anything mixed lands in `Folders`/`Папки`. What is inside keeps its own arrangement, which
  is somebody's work. The Disk's own folders (`Фотокамера`, `Скриншоты`, app folders), the
  category folders themselves and the duplicates folder are never moved.
- **A folder move is checked, not assumed.** The API performs it in the background and can
  stop halfway, so after every folder move the skill looks at the disk: source gone means
  done; both source and destination present means it stopped, and the remainder is carried
  over item by item and merged into the destination. Files already at the destination are
  never overwritten — a clash gets a ` (2)` suffix — and the emptied source folder goes to
  the bin only once it is genuinely empty.
- **Partial downloads are skipped** (`.crdownload`, `.part`, `.download`, ...) together with
  lock files (`~$x.docx`) and OS metadata; moving a file still being written corrupts it.
- **Recently modified files are skipped** (5 minutes by default): the Disk client may still
  be syncing them.
- **Idempotent and stale-proof.** Re-running analyze + plan on an already sorted folder yields
  an empty plan; `apply` re-checks each file's identity before touching it, so a plan that
  aged while the user thought about it cannot misplace a file that changed meanwhile.
- **Rate limits and deferred operations are handled.** 429/5xx are retried with
  `Retry-After`; a `202 Accepted` move is polled until it finishes.

## Troubleshooting

| Symptom | Meaning | Do |
|---|---|---|
| `401 UnauthorizedError` | Token missing, expired (they last about a year) or revoked | New token via `references/oauth-token.md` |
| `403` | Token lacks a permission (sorting needs `cloud_api:disk.write`) | Re-issue the token with the Disk scopes |
| `404 DownloadsNotFound` on `check` | The Disk has not created the system folder yet (fresh account) or it is named differently | Ask the user; `--path "disk:/…"` |
| `429` / `503` | Rate limited or API hiccup | Scripts retry; if it persists, wait a minute |
| A move shows `(renamed)` | The target name was taken; the file got a ` (2)` suffix | Nothing to do; tell the user |
| A folder shows `(merged N item(s) …)` | Something already lived at the destination, so the two were merged | Expected; report the count |
| A folder shows `(deferred move ended failed …)` | The background move stopped halfway; the rest was carried over by hand | Already handled; say so, and check the final counts |
| A file shows as `missing` or `changed` | It vanished, or its content/type changed, between `plan` and `apply` | Re-run `analyze` + `plan` |
| Folder names came out English on a Russian Disk | Downloads folder has a Latin name | `--names ru` |
| `CERTIFICATE_VERIFY_FAILED` (macOS) | python.org Python without root certificates | `export SSL_CERT_FILE=/etc/ssl/cert.pem`, or run `Install Certificates.command`, or `pip install certifi` |
| `Could not reach Yandex Disk` with no TLS mention | No outbound network in this sandbox (claude.ai Team/Enterprise, Cowork allowlist) | Tell the user; suggest Claude Code/Codex locally or the ChatGPT action in the repository |

## Without the scripts

If scripts cannot run (no Python, no shell), the same workflow is four API calls:
`GET /disk` (find `system_folders.downloads`), `GET /resources?path=...&limit=200&offset=N`
(list, paginate on `_embedded.total`), `PUT /resources?path=...` (mkdir),
`POST /resources/move?from=...&path=...&overwrite=false` (move; poll the `href` on 202).
`references/yandex-disk-api.md` has the exact parameters, fields and error codes, and the
repository's `chatgpt/` directory ships the same workflow as a Custom GPT action schema.

## Files in this skill

- `scripts/downloads_sort.py`: the workflow CLI (`check`, `analyze`, `plan`, `apply`, `undo`).
- `scripts/yadisk_api.py`: the REST client; also a small CLI (`info`, `ls`, `mkdir`, `move`, `exists`) for ad-hoc requests.
- `scripts/classify.py`: the rules engine (pure Python, no network).
- `assets/rules.default.json`: default categories, extensions, screenshot patterns, skip rules; copy and edit for custom sorting.
- `references/oauth-token.md`: how the user gets a token (Poligon page or their own OAuth app), lifetime, revocation.
- `references/yandex-disk-api.md`: API cheat sheet used by the scripts, for debugging or scriptless use.
- `references/sorting-rules.md`: how classification works and how to customize it.
