import asyncio
import os
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Union

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from telegram import Bot

load_dotenv(Path(__file__).resolve().parent / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set")

CHAT_IDS = [int(os.environ.get("CHAT_ID_1"))]

if not CHAT_IDS:
    raise ValueError("CHAT_ID_1 is not set")
    
TREASURY_API = "https://api.epfund.org/v1/landing/home/treasury-info"
INPUT_IMAGE = "input.png"

# Center of each value area (fractions of image width/height)
VALUE_BOXES = [
    (0, 0.40, 0.55, 0.62),  # Insurance Reserve Capital (no risk)
    (0, 0.54, 0.34, 0.80),  # At Risk
    (0, 0.68, 0.42, 1.03),  # Profit Ratio
]

def load_font(size: int) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    candidates = [
        # "./fonts/IRANYekanRegular.ttf",
        # "./fonts/IRANYekanBold.ttf",
        # "./fonts/IRANYekanExtraBold.ttf",
        "./fonts/IRANYekanExtraBlack.ttf"
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def fetch_treasury_info() -> dict:
    response = httpx.get(TREASURY_API, timeout=30.0)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError("Treasury API returned success=false")
    return payload["result"]


def parse_amount(value: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"Invalid numeric value: {value!r}") from exc


def format_amount(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"))
    text = f"{quantized:,.2f}"
    if text.endswith(".00"):
        return text[:-3]
    return text


def format_profit_ratio(reserve: Decimal, at_risk: Decimal) -> str:
    if reserve == 0:
        return "N/A"
    ratio =  at_risk / reserve
    return f"{ratio:,.2f}"


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: tuple[float, float, float, float],
    font: Union[ImageFont.FreeTypeFont, ImageFont.ImageFont],
    fill: str = "#2d2d2d",
) -> None:
    width, height = draw.im.size
    x0 = int(box[0] * width)
    y0 = int(box[1] * height)
    x1 = int(box[2] * width)
    y1 = int(box[3] * height)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = x0 + (x1 - x0 - text_width) / 2
    y = y0 + (y1 - y0 - text_height) / 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=fill)


def build_report_image(result: dict) -> bytes:
    reserve = parse_amount(result["insuranceReserveCapital"])
    at_risk = parse_amount(result["atRisk"])
    profit_ratio = format_profit_ratio(reserve, at_risk)

    values = [
        f'${format_amount(reserve)}',
        f'${format_amount(at_risk)}',
        profit_ratio,
    ]

    image = Image.open(INPUT_IMAGE).convert("RGB")
    draw = ImageDraw.Draw(image)
    font_size = 96
    # max(28, image.width // 22)
    font = load_font(font_size)

    for value, box in zip(values, VALUE_BOXES):
        draw_centered_text(draw, value, box, font)

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    buffer.seek(0)
    return buffer.getvalue()


async def send_report_to_chats() -> None:
    result = fetch_treasury_info()
    image_bytes = build_report_image(result)

    bot = Bot(token=BOT_TOKEN)
    for chat_id in CHAT_IDS:
        await bot.send_photo(chat_id=chat_id, photo=image_bytes)


def run_report() -> None:
    asyncio.run(send_report_to_chats())


def main() -> None:
    run_report()
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_report,
        trigger=CronTrigger(minute="0,10,20,30,40,50"),
        id="treasury_report",
        name="Send treasury report",
    )
    print("Scheduler running — reports at :00, :10, :20, :30, :40, :50 each hour")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
