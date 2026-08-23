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
# INTERACTION DEFER HELPER
# =========================================================

async def safe_defer(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = False
):
    """
    Defer Interaction safely when an operation may take longer than Discord's
    initial response window. Returns True when a defer was performed.
    If the interaction was already acknowledged, it simply returns False.
    """

    try:

        if interaction.response.is_done():

            return False

        await interaction.response.defer(
            ephemeral=ephemeral
        )

        return True

    except Exception:

        log.exception(
            "Failed to defer interaction"
        )

        return False


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

        super().__init__(
            command_prefix="!",
            intents=intents
        )

        self.bot_start_time = time.time()

    async def setup_hook(
        self
    ):

        # ---------------------------------------------
        # Persistent Views
        # ---------------------------------------------

        self.add_view(
            ReportPanelView()
        )

        self.add_view(
            ReportAdminView()
        )

        # ---------------------------------------------
        # Sync เฉพาะ Server ส่วนตัว
        # ---------------------------------------------

        if GUILD_ID:

            guild = discord.Object(
                id=GUILD_ID
            )

            self.tree.copy_global_to(
                guild=guild
            )

            await self.tree.sync(
                guild=guild
            )

            log.info(
                "Commands synced to guild %s",
                GUILD_ID
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
                    "ไม่สามารถใช้ระบบนี้ใน DM ได้",
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
                    "ยังไม่ได้ตั้งห้องรับ Report\n"
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
            # Defer ก่อนทำงานที่อาจใช้เวลาเกิน 3 วินาที
            # -----------------------------------------

            await safe_defer(
                interaction,
                ephemeral=True
            )

            # -----------------------------------------
            # Embed
            # -----------------------------------------

            embed = discord.Embed(
                title="มีการแจ้งปัญหาใหม่เข้ามาค่ะ",
                color=discord.Color.red(),
                timestamp=datetime.now(
                    timezone.utc
                )
            )

            # ไม่ใส่ User ID
            embed.add_field(
                name="ผู้ส่งรายงาน",
                value=interaction.user.mention,
                inline=False
            )

            embed.add_field(
                name="ชื่อในฟอร์ม",
                value=truncate_field_value(
                    self.form_name.value
                ),
                inline=False
            )

            embed.add_field(
                name="ปัญหา",
                value=truncate_field_value(
                    self.problem.value
                ),
                inline=False
            )

            embed.add_field(
                name="เหตุผล / รายละเอียด",
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
                "มี Report ใหม่เข้ามา!\n"
                + " ".join(admin_mentions)
                if admin_mentions
                else "มี Report ใหม่เข้ามา!"
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

            await interaction.edit_original_response(
                content=(
                    "ส่งรายงานเรียบร้อยแล้ว\n"
                    "เมื่อทีมงานตอบรับปัญหา "
                    "บอทจะแจ้งเตือนคุณทาง DM"
                )
            )

        except Exception:

            log.exception(
                "Report submit error"
            )

            await safe_error_reply(
                interaction,
                "เกิดข้อผิดพลาดในการส่ง Report"
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
                    "ใช้งานได้เฉพาะใน Server",
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
                    "ระบบ Report ยังไม่ได้ตั้งค่า",
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
                "เกิดข้อผิดพลาด"
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
                    "คุณไม่ได้รับอนุญาตให้ตอบรับปัญหา",
                    ephemeral=True
                )

                return

            if not interaction.message.embeds:

                await interaction.response.send_message(
                    "ไม่พบข้อมูล Report",
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
                        "Report นี้มีคนตอบรับไปแล้ว",
                        ephemeral=True
                    )

                    return

            # -----------------------------------------
            # Defer ก่อนแก้ไข Message / ส่ง DM
            # -----------------------------------------

            await safe_defer(
                interaction,
                ephemeral=False
            )

            # -----------------------------------------
            # เปลี่ยนสี
            # -----------------------------------------

            embed.color = discord.Color.gold()

            # -----------------------------------------
            # เพิ่มผู้ตอบรับ
            # -----------------------------------------

            embed.add_field(
                name="ผู้ตอบรับปัญหา",
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

                if field.name == "ผู้ส่งรายงาน":

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
                title="ทีมงานตอบรับปัญหาของคุณแล้ว",
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
                name="ปัญหา",
                value="ดูรายละเอียดจาก Report ที่คุณส่ง",
                inline=False
            )

            dm_embed.add_field(
                name="ผู้ดูแล",
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

            await interaction.edit_original_response(
                content=(
                    f"{interaction.user.mention} "
                    "ตอบรับปัญหาเรียบร้อยแล้ว"
                ),
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
                "เกิดข้อผิดพลาดในการตอบรับ Report"
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
                    "คุณไม่ได้รับอนุญาตให้ปิด Report",
                    ephemeral=True
                )

                return

            if not interaction.message.embeds:

                await interaction.response.send_message(
                    "ไม่พบข้อมูล Report",
                    ephemeral=True
                )

                return

            embed = interaction.message.embeds[0]

            # -----------------------------------------
            # Defer ก่อนแก้ไข Message / ส่ง DM
            # -----------------------------------------

            await safe_defer(
                interaction,
                ephemeral=False
            )

            # -----------------------------------------
            # เปลี่ยนสี
            # -----------------------------------------

            embed.color = discord.Color.green()

            embed.add_field(
                name="ปิดเรื่องโดย",
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

                if field.name == "ผู้ส่งรายงาน":

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
                    title="ปัญหาของคุณถูกปิดเรื่องแล้ว",
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
                    name="ผู้ปิดเรื่อง",
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

            await interaction.edit_original_response(
                content=(
                    f"ปิด Report โดย "
                    f"{interaction.user.mention}"
                ),
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
                "เกิดข้อผิดพลาดในการปิด Report"
            )


# =========================================================
# /setup
# =========================================================

@bot.tree.command(
    name="setup",
    description="เริ่มระบบแจ้งปัญหา"
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
                "คำสั่งนี้ใช้ได้เฉพาะหัวดิส",
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

        await safe_defer(
            interaction,
            ephemeral=True
        )

        await interaction.channel.send(
            embed=embed,
            view=ReportPanelView()
        )

        await interaction.edit_original_response(
            content="ส่งหน้า Report เรียบร้อยแล้ว"
        )

    except discord.HTTPException:

        log.exception(
            "Setup error - Discord rejected the embed (invalid image URL or data too long?)"
        )

        await safe_error_reply(
            interaction,
            "ไม่สามารถส่งหน้า Report ได้ "
            "อาจเป็นเพราะ URL รูปภาพไม่ถูกต้อง หรือข้อมูล Embed ที่บันทึกไว้ยาวเกินไป "
            "กรุณาใช้ `/embed create` เพื่อแก้ไขข้อมูลใหม่"
        )

    except Exception:

        log.exception(
            "Setup error"
        )

        await safe_error_reply(
            interaction,
            "เกิดข้อผิดพลาดในการ Setup"
        )


# =========================================================
# /embed create
# =========================================================

embed_group = app_commands.Group(
    name="embed",
    description="สร้าง embed"
)


@embed_group.command(
    name="create",
    description="สร้างและบันทึก Embed สำหรับหน้า Report"
)
@app_commands.describe(
    title="หัวข้อ Embed ใส่ก็ได้ไม่ใส่ก็ได้่",
    description="ข้อความ Embed ใส่ก็ได้ไม่ใส่ก็ได้",
    image="URL รูปภาพ ใส่ก็ได้ ไม่ใส่ก็ได้",
    footer="ข้อความ Footer ใส่ก็ได้ ไม่ใส่ก็ได้",
    color="สี Hex เช่น #5865F2 ใส่ก็ได้ไม่ใส่ก็ได้่"
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
                "คำสั่งนี้ใช้ได้เฉพาะหัวดิส",
                ephemeral=True
            )

            return

        await safe_defer(
            interaction,
            ephemeral=True
        )

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
                "ไม่สามารถบันทึก Embed ได้ เนื่องจากเกินขีดจำกัดของ Discord:\n"
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

        await interaction.edit_original_response(
            content="บันทึก Embed เรียบร้อยแล้ว\n"
                    "ตัวอย่าง Embed:",
            embed=embed
        )

    except discord.HTTPException:

        log.exception(
            "Embed create error - Discord rejected the embed"
        )

        await safe_error_reply(
            interaction,
            "Discord ปฏิเสธ Embed นี้ (อาจเป็นเพราะ URL รูปภาพไม่ถูกต้อง "
            "หรือข้อมูลเกินขีดจำกัด) กรุณาลองแก้ไขข้อมูลแล้วลองใหม่"
        )

    except Exception:

        log.exception(
            "Embed create error"
        )

        await safe_error_reply(
            interaction,
            "ไม่สามารถสร้าง Embed ได้"
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
                "คำสั่งนี้ใช้ได้เฉพาะหัวดิส",
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
            f"ตั้งห้อง Report เป็น {channel.mention} แล้ว",
            ephemeral=True
        )

    except Exception:

        log.exception(
            "Set channel error"
        )

        await safe_error_reply(
            interaction,
            "ตั้งค่าห้องไม่สำเร็จ"
        )


# =========================================================
# /set admin
# =========================================================

@set_group.command(
    name="admin",
    description="เพิ่มหรือลบ คนที่สามารถตอบปัญหาได้"
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
                "คำสั่งนี้ใช้ได้เฉพาะหัวดิส",
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

            status = "นำออกจากรายชื่อแล้ว"

        else:

            admins.append(
                user.id
            )

            status = "เพิ่มเข้าสู่รายชื่อแล้ว"

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
            "ตั้งค่า Admin ไม่สำเร็จ"
        )


bot.tree.add_command(
    set_group
)


# =========================================================
# /ping
# =========================================================

@bot.tree.command(
    name="ping",
    description="แสดงค่าปิงบอท"
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
    description="ดูคำสั่งทั้งหมด"
)
async def help_command(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="คำสั่งทั้งหมด",
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
    description="แสดงข้อมูลบอท"
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
            title="bot info",
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="Developer",
            value="LevelingX",
            inline=False
        )

        embed.add_field(
            name="Bot",
            value=str(bot.user),
            inline=False
        )

        embed.add_field(
            name="discord.py",
            value=discord.__version__,
            inline=True
        )

        embed.add_field(
            name="Python",
            value=platform.python_version(),
            inline=True
        )

        embed.add_field(
            name="OS",
            value=platform.system(),
            inline=True
        )

        embed.add_field(
            name="Architecture",
            value=platform.machine(),
            inline=True
        )

        embed.add_field(
            name="CPU",
            value=f"{cpu:.1f}%",
            inline=True
        )

        embed.add_field(
            name="RAM",
            value=(
                f"{ram_used:.0f} MB / "
                f"{ram_total:.0f} MB"
            ),
            inline=True
        )

        embed.add_field(
            name="Uptime",
            value=format_uptime(
                uptime
            ),
            inline=False
        )

        embed.add_field(
            name="Server",
            value=(
                f"{len(bot.guilds)} Server"
            ),
            inline=True
        )

        embed.add_field(
            name="ผู้ใช้ที่มองเห็น",
            value=str(
                sum(
                    guild.member_count or 0
                    for guild in bot.guilds
                )
            ),
            inline=True
        )

        embed.add_field(
            name="Voice",
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
            "ไม่สามารถแสดงข้อมูลบอทได้"
        )


# =========================================================
# /botonline
# =========================================================

@bot.tree.command(
    name="botonline",
    description="ออนช่องเสียง 24/7"
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
                "คำสั่งนี้ใช้ได้เฉพาะหัวดิส",
                ephemeral=True
            )

            return

        await safe_defer(
            interaction,
            ephemeral=True
        )

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

        await interaction.edit_original_response(
            content=(
                f"บอทเข้า {channel.mention} แล้ว\n"
                "ระบบจะพยายามเชื่อมต่อกลับให้อัตโนมัติหากหลุด"
            )
        )

    except discord.Forbidden:

        await safe_error_reply(
            interaction,
            "บอทไม่มีสิทธิ์เข้าหรือเชื่อมต่อห้องเสียงนี้"
        )

    except discord.ClientException:

        log.exception(
            "Bot online error - voice client issue"
        )

        await safe_error_reply(
            interaction,
            "ไม่สามารถเชื่อมต่อห้องเสียงได้ในขณะนี้ "
            "บอทอยู่ในห้องเสียงอยู่แล้ว หรือ บอทบัค ให้แจ้งผู้สร้างบอท"
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
            "เกิดข้อผิดพลาดในการใช้คำสั่ง\n"
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

    log.error(
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
# MAIN
# =========================================================

def main():

    # ---------------------------------------------
    # ตรวจสอบ Environment Variables ก่อนเริ่มทำงานใด ๆ
    # ถ้าค่าที่จำเป็น (เช่น DISCORD_TOKEN, GUILD_ID) ขาดหรือผิด
    # จะหยุดโปรแกรมทันทีพร้อม log สาเหตุ ไม่ปล่อยให้บอทรันแบบพัง
    # ---------------------------------------------

    validate_startup_env()

    # ---------------------------------------------
    # Start Web Server
    # ---------------------------------------------

    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True
    )

    web_thread.start()

    # ---------------------------------------------
    # Start Discord Bot
    # ---------------------------------------------
    # หลุดการเชื่อมต่อ (network, Discord API ล่ม ฯลฯ) -> reconnect อัตโนมัติ
    # แต่ถ้า Token ผิด หรือ Privileged Intents ไม่ได้เปิดใน Developer Portal
    # ถือเป็นปัญหาการตั้งค่า ไม่ใช่ปัญหาเครือข่ายชั่วคราว จึงไม่วน retry
    # ทุก 10 วินาทีแบบไม่มีที่สิ้นสุด ให้หยุดและ log สาเหตุให้ชัดเจนแทน
    # ---------------------------------------------

    while True:

        try:

            log.info(
                "Starting Discord Bot..."
            )

            bot.run(
                TOKEN,
                log_handler=None
            )

            # bot.run() คืนค่าปกติเมื่อบอทถูกปิดแบบตั้งใจ (เช่น bot.close())
            # ไม่ควร restart วนอีก
            log.info(
                "Bot has shut down cleanly."
            )

            break

        except discord.LoginFailure:

            log.error(
                "DISCORD_TOKEN ไม่ถูกต้อง กรุณาตรวจสอบค่าใน Environment "
                "Variables ให้ถูกต้อง (บอทจะไม่พยายามเชื่อมต่อใหม่)"
            )

            break

        except discord.PrivilegedIntentsRequired:

            log.error(
                "บอทต้องเปิดใช้งาน Privileged Intents (เช่น Server Members "
                "Intent) ในหน้า Discord Developer Portal ก่อนถึงจะรันได้ "
                "(บอทจะไม่พยายามเชื่อมต่อใหม่)"
            )

            break

        except KeyboardInterrupt:

            break

        except Exception:

            log.exception(
                "Bot crashed unexpectedly. Restarting in 10 seconds..."
            )

            time.sleep(10)


if __name__ == "__main__":

    main()
