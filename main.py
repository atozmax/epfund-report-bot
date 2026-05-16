import asyncio
import os
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Union

import requests
from PIL import Image, ImageDraw, ImageFont
from telegram import Bot

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set")

CHAT_IDS = [
    -1003702041232,
]

TREASURY_API = "https://api.epfund.org/v1/landing/home/treasury-info"
INPUT_IMAGE = "input.jpg"

# Center of each value area (fractions of image width/height)
VALUE_BOXES = [
    (0.08, 0.40, 0.92, 0.52),  # Insurance Reserve Capital
    (0.08, 0.54, 0.92, 0.66),  # At Risk
    (0.08, 0.68, 0.92, 0.80),  # Profit Ratio
]


def load_font(size: int) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def fetch_treasury_info() -> dict:
    response = requests.get(TREASURY_API, timeout=30)
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
    if at_risk == 0:
        return "N/A"
    ratio = reserve / at_risk
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
        format_amount(reserve),
        format_amount(at_risk),
        profit_ratio,
    ]

    image = Image.open(INPUT_IMAGE).convert("RGB")
    draw = ImageDraw.Draw(image)
    font_size = max(28, image.width // 22)
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


def main() -> None:
    asyncio.run(send_report_to_chats())


if __name__ == "__main__":
    main()
