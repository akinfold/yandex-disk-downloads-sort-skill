import unittest

import helpers  # noqa: F401  (sets sys.path)
import classify as cls


class ClassifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls_):
        cls_.rules = cls.load_rules()

    def verdict(self, name, lang="en", **extra):
        return cls.classify(dict(name=name, **extra), self.rules, lang)

    def test_extensions_map_to_categories(self):
        cases = {
            "report.pdf": ("documents", "Documents"),
            "photo.JPG": ("images", "Images"),
            "clip.mkv": ("videos", "Videos"),
            "song.flac": ("audio", "Audio"),
            "bundle.tar.gz": ("archives", "Archives"),
            "Setup.EXE": ("installers", "Installers"),
            "ubuntu.iso": ("disk-images", "Disk images"),
            "data.parquet": ("code", "Code and data"),
            "Roboto.woff2": ("fonts", "Fonts"),
            "movie.torrent": ("torrents", "Torrents"),
            "book.fb2.zip": ("books", "Books"),
            "deck.pptx": ("presentations", "Presentations"),
            "sheet.xlsx": ("spreadsheets", "Spreadsheets"),
        }
        for name, (category, folder) in cases.items():
            v = self.verdict(name)
            self.assertEqual((v.category, v.folder), (category, folder), name)

    def test_russian_folder_names(self):
        self.assertEqual(self.verdict("report.pdf", "ru").folder, "Документы")
        self.assertEqual(self.verdict("photo.png", "ru").folder, "Изображения")

    def test_screenshots_need_an_image_extension(self):
        for name in [
            "Screenshot 2026-08-21 at 10.11.12.png",
            "Screen Shot 2025-01-02 at 3.04.05 PM.png",
            "Снимок экрана 2026-08-21 в 10.11.12.png",
            "Screenshot_20260821-101112.png",
            "Screenshot (12).png",
            "CleanShot 2026-08-21 at 10.11.12@2x.png",
        ]:
            self.assertEqual(self.verdict(name).category, "screenshots", name)
        self.assertEqual(self.verdict("Screenshot 2026-08-21.pdf").category, "documents")
        self.assertEqual(self.verdict("IMG_20260821_101112.jpg").category, "images")
        self.assertEqual(self.verdict("Снимок УЗИ.jpg").category, "images")
        self.assertEqual(self.verdict("photo_2026-08-21_10-11-12.jpg").category, "images")
        self.assertEqual(self.verdict("IMG-20260821-WA0001.jpg").category, "images")

    def test_skip_rules(self):
        self.assertEqual(self.verdict("big.iso.crdownload").skip, "partial-download")
        self.assertEqual(self.verdict("movie.mkv.part").skip, "partial-download")
        self.assertEqual(self.verdict("~$budget.xlsx").skip, "system-file")
        self.assertEqual(self.verdict(".DS_Store").skip, "system-file")
        self.assertEqual(self.verdict("Thumbs.db").skip, "system-file")
        self.assertIsNone(self.verdict("normal.pdf").skip)

    def test_name_rules_refine_folder_or_switch_category(self):
        self.assertEqual(self.verdict("Счёт 123.pdf").folder, "Documents/Finance")
        self.assertEqual(self.verdict("invoice_2026.xlsx", "ru").folder, "Документы/Финансы")
        self.assertEqual(self.verdict("Контакты.pdf").folder, "Documents")  # 'акт' inside a word must not match
        for name in ["Чек-лист.pdf", "Чек лист.xlsx", "Чеклист.pdf", "Bill Gates.pdf", "Mission statement.pdf", "Контракт.pdf"]:
            self.assertNotIn("Finance", self.verdict(name).folder, name)
        for name in ["Invoices 2025.pdf", "Счета за июль.pdf", "Налоговая декларация.pdf", "Чеки.pdf", "Акты 2025.xlsx", "Bank statement.pdf", "Оплата за август.pdf"]:
            self.assertIn("Finance", self.verdict(name).folder, name)
        key = self.verdict("id_rsa.key")
        self.assertEqual((key.category, key.sensitive), ("certificates", True))
        self.assertEqual(self.verdict("server.key", media_type="text").category, "certificates")
        self.assertEqual(self.verdict("id_rsa").category, "certificates")
        self.assertEqual(self.verdict("Quarterly deck.key").category, "presentations")
        for name in ["concert tickets.pdf", "Certificate of completion.pdf", "Secret Santa.docx", "private notes.pdf"]:
            v = self.verdict(name)
            self.assertEqual((v.category, v.sensitive), ("documents", False), name)
        self.assertEqual(self.verdict("cert-guide.pptx").category, "presentations")

    def test_media_type_fallback_then_other(self):
        self.assertEqual(self.verdict("weird.xyz", media_type="image").category, "images")
        self.assertEqual(self.verdict("weird.xyz", media_type="compressed").category, "archives")
        self.assertEqual(self.verdict("weird.xyz", media_type="unknown").category, "other")
        self.assertEqual(self.verdict("noext").category, "other")

    def test_ambiguous_extensions_use_media_type(self):
        self.assertEqual(self.verdict("episode.ts", media_type="video").category, "videos")
        self.assertEqual(self.verdict("app.ts", media_type="development").category, "code")
        self.assertEqual(self.verdict("app.ts").category, "videos")  # default when unknown
        self.assertEqual(self.verdict("disk.img", media_type="diskimage").category, "disk-images")
        self.assertEqual(self.verdict("scan.img", media_type="image").category, "images")
        self.assertEqual(self.verdict("server.key", media_type="encoded").category, "certificates")

    def test_split_extension(self):
        self.assertEqual(cls.split_extension("a.tar.gz"), ("a", "tar.gz"))
        self.assertEqual(cls.split_extension("a.b.c"), ("a.b", "c"))
        self.assertEqual(cls.split_extension(".hidden"), (".hidden", ""))
        self.assertEqual(cls.split_extension("README"), ("README", ""))

    def test_copy_stem(self):
        for name in ["report (1).pdf", "report (12).pdf", "report копия.pdf", "report копия 2.pdf", "report-copy.pdf", "report copy 3.pdf",
                     "report - Copy.pdf", "report - Copy (2).pdf", "report - копия.pdf", "report \u2013 копия (3).pdf"]:
            self.assertEqual(cls.copy_stem(name), "report.pdf", name)
        self.assertEqual(cls.copy_stem("report.pdf"), "report.pdf")
        self.assertEqual(cls.copy_stem("Budget (2024).xlsx"), "Budget (2024).xlsx")  # a year is not a copy counter

    def test_exact_duplicates_keep_cleanest_name(self):
        items = [
            {"name": "a (1).pdf", "path": "disk:/d/a (1).pdf", "md5": "x", "size": 10, "created": "2026-01-01T00:00:00+00:00"},
            {"name": "a.pdf", "path": "disk:/d/a.pdf", "md5": "x", "size": 10, "created": "2026-02-01T00:00:00+00:00"},
            {"name": "b.pdf", "path": "disk:/d/b.pdf", "md5": "y", "size": 10},
            {"name": "empty1", "path": "disk:/d/empty1", "md5": "z", "size": 0},
            {"name": "empty2", "path": "disk:/d/empty2", "md5": "z", "size": 0},
        ]
        groups = cls.find_exact_duplicates(items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["keep"]["name"], "a.pdf")
        self.assertEqual([e["name"] for e in groups[0]["extra"]], ["a (1).pdf"])
        twice = [{"name": "x.pdf", "path": "disk:/d/x.pdf", "md5": "q", "size": 3}] * 2
        self.assertEqual(cls.find_exact_duplicates(twice), [])  # the same path listed twice is not a duplicate

    def test_lookalikes_exclude_exact_duplicates(self):
        items = [
            {"name": "p.jpg", "md5": "1"},
            {"name": "p (1).jpg", "md5": "2"},
            {"name": "q.jpg", "md5": "3"},
            {"name": "q (1).jpg", "md5": "3"},
        ]
        looks = cls.find_name_lookalikes(items)
        self.assertEqual([g["base"] for g in looks], ["p.jpg"])

    def test_dates(self):
        self.assertEqual(cls.date_bucket({"created": "2026-08-21T10:00:00+00:00"}, "year"), "2026")
        self.assertEqual(cls.date_bucket({"created": "2026-08-21T10:00:00Z"}, "month"), "2026-08")
        self.assertEqual(cls.date_bucket({"created": "2026-08-21T10:00:00+00:00", "exif": {"date_time": "2020-01-05T00:00:00+00:00"}}, "year"), "2020")
        self.assertEqual(cls.date_bucket({}, "year"), "")
        self.assertEqual(cls.date_bucket({"created": "2026-08-21T10:00:00+00:00"}, "none"), "")

    def test_detect_lang(self):
        self.assertEqual(cls.detect_lang("disk:/Загрузки"), "ru")
        self.assertEqual(cls.detect_lang("disk:/Downloads"), "en")
        self.assertEqual(cls.detect_lang("disk:/Загрузки", "en"), "en")

    def test_human_size(self):
        self.assertEqual(cls.human_size(0), "0 B")
        self.assertEqual(cls.human_size(1536), "1.5 KB")
        self.assertEqual(cls.human_size(3 * 1024**3), "3.0 GB")


if __name__ == "__main__":
    unittest.main()
