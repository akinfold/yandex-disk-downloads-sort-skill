# Yandex Disk REST API cheat sheet

What the scripts rely on, condensed from the official reference
(https://yandex.ru/dev/disk-api/doc/ru/, English: https://yandex.com/dev/disk-api/doc/en/) and
verified live in August 2026. Use it to debug the scripts or to run the workflow by hand.

Contents: basics · paths · endpoints used by the skill · resource fields · limits ·
curl recipes · other endpoints.

## Basics

- Base URL: `https://cloud-api.yandex.net/v1/disk`. JSON only: send `Accept: application/json`.
- Auth on every request: `Authorization: OAuth <token>`. The `Bearer` scheme is not
  documented and is reported to fail with 401; use `OAuth`.
- Errors come as `{"error": "<Code>", "description": "<English>", "message": "<localized>"}`.
  Read `description` (stable English); `message` follows the account language.
- Status codes: 400 bad parameter (`FieldValidationError`); 401 `UnauthorizedError`; 403
  forbidden (rights, quota, bad target); 404 `DiskNotFoundError`; 406 unsupported format; 409
  conflict (see mkdir/move); 413 too large; 423 read-only (maintenance, `DiskResourceLockedError`);
  429 too many requests; 503 unavailable; 507 out of space.
- Retry 429, 500, 502, 503, 504 with backoff; honour `Retry-After` when present. The terms of
  service cap clients at 40 requests per second; the scripts stay far below that.
- Deferred work: some calls answer `202 Accepted` with a Link whose `href` is
  `…/v1/disk/operations?id=<hex>`. `GET` that href (with the auth header) until `status` is
  `success` or `failed` (`in-progress` meanwhile). `GET /operations/<id>` is the documented
  equivalent.

## Paths

- Absolute paths with a scheme: `disk:/Загрузки/report.pdf`. `/Загрузки/report.pdf` is
  accepted as well; `app:/` addresses the application folder; `trash:/` the bin. Responses
  always return unencoded `disk:/…` paths.
- In query strings percent-encode the value as UTF-8: space `%20`, `+` `%2B`, `#` `%23`,
  `%` `%25`, `&` `%26`. A literal `+` is decoded as a space, so it must be encoded.
  `urllib.parse.quote(path, safe="")` is right; the scripts do exactly that.
- Folder name ≤ 255 characters, full path ≤ 32 760.
- The Downloads folder is a *system folder*: `GET /disk` → `system_folders.downloads`
  (e.g. `"disk:/Загрузки/"`, note the trailing slash; on English-interface accounts
  `disk:/Downloads`). System folders are created lazily, so the advertised path may not exist
  on a fresh account. Other keys: `applications`, `screenshots`, `photostream`, `social`,
  `scans`, `attach`, `messenger`, `calendar`, plus social-network subfolders.

## Endpoints used by the skill

### `GET /disk` — quota and system folders

Response: `total_space`, `used_space`, `trash_size`, `system_folders{…}`, `user{login,…}`,
`is_paid`, `max_file_size`. Optional `fields`.

### `GET /resources?path=…` — metadata and folder listing

Query: `path` (required), `limit` (default 20; no documented maximum, 200 is a safe page),
`offset`, `sort` (`name`, `path`, `created`, `modified`, `size`; prefix `-` to reverse),
`fields` (comma-separated, dot notation for nested: `_embedded.items.md5,_embedded.total`),
`preview_size`, `preview_crop`.

A folder answers with its own metadata plus `_embedded: {items: [...], total, limit, offset,
sort, path}`. Paginate with `offset += len(items)` until `offset >= total`. A file answers with
the Resource object alone. 404 if the path does not exist.

### `PUT /resources?path=…` — create a folder

201 + Link on success. The parent must exist (no recursive creation). Conflicts are 409:
`DiskPathPointsToExistentDirectoryError` (already a folder there: treat as success),
`DiskPathDoesntExistsError` (parent missing: create it first),
`DiskResourceAlreadyExistsError` (a file occupies the name).

### `POST /resources/move?from=…&path=…&overwrite=false` — move or rename

`from` and `path` are full paths including the file name. `overwrite=false` refuses an existing
target with 409 `DiskResourceAlreadyExistsError`; the scripts then retry with ` (2)`, ` (3)`…
before the extension. 201 + Link when done synchronously (files, empty folders); 202 + Link to
an operation for non-empty folders or with `force_async=true`. 404 when `from` is gone; 409
`DiskPathDoesntExistsError` when the target folder is missing.

### `GET /operations?id=…` — deferred operation status

`{"status": "in-progress" | "success" | "failed"}`; 404 `DiskOperationNotFoundError` for an
unknown id. On `failed`, repeat the original request.

### `DELETE /resources?path=…&permanently=false` — to the trash

Only used by `undo --remove-empty-folders`, on folders the skill created and that are empty.
204 (file or empty folder) or 202 + operation. `permanently=true` bypasses the trash; the
scripts never send it.

## Resource fields

| Field | Notes |
|---|---|
| `name`, `path`, `type` | `type` is `file` or `dir` |
| `size` | bytes, files only |
| `created`, `modified` | ISO 8601 with offset, e.g. `2026-08-21T21:09:14+00:00` |
| `md5`, `sha256` | present for every file in listings: duplicates need no download |
| `media_type` | server-computed: `audio`, `backup`, `book`, `compressed`, `data`, `development`, `diskimage`, `document`, `encoded`, `executable`, `flash`, `font`, `image`, `settings`, `spreadsheet`, `text`, `unknown`, `video`, `web` |
| `mime_type` | e.g. `application/gzip` |
| `exif` | `{}` or `{date_time, gps_latitude, gps_longitude}` for photos |
| `resource_id` | `uid:hash`, stable across moves and renames |
| `revision`, `antivirus_status`, `preview`, `file`, `sizes`, `custom_properties`, `public_url` | not used by the skill |

The scripts request only the fields they need via `fields`, which keeps a 1,000-file listing
under a megabyte.

## Limits worth knowing

- `custom_properties` (via `PATCH /resources`) ≤ 1 024 characters in total; flat key/value only.
- Folder listings of many thousands of items with a huge `limit` can time out: paginate.
- 423 means the Disk is temporarily read-only (maintenance): wait and retry.

## curl recipes (scriptless workflow)

```bash
T="Authorization: OAuth $YANDEX_DISK_TOKEN"
B=https://cloud-api.yandex.net/v1/disk

# 1. Where is Downloads?
curl -sS -H "$T" "$B?fields=system_folders.downloads,used_space,total_space"

# 2. One page of the listing (repeat with offset=200, 400, ... until offset >= total)
curl -sS -H "$T" -G "$B/resources" \
  --data-urlencode "path=disk:/Загрузки" --data-urlencode "limit=200" --data-urlencode "offset=0" \
  --data-urlencode "fields=_embedded.items.name,_embedded.items.path,_embedded.items.type,_embedded.items.size,_embedded.items.media_type,_embedded.items.md5,_embedded.items.modified,_embedded.total"

# 3. Create a target folder (409 DiskPathPointsToExistentDirectoryError = already there)
curl -sS -H "$T" -X PUT -G "$B/resources" --data-urlencode "path=disk:/Загрузки/Документы"

# 4. Move a file, never overwriting (409 DiskResourceAlreadyExistsError = pick another name)
curl -sS -H "$T" -X POST -G "$B/resources/move" \
  --data-urlencode "from=disk:/Загрузки/report.pdf" \
  --data-urlencode "path=disk:/Загрузки/Документы/report.pdf" --data-urlencode "overwrite=false"

# 5. If step 4 returned 202, poll its href until status != in-progress
curl -sS -H "$T" "https://cloud-api.yandex.net/v1/disk/operations?id=<id from href>"
```

`curl -G --data-urlencode` does the percent-encoding; the same calls are available as
`python scripts/yadisk_api.py info|ls|mkdir|move|exists`.

## Other endpoints (not used by the skill)

- `POST /resources/copy` — same parameters and codes as move.
- `GET /resources/files?limit&offset&media_type` — flat list of every file on the Disk, no
  folder filter, no `total`; `GET /resources/last-uploaded?limit&media_type` — newest uploads.
- `GET /resources/upload?path&overwrite` → upload URL, then `PUT` the bytes there (no auth
  header on that host); `POST /resources/upload?path&url` — fetch from a public URL.
- `GET /resources/download?path` → temporary download URL.
- `PATCH /resources?path` with `{"custom_properties": {...}}` — attach metadata.
- `PUT /resources/publish`, `PUT /resources/unpublish` — public links.
- `GET /trash/resources?path`, `PUT /trash/resources/restore?path&name&overwrite`,
  `DELETE /trash/resources[?path]` — the bin (items there have `origin_path`).
