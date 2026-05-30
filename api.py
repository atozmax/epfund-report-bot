import asyncio
import os
import sys
import time
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit
from functools import wraps
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
WITHDRAW_IMAGE = ROOT_DIR / "images" / "withdraw.png"
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

FAILED_REQUIRED_FIELDS = ("username", "login", "reason", "final_equity")
PHASE_PASS_REQUIRED_FIELDS = ("username", "login", "initial_balance", "total_profit")
WITHDRAW_REQUIRED_FIELDS = ("username", "login", "withdraw_amount", "total_withdraw")

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
        f"👇 {link}"
    )


def build_phase1_telegram_caption(login: str) -> str:
    link = build_dashboard_link_markdown(login)
    return (
        f"✅ 🎉 *Phase 1 passed* — account successfully advanced to Phase 2!\n\n"
        f"All trading objectives were met.\n\n"
        f"👇 {link}"
    )


def build_phase2_telegram_caption(login: str) -> str:
    link = build_dashboard_link_markdown(login)
    return (
        f"✅ 🏆 *Phase 2 passed* — account is now funded on Phase Real!\n\n"
        f"Congratulations on completing the evaluation program.\n\n"
        f"👇 {link}"
    )


def build_withdraw_telegram_caption(login: str) -> str:
    link = build_dashboard_link_markdown(login)
    return (
        f"💸 *Withdrawal processed* for this account.\n\n"
        f"👇 {link}"
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
    result = fetch_treasury_info()
    print(result)
    image_bytes = build_treasury_report_image(result)

    bot_token, chat_ids = _telegram_config()
    bot = Bot(token=bot_token)
    for chat_id in chat_ids:
        message = await bot.send_photo(chat_id=chat_id, photo=image_bytes)
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message.message_id,
            disable_notification=True,
        )


def run_treasury_report() -> None:
    asyncio.run(send_treasury_report_to_chats())


def start_treasury_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_treasury_report,
        trigger=CronTrigger(minute="0,5,10,15,20,25,30,35,40,45,50,55"),
        id="treasury_report",
        name="Send treasury report",
    )
    scheduler.start()
    print("Scheduler running — treasury reports every 5 minutes")
    return scheduler


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
    for chat_id in chat_ids:
        await telegram_bot.send_photo(
            chat_id=chat_id,
            photo=image_bytes,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
        )


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


def _normalize_pass_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": item.get("username"),
        "login": item.get("login"),
        "initial_balance": item.get("initial_balance") or item.get("initialBalance"),
        "total_profit": item.get("total_profit") or item.get("totalProfit"),
    }


def _normalize_withdraw_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": item.get("username"),
        "login": item.get("login"),
        "withdraw_amount": item.get("withdraw_amount") if item.get("withdraw_amount") is not None else item.get("withdrawAmount"),
        "total_withdraw": item.get("total_withdraw") if item.get("total_withdraw") is not None else item.get("totalWithdraw"),
    }


async def send_failed_reports_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bot_token, chat_ids = _telegram_config()
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
    return asyncio.run(send_failed_reports_batch(items))


async def _send_pass_reports_batch(
    items: list[dict[str, Any]],
    template_path: Path,
    caption_builder: Callable[[str], str],
    filename_prefix: str,
) -> list[dict[str, Any]]:
    bot_token, chat_ids = _telegram_config()
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
    return asyncio.run(send_phase1_reports_batch(items))


def send_phase2_reports(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return asyncio.run(send_phase2_reports_batch(items))


async def send_withdraw_reports_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bot_token, chat_ids = _telegram_config()
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
            login = str(normalized["login"])
            image_bytes = build_pass_image(
                WITHDRAW_IMAGE,
                username=str(normalized["username"]),
                login=login,
                initial_balance=_format_report_value(normalized["withdraw_amount"]),
                total_profit=_format_report_value(normalized["total_withdraw"]),
            )
            caption = build_withdraw_telegram_caption(login)
            url = _upload_report_image(image_bytes, "withdraw")
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


def send_withdraw_reports(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return asyncio.run(send_withdraw_reports_batch(items))


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
    Build the withdraw report image from a JSON-like dict and save it locally.

    Example payload:
        {
            "username": "pips_shark",
            "login": "12387427863",
            "withdraw_amount": 5000,
            "total_withdraw": 15000
        }

    Saves to database/withdraw-<random_id>.jpg and returns metadata.
    """
    normalized = _normalize_withdraw_item(payload)
    missing = [f for f in WITHDRAW_REQUIRED_FIELDS if _field_missing(normalized, f)]
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")

    login = str(normalized["login"])
    image_bytes = build_pass_image(
        WITHDRAW_IMAGE,
        username=str(normalized["username"]),
        login=login,
        initial_balance=_format_report_value(normalized["withdraw_amount"]),
        total_profit=_format_report_value(normalized["total_withdraw"]),
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
            "username": "pips_shark",
            "login": "12387427863",
            "reason": "failed by last week trading",
            "final_equity": "378433",
        }
        result = save_failed_report_preview(sample)
        print(result["path"])

        sample = {
            "username": "pips_shark",
            "login": "12387427863",
            "initial_balance": "100000",
            "total_profit": "12500",
        }
        result = save_phase1_report_preview(sample)
        print(result["path"])

        sample = {
            "username": "pips_shark",
            "login": "12387427863",
            "initial_balance": "100000",
            "total_profit": "12500",
        }
        result = save_phase2_report_preview(sample)
        print(result["path"])

        sample = {
            "username": "pips_shark",
            "login": "12387427863",
            "withdraw_amount": 5000,
            "total_withdraw": 15000,
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
            print(f"Treasury report on startup failed: {exc}")
        try:
            app.run(host="0.0.0.0", port=port, use_reloader=False)
        finally:
            scheduler.shutdown()
