import csv
import json
import multiprocessing
import re
import time
from datetime import datetime
from pathlib import Path

import fcntl
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "settings.json"
INPUT_FILE = BASE_DIR / "input.csv"
OUTPUT_FILE = BASE_DIR / "results.csv"
CHECKPOINT_FILE = BASE_DIR / "lookup_resume.txt"

with SETTINGS_FILE.open("r", encoding="utf-8") as settings_file:
    _settings = json.load(settings_file)

URL = _settings["URL"]
TARGET_QQ = _settings["TARGET_QQ"]

HAS_HEADER = True
PHONE_COLUMN_INDEX = 0

# Maintain one sequential query at a time while using the shortest
# practical delay and timeout to minimize total runtime.
TIMEOUT_MS = 5_000
DELAY_SECONDS = 0.5
OUTPUT_FIELDS = [
    "worker_id",
    "csv_row",
    "phone",
    "qq",
    "status",
    "message",
    "checked_at",
]


def run_query_until_success(
    page, phone, dialog_messages, query_func, reset_page_func
):
    attempt = 0
    while True:
        attempt += 1
        try:
            return query_func(page, phone, dialog_messages)
        except Exception as exc:
            print(
                f"Query failed for {phone} (attempt {attempt}): "
                f"{type(exc).__name__}: {exc}"
            )
            page = reset_page_func()
            time.sleep(DELAY_SECONDS)


def format_progress(completed, total, bar_width=40):
    if total <= 0:
        return f"[{ '█' * bar_width }] 0.0% 0/{total}"

    percentage = completed / total * 100
    filled = min(bar_width, int(round(percentage / 100 * bar_width)))
    bar = "█" * filled + " " * (bar_width - filled)
    return f"[{bar} {percentage:.1f}%] {completed}/{total}"


def load_checkpoint(checkpoint_path=CHECKPOINT_FILE):
    if not checkpoint_path.exists():
        return 1

    try:
        value = checkpoint_path.read_text(
            encoding="utf-8"
        ).strip()
        return max(1, int(value))
    except (ValueError, TypeError):
        return 1


def save_checkpoint(csv_row, checkpoint_path=CHECKPOINT_FILE):
    checkpoint_path.write_text(str(csv_row), encoding="utf-8")


def assign_workers(worker_count, total_rows):
    if worker_count <= 0 or total_rows <= 0:
        return []

    rows_per_worker = total_rows // worker_count
    remainder = total_rows % worker_count
    workers = []
    start_row = 1

    for worker_id in range(1, worker_count + 1):
        size = rows_per_worker + (1 if worker_id <= remainder else 0)
        end_row = start_row + size - 1
        workers.append((worker_id, start_row, end_row))
        start_row = end_row + 1

    return workers


def load_processed_phones(output_file=OUTPUT_FILE):
    if not output_file.exists():
        return set()

    processed = set()
    with output_file.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            phone = (row.get("phone") or "").strip()
            if phone and re.fullmatch(r"[0-9]+", phone):
                processed.add(phone)

    return processed


def is_duplicate_phone(phone, phones_seen):
    return phone in phones_seen


def read_phones(start_after_row=1):
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"CSV not found: {INPUT_FILE}")

    with INPUT_FILE.open(
        "r", encoding="utf-8-sig", newline=""
    ) as file:
        reader = csv.reader(file)

        if HAS_HEADER:
            next(reader, None)

        first_row = 2 if HAS_HEADER else 1

        for csv_row, row in enumerate(reader, start=first_row):
            if csv_row <= start_after_row:
                continue

            if not row or not any(cell.strip() for cell in row):
                continue

            phone = (
                row[PHONE_COLUMN_INDEX].strip()
                if len(row) > PHONE_COLUMN_INDEX
                else ""
            )

            yield csv_row, phone


def click_confirm_if_present(page):
    """Close the HTML confirmation shown in the recorded workflow."""
    confirm = page.get_by_text("确定", exact=True).first

    try:
        confirm.click(timeout=800)
    except PlaywrightTimeoutError:
        pass


def is_target_qq(qq):
    return qq == TARGET_QQ


def click_warning_and_confirm_if_present(page):
    """Accept the usage warning when it appears before querying."""
    warning = page.get_by_text(
        "本站数据仅用作安全检查，严禁用于违法用途！",
        exact=True,
    ).first

    try:
        warning.click(timeout=800)
    except PlaywrightTimeoutError:
        pass

    click_confirm_if_present(page)


def read_qq_candidates(page):
    """
    Inspect the QQ label and its nearby containers.
    Never search the whole page for arbitrary numbers.
    """
    labels = page.get_by_text(
        re.compile(r"^\s*QQ\s*[:：]", re.IGNORECASE)
    )

    return labels.evaluate_all(
        r"""
        elements => {
            const values = new Set();

            for (const element of elements) {
                let node = element;

                for (let depth = 0; depth < 3 && node; depth++) {
                    if (["BODY", "HTML"].includes(node.tagName)) {
                        break;
                    }

                    const text = node.innerText || "";
                    const pattern = /QQ\s*[:：]\s*(\d+)(?!\d)/gi;

                    for (const match of text.matchAll(pattern)) {
                        values.add(match[1]);
                    }

                    node = node.parentElement;
                }
            }

            return [...values];
        }
        """
    )


def query_phone(page, phone, dialog_messages):
    response = page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=TIMEOUT_MS,
    )

    if response and response.status >= 400:
        raise RuntimeError(f"HTTP {response.status}")

    click_warning_and_confirm_if_present(page)

    phone_input = page.get_by_role(
        "spinbutton",
        name="请输入手机号",
    )
    phone_input.fill(phone)

    # Reject pre-existing values to avoid recording stale results.
    if read_qq_candidates(page):
        raise RuntimeError(
            "A QQ value was present before submitting the query. "
            "The result locator needs adjustment."
        )

    dialog_messages.clear()
    page.get_by_role("button", name="查询", exact=True).click()

    deadline = time.monotonic() + TIMEOUT_MS / 1000
    status_message = "No result was detected."

    while time.monotonic() < deadline:
        click_confirm_if_present(page)

        candidates = read_qq_candidates(page)
        if len(candidates) == 1:
            return "captured", candidates[0], "查询成功！"

        if len(candidates) > 1:
            raise RuntimeError(
                "Multiple QQ values were detected. "
                "A more specific result locator is required."
            )

        result_locator = page.get_by_text("未泄露！", exact=True)
        if result_locator.is_visible(timeout=200):
            return "not_leaked", "", "未泄露！"

        if dialog_messages:
            status_message = " | ".join(dialog_messages)

        page.wait_for_timeout(300)

    raise PlaywrightTimeoutError(status_message)


class ResultWriter:
    def __init__(self, output_file=OUTPUT_FILE):
        self.output_file = output_file
        self.lock_file = BASE_DIR / ".results.lock"

    def write(self, record):
        with self.lock_file.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                if self.output_file.exists():
                    with self.output_file.open(
                        "r", encoding="utf-8-sig", newline=""
                    ) as source:
                        reader = csv.reader(source)
                        rows = list(reader)

                    if rows and rows[0] != OUTPUT_FIELDS:
                        migrated = [OUTPUT_FIELDS]
                        for row in rows[1:]:
                            if not row:
                                continue
                            if len(row) == len(OUTPUT_FIELDS):
                                migrated.append(row)
                            else:
                                migrated.append(["", *row])

                        with self.output_file.open(
                            "w", encoding="utf-8", newline=""
                        ) as output:
                            writer = csv.writer(output)
                            writer.writerows(migrated)
                with self.output_file.open(
                    "a", encoding="utf-8-sig", newline=""
                ) as output:
                    writer = csv.DictWriter(
                        output, fieldnames=OUTPUT_FIELDS
                    )
                    if self.output_file.stat().st_size == 0:
                        writer.writeheader()
                    writer.writerow(record)
                    output.flush()
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def mark_phone_processed(phone, processed_phones, duplicate_lock):
    if duplicate_lock is not None:
        with duplicate_lock:
            if phone in processed_phones:
                return True
            processed_phones.add(phone)
            return False

    if phone in processed_phones:
        return True
    processed_phones.add(phone)
    return False


def sort_results_by_csv_row(output_file=OUTPUT_FILE):
    if not output_file.exists():
        return

    with output_file.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))

    if not rows:
        return

    rows.sort(key=lambda row: int(row["csv_row"]))

    with output_file.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def migrate_legacy_results(output_file=OUTPUT_FILE):
    if not output_file.exists():
        return

    with output_file.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file)
        rows = list(reader)

    if not rows or rows[0] == OUTPUT_FIELDS:
        return

    if rows[0] and rows[0][0] == "worker_id":
        return

    migrated = [OUTPUT_FIELDS]
    for row in rows[1:]:
        if not row:
            continue
        if len(row) == len(OUTPUT_FIELDS):
            migrated.append(row)
            continue
        legacy = ["", *row]
        migrated.append(legacy)

    with output_file.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerows(migrated)


def run_worker(
    worker_id,
    start_row,
    end_row,
    result_writer,
    processed_phones,
    duplicate_lock,
    progress_counter,
    checkpoint_path=CHECKPOINT_FILE,
):
    start_after_row = load_checkpoint(checkpoint_path)
    if start_after_row < start_row:
        start_after_row = start_row

    phones = list(read_phones(start_after_row=start_after_row))
    phones = [
        (csv_row, phone)
        for csv_row, phone in phones
        if start_row <= csv_row <= end_row
    ]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-background-networking"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)

        dialog_messages = []

        def handle_dialog(dialog):
            dialog_messages.append(dialog.message)
            dialog.dismiss()

        def create_page():
            nonlocal page
            page = context.new_page()
            page.set_default_timeout(TIMEOUT_MS)
            page.on("dialog", handle_dialog)
            return page

        create_page()

        try:
            for index, (csv_row, phone) in enumerate(phones, start=1):
                if mark_phone_processed(
                    phone, processed_phones, duplicate_lock
                ):
                    record = {
                        "worker_id": worker_id,
                        "csv_row": csv_row,
                        "phone": phone,
                        "qq": "",
                        "status": "duplicate_phone",
                        "message": "Skipped because another Worker processed this phone number.",
                        "checked_at": datetime.now()
                        .astimezone()
                        .isoformat(timespec="seconds"),
                    }
                    result_writer.write(record)
                    save_checkpoint(csv_row, checkpoint_path)
                    continue

                record = {
                    "worker_id": worker_id,
                    "csv_row": csv_row,
                    "phone": phone,
                    "qq": "",
                    "status": "",
                    "message": "",
                    "checked_at": "",
                }

                if not re.fullmatch(r"[0-9]+", phone):
                    record["status"] = "invalid_input"
                    record["message"] = "Expected a non-empty string of digits."
                else:
                    status, qq, message = run_query_until_success(
                        page,
                        phone,
                        dialog_messages,
                        query_phone,
                        create_page,
                    )
                    record["status"] = status
                    record["qq"] = qq
                    record["message"] = message

                record["checked_at"] = (
                    datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds")
                )
                result_writer.write(record)
                save_checkpoint(csv_row, checkpoint_path)
                progress_counter.value += 1

                print(
                    f"Worker {worker_id}: row {csv_row}: "
                    f"{record['status']}"
                )

                if is_target_qq(record["qq"]):
                    print(
                        "\nTarget QQ found!\n"
                        f"Worker: {worker_id}\n"
                        f"CSV row: {record['csv_row']}\n"
                        f"Phone: {record['phone']}\n"
                        f"QQ: {record['qq']}\n"
                        f"Status: {record['status']}"
                    )
                    return

                if index < len(phones):
                    time.sleep(DELAY_SECONDS)

        finally:
            context.close()
            browser.close()


def main():
    migrate_legacy_results()

    total_rows = sum(1 for _ in read_phones())
    worker_id = 1
    start_row, end_row = 1, total_rows
    processed_phones = load_processed_phones()
    duplicate_lock = multiprocessing.Lock()
    progress_counter = multiprocessing.Value("i", 0)

    print(f"Total rows: {total_rows}")
    print(f"Worker {worker_id}: rows {start_row}-{end_row}")

    run_worker(
        worker_id,
        start_row,
        end_row,
        ResultWriter(),
        processed_phones,
        duplicate_lock,
        progress_counter,
        checkpoint_path=CHECKPOINT_FILE,
    )

    print(format_progress(progress_counter.value, total_rows))
    sort_results_by_csv_row()
    print(f"Results saved to: {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
