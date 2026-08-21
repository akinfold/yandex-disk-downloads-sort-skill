# Sorting rules: how files are classified and how to change it

The rules live in `assets/rules.default.json`. To customize, copy the file, edit it, and pass
`--rules my-rules.json` (a global option, before the subcommand) to `downloads_sort.py`.
`plan` re-classifies from the saved inventory, so you can try rule changes without
re-fetching the folder.

## Resolution order

For every file directly inside Downloads, `classify.py` decides in this order; the first
match wins:

1. **Skip rules** (`skip`): partial downloads (`crdownload`, `part`, `download`, `tmp`, ...),
   editor lock files (`~$budget.xlsx`), dotfiles, `desktop.ini`, `Thumbs.db`. Skipped files
   are reported but never moved.
2. **Categories in file order.** A category matches by `name_regex` (case-insensitive, tested
   against the whole file name) or by `extensions`. Name patterns are checked before
   extensions across all categories, which is how `Screenshot 2026-08-21 at 10.11.12.png`
   lands in Screenshots and not Images. A category with `requires_extensions_of: "images"`
   only applies its name patterns to files whose extension belongs to the `images` category,
   so `Screenshot.pdf` stays a document.
3. **Name rules** (`name_rules`) refine the result for files already assigned to a listed
   category (`applies_to`). A rule can switch the category (`"category": "certificates"`,
   which also makes the file *sensitive*) or pick a deeper folder
   (`"folder": {"en": "Documents/Finance", "ru": "Документы/Финансы"}`).
4. **`media_type` fallback** (`media_type_fallback`): Yandex computes a media type for every
   file (`image`, `video`, `audio`, `compressed`, `document`, `spreadsheet`, `book`,
   `executable`, `diskimage`, `font`, `development`, `data`, `text`, `web`, `settings`,
   `backup`, `encoded`, `flash`, `unknown`). Unknown extensions are mapped through it.
5. **`other`** for everything else. `plan --keep-other` leaves those files in place.

Ambiguous extensions (`ambiguous_extensions`: `.ts` is MPEG-TS or TypeScript, `.img` a disk
image or a picture, `.key` a Keynote deck or a private key) consult Yandex's `media_type` first
(`.key` reported as `text`, `web` or `encoded` is a key) and fall back to the extension's
default category when the media type is not listed.

Extensions are matched case-insensitively and without the dot. Multi-part extensions
(`tar.gz`, `tar.bz2`, `tar.xz`, `tar.zst`, `fb2.zip`) are recognized before the last segment.

## Default categories

| id | Folder (en / ru) | Matches |
|---|---|---|
| `screenshots` | Screenshots / Скриншоты | image files named like macOS, Windows, Android, Yandex, CleanShot screenshots (`Screenshot 2026-…`, `Снимок экрана …`, `Screenshot_2026…`, `Screenshot (3)`, …) |
| `images` | Images / Изображения | jpg, png, gif, webp, heic, avif, svg, psd, raw formats, … |
| `documents` | Documents / Документы | pdf, doc(x), odt, rtf, txt, md, pages, tex, xps, eml, msg, vcf, ics, … |
| `spreadsheets` | Spreadsheets / Таблицы | xls(x), ods, csv, tsv, numbers |
| `presentations` | Presentations / Презентации | ppt(x), odp, key |
| `books` | Books / Книги | epub, mobi, azw3, fb2, fb2.zip, djvu, cbz, cbr |
| `videos` | Videos / Видео | mp4, mkv, mov, avi, webm, ts, … |
| `audio` | Audio / Аудио | mp3, flac, m4a, wav, opus, ogg, … |
| `archives` | Archives / Архивы | zip, rar, 7z, tar.*, gz, xz, zst, … |
| `installers` | Installers / Установщики | dmg, pkg, exe, msi, apk, ipa, deb, rpm, AppImage, … |
| `disk-images` | Disk images / Образы дисков | iso, img, vhd(x), vmdk, ova, qcow2, cue, … |
| `code` | Code and data / Код и данные | py, js, ts, json, yaml, sql, sh, html, css, ipynb, parquet, sqlite, log, … |
| `fonts` | Fonts / Шрифты | ttf, otf, woff(2), … |
| `torrents` | Torrents / Торренты | torrent |
| `certificates` | Certificates and keys / Сертификаты и ключи | pem, crt, cer, p12, pfx, ovpn, ppk, kdbx, gpg, jks, … (**sensitive**) |
| `other` | Other / Прочее | everything else |

Default name rules:

- `finance`: documents, spreadsheets and images whose name contains a standalone word such as
  `invoice(s)`, `receipt(s)`, `bank/account/card statement`, `purchase order`, `contract`,
  `agreement`, `счёт`/`счета`, `чек`/`чеки`, `квитанция`, `выписка`, `акт(ы)`, `договор(ы)`,
  `оплата`, `платёж`, `налог`, `справка` (Russian inflections included) go to
  `Documents/Finance`. Word boundaries are respected and `чек-лист` is excluded, so
  `Контакты.pdf`, `Чек-лист.pdf` or `Bill Gates.pdf` stay plain documents.
- `private-key`: a `.key` file (or a file without extension) whose name has a whole-word
  `id_rsa`, `id_ed25519`, `private`, `secret`, `ssl`, `tls`, `cert`, … is a private key, not a
  Keynote deck, and switches to `certificates`. It never touches `.pdf`/`.docx` files, so
  `Certificate of completion.pdf` is a document.

Special folders: exact duplicates go to `_Duplicates` / `_Дубликаты` (`special_folders.duplicates`).

## Things the rules do not decide

- **Date subfolders** come from `plan --by-date year|month`, not from the rules. The date
  used is EXIF capture time when present, else `created`, else `modified`.
- **Duplicates** are detected by identical `md5` *and* `size` (empty files are ignored). The
  kept copy is the one without a copy suffix (`(1)`, `копия`, `copy`), then the shortest
  name, then the earliest `created`. Files whose names differ only by a copy suffix but whose
  content differs are reported as "look-alikes" and sorted normally.
- **Age** is reported (files untouched for 180+ days) but never acted on by default. To
  archive old files, write a rule file with the categories you want and use
  `--by-date year`, or ask for a separate pass with `--only`.

## Writing a custom rules file

Minimal example that adds a `Receipts` category, moves everything else as usual, and keeps
English names even on a Russian Disk:

```json
{
  "version": 1,
  "categories": [
    {"id": "receipts", "folder": {"en": "Receipts", "ru": "Чеки"},
     "name_regex": ["(^|[^a-zа-яё])(receipt|чек)($|[^a-zа-яё])"]},
    {"id": "images", "folder": {"en": "Images", "ru": "Изображения"},
     "extensions": ["jpg", "jpeg", "png", "gif", "webp", "heic"]},
    {"id": "documents", "folder": {"en": "Documents", "ru": "Документы"},
     "extensions": ["pdf", "doc", "docx", "txt"]}
  ],
  "media_type_fallback": {"image": "images", "document": "documents"},
  "skip": {"extensions": ["crdownload", "part", "download", "tmp"], "name_regex": ["^~\\$", "^\\."]},
  "special_folders": {"duplicates": {"en": "_Duplicates", "ru": "_Дубликаты"}}
}
```

Run it with `python scripts/downloads_sort.py --rules my-rules.json --names en plan`.
A missing `other` category is added automatically. Folder values may be nested
(`"Documents/Finance"`); the folders are created on `apply`, parents first.
