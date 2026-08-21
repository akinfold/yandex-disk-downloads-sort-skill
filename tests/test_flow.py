import io
import json
import os
import unittest
from contextlib import redirect_stdout

import helpers
import downloads_sort as ds


class FlowTests(helpers.FakeServerTest):
    def seed(self):
        f = self.fake
        d = self.downloads
        f.add_file(f"{d}/report.pdf", 2000, md5="same-report", media_type="document")
        f.add_file(f"{d}/report (1).pdf", 2000, md5="same-report", media_type="document")
        f.add_file(f"{d}/report (2).pdf", 2000, md5="same-report", media_type="document")
        f.add_file(f"{d}/Снимок экрана 2026-08-21 в 10.11.12.png", 500, media_type="image")
        f.add_file(f"{d}/photo.jpg", 3000, media_type="image", created="2024-03-01T00:00:00+00:00")
        f.add_file(f"{d}/setup.dmg", 50000, media_type="diskimage")
        f.add_file(f"{d}/archive.tar.gz", 700, media_type="compressed")
        f.add_file(f"{d}/movie.mkv.crdownload", 1, media_type="unknown")
        f.add_file(f"{d}/Счёт 42.pdf", 100, media_type="document")
        f.add_file(f"{d}/mystery.xyz", 10, media_type="unknown")
        f.add_file(f"{d}/fresh.txt", 10, modified="2999-01-01T00:00:00+00:00")
        f.add_file(f"{d}/clash.txt", 10)
        f.add_dir(f"{d}/Existing folder")
        f.add_file(f"{d}/Existing folder/inside.pdf", 10)
        f.add_dir(f"{d}/Документы")
        f.add_file(f"{d}/Документы/clash.txt", 99)  # forces a rename on move

    def load(self, name):
        with open(os.path.join(self.workdir, name), encoding="utf-8") as handle:
            return json.load(handle)

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ds.main(["--workdir", self.workdir, *argv])
        return code, buf.getvalue()

    def test_check(self):
        self.seed()
        code, out = self.run_cli("check", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["downloads"], "disk:/Загрузки")
        self.assertEqual((data["files"], data["subfolders"]), (12, 2))

    def test_full_cycle(self):
        self.seed()
        code, report = self.run_cli("analyze")
        self.assertEqual(code, 0)
        self.assertIn("# Downloads on Yandex Disk: analysis", report)
        self.assertIn("Exact duplicates: 1 group(s), 2 redundant", report)
        self.assertIn("keep `report.pdf`", report)
        self.assertIn("movie.mkv.crdownload", report)
        inv = self.load("inventory.json")
        self.assertEqual(inv["lang"], "ru")
        self.assertEqual(len(inv["files"]), 12)
        self.assertEqual(inv["folders"], ["Existing folder", "Документы"])

        code, summary = self.run_cli("plan")
        self.assertEqual(code, 0)
        plan = self.load("plan.json")
        targets = {m["from"].rsplit("/", 1)[-1]: m["folder"] for m in plan["moves"]}
        self.assertEqual(targets["report.pdf"], "Документы")
        self.assertEqual(targets["report (1).pdf"], "_Дубликаты")
        self.assertEqual(targets["report (2).pdf"], "_Дубликаты")
        self.assertEqual(targets["Снимок экрана 2026-08-21 в 10.11.12.png"], "Скриншоты")
        self.assertEqual(targets["setup.dmg"], "Установщики")
        self.assertEqual(targets["archive.tar.gz"], "Архивы")
        self.assertEqual(targets["Счёт 42.pdf"], "Документы/Финансы")
        self.assertEqual(targets["mystery.xyz"], "Прочее")
        self.assertEqual(targets["clash.txt"], "Документы")
        skipped = {s["path"].rsplit("/", 1)[-1]: s["reason"] for s in plan["skipped"]}
        self.assertIn("movie.mkv.crdownload", skipped)
        self.assertIn("fresh.txt", skipped)
        self.assertNotIn("inside.pdf", targets)
        self.assertEqual(plan["folders"][0].count("/"), min(p.count("/") for p in plan["folders"]))

        code, out = self.run_cli("apply")
        self.assertEqual(code, 2)
        self.assertIn("Dry run", out)
        self.assertIn("disk:/Загрузки/report.pdf", self.fake.tree)

        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0, out)
        tree = self.fake.tree
        self.assertIn("disk:/Загрузки/Документы/report.pdf", tree)
        self.assertIn("disk:/Загрузки/_Дубликаты/report (1).pdf", tree)
        self.assertIn("disk:/Загрузки/Документы/Финансы/Счёт 42.pdf", tree)
        self.assertIn("disk:/Загрузки/Документы/clash (2).txt", tree)  # renamed, not overwritten
        self.assertEqual(tree["disk:/Загрузки/Документы/clash.txt"]["size"], 99)
        self.assertIn("disk:/Загрузки/movie.mkv.crdownload", tree)
        self.assertIn("disk:/Загрузки/fresh.txt", tree)
        self.assertIn("disk:/Загрузки/Existing folder/inside.pdf", tree)
        journals = [n for n in os.listdir(self.workdir) if n.startswith("journal-")]
        self.assertEqual(len(journals), 1)
        self.assertTrue(journals[0].endswith(".jsonl"))
        journal = ds.load_journal(os.path.join(self.workdir, journals[0]))
        self.assertEqual(len(journal["entries"]), len(plan["moves"]))
        self.assertTrue(all(e["status"] == "moved" for e in journal["entries"]))
        self.assertTrue(journal["finished"])
        self.assertIn("disk:/Загрузки/Скриншоты", journal["folders_created"])
        self.assertIn("disk:/Загрузки/Документы/Финансы", journal["folders_created"])
        self.assertNotIn("disk:/Загрузки/Документы", journal["folders_created"])
        # write-ahead: every move has a pending line before its result line
        with open(os.path.join(self.workdir, journals[0]), encoding="utf-8") as handle:
            kinds = [json.loads(line)["type"] for line in handle if line.strip()]
        first_pending = kinds.index("pending")
        self.assertLess(first_pending, kinds.index("result"))
        self.assertEqual(kinds[-1], "finished")
        overwrite_calls = [q for m, p, q in self.fake.log if p.endswith("/move") and q.get("overwrite") != "false"]
        self.assertEqual(overwrite_calls, [])

        # Re-running analyze+plan on the sorted folder must find nothing to do.
        self.run_cli("analyze")
        code, out = self.run_cli("plan")
        plan2 = self.load("plan.json")
        self.assertEqual(plan2["moves"], [])

        code, out = self.run_cli("undo")
        self.assertEqual(code, 2)
        code, out = self.run_cli("undo", "--yes", "--remove-empty-folders")
        self.assertEqual(code, 0, out)
        self.assertIn("disk:/Загрузки/report.pdf", self.fake.tree)
        self.assertIn("disk:/Загрузки/report (1).pdf", self.fake.tree)
        self.assertIn("disk:/Загрузки/clash.txt", self.fake.tree)
        self.assertEqual(self.fake.tree["disk:/Загрузки/Документы/clash.txt"]["size"], 99)
        self.assertNotIn("disk:/Загрузки/Скриншоты", self.fake.tree)
        self.assertIn("disk:/Загрузки/Документы", self.fake.tree)  # pre-existing, never removed

    def test_plan_options(self):
        self.seed()
        self.run_cli("analyze")
        code, _ = self.run_cli("--names", "en", "plan", "--by-date", "month", "--duplicates", "ignore", "--keep-other", "--only", "images,documents,other")
        plan = self.load("plan.json")
        targets = {m["from"].rsplit("/", 1)[-1]: m["folder"] for m in plan["moves"]}
        self.assertEqual(targets["photo.jpg"], "Images/2024-03")
        self.assertEqual(targets["report (1).pdf"], "Documents/2026-05")
        self.assertNotIn("setup.dmg", targets)
        self.assertNotIn("mystery.xyz", targets)

    def test_apply_with_async_operations_and_missing_source(self):
        self.seed()
        self.fake.async_moves = True
        self.run_cli("analyze")
        self.run_cli("plan")
        del self.fake.tree["disk:/Загрузки/photo.jpg"]  # vanished between plan and apply
        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("missing  photo.jpg", out)
        self.assertIn("disk:/Загрузки/Документы/report.pdf", self.fake.tree)

    def test_apply_stops_after_consecutive_failures(self):
        self.seed()
        self.run_cli("analyze")
        self.run_cli("plan")
        self.fake.inject_routes = ("/v1/disk/resources/move",)
        self.fake.inject.extend([(403, {"error": "DiskAccessForbidden", "description": "nope"}, {})] * 40)
        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("consecutive failures", out)

    def test_large_apply_output_is_bounded(self):
        for i in range(300):
            self.fake.add_file(f"{self.downloads}/doc {i}.pdf", 10 + i, media_type="document")
        self.run_cli("analyze")
        self.run_cli("plan")
        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0)
        self.assertLess(len(out), 6000, "apply output must stay readable for agent harnesses")
        self.assertIn("[300/300]", out)
        self.assertIn("Done: 300 moved", out)
        first_journal = os.path.join(self.workdir, sorted(n for n in os.listdir(self.workdir) if n.startswith("journal-"))[0])
        self.assertEqual(len([n for n in os.listdir(self.workdir) if n.startswith("journal-")]), 1)
        code, out = self.run_cli("apply", "--yes", "--plan", os.path.join(self.workdir, "plan.json"))
        self.assertIn("missing", out)  # sources already moved: every move reported, none silently dropped
        self.assertEqual(len([n for n in os.listdir(self.workdir) if n.startswith("journal-")]), 2)  # never shares a journal file
        code, out = self.run_cli("undo", "--yes")  # newest journal has nothing to undo, but points at the older one
        self.assertEqual(code, 0)
        self.assertIn("Nothing to undo", out)
        self.assertIn(first_journal, out)
        code, out = self.run_cli("undo", "--yes", "--journal", first_journal)
        self.assertEqual(code, 0)
        self.assertLess(len(out), 6000)
        self.assertIn("Undo done: 300 restored", out)

    def test_lost_response_is_recovered_and_undoable(self):
        self.seed()
        self.run_cli("analyze")
        self.run_cli("plan")
        self.fake.lose_response_for.add("disk:/Загрузки/photo.jpg")
        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("photo.jpg -> Изображения (recovered after a lost response)", out)
        self.assertIn("disk:/Загрузки/Изображения/photo.jpg", self.fake.tree)
        journal = ds.load_journal(ds.latest_journal(self.workdir))
        photo = next(e for e in journal["entries"] if e["from"].endswith("photo.jpg"))
        self.assertEqual((photo["status"], photo.get("recovered")), ("moved", True))
        code, out = self.run_cli("undo", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("disk:/Загрузки/photo.jpg", self.fake.tree)

    def test_stale_plan_leaves_changed_files_alone(self):
        self.seed()
        self.run_cli("analyze")
        self.run_cli("plan")
        self.fake.tree["disk:/Загрузки/photo.jpg"]["md5"] = "rewritten"
        self.fake.tree["disk:/Загрузки/photo.jpg"]["resource_id"] = "rid-new"
        del self.fake.tree["disk:/Загрузки/setup.dmg"]
        self.fake.add_dir("disk:/Загрузки/setup.dmg")  # a folder took the file's name
        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("changed  photo.jpg", out)
        self.assertIn("changed  setup.dmg", out)
        self.assertIn("disk:/Загрузки/photo.jpg", self.fake.tree)
        self.assertEqual(self.fake.tree["disk:/Загрузки/setup.dmg"]["type"], "dir")
        self.assertIn("2 changed since the plan", out)

    def test_file_occupying_a_folder_name_is_reported_not_moved_into(self):
        self.seed()
        self.fake.add_file(f"{self.downloads}/Архивы", 5)  # a FILE named like the target folder
        self.run_cli("analyze")
        code, out = self.run_cli("plan")
        plan = self.load("plan.json")
        skipped = {s["path"].rsplit("/", 1)[-1]: s["reason"] for s in plan["skipped"]}
        self.assertIn("archive.tar.gz", skipped)
        self.assertIn("taken by a file", skipped["archive.tar.gz"])
        self.assertNotIn("disk:/Загрузки/Архивы", plan["folders"])
        code, out = self.run_cli("apply", "--yes")
        self.assertEqual(code, 0, out)
        # The extension-less file itself is sorted into Other; the archive stayed put and nothing was moved "into" a file.
        self.assertEqual(self.fake.tree["disk:/Загрузки/Прочее/Архивы"]["type"], "file")
        self.assertNotIn("disk:/Загрузки/Архивы", self.fake.tree)
        self.assertIn("disk:/Загрузки/archive.tar.gz", self.fake.tree)

    def test_repeated_listing_item_is_not_its_own_duplicate(self):
        self.seed()
        self.fake.echo_twice = "disk:/Загрузки/photo.jpg"
        self.run_cli("analyze")
        inv = self.load("inventory.json")
        paths = [f["path"] for f in inv["files"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertFalse(any("photo.jpg" in g["keep"] for g in inv["duplicates"]))

    def test_undo_resolves_pending_entries(self):
        self.seed()
        self.run_cli("analyze")
        self.run_cli("plan")
        self.run_cli("apply", "--yes")
        journal_path = ds.latest_journal(self.workdir)
        with open(journal_path, encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        # Simulate a crash right after the move of report.pdf was sent: drop its result line.
        kept = []
        for line in lines:
            event = json.loads(line)
            if event["type"] == "result" and event.get("to", "").endswith("/report.pdf"):
                continue
            if event["type"] in ("finished",):
                continue
            kept.append(line)
        with open(journal_path, "w", encoding="utf-8") as handle:
            handle.writelines(kept)
        journal = ds.load_journal(journal_path)
        self.assertEqual([e["status"] for e in journal["entries"] if e["from"].endswith("report.pdf")], ["pending"])
        code, out = self.run_cli("undo", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("disk:/Загрузки/report.pdf", self.fake.tree)
        self.assertNotIn("disk:/Загрузки/Документы/report.pdf", self.fake.tree)

    def test_plan_summary_is_bounded_with_by_date(self):
        for i in range(150):
            month = 1 + i % 12
            year = 2005 + i // 12  # all in the past, so nothing counts as "just modified"
            self.fake.add_file(f"{self.downloads}/f{i}.pdf", 10, created=f"{year}-{month:02d}-10T00:00:00+00:00")
            self.fake.add_file(f"{self.downloads}/p{i}.png", 10, created=f"{year}-{month:02d}-11T00:00:00+00:00")
        self.run_cli("analyze")
        code, out = self.run_cli("plan", "--by-date", "month")
        self.assertEqual(code, 0)
        self.assertLess(len(out), 6000)
        self.assertIn("more folder(s)", out)
        plan = self.load("plan.json")
        self.assertEqual(len(plan["moves"]), 300)

    def test_unreadable_rules_file_is_a_clean_error(self):
        self.seed()
        code, out = self.run_cli("--rules", "/nonexistent/rules.json", "analyze")
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", out)
        self.assertNotIn("Traceback", out)

    def test_missing_token_is_reported(self):
        del os.environ["YANDEX_DISK_TOKEN"]
        code, out = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("No Yandex Disk OAuth token", out)
        self.assertNotIn(helpers.TOKEN, out)


if __name__ == "__main__":
    unittest.main()
