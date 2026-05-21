import asyncio
import os
import uuid
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Union

import qrcode
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from PIL import Image, ImageDraw, ImageFont
from telegram import Bot

load_dotenv(Path(__file__).resolve().parent / ".env")

ROOT_DIR = Path(__file__).resolve().parent
FAILED_IMAGE = ROOT_DIR / "images" / "failed.png"
DATABASE_DIR = ROOT_DIR / "database"
TEXT_FILL = "#2d2d2d"
REASON_FILL = "#c62828"
DATE_FILL = "#1e6fd9"

USERNAME_FONT_SIZE = 32
LOGIN_FONT_SIZE = 24
BODY_FONT_SIZE = 28
DATE_FONT_SIZE = 18

# Layout as fractions of image (width, height)
RIGHT_MARGIN = 0.04
DATE_TOP_Y = 0.03
QR_TOP_Y = 0.08
QR_SIZE_RATIO = 0.12

USER_LOGIN_TOP_Y = 0.10
USER_LOGIN_LEFT_MARGIN = 0.04
USER_LOGIN_GAP = 14

# Shared left edge for final equity and reason (left-aligned, not centered)
LEFT_TEXT_X = 0.04
FINAL_EQUITY_BOX = (LEFT_TEXT_X+ 0.1, 0.42, 0.78, 0.52)
REASON_BOX = (LEFT_TEXT_X+ 0.1, 0.6, 0.78, 0.68)

app = Flask(__name__)


def load_font(size: int) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    candidates = [
        "./fonts/IRANYekanExtraBold.ttf",
        "./fonts/IRANYekanBold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_box_text_left(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: tuple[float, float, float, float],
    font: Union[ImageFont.FreeTypeFont, ImageFont.ImageFont],
    fill: str = TEXT_FILL,
) -> None:
    width, height = draw.im.size
    x = int(box[0] * width)
    y0 = int(box[1] * height)
    y1 = int(box[3] * height)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_height = bbox[3] - bbox[1]
    y = y0 + (y1 - y0 - text_height) / 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=fill)


def draw_username_login(
    draw: ImageDraw.ImageDraw,
    username: str,
    login: str,
    image_width: int,
    image_height: int,
) -> None:
    font_username = load_font(USERNAME_FONT_SIZE)
    font_login = load_font(LOGIN_FONT_SIZE)

    user_bbox = draw.textbbox((0, 0), username, font=font_username)
    login_bbox = draw.textbbox((0, 0), login, font=font_login)
    user_width = user_bbox[2] - user_bbox[0]
    login_width = login_bbox[2] - login_bbox[0]

    baseline_y = int(USER_LOGIN_TOP_Y * image_height) + max(
        user_bbox[3] - user_bbox[1],
        login_bbox[3] - login_bbox[1],
    )
    username_x = int(USER_LOGIN_LEFT_MARGIN * image_width) + 60
    login_x = username_x + user_width + USER_LOGIN_GAP

    draw.text((username_x, baseline_y), username, font=font_username, fill=TEXT_FILL, anchor="ls")
    draw.text((login_x, baseline_y), login, font=font_login, fill=TEXT_FILL, anchor="ls")


def make_qr_image(link: str, size: int) -> Image.Image:
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(link)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return qr_img.resize((size, size), Image.Resampling.LANCZOS)


def build_failed_image(
    username: str,
    login: str,
    reason: str,
    link: str,
    final_equity: str,
) -> bytes:
    image = Image.open(FAILED_IMAGE).convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    body_font = load_font(BODY_FONT_SIZE)
    date_font = load_font(DATE_FONT_SIZE)

    today_text = date.today().strftime("%Y-%m-%d")
    qr_size = int(width * QR_SIZE_RATIO)
    right_edge = int((1 - RIGHT_MARGIN) * width)
    qr_x = right_edge - qr_size
    qr_y = int(QR_TOP_Y * height)

    date_bbox = draw.textbbox((0, 0), today_text, font=date_font)
    date_width = date_bbox[2] - date_bbox[0]
    date_x = right_edge - date_width
    date_y = int(DATE_TOP_Y * height)
    draw.text((date_x - 140, date_y - date_bbox[1] + 100), today_text, font=date_font, fill=DATE_FILL)

    qr_img = make_qr_image(link, qr_size)
    image.paste(qr_img, (qr_x - 105, qr_y + 90))

    draw = ImageDraw.Draw(image)
    draw_username_login(draw, username, login, width, height)
    draw_box_text_left(draw, final_equity, FINAL_EQUITY_BOX, body_font)
    draw_box_text_left(draw, reason, REASON_BOX, body_font, fill=REASON_FILL)

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    buffer.seek(0)
    return buffer.getvalue()


def _telegram_config() -> tuple[str, list[int]]:
    bot_token = os.environ.get("BOT_TOKEN")
    if not bot_token:
        raise ValueError("BOT_TOKEN is not set")
    chat_id = os.environ.get("CHAT_ID_1")
    if not chat_id:
        raise ValueError("CHAT_ID_1 is not set")
    return bot_token, [int(chat_id)]


async def send_image_to_chats(image_bytes: bytes) -> None:
    bot_token, chat_ids = _telegram_config()
    bot = Bot(token=bot_token)
    for chat_id in chat_ids:
        await bot.send_photo(chat_id=chat_id, photo=image_bytes)


def save_failed_report_preview(payload: dict[str, Any]) -> dict[str, str]:
    """
    Build the failed-report image from a JSON-like dict and save it locally.

    Example payload:
        {
            "username": "pips_shark",
            "login": "12387427863",
            "reason": "failed by last week trading",
            "link": "https://google.com",
            "final_equity": "378433"
        }

    Saves to database/failed-<random_id>.jpg and returns metadata.
    """
    missing = [f for f in REQUIRED_FIELDS if not payload.get(f)]
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")

    image_bytes = build_failed_image(
        username=str(payload["username"]),
        login=str(payload["login"]),
        reason=str(payload["reason"]),
        link=str(payload["link"]),
        final_equity=str(payload["final_equity"]),
    )

    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex[:12]
    filename = f"failed-{file_id}.jpg"
    filepath = DATABASE_DIR / filename
    filepath.write_bytes(image_bytes)

    return {
        "id": file_id,
        "filename": filename,
        "path": str(filepath),
    }


def send_failed_report(
    username: str,
    login: str,
    reason: str,
    link: str,
    final_equity: str,
) -> None:
    image_bytes = build_failed_image(username, login, reason, link, final_equity)
    asyncio.run(send_image_to_chats(image_bytes))


REQUIRED_FIELDS = ("username", "login", "reason", "link", "final_equity")


@app.post("/failed-report")
def failed_report():
    data = request.get_json(silent=True) or {}
    missing = [f for f in REQUIRED_FIELDS if not data.get(f)]
    if missing:
        return jsonify({"ok": False, "error": f"Missing fields: {', '.join(missing)}"}), 400

    try:
        send_failed_report(
            username=str(data["username"]),
            login=str(data["login"]),
            reason=str(data["reason"]),
            link=str(data["link"]),
            final_equity=str(data["final_equity"]),
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True})


@app.post("/failed-report/preview")
def failed_report_preview():
    """Generate image and save to database/ without sending to Telegram."""
    data = request.get_json(silent=True) or {}
    try:
        result = save_failed_report_preview(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, **result})


@app.get("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "preview":
        sample = {
            "username": "pips_shark",
            "login": "12387427863",
            "reason": "failed by last week trading",
            "link": "https://google.com",
            "final_equity": "378433",
        }
        result = save_failed_report_preview(sample)
        print(result["path"])
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 4000)))
