import asyncio
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from telegram import Bot
from telegram.constants import ParseMode

BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "config.json") as f:
    CONFIGS = json.load(f)

if isinstance(CONFIGS, dict):
    CONFIGS = [CONFIGS]

DEFAULT_SELECTORS = {
    "seat_count_button": None,
    "select_seats_button": "button[aria-label='Select Seats']",
    "accessibility_button": "button[aria-label='Open accessibility seat selection']",
    "seat_map": "body",
    "sold_out": "text=/sold out|housefull|not available|Something is not right|connectivity issue|#5/i",
    "format_filter": None,
}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = int(os.getenv("TELEGRAM_USER_ID", "8537584726"))

bot = Bot(token=TELEGRAM_BOT_TOKEN)


class QuietSkip(Exception):
    pass


async def send_telegram_message(message):
    try:
        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=message, parse_mode=ParseMode.HTML)
        print("Telegram message sent!")
    except Exception as e:
        print("Failed to send Telegram message:", e)


def normalize_rows(rows):
    if isinstance(rows, str):
        rows = [row.strip() for row in rows.split(",")]
    return [row.upper() for row in rows if row]


def expand_row_range(row_range):
    if not row_range:
        return []

    row_range = str(row_range).strip().upper()
    match = re.fullmatch(r"([A-Z])-([A-Z])", row_range)
    if not match:
        return normalize_rows(row_range)

    start = ord(match.group(1))
    end = ord(match.group(2))
    if start > end:
        start, end = end, start

    return [chr(code) for code in range(start, end + 1)]


def parse_seat_targets(rows):
    targets = []
    for token in normalize_rows(rows):
        match = re.fullmatch(r"([A-Z]+)\s*[- ]?\s*(\d+)", token)
        if match:
            targets.append({"row": match.group(1), "seat": match.group(2).zfill(2)})
        else:
            targets.append({"row": token, "seat": None})
    return targets


def parse_seat_range(seat_range):
    if not seat_range:
        return None

    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(seat_range))
    if not match:
        seat = str(seat_range).strip().zfill(2)
        return seat, seat

    start = int(match.group(1))
    end = int(match.group(2))
    if start > end:
        start, end = end, start

    return str(start).zfill(2), str(end).zfill(2)


def build_targets(cfg):
    if cfg.get("row_range"):
        return [{"row": row, "seat": None} for row in expand_row_range(cfg["row_range"])]

    return parse_seat_targets(cfg["rows"])


def seat_in_range(seat_number, seat_range):
    if not seat_range:
        return True
    if not seat_number:
        return False

    start, end = seat_range
    return int(start) <= int(seat_number) <= int(end)


def extract_seat_number(text):
    match = re.search(r"seat\s+(\d+)", text, re.IGNORECASE)
    return match.group(1).zfill(2) if match else ""


def get_selectors(cfg):
    selectors = DEFAULT_SELECTORS.copy()
    selectors.update(cfg.get("selectors", {}))
    return selectors


def normalize_showtime(showtime):
    showtime = str(showtime).strip().upper()
    if showtime.endswith(" AM") or showtime.endswith(" PM"):
        return showtime

    match = re.fullmatch(r"(\d{1,2}):(\d{2})", showtime)
    if not match:
        return showtime

    hour = int(match.group(1))
    minute = match.group(2)
    suffix = "AM" if hour < 12 else "PM"
    hour_12 = hour % 12 or 12
    return f"{hour_12:02d}:{minute} {suffix}"


def date_from_movie_url(movie_url):
    match = re.search(r"(20\d{2})(\d{2})(\d{2})", movie_url)
    if not match:
        return ""
    return match.group(3).lstrip("0")


def render_selector(selector, cfg):
    if not selector:
        return selector
    return selector.format(
        showtime=normalize_showtime(cfg.get("showtime", "")),
        format=cfg.get("format", ""),
        seat_count=cfg.get("seat_count", "1"),
    )


def log_to_file(message, movie_name):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_path = BASE_DIR / f"log_{movie_name.replace(' ', '_')}.txt"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{now}] {message}\n\n")


async def click_if_present(page, selector, label, timeout=4000):
    if not selector:
        return False
    try:
        target = page.locator(selector).first
        await target.wait_for(state="visible", timeout=timeout)
        await target.click()
        print(f"Clicked {label}")
        return True
    except Exception:
        return False


async def click_select_seats_or_choose_count(page, cfg, selectors):
    select_seats_selector = render_selector(selectors["select_seats_button"], cfg)
    if await click_if_present(page, select_seats_selector, "select seats button", timeout=1000):
        return

    seat_count = str(cfg.get("seat_count", "1"))
    seat_count_selector = render_selector(selectors["seat_count_button"], cfg) or f"text=/^{seat_count}$/"
    await click_if_present(page, seat_count_selector, f"{seat_count} seat option", timeout=1000)

    if await click_if_present(page, select_seats_selector, "select seats button", timeout=3000):
        return

    raise QuietSkip("Select Seats button did not appear. Showtime is likely sold out or unavailable.")


async def check_bms_error(page):
    cloudflare_block = page.locator("text=/Sorry, you have been blocked|Cloudflare Ray ID|unable to access bookmyshow\\.com/i")
    if await cloudflare_block.count() > 0:
        raise RuntimeError(
            "BookMyShow blocked this automated browser with Cloudflare. "
            "The page did not load dates/showtimes."
        )

    error = page.locator("text=/Something is not right|connectivity issue|#5/i")
    if await error.count() > 0:
        raise QuietSkip("BMS did not open seat layout for this show.")


async def prepare_bms_seat_page(page, cfg, selectors):
    await page.goto(cfg["movie_url"], wait_until="domcontentloaded", timeout=60000)
    await check_bms_error(page)

    await click_if_present(page, render_selector(selectors["format_filter"], cfg), "format filter")

    showtime = normalize_showtime(cfg["showtime"])
    showtime_button = page.locator(
        f"xpath=//div[@role='button' and contains(@aria-label, '{showtime}')]"
    )

    if await showtime_button.count() == 0:
        body_text = await page.locator("body").inner_text()
        raise RuntimeError(
            f"Showtime '{showtime}' not found on BMS page. Page says: "
            + body_text[:700]
        )

    showtime_class = await showtime_button.first.get_attribute("class") or ""
    showtime_text = (await showtime_button.first.inner_text()).lower()
    if "grey" in showtime_class.lower() or "sold" in showtime_text:
        raise QuietSkip(f"Showtime '{showtime}' is sold out or unavailable.")

    await showtime_button.first.click()
    await page.wait_for_timeout(1000)

    await click_select_seats_or_choose_count(page, cfg, selectors)
    await check_bms_error(page)
    await page.wait_for_selector(selectors["seat_map"], timeout=15000)


async def get_select_options(page, label):
    return await page.get_by_label(label, exact=True).evaluate(
        """select => Array.from(select.options).map(option => ({
            label: option.label || option.textContent.trim(),
            value: option.value,
            disabled: option.disabled
        }))"""
    )


async def select_first_matching_option(page, label, preferred_text=None):
    select = page.get_by_label(label, exact=True)
    await select.wait_for(state="visible", timeout=10000)
    options = await get_select_options(page, label)

    if preferred_text:
        preferred = preferred_text.lower()
        for option in options:
            if not option["disabled"] and preferred in option["label"].lower():
                await select.select_option(value=option["value"])
                return option["label"]

    for option in options:
        if not option["disabled"] and not option["label"].lower().startswith("select"):
            await select.select_option(value=option["value"])
            return option["label"]

    raise RuntimeError(f"No selectable option found for '{label}'.")


async def read_available_seats_from_accessibility(page, cfg, selectors):
    await click_if_present(
        page,
        render_selector(selectors["accessibility_button"], cfg),
        "accessibility seat selection",
        timeout=8000,
    )
    await check_bms_error(page)

    category_dropdown = page.get_by_label("Select seat category", exact=True)
    if await category_dropdown.count() == 0:
        body_text = await page.locator("body").inner_text()
        raise QuietSkip("BMS did not load seat categories. Page says: " + body_text[:500])

    preferred_category = cfg.get("seat_category")
    category_label = await select_first_matching_option(page, "Select seat category", preferred_category)
    price = category_label.split("₹")[-1].strip() if "₹" in category_label else "?"

    row_map = defaultdict(list)
    targets = build_targets(cfg)
    seat_range = parse_seat_range(cfg.get("seat_range"))

    for target in targets:
        row = target["row"]
        wanted_seat = target["seat"]
        row_label = await select_first_matching_option(page, "Select row", f"row {row}")
        await page.wait_for_timeout(500)

        available_buttons = page.locator(f"button[aria-label^='Select seat'][aria-label*=' in row {row}']")
        button_count = await available_buttons.count()

        for index in range(button_count):
            button = available_buttons.nth(index)
            aria_label = await button.get_attribute("aria-label") or ""
            seat_number = extract_seat_number(aria_label)

            if wanted_seat and seat_number != wanted_seat:
                continue
            if not seat_in_range(seat_number, seat_range):
                continue

            row_map[row].append((seat_number or "?", price))

        if wanted_seat and not row_map[row]:
            print(f"Seat {row}{wanted_seat} is not available. Checked {row_label}.")

    return row_map


async def check_seats_for_show(cfg):
    movie_name = cfg["movie_name"]
    showdate = date_from_movie_url(cfg["movie_url"])
    showtime = cfg.get("showtime", "")
    seat_row = cfg.get("row_range") or cfg["rows"]
    seat_range = cfg.get("seat_range")
    selectors = get_selectors(cfg)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        print(f"Checking {movie_name} on {showdate} at {showtime}...")

        try:
            await prepare_bms_seat_page(page, cfg, selectors)
            row_map = await read_available_seats_from_accessibility(page, cfg, selectors)

            if row_map:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                range_text = f"Rows {seat_row}"
                if seat_range:
                    range_text += f", Seats {seat_range}"
                message_lines = [f"🎯 <b>{movie_name}</b> | <i>{showdate}, {showtime}</i> | {range_text} at {now}:\n"]
                for row in sorted(row_map):
                    cols = ", ".join([col for col, _ in row_map[row]])
                    price = row_map[row][0][1]
                    message_lines.append(f"{row} - {cols} (₹{price})")
                final_message = "\n".join(message_lines)
                print(final_message)
                await send_telegram_message(final_message)
                log_to_file(final_message, movie_name)
            else:
                print(f"No matching seats found for {movie_name} on {showdate}")

        except QuietSkip as e:
            print(f"Skipping {movie_name}: {e}")
        except Exception as e:
            print(f"Error for {movie_name}:", e)
        finally:
            await browser.close()


async def run_all():
    while True:
        for cfg in CONFIGS:
            await check_seats_for_show(cfg)
        if os.getenv("RUN_ONCE") == "1":
            break
        print("Sleeping for 3 minutes before next cycle...\n")
        await asyncio.sleep(180)


if __name__ == "__main__":
    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        print("Stopped by user.")
