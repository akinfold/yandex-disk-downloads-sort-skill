#!/usr/bin/env python3
"""Analyze and sort the Downloads folder on Yandex Disk.

    python downloads_sort.py check                 # token OK? where is Downloads? how big?
    python downloads_sort.py analyze               # inventory.json + report.md (printed)
    python downloads_sort.py plan [--by-date year] # plan.json from the inventory, no network
    python downloads_sort.py apply --yes           # create folders + move files, write a journal
    python downloads_sort.py undo --yes            # move everything back using the journal

Guarantees that hold for every command:

* Nothing is deleted and nothing is overwritten. A name clash gets a " (2)" suffix.
* Only files directly inside Downloads are touched; subfolders are left alone.
* Partial downloads, lock files and files modified minutes ago are skipped.
* Before a file is moved, apply checks it is still the file the plan saw (same
  resource id, or same md5 and size); anything that changed is left alone.
* Every move is written to an append-only journal *before* it is sent and its
  outcome right after, so ``undo`` works even after an interrupted run.

The OAuth token is read from ``YANDEX_DISK_TOKEN`` (or ``YANDEX_DISK_OAUTH_TOKEN``,
or ``--token-file``) and is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import classify as cls  # noqa: E402
from yadisk_api import (  # noqa: E402
    ERR_PARENT_MISSING,
    YandexDisk,
    YandexDiskError,
    join_path,
    load_token,
    normalize_path,
    parent_of,
    with_suffix,
)

DEFAULT_WORKDIR = os.path.join(tempfile.gettempdir(), "yandex-disk-downloads-sort")
ITEM_FIELDS = ("name", "path", "type", "size", "created", "modified", "media_type", "mime_type", "md5", "sha256", "exif", "resource_id")
FIELDS = ",".join(f"_embedded.items.{f}" for f in ITEM_FIELDS) + ",_embedded.total,_embedded.limit,_embedded.offset,name,path,type"
STATE_FIELDS = ",".join(f"_embedded.items.{f}" for f in ("path", "type", "md5", "size", "resource_id")) + ",_embedded.total,_embedded.limit,_embedded.offset"
STALE_DAYS = 180
MAX_CONSECUTIVE_FAILURES = 5
SUFFIX_ATTEMPTS = 50
VERBOSE_FIRST = 40      # apply/undo print every move up to this many...
PROGRESS_EVERY = 50     # ...then one progress line per this many (agent harnesses truncate long output)
SUMMARY_ROWS = 40       # plan summary table rows; the rest is in plan.json
MERGE_MAX_DEPTH = 16    # refuse to recurse forever when merging a partially moved folder
FOLDER_SCAN_FIELDS = ",".join(f"_embedded.items.{f}" for f in ("name", "path", "type", "size", "media_type", "md5", "resource_id")) + ",_embedded.total,_embedded.limit,_embedded.offset"
JOURNAL_VERSION = 2


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def say(text: str = "") -> None:
    print(text, flush=True)


def write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def md_escape(text: str) -> str:
    return str(text).replace("|", "\\|")


def short(path: str) -> str:
    return path.rsplit("/", 1)[-1]


# -- journal (append-only JSON lines) ------------------------------------------


def new_journal_path(workdir: str, explicit: Optional[str] = None) -> str:
    """Allocate a journal file that no other run can share (exclusive create, microsecond stamp)."""
    if explicit:
        os.makedirs(os.path.dirname(os.path.abspath(explicit)) or ".", exist_ok=True)
        fd = os.open(explicit, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return explicit
    os.makedirs(workdir, exist_ok=True)
    while True:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = os.path.join(workdir, f"journal-{stamp}.jsonl")
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            continue


def journal_append(path: str, event: Dict[str, Any]) -> None:
    event = dict(event, time=now_iso())
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()


def load_journal(path: str) -> Dict[str, Any]:
    """Fold the event lines into header, created folders and per-move entries."""
    header: Dict[str, Any] = {}
    folders: List[str] = []
    entries: Dict[int, Dict[str, Any]] = {}
    merged: Dict[int, List[Dict[str, Any]]] = {}
    finished = False
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue  # a torn last line after a crash
            kind = event.get("type")
            if kind == "header":
                header = event
            elif kind == "folder":
                folders.append(event["path"])
            elif kind == "folder_removed":
                if event["path"] in folders:
                    folders.remove(event["path"])
            elif kind == "pending":
                entry = {k: v for k, v in event.items() if k not in ("type", "time")}
                entry["status"] = "pending"
                entry["undone"] = False
                entries[event["idx"]] = entry
            elif kind == "result" and event.get("idx") in entries:
                entry = entries[event["idx"]]
                entry.update({k: v for k, v in event.items() if k not in ("type", "time", "idx")})
                entry["actual_to"] = event.get("to", entry.get("to"))
            elif kind == "merged":
                # A merge moved items one by one; undo has to put them back the same way,
                # because the destination folder may hold things that were always there.
                merged.setdefault(event["idx"], []).append(
                    {k: v for k, v in event.items() if k not in ("type", "time")}
                )
            elif kind == "merged_undone":
                for record in merged.get(event.get("idx"), []):
                    if record.get("to") == event.get("to"):
                        record["undone"] = True
            elif kind == "undone" and event.get("idx") in entries:
                entries[event["idx"]]["undone"] = True
                entries[event["idx"]]["restored_to"] = event.get("restored_to")
            elif kind == "undo_error" and event.get("idx") in entries:
                entries[event["idx"]]["undo_error"] = event.get("error")
            elif kind in ("finished", "aborted", "interrupted"):
                finished = True
    for idx, records in merged.items():
        if idx in entries:
            entries[idx]["merged_children"] = records
    return {
        "path": path,
        "header": header,
        "downloads": header.get("downloads"),
        "folders_created": folders,
        "entries": [entries[k] for k in sorted(entries)],
        "finished": finished,
    }


def undoable(entry: Dict[str, Any]) -> bool:
    return entry.get("status") in ("moved", "pending") and not entry.get("undone")


def journals_with_pending_moves(workdir: str) -> List[str]:
    """Newest first: every journal in the workdir that still has un-undone moves."""
    if not os.path.isdir(workdir):
        return []
    out = []
    for name in sorted(os.listdir(workdir), reverse=True):
        if not (name.startswith("journal-") and name.endswith(".jsonl")):
            continue
        path = os.path.join(workdir, name)
        try:
            entries = load_journal(path)["entries"]
        except OSError:
            continue
        if any(undoable(e) for e in entries):
            out.append(path)
    return out


def latest_journal(workdir: str) -> Optional[str]:
    if not os.path.isdir(workdir):
        return None
    names = sorted(n for n in os.listdir(workdir) if n.startswith("journal-") and n.endswith(".jsonl"))
    return os.path.join(workdir, names[-1]) if names else None


# -- check -------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    disk = YandexDisk(load_token(args.token_file))
    info = disk.disk_info()
    path = normalize_path(args.path) if args.path else disk.downloads_path()
    files = folders = 0
    total = 0
    for item in disk.iter_children(path, fields="_embedded.items.type,_embedded.items.size,_embedded.total,_embedded.limit,_embedded.offset"):
        if item.get("type") == "dir":
            folders += 1
        else:
            files += 1
            total += int(item.get("size") or 0)
    result = {
        "ok": True,
        "downloads": path,
        "files": files,
        "subfolders": folders,
        "files_size": total,
        "files_size_human": cls.human_size(total),
        "disk_used": info.get("used_space"),
        "disk_total": info.get("total_space"),
        "trash_size": info.get("trash_size"),
        "system_folders": info.get("system_folders"),
        "requests": disk.requests_made,
    }
    if args.json:
        say(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        say(f"Token OK. Downloads folder: {path}")
        say(f"Files: {files} ({cls.human_size(total)}), subfolders: {folders} (never touched)")
        say(f"Disk: {cls.human_size(info.get('used_space'))} used of {cls.human_size(info.get('total_space'))}, trash {cls.human_size(info.get('trash_size'))}")
    return 0


# -- analyze -----------------------------------------------------------------


def list_top_level(disk: YandexDisk, path: str, fields: str) -> List[Dict[str, Any]]:
    """Direct children, deduplicated by path (a folder changing under pagination can repeat an item)."""
    seen = set()
    items = []
    for item in disk.iter_children(path, fields=fields):
        key = item.get("path")
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def cmd_analyze(args: argparse.Namespace) -> int:
    disk = YandexDisk(load_token(args.token_file))
    info = disk.disk_info()
    path = normalize_path(args.path) if args.path else disk.downloads_path()
    lang = cls.detect_lang(path, args.names)
    rules = cls.load_rules(args.rules)

    items = list_top_level(disk, path, FIELDS)
    files = [dict(i) for i in items if i.get("type") == "file"]
    for item in files:
        verdict = cls.classify(item, rules, lang)
        item["verdict"] = verdict.as_dict()
    folders = [] if args.folders == "skip" else scan_folders(disk, items, rules, lang)
    duplicates = cls.find_exact_duplicates(files)
    lookalikes = cls.find_name_lookalikes(files)

    inventory = {
        "version": 1,
        "generated": now_iso(),
        "downloads": path,
        "lang": lang,
        "rules": os.path.abspath(args.rules) if args.rules else "default",
        "disk": {k: info.get(k) for k in ("total_space", "used_space", "trash_size")},
        "folders": folders,
        "folder_names": [i.get("name") for i in items if i.get("type") == "dir"],
        "files": files,
        "duplicates": [
            {"md5": g["md5"], "size": g["size"], "keep": g["keep"]["path"], "extra": [e["path"] for e in g["extra"]]}
            for g in duplicates
        ],
        "lookalikes": [{"base": g["base"], "paths": [i["path"] for i in g["items"]]} for g in lookalikes],
    }
    os.makedirs(args.workdir, exist_ok=True)
    inv_path = os.path.join(args.workdir, "inventory.json")
    write_json(inv_path, inventory)
    report = build_report(inventory, rules)
    report_path = os.path.join(args.workdir, "report.md")
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(report)
    say(report)
    say(f"\nInventory: {inv_path}\nReport: {report_path}\nRequests made: {disk.requests_made}")
    return 0


def scan_folders(disk: YandexDisk, items: List[Dict[str, Any]], rules: cls.Rules, lang: str) -> List[Dict[str, Any]]:
    """Look inside each subfolder far enough to say what kind of folder it is."""
    conf = rules.folder_rules
    max_items = int(conf.get("max_scan_items", 400) or 400)
    max_depth = int(conf.get("max_depth", 3) or 3)
    out = []
    for item in items:
        if item.get("type") != "dir":
            continue
        contents, truncated = scan_tree(disk, item["path"], max_items, max_depth)
        verdict = cls.classify_folder(item, contents, rules, lang)
        record = dict(item)
        record["verdict"] = verdict.as_dict()
        record["scanned"] = len(contents)
        record["truncated"] = truncated
        out.append(record)
    return out


def scan_tree(disk: YandexDisk, path: str, max_items: int, max_depth: int) -> Tuple[List[Dict[str, Any]], bool]:
    """Breadth-first listing of a folder, bounded so one huge folder cannot stall a run."""
    found: List[Dict[str, Any]] = []
    queue = [(path, 0)]
    truncated = False
    while queue:
        current, depth = queue.pop(0)
        try:
            children = list(disk.iter_children(current, fields=FOLDER_SCAN_FIELDS))
        except YandexDiskError:
            truncated = True
            continue
        for child in children:
            if len(found) >= max_items:
                return found, True
            if child.get("type") == "dir":
                if depth + 1 < max_depth:
                    queue.append((child["path"], depth + 1))
                else:
                    truncated = True
            else:
                found.append(child)
    return found, truncated


def build_report(inv: Dict[str, Any], rules: cls.Rules) -> str:
    files: List[Dict[str, Any]] = inv["files"]
    lang = inv["lang"]
    total_size = sum(int(f.get("size") or 0) for f in files)
    lines: List[str] = []
    lines.append("# Downloads on Yandex Disk: analysis")
    lines.append("")
    lines.append(f"- Folder: `{inv['downloads']}`")
    folder_records = [f for f in (inv.get("folders") or []) if isinstance(f, dict)]
    subfolder_count = len(inv.get("folder_names") or folder_records)
    if folder_records:
        movable = [f for f in folder_records if not (f["verdict"].get("skip"))]
        lines.append(f"- Files: {len(files)} ({cls.human_size(total_size)}); subfolders: {subfolder_count} ({len(movable)} of them will be sorted too)")
    else:
        lines.append(f"- Files: {len(files)} ({cls.human_size(total_size)}); subfolders: {subfolder_count} (left untouched)")
    disk = inv.get("disk") or {}
    if disk.get("total_space"):
        lines.append(f"- Disk: {cls.human_size(disk.get('used_space'))} used of {cls.human_size(disk.get('total_space'))}, trash {cls.human_size(disk.get('trash_size'))}")
    lines.append(f"- Folder names: {'Russian' if lang == 'ru' else 'English'} (override with --names)")
    lines.append(f"- Generated: {inv['generated']}")
    lines.append("")

    by_folder: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    skipped: List[Dict[str, Any]] = []
    sensitive: List[Dict[str, Any]] = []
    for f in files:
        v = f["verdict"]
        if v.get("skip"):
            skipped.append(f)
            continue
        if v.get("sensitive"):
            sensitive.append(f)
        bucket = by_folder.setdefault(v["folder"], {"category": v["category"], "count": 0, "size": 0, "names": []})
        bucket["count"] += 1
        bucket["size"] += int(f.get("size") or 0)
        if len(bucket["names"]) < 3:
            bucket["names"].append(f["name"])

    lines.append("## Proposed categories")
    lines.append("")
    if by_folder:
        lines.append("| Target folder | Category | Files | Size | Examples |")
        lines.append("|---|---|---:|---:|---|")
        rows = sorted(by_folder.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        for folder, b in rows[:SUMMARY_ROWS]:
            examples = ", ".join(f"`{md_escape(n)}`" for n in b["names"])
            lines.append(f"| `{md_escape(folder)}` | {b['category']} | {b['count']} | {cls.human_size(b['size'])} | {examples} |")
        if len(rows) > SUMMARY_ROWS:
            rest = rows[SUMMARY_ROWS:]
            lines.append(f"| ... and {len(rest)} more folder(s) | | {sum(b['count'] for _, b in rest)} | {cls.human_size(sum(b['size'] for _, b in rest))} | full list in inventory.json |")
    else:
        lines.append("Nothing to sort: no loose files in the folder.")
    lines.append("")

    lines.append("## Attention")
    lines.append("")
    notes = 0
    if skipped:
        notes += 1
        names = ", ".join(f"`{md_escape(f['name'])}`" for f in skipped[:8])
        more = f" and {len(skipped) - 8} more" if len(skipped) > 8 else ""
        lines.append(f"- {len(skipped)} file(s) will be skipped (partial downloads / lock files): {names}{more}")
    dups = inv.get("duplicates") or []
    if dups:
        notes += 1
        extra = sum(len(g["extra"]) for g in dups)
        reclaim = sum(g["size"] * len(g["extra"]) for g in dups)
        dup_folder = cls.special_folder(rules, "duplicates", lang)
        lines.append(f"- Exact duplicates: {len(dups)} group(s), {extra} redundant file(s), {cls.human_size(reclaim)} reclaimable. The plan moves the copies to `{dup_folder}` and keeps one original each.")
        for g in dups[:10]:
            extras = ", ".join(f"`{md_escape(short(p))}`" for p in g["extra"])
            lines.append(f"  - keep `{md_escape(short(g['keep']))}`; copies: {extras} ({cls.human_size(g['size'])} each)")
        if len(dups) > 10:
            lines.append(f"  - ... and {len(dups) - 10} more group(s) in inventory.json")
    looks = inv.get("lookalikes") or []
    if looks:
        notes += 1
        lines.append(f"- Look-alike names with different content (not duplicates, review by hand): {len(looks)} group(s)")
        for g in looks[:8]:
            lines.append("  - " + ", ".join(f"`{md_escape(short(p))}`" for p in g["paths"][:6]))
    if sensitive:
        notes += 1
        names = ", ".join(f"`{md_escape(f['name'])}`" for f in sensitive[:8])
        lines.append(f"- {len(sensitive)} key/certificate file(s) found (sensitive, consider moving out of the cloud): {names}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)
    stale = [f for f in files if (cls.parse_time(f.get("modified")) or datetime.now(timezone.utc)) < cutoff]
    if stale:
        notes += 1
        lines.append(f"- {len(stale)} file(s) untouched for more than {STALE_DAYS} days ({cls.human_size(sum(int(f.get('size') or 0) for f in stale))}); candidates for archiving or deletion, the plan only sorts them")
    recent = [f for f in files if (cls.parse_time(f.get("modified")) or cutoff) > datetime.now(timezone.utc) - timedelta(days=7)]
    if recent:
        lines.append(f"- {len(recent)} file(s) added in the last 7 days")
    if not notes:
        lines.append("- Nothing unusual.")
    lines.append("")

    if folder_records:
        lines.append("## Subfolders")
        lines.append("")
        lines.append("| Folder | Files inside | Goes to | Why |")
        lines.append("|---|---:|---|---|")
        for rec in folder_records[:SUMMARY_ROWS]:
            v = rec["verdict"]
            destination = "stays" if v.get("skip") else f"`{md_escape(v['folder'])}`"
            note = v["reason"] + (", listing truncated" if rec.get("truncated") else "")
            lines.append(f"| `{md_escape(rec['name'])}` | {v.get('files', 0)} | {destination} | {note} |")
        if len(folder_records) > SUMMARY_ROWS:
            lines.append(f"| ... and {len(folder_records) - SUMMARY_ROWS} more | | | full list in inventory.json |")
        lines.append("")

    largest = sorted(files, key=lambda f: -int(f.get("size") or 0))[:10]
    if largest:
        lines.append("## Largest files")
        lines.append("")
        lines.append("| File | Size | Category |")
        lines.append("|---|---:|---|")
        for f in largest:
            lines.append(f"| `{md_escape(f['name'])}` | {cls.human_size(f.get('size'))} | {f['verdict']['category']} |")
        lines.append("")

    oldest = sorted((f for f in files if f.get("modified")), key=lambda f: str(f.get("modified")))[:10]
    if oldest:
        lines.append("## Oldest files")
        lines.append("")
        lines.append("| File | Modified | Category |")
        lines.append("|---|---|---|")
        for f in oldest:
            lines.append(f"| `{md_escape(f['name'])}` | {str(f.get('modified'))[:10]} | {f['verdict']['category']} |")
        lines.append("")

    ext_counts: Dict[str, int] = {}
    for f in files:
        ext = f["verdict"].get("extension") or "(none)"
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
    if ext_counts:
        top = sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
        lines.append("## Extensions")
        lines.append("")
        lines.append(", ".join(f"`{e}` x{n}" for e, n in top))
        lines.append("")
    return "\n".join(lines)


# -- plan --------------------------------------------------------------------


def ancestors_within(folder: str, root: str) -> List[str]:
    """``disk:/D/A/B/C`` under ``disk:/D`` -> [``disk:/D/A``, ``disk:/D/A/B``, ``disk:/D/A/B/C``]."""
    root = normalize_path(root)
    chain = []
    current = normalize_path(folder)
    while current and current != root and current.startswith(root + "/"):
        chain.append(current)
        current = parent_of(current)
    return list(reversed(chain))


def cmd_plan(args: argparse.Namespace) -> int:
    inv_path = args.inventory or os.path.join(args.workdir, "inventory.json")
    if not os.path.isfile(inv_path):
        say(f"No inventory at {inv_path}. Run `analyze` first.")
        return 2
    inv = read_json(inv_path)
    rules = cls.load_rules(args.rules or (None if inv.get("rules") in (None, "default") else inv.get("rules")))
    lang = inv["lang"] if args.names == "auto" else args.names
    downloads = inv["downloads"]
    only = {c.strip() for c in (args.only or "").split(",") if c.strip()}
    exclude = {c.strip() for c in (args.exclude or "").split(",") if c.strip()}
    min_age = timedelta(minutes=args.min_age_minutes)
    now = datetime.now(timezone.utc)
    file_paths = {f["path"] for f in inv["files"]}

    dup_of: Dict[str, str] = {}
    if args.duplicates == "quarantine":
        for group in inv.get("duplicates") or []:
            for extra in group["extra"]:
                dup_of[extra] = short(group["keep"])
    dup_folder = cls.special_folder(rules, "duplicates", lang)
    generic_folder = cls.folder_for(rules.by_id["folders"], lang)

    moves: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    folders: "OrderedDict[str, None]" = OrderedDict()
    summary: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    for f in inv["files"]:
        # Re-classify so a changed --names/--rules takes effect without re-fetching.
        verdict = cls.classify(f, rules, lang)
        name = f["name"]
        if verdict.skip:
            skipped.append({"path": f["path"], "reason": verdict.reason})
            continue
        if only and verdict.category not in only:
            skipped.append({"path": f["path"], "reason": f"category {verdict.category} not in --only"})
            continue
        if verdict.category in exclude:
            skipped.append({"path": f["path"], "reason": f"category {verdict.category} excluded"})
            continue
        if args.keep_other and verdict.category == "other":
            skipped.append({"path": f["path"], "reason": "unclassified, kept in place (--keep-other)"})
            continue
        modified = cls.parse_time(f.get("modified"))
        if modified and now - modified < min_age:
            skipped.append({"path": f["path"], "reason": f"modified less than {args.min_age_minutes} min ago (may still be syncing)"})
            continue
        if f["path"] in dup_of:
            folder = dup_folder
            reason = f"duplicate of {dup_of[f['path']]}"
            category = "duplicate"
        else:
            folder = verdict.folder
            bucket = cls.date_bucket(f, args.by_date)
            if bucket:
                folder = f"{folder}/{bucket}"
            reason = verdict.reason
            category = verdict.category
        target_folder = join_path(downloads, folder)
        target = join_path(target_folder, name)
        if normalize_path(target) == normalize_path(f["path"]):
            skipped.append({"path": f["path"], "reason": "already in place"})
            continue
        chain = ancestors_within(target_folder, downloads)
        taken = next((p for p in chain if p in file_paths), None)
        if taken:
            skipped.append({"path": f["path"], "reason": f"target folder name is taken by a file: {short(taken)}"})
            continue
        for ancestor in chain:
            folders[ancestor] = None
        moves.append({
            "from": f["path"],
            "to": target,
            "folder": folder,
            "category": category,
            "reason": reason,
            "size": int(f.get("size") or 0),
            "md5": f.get("md5"),
            "resource_id": f.get("resource_id"),
            "modified": f.get("modified"),
            "sensitive": verdict.sensitive,
        })
        entry = summary.setdefault(folder, {"count": 0, "size": 0, "examples": []})
        entry["count"] += 1
        entry["size"] += int(f.get("size") or 0)
        if len(entry["examples"]) < 3:
            entry["examples"].append(name)

    # Folders come after files so that a folder cannot be moved out from under a file the
    # plan already accounted for, and so the destination folders exist by then.
    # Every folder a category could ever occupy is a destination, whether or not this run
    # happens to fill it. Otherwise a tidy folder would be moved into itself the moment a
    # run has nothing else to do: Документы -> Документы/Документы.
    target_folders = {normalize_path(p) for p in folders}
    for cat in rules.categories:
        target_folders.add(normalize_path(join_path(downloads, cls.folder_for(cat, lang))))
    for rule in rules.name_rules:
        if rule.get("folder"):
            target_folders.add(normalize_path(join_path(downloads, cls.folder_for(rule, lang))))
    target_folders.add(normalize_path(join_path(downloads, dup_folder)))
    for rec in inv.get("folders") or []:
        if not isinstance(rec, dict):
            continue  # inventory from an older run listed names only
        verdict = cls.classify_folder(rec, [], rules, lang) if "verdict" not in rec else None
        v = rec.get("verdict") or (verdict.as_dict() if verdict else {})
        name, source = rec.get("name") or short(rec["path"]), rec["path"]
        if args.folders == "skip":
            continue
        if v.get("skip"):
            skipped.append({"path": source, "reason": v.get("reason", "left alone")})
            continue
        folder = generic_folder if args.folders == "group" else v["folder"]
        target_folder = join_path(downloads, folder)
        target = join_path(target_folder, name)
        if normalize_path(source) in target_folders or normalize_path(source) == normalize_path(target_folder):
            skipped.append({"path": source, "reason": "this folder is one of the sorting destinations"})
            continue
        if normalize_path(target) == normalize_path(source):
            skipped.append({"path": source, "reason": "already in place"})
            continue
        if normalize_path(target_folder).startswith(normalize_path(source) + "/"):
            skipped.append({"path": source, "reason": "cannot move a folder inside itself"})
            continue
        chain = ancestors_within(target_folder, downloads)
        taken = next((p for p in chain if p in file_paths), None)
        if taken:
            skipped.append({"path": source, "reason": f"target folder name is taken by a file: {short(taken)}"})
            continue
        for ancestor in chain:
            folders[ancestor] = None
        moves.append({
            "from": source,
            "to": target,
            "folder": folder,
            "category": v.get("category", "folders"),
            "reason": v.get("reason", ""),
            "size": int(v.get("size") or 0),
            "kind": "dir",
            "files_inside": v.get("files", 0),
            "sensitive": False,
        })
        entry = summary.setdefault(folder, {"count": 0, "size": 0, "examples": []})
        entry["count"] += 1
        entry["size"] += int(v.get("size") or 0)
        if len(entry["examples"]) < 3:
            entry["examples"].append(name + "/")

    truncated = 0
    if args.max_moves and len(moves) > args.max_moves:
        truncated = len(moves) - args.max_moves
        moves = moves[: args.max_moves]
        folders = OrderedDict()
        for m in moves:
            for ancestor in ancestors_within(parent_of(m["to"]), downloads):
                folders[ancestor] = None

    # Parents first, so apply can create them in order.
    ordered_folders = sorted(folders, key=lambda p: (p.count("/"), p))

    plan = {
        "version": 1,
        "created": now_iso(),
        "downloads": downloads,
        "lang": lang,
        "inventory": os.path.abspath(inv_path),
        "options": {
            "by_date": args.by_date,
            "duplicates": args.duplicates,
            "keep_other": args.keep_other,
            "min_age_minutes": args.min_age_minutes,
            "only": sorted(only),
            "exclude": sorted(exclude),
            "max_moves": args.max_moves,
            "folders": args.folders,
        },
        "folders": ordered_folders,
        "moves": moves,
        "skipped": skipped,
        "truncated": truncated,
    }
    plan_path = args.output or os.path.join(args.workdir, "plan.json")
    write_json(plan_path, plan)

    say(render_plan_summary(plan, summary))
    say(f"\nPlan: {plan_path}")
    if moves:
        say("Next: review with the user, then run `apply --yes` to execute.")
    return 0


def render_plan_summary(plan: Dict[str, Any], summary: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    moves = plan["moves"]
    if summary is None:
        summary = OrderedDict()
        for m in moves:
            entry = summary.setdefault(m["folder"], {"count": 0, "size": 0, "examples": []})
            entry["count"] += 1
            entry["size"] += int(m.get("size") or 0)
            if len(entry["examples"]) < 3:
                entry["examples"].append(short(m["from"]))
    lines = ["# Sorting plan", ""]
    lines.append(f"- Downloads: `{plan['downloads']}`")
    lines.append(f"- Moves: {len(moves)} file(s), {cls.human_size(sum(int(m.get('size') or 0) for m in moves))}, into {len(plan['folders'])} folder(s)")
    lines.append(f"- Skipped: {len(plan['skipped'])} file(s)")
    opts = plan.get("options") or {}
    lines.append(f"- Options: by-date={opts.get('by_date')}, duplicates={opts.get('duplicates')}, keep-other={opts.get('keep_other')}")
    if plan.get("truncated"):
        lines.append(f"- NOTE: plan limited by --max-moves; {plan['truncated']} more file(s) would move on a later run")
    lines.append("")
    if summary:
        lines.append("| Target folder (inside Downloads) | Files | Size | Examples |")
        lines.append("|---|---:|---:|---|")
        rows = sorted(summary.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        for folder, e in rows[:SUMMARY_ROWS]:
            examples = ", ".join(f"`{md_escape(n)}`" for n in e["examples"])
            lines.append(f"| `{md_escape(folder)}` | {e['count']} | {cls.human_size(e['size'])} | {examples} |")
        if len(rows) > SUMMARY_ROWS:
            rest = rows[SUMMARY_ROWS:]
            lines.append(f"| ... and {len(rest)} more folder(s) | {sum(e['count'] for _, e in rest)} | {cls.human_size(sum(e['size'] for _, e in rest))} | full list in plan.json |")
    else:
        lines.append("Nothing to move.")
    sensitive = [m for m in moves if m.get("sensitive")]
    if sensitive:
        lines.append("")
        lines.append(f"Sensitive files in the plan (keys/certificates): {len(sensitive)}. They are only moved, never uploaded or read.")
    reasons: Dict[str, int] = {}
    for s in plan["skipped"]:
        key = s["reason"].split(" (")[0].split(": ")[0]
        reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        lines.append("")
        lines.append("Skipped: " + "; ".join(f"{n} x {r}" for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])))
    return "\n".join(lines)


# -- moving folders ------------------------------------------------------------
#
# A folder move is deferred: the API answers 202 and finishes in the background. That
# background half can fail after moving part of the tree, and it refuses outright when
# something already sits at the destination. So a folder move here is never "fire and
# forget": whatever the operation reports, the result is checked against the disk, and
# anything left behind is carried over item by item and merged into the destination.


def move_folder(disk: YandexDisk, source: str, target: str, journal, idx: int, verbose: bool = False) -> Dict[str, Any]:
    """Move a folder, then make sure it really got there. Returns a result dict."""
    moved_children: List[Dict[str, Any]] = []

    if disk.exists(target):
        # The server would answer 409 rather than merge, so merge deliberately.
        result = merge_folder(disk, source, target, journal, idx, moved_children, verbose)
        result["merged"] = True
        return result

    try:
        outcome = disk.move_deferred(source, target, overwrite=False)
    except YandexDiskError as exc:
        if exc.status == 404:
            return {"status": "missing", "to": target, "error": exc.message}
        if exc.status == 409 and exc.code == ERR_PARENT_MISSING:
            disk.mkdir(parent_of(target))
            try:
                outcome = disk.move_deferred(source, target, overwrite=False)
            except YandexDiskError as retry_exc:
                return {"status": "failed", "to": target, "error": str(retry_exc), "code": retry_exc.code}
        else:
            return {"status": "failed", "to": target, "error": str(exc), "code": exc.code}

    if outcome.get("status") == "success" and not disk.exists(source):
        return {"status": "moved", "to": target, "kind": "dir", "whole": True}

    # Either the operation failed, timed out, or claimed success while the source is still
    # there. Trust the disk, not the report.
    state = outcome.get("status", "unknown")
    if not disk.exists(source):
        return {"status": "moved", "to": target, "kind": "dir", "whole": True, "operation": state}
    if not disk.exists(target):
        return {"status": "failed", "to": target, "error": f"the deferred move ended as {state} and nothing was moved"}

    result = merge_folder(disk, source, target, journal, idx, moved_children, verbose)
    result["partial_recovered"] = True
    result["operation"] = state
    return result


def merge_folder(
    disk: YandexDisk,
    source: str,
    target: str,
    journal,
    idx: int,
    moved_children: List[Dict[str, Any]],
    verbose: bool = False,
    depth: int = 0,
) -> Dict[str, Any]:
    """Carry everything left in ``source`` over into ``target``, one item at a time.

    Each child is journaled on its own, because undoing a merge means putting those items
    back where they came from — moving the whole folder back would drag along whatever was
    already living at the destination.
    """
    if depth > MERGE_MAX_DEPTH:
        return {"status": "failed", "to": target, "error": f"folder nesting deeper than {MERGE_MAX_DEPTH} levels"}

    try:
        if disk.mkdir(target) == "created":
            journal_append(journal, {"type": "folder", "path": target})
    except YandexDiskError as exc:
        return {"status": "failed", "to": target, "error": str(exc), "code": exc.code}

    failures: List[str] = []
    try:
        children = list_top_level(disk, source, STATE_FIELDS + ",_embedded.items.name")
    except YandexDiskError as exc:
        return {"status": "failed", "to": target, "error": f"cannot list {source}: {exc}"}

    for child in children:
        name = child.get("name") or short(child["path"])
        child_target = join_path(target, name)
        if child.get("type") == "dir":
            if disk.exists(child_target):
                sub = merge_folder(disk, child["path"], child_target, journal, idx, moved_children, verbose, depth + 1)
            else:
                sub = move_folder(disk, child["path"], child_target, journal, idx, verbose)
                if sub["status"] == "moved" and sub.get("whole"):
                    record = {"from": child["path"], "to": child_target, "kind": "dir"}
                    moved_children.append(record)
                    journal_append(journal, dict(record, type="merged", idx=idx))
            if sub["status"] != "moved":
                failures.append(f"{name}: {sub.get('error', sub['status'])}")
            elif verbose:
                say(f"      merged {name}/")
            continue
        outcome = _move_with_fallbacks(disk, child["path"], child_target, identity_of(child))
        if outcome["status"] == "moved":
            record = {"from": child["path"], "to": outcome["to"], "size": child.get("size"),
                      "md5": child.get("md5"), "resource_id": child.get("resource_id")}
            moved_children.append(record)
            journal_append(journal, dict(record, type="merged", idx=idx, renamed=outcome.get("renamed", False)))
            if verbose:
                say(f"      merged {name}")
        elif outcome["status"] == "missing":
            continue  # gone from under us; nothing to carry
        else:
            failures.append(f"{name}: {outcome.get('error')}")

    if failures:
        return {"status": "failed", "to": target, "error": "; ".join(failures[:3]),
                "moved_children": len(moved_children)}

    # The source should be empty now. Removing it is finishing the move the server started,
    # not a deletion of the user's data — and it goes to the bin, which is recoverable.
    emptied = remove_if_empty(disk, source)
    return {"status": "moved", "to": target, "kind": "dir", "whole": False,
            "moved_children": len(moved_children), "source_removed": emptied}


def remove_if_empty(disk: YandexDisk, path: str) -> bool:
    try:
        meta = disk.get(path, limit=1, fields="_embedded.total,path,type")
    except YandexDiskError:
        return False
    if meta.get("type") != "dir" or ((meta.get("_embedded") or {}).get("total") or 0) != 0:
        return False
    try:
        disk.delete(path, permanently=False)
        return True
    except YandexDiskError:
        return False


# -- apply -------------------------------------------------------------------


def identity_of(item: Dict[str, Any]) -> Dict[str, Any]:
    return {"md5": item.get("md5"), "size": item.get("size"), "resource_id": item.get("resource_id")}


def same_file(meta: Dict[str, Any], identity: Dict[str, Any]) -> bool:
    """Is ``meta`` the file the plan saw? resource_id is authoritative; md5+size is the fallback."""
    if meta.get("type") not in (None, "file"):
        return False
    if identity.get("resource_id") and meta.get("resource_id"):
        return meta["resource_id"] == identity["resource_id"]
    if identity.get("md5"):
        return meta.get("md5") == identity["md5"] and int(meta.get("size") or 0) == int(identity.get("size") or 0)
    return False


def landed(disk: YandexDisk, source: str, dest: str, identity: Dict[str, Any]) -> bool:
    """After a lost response: did the move actually happen? Only trust an identity match."""
    if not identity.get("resource_id") and not identity.get("md5"):
        return False
    if disk.exists(source):
        return False
    try:
        meta = disk.get(dest, limit=0, fields="path,type,md5,size,resource_id")
    except YandexDiskError as exc:
        if exc.status == 404:
            return False
        raise
    return meta.get("type") == "file" and same_file(meta, identity)


def _move_with_fallbacks(disk: YandexDisk, source: str, target: str, identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Move, resolving name clashes with a numeric suffix and a missing parent with one mkdir."""
    folder = parent_of(target)
    name = short(target)
    dest = target
    parent_created = False
    identity = identity or {}
    for attempt in range(SUFFIX_ATTEMPTS):
        try:
            disk.move(source, dest, overwrite=False)
            return {"status": "moved", "to": dest, "renamed": dest != target}
        except YandexDiskError as exc:
            if exc.status == 404:
                if disk.last_retried and landed(disk, source, dest, identity):
                    return {"status": "moved", "to": dest, "renamed": dest != target, "recovered": True}
                return {"status": "missing", "to": dest, "error": exc.message}
            if exc.status == 409:
                if not parent_created and (exc.code == ERR_PARENT_MISSING or not disk.exists(folder)):
                    disk.mkdir(folder)
                    parent_created = True
                    continue
                if disk.exists(dest):
                    dest = join_path(folder, with_suffix(name, attempt + 2))
                    continue
            return {"status": "failed", "to": dest, "error": str(exc), "code": exc.code}
    return {"status": "failed", "to": dest, "error": "too many name clashes"}


def cmd_apply(args: argparse.Namespace) -> int:
    plan_path = args.plan or os.path.join(args.workdir, "plan.json")
    if not os.path.isfile(plan_path):
        say(f"No plan at {plan_path}. Run `plan` first.")
        return 2
    plan = read_json(plan_path)
    if not plan["moves"]:
        say("The plan has no moves. Nothing to do.")
        return 0
    if not args.yes:
        say(render_plan_summary(plan))
        say("\nDry run: nothing was changed. Re-run with --yes after the user confirms.")
        return 2

    disk = YandexDisk(load_token(args.token_file))
    downloads = plan["downloads"]
    try:
        current = {i["path"]: i for i in list_top_level(disk, downloads, STATE_FIELDS)}
    except YandexDiskError as exc:
        if exc.status == 404:
            say(f"Downloads folder {downloads} no longer exists; aborting.")
            return 1
        raise

    journal_path = new_journal_path(args.workdir, args.journal)
    journal_append(journal_path, {"type": "header", "version": JOURNAL_VERSION, "plan": os.path.abspath(plan_path), "downloads": downloads, "lang": plan.get("lang")})

    bad_folders: Dict[str, str] = {}
    for folder in plan["folders"]:
        if any(folder == b or folder.startswith(b + "/") for b in bad_folders):
            continue
        try:
            if disk.mkdir(folder) == "created":
                journal_append(journal_path, {"type": "folder", "path": folder})
                say(f"created  {folder}")
        except YandexDiskError as exc:
            bad_folders[folder] = str(exc)
            say(f"WARNING: could not create {folder}: {exc}")

    counts = {"moved": 0, "missing": 0, "changed": 0, "failed": 0}
    consecutive = 0
    total = len(plan["moves"])
    outcome = "finished"
    try:
        for index, move in enumerate(plan["moves"], 1):
            identity = identity_of(move)
            folder = parent_of(move["to"])
            before = current.get(move["from"])
            is_dir = move.get("kind") == "dir"
            if before is None:
                result: Dict[str, Any] = {"status": "missing", "to": move["to"], "error": "not in the folder any more"}
            elif is_dir and before.get("type") != "dir":
                result = {"status": "changed", "to": move["to"], "error": "a file now occupies that name"}
            elif not is_dir and not same_file(before, identity):
                result = {"status": "changed", "to": move["to"], "error": "content or type changed since the plan was made"}
            elif any(folder == b or folder.startswith(b + "/") for b in bad_folders):
                result = {"status": "failed", "to": move["to"], "error": "target folder could not be created"}
            else:
                journal_append(journal_path, dict(move, type="pending", idx=index - 1))
                try:
                    if move.get("kind") == "dir":
                        result = move_folder(disk, move["from"], move["to"], journal_path, index - 1, args.verbose)
                    else:
                        result = _move_with_fallbacks(disk, move["from"], move["to"], identity)
                except YandexDiskError as exc:
                    result = {"status": "failed", "to": move["to"], "error": str(exc), "code": exc.code}
                journal_append(journal_path, dict(result, type="result", idx=index - 1))
            if result["status"] in ("missing", "changed"):
                journal_append(journal_path, dict(move, type="pending", idx=index - 1))
                journal_append(journal_path, dict(result, type="result", idx=index - 1))
            counts[result["status"]] += 1
            label = {"moved": "moved   ", "missing": "missing ", "changed": "changed ", "failed": "FAILED  "}[result["status"]]
            suffix = " (renamed)" if result.get("renamed") else ""
            suffix += " (recovered after a lost response)" if result.get("recovered") else ""
            if result.get("merged"):
                suffix += f" (merged {result.get('moved_children', 0)} item(s) into an existing folder)"
            elif result.get("partial_recovered"):
                suffix += f" (deferred move ended {result.get('operation')}; carried over {result.get('moved_children', 0)} item(s) by hand)"
            detail = f" - {result.get('error')}" if result["status"] != "moved" else ""
            noteworthy = (result["status"] != "moved" or result.get("renamed") or result.get("recovered")
                          or result.get("merged") or result.get("partial_recovered"))
            if args.verbose or index <= VERBOSE_FIRST or noteworthy:
                say(f"[{index}/{total}] {label} {short(move['from'])} -> {move['folder']}{suffix}{detail}")
            elif index % PROGRESS_EVERY == 0 or index == total:
                say(f"[{index}/{total}] ... {counts['moved']} moved so far")
            if result["status"] == "failed":
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    say(f"Stopping after {consecutive} consecutive failures. Journal: {journal_path}")
                    outcome = "aborted"
                    break
            else:
                consecutive = 0
            if args.pause:
                time.sleep(args.pause)
    except KeyboardInterrupt:
        outcome = "interrupted"
        raise
    finally:
        journal_append(journal_path, {"type": outcome, "counts": counts})

    say("")
    say(f"Done: {counts['moved']} moved, {counts['missing']} missing (already gone), {counts['changed']} changed since the plan (left alone), {counts['failed']} failed. Requests made: {disk.requests_made}")
    say(f"Journal: {journal_path}  (undo with: downloads_sort.py undo --yes --journal \"{journal_path}\")")
    if outcome == "aborted":
        return 1
    return 0 if counts["failed"] == 0 else 1


# -- undo --------------------------------------------------------------------


def cmd_undo(args: argparse.Namespace) -> int:
    journal_path = args.journal or latest_journal(args.workdir)
    if not journal_path or not os.path.isfile(journal_path):
        say("No journal found. Pass --journal <file>.")
        return 2
    journal = load_journal(journal_path)
    pending = [e for e in journal["entries"] if undoable(e)]
    if not pending:
        say(f"Nothing to undo in {journal_path}.")
        others = [p for p in journals_with_pending_moves(args.workdir) if os.path.abspath(p) != os.path.abspath(journal_path)]
        if others:
            say("Other journals with moves that can still be undone (pass --journal <file>):")
            for path in others:
                say(f"  {path}")
        return 0
    if not args.yes:
        say(f"Would move {len(pending)} file(s) back to {journal['downloads']}. Re-run with --yes to do it.")
        return 2

    disk = YandexDisk(load_token(args.token_file))
    restored = failed = unresolved = 0
    total = len(pending)
    for index, entry in enumerate(reversed(pending), 1):
        identity = identity_of(entry)
        actual_to = entry.get("actual_to") or entry["to"]

        children = entry.get("merged_children") or []
        if children:
            # Undo a merge item by item: the destination folder may hold files that were
            # there before, and carrying the whole folder back would take them along.
            back = failed_back = 0
            for record in reversed(children):
                if record.get("undone"):
                    continue
                if record.get("kind") == "dir":
                    outcome = move_folder(disk, record["to"], record["from"], journal_path, entry["idx"], args.verbose)
                else:
                    outcome = _move_with_fallbacks(disk, record["to"], record["from"], identity_of(record))
                if outcome["status"] == "moved":
                    journal_append(journal_path, {"type": "merged_undone", "idx": entry["idx"], "to": record["to"]})
                    back += 1
                elif outcome["status"] == "missing":
                    journal_append(journal_path, {"type": "merged_undone", "idx": entry["idx"], "to": record["to"]})
                else:
                    failed_back += 1
            if failed_back:
                failed += 1
                say(f"[{index}/{total}] FAILED   {short(entry['from'])}: {failed_back} of {len(children)} item(s) would not go back")
            else:
                journal_append(journal_path, {"type": "undone", "idx": entry["idx"], "restored_to": entry["from"]})
                restored += 1
                if args.verbose or index <= VERBOSE_FIRST:
                    say(f"[{index}/{total}] restored {short(entry['from'])}/ ({back} item(s) carried back)")
            # Deepest first, so a folder emptied by its children can go too.
            created = journal.get("folders_created") or []
            for path in sorted((p for p in created if p == actual_to or p.startswith(actual_to + "/")),
                               key=lambda p: -p.count("/")):
                remove_if_empty(disk, path)
            remove_if_empty(disk, actual_to)
            continue

        if entry.get("kind") == "dir":
            outcome = move_folder(disk, actual_to, entry["from"], journal_path, entry["idx"], args.verbose)
            if outcome["status"] == "moved":
                journal_append(journal_path, {"type": "undone", "idx": entry["idx"], "restored_to": entry["from"]})
                restored += 1
                if args.verbose or index <= VERBOSE_FIRST:
                    say(f"[{index}/{total}] restored {short(actual_to)}/ -> {entry['from']}")
            else:
                journal_append(journal_path, {"type": "undo_error", "idx": entry["idx"], "error": outcome.get("error")})
                failed += 1
                say(f"[{index}/{total}] FAILED   {actual_to} - {outcome.get('error')}")
            continue
        if entry.get("status") == "pending":
            # The run died between sending the move and recording its outcome.
            if landed(disk, entry["from"], actual_to, identity):
                journal_append(journal_path, {"type": "result", "idx": entry["idx"], "status": "moved", "to": actual_to, "recovered": True})
            else:
                journal_append(journal_path, {"type": "result", "idx": entry["idx"], "status": "missing", "to": actual_to, "error": "never moved (interrupted before the move)"})
                unresolved += 1
                say(f"[{index}/{total}] skipped  {short(entry['from'])}: was never moved")
                continue
        try:
            result = _move_with_fallbacks(disk, actual_to, entry["from"], identity)
        except YandexDiskError as exc:
            result = {"status": "failed", "to": entry["from"], "error": str(exc)}
        if result["status"] == "moved":
            journal_append(journal_path, {"type": "undone", "idx": entry["idx"], "restored_to": result["to"]})
            restored += 1
            if args.verbose or index <= VERBOSE_FIRST or result.get("renamed"):
                say(f"[{index}/{total}] restored {short(actual_to)} -> {result['to']}")
            elif index % PROGRESS_EVERY == 0 or index == total:
                say(f"[{index}/{total}] ... {restored} restored so far")
        else:
            journal_append(journal_path, {"type": "undo_error", "idx": entry["idx"], "error": result.get("error")})
            failed += 1
            say(f"[{index}/{total}] FAILED   {actual_to} - {result.get('error')}")

    removed = 0
    if args.remove_empty_folders:
        for folder in sorted(journal.get("folders_created") or [], key=lambda p: (-p.count("/"), p)):
            try:
                meta = disk.get(folder, limit=1, fields="_embedded.total,path,type")
            except YandexDiskError:
                continue
            if meta.get("type") == "dir" and ((meta.get("_embedded") or {}).get("total") or 0) == 0:
                disk.delete(folder, permanently=False)
                journal_append(journal_path, {"type": "folder_removed", "path": folder})
                removed += 1
                say(f"removed empty folder {folder} (to trash)")
    say("")
    say(f"Undo done: {restored} restored, {failed} failed, {unresolved} never moved, {removed} empty folders removed. Journal: {journal_path}")
    return 0 if failed == 0 else 1


# -- main --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token-file", help="file containing the OAuth token (takes precedence over YANDEX_DISK_TOKEN)")
    parser.add_argument("--path", help="Downloads folder path (default: auto-detected, e.g. disk:/Загрузки)")
    parser.add_argument("--workdir", default=os.environ.get("YADISK_SORT_WORKDIR", DEFAULT_WORKDIR), help="where inventory/plan/journal files live")
    parser.add_argument("--names", choices=("auto", "en", "ru"), default="auto", help="language of the category folder names (auto: Russian if the Downloads folder has a Cyrillic name)")
    parser.add_argument("--rules", help="custom rules JSON (see assets/rules.default.json)")
    parser.add_argument(
        "--folders",
        choices=("content", "group", "skip"),
        default="content",
        help="what to do with subfolders: content (default) sorts each into the category its files belong to, "
             "group puts them all in one Folders/Папки folder, skip leaves them alone",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="verify the token and locate the Downloads folder")
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=cmd_check)

    p_an = sub.add_parser("analyze", help="inventory the folder and print a report")
    p_an.set_defaults(func=cmd_analyze)

    p_plan = sub.add_parser("plan", help="build plan.json from the inventory (no network)")
    p_plan.add_argument("--inventory", help="inventory.json to use (default: <workdir>/inventory.json)")
    p_plan.add_argument("--output", help="where to write plan.json")
    p_plan.add_argument("--by-date", choices=("none", "year", "month"), default="none", help="add YYYY or YYYY-MM subfolders inside each category")
    p_plan.add_argument("--duplicates", choices=("quarantine", "ignore"), default="quarantine", help="quarantine: move redundant copies to the duplicates folder; ignore: sort them like any file")
    p_plan.add_argument("--keep-other", action="store_true", help="leave unclassified files where they are")
    p_plan.add_argument("--min-age-minutes", type=int, default=5, help="skip files modified more recently than this")
    p_plan.add_argument("--only", help="comma-separated category ids to include (e.g. images,screenshots)")
    p_plan.add_argument("--exclude", help="comma-separated category ids to leave in place")
    p_plan.add_argument("--max-moves", type=int, default=0, help="cap the number of moves (0 = no cap)")
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="execute plan.json (requires --yes)")
    p_apply.add_argument("--plan", help="plan.json to execute (default: <workdir>/plan.json)")
    p_apply.add_argument("--journal", help="journal file to create (default: <workdir>/journal-<timestamp>.jsonl); must not exist yet")
    p_apply.add_argument("--yes", action="store_true", help="actually move files; without it only the summary is printed")
    p_apply.add_argument("--pause", type=float, default=0.0, help="seconds to wait between moves")
    p_apply.add_argument("--verbose", action="store_true", help="print every move (default: first 40, then progress lines; renames and failures always)")
    p_apply.set_defaults(func=cmd_apply)

    p_undo = sub.add_parser("undo", help="move files back according to a journal (requires --yes)")
    p_undo.add_argument("--journal", help="journal file (default: newest in <workdir>)")
    p_undo.add_argument("--yes", action="store_true")
    p_undo.add_argument("--remove-empty-folders", action="store_true", help="send folders created by apply to the trash if they ended up empty")
    p_undo.add_argument("--verbose", action="store_true", help="print every restored file")
    p_undo.set_defaults(func=cmd_undo)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except YandexDiskError as exc:
        hint = ""
        if exc.status == 401:
            hint = " -> The token is missing, expired or revoked; see references/oauth-token.md."
        elif exc.status == 403:
            hint = " -> The token lacks a needed permission (cloud_api:disk.write for sorting); re-issue it with the Disk scopes."
        elif exc.status == 404 and exc.code != "DownloadsNotFound":
            hint = " -> Check the folder path (use --path if auto-detection picked the wrong folder)."
        elif exc.status == 429:
            hint = " -> Yandex is rate-limiting; wait a minute and retry."
        say(f"ERROR: {exc}{hint}")
        return 1
    except FileExistsError as exc:
        say(f"ERROR: refusing to overwrite an existing journal: {exc.filename}")
        return 1
    except (OSError, ValueError) as exc:
        say(f"ERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        say("Interrupted. The journal written so far remains valid for `undo`.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
