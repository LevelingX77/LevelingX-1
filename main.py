import os
import json
import time
import asyncio
import logging
import platform
import threading
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from flask import Flask, jsonify
from waitress import serve

from dotenv import load_dotenv

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None
    PSUTIL_AVAILABLE = False


# =========================================================
# ENV
# =========================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

_raw_guild_id = os.getenv("GUILD_ID", "").strip()

try:
    GUILD_ID = int(_raw_guild_id) if _raw_guild_id else 0
except (TypeError, ValueError):
    GUILD_ID = 0

PORT = int(
    os.getenv(
        "PORT",
        "10000"
    )
)


# =========================================================
# FILE
# =========================================================

DATA_FILE = "data.json"


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

log = logging.getLogger("LevelingXBot")


# =========================================================
# SUPPRESS BENIGN "PRIVILEGED MESSAGE CONTENT INTENT" WARNING
# =========================================================
# บอทนี้ใช้ระบบคำสั่งแบบ Slash Command (app_commands) ทั้งหมด
# ไม่มีการใช้ Prefix Command ("!") ใด ๆ (ไม่มี @bot.command หรือ
# on_message ที่ประมวลผลเนื้อหาข้อความ) ดังนั้น Privileged
# "Message Content Intent" จึงไม่จำเป็นต่อการทำงานของคำสั่งใด ๆ เลย
# คำเตือนนี้เป็นคำเตือนมาตรฐานของ discord.py ที่ขึ้นทุกครั้งเมื่อมีการ
# ตั้งค่า command_prefix โดยไม่เปิด message_content intent - ไม่ได้
# บ่งบอกว่าคำสั่ง Slash Command จะใช้งานไม่ได้แต่อย่างใด จึงกรอง log
# บรรทัดนี้ทิ้งเพื่อลด Noise โดยไม่กระทบการทำงานของระบบคำสั่งเดิม

class _SuppressMessageContentWarning(logging.Filter):

    def filter(self, record):

        return "Privileged message content intent is missing" not in record.getMessage()


logging.getLogger("discord.ext.commands.bot").addFilter(
    _SuppressMessageContentWarning()
)


# =========================================================
# STARTUP VALIDATION
# =========================================================

def validate_startup_env():
    """
    ตรวจสอบ Environment Variables ที่จำเป็นก่อนบอทเริ่มทำงาน
    ถ้าค่าที่จำเป็นขาดหรือผิดพลาด จะ log สาเหตุให้ชัดเจนและ
    หยุดโปรแกรมทันที (ไม่ปล่อยให้บอทรันแบบพัง หรือ GUILD_ID เป็น 0
    ซึ่งจะทำให้ระบบจำกัด Guild ทำงานผิดพลาดและออกจากทุก Server)
    """

    errors = []

    if not TOKEN:

        errors.append(
            "DISCORD_TOKEN ไม่ได้ถูกตั้งค่าใน Environment Variables"
        )

    if not GUILD_ID:

        errors.append(
            "GUILD_ID ไม่ได้ถูกตั้งค่า หรือมีค่าเป็น 0 / ไม่ใช่ตัวเลข "
            "ใน Environment Variables (ต้องระบุ Guild ID ของ Server หลักที่อนุญาตให้บอทใช้งาน)"
        )

    if errors:

        log.error(
            "ไม่สามารถเริ่มบอทได้ เนื่องจากตั้งค่า Environment Variables ไม่ถูกต้อง:"
        )

        for message in errors:

            log.error(
                " - %s",
                message
            )

        raise SystemExit(1)

    log.info(
        "Startup environment validation passed. GUILD_ID=%s",
        GUILD_ID
    )

    # ---------------------------------------------
    # เตือนเรื่อง Ephemeral Filesystem
    # ---------------------------------------------
    # Hosting บางเจ้า (เช่น Render Free/Starter โดยไม่ผูก Persistent Disk,
    # Heroku, บาง Container Platform) จะรีเซ็ต Filesystem ทุกครั้งที่
    # restart/redeploy ทำให้ data.json ที่บันทึกไว้ (ห้อง Report, Admin,
    # ห้องเสียง, Embed ที่ตั้งไว้) หายทั้งหมด บอทจะยังทำงานได้ปกติ
    # (จะสร้าง data.json ใหม่เป็นค่า default อัตโนมัติ) แต่การตั้งค่าที่
    # เคยทำไว้จะหายไป จึงแจ้งเตือนไว้ที่นี่เพื่อให้ผู้ดูแลระบบทราบ
    # และพิจารณาผูก Persistent Disk หรือย้ายไปใช้ Database แทน

    log.warning(
        "หมายเหตุ: ระบบเก็บข้อมูลใช้ไฟล์ %s บน Local Filesystem หาก Hosting "
        "ที่ใช้เป็นแบบ Ephemeral (เช่น รีเซ็ต Filesystem ทุกครั้งที่ Restart/"
        "Redeploy) การตั้งค่าห้อง Report, Admin, ห้องเสียง 24/7 และ Embed "
        "ที่บันทึกไว้จะหายไป แนะนำให้ผูก Persistent Disk เข้ากับ path นี้ "
        "หรือย้ายไปใช้ฐานข้อมูลถาวร (เช่น PostgreSQL/SQLite บน Volume) "
        "เพื่อความปลอดภัยของข้อมูลในระยะยาว",
        DATA_FILE
    )

    # ---------------------------------------------
    # ตรวจสอบว่ามี PyNaCl สำหรับระบบ Voice หรือไม่
    # ---------------------------------------------
    # ถ้าไม่มี PyNaCl บอทจะยัง Login และใช้ Slash Command อื่น ๆ ได้ปกติ
    # แต่คำสั่ง /botonline และระบบต่อห้องเสียง 24/7 จะใช้งานไม่ได้

    try:

        import nacl  # noqa: F401

    except ImportError:

        log.warning(
            "ไม่พบไลบรารี PyNaCl - ระบบ Voice (/botonline และการต่อห้องเสียง "
            "24/7) จะไม่สามารถใช้งานได้ กรุณาติดตั้ง PyNaCl ตาม requirements.txt"
        )


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)

START_TIME = time.time()


@app.route("/")
def home():

    return jsonify({
        "status": "online",
        "bot": "LevelingX",
        "service": "Discord Bot"
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "online",
        "bot": "LevelingX",
        "uptime": int(
            time.time() - START_TIME
        )
    }), 200


def run_web_server():

    try:

        log.info(
            "Starting web server on port %s",
            PORT
        )

        serve(
            app,
            host="0.0.0.0",
            port=PORT
        )

    except Exception:

        log.exception(
            "Web server crashed."
        )


# =========================================================
# DATA SYSTEM
# =========================================================

def default_data():
    """คืนค่าโครงสร้างข้อมูลเริ่มต้นชุดใหม่เสมอ (ไม่ใช้ dict ร่วมกัน
    ระหว่างการเรียกหลายครั้ง เพื่อป้องกัน guilds ของแต่ละ Guild
    เขียนทับกันโดยไม่ตั้งใจ)."""

    return {
        "guilds": {}
    }


# ป้องกันการเขียนไฟล์ data.json พร้อมกันจากหลาย Thread/Coroutine
_file_lock = threading.Lock()


def load_data():

    if not os.path.exists(
        DATA_FILE
    ):

        fresh = default_data()

        save_data(fresh)

        return fresh

    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            raw = file.read()

        if not raw.strip():

            raise ValueError(
                "data.json is empty"
            )

        parsed = json.loads(raw)

        if not isinstance(
            parsed,
            dict
        ):

            raise ValueError(
                "data.json root is not a JSON object"
            )

        # -----------------------------------------
        # ตรวจโครงสร้างว่ามี guilds เสมอ
        # ถ้าไม่มี/ผิดชนิด ให้ซ่อมแซมแทนที่จะทิ้งข้อมูลทั้งหมด
        # -----------------------------------------

        if "guilds" not in parsed or not isinstance(
            parsed.get("guilds"),
            dict
        ):

            log.warning(
                "data.json missing or invalid 'guilds' key - repairing structure"
            )

            parsed["guilds"] = {}

            save_data(parsed)

        return parsed

    except Exception:

        log.exception(
            "Failed to load data.json (corrupted or unreadable) - "
            "recovering with default data"
        )

        fresh = default_data()

        save_data(fresh)

        return fresh


def save_data(data):

    try:

        temp_file = DATA_FILE + ".tmp"

        with _file_lock:

            with open(
                temp_file,
                "w",
                encoding="utf-8"
            ) as file:

                json.dump(
                    data,
                    file,
                    ensure_ascii=False,
                    indent=4
                )

            os.replace(
                temp_file,
                DATA_FILE
            )

    except Exception:

        log.exception(
            "Failed to save data.json"
        )


data = load_data()

# ป้องกันการเขียนไฟล์ data.json พร้อมกันจากหลาย Interaction (ระดับ asyncio)
data_lock = asyncio.Lock()


async def persist_data():
    """บันทึก data.json อย่างปลอดภัยจาก async context (กันเขียนพร้อมกัน)."""

    async with data_lock:

        save_data(data)


def get_guild_data(
    guild_id: int
):

    key = str(guild_id)

    if key not in data["guilds"]:

        data["guilds"][key] = {

            "report_channel_id": None,

            "admin_users": [],

            "voice_channel_id": None,

            "embed": {

                "title": "📨 รายงานปัญหา",

                "description": (
                    "หากพบปัญหาหรือมีเรื่องที่ต้องการแจ้งทีมงาน\n"
                    "สามารถกดปุ่มด้านล่างเพื่อส่งรายงานได้เลย\n\n"
                    "กรุณากรอกข้อมูลให้ละเอียด "
                    "เพื่อให้ทีมงานตรวจสอบได้เร็วขึ้น"
                ),

                "color": "#5865F2",

                "image": "",

                "footer": "Developer : LevelingX"

            }

        }

        save_data(data)

    return data["guilds"][key]


# =========================================================
# HELPERS
# =========================================================

def hex_to_color(
    value: str
):

    try:

        value = (
            value
            .replace("#", "")
            .strip()
        )

        if len(value) != 6:

            return discord.Color.blurple()

        return discord.Color(
            int(value, 16)
        )

    except Exception:

        return discord.Color.blurple()


def is_owner(
    interaction: discord.Interaction
):

    if not interaction.guild:

        return False

    return (
        interaction.user.id
        == interaction.guild.owner_id
    )


def is_report_admin(
    interaction: discord.Interaction
):

    if not interaction.guild:

        return False

    guild_data = get_guild_data(
        interaction.guild.id
    )

    user_id = interaction.user.id

    # Owner มีสิทธิ์ทุกอย่าง
    if user_id == interaction.guild.owner_id:

        return True

    # ผู้ที่ได้รับอนุญาต
    if user_id in guild_data[
        "admin_users"
    ]:

        return True

    return False


def format_uptime(
    seconds: int
):

    days = seconds // 86400
    seconds %= 86400

    hours = seconds // 3600
    seconds %= 3600

    minutes = seconds // 60
    seconds %= 60

    return (
        f"{days} วัน "
        f"{hours} ชั่วโมง "
        f"{minutes} นาที "
        f"{seconds} วินาที"
    )


async def safe_dm(
    user,
    embed
):

    try:

        await user.send(
            embed=embed
        )

        return True

    except discord.Forbidden:

        log.warning(
            "Cannot DM user %s",
            getattr(
                user,
                "id",
                "unknown"
            )
        )

        return False

    except Exception:

        log.exception(
            "DM error"
        )

        return False


# =========================================================
# INTERACTION-SAFE REPLY HELPER
# =========================================================

async def safe_error_reply(
    interaction: discord.Interaction,
    message: str
):
    """
    ตอบกลับ Interaction ด้วยข้อความ error โดยไม่เสี่ยงเกิด
    'Interaction already acknowledged' หรือ response ซ้ำ:
    ถ้า Interaction ถูกตอบ/deferred ไปแล้ว ให้ใช้ followup แทน
    """

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True
            )

    except Exception:

        log.exception(
            "Failed to send error reply to interaction"
        )


# =========================================================
# EMBED / TEXT LIMIT HELPERS
# =========================================================

EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FOOTER_LIMIT = 2048
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_TOTAL_LIMIT = 6000
EMBED_URL_LIMIT = 2048


def truncate_field_value(
    text,
    limit=EMBED_FIELD_VALUE_LIMIT
):
    """
    ตัดข้อความให้ไม่เกินขีดจำกัดของ Embed Field (ปกติ 1024 ตัวอักษร)
    เพื่อป้องกัน Discord API error เมื่อผู้ใช้กรอกข้อมูลยาวเกินไปใน Modal
    """

    if text is None:

        return "-"

    text = str(text)

    if len(text) <= limit:

        return text

    suffix = "\n...(ข้อความถูกตัดเนื่องจากยาวเกินไป)"

    cutoff = max(
        limit - len(suffix),
        0
    )

    return text[:cutoff] + suffix


def validate_embed_limits(
    title="",
    description="",
    footer="",
    image=""
):
    """
    ตรวจสอบว่าค่าที่จะใช้สร้าง Embed อยู่ในขีดจำกัดของ Discord หรือไม่
    คืนค่าเป็น list ของข้อความ error (list ว่าง = ผ่านการตรวจสอบ)
    """

    errors = []

    title = title or ""
    description = description or ""
    footer = footer or ""
    image = image or ""

    if len(title) > EMBED_TITLE_LIMIT:

        errors.append(
            f"หัวข้อ (title) ยาวเกินไป "
            f"(สูงสุด {EMBED_TITLE_LIMIT} ตัวอักษร ตอนนี้ {len(title)})"
        )

    if len(description) > EMBED_DESCRIPTION_LIMIT:

        errors.append(
            f"เนื้อหา (description) ยาวเกินไป "
            f"(สูงสุด {EMBED_DESCRIPTION_LIMIT} ตัวอักษร ตอนนี้ {len(description)})"
        )

    if len(footer) > EMBED_FOOTER_LIMIT:

        errors.append(
            f"Footer ยาวเกินไป "
            f"(สูงสุด {EMBED_FOOTER_LIMIT} ตัวอักษร ตอนนี้ {len(footer)})"
        )

    if image:

        if len(image) > EMBED_URL_LIMIT:

            errors.append(
                f"URL รูปภาพยาวเกินไป (สูงสุด {EMBED_URL_LIMIT} ตัวอักษร)"
            )

        elif not (
            image.startswith("http://")
            or image.startswith("https://")
        ):

            errors.append(
                "URL รูปภาพต้องขึ้นต้นด้วย http:// หรือ https:// เท่านั้น"
            )

    total_length = (
        len(title)
        + len(description)
        + len(footer)
    )

    if total_length > EMBED_TOTAL_LIMIT:

        errors.append(
            f"ความยาวรวมของ Embed เกินขีดจำกัด "
            f"(สูงสุด {EMBED_TOTAL_LIMIT} ตัวอักษร ตอนนี้ {total_length})"
        )

    return errors


# =========================================================
# BOT
# =========================================================

class LevelingXBot(
    commands.Bot
):

    def __init__(self):

        intents = discord.Intents.default()

        intents.guilds = True
        intents.members = True
        intents.voice_states = True

        # ใช้ commands.when_mentioned_or แทน Prefix ตายตัวอย่าง "!" -
        # Bot นี้ใช้ Slash Commands เป็นหลัก ไม่มี Prefix Command ใด ๆ
        # จริง แต่ discord.py (commands.Bot) ยังคงต้องการค่า
        # command_prefix เสมอ การ Mention ตรวจจับได้โดยไม่ต้องเปิด
        # Message Content Intent เพราะข้อความที่ Mention บอทได้รับการ
        # ยกเว้นจาก Discord อยู่แล้ว

        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents
        )

        self.bot_start_time = time.time()

    async def setup_hook(
        self
    ):

        # ---------------------------------------------
        # Persistent Views (ลงทะเบียนเสมอ ไม่ว่าจะอยู่โหมดไหน)
        # ---------------------------------------------
        # ต้องลงทะเบียนก่อนเสมอเพื่อให้ Component (custom_id เดิม:
        # levelingx:report / levelingx:accept / levelingx:close) ทำงาน
        # ต่อได้หลัง Restart โดยไม่ขึ้นกับว่าจะ Sync คำสั่งหรือไม่
        # ---------------------------------------------

        self.add_view(
            ReportPanelView()
        )

        self.add_view(
            ReportAdminView()
        )

        # ---------------------------------------------
        # Command Sync - ควบคุมด้วย Environment Variable เท่านั้น
        # ---------------------------------------------
        # ค่า Default: COMMAND_SYNC=0 -> ไม่เรียก tree.sync() เลย ไม่ว่า
        # กรณีใด ๆ (ไม่ใช้ data.json หรือ Hash ใด ๆ มาตัดสินใจอีกต่อไป
        # เพราะ Filesystem ของ Render Free อาจไม่ถาวร ทำให้ข้อมูลที่ใช้
        # ตัดสินใจหายไปได้โดยไม่คาดคิด)
        #
        # COMMAND_SYNC=1 -> เข้าสู่ "โหมด Sync ครั้งเดียว": Sync คำสั่ง
        # ไปยัง Guild ที่กำหนด แล้วปิด Bot ลงทันที (ไม่เชื่อมต่อ Gateway
        # ต่อ ไม่ทำงานเป็นบอทตามปกติในรอบนี้) เพื่อให้ Process จบแบบ
        # สะอาด ไม่มี Sync ซ้ำ ไม่มี Retry Loop ผู้ดูแลต้องตั้งค่ากลับ
        # เป็น COMMAND_SYNC=0 แล้ว Deploy/Restart อีกครั้งเพื่อให้บอท
        # กลับมาทำงานตามปกติ
        # ---------------------------------------------

        command_sync_enabled = os.getenv(
            "COMMAND_SYNC",
            "0"
        ).strip() == "1"

        if command_sync_enabled:

            self._command_sync_ok = False

            if not GUILD_ID:

                log.error(
                    "COMMAND_SYNC=1 ถูกตั้งไว้ แต่ไม่มี GUILD_ID ที่ถูกต้อง "
                    "- ไม่สามารถ Sync ได้ จะหยุด Process (ไม่ทำงานเป็นบอท "
                    "ต่อในรอบนี้)"
                )

            else:

                guild = discord.Object(
                    id=GUILD_ID
                )

                self.tree.copy_global_to(
                    guild=guild
                )

                try:

                    synced = await self.tree.sync(
                        guild=guild
                    )

                    log.info(
                        "COMMAND_SYNC=1: Sync สำเร็จ %d คำสั่ง ไปยัง Guild "
                        "%s แล้ว - กรุณาตั้งค่า COMMAND_SYNC=0 กลับ แล้ว "
                        "Deploy/Restart อีกครั้งเพื่อให้บอทกลับมาทำงาน "
                        "ตามปกติ (ไม่เช่นนั้นทุก Restart จะ Sync ซ้ำและจบ "
                        "Process ทันทีแบบนี้ไปเรื่อย ๆ)",
                        len(synced),
                        GUILD_ID
                    )

                    self._command_sync_ok = True

                except discord.HTTPException:

                    log.exception(
                        "COMMAND_SYNC=1: Sync ล้มเหลวสำหรับ Guild %s",
                        GUILD_ID
                    )

            # โหมด Sync ครั้งเดียว: ปิด Bot ทันทีหลัง Sync (สำเร็จหรือไม่
            # ก็ตาม) ไม่เชื่อมต่อ Gateway ต่อ ไม่ Start Voice Loop ไม่มี
            # การเรียก tree.sync() ซ้ำในรอบนี้อีก

            await self.close()

            return

        # ---------------------------------------------
        # โหมดทำงานปกติ (COMMAND_SYNC != "1")
        # ---------------------------------------------
        # ไม่เรียก tree.sync() เลยในโหมดนี้ - คำสั่งที่เคย Sync ไว้จาก
        # รอบ COMMAND_SYNC=1 ก่อนหน้ายังคงอยู่ฝั่ง Discord ตามปกติ ไม่ต้อง
        # Sync ซ้ำทุกครั้งที่ Restart
        # ---------------------------------------------

        log.info(
            "COMMAND_SYNC=0 (ปกติ) - ข้าม tree.sync() เพื่อไม่เรียก "
            "Discord API โดยไม่จำเป็นตอน Restart"
        )

        # ---------------------------------------------
        # Voice Reconnect Loop
        # ---------------------------------------------

        self.voice_reconnect_loop.start()

    async def on_ready(
        self
    ):

        log.info(
            "Logged in as %s (%s)",
            self.user,
            self.user.id
        )

        # ---------------------------------------------
        # Login/Connect สำเร็จ -> เคลียร์ Cooldown จาก 429 ครั้งก่อน (ถ้ามี)
        # ป้องกันไม่ให้ Cooldown เก่าค้างอยู่และทำให้ Startup รอบถัดไป
        # (เช่น redeploy ปกติ) ต้องรอโดยไม่จำเป็น ทั้งที่ Login ผ่านแล้ว
        # ---------------------------------------------

        clear_login_cooldown()

        # ---------------------------------------------
        # Status
        # ---------------------------------------------

        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Game(
                "Developer : LevelingX"
            )
        )

        # ---------------------------------------------
        # ป้องกัน Bot อยู่ใน Guild อื่นที่ไม่ได้รับอนุญาต
        # (กันกรณี Bot เคยเข้าร่วม Guild อื่นไว้ก่อนตั้งค่า GUILD_ID)
        # ---------------------------------------------

        await self.enforce_single_guild()

        # ---------------------------------------------
        # ตรวจ Voice
        # ---------------------------------------------

        await self.restore_voice()

    async def enforce_single_guild(
        self
    ):

        if not GUILD_ID:

            return

        for guild in list(self.guilds):

            if guild.id != GUILD_ID:

                log.warning(
                    "Leaving unauthorized guild: %s (%s)",
                    guild.name,
                    guild.id
                )

                try:

                    await guild.leave()

                except Exception:

                    log.exception(
                        "Failed to leave unauthorized guild"
                    )

    async def restore_voice(
        self
    ):

        if not GUILD_ID:

            return

        guild = self.get_guild(
            GUILD_ID
        )

        if not guild:

            return

        guild_data = get_guild_data(
            GUILD_ID
        )

        channel_id = guild_data.get(
            "voice_channel_id"
        )

        if not channel_id:

            return

        channel = guild.get_channel(
            channel_id
        )

        if not isinstance(
            channel,
            discord.VoiceChannel
        ):

            # ห้องเสียงที่เคยตั้งไว้ถูกลบ หรือหาไม่เจอ
            # เคลียร์ค่าที่บันทึกไว้เพื่อไม่ให้วนพยายามต่อห้องที่ไม่มีอยู่จริงตลอดไป
            log.warning(
                "Stored voice channel %s not found (deleted?) - clearing voice_channel_id",
                channel_id
            )

            guild_data["voice_channel_id"] = None

            await persist_data()

            return

        try:

            voice_client = guild.voice_client

            if voice_client:

                if voice_client.channel.id == channel.id:

                    return

                await voice_client.move_to(
                    channel
                )

                return

            await channel.connect(
                reconnect=True
            )

            log.info(
                "Connected to voice channel: %s",
                channel.name
            )

        except Exception:

            log.exception(
                "Voice connection error"
            )

    @tasks.loop(
        minutes=1
    )
    async def voice_reconnect_loop(
        self
    ):

        try:

            await self.restore_voice()

        except Exception:

            log.exception(
                "Voice reconnect loop error"
            )

    @voice_reconnect_loop.before_loop
    async def before_voice_loop(
        self
    ):

        await self.wait_until_ready()


bot = LevelingXBot()


# =========================================================
# REPORT MODAL
# =========================================================

class ReportModal(
    discord.ui.Modal,
    title="📨 รายงานปัญหา"
):

    form_name = discord.ui.TextInput(
        label="ชื่อในฟอร์ม",
        placeholder="กรอกชื่อของคุณ",
        required=True,
        max_length=100
    )

    problem = discord.ui.TextInput(
        label="ปัญหา",
        placeholder="อธิบายปัญหาที่พบ",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000
    )

    reason = discord.ui.TextInput(
        label="เหตุผล / รายละเอียดเพิ่มเติม",
        placeholder="รายละเอียดเพิ่มเติม",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=2000
    )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        try:

            if not interaction.guild:

                await interaction.response.send_message(
                    "❌ ไม่สามารถใช้ระบบนี้ใน DM ได้",
                    ephemeral=True
                )

                return

            guild_data = get_guild_data(
                interaction.guild.id
            )

            channel_id = guild_data.get(
                "report_channel_id"
            )

            if not channel_id:

                await interaction.response.send_message(
                    "❌ ยังไม่ได้ตั้งห้องรับ Report\n"
                    "ให้หัวดิสใช้ `/set channel` ก่อน",
                    ephemeral=True
                )

                return

            channel = interaction.guild.get_channel(
                channel_id
            )

            if not isinstance(
                channel,
                discord.TextChannel
            ):

                await interaction.response.send_message(
                    "❌ ห้อง Report ที่ตั้งไว้ใช้งานไม่ได้",
                    ephemeral=True
                )

                return

            # -----------------------------------------
            # Embed
            # -----------------------------------------

            embed = discord.Embed(
                title="🚨 มีการแจ้งปัญหาใหม่เข้ามาค่ะ!",
                color=discord.Color.red(),
                timestamp=datetime.now(
                    timezone.utc
                )
            )

            # ไม่ใส่ User ID
            embed.add_field(
                name="👤 ผู้ส่งรายงาน",
                value=interaction.user.mention,
                inline=False
            )

            embed.add_field(
                name="🧰 ชื่อในฟอร์ม",
                value=truncate_field_value(
                    self.form_name.value
                ),
                inline=False
            )

            embed.add_field(
                name="📌 ปัญหา",
                value=truncate_field_value(
                    self.problem.value
                ),
                inline=False
            )

            embed.add_field(
                name="💡 เหตุผล / รายละเอียด",
                value=truncate_field_value(
                    self.reason.value or "-"
                ),
                inline=False
            )

            embed.set_thumbnail(
                url=interaction.user.display_avatar.url
            )

            embed.set_footer(
                text="สถานะ: รอการตอบรับ • Developer : LevelingX"
            )

            # -----------------------------------------
            # Admin Mention
            # -----------------------------------------

            admin_mentions = []

            for user_id in guild_data[
                "admin_users"
            ]:

                member = interaction.guild.get_member(
                    user_id
                )

                if member:

                    admin_mentions.append(
                        member.mention
                    )

            content = (
                "🚨 มี Report ใหม่เข้ามา!\n"
                + " ".join(admin_mentions)
                if admin_mentions
                else "🚨 มี Report ใหม่เข้ามา!"
            )

            # -----------------------------------------
            # ส่ง
            # -----------------------------------------

            await channel.send(
                content=content,
                embed=embed,
                view=ReportAdminView(),
                allowed_mentions=discord.AllowedMentions(
                    users=True
                )
            )

            # -----------------------------------------
            # DM ผู้แจ้ง
            # -----------------------------------------

            await interaction.response.send_message(
                "✅ ส่งรายงานเรียบร้อยแล้ว\n"
                "เมื่อทีมงานตอบรับปัญหา "
                "บอทจะแจ้งเตือนคุณทาง DM",
                ephemeral=True
            )

        except Exception:

            log.exception(
                "Report submit error"
            )

            await safe_error_reply(
                interaction,
                "❌ เกิดข้อผิดพลาดในการส่ง Report"
            )


# =========================================================
# REPORT PANEL
# =========================================================

class ReportPanelView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    @discord.ui.button(
        label="รายงานปัญหา!!",
        emoji="📨",
        style=discord.ButtonStyle.primary,
        custom_id="levelingx:report"
    )
    async def report_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        try:

            if not interaction.guild:

                await interaction.response.send_message(
                    "❌ ใช้งานได้เฉพาะใน Server",
                    ephemeral=True
                )

                return

            guild_data = get_guild_data(
                interaction.guild.id
            )

            if not guild_data.get(
                "report_channel_id"
            ):

                await interaction.response.send_message(
                    "❌ ระบบ Report ยังไม่ได้ตั้งค่า",
                    ephemeral=True
                )

                return

            await interaction.response.send_modal(
                ReportModal()
            )

        except Exception:

            log.exception(
                "Report button error"
            )

            await safe_error_reply(
                interaction,
                "❌ เกิดข้อผิดพลาด"
            )


# =========================================================
# REPORT ADMIN VIEW
# =========================================================

class ReportAdminView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    # =====================================================
    # ACCEPT
    # =====================================================

    @discord.ui.button(
        label="ตอบรับปัญหา",
        emoji="🟡",
        style=discord.ButtonStyle.primary,
        custom_id="levelingx:accept"
    )
    async def accept_report(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        try:

            if not interaction.guild:

                return

            if not is_report_admin(
                interaction
            ):

                await interaction.response.send_message(
                    "⛔ คุณไม่ได้รับอนุญาตให้ตอบรับปัญหา",
                    ephemeral=True
                )

                return

            if not interaction.message.embeds:

                await interaction.response.send_message(
                    "❌ ไม่พบข้อมูล Report",
                    ephemeral=True
                )

                return

            embed = interaction.message.embeds[0]

            # -----------------------------------------
            # ป้องกันรับซ้ำ
            # -----------------------------------------

            for field in embed.fields:

                if field.name == "🛠️ ผู้ตอบรับปัญหา":

                    await interaction.response.send_message(
                        "⚠️ Report นี้มีคนตอบรับไปแล้ว",
                        ephemeral=True
                    )

                    return

            # -----------------------------------------
            # เปลี่ยนสี
            # -----------------------------------------

            embed.color = discord.Color.gold()

            # -----------------------------------------
            # เพิ่มผู้ตอบรับ
            # -----------------------------------------

            embed.add_field(
                name="🛠️ ผู้ตอบรับปัญหา",
                value=interaction.user.mention,
                inline=False
            )

            embed.set_footer(
                text=(
                    "สถานะ: กำลังดำเนินการ • "
                    "Developer : LevelingX"
                )
            )

            # -----------------------------------------
            # ปิดปุ่มรับเรื่อง
            # -----------------------------------------

            button.disabled = True

            await interaction.message.edit(
                embed=embed,
                view=self
            )

            # -----------------------------------------
            # หาเจ้าของ Report
            # -----------------------------------------

            reporter = None

            for field in embed.fields:

                if field.name == "👤 ผู้ส่งรายงาน":

                    text = field.value

                    # Discord Mention
                    # รูปแบบ <@123456>
                    if "<@" in text:

                        try:

                            user_id = int(
                                text
                                .replace("<@", "")
                                .replace(">", "")
                                .replace("!", "")
                            )

                            reporter = await bot.fetch_user(
                                user_id
                            )

                        except Exception:

                            reporter = None

                    break

            # -----------------------------------------
            # DM
            # -----------------------------------------

            dm_embed = discord.Embed(
                title="🟡 ทีมงานตอบรับปัญหาของคุณแล้ว",
                description=(
                    "ทีมงานได้รับเรื่องของคุณแล้ว\n"
                    "และกำลังดำเนินการตรวจสอบปัญหา"
                ),
                color=discord.Color.gold(),
                timestamp=datetime.now(
                    timezone.utc
                )
            )

            dm_embed.add_field(
                name="📌 ปัญหา",
                value="ดูรายละเอียดจาก Report ที่คุณส่ง",
                inline=False
            )

            dm_embed.add_field(
                name="🛠️ ผู้ดูแล",
                value=interaction.user.mention,
                inline=False
            )

            dm_embed.set_footer(
                text="Developer : LevelingX"
            )

            # -----------------------------------------
            # DM ด้วยการอ่าน Mention
            # -----------------------------------------

            if reporter:

                await safe_dm(
                    reporter,
                    dm_embed
                )

            await interaction.response.send_message(
                f"🟡 {interaction.user.mention} "
                "ตอบรับปัญหาเรียบร้อยแล้ว",
                allowed_mentions=discord.AllowedMentions(
                    users=True
                )
            )

        except Exception:

            log.exception(
                "Accept report error"
            )

            await safe_error_reply(
                interaction,
                "❌ เกิดข้อผิดพลาดในการตอบรับ Report"
            )

    # =====================================================
    # CLOSE
    # =====================================================

    @discord.ui.button(
        label="ปิดเรื่อง",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="levelingx:close"
    )
    async def close_report(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        try:

            if not interaction.guild:

                return

            if not is_report_admin(
                interaction
            ):

                await interaction.response.send_message(
                    "⛔ คุณไม่ได้รับอนุญาตให้ปิด Report",
                    ephemeral=True
                )

                return

            if not interaction.message.embeds:

                await interaction.response.send_message(
                    "❌ ไม่พบข้อมูล Report",
                    ephemeral=True
                )

                return

            embed = interaction.message.embeds[0]

            # -----------------------------------------
            # เปลี่ยนสี
            # -----------------------------------------

            embed.color = discord.Color.green()

            embed.add_field(
                name="✅ ปิดเรื่องโดย",
                value=interaction.user.mention,
                inline=False
            )

            embed.set_footer(
                text=(
                    "สถานะ: ปิดเรื่องแล้ว • "
                    "Developer : LevelingX"
                )
            )

            # -----------------------------------------
            # ปิดทุกปุ่ม
            # -----------------------------------------

            for item in self.children:

                item.disabled = True

            await interaction.message.edit(
                embed=embed,
                view=self
            )

            # -----------------------------------------
            # หา Reporter
            # -----------------------------------------

            reporter = None

            for field in embed.fields:

                if field.name == "👤 ผู้ส่งรายงาน":

                    try:

                        text = field.value

                        user_id = int(
                            text
                            .replace("<@", "")
                            .replace(">", "")
                            .replace("!", "")
                        )

                        reporter = await bot.fetch_user(
                            user_id
                        )

                    except Exception:

                        reporter = None

                    break

            # -----------------------------------------
            # DM
            # -----------------------------------------

            if reporter:

                dm_embed = discord.Embed(
                    title="✅ ปัญหาของคุณถูกปิดเรื่องแล้ว",
                    description=(
                        "ทีมงานดำเนินการกับ Report "
                        "ของคุณเรียบร้อยแล้ว"
                    ),
                    color=discord.Color.green(),
                    timestamp=datetime.now(
                        timezone.utc
                    )
                )

                dm_embed.add_field(
                    name="🛠️ ผู้ปิดเรื่อง",
                    value=interaction.user.mention,
                    inline=False
                )

                dm_embed.set_footer(
                    text="Developer : LevelingX"
                )

                await safe_dm(
                    reporter,
                    dm_embed
                )

            await interaction.response.send_message(
                f"✅ ปิด Report โดย "
                f"{interaction.user.mention}",
                allowed_mentions=discord.AllowedMentions(
                    users=True
                )
            )

        except Exception:

            log.exception(
                "Close report error"
            )

            await safe_error_reply(
                interaction,
                "❌ เกิดข้อผิดพลาดในการปิด Report"
            )


# =========================================================
# /setup
# =========================================================

@bot.tree.command(
    name="setup",
    description="ส่งหน้าแจ้งปัญหาที่บันทึกไว้พร้อมปุ่ม"
)
@app_commands.guild_only()
async def setup(
    interaction: discord.Interaction
):

    try:

        if not is_owner(
            interaction
        ):

            await interaction.response.send_message(
                "⛔ คำสั่งนี้ใช้ได้เฉพาะหัวดิส",
                ephemeral=True
            )

            return

        guild_data = get_guild_data(
            interaction.guild.id
        )

        embed_data = guild_data[
            "embed"
        ]

        embed = discord.Embed(
            title=embed_data.get(
                "title"
            ) or None,
            description=embed_data.get(
                "description"
            ) or None,
            color=hex_to_color(
                embed_data.get(
                    "color",
                    "#5865F2"
                )
            )
        )

        if embed_data.get(
            "image"
        ):

            embed.set_image(
                url=embed_data["image"]
            )

        if embed_data.get(
            "footer"
        ):

            embed.set_footer(
                text=embed_data["footer"]
            )

        await interaction.channel.send(
            embed=embed,
            view=ReportPanelView()
        )

        await interaction.response.send_message(
            "✅ ส่งหน้า Report เรียบร้อยแล้ว",
            ephemeral=True
        )

    except discord.HTTPException:

        log.exception(
            "Setup error - Discord rejected the embed (invalid image URL or data too long?)"
        )

        await safe_error_reply(
            interaction,
            "❌ ไม่สามารถส่งหน้า Report ได้ "
            "อาจเป็นเพราะ URL รูปภาพไม่ถูกต้อง หรือข้อมูล Embed ที่บันทึกไว้ยาวเกินไป "
            "กรุณาใช้ `/embed create` เพื่อแก้ไขข้อมูลใหม่"
        )

    except Exception:

        log.exception(
            "Setup error"
        )

        await safe_error_reply(
            interaction,
            "❌ เกิดข้อผิดพลาดในการ Setup"
        )


# =========================================================
# /embed create
# =========================================================

embed_group = app_commands.Group(
    name="embed",
    description="สร้างและตกแต่ง Embed ของระบบ"
)


@embed_group.command(
    name="create",
    description="สร้างและบันทึก Embed สำหรับหน้า Report"
)
@app_commands.describe(
    title="หัวข้อ Embed ไม่จำเป็นต้องใส่",
    description="ข้อความ Embed ไม่จำเป็นต้องใส่",
    image="URL รูปภาพ ไม่จำเป็นต้องใส่",
    footer="ข้อความ Footer ไม่จำเป็นต้องใส่",
    color="สี Hex เช่น #5865F2 ไม่จำเป็นต้องใส่"
)
@app_commands.guild_only()
async def embed_create(
    interaction: discord.Interaction,
    title: str = "",
    description: str = "",
    image: str = "",
    footer: str = "",
    color: str = "#5865F2"
):

    try:

        if not is_owner(
            interaction
        ):

            await interaction.response.send_message(
                "⛔ คำสั่งนี้ใช้ได้เฉพาะหัวดิส",
                ephemeral=True
            )

            return

        guild_data = get_guild_data(
            interaction.guild.id
        )

        old = guild_data[
            "embed"
        ]

        # ถ้าไม่กรอก ให้ใช้ค่าที่เคยบันทึก
        merged_embed = {

            "title": (
                title
                if title
                else old.get(
                    "title",
                    ""
                )
            ),

            "description": (
                description
                if description
                else old.get(
                    "description",
                    ""
                )
            ),

            "image": (
                image
                if image
                else old.get(
                    "image",
                    ""
                )
            ),

            "footer": (
                footer
                if footer
                else old.get(
                    "footer",
                    ""
                )
            ),

            "color": (
                color
                if color
                else old.get(
                    "color",
                    "#5865F2"
                )
            )

        }

        # -----------------------------------------
        # ตรวจสอบ Discord Embed Limits ก่อนบันทึก
        # -----------------------------------------

        limit_errors = validate_embed_limits(
            title=merged_embed["title"],
            description=merged_embed["description"],
            footer=merged_embed["footer"],
            image=merged_embed["image"]
        )

        if limit_errors:

            error_list = "\n".join(
                f"• {err}" for err in limit_errors
            )

            await interaction.response.send_message(
                "❌ ไม่สามารถบันทึก Embed ได้ เนื่องจากเกินขีดจำกัดของ Discord:\n"
                f"{error_list}",
                ephemeral=True
            )

            return

        guild_data["embed"] = merged_embed

        await persist_data()

        # -----------------------------------------
        # Preview
        # -----------------------------------------

        embed = discord.Embed(
            title=guild_data["embed"][
                "title"
            ] or None,

            description=guild_data["embed"][
                "description"
            ] or None,

            color=hex_to_color(
                guild_data["embed"][
                    "color"
                ]
            )
        )

        if guild_data["embed"].get(
            "image"
        ):

            embed.set_image(
                url=guild_data["embed"][
                    "image"
                ]
            )

        if guild_data["embed"].get(
            "footer"
        ):

            embed.set_footer(
                text=guild_data["embed"][
                    "footer"
                ]
            )

        await interaction.response.send_message(
            content="✅ บันทึก Embed เรียบร้อยแล้ว\n"
                    "ตัวอย่าง Embed:",
            embed=embed,
            ephemeral=True
        )

    except discord.HTTPException:

        log.exception(
            "Embed create error - Discord rejected the embed"
        )

        await safe_error_reply(
            interaction,
            "❌ Discord ปฏิเสธ Embed นี้ (อาจเป็นเพราะ URL รูปภาพไม่ถูกต้อง "
            "หรือข้อมูลเกินขีดจำกัด) กรุณาลองแก้ไขข้อมูลแล้วลองใหม่"
        )

    except Exception:

        log.exception(
            "Embed create error"
        )

        await safe_error_reply(
            interaction,
            "❌ ไม่สามารถสร้าง Embed ได้"
        )


bot.tree.add_command(
    embed_group
)


# =========================================================
# /set channel
# =========================================================

set_group = app_commands.Group(
    name="set",
    description="ตั้งค่าระบบของบอท"
)


@set_group.command(
    name="channel",
    description="ตั้งห้องที่บอทจะส่งแบบฟอร์ม Report ให้แอดมิน"
)
@app_commands.describe(
    channel="เลือกห้องสำหรับรับ Report"
)
@app_commands.guild_only()
async def set_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):

    try:

        if not is_owner(
            interaction
        ):

            await interaction.response.send_message(
                "⛔ คำสั่งนี้ใช้ได้เฉพาะหัวดิส",
                ephemeral=True
            )

            return

        guild_data = get_guild_data(
            interaction.guild.id
        )

        guild_data[
            "report_channel_id"
        ] = channel.id

        await persist_data()

        await interaction.response.send_message(
            f"✅ ตั้งห้อง Report เป็น {channel.mention} แล้ว",
            ephemeral=True
        )

    except Exception:

        log.exception(
            "Set channel error"
        )

        await safe_error_reply(
            interaction,
            "❌ ตั้งค่าห้องไม่สำเร็จ"
        )


# =========================================================
# /set admin
# =========================================================

@set_group.command(
    name="admin",
    description="เพิ่มหรือนำสมาชิกออกจากรายชื่อผู้มีสิทธิ์ตอบรับ Report"
)
@app_commands.describe(
    user="เลือกสมาชิกที่จะให้สิทธิ์ตอบรับ Report"
)
@app_commands.guild_only()
async def set_admin(
    interaction: discord.Interaction,
    user: discord.Member
):

    try:

        if not is_owner(
            interaction
        ):

            await interaction.response.send_message(
                "⛔ คำสั่งนี้ใช้ได้เฉพาะหัวดิส",
                ephemeral=True
            )

            return

        guild_data = get_guild_data(
            interaction.guild.id
        )

        admins = guild_data[
            "admin_users"
        ]

        if user.id in admins:

            admins.remove(
                user.id
            )

            status = "❌ นำออกจากรายชื่อแล้ว"

        else:

            admins.append(
                user.id
            )

            status = "✅ เพิ่มเข้าสู่รายชื่อแล้ว"

        await persist_data()

        await interaction.response.send_message(
            f"{status}\n"
            f"สมาชิก: {user.mention}",
            ephemeral=True
        )

    except Exception:

        log.exception(
            "Set admin error"
        )

        await safe_error_reply(
            interaction,
            "❌ ตั้งค่า Admin ไม่สำเร็จ"
        )


bot.tree.add_command(
    set_group
)


# =========================================================
# /ping
# =========================================================

@bot.tree.command(
    name="ping",
    description="แสดงค่าปิงของบอท"
)
async def ping(
    interaction: discord.Interaction
):

    latency = round(
        bot.latency * 1000
    )

    await interaction.response.send_message(
        f"{latency} ms"
    )


# =========================================================
# /help
# =========================================================

@bot.tree.command(
    name="help",
    description="แสดงรายการคำสั่งทั้งหมดของบอท"
)
async def help_command(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="📚 คำสั่งของ LevelingX",
        color=discord.Color.blurple()
    )

    embed.description = (
        "`/setup` — ส่งหน้า Report ที่บันทึกไว้\n"
        "`/embed create` — สร้าง/แก้ Embed\n"
        "`/set channel` — ตั้งห้องรับ Report\n"
        "`/set admin` — ตั้งคนที่ตอบรับ Report ได้\n"
        "`/ping` — ดูค่าปิง\n"
        "`/help` — ดูรายการคำสั่ง\n"
        "`/botinfo` — ดูข้อมูลบอทและเครื่อง\n"
        "`/botonline` — ตั้งห้องเสียงให้บอทอยู่ 24/7"
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /botinfo
# =========================================================

@bot.tree.command(
    name="botinfo",
    description="แสดงข้อมูลเกี่ยวกับบอทและระบบที่กำลังทำงาน"
)
async def botinfo(
    interaction: discord.Interaction
):

    try:

        if PSUTIL_AVAILABLE:

            try:

                memory = psutil.virtual_memory()

                cpu = psutil.cpu_percent(
                    interval=0.5
                )

                ram_used = (
                    memory.used
                    / 1024
                    / 1024
                )

                ram_total = (
                    memory.total
                    / 1024
                    / 1024
                )

            except Exception:

                log.exception(
                    "Failed to read psutil stats"
                )

                cpu = 0

                ram_used = 0

                ram_total = 0

        else:

            cpu = 0

            ram_used = 0

            ram_total = 0

        uptime = int(
            time.time()
            - START_TIME
        )

        embed = discord.Embed(
            title="🤖 LevelingX Bot Information",
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="👤 Developer",
            value="LevelingX",
            inline=False
        )

        embed.add_field(
            name="🤖 Bot",
            value=str(bot.user),
            inline=False
        )

        embed.add_field(
            name="🧩 discord.py",
            value=discord.__version__,
            inline=True
        )

        embed.add_field(
            name="🐍 Python",
            value=platform.python_version(),
            inline=True
        )

        embed.add_field(
            name="💻 OS",
            value=platform.system(),
            inline=True
        )

        embed.add_field(
            name="🏗️ Architecture",
            value=platform.machine(),
            inline=True
        )

        embed.add_field(
            name="⚙️ CPU",
            value=f"{cpu:.1f}%",
            inline=True
        )

        embed.add_field(
            name="🧠 RAM",
            value=(
                f"{ram_used:.0f} MB / "
                f"{ram_total:.0f} MB"
            ),
            inline=True
        )

        embed.add_field(
            name="⏱️ Uptime",
            value=format_uptime(
                uptime
            ),
            inline=False
        )

        embed.add_field(
            name="🌐 Server",
            value=(
                f"{len(bot.guilds)} Server"
            ),
            inline=True
        )

        embed.add_field(
            name="👥 ผู้ใช้ที่มองเห็น",
            value=str(
                sum(
                    guild.member_count or 0
                    for guild in bot.guilds
                )
            ),
            inline=True
        )

        embed.add_field(
            name="🎙️ Voice",
            value=(
                "Connected"
                if any(
                    guild.voice_client
                    for guild in bot.guilds
                )
                else "Not Connected"
            ),
            inline=True
        )

        embed.set_footer(
            text="Developer : LevelingX"
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    except Exception:

        log.exception(
            "Botinfo error"
        )

        await safe_error_reply(
            interaction,
            "❌ ไม่สามารถแสดงข้อมูลบอทได้"
        )


# =========================================================
# /botonline
# =========================================================

@bot.tree.command(
    name="botonline",
    description="ตั้งห้องเสียงที่ให้บอทเข้าไปออนไลน์ตลอดเวลา"
)
@app_commands.describe(
    channel="เลือกห้องเสียงที่ต้องการให้บอทเข้า"
)
@app_commands.guild_only()
async def botonline(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel
):

    try:

        if not is_owner(
            interaction
        ):

            await interaction.response.send_message(
                "⛔ คำสั่งนี้ใช้ได้เฉพาะหัวดิส",
                ephemeral=True
            )

            return

        guild_data = get_guild_data(
            interaction.guild.id
        )

        guild_data[
            "voice_channel_id"
        ] = channel.id

        await persist_data()

        voice_client = interaction.guild.voice_client

        if voice_client:

            if voice_client.channel.id != channel.id:

                await voice_client.move_to(
                    channel
                )

        else:

            await channel.connect(
                reconnect=True
            )

        await interaction.response.send_message(
            f"🎙️ บอทเข้า {channel.mention} แล้ว\n"
            "ระบบจะพยายามเชื่อมต่อกลับให้อัตโนมัติหากหลุด",
            ephemeral=True
        )

    except discord.Forbidden:

        await safe_error_reply(
            interaction,
            "❌ บอทไม่มีสิทธิ์เข้าหรือเชื่อมต่อห้องเสียงนี้"
        )

    except discord.ClientException:

        log.exception(
            "Bot online error - voice client issue"
        )

        await safe_error_reply(
            interaction,
            "❌ ไม่สามารถเชื่อมต่อห้องเสียงได้ในขณะนี้ "
            "(บอทอาจกำลังเชื่อมต่ออยู่แล้ว หรือ PyNaCl ยังไม่ได้ติดตั้ง)"
        )

    except Exception:

        log.exception(
            "Bot online error"
        )

        await safe_error_reply(
            interaction,
            "❌ ไม่สามารถเข้าห้องเสียงได้"
        )


# =========================================================
# GLOBAL ERROR HANDLER
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    log.error(
        "Slash command error: %s",
        error
    )

    try:

        message = (
            "❌ เกิดข้อผิดพลาดในการใช้คำสั่ง\n"
            "กรุณาลองใหม่อีกครั้ง"
        )

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True
            )

    except Exception:

        log.exception(
            "Failed to send error message"
        )


# =========================================================
# UNHANDLED ERRORS
# =========================================================

@bot.event
async def on_error(
    event,
    *args,
    **kwargs
):

    log.exception(
        "Discord event error: %s",
        event
    )


# =========================================================
# PRIVATE BOT SECURITY
# =========================================================

@bot.event
async def on_guild_join(
    guild: discord.Guild
):

    # ป้องกันกรณี GUILD_ID เป็น 0/ไม่ได้ตั้งค่า ไม่ให้บอทออกจากทุก Server
    # (ในทางปฏิบัติจะไม่เกิดขึ้น เพราะ validate_startup_env() บล็อกการรันไว้แล้ว
    # แต่ใส่ไว้เป็นการป้องกันซ้อนอีกชั้น)
    if not GUILD_ID:

        log.warning(
            "GUILD_ID is not configured - skipping guild restriction check"
        )

        return

    if guild.id != GUILD_ID:

        log.warning(
            "Unauthorized guild detected: %s",
            guild.id
        )

        try:

            await guild.leave()

        except Exception:

            log.exception(
                "Failed to leave unauthorized guild"
            )


# =========================================================
# LOGIN RATE-LIMIT COOLDOWN (บันทึกลง data.json - รอดข้าม Restart)
# =========================================================
# ถ้าโดน Discord Global Rate Limit (429) ตอน Login/Connect จะบันทึก
# เวลาที่ "ห้าม Login ก่อนถึง" ไว้ในนี้ เพื่อให้ถึงแม้ Render จะสั่ง
# Restart Process ใหม่ทันที (เช่น Process ก่อนหน้า Exit เพราะโดน 429)
# Process รอบใหม่ก็จะยังจำได้ว่าต้องรอเวลาต่อจากเดิม ไม่ใช่พยายาม
# Login ทันทีซ้ำอีกครั้งจนซ้ำเติม Rate Limit ให้หนักขึ้นไปอีก
# =========================================================

RATE_LIMIT_COOLDOWN_KEY = "_login_rate_limit_until"


def get_login_cooldown_remaining():
    """คืนจำนวนวินาทีที่ยังต้องรอก่อน Login ได้ (0 ถ้าไม่ต้องรอแล้ว)"""

    until = data.get(
        RATE_LIMIT_COOLDOWN_KEY
    )

    if not until:

        return 0

    try:

        remaining = float(until) - time.time()

    except (TypeError, ValueError):

        return 0

    return max(0.0, remaining)


def set_login_cooldown(retry_after_seconds):
    """บันทึกเวลาที่ห้าม Login ก่อนถึง ลง data.json (กันข้าม Restart)"""

    data[RATE_LIMIT_COOLDOWN_KEY] = time.time() + float(retry_after_seconds)

    save_data(data)


def clear_login_cooldown():
    """เคลียร์ Cooldown เก่า (เรียกหลัง Login/Connect สำเร็จใน on_ready)"""

    if RATE_LIMIT_COOLDOWN_KEY in data:

        del data[RATE_LIMIT_COOLDOWN_KEY]

        save_data(data)


def extract_retry_after(error):
    """ดึงค่า retry_after (วินาที) จาก discord.HTTPException ถ้ามี"""

    retry_after = getattr(
        error,
        "retry_after",
        None
    )

    if retry_after:

        try:

            return float(retry_after)

        except (TypeError, ValueError):

            pass

    response = getattr(
        error,
        "response",
        None
    )

    header_value = None

    if response is not None and getattr(
        response,
        "headers",
        None
    ):

        header_value = response.headers.get(
            "Retry-After"
        )

    if header_value:

        try:

            return float(header_value)

        except (TypeError, ValueError):

            return None

    return None


# =========================================================
# MAIN
# =========================================================


def main():

    # ---------------------------------------------
    # ตรวจสอบ Environment Variables ก่อนเริ่มทำงานใด ๆ
    # ---------------------------------------------

    validate_startup_env()

    # ---------------------------------------------
    # Start Web Server (เริ่มก่อนเสมอ แยก Thread)
    # ---------------------------------------------
    # /health ต้องตอบได้ทันที ไม่ว่า Discord Login จะสำเร็จ ล่าช้าจาก
    # การรอ Cooldown หรือ Fail ก็ตาม เพื่อไม่ให้ Render มองว่า Service
    # ค้าง/ไม่ Healthy ระหว่างที่บอทกำลังรอหรือพยายามเชื่อมต่อ Discord
    # ---------------------------------------------

    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True
    )

    web_thread.start()

    # ---------------------------------------------
    # เคารพ Cooldown จากการโดน 429 ครั้งก่อน (ถ้ามี) ก่อน Login
    # ---------------------------------------------
    # ค่านี้ถูกบันทึกไว้ใน data.json (ดูฟังก์ชัน set_login_cooldown)
    # เพื่อให้ "รอด" ข้าม Restart ของ Render - ถึงแม้ Render จะสั่ง
    # Restart Process ใหม่ทันทีหลังจาก Process ก่อนหน้าโดน 429 และ
    # หยุดตัวเอง โค้ดในรอบใหม่นี้ก็จะยังคงเช็คและรอเวลาที่เหลือต่อ
    # ไม่ใช่พยายาม Login ทันทีซ้ำอีกครั้งจนโดน Rate Limit หนักขึ้นไปอีก
    # ---------------------------------------------

    remaining = get_login_cooldown_remaining()

    if remaining > 0:

        log.error(
            "ยังอยู่ในช่วง Cooldown จากการโดน Discord Global Rate Limit "
            "(429) ครั้งก่อน - จะรออีก %.1f วินาที ก่อนพยายาม Login "
            "(ค่านี้ถูกบันทึกไว้ข้าม Restart เพื่อไม่ให้ยิง Discord API "
            "ซ้ำก่อนเวลาที่ Discord กำหนด)",
            remaining
        )

        time.sleep(remaining)

    # ---------------------------------------------
    # Start Discord Bot - เรียก bot.run() เพียงครั้งเดียว
    # ---------------------------------------------
    # ไม่มี while True + retry loop ที่ทำเอง: Render เป็นผู้จัดการ
    # การ Restart Service อยู่แล้ว (ทั้งกรณีปิดปกติและ Error) ส่วนการ
    # หลุดของ Gateway ระหว่างที่บอทกำลังทำงานอยู่ (network สะดุด,
    # Discord ล่มชั่วคราว ฯลฯ) discord.py จะจัดการ Reconnect ให้เอง
    # โดยอัตโนมัติภายใน bot.run() (พฤติกรรม Default อยู่แล้ว) โดยไม่
    # ต้องเขียน Loop restart ซ้อนขึ้นมาอีกชั้น ซึ่งจะยิ่งเสี่ยงยิง
    # Discord API ซ้ำเกินจำเป็นเวลาเกิดปัญหาต่อเนื่อง
    #
    # กรณีที่ควรหยุด Process ทันที (ไม่ Retry เอง) เพราะเป็นปัญหาที่
    # Retry ไปก็ไม่มีทางหาย หรือเป็นปัญหาระดับ Rate Limit ที่ต้องรอ
    # เวลาจริง ๆ ก่อน:
    #   - Token ผิด (LoginFailure)
    #   - ขาด Privileged Intents ที่ตั้งค่าใน Developer Portal
    #   - โดน 429 Global Rate Limit ตอน Login/Connect
    #   - Error อื่น ๆ ที่ไม่คาดคิดตอน Startup/Runtime
    # ทุกกรณีข้างต้นจะ Log สาเหตุให้ชัดเจนแล้วจบ Process ด้วย
    # SystemExit(1) ปล่อยให้ Render เป็นผู้ตัดสินใจ Restart ตามปกติ
    # ---------------------------------------------

    command_sync_mode = os.getenv(
        "COMMAND_SYNC",
        "0"
    ).strip() == "1"

    try:

        if command_sync_mode:

            log.info(
                "COMMAND_SYNC=1 - กำลังเข้าสู่โหมด Sync Slash Commands "
                "ครั้งเดียว (จะปิด Bot ทันทีหลัง Sync เสร็จ ไม่ทำงานเป็น "
                "บอทตามปกติในรอบนี้)"
            )

        else:

            log.info(
                "Starting Discord Bot..."
            )

        bot.run(
            TOKEN,
            log_handler=None
        )

        if command_sync_mode:

            if getattr(bot, "_command_sync_ok", False):

                log.info(
                    "Sync เสร็จสมบูรณ์ - กรุณาตั้งค่า COMMAND_SYNC=0 กลับ "
                    "แล้ว Deploy/Restart อีกครั้งเพื่อให้บอทกลับมาทำงาน "
                    "ตามปกติ"
                )

                raise SystemExit(0)

            else:

                log.error(
                    "Sync ไม่สำเร็จ (ดู Log ด้านบนสำหรับสาเหตุ) - จะไม่ "
                    "Retry เอง กรุณาตรวจสอบแล้วลองใหม่"
                )

                raise SystemExit(1)

        log.info(
            "Bot has shut down cleanly."
        )

    except discord.LoginFailure:

        log.error(
            "DISCORD_TOKEN ไม่ถูกต้อง กรุณาตรวจสอบค่าใน Environment "
            "Variables ให้ถูกต้อง (จะไม่ Retry เพราะปัญหานี้ Retry ไป "
            "ก็ไม่มีทางหาย)"
        )

        raise SystemExit(1)

    except discord.PrivilegedIntentsRequired:

        log.error(
            "บอทต้องเปิดใช้งาน Privileged Intents (Server Members Intent) "
            "ในหน้า Discord Developer Portal ก่อนถึงจะรันได้ (จะไม่ Retry "
            "เพราะปัญหานี้ Retry ไปก็ไม่มีทางหาย)"
        )

        raise SystemExit(1)

    except discord.HTTPException as error:

        status = getattr(
            error,
            "status",
            None
        )

        if status == 429:

            retry_after = extract_retry_after(error)

            if not retry_after or retry_after <= 0:

                retry_after = 60.0

            set_login_cooldown(retry_after)

            log.error(
                "โดน Discord API 429 Too Many Requests (Global Rate Limit) "
                "ตอน Login/Connect - บันทึก Cooldown %.1f วินาที ลง "
                "data.json แล้วหยุด Process อย่างปลอดภัย (จะไม่พยายาม "
                "Login ซ้ำใน Process นี้อีก) หาก Render Restart Service "
                "ให้ใหม่ รอบถัดไปจะเช็ค Cooldown นี้ก่อนเสมอ และจะยังไม่ "
                "Login จนกว่าจะครบเวลาที่ Discord กำหนด - โค้ดนี้ป้องกัน "
                "ได้แค่ไม่ให้ยิงซ้ำเพิ่ม แต่ลบ Rate Limit ที่ Discord "
                "บังคับอยู่ไม่ได้ ต้องรอให้ครบเวลาก่อนถึงจะ Login ได้จริง",
                retry_after
            )

        else:

            log.exception(
                "Discord HTTPException (status: %s) ตอน Startup - หยุด "
                "Process อย่างปลอดภัย ให้ Render เป็นผู้จัดการ Restart",
                status
            )

        raise SystemExit(1)

    except KeyboardInterrupt:

        pass

    except Exception:

        log.exception(
            "Bot crashed unexpectedly ตอน Startup/Runtime - หยุด Process "
            "อย่างปลอดภัย ให้ Render เป็นผู้จัดการ Restart เอง (ไม่ทำ "
            "Retry Loop เองในโค้ดเพื่อป้องกันการยิง Discord API ซ้ำถี่ "
            "เกินความจำเป็น)"
        )

        raise SystemExit(1)


if __name__ == "__main__":

    main()
