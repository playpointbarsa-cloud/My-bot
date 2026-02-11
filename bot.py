
# -*- coding: utf-8 -*-
import os
import math
import uuid
import shutil
import asyncio
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==========================
# Storage / Defaults
# ==========================
BASE_DIR = Path(os.environ.get("DATA_DIR", "/tmp/telegram_video_bot"))
BASE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SEGMENT = int(os.environ.get("SEGMENT_SECONDS", "180"))  # 3 minutes
SEGMENT_OPTIONS = [60, 120, 180, 300]

MAX_DOWNLOAD_MB = int(os.environ.get("MAX_DOWNLOAD_MB", "400"))  # direct links only
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024

QUALITY_PRESETS = {
    "fast": {"preset": "ultrafast", "crf": "28"},
    "bal":  {"preset": "veryfast",  "crf": "23"},
    "high": {"preset": "medium",    "crf": "20"},
}

AUDIO_MODES = {
    "replace": "استبدال صوت الفيديو",
    "mix":     "دمج مع صوت الفيديو",
}

SYNC_MODES = {
    "restart": "يبدأ من أول الصوت لكل مقطع",
    "cont":    "صوت مستمر عبر المقاطع",
}

ASMR_LEVELS = {
    "light": "ASMR مكتوم (خفيف)",
    "med":   "ASMR مكتوم (متوسط)",
    "full":  "ASMR مكتوم (قوي جداً) ✅",
}

# فلتر ASMR "مكتوم جداً" (full) = Low-pass قوي (500Hz) ليطلع “مكتوم تماماً”
ASMR_FILTERS = {
    "light": "highpass=f=35,lowpass=f=1200,acompressor=threshold=-22dB:ratio=3:attack=10:release=200,volume=1.25,alimiter=limit=0.95",
    "med":   "highpass=f=35,lowpass=f=850,equalizer=f=120:t=q:w=1:g=3,acompressor=threshold=-24dB:ratio=4:attack=10:release=220,volume=1.45,alimiter=limit=0.95",
    "full":  "highpass=f=35,lowpass=f=500,equalizer=f=120:t=q:w=1:g=4,acompressor=threshold=-26dB:ratio=4:attack=10:release=260,volume=1.65,alimiter=limit=0.95",
}

# ==========================
# FFmpeg helpers
# ==========================
def _run(cmd: list[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr

def run_or_raise(cmd: list[str]) -> None:
    code, out, err = _run(cmd)
    if code != 0:
        raise RuntimeError(f"FFmpeg/FFprobe فشل.\nالأمر:\n{' '.join(cmd)}\n\nالخطأ:\n{err[:1800]}")

def ffprobe_duration(path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ]
    code, out, err = _run(cmd)
    if code != 0 or not out.strip():
        raise RuntimeError(f"ffprobe duration failed:\n{err[:1500]}")
    return float(out.strip())

def ffprobe_has_audio(path: str) -> bool:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        path
    ]
    code, out, err = _run(cmd)
    if code != 0:
        return False
    return bool(out.strip())

def fmt_time(sec: int) -> str:
    m = sec // 60
    s = sec % 60
    return f"{m}:{s:02d}"

def safe_suffix(name: Optional[str], default_suffix: str) -> str:
    if not name:
        return default_suffix
    suf = Path(name).suffix.lower()
    return suf if suf else default_suffix

# ==========================
# State / UI
# ==========================
def get_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    ud = context.user_data
    ud.setdefault("seg_len", DEFAULT_SEGMENT)
    ud.setdefault("audio_mode", "replace")  # default: replace
    ud.setdefault("sync_mode", "cont")      # default: continuous audio over all clips
    ud.setdefault("quality", "bal")
    ud.setdefault("cleanup", True)
    ud.setdefault("asmr_level", "full")     # default: FULL muffled

    ud.setdefault("view", "main")
    ud.setdefault("expected", None)         # video_file | audio_file | video_url | audio_url
    ud.setdefault("job_running", False)
    ud.setdefault("cancel", False)

    ud.setdefault("work_dir", None)
    ud.setdefault("video_path", None)
    ud.setdefault("audio_path", None)

    ud.setdefault("dash_chat_id", None)
    ud.setdefault("dash_msg_id", None)
    return ud

def ensure_job_dir(ud: dict, user_id: int, reset: bool = False) -> Path:
    user_dir = BASE_DIR / f"user_{user_id}"
    user_dir.mkdir(parents=True, exist_ok=True)

    if reset and ud.get("work_dir"):
        try:
            shutil.rmtree(ud["work_dir"], ignore_errors=True)
        except Exception:
            pass
        ud["work_dir"] = None
        ud["video_path"] = None
        ud["audio_path"] = None

    if not ud.get("work_dir"):
        job_id = str(uuid.uuid4())[:8]
        work_dir = user_dir / f"job_{job_id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        ud["work_dir"] = str(work_dir)

    return Path(ud["work_dir"])

def main_keyboard(ud: dict) -> InlineKeyboardMarkup:
    v_ok = "✅" if ud.get("video_path") else "❌"
    a_ok = "✅" if ud.get("audio_path") else "❌"
    go_enabled = bool(ud.get("video_path") and ud.get("audio_path") and not ud.get("job_running"))
    go_txt = "✅ ابدأ المعالجة" if go_enabled else "⛔️ ابدأ المعالجة"

    kb = [
        [
            InlineKeyboardButton(f"📹 فيديو (ملف) {v_ok}", callback_data="need_video_file"),
            InlineKeyboardButton(f"🎵 صوت (ملف) {a_ok}", callback_data="need_audio_file"),
        ],
        [
            InlineKeyboardButton("🔗 رابط فيديو مباشر", callback_data="need_video_url"),
            InlineKeyboardButton("🔗 رابط صوت مباشر", callback_data="need_audio_url"),
        ],
        [
            InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings"),
            InlineKeyboardButton(go_txt, callback_data="go"),
        ],
        [InlineKeyboardButton("🧹 Reset", callback_data="reset")],
    ]
    return InlineKeyboardMarkup(kb)

def processing_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 إلغاء المعالجة", callback_data="cancel")]])

def settings_keyboard(ud: dict) -> InlineKeyboardMarkup:
    seg = ud["seg_len"]
    audio_mode = ud["audio_mode"]
    sync_mode = ud["sync_mode"]
    quality = ud["quality"]
    cleanup = ud["cleanup"]
    asmr_level = ud["asmr_level"]

    kb = [
        [InlineKeyboardButton(f"⏱ طول المقطع: {fmt_time(seg)}", callback_data="noop")],
        [
            InlineKeyboardButton("1م", callback_data="seg_60"),
            InlineKeyboardButton("2م", callback_data="seg_120"),
            InlineKeyboardButton("3م", callback_data="seg_180"),
            InlineKeyboardButton("5م", callback_data="seg_300"),
        ],
        [InlineKeyboardButton(f"🎵 وضع الصوت: {AUDIO_MODES[audio_mode]}", callback_data="noop")],
        [
            InlineKeyboardButton("🔊 استبدال", callback_data="mode_replace"),
            InlineKeyboardButton("🎚 دمج", callback_data="mode_mix"),
        ],
        [InlineKeyboardButton(f"🧭 تزامن الصوت: {SYNC_MODES[sync_mode]}", callback_data="noop")],
        [
            InlineKeyboardButton("🔁 إعادة", callback_data="sync_restart"),
            InlineKeyboardButton("➡️ مستمر", callback_data="sync_cont"),
        ],
        [InlineKeyboardButton(f"🎞 الجودة: {quality}", callback_data="noop")],
        [
            InlineKeyboardButton("⚡ سريع", callback_data="q_fast"),
            InlineKeyboardButton("⚖️ متوازن", callback_data="q_bal"),
            InlineKeyboardButton("🏆 عالي", callback_data="q_high"),
        ],
        [InlineKeyboardButton(f"🎧 فلتر ASMR: {ASMR_LEVELS[asmr_level]}", callback_data="noop")],
        [
            InlineKeyboardButton("خفيف", callback_data="asmr_light"),
            InlineKeyboardButton("متوسط", callback_data="asmr_med"),
            InlineKeyboardButton("قوي جداً", callback_data="asmr_full"),
        ],
        [InlineKeyboardButton(f"🧼 تنظيف بعد الانتهاء: {'نعم' if cleanup else 'لا'}", callback_data="toggle_cleanup")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="main")],
    ]
    return InlineKeyboardMarkup(kb)

def dashboard_text(ud: dict) -> str:
    v_ok = "✅" if ud.get("video_path") else "❌"
    a_ok = "✅" if ud.get("audio_path") else "❌"
    seg = fmt_time(ud["seg_len"])
    audio_mode = AUDIO_MODES[ud["audio_mode"]]
    sync_mode = SYNC_MODES[ud["sync_mode"]]
    quality = ud["quality"]
    cleanup = "نعم" if ud["cleanup"] else "لا"
    asmr = ASMR_LEVELS[ud["asmr_level"]]
    running = "🟢 نعم" if ud.get("job_running") else "⚪️ لا"

    return (
        "🎬 <b>بوت تقسيم الفيديو + تركيب ASMR مكتوم</b>\n\n"
        f"📹 فيديو: {v_ok}\n"
        f"🎵 صوت: {a_ok}\n"
        f"⏱ طول المقطع: <b>{seg}</b>\n"
        f"🎚 وضع الصوت: <b>{audio_mode}</b>\n"
        f"🧭 تزامن الصوت: <b>{sync_mode}</b>\n"
        f"🎞 الجودة: <b>{quality}</b>\n"
        f"🎧 فلتر ASMR: <b>{asmr}</b>\n"
        f"🧼 تنظيف بعد الانتهاء: <b>{cleanup}</b>\n"
        f"⚙️ المعالجة الآن: <b>{running}</b>\n\n"
        "📌 <b>الاستخدام</b>\n"
        "1) اختر فيديو (ملف) أو رابط فيديو مباشر.\n"
        "2) اختر صوت (ملف) أو رابط صوت مباشر.\n"
        "3) اضغط ✅ ابدأ المعالجة.\n\n"
        "⚠️ الروابط لازم تكون <b>تحميل مباشر</b> (mp4/mp3...). روابط YouTube ليست تحميل مباشر."
    )

async def send_or_update_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ud = get_state(context)
    chat_id = update.effective_chat.id
    ud["dash_chat_id"] = chat_id

    text = dashboard_text(ud)
    markup = settings_keyboard(ud) if ud.get("view") == "settings" else main_keyboard(ud)

    if ud.get("dash_msg_id"):
        try:
            await context.bot.edit_message_text(
                chat_id=ud["dash_chat_id"],
                message_id=ud["dash_msg_id"],
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            ud["dash_msg_id"] = None

    m = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )
    ud["dash_msg_id"] = m.message_id

# ==========================
# Direct-link downloader
# ==========================
async def download_http(url: str, dest: Path, max_bytes: int) -> int:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as resp:
            resp.raise_for_status()
            total = resp.content_length
            if total is not None and total > max_bytes:
                raise RuntimeError(f"حجم الملف كبير جداً ({total/1024/1024:.1f}MB). الحد {max_bytes/1024/1024:.0f}MB.")
            downloaded = 0
            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise RuntimeError(f"حجم الملف تجاوز الحد {max_bytes/1024/1024:.0f}MB.")
    return downloaded

# ==========================
# Handlers
# ==========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = get_state(context)
    ud["view"] = "main"
    ud["expected"] = None
    await send_or_update_dashboard(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 طريقة الاستخدام:\n"
        "1) اضغط 📹 فيديو (ملف) ثم ارسل الفيديو — أو 🔗 رابط فيديو مباشر.\n"
        "2) اضغط 🎵 صوت (ملف) ثم ارسل الصوت — أو 🔗 رابط صوت مباشر.\n"
        "3) اضغط ✅ ابدأ المعالجة.\n\n"
        "🎧 فلتر ASMR افتراضياً (قوي جداً) = مكتوم جداً.\n"
        "⚠️ روابط YouTube ليست روابط تحميل مباشر."
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    ud = get_state(context)
    data = query.data
    user_id = update.effective_user.id

    if data == "noop":
        return

    if data == "main":
        ud["view"] = "main"
        await send_or_update_dashboard(update, context)
        return

    if data == "settings":
        ud["view"] = "settings"
        await send_or_update_dashboard(update, context)
        return

    if data == "reset":
        if ud.get("job_running"):
            await query.message.reply_text("لا يمكن Reset أثناء المعالجة. اضغط 🛑 إلغاء أولاً.")
            return
        ensure_job_dir(ud, user_id, reset=True)
        ud["expected"] = None
        ud["view"] = "main"
        await query.message.reply_text("✅ تم Reset.")
        await send_or_update_dashboard(update, context)
        return

    if data == "cancel":
        ud["cancel"] = True
        await query.message.reply_text("🛑 تم طلب الإلغاء…")
        return

    if data == "need_video_file":
        ud["expected"] = "video_file"
        await query.message.reply_text("📹 أرسل الفيديو الآن (كـ فيديو أو كـ ملف).")
        return

    if data == "need_audio_file":
        ud["expected"] = "audio_file"
        await query.message.reply_text("🎵 أرسل الصوت الآن (mp3/wav/ogg) أو Voice.")
        return

    if data == "need_video_url":
        ud["expected"] = "video_url"
        await query.message.reply_text(f"🔗 أرسل رابط تحميل مباشر للفيديو (mp4...). حد التنزيل {MAX_DOWNLOAD_MB}MB.")
        return

    if data == "need_audio_url":
        ud["expected"] = "audio_url"
        await query.message.reply_text(f"🔗 أرسل رابط تحميل مباشر للصوت (mp3/wav...). حد التنزيل {MAX_DOWNLOAD_MB}MB.")
        return

    # Settings
    if data.startswith("seg_"):
        sec = int(data.split("_")[1])
        if sec in SEGMENT_OPTIONS:
            ud["seg_len"] = sec
        await send_or_update_dashboard(update, context)
        return

    if data.startswith("mode_"):
        mode = data.split("_")[1]
        if mode in AUDIO_MODES:
            ud["audio_mode"] = mode
        await send_or_update_dashboard(update, context)
        return

    if data.startswith("sync_"):
        sm = data.split("_")[1]
        if sm in SYNC_MODES:
            ud["sync_mode"] = sm
        await send_or_update_dashboard(update, context)
        return

    if data.startswith("q_"):
        q = data.split("_")[1]
        if q in QUALITY_PRESETS:
            ud["quality"] = q
        await send_or_update_dashboard(update, context)
        return

    if data.startswith("asmr_"):
        level = data.split("_")[1]
        if level in ASMR_LEVELS:
            ud["asmr_level"] = level
        await send_or_update_dashboard(update, context)
        return

    if data == "toggle_cleanup":
        ud["cleanup"] = not ud["cleanup"]
        await send_or_update_dashboard(update, context)
        return

    # GO
    if data == "go":
        if ud.get("job_running"):
            await query.message.reply_text("⏳ المعالجة شغالة بالفعل…")
            return
        if not ud.get("video_path") or not ud.get("audio_path"):
            await query.message.reply_text("⛔️ لازم تحدد الفيديو + الصوت أولاً.")
            return

        ud["cancel"] = False
        ud["job_running"] = True
        ud["expected"] = None
        ud["view"] = "main"
        await send_or_update_dashboard(update, context)

        chat_id = query.message.chat_id
        dash_chat_id = ud.get("dash_chat_id")
        dash_msg_id = ud.get("dash_msg_id")

        async def runner():
            try:
                await process_job(context, user_id=user_id, chat_id=chat_id,
                                  dash_chat_id=dash_chat_id, dash_msg_id=dash_msg_id)
            finally:
                ud["job_running"] = False
                ud["cancel"] = False
                try:
                    await send_or_update_dashboard(update, context)
                except Exception:
                    pass

        asyncio.create_task(runner())
        await query.message.reply_text("🚀 بدأت المعالجة…")
        return

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    ud = get_state(context)
    user_id = update.effective_user.id

    if ud.get("job_running"):
        await msg.reply_text("⏳ في معالجة شغّالة. اضغط 🛑 إلغاء إذا تبي توقف.")
        return

    expected = ud.get("expected")
    work_dir = ensure_job_dir(ud, user_id)

    # expecting a direct URL
    if expected in ("video_url", "audio_url"):
        text = (msg.text or "").strip()
        if not (text.startswith("http://") or text.startswith("https://")):
            await msg.reply_text("⛔️ أرسل رابط يبدأ بـ http أو https.")
            return

        if expected == "video_url":
            dest = work_dir / "input_video.mp4"
            await msg.reply_text("⬇️ تنزيل الفيديو من الرابط…")
            try:
                await download_http(text, dest, MAX_DOWNLOAD_BYTES)
            except Exception as e:
                await msg.reply_text(f"❌ فشل تنزيل الفيديو:\n{str(e)[:1200]}")
                return
            ud["video_path"] = str(dest)
            ud["expected"] = None
            await msg.reply_text("✅ تم حفظ الفيديو من الرابط.")
            await send_or_update_dashboard(update, context)
            return

        if expected == "audio_url":
            dest = work_dir / "input_audio.mp3"
            await msg.reply_text("⬇️ تنزيل الصوت من الرابط…")
            try:
                await download_http(text, dest, MAX_DOWNLOAD_BYTES)
            except Exception as e:
                await msg.reply_text(f"❌ فشل تنزيل الصوت:\n{str(e)[:1200]}")
                return
            ud["audio_path"] = str(dest)
            ud["expected"] = None
            await msg.reply_text("✅ تم حفظ الصوت من الرابط.")
            await send_or_update_dashboard(update, context)
            return

    # detect media files
    file_obj = None
    filename = None
    is_video = False
    is_audio = False

    if msg.video:
        file_obj = msg.video
        filename = file_obj.file_name or "video.mp4"
        is_video = True
    elif msg.document and (msg.document.mime_type or "").lower().startswith("video/"):
        file_obj = msg.document
        filename = file_obj.file_name or "video.mp4"
        is_video = True
    elif msg.audio:
        file_obj = msg.audio
        filename = file_obj.file_name or "audio.mp3"
        is_audio = True
    elif msg.voice:
        file_obj = msg.voice
        filename = "voice.ogg"
        is_audio = True
    elif msg.document and (msg.document.mime_type or "").lower().startswith("audio/"):
        file_obj = msg.document
        filename = file_obj.file_name or "audio.mp3"
        is_audio = True
    else:
        text = (msg.text or "").strip()
        if "youtube.com" in text or "youtu.be" in text:
            await msg.reply_text("⚠️ روابط YouTube ليست روابط تحميل مباشر. ارفع الملف هنا أو استخدم رابط تحميل مباشر من تخزينك.")
        await send_or_update_dashboard(update, context)
        return

    # enforce expectation
    if expected == "video_file" and not is_video:
        await msg.reply_text("❌ كنت متوقع فيديو. أرسل فيديو أو اضغط Reset.")
        return
    if expected == "audio_file" and not is_audio:
        await msg.reply_text("❌ كنت متوقع صوت. أرسل صوت أو اضغط Reset.")
        return

    if is_video:
        suf = safe_suffix(filename, ".mp4")
        vpath = work_dir / f"input_video{suf}"
        await msg.reply_text("⬇️ تنزيل الفيديو…")
        tg_file = await context.bot.get_file(file_obj.file_id)
        await tg_file.download_to_drive(custom_path=str(vpath))
        ud["video_path"] = str(vpath)
        ud["expected"] = None
        await msg.reply_text("✅ تم حفظ الفيديو.")
        await send_or_update_dashboard(update, context)
        return

    if is_audio:
        suf = safe_suffix(filename, ".mp3")
        apath = work_dir / f"input_audio{suf}"
        await msg.reply_text("⬇️ تنزيل الصوت…")
        tg_file = await context.bot.get_file(file_obj.file_id)
        await tg_file.download_to_drive(custom_path=str(apath))
        ud["audio_path"] = str(apath)
        ud["expected"] = None
        await msg.reply_text("✅ تم حفظ الصوت.")
        await send_or_update_dashboard(update, context)
        return

# ==========================
# Processing
# ==========================
async def process_job(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int,
                      dash_chat_id: Optional[int], dash_msg_id: Optional[int]) -> None:
    ud = get_state(context)
    work_dir = Path(ud["work_dir"])
    video_path = Path(ud["video_path"])
    audio_path = Path(ud["audio_path"])

    seg_len = int(ud["seg_len"])
    audio_mode = ud["audio_mode"]
    sync_mode = ud["sync_mode"]
    quality_key = ud["quality"]
    cleanup = ud["cleanup"]
    asmr_level = ud["asmr_level"]

    preset = QUALITY_PRESETS[quality_key]["preset"]
    crf = QUALITY_PRESETS[quality_key]["crf"]
    asmr_filter = ASMR_FILTERS[asmr_level]

    out_dir = work_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    async def progress(text: str):
        if dash_chat_id and dash_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=dash_chat_id,
                    message_id=dash_msg_id,
                    text=text,
                    reply_markup=processing_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
                return
            except Exception:
                pass
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

    try:
        await progress("🔎 <b>أقرأ مدة الفيديو…</b>")
        video_dur = await asyncio.to_thread(ffprobe_duration, str(video_path))
        has_vid_audio = await asyncio.to_thread(ffprobe_has_audio, str(video_path))
        n = max(1, math.ceil(video_dur / seg_len))

        # 1) Apply ASMR muffled filter once
        audio_muffled = work_dir / "audio_muffled.m4a"
        await progress("🎧 <b>تطبيق فلتر ASMR (مكتوم جداً)…</b>")
        cmd_muffle = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(audio_path),
            "-af", asmr_filter,
            "-c:a", "aac", "-b:a", "192k",
            "-ar", "44100", "-ac", "2",
            str(audio_muffled)
        ]
        await asyncio.to_thread(run_or_raise, cmd_muffle)

        # 2) Continuous audio (looped) to match whole video duration
        audio_full = None
        if sync_mode == "cont":
            audio_full = work_dir / "audio_full.m4a"
            await progress("🎚 <b>تحضير صوت مستمر بطول الفيديو…</b>")
            cmd_full = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-stream_loop", "-1", "-i", str(audio_muffled),
                "-t", f"{video_dur:.3f}",
                "-c:a", "aac", "-b:a", "192k",
                "-ar", "44100", "-ac", "2",
                str(audio_full)
            ]
            await asyncio.to_thread(run_or_raise, cmd_full)

        # 3) Split + merge
        for i in range(n):
            if ud.get("cancel"):
                await context.bot.send_message(chat_id=chat_id, text="🛑 تم الإلغاء ✅")
                return

            start = i * seg_len
            length = min(seg_len, max(0.0, video_dur - start))
            if length <= 0:
                break

            out_path = out_dir / f"clip_{i+1:03d}.mp4"
            await progress(f"🎬 <b>المقطع {i+1}/{n}</b>\n⏱ {fmt_time(int(length))} | 🎧 {ASMR_LEVELS[asmr_level]}")

            video_in = ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(video_path)]

            if sync_mode == "cont" and audio_full:
                audio_in = ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(audio_full)]
            else:
                audio_in = ["-stream_loop", "-1", "-i", str(audio_muffled)]

            if audio_mode == "mix" and has_vid_audio:
                cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    *video_in,
                    *audio_in,
                    "-t", f"{length:.3f}",
                    "-filter_complex",
                    "[0:a]volume=1.0[a0];[1:a]volume=1.0[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                    "-map", "0:v:0",
                    "-map", "[aout]",
                    "-c:v", "libx264", "-preset", preset, "-crf", crf,
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    str(out_path)
                ]
            else:
                cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    *video_in,
                    *audio_in,
                    "-t", f"{length:.3f}",
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "libx264", "-preset", preset, "-crf", crf,
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    str(out_path)
                ]

            await asyncio.to_thread(run_or_raise, cmd)

            caption = f"✅ مقطع {i+1}/{n} | {fmt_time(int(length))}"
            try:
                with open(out_path, "rb") as f:
                    await context.bot.send_video(chat_id=chat_id, video=f, caption=caption)
            except Exception:
                with open(out_path, "rb") as f:
                    await context.bot.send_document(chat_id=chat_id, document=f, caption=caption)

        await progress("✅ <b>انتهت المعالجة!</b>\nاضغط 🧹 Reset لبدء مشروع جديد ✨")

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ خطأ:\n{str(e)[:3500]}")
    finally:
        if cleanup:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
                ud["work_dir"] = None
                ud["video_path"] = None
                ud["audio_path"] = None
            except Exception:
                pass

# ==========================
# Entrypoint
# ==========================
def main():
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ضع توكن البوت في المتغير BOT_TOKEN في Railway.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))

    print("Bot is running (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
