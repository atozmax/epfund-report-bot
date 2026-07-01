import asyncio
import os
import sys
import time
import traceback
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit
from functools import lru_cache, wraps
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Optional, Union

import boto3
import httpx
import qrcode
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from botocore.client import Config
from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException
from PIL import Image, ImageDraw, ImageFont
from telegram import Bot
from telegram.constants import ParseMode

from env_loader import load_app_env

load_app_env()

ROOT_DIR = Path(__file__).resolve().parent
FONTS_DIR = ROOT_DIR / "fonts"
FAILED_IMAGE = ROOT_DIR / "images" / "failed.png"
PHASE1_IMAGE = ROOT_DIR / "images" / "phase1.jpg"
PHASE2_IMAGE = ROOT_DIR / "images" / "phase2.jpg"
WITHDRAW_IMAGE = ROOT_DIR / "images" / "withdraw-template.png"
WITHDRAW_CERT_DESIGN_WIDTH = 2975
WITHDRAW_CERT_DESIGN_HEIGHT = 4210
TREASURY_INPUT_IMAGE = ROOT_DIR / "images" / "input.png"
DATABASE_DIR = ROOT_DIR / "database"

TREASURY_API = "https://api.epfund.org/v1/landing/home/treasury-info"
TREASURY_FETCH_MAX_ATTEMPTS = 3
TREASURY_FETCH_RETRY_DELAY_SECONDS = 2
TREASURY_VALUE_LEFT_MARGIN = 0.15
TREASURY_VALUE_BOXES = [
    (0.40, 0.62),
    (0.54, 0.84),
    (0.68, 1.06),
]

REGULAR_FONT_PATHS = [
    FONTS_DIR / "IRANYekanRegular.ttf",
]
BOLD_FONT_PATHS = [
    FONTS_DIR / "IRANYekanExtraBold.ttf",
    FONTS_DIR / "IRANYekanBold.ttf",
]
TEXT_FILL = "#2d2d2d"
REASON_FILL = "#c62828"
DATE_FILL = "#1e6fd9"
PASS_FILL = "#00821c"

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


def _log_error(context: str, exc: Exception) -> None:
    print(f"{context}: {exc}", file=sys.stderr)
    traceback.print_exc()


def _batch_config_error_results(
    items: list[Any],
    exc: Exception,
) -> list[dict[str, Any]]:
    message = str(exc)
    return [{"index": index, "ok": False, "error": message} for index in range(len(items))]


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception) -> Response:
    if isinstance(exc, HTTPException):
        return exc
    _log_error("Unhandled request error", exc)
    return jsonify({"ok": False, "error": str(exc)}), 500

FAILED_REQUIRED_FIELDS = ("username", "login", "reason", "final_equity")
PHASE_PASS_REQUIRED_FIELDS = ("username", "login", "initial_balance", "total_profit")
WITHDRAW_REQUIRED_FIELDS = (
    "cert_id",
    "date",
    "username",
    "total_amount",
    "profit_share",
    "profit_withdraw",
    "tx_link",
    "analyze_link",
)
WITHDRAW_CERT_FONT_PATHS = {
    "be_vietnam": [FONTS_DIR / "Be_Vietnam_Pro" / "BeVietnamPro-Light.ttf"],
    "arizonia": [FONTS_DIR / "Arizonia" / "Arizonia-Regular.ttf"],
    "baskervville": [FONTS_DIR / "Baskervville_SC" / "static" / "BaskervvilleSC-Regular.ttf"],
}
WITHDRAW_CERT_META_FONT_SIZE = 50
WITHDRAW_CERT_META_REFERENCE_SIZE = (1809, 2560)
WITHDRAW_CERT_META_CERT_ORIGIN = (141, 153)
WITHDRAW_CERT_META_DATE_ORIGIN = (1609, 154)
WITHDRAW_CERT_USERNAME_FONT_SIZE = 240
WITHDRAW_CERT_USERNAME_CENTER_REF = (897, 678)
WITHDRAW_CERT_USERNAME_MAX_WIDTH = 2051
WITHDRAW_CERT_USERNAME_MAX_HEIGHT = 452
WITHDRAW_CERT_TOTAL_AMOUNT_CENTER_REF = (899.5, 981.5)
WITHDRAW_CERT_PROFIT_WITHDRAW_CENTER_REF = (416.5, 952.5)
WITHDRAW_CERT_PROFIT_SHARE_CENTER_REF = (1352.5, 953)
WITHDRAW_CERT_TOTAL_AMOUNT_FONT_SIZE = 240
WITHDRAW_CERT_PROFIT_VALUE_FONT_SIZE = 120
WITHDRAW_CERT_META_FILL = (142, 142, 142, int(255 * 0.80))
WITHDRAW_CERT_USERNAME_FILL = "#040403"
WITHDRAW_CERT_AMOUNT_FILL = "#040403"
WITHDRAW_CERT_META_LINE_GAP = 12

TRADER_CAPTION_URL = "https://epfund.org/en/wallet?tab=traders&trader={login}&status=inactive"


def build_trader_link(login: str) -> str:
    return TRADER_CAPTION_URL.format(login=login)


def _escape_telegram_markdown(text: str) -> str:
    for char in ("_", "*", "`", "["):
        text = text.replace(char, f"\\{char}")
    return text


def build_dashboard_link_markdown(login: str) -> str:
    return f"[Analyze Dashboard]({build_trader_link(login)})"


def build_failed_telegram_caption(login: str, reason: str) -> str:
    link = build_dashboard_link_markdown(login)
    safe_reason = _escape_telegram_markdown(reason)
    return (
        f"❌ 📉 Unfortunately, this account did not pass evaluation.\n\n"
        f"Breached due to: *{safe_reason}*\n\n"
        f"👉 {link}"
    )


def build_phase1_telegram_caption(login: str) -> str:
    link = build_dashboard_link_markdown(login)
    return (
        f"✅ 🎉 *Phase 1 passed* — account successfully advanced to Phase 2!\n\n"
        f"All trading objectives were met.\n\n"
        f"👉 {link}"
    )


def build_phase2_telegram_caption(login: str) -> str:
    link = build_dashboard_link_markdown(login)
    return (
        f"✅ 🏆 *Phase 2 passed* — account is now funded on Phase Real!\n\n"
        f"Congratulations on completing the evaluation program.\n\n"
        f"👉 {link}"
    )


def build_withdraw_telegram_caption(username: str, analyze_link: str) -> str:
    safe_username = _escape_telegram_markdown(username)
    return (
        f"✅ 🎉 Trader Payout Approved\n\n"
        f"This trader's profit withdrawal has been successfully approved.\n\n"
        f"Certificate issued for *{safe_username}*.\n\n"
        f"This certificate officially confirms the payout issued by EPFund and reflects "
        f"our commitment to transparency, trust, and a verifiable financial structure "
        f"within decentralized trading.\n\n"
        f"Review time by AI: 1 minute\n\n"
        f"👉 [Analyze Dashboard]({analyze_link})\n\n"
        f"EPFund\n"
        f"The First Decentralized Prop Firm"
    )


def require_api_token(view: Callable[..., Response]) -> Callable[..., Response]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Response:
        expected = os.environ.get("API_TOKEN")
        if not expected:
            return jsonify({"ok": False, "error": "API_TOKEN is not configured"}), 500

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:].strip()
        else:
            provided = request.headers.get("X-API-Token", "").strip()

        if not provided or provided != expected:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        return view(*args, **kwargs)

    return wrapped


def _load_font(
    size: int,
    candidates: list[Path],
) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    for path in candidates:
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def load_regular_font(size: int) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    return _load_font(size, REGULAR_FONT_PATHS)


def load_bold_font(size: int) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    return _load_font(size, BOLD_FONT_PATHS)


def load_withdraw_cert_font(
    family: str,
    size: int,
) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    return _load_font(size, WITHDRAW_CERT_FONT_PATHS[family])


def format_datetime_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %I:%M %p")


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

    offset = 20 if fill == PASS_FILL else 0

    bbox = draw.textbbox((0, 0), text, font=font)
    text_height = bbox[3] - bbox[1]
    y = y0 + (y1 - y0 - text_height) / 2 - bbox[1]
    draw.text((x, y + offset), text, font=font, fill=fill)


def draw_username_login(
    draw: ImageDraw.ImageDraw,
    username: str,
    login: str,
    image_width: int,
    image_height: int,
) -> None:
    font_username = load_bold_font(USERNAME_FONT_SIZE)
    font_login = load_regular_font(LOGIN_FONT_SIZE)

    user_bbox = draw.textbbox((0, 0), username, font=font_username)
    login_bbox = draw.textbbox((0, 0), login, font=font_login)
    user_width = user_bbox[2] - user_bbox[0]
    login_width = login_bbox[2] - login_bbox[0]

    baseline_y = int(USER_LOGIN_TOP_Y * image_height) + max(
        user_bbox[3] - user_bbox[1],
        login_bbox[3] - login_bbox[1],
    )
    username_x = int(USER_LOGIN_LEFT_MARGIN * image_width) + 64
    login_x = username_x + user_width + USER_LOGIN_GAP

    draw.text((username_x, baseline_y), username, font=font_username, fill=TEXT_FILL, anchor="ls")
    draw.text((login_x, baseline_y), login, font=font_login, fill=TEXT_FILL, anchor="ls")


def make_qr_image(link: str, size: int) -> Image.Image:
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(link)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return qr_img.resize((size, size), Image.Resampling.LANCZOS)


def _is_withdraw_cert_qr_card_pixel(red: int, green: int, blue: int) -> bool:
    return blue > 235 and green > 225 and 200 <= red <= 250


def _withdraw_cert_card_visual_bounds(
    pixels: Any,
    min_x: int,
    min_y: int,
    max_x: int,
    max_y: int,
) -> tuple[int, int, int, int]:
    left_edges: list[int] = []
    right_edges: list[int] = []
    scan_top = min_y + int((max_y - min_y) * 0.08)
    scan_bottom = max_y - int((max_y - min_y) * 0.28)

    for y in range(scan_top, scan_bottom, 5):
        xs = [
            x
            for x in range(min_x, max_x + 1)
            if _is_withdraw_cert_qr_card_pixel(*pixels[x, y][:3])
        ]
        if len(xs) < max(50, (max_x - min_x) // 3):
            continue

        runs: list[tuple[int, int]] = []
        start = xs[0]
        prev = xs[0]
        for x in xs[1:]:
            if x > prev + 1:
                runs.append((start, prev))
                start = x
            prev = x
        runs.append((start, prev))
        if not runs:
            continue

        left, right = max(runs, key=lambda run: run[1] - run[0])
        if right - left < (max_x - min_x) * 0.4:
            continue
        left_edges.append(left)
        right_edges.append(right)

    if left_edges and right_edges:
        left_edges.sort()
        right_edges.sort()
        median_left = left_edges[len(left_edges) // 2]
        median_right = right_edges[len(right_edges) // 2]
        return median_left, min_y, median_right, max_y

    return min_x, min_y, max_x, max_y


def _find_withdraw_cert_qr_slots(image: Image.Image) -> list[tuple[int, int, int]]:
    """Detect the two footer placeholder cards and return center/size for each QR."""
    width, height = image.size
    pixels = image.convert("RGBA").load()
    y_start = int(height * 0.78)
    y_end = int(height * 0.93)
    visited: set[tuple[int, int]] = set()
    components: list[tuple[int, int, int, int]] = []

    for y in range(y_start, y_end):
        for x in range(width):
            if (x, y) in visited:
                continue
            red, green, blue, _alpha = pixels[x, y]
            if not _is_withdraw_cert_qr_card_pixel(red, green, blue):
                continue

            stack = [(x, y)]
            min_x = max_x = x
            min_y = max_y = y
            visited.add((x, y))

            while stack:
                cx, cy = stack.pop()
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if nx < 0 or ny < y_start or nx >= width or ny >= y_end:
                        continue
                    if (nx, ny) in visited:
                        continue
                    pr, pg, pb, _ = pixels[nx, ny]
                    if _is_withdraw_cert_qr_card_pixel(pr, pg, pb):
                        visited.add((nx, ny))
                        stack.append((nx, ny))

            box_w = max_x - min_x + 1
            box_h = max_y - min_y + 1
            if box_w > 250 and box_h > 250 and 0.65 <= box_w / box_h <= 1.35:
                components.append((min_x, min_y, max_x, max_y))

    components.sort(key=lambda box: box[0])
    deduped: list[tuple[int, int, int, int]] = []
    for box in components:
        if deduped and box[0] - deduped[-1][0] < 80:
            prev = deduped[-1]
            prev_area = (prev[2] - prev[0]) * (prev[3] - prev[1])
            box_area = (box[2] - box[0]) * (box[3] - box[1])
            if box_area > prev_area:
                deduped[-1] = box
            continue
        deduped.append(box)

    if len(deduped) < 2:
        raise ValueError("Could not detect withdraw certificate QR placeholder boxes")

    slots: list[tuple[int, int, int]] = []
    for min_x, min_y, max_x, max_y in deduped[:2]:
        vis_min_x, vis_min_y, vis_max_x, vis_max_y = _withdraw_cert_card_visual_bounds(
            pixels,
            min_x,
            min_y,
            max_x,
            max_y,
        )
        box_w = vis_max_x - vis_min_x + 1
        box_h = vis_max_y - vis_min_y + 1
        center_x = (vis_min_x + vis_max_x) // 2
        label_height = int(box_h * 0.24)
        qr_top = vis_min_y + int(box_h * 0.08)
        qr_bottom = vis_max_y - label_height
        center_y = (qr_top + qr_bottom) // 2
        qr_size = int(min(box_w * 0.78, (qr_bottom - qr_top) * 0.92))
        slots.append((center_x, center_y, max(1, qr_size)))

    return slots


@lru_cache(maxsize=4)
def _withdraw_cert_qr_slots(
    template_path: str,
    width: int,
    height: int,
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    with Image.open(template_path) as template:
        slots = _find_withdraw_cert_qr_slots(template)
    return (slots[0], slots[1])


def _build_trader_report_image(
    template_path: Path,
    username: str,
    login: str,
    top_value: str,
    bottom_value: str,
    *,
    bottom_fill: str = REASON_FILL,
) -> bytes:
    image = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    bold_font = load_bold_font(BODY_FONT_SIZE)

    qr_size = int(width * QR_SIZE_RATIO)
    right_edge = int((1 - RIGHT_MARGIN) * width)
    qr_x = right_edge - qr_size
    qr_y = int(QR_TOP_Y * height)

    qr_img = make_qr_image(build_trader_link(login), qr_size)
    image.paste(qr_img, (qr_x - 105, qr_y + 90))

    draw = ImageDraw.Draw(image)
    draw_username_login(draw, username, login, width, height)
    draw_box_text_left(draw, top_value, FINAL_EQUITY_BOX, bold_font)
    draw_box_text_left(draw, bottom_value, REASON_BOX, bold_font, fill=bottom_fill)

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    buffer.seek(0)
    return buffer.getvalue()


def build_failed_image(
    username: str,
    login: str,
    reason: str,
    final_equity: str,
) -> bytes:
    return _build_trader_report_image(
        FAILED_IMAGE,
        username,
        login,
        final_equity,
        reason,
    )


def build_pass_image(
    template_path: Path,
    username: str,
    login: str,
    initial_balance: str,
    total_profit: str,
) -> bytes:
    return _build_trader_report_image(
        template_path,
        username,
        login,
        initial_balance,
        total_profit,
        bottom_fill=PASS_FILL,
    )


def _withdraw_cert_design_font_size(design_px: float, image_height: int) -> int:
    return max(1, int(round(design_px * image_height / WITHDRAW_CERT_DESIGN_HEIGHT)))


def _withdraw_cert_scale_x(design_value: float, image_width: int) -> float:
    return design_value * image_width / WITHDRAW_CERT_DESIGN_WIDTH


def _withdraw_cert_scale_y(design_value: float, image_height: int) -> float:
    return design_value * image_height / WITHDRAW_CERT_DESIGN_HEIGHT


def _withdraw_cert_text_bbox(
    text: str,
    font: Union[ImageFont.FreeTypeFont, ImageFont.ImageFont],
) -> tuple[int, int, int, int]:
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    return probe.textbbox((0, 0), text, font=font)


def _fit_withdraw_cert_username_font(
    username: str,
    image_width: int,
    image_height: int,
    box_width: float,
    box_height: float,
) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    size = _withdraw_cert_design_font_size(WITHDRAW_CERT_USERNAME_FONT_SIZE, image_height)
    min_size = _withdraw_cert_design_font_size(60, image_height)
    while size >= min_size:
        font = load_withdraw_cert_font("arizonia", size)
        bbox = _withdraw_cert_text_bbox(username, font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        if text_width <= box_width and text_height <= box_height:
            return font
        size -= 1
    return load_withdraw_cert_font("arizonia", min_size)


def _withdraw_cert_reference_position(
    ref_x: float,
    ref_y: float,
    image_width: int,
    image_height: int,
) -> tuple[float, float]:
    return (
        _withdraw_cert_meta_scale_x(ref_x, image_width),
        _withdraw_cert_meta_scale_y(ref_y, image_height),
    )


def _withdraw_cert_total_font_size(image_height: int) -> int:
    return max(1, int(round(WITHDRAW_CERT_TOTAL_AMOUNT_FONT_SIZE * image_height / WITHDRAW_CERT_DESIGN_HEIGHT)))


def _withdraw_cert_profit_font_size(image_height: int) -> int:
    return max(1, int(round(WITHDRAW_CERT_PROFIT_VALUE_FONT_SIZE * image_height / WITHDRAW_CERT_DESIGN_HEIGHT)))


def _draw_withdraw_cert_username(
    draw: ImageDraw.ImageDraw,
    username: str,
    image_width: int,
    image_height: int,
) -> None:
    center_x, center_y = _withdraw_cert_reference_position(
        WITHDRAW_CERT_USERNAME_CENTER_REF[0],
        WITHDRAW_CERT_USERNAME_CENTER_REF[1],
        image_width,
        image_height,
    )
    box_width = image_width * WITHDRAW_CERT_USERNAME_MAX_WIDTH / WITHDRAW_CERT_DESIGN_WIDTH
    box_height = image_height * WITHDRAW_CERT_USERNAME_MAX_HEIGHT / WITHDRAW_CERT_DESIGN_HEIGHT
    font = _fit_withdraw_cert_username_font(username, image_width, image_height, box_width, box_height)
    draw.text(
        (center_x, center_y),
        username,
        font=font,
        fill=WITHDRAW_CERT_USERNAME_FILL,
        anchor="mm",
    )


def _draw_withdraw_cert_amounts(
    draw: ImageDraw.ImageDraw,
    *,
    total_amount: str,
    profit_share: str,
    profit_withdraw: str,
    image_width: int,
    image_height: int,
) -> None:
    total_font = load_withdraw_cert_font("baskervville", _withdraw_cert_total_font_size(image_height))
    profit_font = load_withdraw_cert_font("baskervville", _withdraw_cert_profit_font_size(image_height))

    placements = (
        (WITHDRAW_CERT_TOTAL_AMOUNT_CENTER_REF, _format_withdraw_cert_currency(total_amount), total_font),
        (WITHDRAW_CERT_PROFIT_WITHDRAW_CENTER_REF, _format_withdraw_cert_currency(profit_withdraw), profit_font),
        (WITHDRAW_CERT_PROFIT_SHARE_CENTER_REF, _format_withdraw_cert_profit_share(profit_share), profit_font),
    )
    for (ref_x, ref_y), text, font in placements:
        x, y = _withdraw_cert_reference_position(ref_x, ref_y, image_width, image_height)
        draw.text((x, y), text, font=font, fill=WITHDRAW_CERT_AMOUNT_FILL, anchor="mm")


def _withdraw_cert_meta_scale_x(design_value: float, image_width: int) -> float:
    ref_width, _ref_height = WITHDRAW_CERT_META_REFERENCE_SIZE
    return design_value * image_width / ref_width


def _withdraw_cert_meta_scale_y(design_value: float, image_height: int) -> float:
    _ref_width, ref_height = WITHDRAW_CERT_META_REFERENCE_SIZE
    return design_value * image_height / ref_height


def _withdraw_cert_meta_font_size(image_height: int) -> int:
    return max(1, int(round(WITHDRAW_CERT_META_FONT_SIZE * image_height / WITHDRAW_CERT_DESIGN_HEIGHT)))


def _withdraw_cert_meta_positions(
    image_width: int,
    image_height: int,
) -> tuple[float, float, float, float]:
    cert_x = _withdraw_cert_meta_scale_x(WITHDRAW_CERT_META_CERT_ORIGIN[0], image_width)
    cert_y = _withdraw_cert_meta_scale_y(WITHDRAW_CERT_META_CERT_ORIGIN[1], image_height)
    date_x = _withdraw_cert_meta_scale_x(WITHDRAW_CERT_META_DATE_ORIGIN[0], image_width)
    date_y = _withdraw_cert_meta_scale_y(WITHDRAW_CERT_META_DATE_ORIGIN[1], image_height)
    return cert_x, cert_y, date_x, date_y


def _withdraw_cert_ordinal(day: int) -> str:
    if 11 <= (day % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def format_withdraw_cert_date(value: str) -> str:
    raw = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
            month = parsed.strftime("%b")
            return f"{_withdraw_cert_ordinal(parsed.day)},{month} {parsed.year}"
        except ValueError:
            continue
    return raw


def _draw_withdraw_cert_meta_block(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    label: str,
    value: str,
    font: Union[ImageFont.FreeTypeFont, ImageFont.ImageFont],
    *,
    anchor: str,
    fill: tuple[int, int, int, int] = WITHDRAW_CERT_META_FILL,
) -> None:
    draw.text((x, y), label, font=font, fill=fill, anchor=anchor)
    label_bbox = draw.textbbox((x, y), label, font=font, anchor=anchor)
    gap = _withdraw_cert_meta_scale_y(WITHDRAW_CERT_META_LINE_GAP, draw.im.size[1])
    value_y = label_bbox[3] + gap
    draw.text((x, value_y), value, font=font, fill=fill, anchor=anchor)


def _withdraw_cert_point(
    width: int,
    height: int,
    *,
    left: Optional[float] = None,
    right: Optional[float] = None,
    top: Optional[float] = None,
    bottom: Optional[float] = None,
) -> tuple[float, float]:
    x_scale = width / WITHDRAW_CERT_DESIGN_WIDTH
    y_scale = height / WITHDRAW_CERT_DESIGN_HEIGHT
    if right is not None:
        x = width - right * x_scale
    elif left is not None:
        x = left * x_scale
    else:
        x = 0.0
    if bottom is not None:
        y = height - bottom * y_scale
    elif top is not None:
        y = top * y_scale
    else:
        y = 0.0
    return x, y


def build_withdraw_cert_image(
    *,
    cert_id: str,
    date: str,
    username: str,
    total_amount: str,
    profit_share: str,
    profit_withdraw: str,
    tx_link: str,
    analyze_link: str,
) -> bytes:
    image = Image.open(WITHDRAW_IMAGE).convert("RGBA")
    draw = ImageDraw.Draw(image)
    width, height = image.size

    meta_font = load_withdraw_cert_font("be_vietnam", _withdraw_cert_meta_font_size(height))

    cert_x, cert_y, date_x, date_y = _withdraw_cert_meta_positions(width, height)

    tx_slot, analyze_slot = _withdraw_cert_qr_slots(str(WITHDRAW_IMAGE), width, height)

    _draw_withdraw_cert_meta_block(
        draw,
        cert_x,
        cert_y,
        "Certificate ID:",
        cert_id,
        meta_font,
        anchor="lt",
    )
    _draw_withdraw_cert_meta_block(
        draw,
        date_x,
        date_y,
        "Date:",
        format_withdraw_cert_date(date),
        meta_font,
        anchor="rt",
    )
    _draw_withdraw_cert_username(draw, username, width, height)
    _draw_withdraw_cert_amounts(
        draw,
        total_amount=total_amount,
        profit_share=profit_share,
        profit_withdraw=profit_withdraw,
        image_width=width,
        image_height=height,
    )

    tx_cx, tx_cy, tx_qr_size = tx_slot
    analyze_cx, analyze_cy, analyze_qr_size = analyze_slot
    tx_qr = make_qr_image(tx_link, tx_qr_size)
    analyze_qr = make_qr_image(analyze_link, analyze_qr_size)
    image.paste(tx_qr, (int(tx_cx - tx_qr_size / 2), int(tx_cy - tx_qr_size / 2)))
    image.paste(
        analyze_qr,
        (int(analyze_cx - analyze_qr_size / 2), int(analyze_cy - analyze_qr_size / 2)),
    )

    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=95)
    buffer.seek(0)
    return buffer.getvalue()


def fetch_treasury_info() -> dict:
    last_error: Optional[Exception] = None
    for attempt in range(1, TREASURY_FETCH_MAX_ATTEMPTS + 1):
        try:
            response = httpx.get(TREASURY_API, timeout=30.0)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("success"):
                raise RuntimeError("Treasury API returned success=false")
            return payload["result"]
        except Exception as exc:
            last_error = exc
            if attempt < TREASURY_FETCH_MAX_ATTEMPTS:
                print(
                    f"Treasury fetch attempt {attempt}/{TREASURY_FETCH_MAX_ATTEMPTS} "
                    f"failed: {exc}; retrying in {TREASURY_FETCH_RETRY_DELAY_SECONDS}s..."
                )
                time.sleep(TREASURY_FETCH_RETRY_DELAY_SECONDS)
    assert last_error is not None
    raise last_error


def parse_treasury_amount(value: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"Invalid numeric value: {value!r}") from exc


def format_treasury_amount(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"))
    text = f"{quantized:,.2f}"
    if text.endswith(".00"):
        return text[:-3]
    return text


def format_treasury_profit_ratio(reserve: Decimal, at_risk: Decimal) -> str:
    if reserve == 0:
        return "N/A"
    ratio = at_risk / reserve
    return f"{ratio:,.4f}"


def draw_treasury_box_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: tuple[float, float],
    font: Union[ImageFont.FreeTypeFont, ImageFont.ImageFont],
    fill: str = "#2d2d2d",
) -> None:
    width, height = draw.im.size
    x = int(TREASURY_VALUE_LEFT_MARGIN * width)
    y0 = int(box[0] * height)
    y1 = int(box[1] * height)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_height = bbox[3] - bbox[1]
    y = y0 + (y1 - y0 - text_height) / 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=fill)


def build_treasury_report_image(result: dict) -> bytes:
    reserve = parse_treasury_amount(result["insuranceReserveCapital"])
    at_risk = parse_treasury_amount(result["atRisk"])
    profit_ratio = format_treasury_profit_ratio(reserve, at_risk)

    values = [
        f"${format_treasury_amount(reserve)}",
        f"${format_treasury_amount(at_risk)}",
        profit_ratio,
    ]

    image = Image.open(TREASURY_INPUT_IMAGE).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = load_bold_font(96)

    for value, box in zip(values, TREASURY_VALUE_BOXES):
        draw_treasury_box_text(draw, value, box, font)

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    buffer.seek(0)
    return buffer.getvalue()


async def send_treasury_report_to_chats() -> None:
    try:
        result = fetch_treasury_info()
    except Exception as exc:
        _log_error("Treasury report skipped — fetch failed after retries", exc)
        return

    try:
        print(result)
        image_bytes = build_treasury_report_image(result)
    except Exception as exc:
        _log_error("Treasury report skipped — image build failed", exc)
        return

    try:
        bot_token, chat_ids = _telegram_config()
    except Exception as exc:
        _log_error("Treasury report skipped — Telegram config error", exc)
        return

    bot = Bot(token=bot_token)
    for chat_id in chat_ids:
        try:
            reserve = parse_treasury_amount(result["insuranceReserveCapital"])
            at_risk = parse_treasury_amount(result["atRisk"])
            profit_ratio = format_treasury_profit_ratio(reserve, at_risk)

            caption = f'''
*🛡 Insurance Reserve Capital*  :  *${reserve:,.2f}*

نمایانگر سرمایه ذخیره‌ای است که برای پشتیبانی از ساختار مالی و مدیریت تعهدات شرکت در نظر گرفته شده است.

*⚠️  At Risk* : *${at_risk:,.2f}*

پارامتر  *At Risk* نشان‌دهنده میانگین میزان برداشت مورد انتظار از تمامی اکانت‌های فعال در پراپ‌فرم است.

هر اکانت، بسته به مرحله‌ای که در آن قرار دارد، احتمال موفقیت در برداشت و امید ریاضی سودآوری، دارای یک ارزش مورد انتظار مشخص است. پارامتر *At Risk* با تجمیع این مقادیر، میزان کل تعهدات بالقوه شرکت در برابر اکانت‌های فعال را نمایش می‌دهد.

به بیان ساده، این شاخص نشان می‌دهد در صورت ادامه فعالیت اکانت‌های فعلی، چه میزان سرمایه در معرض برداشت قرار دارد.

📉  *Risk Ratio* : *{profit_ratio}*

نسبت ریسک به پشتوانه مالی شرکت را مشخص می‌کند و دید روشن‌تری از سلامت ساختار مالی ارائه می‌دهد.
'''
            message = await bot.send_photo(
                chat_id=chat_id, 
                photo=image_bytes,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                )
            # await bot.pin_chat_message(
            #     chat_id=chat_id,
            #     message_id=message.message_id,
            #     disable_notification=True,
            # )
        except Exception as exc:
            _log_error(f"Treasury report failed for chat {chat_id}", exc)


def run_treasury_report() -> None:
    try:
        asyncio.run(send_treasury_report_to_chats())
    except Exception as exc:
        _log_error("Treasury report job failed", exc)


def start_treasury_scheduler() -> Optional[BackgroundScheduler]:
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            run_treasury_report,
            trigger=CronTrigger(minute="0,30"),
            id="treasury_report",
            name="Send treasury report",
        )
        scheduler.start()
        print("Scheduler running — treasury reports every 5 minutes")
        return scheduler
    except Exception as exc:
        _log_error("Treasury scheduler failed to start", exc)
        return None


def _telegram_config() -> tuple[str, list[int]]:
    bot_token = os.environ.get("BOT_TOKEN")
    if not bot_token:
        raise ValueError("BOT_TOKEN is not set")
    chat_id = os.environ.get("CHAT_ID_1")
    if not chat_id:
        raise ValueError("CHAT_ID_1 is not set")
    return bot_token, [int(chat_id)]


async def send_image_to_chats(
    image_bytes: bytes,
    caption: str,
    bot: Optional[Bot] = None,
) -> None:
    bot_token, chat_ids = _telegram_config()
    telegram_bot = bot or Bot(token=bot_token)
    errors: list[str] = []
    for chat_id in chat_ids:
        try:
            message = await telegram_bot.send_photo(
                chat_id=chat_id,
                photo=image_bytes,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
            await telegram_bot.pin_chat_message(
                chat_id=chat_id,
                message_id=message.message_id,
                disable_notification=True,
            )
        except Exception as exc:
            _log_error(f"Failed to send image to chat {chat_id}", exc)
            errors.append(f"chat {chat_id}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))


def _field_missing(item: dict[str, Any], field: str) -> bool:
    value = item.get(field)
    return value is None or value == ""


def _validate_report_item(
    item: Any,
    index: int,
    required_fields: tuple[str, ...],
) -> Optional[str]:
    if not isinstance(item, dict):
        return f"Item {index} must be an object"
    missing = [f for f in required_fields if _field_missing(item, f)]
    if missing:
        return f"Item {index} missing fields: {', '.join(missing)}"
    return None


def _format_report_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


def _format_withdraw_cert_currency(value: Any) -> str:
    raw = _format_report_value(value).strip()
    if raw.startswith("$"):
        return raw if raw.startswith("$ ") else f"$ {raw[1:].strip()}"
    try:
        number = float(raw.replace(",", ""))
        if number.is_integer():
            formatted = f"{int(number):,}"
        else:
            formatted = f"{number:,.2f}"
    except (ValueError, TypeError):
        formatted = raw
    return f"$ {formatted}"


def _format_withdraw_cert_profit_share(value: Any) -> str:
    raw = _format_report_value(value).strip()
    if raw.startswith("%"):
        return raw if raw.startswith("% ") else f"% {raw[1:].strip()}"
    if raw.endswith("%"):
        return f"% {raw[:-1].strip()}"
    return f"% {raw}"


def _normalize_pass_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": item.get("username"),
        "login": item.get("login"),
        "initial_balance": item.get("initial_balance") or item.get("initialBalance"),
        "total_profit": item.get("total_profit") or item.get("totalProfit"),
    }


def _normalize_withdraw_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "cert_id": item.get("cert_id") if item.get("cert_id") is not None else item.get("certId"),
        "date": item.get("date"),
        "username": item.get("username"),
        "total_amount": item.get("total_amount") if item.get("total_amount") is not None else item.get("totalAmount"),
        "profit_share": item.get("profit_share") if item.get("profit_share") is not None else item.get("profitShare"),
        "profit_withdraw": item.get("profit_withdraw") if item.get("profit_withdraw") is not None else item.get("profitWithdraw"),
        "tx_link": item.get("tx_link") if item.get("tx_link") is not None else item.get("txLink"),
        "analyze_link": item.get("analyze_link") if item.get("analyze_link") is not None else item.get("analyzeLink"),
    }


async def send_failed_reports_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        bot_token, chat_ids = _telegram_config()
    except Exception as exc:
        _log_error("Failed report batch skipped — Telegram config error", exc)
        return _batch_config_error_results(items, exc)

    bot = Bot(token=bot_token)
    results: list[dict[str, Any]] = []

    for index, item in enumerate(items):
        error = _validate_report_item(item, index, FAILED_REQUIRED_FIELDS)
        if error:
            results.append({"index": index, "ok": False, "error": error})
            continue

        try:
            login = str(item["login"])
            image_bytes = build_failed_image(
                username=str(item["username"]),
                login=login,
                reason=str(item["reason"]),
                final_equity=str(item["final_equity"]),
            )
            caption = build_failed_telegram_caption(login, str(item["reason"]))
            url = _upload_report_image(image_bytes, "failed")
            await send_image_to_chats(image_bytes, caption=caption, bot=bot)
            results.append(
                {
                    "index": index,
                    "ok": True,
                    "username": str(item["username"]),
                    "login": login,
                    "caption": caption,
                    "url": url,
                }
            )
        except Exception as exc:
            results.append({"index": index, "ok": False, "error": str(exc)})

    return results


def send_failed_reports(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return asyncio.run(send_failed_reports_batch(items))
    except Exception as exc:
        _log_error("send_failed_reports failed", exc)
        return _batch_config_error_results(items, exc)


async def _send_pass_reports_batch(
    items: list[dict[str, Any]],
    template_path: Path,
    caption_builder: Callable[[str], str],
    filename_prefix: str,
) -> list[dict[str, Any]]:
    try:
        bot_token, chat_ids = _telegram_config()
    except Exception as exc:
        _log_error("Pass report batch skipped — Telegram config error", exc)
        return _batch_config_error_results(items, exc)

    bot = Bot(token=bot_token)
    results: list[dict[str, Any]] = []

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            results.append({"index": index, "ok": False, "error": f"Item {index} must be an object"})
            continue
        normalized = _normalize_pass_item(item)
        error = _validate_report_item(normalized, index, PHASE_PASS_REQUIRED_FIELDS)
        if error:
            results.append({"index": index, "ok": False, "error": error})
            continue

        try:
            login = str(normalized["login"])
            image_bytes = build_pass_image(
                template_path,
                username=str(normalized["username"]),
                login=login,
                initial_balance=str(normalized["initial_balance"]),
                total_profit=str(normalized["total_profit"]),
            )
            caption = caption_builder(login)
            url = _upload_report_image(image_bytes, filename_prefix)
            await send_image_to_chats(image_bytes, caption=caption, bot=bot)
            results.append(
                {
                    "index": index,
                    "ok": True,
                    "username": str(normalized["username"]),
                    "login": login,
                    "caption": caption,
                    "url": url,
                }
            )
        except Exception as exc:
            results.append({"index": index, "ok": False, "error": str(exc)})

    return results


async def send_phase1_reports_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return await _send_pass_reports_batch(
        items, PHASE1_IMAGE, build_phase1_telegram_caption, "phase1"
    )


async def send_phase2_reports_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return await _send_pass_reports_batch(
        items, PHASE2_IMAGE, build_phase2_telegram_caption, "phase2"
    )


def send_phase1_reports(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return asyncio.run(send_phase1_reports_batch(items))
    except Exception as exc:
        _log_error("send_phase1_reports failed", exc)
        return _batch_config_error_results(items, exc)


def send_phase2_reports(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return asyncio.run(send_phase2_reports_batch(items))
    except Exception as exc:
        _log_error("send_phase2_reports failed", exc)
        return _batch_config_error_results(items, exc)


async def send_withdraw_reports_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        bot_token, chat_ids = _telegram_config()
    except Exception as exc:
        _log_error("Withdraw report batch skipped — Telegram config error", exc)
        return _batch_config_error_results(items, exc)

    bot = Bot(token=bot_token)
    results: list[dict[str, Any]] = []

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            results.append({"index": index, "ok": False, "error": f"Item {index} must be an object"})
            continue
        normalized = _normalize_withdraw_item(item)
        error = _validate_report_item(normalized, index, WITHDRAW_REQUIRED_FIELDS)
        if error:
            results.append({"index": index, "ok": False, "error": error})
            continue

        try:
            image_bytes = build_withdraw_cert_image(
                cert_id=str(normalized["cert_id"]),
                date=str(normalized["date"]),
                username=str(normalized["username"]),
                total_amount=_format_report_value(normalized["total_amount"]),
                profit_share=_format_report_value(normalized["profit_share"]),
                profit_withdraw=_format_report_value(normalized["profit_withdraw"]),
                tx_link=str(normalized["tx_link"]),
                analyze_link=str(normalized["analyze_link"]),
            )
            caption = build_withdraw_telegram_caption(
                str(normalized["username"]),
                str(normalized["analyze_link"]),
            )
            url = _upload_report_image(image_bytes, "withdraw")
            await send_image_to_chats(image_bytes, caption=caption, bot=bot)
            results.append(
                {
                    "index": index,
                    "ok": True,
                    "username": str(normalized["username"]),
                    "cert_id": str(normalized["cert_id"]),
                    "caption": caption,
                    "url": url,
                }
            )
        except Exception as exc:
            results.append({"index": index, "ok": False, "error": str(exc)})

    return results


def send_withdraw_reports(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return asyncio.run(send_withdraw_reports_batch(items))
    except Exception as exc:
        _log_error("send_withdraw_reports failed", exc)
        return _batch_config_error_results(items, exc)


def _liara_s3_client():
    endpoint = os.environ.get("LIARA_ENDPOINT", "").strip()
    access_key = os.environ.get("LIARA_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("LIARA_SECRET_KEY", "").strip()
    if not endpoint or not access_key or not secret_key:
        raise ValueError(
            "Liara S3 is not configured (LIARA_ENDPOINT, LIARA_ACCESS_KEY, LIARA_SECRET_KEY)"
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


def _public_object_url(bucket: str, key: str) -> str:
    endpoint = os.environ.get("LIARA_ENDPOINT", "").strip().rstrip("/")
    url = f"{endpoint}/{bucket}/{key}"
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def upload_report_image_to_public_bucket(image_bytes: bytes, filename: str) -> str:
    bucket = os.environ.get("LIARA_PUBLIC_BUCKET_NAME", "").strip()
    if not bucket:
        raise ValueError("LIARA_PUBLIC_BUCKET_NAME is not set")

    key = f"reports/{filename}"
    client = _liara_s3_client()
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=image_bytes,
        ContentType="image/jpeg",
    )
    return _public_object_url(bucket, key)


def _upload_report_image(image_bytes: bytes, prefix: str) -> str:
    filename = f"{prefix}-{uuid.uuid4().hex[:12]}.jpg"
    return upload_report_image_to_public_bucket(image_bytes, filename)


def save_failed_report_preview(payload: dict[str, Any]) -> dict[str, str]:
    """
    Build the failed-report image from a JSON-like dict and save it locally.

    Example payload:
        {
            "username": "pips_shark",
            "login": "12387427863",
            "reason": "failed by last week trading",
            "final_equity": "378433"
        }

    Saves to database/failed-<random_id>.jpg and returns metadata.
    """
    missing = [f for f in FAILED_REQUIRED_FIELDS if not payload.get(f)]
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")

    login = str(payload["login"])
    image_bytes = build_failed_image(
        username=str(payload["username"]),
        login=login,
        reason=str(payload["reason"]),
        final_equity=str(payload["final_equity"]),
    )

    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex[:12]
    filename = f"failed-{file_id}.jpg"
    filepath = DATABASE_DIR / filename
    filepath.write_bytes(image_bytes)
    url = upload_report_image_to_public_bucket(image_bytes, filename)

    return {
        "id": file_id,
        "filename": filename,
        "path": str(filepath),
        "url": url,
    }


def save_pass_report_preview(
    payload: dict[str, Any],
    template_path: Path,
    filename_prefix: str,
) -> dict[str, str]:
    normalized = _normalize_pass_item(payload)
    missing = [f for f in PHASE_PASS_REQUIRED_FIELDS if not normalized.get(f)]
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")

    login = str(normalized["login"])
    image_bytes = build_pass_image(
        template_path,
        username=str(normalized["username"]),
        login=login,
        initial_balance=str(normalized["initial_balance"]),
        total_profit=str(normalized["total_profit"]),
    )

    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex[:12]
    filename = f"{filename_prefix}-{file_id}.jpg"
    filepath = DATABASE_DIR / filename
    filepath.write_bytes(image_bytes)
    url = upload_report_image_to_public_bucket(image_bytes, filename)

    return {
        "id": file_id,
        "filename": filename,
        "path": str(filepath),
        "url": url,
    }


def save_phase1_report_preview(payload: dict[str, Any]) -> dict[str, str]:
    """
    Build the phase1 pass-account image from a JSON-like dict and save it locally.

    Example payload:
        {
            "username": "pips_shark",
            "login": "12387427863",
            "initial_balance": "100000",
            "total_profit": "12500"
        }

    Saves to database/phase1-<random_id>.jpg and returns metadata.
    """
    return save_pass_report_preview(payload, PHASE1_IMAGE, "phase1")


def save_phase2_report_preview(payload: dict[str, Any]) -> dict[str, str]:
    """
    Build the phase2 pass-account image from a JSON-like dict and save it locally.

    Saves to database/phase2-<random_id>.jpg and returns metadata.
    """
    return save_pass_report_preview(payload, PHASE2_IMAGE, "phase2")


def save_withdraw_report_preview(payload: dict[str, Any]) -> dict[str, str]:
    """
    Build the withdraw certificate image from a JSON-like dict and save it locally.

    Example payload:
        {
            "cert_id": "EPF-2026-0042",
            "date": "2026-07-02",
            "username": "pips_shark",
            "total_amount": 15000,
            "profit_share": "80%",
            "profit_withdraw": 5000,
            "tx_link": "https://etherscan.io/tx/0xabc...",
            "analyze_link": "https://epfund.org/en/wallet?tab=traders&trader=123"
        }

    Saves to database/withdraw-<random_id>.jpg and returns metadata.
    """
    normalized = _normalize_withdraw_item(payload)
    missing = [f for f in WITHDRAW_REQUIRED_FIELDS if _field_missing(normalized, f)]
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")

    image_bytes = build_withdraw_cert_image(
        cert_id=str(normalized["cert_id"]),
        date=str(normalized["date"]),
        username=str(normalized["username"]),
        total_amount=_format_report_value(normalized["total_amount"]),
        profit_share=_format_report_value(normalized["profit_share"]),
        profit_withdraw=_format_report_value(normalized["profit_withdraw"]),
        tx_link=str(normalized["tx_link"]),
        analyze_link=str(normalized["analyze_link"]),
    )

    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex[:12]
    filename = f"withdraw-{file_id}.jpg"
    filepath = DATABASE_DIR / filename
    filepath.write_bytes(image_bytes)
    url = upload_report_image_to_public_bucket(image_bytes, filename)

    return {
        "id": file_id,
        "filename": filename,
        "path": str(filepath),
        "url": url,
    }


@app.post("/withdraw-report")
@require_api_token
def withdraw_report():
    items = request.get_json(silent=True)
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "Request body must be a JSON array"}), 400
    if not items:
        return jsonify({"ok": False, "error": "Request body must not be empty"}), 400

    try:
        results = send_withdraw_reports(items)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    sent = sum(1 for r in results if r.get("ok"))
    failed = len(results) - sent
    return jsonify(
        {
            "ok": failed == 0,
            "sent": sent,
            "failed": failed,
            "total": len(results),
            "results": results,
        }
    )


@app.post("/withdraw-report/preview")
@require_api_token
def withdraw_report_preview():
    """Generate withdraw image and save to database/ without sending to Telegram."""
    data = request.get_json(silent=True) or {}
    try:
        result = save_withdraw_report_preview(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, **result})


@app.post("/failed-report")
@require_api_token
def failed_report():
    items = request.get_json(silent=True)
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "Request body must be a JSON array"}), 400
    if not items:
        return jsonify({"ok": False, "error": "Request body must not be empty"}), 400

    try:
        results = send_failed_reports(items)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    sent = sum(1 for r in results if r.get("ok"))
    failed = len(results) - sent
    return jsonify(
        {
            "ok": failed == 0,
            "sent": sent,
            "failed": failed,
            "total": len(results),
            "results": results,
        }
    )


@app.post("/failed-report/preview")
@require_api_token
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


@app.post("/phase1-report")
@require_api_token
def phase1_report():
    items = request.get_json(silent=True)
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "Request body must be a JSON array"}), 400
    if not items:
        return jsonify({"ok": False, "error": "Request body must not be empty"}), 400

    try:
        results = send_phase1_reports(items)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    sent = sum(1 for r in results if r.get("ok"))
    failed = len(results) - sent
    return jsonify(
        {
            "ok": failed == 0,
            "sent": sent,
            "failed": failed,
            "total": len(results),
            "results": results,
        }
    )


@app.post("/phase1-report/preview")
@require_api_token
def phase1_report_preview():
    """Generate phase1 image and save to database/ without sending to Telegram."""
    data = request.get_json(silent=True) or {}
    try:
        result = save_phase1_report_preview(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, **result})


@app.post("/phase2-report")
@require_api_token
def phase2_report():
    items = request.get_json(silent=True)
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "Request body must be a JSON array"}), 400
    if not items:
        return jsonify({"ok": False, "error": "Request body must not be empty"}), 400

    try:
        results = send_phase2_reports(items)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    sent = sum(1 for r in results if r.get("ok"))
    failed = len(results) - sent
    return jsonify(
        {
            "ok": failed == 0,
            "sent": sent,
            "failed": failed,
            "total": len(results),
            "results": results,
        }
    )


@app.post("/phase2-report/preview")
@require_api_token
def phase2_report_preview():
    """Generate phase2 image and save to database/ without sending to Telegram."""
    data = request.get_json(silent=True) or {}
    try:
        result = save_phase2_report_preview(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, **result})


@app.get("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "preview":
        sample = {
            "cert_id": "1234567890",
            "date": "2026-07-02",
            "username": "pips_shark",
            "total_amount": 15000,
            "profit_share": "80%",
            "profit_withdraw": 5000,
            "tx_link": "https://etherscan.io/tx/0xabc123",
            "analyze_link": "https://epfund.org/en/wallet?tab=traders&trader=12387427863",
        }
        result = save_withdraw_report_preview(sample)
        print(result["path"])

    else:
        port = int(os.environ.get("PORT", 4000))
        scheduler = start_treasury_scheduler()
        try:
            pass
            # run_treasury_report()
        except Exception as exc:
            _log_error("Treasury report on startup failed", exc)
        try:
            app.run(host="0.0.0.0", port=port, use_reloader=False)
        finally:
            if scheduler is not None:
                scheduler.shutdown()
