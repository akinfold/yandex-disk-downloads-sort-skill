"""Folder support: classification, deferred moves, partial failures and merges."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout

import helpers  # noqa: F401  (sets sys.path)
import classify as cls
import downloads_sort as ds


class FolderClassifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls_):
        cls_.rules = cls.load_rules()

    def verdict(self, name, kids, lang="en"):
        return cls.classify_folder({"name": name, "type": "dir", "path": f"disk:/Загрузки/{name}"}, kids, self.rules, lang)

    def files(self, *names):
        return [{"name": n, "type": "file", "size": 10} for n in names]

    def test_dominant_content_wins(self):
        v = self.verdict("Отпуск", self.files("a.jpg", "b.jpg", "c.png", "d.heic"))
        self.assertEqual((v.category, v.folder), ("images", "Images"))
        self.assertIn("100%", v.reason)

    def test_mixed_goes_to_the_generic_folder(self):
        v = self.verdict("Проект", self.files("a.jpg", "b.pdf", "c.xlsx", "d.mp4"))
        self.assertEqual((v.category, v.folder), ("folders", "Folders"))
        self.assertIn("mixed", v.reason)

    def test_empty_folder_is_not_guessed_at(self):
        v = self.verdict("Пустая", [])
        self.assertEqual(v.category, "folders")
        self.assertIsNone(v.skip)

    def test_disk_system_folders_are_left_alone(self):
        for name in ("Фотокамера", "Скриншоты", "Социальные сети"):
            self.assertEqual(self.verdict(name, self.files("a.jpg")).skip, "system-folder", name)

    def test_russian_names(self):
        self.assertEqual(self.verdict("Отпуск", self.files("a.jpg", "b.jpg"), "ru").folder, "Изображения")
        self.assertEqual(self.verdict("Разное", self.files("a.jpg", "b.pdf"), "ru").folder, "Папки")

    def test_a_folder_of_partial_downloads_is_not_classified_by_them(self):
        v = self.verdict("Качается", self.files("big.iso.crdownload", "x.part"))
        self.assertEqual(v.category, "folders")


class FolderFlowTests(helpers.FakeServerTest):
    def seed(self):
        f, d = self.fake, self.downloads
        f.add_file(f"{d}/loose.pdf", 100, media_type="document")
        # a folder of photos: should join Images
        f.add_dir(f"{d}/Отпуск")
        for i in range(4):
            f.add_file(f"{d}/Отпуск/photo{i}.jpg", 50, media_type="image")
        # a mixed folder: should go to the generic folder
        f.add_dir(f"{d}/Разное")
        f.add_file(f"{d}/Разное/note.pdf", 10, media_type="document")
        f.add_file(f"{d}/Разное/clip.mp4", 20, media_type="video")
        # nested structure, to prove the merge walks it
        f.add_dir(f"{d}/Отпуск/Ещё")
        f.add_file(f"{d}/Отпуск/Ещё/deep.jpg", 30, media_type="image")

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ds.main(["--workdir", self.workdir, *argv])
        return code, buf.getvalue()

    def load(self, name):
        with open(os.path.join(self.workdir, name), encoding="utf-8") as handle:
            return json.load(handle)

    def test_folders_are_classified_and_moved_whole(self):
        self.seed()
        self.run_cli("analyze")
        inv = self.load("inventory.json")
        verdicts = {f["name"]: f["verdict"]["folder"] for f in inv["folders"]}
        self.assertEqual(verdicts["Отпуск"], "Изображения")
        self.assertEqual(verdicts["Разное"], "Папки")

        self.run_cli("plan", "--min-age-minutes", "0")
        plan = self.load("plan.json")
        dirs = {m["from"].rsplit("/", 1)[-1]: m for m in plan["moves"] if m.get("kind") == "dir"}
        self.assertEqual(set(dirs), {"Отпуск", "Разное"})

        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("disk:/Загрузки/Изображения/Отпуск/photo0.jpg", self.fake.tree)
        self.assertIn("disk:/Загрузки/Изображения/Отпуск/Ещё/deep.jpg", self.fake.tree)
        self.assertIn("disk:/Загрузки/Папки/Разное/note.pdf", self.fake.tree)
        self.assertNotIn("disk:/Загрузки/Отпуск", self.fake.tree)

        code, out = self.run_cli("undo", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("disk:/Загрузки/Отпуск/photo0.jpg", self.fake.tree)
        self.assertIn("disk:/Загрузки/Отпуск/Ещё/deep.jpg", self.fake.tree)
        self.assertIn("disk:/Загрузки/Разное/note.pdf", self.fake.tree)

    def test_operation_reports_failed_after_moving_part_of_the_tree(self):
        self.seed()
        self.run_cli("analyze")
        self.run_cli("plan", "--min-age-minutes", "0")
        # The server moves two items, then the deferred operation reports failure.
        self.fake.partial_move_for["disk:/Загрузки/Отпуск"] = 2
        self.fake.fail_operation_for.add("disk:/Загрузки/Отпуск")
        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("deferred move ended failed", out)
        # Everything ends up at the destination anyway, and the source is gone.
        for name in ("photo0.jpg", "photo1.jpg", "photo2.jpg", "photo3.jpg"):
            self.assertIn(f"disk:/Загрузки/Изображения/Отпуск/{name}", self.fake.tree, name)
        self.assertIn("disk:/Загрузки/Изображения/Отпуск/Ещё/deep.jpg", self.fake.tree)
        self.assertNotIn("disk:/Загрузки/Отпуск", self.fake.tree)

    def test_a_hanging_operation_is_reconciled_not_trusted(self):
        self.seed()
        self.run_cli("analyze")
        self.run_cli("plan", "--min-age-minutes", "0")
        self.fake.partial_move_for["disk:/Загрузки/Отпуск"] = 1
        self.fake.hang_operation_for.add("disk:/Загрузки/Отпуск")
        # Cap the poll so the test does not sit through the real two-minute wait.
        original = ds.YandexDisk.poll_operation
        ds.YandexDisk.poll_operation = lambda self, href, timeout=0.3: original(self, href, timeout=0.3)
        try:
            code, out = self.run_cli("apply", "--yes")
        finally:
            ds.YandexDisk.poll_operation = original
        self.assertEqual(code, 0, out)
        self.assertIn("timeout", out)
        self.assertIn("disk:/Загрузки/Изображения/Отпуск/photo3.jpg", self.fake.tree)
        self.assertNotIn("disk:/Загрузки/Отпуск", self.fake.tree)

    def test_merging_into_a_destination_that_already_holds_files(self):
        self.seed()
        # Someone already has an Отпуск folder at the destination, with a file of their own.
        self.fake.add_dir("disk:/Загрузки/Изображения/Отпуск")
        self.fake.add_file("disk:/Загрузки/Изображения/Отпуск/old.jpg", 5, media_type="image")
        self.fake.add_file("disk:/Загрузки/Изображения/Отпуск/photo0.jpg", 999, md5="different", media_type="image")
        self.run_cli("analyze")
        self.run_cli("plan", "--min-age-minutes", "0")
        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("merged", out)
        # The pre-existing files survive untouched, the clashing one is renamed.
        self.assertEqual(self.fake.tree["disk:/Загрузки/Изображения/Отпуск/old.jpg"]["size"], 5)
        self.assertEqual(self.fake.tree["disk:/Загрузки/Изображения/Отпуск/photo0.jpg"]["size"], 999)
        self.assertIn("disk:/Загрузки/Изображения/Отпуск/photo0 (2).jpg", self.fake.tree)
        self.assertNotIn("disk:/Загрузки/Отпуск", self.fake.tree)

        code, out = self.run_cli("undo", "--yes")
        self.assertEqual(code, 0, out)
        # Undo returns only what we moved; the stranger's files stay where they were.
        self.assertIn("disk:/Загрузки/Отпуск/photo0.jpg", self.fake.tree)
        self.assertEqual(self.fake.tree["disk:/Загрузки/Изображения/Отпуск/old.jpg"]["size"], 5)
        self.assertEqual(self.fake.tree["disk:/Загрузки/Изображения/Отпуск/photo0.jpg"]["size"], 999)

    def test_destination_folders_and_system_folders_are_never_moved(self):
        self.seed()
        self.fake.add_dir(f"{self.downloads}/Изображения")   # already a sorting destination
        self.fake.add_file(f"{self.downloads}/Изображения/existing.jpg", 10, media_type="image")
        self.fake.add_dir(f"{self.downloads}/Фотокамера")     # the Disk's own folder
        self.fake.add_file(f"{self.downloads}/Фотокамера/cam.jpg", 10, media_type="image")
        self.run_cli("analyze")
        self.run_cli("plan", "--min-age-minutes", "0")
        plan = self.load("plan.json")
        moved = {m["from"] for m in plan["moves"]}
        self.assertNotIn(f"{self.downloads}/Изображения", moved)
        self.assertNotIn(f"{self.downloads}/Фотокамера", moved)
        reasons = {s["path"]: s["reason"] for s in plan["skipped"]}
        self.assertIn("destinations", reasons[f"{self.downloads}/Изображения"])
        self.assertIn("Disk manages", reasons[f"{self.downloads}/Фотокамера"])
        self.run_cli("apply", "--yes")
        self.assertIn(f"{self.downloads}/Фотокамера/cam.jpg", self.fake.tree)

    def test_folders_skip_restores_the_old_behaviour(self):
        self.seed()
        self.run_cli("--folders", "skip", "analyze")
        inv = self.load("inventory.json")
        self.assertEqual(inv["folders"], [])
        self.assertEqual(sorted(inv["folder_names"]), ["Отпуск", "Разное"])
        self.run_cli("--folders", "skip", "plan", "--min-age-minutes", "0")
        plan = self.load("plan.json")
        self.assertTrue(all(m.get("kind") != "dir" for m in plan["moves"]))

    def test_group_puts_every_folder_in_one_place(self):
        self.seed()
        self.run_cli("analyze")
        self.run_cli("--folders", "group", "plan", "--min-age-minutes", "0")
        plan = self.load("plan.json")
        dirs = [m for m in plan["moves"] if m.get("kind") == "dir"]
        self.assertEqual({m["folder"] for m in dirs}, {"Папки"})


if __name__ == "__main__":
    unittest.main()


class FolderIdempotencyTests(helpers.FakeServerTest):
    """A sorted folder must stay sorted: the category folders are destinations, not cargo."""

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ds.main(["--workdir", self.workdir, *argv])
        return code, buf.getvalue()

    def load(self, name):
        with open(os.path.join(self.workdir, name), encoding="utf-8") as handle:
            return json.load(handle)

    def test_category_folders_are_never_moved_even_with_nothing_else_to_do(self):
        d = self.downloads
        for folder, name, mt in (("Документы", "a.pdf", "document"), ("Изображения", "b.jpg", "image"),
                                 ("Документы/Финансы", "счёт.pdf", "document"), ("_Дубликаты", "dup.pdf", "document"),
                                 ("Папки", "misc.bin", "unknown")):
            self.fake.add_dir(f"{d}/{folder}")
            self.fake.add_file(f"{d}/{folder}/{name}", 10, media_type=mt)
        self.run_cli("analyze")
        code, out = self.run_cli("plan", "--min-age-minutes", "0")
        plan = self.load("plan.json")
        self.assertEqual(plan["moves"], [], out)
        reasons = {s["path"].rsplit("/", 1)[-1]: s["reason"] for s in plan["skipped"]}
        for name in ("Документы", "Изображения", "_Дубликаты", "Папки"):
            self.assertIn("destinations", reasons[name], name)

    def test_sorting_twice_changes_nothing_the_second_time(self):
        d = self.downloads
        self.fake.add_file(f"{d}/report.pdf", 10, media_type="document")
        self.fake.add_dir(f"{d}/Отпуск")
        self.fake.add_file(f"{d}/Отпуск/p.jpg", 10, media_type="image")
        for _ in range(2):
            self.run_cli("analyze")
            self.run_cli("plan", "--min-age-minutes", "0")
            self.run_cli("apply", "--yes")
        self.run_cli("analyze")
        self.run_cli("plan", "--min-age-minutes", "0")
        self.assertEqual(self.load("plan.json")["moves"], [])
        self.assertIn("disk:/Загрузки/Изображения/Отпуск/p.jpg", self.fake.tree)
        self.assertNotIn("disk:/Загрузки/Изображения/Изображения", self.fake.tree)


class DuplicateFolderTests(helpers.FakeServerTest):
    """Two folders holding exactly the same bytes, found without scanning everything."""

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ds.main(["--workdir", self.workdir, *argv])
        return code, buf.getvalue()

    def load(self, name):
        with open(os.path.join(self.workdir, name), encoding="utf-8") as handle:
            return json.load(handle)

    def twins(self, a, b, contents):
        for folder in (a, b):
            self.fake.add_dir(f"{self.downloads}/{folder}")
            for name, md5, size in contents:
                self.fake.add_file(f"{self.downloads}/{folder}/{name}", size, md5=md5, media_type="video")

    def test_identical_folders_are_found_and_quarantined(self):
        contents = [("a.mp4", "h1", 1000), ("b.mp4", "h2", 2000)]
        self.twins("DRAFTS", "DRAFTS (1)", contents)
        self.run_cli("analyze")
        inv = self.load("inventory.json")
        self.assertEqual(len(inv["duplicate_folders"]), 1)
        group = inv["duplicate_folders"][0]
        self.assertTrue(group["keep"].endswith("/DRAFTS"))
        self.assertEqual(group["extra"], [f"{self.downloads}/DRAFTS (1)"])
        self.assertEqual(group["reclaimable"], 3000)

        self.run_cli("plan", "--min-age-minutes", "0")
        plan = self.load("plan.json")
        targets = {m["from"].rsplit("/", 1)[-1]: m["folder"] for m in plan["moves"]}
        self.assertEqual(targets["DRAFTS (1)"], "_Дубликаты")
        self.assertEqual(targets["DRAFTS"], "Видео")

        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("disk:/Загрузки/_Дубликаты/DRAFTS (1)/a.mp4", self.fake.tree)
        self.assertIn("disk:/Загрузки/Видео/DRAFTS/a.mp4", self.fake.tree)

    def test_folders_that_only_look_alike_are_not_called_duplicates(self):
        # Same file names, one differing checksum: not the same folder.
        self.twins("Копия A", "Копия B", [("a.mp4", "h1", 1000), ("b.mp4", "h2", 2000)])
        self.fake.tree[f"{self.downloads}/Копия B/b.mp4"]["md5"] = "different"
        self.run_cli("analyze")
        self.assertEqual(self.load("inventory.json")["duplicate_folders"], [])

    def test_different_names_inside_are_not_even_candidates(self):
        self.fake.add_dir(f"{self.downloads}/One")
        self.fake.add_file(f"{self.downloads}/One/a.mp4", 10, md5="h", media_type="video")
        self.fake.add_dir(f"{self.downloads}/Two")
        self.fake.add_file(f"{self.downloads}/Two/b.mp4", 10, md5="h", media_type="video")
        self.run_cli("analyze")
        inv = self.load("inventory.json")
        self.assertEqual(inv["duplicate_folders"], [])
        # The cheap signature differs, so neither folder was scanned a second time.
        self.assertFalse(any(f.get("full_scan") for f in inv["folders"]))

    def test_folders_are_listed_once_and_not_read_twice(self):
        for i in range(6):
            self.fake.add_dir(f"{self.downloads}/f{i}")
            self.fake.add_file(f"{self.downloads}/f{i}/uniq{i}.mp4", 10, md5=f"m{i}", media_type="video")
        self.twins("T", "T copy", [("same.mp4", "same-hash", 100)])
        self.run_cli("analyze")
        inv = self.load("inventory.json")
        self.assertEqual(len(inv["duplicate_folders"]), 1)
        # A folder small enough to be read whole already has its fingerprint, so the
        # comparison pass costs nothing extra: nothing is read a second time.
        self.assertFalse([f["name"] for f in inv["folders"] if f.get("full_scan")])
        listings = [q.get("path") for m, p, q in self.fake.log if p == "/v1/disk/resources" and m == "GET"]
        for name in ("f0", "T", "T copy"):
            self.assertEqual(listings.count(f"{self.downloads}/{name}"), 1, name)

    def test_a_folder_too_big_to_scan_is_never_called_a_duplicate(self):
        contents = [(f"f{i}.mp4", f"h{i}", 10) for i in range(3)]
        self.twins("Big", "Big (1)", contents)
        rules = json.load(open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "skills/yandex-disk-downloads-sort/assets/rules.default.json"), encoding="utf-8"))
        rules["folder_rules"]["max_scan_items"] = 2  # forces truncation
        path = os.path.join(self.workdir, "tiny-rules.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(rules, handle, ensure_ascii=False)
        self.run_cli("--rules", path, "analyze")
        inv = self.load("inventory.json")
        # The full second pass still runs for candidates, so they are compared properly.
        self.assertEqual(len(inv["duplicate_folders"]), 1)

    def test_deep_duplicates_reports_files_inside_folders_without_moving_them(self):
        self.fake.add_dir(f"{self.downloads}/Проект")
        self.fake.add_file(f"{self.downloads}/Проект/report.pdf", 500, md5="same", media_type="document")
        self.fake.add_dir(f"{self.downloads}/Проект/Копии")
        self.fake.add_file(f"{self.downloads}/Проект/Копии/report.pdf", 500, md5="same", media_type="document")
        code, out = self.run_cli("analyze", "--deep-duplicates")
        self.assertEqual(code, 0)
        inv = self.load("inventory.json")
        self.assertEqual(len(inv["inner_duplicates"]), 1)
        self.assertEqual(inv["inner_duplicates"][0]["reclaimable"], 500)
        self.assertIn("only reported", out)
        # Nothing inside a folder is ever planned for a move.
        self.run_cli("plan", "--min-age-minutes", "0")
        plan = self.load("plan.json")
        self.assertTrue(all("/Проект/" not in m["from"] for m in plan["moves"]))


class NestedDuplicateTests(helpers.FakeServerTest):
    """Identical folders that a previous run already tucked away under a category."""

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ds.main(["--workdir", self.workdir, *argv])
        return code, buf.getvalue()

    def load(self, name):
        with open(os.path.join(self.workdir, name), encoding="utf-8") as handle:
            return json.load(handle)

    def test_identical_folders_deeper_in_the_tree_are_reported_not_moved(self):
        d = self.downloads
        # Exactly the shape a sorted disk ends up in: the twins live inside Видео.
        for twin in ("DRAFTS", "DRAFTS (1)"):
            self.fake.add_dir(f"{d}/Видео/{twin}")
            for i in range(3):
                self.fake.add_file(f"{d}/Видео/{twin}/clip{i}.mp4", 1000, md5=f"h{i}", media_type="video")
        code, out = self.run_cli("analyze")
        self.assertEqual(code, 0)
        inv = self.load("inventory.json")
        self.assertEqual(len(inv["nested_duplicate_folders"]), 1)
        group = inv["nested_duplicate_folders"][0]
        self.assertEqual(group["keep"], f"{d}/Видео/DRAFTS")
        self.assertEqual(group["extra"], [f"{d}/Видео/DRAFTS (1)"])
        self.assertEqual(group["reclaimable"], 3000)
        self.assertIn("Only reported", out)

        # Nothing inside a folder is touched, and Видео itself stays put.
        self.run_cli("plan", "--min-age-minutes", "0")
        plan = self.load("plan.json")
        self.assertEqual(plan["moves"], [])
        self.assertIn("destinations", {s["path"]: s["reason"] for s in plan["skipped"]}[f"{d}/Видео"])

    def test_a_truncated_scan_never_claims_two_folders_are_identical(self):
        d = self.downloads
        for twin in ("A", "B"):
            self.fake.add_dir(f"{d}/Видео/{twin}")
            for i in range(3):
                self.fake.add_file(f"{d}/Видео/{twin}/c{i}.mp4", 10, md5=f"h{i}", media_type="video")
        rules = json.load(open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "skills/yandex-disk-downloads-sort/assets/rules.default.json"), encoding="utf-8"))
        rules["folder_rules"]["max_scan_items"] = 2  # the scan cannot see the whole tree
        path = os.path.join(self.workdir, "tiny.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(rules, handle, ensure_ascii=False)
        self.run_cli("--rules", path, "analyze")
        self.assertEqual(self.load("inventory.json")["nested_duplicate_folders"], [])

    def test_the_report_says_destination_folders_stay(self):
        d = self.downloads
        self.fake.add_dir(f"{d}/Документы")
        self.fake.add_file(f"{d}/Документы/a.pdf", 10, media_type="document")
        _, out = self.run_cli("analyze")
        self.assertIn("already one of the sorting destinations", out)
        self.assertNotIn("| `Документы` | 1 | `Документы`", out)
