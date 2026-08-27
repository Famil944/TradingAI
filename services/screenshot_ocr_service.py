import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


WINDOWS_PATHS = (
    Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
)


@dataclass(frozen=True)
class ScreenshotTradeData:
    entry_price: float | None
    quantity: float | None
    opened_at: str | None
    raw_text: str


def find_tesseract() -> str | None:
    command = shutil.which("tesseract")
    if command:
        return command
    return next((str(path) for path in WINDOWS_PATHS if path.exists()), None)


def _number(value: str) -> float:
    return float(value.replace(" ", "").replace(",", "."))


def parse_binance_screenshot_text(text: str) -> ScreenshotTradeData:
    normalized = text.replace("−", "-").replace("—", "-")
    # Tesseract часто читает ноль перед запятой как латинскую O.
    normalized = re.sub(r"(?<![\w])[Oo](?=[.,]\d)", "0", normalized)
    # Binance может показывать как $128,72, так и целую себестоимость $253.
    # Знак доллара иногда распознаётся как S.
    price_match = re.search(
        r"(?:\$|(?<![A-Za-z])S)\s*([0-9][0-9\s]*(?:[.,][0-9]+)?)",
        normalized,
    )
    entry_price = _number(price_match.group(1)) if price_match else None

    quantity = None
    candidates = []
    for line in normalized.splitlines():
        lowered = line.lower()
        if "usdt" in lowered or "pnl" in lowered or "комисс" in lowered:
            continue
        for value in re.findall(r"(?<![\d])0[.,]\d{4,}(?![\d])", line):
            candidates.append(_number(value))
    if candidates:
        # Баланс после комиссии обычно имеет больше знаков и находится сверху.
        quantity = max(candidates, key=lambda value: len(str(value).split(".")[-1]))

    date_match = re.search(
        r"(\d{1,2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{4})\s+[г.]*\s*"
        r"(\d{1,2}):(\d{2}):(\d{2})",
        normalized,
    )
    word_date_match = re.search(
        r"(\d{1,2})\s+(янв|фев|мар|апр|май|мая|июн|июл|авг|сен|окт|ноя|дек)"
        r"[а-я.]*\s+(\d{4})\s+[г.]*\s*(\d{1,2}):(\d{2}):(\d{2})",
        normalized.lower(),
    )
    opened_at = None
    if date_match:
        day, month, year, hour, minute, second = date_match.groups()
        opened_at = (
            f"{int(year):04d}-{int(month):02d}-{int(day):02d} "
            f"{int(hour):02d}:{int(minute):02d}:{int(second):02d}"
        )
    elif word_date_match:
        day, month_word, year, hour, minute, second = word_date_match.groups()
        months = {
            "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5,
            "мая": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9,
            "окт": 10, "ноя": 11, "дек": 12,
        }
        opened_at = (
            f"{int(year):04d}-{months[month_word]:02d}-{int(day):02d} "
            f"{int(hour):02d}:{int(minute):02d}:{int(second):02d}"
        )
    return ScreenshotTradeData(entry_price, quantity, opened_at, text)


async def recognize_binance_screenshot(path: str | Path) -> ScreenshotTradeData:
    executable = find_tesseract()
    if not executable:
        raise RuntimeError("Tesseract OCR не установлен рядом с ботом")
    process = await asyncio.create_subprocess_exec(
        executable, str(path), "stdout", "-l", "eng", "--psm", "6",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace").strip())
    return parse_binance_screenshot_text(stdout.decode(errors="replace"))
