import csv
import tempfile
import unittest
from pathlib import Path

import lookup_csv


TARGET_QQ = "2929243450"


class ResumeCheckpointTests(unittest.TestCase):
    def test_settings_are_loaded_from_config_file(self):
        self.assertEqual(lookup_csv.URL, "https://avsov.com/tel_qq.html")
        self.assertEqual(lookup_csv.TARGET_QQ, "2929243450")

    def test_save_and_load_checkpoint(self):
        checkpoint = lookup_csv.CHECKPOINT_FILE
        lookup_csv.save_checkpoint(7, checkpoint)

        self.assertEqual(lookup_csv.load_checkpoint(checkpoint), 7)

    def test_resume_starts_after_last_completed_row(self):
        checkpoint = lookup_csv.CHECKPOINT_FILE
        lookup_csv.save_checkpoint(5, checkpoint)

        start_after_row = lookup_csv.load_checkpoint(checkpoint)
        rows = list(lookup_csv.read_phones(start_after_row=start_after_row))

        self.assertEqual(rows[0], (6, "13400000459"))
        self.assertEqual(rows[1], (7, "13400000559"))

    def test_target_qq_is_detected(self):
        self.assertTrue(lookup_csv.is_target_qq(TARGET_QQ))
        self.assertFalse(lookup_csv.is_target_qq("123456789"))

    def test_query_retries_until_success(self):
        calls = []
        pages = [object(), object()]

        def fake_query(page, phone, dialog_messages):
            calls.append(page)
            if len(calls) < 3:
                raise RuntimeError("temporary error")
            return "captured", "2929243450", "查询成功！"

        def reset_page():
            return pages[len(calls) - 1]

        self.assertEqual(
            lookup_csv.run_query_until_success(
                pages[0],
                "13400000059",
                [],
                fake_query,
                reset_page,
            ),
            ("captured", "2929243450", "查询成功！"),
        )
        self.assertEqual(len(calls), 3)

    def test_progress_bar_formats_completed_and_percentage(self):
        expected = f"[{ '█' * 10}{' ' * 30} 25.0%] 250/1000"
        self.assertEqual(lookup_csv.format_progress(250, 1000, 40), expected)

    def test_assigns_10_workers_with_100000_rows_each(self):
        workers = lookup_csv.assign_workers(10, 1_000_000)

        self.assertEqual(len(workers), 10)
        self.assertEqual(
            workers,
            [
                (1, 1, 100000),
                (2, 100001, 200000),
                (3, 200001, 300000),
                (4, 300001, 400000),
                (5, 400001, 500000),
                (6, 500001, 600000),
                (7, 600001, 700000),
                (8, 700001, 800000),
                (9, 800001, 900000),
                (10, 900001, 1_000_000),
            ],
        )

    def test_10_workers_have_independent_checkpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            worker_1_checkpoint = Path(temp_dir) / "worker_1_resume.txt"
            worker_2_checkpoint = Path(temp_dir) / "worker_2_resume.txt"

            lookup_csv.save_checkpoint(100, worker_1_checkpoint)
            lookup_csv.save_checkpoint(200, worker_2_checkpoint)

            self.assertEqual(
                lookup_csv.load_checkpoint(worker_1_checkpoint),
                100,
            )
            self.assertEqual(
                lookup_csv.load_checkpoint(worker_2_checkpoint),
                200,
            )

    def test_existing_result_phones_are_skipped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "results.csv"
            output.write_text(
                "csv_row,phone,qq,status,message,checked_at\n"
                "1,13400000059,373452857,captured,,2026-10-07T00:00:00+00:00\n"
                "2,13400000159,,not_leaked,,2026-10-07T00:00:00+00:00\n",
                encoding="utf-8",
            )

            processed = lookup_csv.load_processed_phones(output)

            self.assertTrue(
                lookup_csv.is_duplicate_phone("13400000059", processed)
            )
            self.assertTrue(
                lookup_csv.is_duplicate_phone("13400000159", processed)
            )
            self.assertFalse(
                lookup_csv.is_duplicate_phone("13400000259", processed)
            )

    def test_mark_phone_processed_uses_set_semantics(self):
        processed = set()

        self.assertFalse(lookup_csv.mark_phone_processed("13400000059", processed, None))
        self.assertTrue(lookup_csv.mark_phone_processed("13400000059", processed, None))
        self.assertFalse(lookup_csv.mark_phone_processed("13400000159", processed, None))

    def test_legacy_results_are_migrated_to_worker_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "results.csv"
            output.write_text(
                "csv_row,phone,qq,status,message,checked_at\n"
                "1,13400000059,373452857,captured,,2026-10-07T00:00:00+00:00\n",
                encoding="utf-8",
            )

            lookup_csv.ResultWriter(output).write({
                "worker_id": "1",
                "csv_row": "2",
                "phone": "13400000159",
                "qq": "",
                "status": "not_leaked",
                "message": "未泄露！",
                "checked_at": "2026-10-07T00:00:00+00:00",
            })

            with output.open("r", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))

            self.assertEqual(rows[0]["worker_id"], "")
            self.assertEqual(rows[0]["phone"], "13400000059")
            self.assertEqual(rows[1]["worker_id"], "1")
            self.assertEqual(rows[1]["phone"], "13400000159")


if __name__ == "__main__":
    unittest.main()
