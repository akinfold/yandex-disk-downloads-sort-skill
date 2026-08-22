#!/usr/bin/env python3
"""Classification rules for Downloads files. Pure logic, no network.

A file is assigned to exactly one *category* (stable id such as ``images``)
which maps to a *folder* name in the chosen language (``Images`` / ``Изображения``).
Rules come from ``assets/rules.default.json`` unless a custom file is given.

Resolution order, most specific first:

1. Skip rules — partial downloads, lock files, OS metadata are never moved.
2. Categories in file order: name patterns (screenshots), then extensions
   (ambiguous extensions such as ``.ts`` consult Yandex's ``media_type`` first).
3. Name rules (e.g. invoices -> ``Documents/Finance``) refine a category.
4. Yandex's own ``media_type`` as a fallback for unknown extensions.
5. ``other``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "rules.default.json")

MULTI_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".fb2.zip")

_COPY_SUFFIX = re.compile(
    r"^(?P<stem>.+?)(?:\s*\((?P<n>\d{1,3})\)|(?:\s+[-\u2013\u2014]\s+|\s+|[_-])(?:copy|копия)(?:\s*\d+|\s*\(\d+\))?|\s+\(копия(?:\s*\d+)?\))$",
    re.IGNORECASE,
)


@dataclass
class Verdict:
    category: str
    folder: str
    reason: str
    extension: str = ""
    skip: Optional[str] = None
    sensitive: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "folder": self.folder,
            "reason": self.reason,
            "extension": self.extension,
            "skip": self.skip,
            "sensitive": self.sensitive,
        }


@dataclass
class FolderVerdict:
    """Where a subfolder should go, and why."""

    folder: str
    reason: str
    category: str = "folders"
    skip: Optional[str] = None
    files: int = 0
    size: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "folder": self.folder,
            "reason": self.reason,
            "category": self.category,
            "skip": self.skip,
            "files": self.files,
            "size": self.size,
        }


@dataclass
class Rules:
    raw: Dict[str, Any]
    categories: List[Dict[str, Any]]
    by_id: Dict[str, Dict[str, Any]]
    ext_index: Dict[str, str]
    name_rules: List[Dict[str, Any]]
    media_fallback: Dict[str, str]
    folder_rules: Dict[str, Any]
    ambiguous: Dict[str, Dict[str, str]]
    skip_extensions: set
    skip_patterns: List[re.Pattern] = field(default_factory=list)
    compiled: Dict[str, List[re.Pattern]] = field(default_factory=dict)


def load_rules(path: Optional[str] = None) -> Rules:
    with open(path or DEFAULT_RULES_PATH, encoding="utf-8") as handle:
        raw = json.load(handle)
    categories = list(raw.get("categories") or [])
    by_id = {c["id"]: c for c in categories}
    if "other" not in by_id:
        other = {"id": "other", "folder": {"en": "Other", "ru": "Прочее"}, "extensions": []}
        categories.append(other)
        by_id["other"] = other
    ext_index: Dict[str, str] = {}
    compiled: Dict[str, List[re.Pattern]] = {}
    for cat in categories:
        for ext in cat.get("extensions") or []:
            ext_index.setdefault(ext.lower().lstrip("."), cat["id"])
        compiled[cat["id"]] = [re.compile(p, re.IGNORECASE) for p in cat.get("name_regex") or []]
    name_rules = []
    for rule in raw.get("name_rules") or []:
        if rule.get("enabled", True):
            name_rules.append(dict(rule, _re=re.compile(rule["regex"])))
    skip = raw.get("skip") or {}
    return Rules(
        raw=raw,
        categories=categories,
        by_id=by_id,
        ext_index=ext_index,
        name_rules=name_rules,
        media_fallback=dict(raw.get("media_type_fallback") or {}),
        folder_rules=dict(raw.get("folder_rules") or {}),
        ambiguous={k: v for k, v in (raw.get("ambiguous_extensions") or {}).items() if isinstance(v, dict)},
        skip_extensions={e.lower().lstrip(".") for e in skip.get("extensions") or []},
        skip_patterns=[re.compile(p, re.IGNORECASE) for p in skip.get("name_regex") or []],
        compiled=compiled,
    )


def detect_lang(downloads_path: str, requested: str = "auto") -> str:
    """``ru`` when the Downloads folder itself has a Cyrillic name, else ``en``."""
    if requested in ("en", "ru"):
        return requested
    name = downloads_path.rstrip("/").rsplit("/", 1)[-1]
    return "ru" if re.search(r"[А-Яа-яЁё]", name) else "en"


def folder_for(category: Dict[str, Any], lang: str) -> str:
    folder = category.get("folder")
    if isinstance(folder, dict):
        return folder.get(lang) or folder.get("en") or next(iter(folder.values()))
    return str(folder or category["id"])


def special_folder(rules: Rules, key: str, lang: str) -> str:
    spec = (rules.raw.get("special_folders") or {}).get(key) or {}
    if isinstance(spec, dict):
        return spec.get(lang) or spec.get("en") or f"_{key}"
    return str(spec)


def split_extension(name: str) -> Tuple[str, str]:
    """Return (stem, ext-without-dot, lower-case). ``a.tar.gz`` -> (``a``, ``tar.gz``)."""
    lower = name.lower()
    for multi in MULTI_EXTENSIONS:
        if lower.endswith(multi) and len(name) > len(multi):
            return name[: -len(multi)], multi[1:]
    if "." in name[1:]:
        stem, ext = name.rsplit(".", 1)
        return stem, ext.lower()
    return name, ""


def classify(item: Dict[str, Any], rules: Rules, lang: str = "en") -> Verdict:
    name = str(item.get("name") or "")
    stem, ext = split_extension(name)
    ext_display = f".{ext}" if ext else ""

    # 1. Skip rules.
    if ext in rules.skip_extensions:
        return Verdict("skip", "", f"partial or temporary file ({ext_display})", ext_display, skip="partial-download")
    for pattern in rules.skip_patterns:
        if pattern.search(name):
            return Verdict("skip", "", f"system or lock file matches /{pattern.pattern}/", ext_display, skip="system-file")

    # 2. Categories in order: name patterns first (they are the most specific).
    chosen: Optional[Dict[str, Any]] = None
    reason = ""
    for cat in rules.categories:
        patterns = rules.compiled.get(cat["id"]) or []
        if not patterns:
            continue
        required = cat.get("requires_extensions_of")
        if required and rules.ext_index.get(ext) != required:
            continue
        if any(p.search(name) for p in patterns):
            chosen, reason = cat, "name pattern"
            break
    if chosen is None and ext in rules.ambiguous:
        # ".ts" is MPEG-TS or TypeScript, ".img" a disk image or a picture: let Yandex's
        # media_type decide when it knows, fall through to the default otherwise.
        media = str(item.get("media_type") or "")
        target = rules.ambiguous[ext].get(media)
        if target in rules.by_id:
            chosen, reason = rules.by_id[target], f"extension {ext_display} with media_type={media}"
    if chosen is None and ext and ext in rules.ext_index:
        chosen, reason = rules.by_id[rules.ext_index[ext]], f"extension {ext_display}"

    # 4. Yandex media_type fallback.
    if chosen is None:
        media = str(item.get("media_type") or "")
        target = rules.media_fallback.get(media)
        if target and target in rules.by_id:
            chosen, reason = rules.by_id[target], f"media_type={media}"
    if chosen is None:
        chosen, reason = rules.by_id["other"], "no matching rule"

    folder = folder_for(chosen, lang)

    # 3. Name rules refine the result: either switch the category entirely
    #    (a ".key" named id_rsa.key is a private key, not a Keynote deck) or
    #    pick a more specific folder (invoices -> Documents/Finance).
    for rule in rules.name_rules:
        if chosen["id"] in (rule.get("applies_to") or []) and rule["_re"].search(name):
            reason = f"{reason}; name matches rule '{rule.get('id')}'"
            if rule.get("category") in rules.by_id:
                chosen = rules.by_id[rule["category"]]
                folder = folder_for(chosen, lang)
            if rule.get("folder"):
                folder = folder_for(rule, lang)
            break

    return Verdict(chosen["id"], folder, reason, ext_display, sensitive=bool(chosen.get("sensitive")))


# -- folders ---------------------------------------------------------------


def classify_folder(
    item: Dict[str, Any],
    contents: List[Dict[str, Any]],
    rules: Rules,
    lang: str = "en",
) -> FolderVerdict:
    """Decide where a subfolder belongs from the files inside it.

    ``contents`` is the folder's files (recursively, as far as the caller scanned). A
    folder whose files are overwhelmingly of one kind joins that category — a folder of
    photos belongs with the images, not in a bucket named after its own shape. Anything
    mixed, empty or unscannable goes to the generic folders category, which is the honest
    answer rather than a guess.
    """
    name = str(item.get("name") or "")
    conf = rules.folder_rules
    never = ((conf.get("never_move") or {}).get("names")) or []
    if name in never:
        return FolderVerdict("", "a folder the Disk manages itself", "folders", skip="system-folder")

    files = [c for c in contents if c.get("type") == "file"]
    total_size = sum(int(c.get("size") or 0) for c in files)
    generic = folder_for(rules.by_id["folders"], lang)

    if len(files) < int(conf.get("min_files", 1) or 1):
        return FolderVerdict(generic, "no files to judge it by", "folders", files=len(files), size=total_size)

    counts: Dict[str, int] = {}
    for child in files:
        verdict = classify(child, rules, lang)
        if verdict.skip:
            continue
        counts[verdict.category] = counts.get(verdict.category, 0) + 1
    if not counts:
        return FolderVerdict(generic, "only partial downloads inside", "folders", files=len(files), size=total_size)

    top, hits = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    counted = sum(counts.values())
    share = hits / counted if counted else 0.0
    threshold = float(conf.get("dominant_share", 0.6) or 0.6)
    if share >= threshold and top not in ("other", "folders"):
        target = folder_for(rules.by_id[top], lang)
        percent = int(round(share * 100))
        return FolderVerdict(target, f"{percent}% of its {counted} files are {top}", top, files=len(files), size=total_size)
    kinds = ", ".join(sorted(counts)[:3])
    return FolderVerdict(generic, f"mixed contents ({kinds})", "folders", files=len(files), size=total_size)


# -- dates -----------------------------------------------------------------


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def best_date(item: Dict[str, Any]) -> Optional[datetime]:
    """EXIF capture time for photos, else ``created``, else ``modified``."""
    exif = item.get("exif") or {}
    for candidate in (exif.get("date_time") if isinstance(exif, dict) else None, item.get("created"), item.get("modified")):
        parsed = parse_time(candidate)
        if parsed:
            return parsed
    return None


def date_bucket(item: Dict[str, Any], mode: str) -> str:
    if mode in (None, "", "none"):
        return ""
    when = best_date(item)
    if not when:
        return ""
    return when.strftime("%Y") if mode == "year" else when.strftime("%Y-%m")


# -- duplicates ------------------------------------------------------------


def copy_stem(name: str) -> str:
    """``report (1).pdf`` / ``report копия.pdf`` / ``report - Copy (2).pdf`` -> ``report.pdf``."""
    stem, ext = split_extension(name)
    match = _COPY_SUFFIX.match(stem)
    base = match.group("stem") if match else stem
    return f"{base}.{ext}" if ext else base


def _keeper_key(item: Dict[str, Any]) -> Tuple[int, int, str, str]:
    name = str(item.get("name") or "")
    has_suffix = 0 if copy_stem(name) == name else 1
    return (has_suffix, len(name), str(item.get("created") or ""), name)


def find_exact_duplicates(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group files with identical (md5, size). The keeper is the 'cleanest' name."""
    groups: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    seen_paths = set()
    for item in items:
        md5 = item.get("md5")
        size = item.get("size")
        path = item.get("path")
        if not md5 or not isinstance(size, int) or size <= 0 or path in seen_paths:
            continue  # a path listed twice (page shift) must never become its own duplicate
        seen_paths.add(path)
        groups.setdefault((str(md5), size), []).append(item)
    out = []
    for (md5, size), members in groups.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=_keeper_key)
        out.append({"md5": md5, "size": size, "keep": members[0], "extra": members[1:]})
    out.sort(key=lambda g: -g["size"] * len(g["extra"]))
    return out


def find_name_lookalikes(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Files whose names differ only by a copy suffix but whose content differs."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(copy_stem(str(item.get("name") or "")).lower(), []).append(item)
    out = []
    for base, members in groups.items():
        if len(members) < 2:
            continue
        hashes = {str(m.get("md5")) for m in members}
        if len(hashes) <= 1:
            continue  # exact duplicates are handled elsewhere
        out.append({"base": base, "items": sorted(members, key=lambda m: str(m.get("name")))})
    out.sort(key=lambda g: g["base"])
    return out


def human_size(num: Optional[int]) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
