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
