from __future__ import annotations
import os
import re
import logging
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta
from typing import Optional
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle
import base64
import tempfile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

handler = WebhookHandler(LINE_CHANNEL_SECRET)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# 予定キーワード
SCHEDULE_KEYWORDS = [
    "会議", "打ち合わせ", "ミーティング", "meeting", "予定", "アポ", "面接",
    "セミナー", "講習", "研修", "食事", "ランチ", "ディナー", "飲み",
    "病院", "歯医者", "診察", "検診", "美容院", "散髪",
    "出張", "旅行", "フライト", "新幹線",
    "締め切り", "〆切", "提出", "納期",
    "誕生日", "記念日", "イベント", "パーティー",
    "から", "まで", "時に", "時から", "日に", "日から",
]


def _parse_date_from_text(text: str) -> Optional[datetime]:
    """テキストから日付を正規表現で抽出する"""
    now = datetime.now()
    year = now.year

    # 曜日マップ
    WEEKDAYS = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}

    # 今日・明日・明後日
    if re.search(r'今日|本日', text):
        return now
    if re.search(r'明日', text):
        return now + timedelta(days=1)
    if re.search(r'明後日', text):
        return now + timedelta(days=2)

    # 来週◯曜日 / 再来週◯曜日
    for wd, wd_num in WEEKDAYS.items():
        m = re.search(rf'(再来週|来週){wd}曜', text)
        if m:
            weeks = 2 if m.group(1) == "再来週" else 1
            days_ahead = (wd_num - now.weekday() + 7 * weeks) % 7 or 7 * weeks
            return now + timedelta(days=days_ahead)

    # ◯曜日
    for wd, wd_num in WEEKDAYS.items():
        if re.search(rf'{wd}曜', text):
            days_ahead = (wd_num - now.weekday()) % 7 or 7
            return now + timedelta(days=days_ahead)

    # ◯月◯日
    m = re.search(r'(\d{1,2})月(\d{1,2})日?', text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            dt = datetime(year, month, day)
            if dt < now - timedelta(days=1):
                dt = datetime(year + 1, month, day)
            return dt
        except ValueError:
            pass

    # ◯/◯ (月/日)
    m = re.search(r'(\d{1,2})/(\d{1,2})', text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            dt = datetime(year, month, day)
            if dt < now - timedelta(days=1):
                dt = datetime(year + 1, month, day)
            return dt
        except ValueError:
            pass

    return None


def extract_schedule(text: str) -> Optional[dict]:
    now = datetime.now()

    # 予定らしいキーワードが含まれているか確認
    has_keyword = any(kw in text for kw in SCHEDULE_KEYWORDS)
    has_date_pattern = bool(re.search(
        r'(\d{1,2}[月/]\d{1,2}[日]?|今日|明日|明後日|来週|再来週|月曜|火曜|水曜|木曜|金曜|土曜|日曜|\d{1,2}時)',
        text
    ))
    if not has_keyword and not has_date_pattern:
        return None

    # 日付を解析
    parsed = _parse_date_from_text(text)
    if not parsed:
        return None

    # 時刻パターンを抽出
    time_match = re.search(r'(\d{1,2})[:時](\d{0,2})', text)
    start_time = None
    end_time = None
    if time_match:
        h = int(time_match.group(1))
        m_str = time_match.group(2)
        m = int(m_str) if m_str else 0
        start_time = f"{h:02d}:{m:02d}"
        # 終了時刻
        end_match = re.search(r'[〜~～\-−](\d{1,2})[:時](\d{0,2})', text)
        if end_match:
            eh = int(end_match.group(1))
            em_str = end_match.group(2)
            em = int(em_str) if em_str else 0
            end_time = f"{eh:02d}:{em:02d}"

    # タイトルを生成
    title = _extract_title(text)

    return {
        "title": title,
        "date": parsed.strftime("%Y-%m-%d"),
        "start_time": start_time,
        "end_time": end_time,
        "description": text,
    }


def _extract_title(text: str) -> str:
    # 日時表現を除いた残りをタイトルにする
    cleaned = re.sub(
        r'(\d{4}年)?(\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2})|今日|明日|明後日|来週|再来週|'
        r'月曜|火曜|水曜|木曜|金曜|土曜|日曜|'
        r'\d{1,2}[:時]\d{0,2}(分)?[〜~～\-−]?\d{0,2}[:時]?\d{0,2}(分)?|'
        r'から|まで|に|で|は|の',
        '', text
    ).strip()
    title = cleaned if cleaned else text[:20]
    return title if title else "予定"


def get_calendar_service():
    creds = None
    # Render環境: env変数からtoken.pickleを復元
    token_b64 = os.environ.get("GOOGLE_TOKEN_PICKLE_BASE64")
    token_path = "token.pickle"
    if token_b64:
        token_data = base64.b64decode(token_b64)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pickle") as f:
            f.write(token_data)
            token_path = f.name
    if os.path.exists(token_path):
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # ローカルのみ保存（Renderはread-only filesystemの場合あり）
            try:
                with open("token.pickle", "wb") as f:
                    pickle.dump(creds, f)
            except Exception:
                pass
        else:
            raise RuntimeError("Google Calendar 認証が必要です。")
    return build("calendar", "v3", credentials=creds)


def add_to_google_calendar(schedule: dict) -> str:
    service = get_calendar_service()
    date_str = schedule["date"]
    if schedule.get("start_time"):
        start_dt = f"{date_str}T{schedule['start_time']}:00"
        end_time = schedule.get("end_time") or _add_one_hour(schedule["start_time"])
        end_dt = f"{date_str}T{end_time}:00"
        event = {
            "summary": schedule["title"],
            "description": schedule.get("description", ""),
            "start": {"dateTime": start_dt, "timeZone": "Asia/Tokyo"},
            "end": {"dateTime": end_dt, "timeZone": "Asia/Tokyo"},
        }
    else:
        event = {
            "summary": schedule["title"],
            "description": schedule.get("description", ""),
            "start": {"date": date_str},
            "end": {"date": date_str},
        }
    result = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return result.get("htmlLink", "")


def _add_one_hour(time_str: str) -> str:
    h, m = map(int, time_str.split(":"))
    h = (h + 1) % 24
    return f"{h:02d}:{m:02d}"


def reply_text(reply_token: str, text: str):
    with ApiClient(line_config) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=text)]
        ))


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    text = event.message.text
    reply_token = event.reply_token
    try:
        schedule = extract_schedule(text)
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return
    if not schedule:
        return
    try:
        link = add_to_google_calendar(schedule)
        time_str = f" {schedule['start_time']}〜" if schedule.get("start_time") else ""
        reply = (
            f"📅 カレンダーに登録しました！\n"
            f"【{schedule['title']}】\n"
            f"{schedule['date']}{time_str}\n"
            f"{link}"
        )
    except Exception as e:
        logger.error(f"Calendar error: {e}")
        reply = f"⚠️ 登録に失敗しました: {e}"
    reply_text(reply_token, reply)


@app.route("/", methods=["GET"])
def index():
    return "LINE Calendar Bot is running!"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
