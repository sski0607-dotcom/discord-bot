import os
import re
import json
import random
import threading
import asyncio
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict
from flask import Flask
import discord
from discord.ext import commands, tasks
from discord import app_commands

# --- 0. 백그라운드 웹 서버 (Render 슬립 방지) ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run)
    t.start()
# -----------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# 🚨 [중요] 경고 및 영구 DB 채널 ID
WARN_LOG_CHANNEL_ID = 1569705414839242884

# 📢 공지 확인 대상 채널 ID
ANNOUNCEMENT_CHANNEL_ID = 1559080610335817779

# 💤 시간 측정을 제외할 잠수방 음성 채널 ID
AFK_VOICE_CHANNEL_ID = 1569698118004514836

# 한국 표준시(KST)
KST = timezone(timedelta(hours=9))

# --- 통화방 시간 및 공지 확인 대상 역할 목록 ---
TARGET_ROLES = [
    "월영 길드원",
    "월영 수습생",
    "영월 길드원",
    "영월 수습생",
    "은월 길드원",
    "은월 수습생"
]

ALL_GUILD_ROLES = TARGET_ROLES + ["커뮤니티"]

def is_target_member(member: discord.Member) -> bool:
    """지정된 길드원/수습생 역할을 하나라도 가지고 있는지 확인"""
    return any(role.name in TARGET_ROLES for role in member.roles)

def is_afk_channel(channel: Optional[discord.VoiceChannel]) -> bool:
    """해당 음성 채널이 잠수방(지정 ID 또는 디스코드 기본 AFK 채널)인지 확인"""
    if not channel:
        return False
    if channel.id == AFK_VOICE_CHANNEL_ID:
        return True
    if channel.guild.afk_channel and channel.id == channel.guild.afk_channel.id:
        return True
    return False

# 서버별 설정 매핑 {guild_id: channel_id}
VOICE_NOTICE_CHANNELS: Dict[int, int] = {}
MEMBER_COUNTER_CHANNELS: Dict[int, int] = {}
WELCOME_CHANNELS: Dict[int, int] = {}
VERIFY_ADMIN_CHANNELS: Dict[int, int] = {}

# 통화방 세션 메모리 {user_id: {"active": bool, "start_time": datetime, "guild_id": int}}
VOICE_ACTIVE_SESSIONS: Dict[int, dict] = {}
VOICE_DAILY_CACHE: Dict[int, Dict[str, Dict[str, int]]] = {}
SERVER_AUDIT_FLAGS: Dict[int, Dict[str, str]] = {}

# 🏖️ 통화 예외/휴가 메모리 {guild_id: {user_id_str: {"until": "YYYY-MM-DD", "reason": str}}}
VOICE_EXEMPTIONS: Dict[int, Dict[str, dict]] = {}

def is_voice_exempt(guild_id: int, user_id: int) -> Optional[dict]:
    """해당 유저가 현재 날짜 기준 통화 출결 예외(휴가) 상태인지 확인"""
    guild_exempts = VOICE_EXEMPTIONS.get(guild_id, {})
    exempt_info = guild_exempts.get(str(user_id))
    if not exempt_info:
        return None
    try:
        until_date = datetime.strptime(exempt_info["until"], "%Y-%m-%d").date()
        today_date = datetime.now(KST).date()
        if today_date <= until_date:
            return exempt_info
    except Exception:
        pass
    return None

def is_admin_or_mod(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild or perms.moderate_members or perms.kick_members

def format_seconds(seconds: int) -> str:
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if hrs > 0:
        return f"{hrs}시간 {mins}분 {secs}초"
    elif mins > 0:
        return f"{mins}분 {secs}초"
    return f"{secs}초"

# --- 오류 발생 시 관리자 채널 로그 전송 ---
async def send_error_log(guild: Optional[discord.Guild], error_title: str, error_detail: str):
    if not guild or WARN_LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
        except Exception:
            return
    embed = discord.Embed(
        title=f"🚨 [시스템 오류 발생] {error_title}",
        description=f"```python\n{error_detail[:3900]}\n```",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(KST)
    )
    try:
        await channel.send(embed=embed)
    except Exception:
        pass

def add_chunked_fields(embed: discord.Embed, base_title: str, lines: List[str], max_chars: int = 900):
    if not lines:
        return
    current_chunk = []
    current_length = 0
    field_index = 1

    for line in lines:
        line_len = len(line) + 1
        if current_length + line_len > max_chars:
            title = base_title if field_index == 1 else f"{base_title} ({field_index})"
            embed.add_field(name=title, value="\n".join(current_chunk), inline=False)
            current_chunk = [line]
            current_length = line_len
            field_index += 1
        else:
            current_chunk.append(line)
            current_length += line_len

    if current_chunk:
        title = base_title if field_index == 1 else f"{base_title} ({field_index})"
        embed.add_field(name=title, value="\n".join(current_chunk), inline=False)

# --- 🖼️ 黑夜 테마 환영 임베드 ---
def create_welcome_embed(guild: discord.Guild) -> discord.Embed:
    description_text = (
        "**黑夜 | 달빛 아래에 오신 것을 환영합니다.**\n\n"
        "월영과 영월, 그리고 다양한 게임을 함께 즐기는 모두를 위한 공간입니다.\n\n"
        "길드 활동부터 자유로운 게임과 소통까지, 각자의 방식으로 편안하게 즐겨주세요.\n\n"
        "아래 인증 신청 버튼을 통해 원하시는 소속을 선택하실 수 있습니다.\n\n"
        "• **月影 월영** · 생활 길드\n"
        "• **影月 영월** · 전투 길드\n"
        "• **커뮤니티** · 종합게임 이용자\n\n"
        "**두 개의 길드, 그리고 하나의 커뮤니티.**"
    )
    embed = discord.Embed(
        title="黑夜 | 달 아래의 새로운 인연",
        description=description_text,
        color=0x800020
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="黑夜 · 月影 × 影月 | 두 개의 길드, 하나의 밤")
    return embed

# --- 채널 기반 영구 데이터 관리 ---
async def fetch_database_from_channel(guild: discord.Guild):
    if WARN_LOG_CHANNEL_ID == 0:
        return {}, {}
    
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
        except Exception:
            return {}, {}

    warnings = {}
    voice_records: Dict[str, Dict[str, int]] = {}
    exempts: Dict[str, dict] = {}

    try:
        raw_messages = []
        async for message in channel.history(limit=5000, oldest_first=False):
            if message.author.id == bot.user.id and message.embeds:
                raw_messages.append(message)

        raw_messages.sort(key=lambda m: m.created_at)

        for message in raw_messages:
            for embed in message.embeds:
                if not (embed.footer and embed.footer.text):
                    continue
                footer_text = embed.footer.text

                if footer_text.startswith("WARN_DATA:"):
                    try:
                        data = json.loads(footer_text.replace("WARN_DATA:", "").strip())
                        u_id = str(data["user_id"])
                        action = data.get("action", "add")
                        if action == "add":
                            if u_id not in warnings:
                                warnings[u_id] = []
                            warnings[u_id].append({"reason": data["reason"], "moderator": data["moderator"], "date": data["date"]})
                        elif action == "remove":
                            deduct = data.get("count", 1)
                            if u_id in warnings:
                                warnings[u_id] = warnings[u_id][:-deduct]
                        elif action == "clear":
                            if u_id in warnings:
                                warnings[u_id] = []
                    except Exception:
                        continue

                elif footer_text.startswith("VOICE_LOG:"):
                    try:
                        data = json.loads(footer_text.replace("VOICE_LOG:", "").strip())
                        d_str = data["date"]
                        u_id = str(data["user_id"])
                        dur = int(data.get("duration", 0))
                        if d_str not in voice_records:
                            voice_records[d_str] = {}
                        voice_records[d_str][u_id] = voice_records[d_str].get(u_id, 0) + dur
                    except Exception:
                        continue

                elif footer_text.startswith("VOICE_EXEMPT:"):
                    try:
                        data = json.loads(footer_text.replace("VOICE_EXEMPT:", "").strip())
                        u_id = str(data["user_id"])
                        action = data.get("action", "add")
                        if action == "add":
                            exempts[u_id] = {"until": data["until"], "reason": data["reason"]}
                        elif action == "remove":
                            if u_id in exempts:
                                del exempts[u_id]
                    except Exception:
                        continue

                elif footer_text.startswith("VOICE_CONFIG:"):
                    try:
                        cfg = json.loads(footer_text.replace("VOICE_CONFIG:", "").strip())
                        VOICE_NOTICE_CHANNELS[cfg.get("guild_id", guild.id)] = cfg.get("channel_id")
                    except Exception:
                        continue

                elif footer_text.startswith("COUNTER_CONFIG:"):
                    try:
                        cfg = json.loads(footer_text.replace("COUNTER_CONFIG:", "").strip())
                        MEMBER_COUNTER_CHANNELS[cfg.get("guild_id", guild.id)] = cfg.get("channel_id")
                    except Exception:
                        continue

                elif footer_text.startswith("WELCOME_CONFIG:"):
                    try:
                        cfg = json.loads(footer_text.replace("WELCOME_CONFIG:", "").strip())
                        WELCOME_CHANNELS[cfg.get("guild_id", guild.id)] = cfg.get("channel_id")
                    except Exception:
                        continue

                elif footer_text.startswith("VERIFY_ADMIN_CONFIG:"):
                    try:
                        cfg = json.loads(footer_text.replace("VERIFY_ADMIN_CONFIG:", "").strip())
                        VERIFY_ADMIN_CHANNELS[cfg.get("guild_id", guild.id)] = cfg.get("channel_id")
                    except Exception:
                        continue

                elif footer_text.startswith("AUDIT_FLAG:"):
                    try:
                        flag_data = json.loads(footer_text.replace("AUDIT_FLAG:", "").strip())
                        g_id = flag_data.get("guild_id", guild.id)
                        if g_id not in SERVER_AUDIT_FLAGS:
                            SERVER_AUDIT_FLAGS[g_id] = {}
                        SERVER_AUDIT_FLAGS[g_id][flag_data["type"]] = flag_data["date"]
                    except Exception:
                        continue

        VOICE_EXEMPTIONS[guild.id] = exempts

    except Exception as e:
        await send_error_log(guild, "fetch_database_from_channel", traceback.format_exc())

    return warnings, voice_records

async def log_warning_action(guild: discord.Guild, user_id: int, action: str, data: dict):
    if WARN_LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID) or await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title=f"🔒 [경고 DB 로그] {action.upper()}", color=discord.Color.dark_grey())
    embed.add_field(name="Target User ID", value=str(user_id), inline=True)
    embed.add_field(name="Action", value=action, inline=True)
    payload = {"user_id": user_id, "action": action, **data}
    embed.set_footer(text=f"WARN_DATA:{json.dumps(payload, ensure_ascii=False)}")
    await channel.send(embed=embed)

async def log_voice_exempt_action(guild: discord.Guild, user_id: int, action: str, data: dict):
    if WARN_LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID) or await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title=f"🏖️ [통화 예외 DB 로그] {action.upper()}", color=discord.Color.teal())
    embed.add_field(name="Target User ID", value=str(user_id), inline=True)
    embed.add_field(name="Action", value=action, inline=True)
    payload = {"user_id": user_id, "action": action, **data}
    embed.set_footer(text=f"VOICE_EXEMPT:{json.dumps(payload, ensure_ascii=False)}")
    await channel.send(embed=embed)

    if guild.id not in VOICE_EXEMPTIONS:
        VOICE_EXEMPTIONS[guild.id] = {}
    u_str = str(user_id)
    if action == "add":
        VOICE_EXEMPTIONS[guild.id][u_str] = {"until": data["until"], "reason": data["reason"]}
    elif action == "remove" and u_str in VOICE_EXEMPTIONS[guild.id]:
        del VOICE_EXEMPTIONS[guild.id][u_str]

async def log_voice_duration(guild: discord.Guild, user_id: int, date_str: str, duration_sec: int):
    if WARN_LOG_CHANNEL_ID == 0 or duration_sec <= 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID) or await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title="🎙️ [통화 DB 로그] RECORD", color=discord.Color.dark_blue())
    embed.add_field(name="User ID", value=str(user_id), inline=True)
    embed.add_field(name="Date", value=date_str, inline=True)
    embed.add_field(name="Duration", value=f"{duration_sec}초 ({duration_sec // 60}분)", inline=True)
    payload = {"date": date_str, "user_id": user_id, "duration": duration_sec}
    embed.set_footer(text=f"VOICE_LOG:{json.dumps(payload, ensure_ascii=False)}")
    await channel.send(embed=embed)

    if guild.id not in VOICE_DAILY_CACHE:
        VOICE_DAILY_CACHE[guild.id] = {}
    if date_str not in VOICE_DAILY_CACHE[guild.id]:
        VOICE_DAILY_CACHE[guild.id][date_str] = {}
    u_str = str(user_id)
    VOICE_DAILY_CACHE[guild.id][date_str][u_str] = VOICE_DAILY_CACHE[guild.id][date_str].get(u_str, 0) + duration_sec

async def log_voice_config(guild: discord.Guild, channel_id: int):
    if WARN_LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID) or await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title="🎙️ [통화 설정 DB 로그] NOTICE_CHANNEL", color=discord.Color.blue())
    payload = {"guild_id": guild.id, "channel_id": channel_id}
    embed.set_footer(text=f"VOICE_CONFIG:{json.dumps(payload, ensure_ascii=False)}")
    await channel.send(embed=embed)
    VOICE_NOTICE_CHANNELS[guild.id] = channel_id

async def log_counter_config(guild: discord.Guild, channel_id: int):
    if WARN_LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID) or await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title="👥 [인원 카운터 DB 로그] COUNTER_CHANNEL", color=discord.Color.dark_teal())
    payload = {"guild_id": guild.id, "channel_id": channel_id}
    embed.set_footer(text=f"COUNTER_CONFIG:{json.dumps(payload, ensure_ascii=False)}")
    await channel.send(embed=embed)
    MEMBER_COUNTER_CHANNELS[guild.id] = channel_id

async def log_welcome_config(guild: discord.Guild, channel_id: int):
    if WARN_LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID) or await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title="🌙 [환영 설정 DB 로그] WELCOME_CHANNEL", color=discord.Color.purple())
    payload = {"guild_id": guild.id, "channel_id": channel_id}
    embed.set_footer(text=f"WELCOME_CONFIG:{json.dumps(payload, ensure_ascii=False)}")
    await channel.send(embed=embed)
    WELCOME_CHANNELS[guild.id] = channel_id

async def log_verify_admin_config(guild: discord.Guild, channel_id: int):
    if WARN_LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID) or await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title="🛡️ [인증 관리 설정 DB 로그] VERIFY_ADMIN", color=discord.Color.dark_green())
    payload = {"guild_id": guild.id, "channel_id": channel_id}
    embed.set_footer(text=f"VERIFY_ADMIN_CONFIG:{json.dumps(payload, ensure_ascii=False)}")
    await channel.send(embed=embed)
    VERIFY_ADMIN_CHANNELS[guild.id] = channel_id

async def log_audit_flag(guild: discord.Guild, flag_type: str, date_str: str):
    if WARN_LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID) or await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title=f"📋 [정산 플래그 로그] {flag_type.upper()}", color=discord.Color.dark_purple())
    payload = {"guild_id": guild.id, "type": flag_type, "date": date_str}
    embed.set_footer(text=f"AUDIT_FLAG:{json.dumps(payload, ensure_ascii=False)}")
    await channel.send(embed=embed)

    if guild.id not in SERVER_AUDIT_FLAGS:
        SERVER_AUDIT_FLAGS[guild.id] = {}
    SERVER_AUDIT_FLAGS[guild.id][flag_type] = date_str

async def update_member_counter(guild: discord.Guild):
    channel_id = MEMBER_COUNTER_CHANNELS.get(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.VoiceChannel):
        return
    human_count = len([m for m in guild.members if not m.bot])
    new_name = f"₍₍👥⁾⁾┇ 전체인원: {human_count}명"
    if channel.name != new_name:
        try:
            await channel.edit(name=new_name)
        except (discord.Forbidden, discord.HTTPException):
            pass

async def update_voice_channel_members(channel: Optional[discord.VoiceChannel]):
    if not channel:
        return
    now = datetime.now(KST)
    if is_afk_channel(channel):
        for m in channel.members:
            if m.id in VOICE_ACTIVE_SESSIONS:
                sess = VOICE_ACTIVE_SESSIONS[m.id]
                if sess["active"] and sess["start_time"]:
                    duration = int((now - sess["start_time"]).total_seconds())
                    date_str = sess["start_time"].strftime("%Y-%m-%d")
                    await log_voice_duration(channel.guild, m.id, date_str, duration)
                sess["active"] = False
                sess["start_time"] = None
        return

    human_members = [m for m in channel.members if not m.bot]
    has_enough_members = len(human_members) >= 2

    for m in human_members:
        if not is_target_member(m):
            continue
        is_deaf = m.voice.self_deaf or m.voice.deaf if m.voice else False
        should_track = has_enough_members and not is_deaf
        sess = VOICE_ACTIVE_SESSIONS.get(m.id)

        if sess and sess["active"] and not should_track:
            if sess["start_time"]:
                duration = int((now - sess["start_time"]).total_seconds())
                date_str = sess["start_time"].strftime("%Y-%m-%d")
                await log_voice_duration(channel.guild, m.id, date_str, duration)
            sess["active"] = False
            sess["start_time"] = None
        elif should_track and (not sess or not sess["active"]):
            VOICE_ACTIVE_SESSIONS[m.id] = {
                "active": True,
                "start_time": now,
                "guild_id": channel.guild.id
            }

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    now = datetime.now(KST)
    if before.channel and not after.channel:
        if member.id in VOICE_ACTIVE_SESSIONS:
            sess = VOICE_ACTIVE_SESSIONS.pop(member.id)
            if sess["active"] and sess["start_time"]:
                duration = int((now - sess["start_time"]).total_seconds())
                date_str = sess["start_time"].strftime("%Y-%m-%d")
                await log_voice_duration(member.guild, member.id, date_str, duration)
        await update_voice_channel_members(before.channel)
        return

    if not before.channel and after.channel:
        await update_voice_channel_members(after.channel)
        return

    if before.channel and after.channel:
        if before.channel.id != after.channel.id:
            if member.id in VOICE_ACTIVE_SESSIONS:
                sess = VOICE_ACTIVE_SESSIONS.pop(member.id)
                if sess["active"] and sess["start_time"]:
                    duration = int((now - sess["start_time"]).total_seconds())
                    date_str = sess["start_time"].strftime("%Y-%m-%d")
                    await log_voice_duration(member.guild, member.id, date_str, duration)
            await update_voice_channel_members(before.channel)
            await update_voice_channel_members(after.channel)
        else:
            await update_voice_channel_members(after.channel)

@bot.event
async def on_member_join(member: discord.Member):
    await update_member_counter(member.guild)

    welcome_channel_id = WELCOME_CHANNELS.get(member.guild.id)
    channel = member.guild.get_channel(welcome_channel_id) if welcome_channel_id else member.guild.system_channel
    
    if not channel:
        for ch in member.guild.text_channels:
            if ch.permissions_for(member.guild.me).send_messages:
                channel = ch
                break

    if channel:
        try:
            embed = create_welcome_embed(member.guild)
            await channel.send(
                content=member.mention,
                embed=embed,
                view=ApplyView(),
                allowed_mentions=discord.AllowedMentions(users=True)
            )
        except Exception:
            pass

@bot.event
async def on_member_remove(member: discord.Member):
    await update_member_counter(member.guild)

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    was_target = is_target_member(before)
    is_target = is_target_member(after)

    if was_target and not is_target:
        if after.id in VOICE_ACTIVE_SESSIONS:
            sess = VOICE_ACTIVE_SESSIONS.pop(after.id)
            if sess["active"] and sess["start_time"]:
                now = datetime.now(KST)
                duration = int((now - sess["start_time"]).total_seconds())
                date_str = sess["start_time"].strftime("%Y-%m-%d")
                await log_voice_duration(after.guild, after.id, date_str, duration)

@tasks.loop(minutes=10)
async def update_member_counter_loop():
    for guild in bot.guilds:
        try:
            await update_member_counter(guild)
        except Exception:
            pass

@tasks.loop(minutes=1)
async def daily_voice_check():
    now = datetime.now(KST)
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    exact_midnight = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=KST)

    for guild in bot.guilds:
        try:
            if now.hour == 0 and now.minute == 0:
                for u_id, sess in list(VOICE_ACTIVE_SESSIONS.items()):
                    if sess["active"] and sess["start_time"] and sess["guild_id"] == guild.id:
                        before_midnight = int((exact_midnight - sess["start_time"]).total_seconds())
                        if before_midnight > 0:
                            await log_voice_duration(guild, u_id, yesterday_str, before_midnight)
                        sess["start_time"] = exact_midnight

            # 매주 월요일 자정 결산 (휴가/사유서 예외자 자동 면제)
            if now.hour == 0 and now.weekday() == 0:
                if guild.id not in VOICE_DAILY_CACHE:
                    _, v_records = await fetch_database_from_channel(guild)
                    VOICE_DAILY_CACHE[guild.id] = v_records

                if guild.id not in SERVER_AUDIT_FLAGS:
                    SERVER_AUDIT_FLAGS[guild.id] = {}

                guild_flags = SERVER_AUDIT_FLAGS[guild.id]

                if guild_flags.get("weekly") != yesterday_str:
                    channel = None
                    target_ch_id = VOICE_NOTICE_CHANNELS.get(guild.id)
                    if target_ch_id:
                        channel = guild.get_channel(target_ch_id)
                    if not channel:
                        channel = guild.system_channel
                    if not channel:
                        for ch in guild.text_channels:
                            if ch.permissions_for(guild.me).send_messages:
                                channel = ch
                                break

                    if channel:
                        guild_data = VOICE_DAILY_CACHE.get(guild.id, {})
                        target_members = [m for m in guild.members if not m.bot and is_target_member(m)]

                        weekly_failed = []
                        weekly_pings = []
                        past_7_days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]

                        for member in target_members:
                            # 🏖️ 통화 예외/휴가자 필터링 (결산 및 태그에서 제외)
                            if is_voice_exempt(guild.id, member.id):
                                continue

                            week_spent = sum(guild_data.get(d_str, {}).get(str(member.id), 0) for d_str in past_7_days)
                            if week_spent < 39600:
                                weekly_failed.append(f"• {member.mention} ({member.display_name}) - `{format_seconds(week_spent)}` / 11시간")
                                weekly_pings.append(member.mention)

                        embed_weekly = discord.Embed(
                            title="📊 [주간 통화시간 결산] 지난주 종합",
                            description="길드원/수습생 대상 지난주(월~일) 최소 **11시간** 통화방 참여 미달자 명단입니다.\n*(휴가 및 사유서 인정자는 집계에서 제외되었습니다)*",
                            color=discord.Color.dark_red() if weekly_failed else discord.Color.blue()
                        )
                        if weekly_failed:
                            add_chunked_fields(embed_weekly, f"🚨 주간 미달 확정자 (총 {len(weekly_failed)}명)", weekly_failed)
                            await channel.send(content=" ".join(weekly_pings[:40]), embed=embed_weekly)
                        else:
                            embed_weekly.add_field(name="👑 전원 달성!", value="지난주 모든 대상 멤버가 11시간 이상 통화방에 참여했습니다.", inline=False)
                            await channel.send(embed=embed_weekly)

                    await log_audit_flag(guild, "weekly", yesterday_str)

        except Exception as e:
            await send_error_log(guild, f"daily_voice_check Loop Error ({guild.name})", traceback.format_exc())

# --- 무기 및 직업 관리 ---
WEAPONS = ["검", "창", "도끼", "활", "클로", "메이스"]
JOBS = ["요리사", "대장장이", "낚시꾼", "연금술사"]

async def process_loadout_selection(interaction: discord.Interaction, weapon_name: Optional[str], job_name: Optional[str]):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    member = interaction.user

    if weapon_name:
        for w in WEAPONS:
            r = discord.utils.get(guild.roles, name=w)
            if r and r in member.roles:
                try:
                    await member.remove_roles(r)
                except discord.Forbidden:
                    pass
        new_w_role = discord.utils.get(guild.roles, name=weapon_name) or await guild.create_role(name=weapon_name)
        if new_w_role:
            try:
                await member.add_roles(new_w_role)
            except discord.Forbidden:
                pass

    if job_name:
        for j in JOBS:
            r = discord.utils.get(guild.roles, name=j)
            if r and r in member.roles:
                try:
                    await member.remove_roles(r)
                except discord.Forbidden:
                    pass
        
        if job_name in JOBS:
            new_j_role = discord.utils.get(guild.roles, name=job_name) or await guild.create_role(name=job_name)
            if new_j_role:
                try:
                    await member.add_roles(new_j_role)
                except discord.Forbidden:
                    pass

    active_weapon = weapon_name
    if not active_weapon:
        for w in WEAPONS:
            r = discord.utils.get(guild.roles, name=w)
            if r and r in member.roles:
                active_weapon = w
                break

    active_job = job_name
    if not active_job:
        for j in JOBS:
            r = discord.utils.get(guild.roles, name=j)
            if r and r in member.roles:
                active_job = j
                break

    chosen_tag = active_job if (active_job and active_job in JOBS) else active_weapon
    prefix = f"[{chosen_tag}] " if chosen_tag else ""
    cleaned_name = re.sub(r'^\[.*?\]\s*', '', member.display_name).strip()
    new_nickname = f"{prefix}{cleaned_name}"[:32]

    try:
        await member.edit(nick=new_nickname)
        nickname_msg = f"닉네임이 **{new_nickname}**(으)로 변경되었습니다!"
    except discord.Forbidden:
        nickname_msg = "(⚠️ 서버장 또는 봇보다 높은 역할이라 닉네임 자동 변경 불가)"

    summary = []
    if active_weapon:
        summary.append(f"무기: **{active_weapon}**")
    if active_job and active_job in JOBS:
        summary.append(f"직업: **{active_job}**")
    else:
        summary.append("직업: **전투직(없음)**")

    await interaction.followup.send(
        f"✅ 설정 완료! ({', '.join(summary)})\n🏷️ 적용된 태그: **{prefix.strip() or '없음'}**\n{nickname_msg}",
        ephemeral=True
    )

class WeaponSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=w, description=f"{w} 무기 선택") for w in WEAPONS]
        super().__init__(placeholder="🗡️ 무기를 선택하세요", min_values=1, max_values=1, options=options, row=0, custom_id="loadout_weapon_select")

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_weapon = self.values[0]
        await interaction.response.send_message(f"무기 **[{self.values[0]}]**(을)를 선택하셨습니다.", ephemeral=True)

class JobSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="없음 (전투직)", description="생활직업 없음 (무기 태그 우선 적용)", value="없음 (전투직)")
        ] + [discord.SelectOption(label=j, description=f"{j} 직업 선택", value=j) for j in JOBS]
        super().__init__(placeholder="🛠️ 직업을 선택하세요", min_values=1, max_values=1, options=options, row=1, custom_id="loadout_job_select")

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_job = self.values[0]
        await interaction.response.send_message(f"직업 **[{self.values[0]}]**(을)를 선택하셨습니다.", ephemeral=True)

class LoadoutConfirmButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✅ 선택 완료 및 적용", style=discord.ButtonStyle.success, row=2, custom_id="loadout_confirm_btn")

    async def callback(self, interaction: discord.Interaction):
        view: LoadoutSelectView = self.view
        if not view.selected_weapon and not view.selected_job:
            await interaction.response.send_message("❌ 무기나 직업 중 최소 하나를 드롭다운에서 선택해 주세요!", ephemeral=True)
            return
        await process_loadout_selection(interaction, view.selected_weapon, view.selected_job)

class LoadoutSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.selected_weapon: Optional[str] = None
        self.selected_job: Optional[str] = None
        self.add_item(WeaponSelect())
        self.add_item(JobSelect())
        self.add_item(LoadoutConfirmButton())


# --- 🛡️ [관리자 검토 & 직급 선택 승인 시스템] ---

AVAILABLE_ROLES = [
    "월영 수습생", "월영 길드원",
    "영월 수습생", "영월 길드원",
    "은월 수습생", "은월 길드원",
    "커뮤니티"
]

class RoleSelectDropdown(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=r, value=r) for r in AVAILABLE_ROLES]
        super().__init__(placeholder="🏷️ 부여할 직급(역할)을 선택하세요", min_values=1, max_values=1, options=options, row=0, custom_id="select_role_to_give")

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_role = self.values[0]
        await interaction.response.send_message(f"부여할 직급으로 **[{self.values[0]}]**(이)가 선택되었습니다.", ephemeral=True)

class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.selected_role: Optional[str] = None
        self.add_item(RoleSelectDropdown())

    @discord.ui.button(label="✅ 승인", style=discord.ButtonStyle.success, row=1, custom_id="btn_review_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if not is_admin_or_mod(interaction):
            await interaction.followup.send("❌ 관리자만 승인할 수 있습니다.", ephemeral=True)
            return

        embed = interaction.message.embeds[0]
        footer_parts = embed.footer.text.split("|")
        target_user_id = int(footer_parts[0].replace("USER_ID:", "").strip())
        new_nick = footer_parts[1].replace("NICK:", "").strip() if len(footer_parts) > 1 else None
        guild_target = footer_parts[2].replace("GUILD:", "").strip() if len(footer_parts) > 2 else "월영"

        guild = interaction.guild
        member = guild.get_member(target_user_id) or await guild.fetch_member(target_user_id)

        if not member:
            await interaction.followup.send("❌ 해당 유저를 서버에서 찾을 수 없습니다.", ephemeral=True)
            return

        chosen_role_name = self.selected_role or f"{guild_target} 수습생"
        role_to_give = discord.utils.get(guild.roles, name=chosen_role_name)
        if not role_to_give:
            try:
                role_to_give = await guild.create_role(name=chosen_role_name)
            except discord.Forbidden:
                role_to_give = None

        if role_to_give:
            try:
                await member.add_roles(role_to_give)
            except discord.Forbidden:
                pass

        if new_nick:
            try:
                await member.edit(nick=new_nick[:32])
            except discord.Forbidden:
                pass

        # 1. 관리자 신청서 메시지 상태 갱신
        embed.color = discord.Color.green()
        embed.title = "🛡️ [인증 신청 - 승인 완료]"
        embed.add_field(name="부여된 직급", value=f"{role_to_give.mention if role_to_give else chosen_role_name}", inline=True)
        embed.add_field(name="승인 관리진", value=f"{interaction.user.mention} ({interaction.user.display_name})", inline=True)
        
        for item in self.children:
            item.disabled = True

        await interaction.message.edit(embed=embed, view=self)

        # 2. 관리자 인증관리 채널에 "黑夜 | 인증 완료" 브리핑 발송
        admin_channel_id = VERIFY_ADMIN_CHANNELS.get(guild.id) or WARN_LOG_CHANNEL_ID
        admin_channel = guild.get_channel(admin_channel_id) or interaction.channel

        complete_embed = discord.Embed(
            title="黑夜 | 인증 완료",
            description=(
                f"{member.mention}\n"
                f"선택된 소속: {role_to_give.mention if role_to_give else f'@{chosen_role_name}'}\n"
                f"승인 관리진: {interaction.user.mention}"
            ),
            color=0x800020
        )
        if guild.icon:
            complete_embed.set_thumbnail(url=guild.icon.url)
        complete_embed.set_footer(text="黑夜 · 月影 × 影月 | 두 개의 길드, 하나의 밤")

        try:
            await admin_channel.send(embed=complete_embed)
        except Exception:
            pass

        try:
            await member.send(f"🎉 **{guild.name}** 서버의 **[{chosen_role_name}]** 인증이 승인되었습니다!")
        except Exception:
            pass

    @discord.ui.button(label="❌ 거절", style=discord.ButtonStyle.danger, row=1, custom_id="btn_review_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if not is_admin_or_mod(interaction):
            await interaction.followup.send("❌ 관리자만 거절할 수 있습니다.", ephemeral=True)
            return

        embed = interaction.message.embeds[0]
        footer_parts = embed.footer.text.split("|")
        target_user_id = int(footer_parts[0].replace("USER_ID:", "").strip())

        embed.color = discord.Color.red()
        embed.title = "🛡️ [인증 신청 - 거절 처리]"
        embed.add_field(name="처리 관리진", value=f"{interaction.user.mention} ({interaction.user.display_name})", inline=False)

        for item in self.children:
            item.disabled = True

        await interaction.message.edit(embed=embed, view=self)

        guild = interaction.guild
        member = guild.get_member(target_user_id)
        if member:
            try:
                await member.send(f"⚠️ **{guild.name}** 서버의 인증 신청이 관리자에 의해 거절되었습니다.")
            except Exception:
                pass


# --- 📝 신청 모달 UI ---
class ApplyModal(discord.ui.Modal, title="길드원 인증 신청서"):
    name = discord.ui.TextInput(label="이름 (또는 별명)", placeholder="예: 쨈", required=True, max_length=15)
    mc_name = discord.ui.TextInput(label="마인크래프트 닉네임", placeholder="예: _s2_jammy", required=True, max_length=25)
    guild_name = discord.ui.TextInput(label="소속 길드 (월영 / 영월 / 은월 / 커뮤니티 등)", placeholder="예: 월영", required=True, max_length=15)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user

        clean_name = self.name.value.strip()
        clean_mc = self.mc_name.value.strip()
        clean_guild = self.guild_name.value.strip()

        prefix_match = re.match(r'^(\[.*?\])\s*', user.display_name)
        prefix = f"{prefix_match.group(1)} " if prefix_match else ""

        formatted_nick = f"{prefix}{clean_name} / {clean_mc} / {clean_guild}"[:32]

        nick_change_status = ""
        try:
            await user.edit(nick=formatted_nick)
            nick_change_status = f"\n🏷️ 서버 닉네임이 **{formatted_nick}**(으)로 즉시 설정되었습니다!"
        except discord.Forbidden:
            nick_change_status = "\n⚠️ (서버장 또는 관리자 권한이 있어 닉네임 자동 변경 불가)"

        review_channel_id = VERIFY_ADMIN_CHANNELS.get(guild.id) or WARN_LOG_CHANNEL_ID
        review_channel = guild.get_channel(review_channel_id)
        if not review_channel:
            try:
                review_channel = await bot.fetch_channel(review_channel_id)
            except Exception:
                review_channel = None

        if not review_channel:
            await interaction.followup.send(f"❌ 관리자 승인 채널을 찾을 수 없습니다.{nick_change_status}\n관리자에게 `/인증관리채널설정`을 요청하세요.", ephemeral=True)
            return

        review_embed = discord.Embed(
            title="🛡️ [신규 길드원 인증 신청]",
            description=f"{user.mention} 님이 인증 신청서를 제출했습니다.\n아래 드롭다운에서 부여할 직급을 선택한 뒤 **[승인]**을 눌러주세요.",
            color=discord.Color.gold(),
            timestamp=datetime.now(KST)
        )
        review_embed.add_field(name="신청자", value=f"{user.display_name} (`{user.id}`)", inline=True)
        review_embed.add_field(name="이름 / 별명", value=clean_name, inline=True)
        review_embed.add_field(name="마크 닉네임", value=clean_mc, inline=True)
        review_embed.add_field(name="희망 소속", value=clean_guild, inline=True)
        review_embed.add_field(name="적용된 닉네임", value=f"`{formatted_nick}`", inline=True)
        review_embed.set_footer(text=f"USER_ID:{user.id}|NICK:{formatted_nick}|GUILD:{clean_guild}")

        await review_channel.send(embed=review_embed, view=ReviewView())
        await interaction.followup.send(f"✅ 인증 신청서가 관리자 채널로 전달되었습니다!{nick_change_status}\n관리진 검토 후 직급 부여가 완료됩니다.", ephemeral=True)

class ApplyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="인증 신청", style=discord.ButtonStyle.secondary, custom_id="btn_guild_apply_start")
    async def apply_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApplyModal())

# --- 📢 공지사항 모달 UI ---
class NoticeModal(discord.ui.Modal, title="📢 공지사항 작성"):
    notice_title = discord.ui.TextInput(label="공지 제목", placeholder="예: 길드 레이드 안내", required=True, max_length=50)
    notice_content = discord.ui.TextInput(label="공지 본문", placeholder="내용을 입력하세요.", style=discord.TextStyle.paragraph, required=True, max_length=1500)
    notice_footer = discord.ui.TextInput(label="추가 안내 (선택)", placeholder="예: 필독 부탁드립니다!", required=False, max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = discord.Embed(title=f"📢 {self.notice_title.value}", description=self.notice_content.value, color=discord.Color.blue())
        if self.notice_footer.value:
            embed.add_field(name="📌 전달사항", value=self.notice_footer.value, inline=False)
        embed.set_footer(text=f"작성자: {interaction.user.display_name} • {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
        await interaction.followup.send(embed=embed)

# --- 📑 공지 미확인자 페이지네이션 View ---
class NoticePaginationView(discord.ui.View):
    def __init__(self, author_id: int, base_embed: discord.Embed, chunks: List[List[str]], total_unreacted: int):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.base_embed = base_embed
        self.chunks = chunks
        self.total_unreacted = total_unreacted
        self.current_page = 0
        self.max_page = len(chunks) - 1
        self.update_buttons()

    def update_buttons(self):
        self.prev_btn_action.disabled = (self.current_page == 0)
        self.next_btn_action.disabled = (self.current_page == self.max_page)

    def get_current_embed(self) -> discord.Embed:
        embed = self.base_embed.copy()
        current_chunk = self.chunks[self.current_page]
        start_idx = self.current_page * 25 + 1
        end_idx = start_idx + len(current_chunk) - 1

        embed.add_field(
            name=f"⚠️ 미반응 멤버 명단 ({start_idx}~{end_idx}명 / 총 {self.total_unreacted}명)",
            value=" ".join(current_chunk),
            inline=False
        )
        embed.set_footer(text=f"페이지 {self.current_page + 1} / {self.max_page + 1} (하단 버튼으로 이동)")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ 명령어를 실행한 관리자만 페이지를 넘길 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary, custom_id="notice_prev_btn")
    async def prev_btn_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_current_embed(), view=self)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.primary, custom_id="notice_next_btn")
    async def next_btn_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.max_page:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_current_embed(), view=self)


# --- 슬래시 명령어 세트 ---

# 🧹 [신규: 인증 초기화 - 관리자 전용]
@bot.tree.command(name="인증초기화", description="[관리자 전용] 특정 유저의 길드원/무기/직업 역할을 모두 회수하고 닉네임을 초기화합니다.")
@app_commands.describe(유저="인증을 초기화할 대상 멤버")
async def reset_user_verification(interaction: discord.Interaction, 유저: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    removed_roles = []
    # 1. 길드 역할 및 무기/직업 역할 회수
    all_target_names = set(ALL_GUILD_ROLES + WEAPONS + JOBS)
    for r in list(유저.roles):
        if r.name in all_target_names:
            try:
                await 유저.remove_roles(r)
                removed_roles.append(r.name)
            except discord.Forbidden:
                pass

    # 2. 닉네임 태그 정리
    current_nick = 유저.display_name
    cleaned_nick = re.sub(r'^\[.*?\]\s*', '', current_nick)
    cleaned_nick = cleaned_nick.split('/')[0].strip()

    try:
        await 유저.edit(nick=cleaned_nick if cleaned_nick else None)
        nick_msg = f"닉네임이 **{cleaned_nick}**(으)로 초기화되었습니다."
    except discord.Forbidden:
        nick_msg = "권한 부족으로 닉네임 자동 변경은 실패했습니다."

    embed = discord.Embed(
        title="🧹 [길드원 인증 초기화 완료]",
        description=f"{유저.mention} 님의 인증이 초기화되었습니다.\n\n• 회수된 역할: {', '.join(removed_roles) if removed_roles else '없음'}\n• {nick_msg}",
        color=discord.Color.dark_grey()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

# 🏖️ [신규: 통화 예외/휴가 등록 - 관리자 전용]
@bot.tree.command(name="통화예외등록", description="[관리자 전용] 특정 유저를 지정한 날짜까지 통화 출결 체크에서 예외(면제) 처리합니다.")
@app_commands.describe(유저="출결 예외 대상 멤버", 종료일="예외 인정 종료일 (형식: YYYY-MM-DD)", 사유="휴가 및 예외 사유")
async def add_voice_exemption(interaction: discord.Interaction, 유저: discord.Member, 종료일: str, 사유: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    try:
        until_date = datetime.strptime(종료일.strip(), "%Y-%m-%d").date()
    except ValueError:
        await interaction.followup.send("❌ 날짜 형식이 올바르지 않습니다. 반드시 `YYYY-MM-DD` 형식으로 입력해 주세요. (예: 2026-11-20)", ephemeral=True)
        return

    if until_date < datetime.now(KST).date():
        await interaction.followup.send("❌ 종료일은 오늘 또는 미래 날짜여야 합니다.", ephemeral=True)
        return

    payload = {"until": 종료일.strip(), "reason": 사유.strip()}
    await log_voice_exempt_action(interaction.guild, 유저.id, "add", payload)

    embed = discord.Embed(
        title="🏖️ [통화 출결 예외(휴가) 등록 완료]",
        description=f"{유저.mention} 님이 **{종료일.strip()}**까지 통화 출결 집계 및 미달자 태그에서 제외됩니다.",
        color=discord.Color.teal()
    )
    embed.add_field(name="종료 예정일", value=f"`{종료일.strip()}` 자정까지", inline=True)
    embed.add_field(name="등록 사유", value=사유.strip(), inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)

# 🏖️ [신규: 통화 예외 해제 - 관리자 전용]
@bot.tree.command(name="통화예외해제", description="[관리자 전용] 등록된 통화 출결 예외(휴가)를 즉시 해제합니다.")
@app_commands.describe(유저="예외를 해제할 멤버")
async def remove_voice_exemption(interaction: discord.Interaction, 유저: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    if not is_voice_exempt(interaction.guild.id, 유저.id):
        await interaction.followup.send("⚠️ 해당 멤버는 현재 통화 예외자로 등록되어 있지 않습니다.", ephemeral=True)
        return

    await log_voice_exempt_action(interaction.guild, 유저.id, "remove", {})
    await interaction.followup.send(f"✅ {유저.mention} 님의 통화 출결 예외 상태가 해제되어 정상 집계됩니다.", ephemeral=True)

# 🏖️ [신규: 통화 예외 목록 조회 - 관리자 전용]
@bot.tree.command(name="통화예외목록", description="[관리자 전용] 현재 통화 출결 체크에서 면제 중인 휴가자 명단을 조회합니다.")
async def list_voice_exemptions(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    guild_exempts = VOICE_EXEMPTIONS.get(interaction.guild.id, {})
    active_lines = []
    today = datetime.now(KST).date()

    for uid, info in guild_exempts.items():
        try:
            u_date = datetime.strptime(info["until"], "%Y-%m-%d").date()
            if today <= u_date:
                active_lines.append(f"• <@{uid}> - `{info['until']}`까지 ({info['reason']})")
        except Exception:
            continue

    if not active_lines:
        await interaction.followup.send("✨ 현재 통화 출결 예외(휴가)로 등록된 멤버가 없습니다.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"🏖️ [통화 출결 예외/휴가자 목록] (총 {len(active_lines)}명)",
        description="\n".join(active_lines),
        color=discord.Color.teal()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="인증관리채널설정", description="[관리자 전용] 유저들의 인증 신청서와 인증 완료 알림이 도착할 관리자 채널을 설정합니다.")
@app_commands.describe(채널="신청서 검토 및 완료 알림이 올라올 관리자 비공개 텍스트 채널")
async def set_verify_admin_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    await log_verify_admin_config(interaction.guild, 채널.id)
    await interaction.response.send_message(f"✅ 앞으로 유저들의 인증 신청서와 인증 완료 메시지가 {채널.mention} 채널로 도착합니다!", ephemeral=True)

@bot.tree.command(name="환영채널설정", description="[관리자 전용] 새 멤버가 들어왔을 때 환영/인증 안내 메시지가 자동 전송될 채널을 지정합니다.")
@app_commands.describe(채널="환영 안내 메시지가 전송될 텍스트 채널")
async def set_welcome_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    await log_welcome_config(interaction.guild, 채널.id)
    await interaction.response.send_message(f"✅ 새 멤버 입장 시 {채널.mention} 채널로 환영/인증 메시지가 자동 발송됩니다!", ephemeral=True)

@bot.tree.command(name="인증메시지전송", description="[관리자 전용] 지정한 채널에 사진과 동일한 환영/인증 안내 메시지를 전송합니다.")
@app_commands.describe(채널="인증 메시지를 보낼 텍스트 채널")
async def send_verification_message(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    embed = create_welcome_embed(interaction.guild)
    try:
        await 채널.send(embed=embed, view=ApplyView())
        await interaction.response.send_message(f"✅ {채널.mention} 채널에 인증 신청 메시지를 전송했습니다!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"⚠️ {채널.mention} 채널에 메시지를 보낼 권한이 없습니다.", ephemeral=True)

@bot.tree.command(name="인원수채널설정", description="[관리자 전용] 실시간 전체 이용자 수가 표시될 음성 채널을 지정합니다.")
@app_commands.describe(채널="전체 인원수가 표시될 음성 채널")
async def set_member_counter_channel(interaction: discord.Interaction, 채널: discord.VoiceChannel):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    await log_counter_config(interaction.guild, 채널.id)
    await update_member_counter(interaction.guild)
    await interaction.response.send_message(f"✅ {채널.mention} 채널이 실시간 전체 인원수 카운터로 설정되었습니다!", ephemeral=True)

@bot.tree.command(name="공지미확인", description="[관리자 전용] 공지 채널의 @everyone 공지에 이모지를 안 단 길드원 목록을 확인합니다.")
@app_commands.describe(메시지id="특정 공지 글을 확인할 경우 메시지 ID 입력 (비워두면 본인의 가장 최근 @everyone 공지)")
async def check_notice_reactions(interaction: discord.Interaction, 메시지id: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    guild = interaction.guild
    channel = guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(ANNOUNCEMENT_CHANNEL_ID)
        except Exception as e:
            await interaction.followup.send(f"❌ 공지 채널을 찾을 수 없습니다. ({e})", ephemeral=True)
            return

    target_message: Optional[discord.Message] = None
    if 메시지id:
        try:
            target_message = await channel.fetch_message(int(메시지id.strip()))
        except Exception:
            await interaction.followup.send("❌ 지정한 메시지 ID의 글을 찾을 수 없습니다.", ephemeral=True)
            return
    else:
        async for msg in channel.history(limit=50):
            if msg.author.id == interaction.user.id and ("@everyone" in msg.content or msg.mention_everyone):
                target_message = msg
                break
        if not target_message:
            async for msg in channel.history(limit=50):
                if "@everyone" in msg.content or msg.mention_everyone:
                    target_message = msg
                    break

    if not target_message:
        await interaction.followup.send("❌ 공지 채널에서 `@everyone` 태그가 포함된 공지 글을 찾지 못했습니다.", ephemeral=True)
        return

    reacted_user_ids = set()
    for reaction in target_message.reactions:
        async for u in reaction.users(limit=None):
            reacted_user_ids.add(u.id)

    target_members = [m for m in guild.members if not m.bot and is_target_member(m)]
    unreacted_members = [m for m in target_members if m.id not in reacted_user_ids]

    summary_content = (target_message.content[:40] + "...") if len(target_message.content) > 40 else target_message.content
    msg_link = f"https://discord.com/channels/{guild.id}/{channel.id}/{target_message.id}"

    base_embed = discord.Embed(
        title="📢 [공지 이모지 확인 현황]",
        description=f"🔗 [해당 공지 바로가기]({msg_link})\n📝 **공지 요약:** {summary_content or '(임베드 공지)'}",
        color=discord.Color.orange() if unreacted_members else discord.Color.green()
    )
    base_embed.add_field(name="👥 전체 대상 인원", value=f"{len(target_members)}명", inline=True)
    base_embed.add_field(name="✅ 확인 (이모지 등록)", value=f"{len(target_members) - len(unreacted_members)}명", inline=True)
    base_embed.add_field(name="❌ 미확인 (미반응)", value=f"**{len(unreacted_members)}명**", inline=True)

    if not unreacted_members:
        base_embed.add_field(name="🎉 전원 확인 완료!", value="모든 길드원/수습생이 공지에 이모지를 남겼습니다.", inline=False)
        await interaction.followup.send(embed=base_embed, ephemeral=True)
        return

    mentions = [m.mention for m in unreacted_members]
    chunk_size = 25
    chunks = [mentions[i:i + chunk_size] for i in range(0, len(mentions), chunk_size)]

    if len(chunks) == 1:
        base_embed.add_field(name=f"⚠️ 미반응 멤버 명단 ({len(unreacted_members)}명)", value=" ".join(chunks[0]), inline=False)
        await interaction.followup.send(embed=base_embed, ephemeral=True)
    else:
        view = NoticePaginationView(
            author_id=interaction.user.id,
            base_embed=base_embed,
            chunks=chunks,
            total_unreacted=len(unreacted_members)
        )
        await interaction.followup.send(embed=view.get_current_embed(), view=view, ephemeral=True)

@bot.tree.command(name="사다리", description="현재 통화방에 있는 인원으로 사다리 타기를 진행합니다.")
@app_commands.describe(당첨인원="당첨될 인원수", 당첨항목="당첨 항목 이름 (기본값: 🎉 당첨)", 꽝항목="꽝 항목 이름 (기본값: ❌ 꽝)")
async def ladder_game(interaction: discord.Interaction, 당첨인원: int, 당첨항목: str = "🎉 당첨", 꽝항목: str = "❌ 꽝"):
    await interaction.response.defer()
    win_label = 당첨항목.strip()
    lose_label = 꽝항목.strip()
    if not win_label or not lose_label:
        await interaction.followup.send("❌ 항목 이름을 비워둘 수 없습니다.", ephemeral=True)
        return
    if win_label == lose_label:
        await interaction.followup.send("❌ **당첨 항목**과 **꽝 항목** 이름을 서로 다르게 입력해 주세요!", ephemeral=True)
        return
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("❌ 먼저 음성 채널(통화방)에 접속한 뒤 사용해 주세요!", ephemeral=True)
        return

    voice_channel = interaction.user.voice.channel
    players = [m.display_name for m in voice_channel.members if not m.bot]
    total_count = len(players)
    if total_count < 2:
        await interaction.followup.send("❌ 통화방에 최소 2명 이상 있어야 합니다.", ephemeral=True)
        return
    if 당첨인원 <= 0 or 당첨인원 >= total_count:
        await interaction.followup.send(f"❌ 당첨 인원은 1명 이상 {total_count - 1}명 이하여야 합니다.", ephemeral=True)
        return

    lose_count = total_count - 당첨인원
    winner_flags = [True] * 당첨인원 + [False] * lose_count
    random.shuffle(winner_flags)

    result_lines = []
    winners = []
    for player, is_winner in zip(players, winner_flags):
        if is_winner:
            result_lines.append(f"👑 **{player}** ➔ **{win_label}**")
            winners.append(player)
        else:
            result_lines.append(f"👤 {player} ➔ {lose_label}")

    embed = discord.Embed(title=f"🪜 [{voice_channel.name}] 사다리 타기 결과", description="\n".join(result_lines), color=discord.Color.purple())
    embed.add_field(name="🏆 최종 당첨자", value=", ".join(winners), inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="통화시간", description="오늘, 이번 주 요일별 참여 시간, 지난 주 기록 등을 확인합니다.")
@app_commands.describe(유저="조회할 멤버 (비워두면 본인)")
async def check_voice_time(interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
    await interaction.response.defer(ephemeral=True)
    target = 유저 if 유저 else interaction.user

    if not is_target_member(target):
        await interaction.followup.send("⚠️ 해당 멤버는 통화 시간 집계 대상(길드원/수습생)이 아닙니다.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    now = datetime.now(KST)
    today_str = now.strftime("%Y-%m-%d")

    if guild_id not in VOICE_DAILY_CACHE:
        _, v_records = await fetch_database_from_channel(interaction.guild)
        VOICE_DAILY_CACHE[guild_id] = v_records

    guild_data = VOICE_DAILY_CACHE.get(guild_id, {})
    u_str = str(target.id)

    today_sec = guild_data.get(today_str, {}).get(u_str, 0)
    current_status = "❌ 현재 미접속"

    cur_live_diff = 0
    if target.id in VOICE_ACTIVE_SESSIONS:
        sess = VOICE_ACTIVE_SESSIONS[target.id]
        if sess["active"] and sess["start_time"]:
            cur_live_diff = int((now - sess["start_time"]).total_seconds())
            today_sec += cur_live_diff
            current_status = f"🟢 참여 중 (+{format_seconds(cur_live_diff)})"
        else:
            if target.voice and target.voice.channel:
                if is_afk_channel(target.voice.channel):
                    current_status = "⏸️ 잠수방 접속 중 (측정 일시정지)"
                else:
                    humans = [m for m in target.voice.channel.members if not m.bot]
                    if len(humans) < 2:
                        current_status = "⏸️ 혼자 접속 중 (2명 이상 모여야 측정)"
                    else:
                        current_status = "⏸️ 헤드셋 끔 (잠수 중 / 측정 일시정지)"
            else:
                current_status = "⏸️ 측정 대기 중"

    # 🏖️ 통화 출결 예외(휴가) 여부 확인
    exempt_info = is_voice_exempt(guild_id, target.id)

    current_weekday = now.weekday()
    monday_date = now - timedelta(days=current_weekday)
    weekdays_ko = ["월", "화", "수", "목", "금", "토", "일"]

    daily_breakdown = []
    this_week_sec = 0

    for i in range(7):
        target_day = monday_date + timedelta(days=i)
        d_str = target_day.strftime("%Y-%m-%d")
        day_spent = guild_data.get(d_str, {}).get(u_str, 0)

        if i == current_weekday:
            day_spent += cur_live_diff

        this_week_sec += day_spent
        day_label = f"**{weekdays_ko[i]}요일**" + (" *(오늘)*" if i == current_weekday else "")
        daily_breakdown.append(f"• {day_label} : `{format_seconds(day_spent)}`")

    last_week_monday = now - timedelta(days=current_weekday + 7)
    last_week_days = [(last_week_monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    last_week_sec = sum(guild_data.get(d_str, {}).get(u_str, 0) for d_str in last_week_days)

    past_7_days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    recent_7_sec = sum(guild_data.get(d_str, {}).get(u_str, 0) for d_str in past_7_days) + cur_live_diff

    embed = discord.Embed(title=f"🎙️ {target.display_name} 님의 통화방 시간표", color=discord.Color.teal())
    embed.add_field(name="📅 오늘 참여 시간", value=f"**{format_seconds(today_sec)}**", inline=True)
    embed.add_field(name="🗓️ 이번 주 총합 (월~오늘)", value=f"**{format_seconds(this_week_sec)}** (기준: 11시간)", inline=True)
    embed.add_field(name="📜 지난주 기록 (월~일)", value=f"**{format_seconds(last_week_sec)}** (기준: 11시간)", inline=True)
    
    if exempt_info:
        embed.add_field(name="🏖️ 출결 예외 상태", value=f"**{exempt_info['until']}**까지 면제 ({exempt_info['reason']})", inline=False)

    embed.add_field(name="📊 이번 주 요일별 상세 기록", value="\n".join(daily_breakdown), inline=False)
    embed.add_field(name="📈 최근 7일 이동 합계", value=f"**{format_seconds(recent_7_sec)}**", inline=True)
    embed.add_field(name="📌 실시간 상태", value=current_status, inline=True)
    embed.set_footer(text="잠수방, 혼자 있는 경우, 헤드셋 끔 상태에서는 시간이 측정되지 않습니다.")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="통화미달목록", description="[관리자 전용] 오늘 1시간 미달자 또는 최근 7일 11시간 미달자를 확인합니다.")
@app_commands.describe(구분="확인할 기준 (일일 or 주간)")
@app_commands.choices(구분=[
    app_commands.Choice(name="📅 오늘 1시간 미달자", value="일일"),
    app_commands.Choice(name="📊 최근 7일 11시간 미달자", value="주간")
])
async def list_voice_failed(interaction: discord.Interaction, 구분: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    guild = interaction.guild
    now = datetime.now(KST)
    today_str = now.strftime("%Y-%m-%d")

    if guild.id not in VOICE_DAILY_CACHE:
        _, v_records = await fetch_database_from_channel(guild)
        VOICE_DAILY_CACHE[guild.id] = v_records

    guild_data = VOICE_DAILY_CACHE.get(guild.id, {})
    failed_lines = []
    target_members = [m for m in guild.members if not m.bot and is_target_member(m)]

    if 구분.value == "일일":
        today_records = guild_data.get(today_str, {})
        for member in target_members:
            if is_voice_exempt(guild.id, member.id):
                continue
            spent = today_records.get(str(member.id), 0)
            if member.id in VOICE_ACTIVE_SESSIONS and VOICE_ACTIVE_SESSIONS[member.id]["active"]:
                spent += int((now - VOICE_ACTIVE_SESSIONS[member.id]["start_time"]).total_seconds())
            if spent < 3600:
                failed_lines.append(f"• {member.mention} ({member.display_name}) : `{format_seconds(spent)}`")
        title = f"📋 오늘 길드 통화방 1시간 미달자 ({len(failed_lines)}명)"
    else:
        past_7_days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        for member in target_members:
            if is_voice_exempt(guild.id, member.id):
                continue
            spent = sum(guild_data.get(d_str, {}).get(str(member.id), 0) for d_str in past_7_days)
            if member.id in VOICE_ACTIVE_SESSIONS and VOICE_ACTIVE_SESSIONS[member.id]["active"]:
                spent += int((now - VOICE_ACTIVE_SESSIONS[member.id]["start_time"]).total_seconds())
            if spent < 39600:
                failed_lines.append(f"• {member.mention} ({member.display_name}) : `{format_seconds(spent)}`")
        title = f"📊 최근 7일 통화방 11시간 미달자 ({len(failed_lines)}명)"

    if not failed_lines:
        await interaction.followup.send("🎉 전원 기준 달성 완료! (휴가자 제외) 미달자가 없습니다.", ephemeral=True)
        return

    embed = discord.Embed(title=title, color=discord.Color.red())
    add_chunked_fields(embed, "미달자 목록", failed_lines)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="통화알림채널설정", description="[관리자 전용] 매주 월요일 자정 주간 정산 브리핑이 전송될 채널을 설정합니다.")
@app_commands.describe(채널="브리핑이 올라갈 텍스트 채널")
async def set_voice_notice_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    await log_voice_config(interaction.guild, 채널.id)
    await interaction.response.send_message(f"✅ 매주 월요일 자정 주간 미달자 결산 브리핑이 {채널.mention} 채널로 전송됩니다!", ephemeral=True)

@bot.tree.command(name="무기직업선택", description="드롭다운 메뉴로 사용할 무기와 직업을 선택합니다.")
async def select_weapon_and_job(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚔️ 무기 & 직업 선택",
        description=(
            "아래 드롭다운에서 **무기**와 **직업**을 선택한 뒤\n"
            "**[선택 완료 및 적용]** 버튼을 눌러주세요!\n\n"
            "🏷️ **태그 표기 규칙:**\n"
            "• 직업이 있으면 **[직업]** 태그가 우선 표기됩니다.\n"
            "• 직업이 없거나 **'없음 (전투직)'**을 선택하면 **[무기]** 태그가 표기됩니다."
        ),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=LoadoutSelectView(), ephemeral=True)

@bot.tree.command(name="무기직업", description="무기와 직업을 직접 텍스트로 지정합니다.")
@app_commands.describe(무기="선택할 무기 (검, 창, 도끼, 활, 클로, 메이스)", 직업="선택할 직업 (요리사, 대장장이, 낚시꾼, 연금술사, 또는 전투직)")
async def direct_set_loadout(interaction: discord.Interaction, 무기: Optional[str] = None, 직업: Optional[str] = None):
    if not 무기 and not 직업:
        await interaction.response.send_message("❌ 무기나 직업 중 최소 하나를 입력해 주세요!", ephemeral=True)
        return
    if 무기 and 무기 not in WEAPONS:
        await interaction.response.send_message(f"❌ 올바른 무기를 입력해 주세요. (가능: {', '.join(WEAPONS)})", ephemeral=True)
        return
    if 직업 and 직업 not in JOBS and 직업 not in ["전투직", "없음"]:
        await interaction.response.send_message(f"❌ 올바른 직업을 입력해 주세요. (가능: {', '.join(JOBS)}, 전투직)", ephemeral=True)
        return
    await process_loadout_selection(interaction, 무기, 직업)

@bot.tree.command(name="공지", description="[관리자 전용] 팝업창을 열어 공지사항을 작성합니다.")
async def create_notice(interaction: discord.Interaction):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 공지를 작성할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.send_modal(NoticeModal())

@bot.tree.command(name="청소", description="[관리자 전용] 지정한 개수만큼 메시지를 삭제합니다.")
@app_commands.describe(개수="삭제할 메시지 개수 (1~100개)")
async def clear_messages(interaction: discord.Interaction, 개수: int):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    if 개수 < 1 or 개수 > 100:
        await interaction.response.send_message("❌ 1~100개 사이로 지정해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=개수)
        await interaction.followup.send(f"🧹 메시지 **{len(deleted)}개**를 삭제했습니다.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("⚠️ 봇에 '메시지 관리' 권한이 없습니다.", ephemeral=True)

@bot.tree.command(name="멤버멘션추출", description="[관리자 전용] 특정 역할을 가진 멤버들의 닉네임을 복사용 텍스트로 출력합니다.")
@app_commands.describe(역할="목록을 추출할 역할")
async def extract_role_mentions(interaction: discord.Interaction, 역할: discord.Role):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    members = [m for m in 역할.members if not m.bot]
    if not members:
        await interaction.response.send_message(f"❌ **{역할.name}** 역할을 가진 유저가 없습니다.", ephemeral=True)
        return
    name_list = "\n".join([m.display_name for m in members])
    msg = f"📋 **{역할.name}** 멤버 닉네임 목록 (총 {len(members)}명):\n\n```text\n{name_list}\n```"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="경고", description="[관리자 전용] 유저에게 경고를 부여합니다.")
@app_commands.describe(유저="대상 멤버", 사유="경고 사유")
async def warn_user(interaction: discord.Interaction, 유저: discord.Member, 사유: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    if 유저.bot:
        await interaction.followup.send("❌ 봇에게는 부여할 수 없습니다.", ephemeral=True)
        return

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    warn_entry = {"reason": 사유, "moderator": interaction.user.display_name, "date": now_str}
    await log_warning_action(interaction.guild, 유저.id, "add", warn_entry)
    
    warnings, _ = await fetch_database_from_channel(interaction.guild)
    count = len(warnings.get(str(유저.id), []))

    embed = discord.Embed(title="⚠️ 경고가 부여되었습니다", description=f"{유저.mention} 님에게 경고가 1회 누적되었습니다.", color=discord.Color.red())
    embed.add_field(name="👤 대상자", value=유저.display_name, inline=True)
    embed.add_field(name="🚨 누적 횟수", value=f"**{count}회**", inline=True)
    embed.add_field(name="📝 사유", value=사유, inline=False)
    await interaction.channel.send(content=유저.mention, embed=embed)
    await interaction.followup.send(f"✅ {유저.display_name} 님에게 경고를 부여했습니다. (누적: {count}회)", ephemeral=True)

@bot.tree.command(name="경고확인", description="경고 내역을 확인합니다.")
@app_commands.describe(유저="조회할 대상 멤버 (비워두면 본인)")
async def check_warnings(interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
    await interaction.response.defer(ephemeral=True)
    target = 유저 if 유저 else interaction.user
    warnings, _ = await fetch_database_from_channel(interaction.guild)
    records = warnings.get(str(target.id), [])
    count = len(records)
    if count == 0:
        await interaction.followup.send(f"✨ **{target.display_name}** 님은 경고가 없습니다! (0회)", ephemeral=True)
        return
    lines = [f"**{i}.** {r['reason']} *({r['moderator']} • {r['date']})*" for i, r in enumerate(records, 1)]
    embed = discord.Embed(title=f"📋 {target.display_name} 님의 경고 내역", description=f"총 누적: **{count}회**\n\n" + "\n".join(lines), color=discord.Color.orange())
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="경고목록", description="[관리자 전용] 현재 경고를 보유 중인 모든 유저의 목록을 확인합니다.")
async def show_all_warnings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    warnings, _ = await fetch_database_from_channel(interaction.guild)
    active_warns = {uid: recs for uid, recs in warnings.items() if len(recs) > 0}
    if not active_warns:
        await interaction.followup.send("✨ 현재 서버에 경고를 받은 유저가 한 명도 없습니다!", ephemeral=True)
        return
    warn_lines = []
    for uid, records in active_warns.items():
        member = interaction.guild.get_member(int(uid))
        name = member.display_name if member else f"(서버 탈퇴/ID: {uid})"
        warn_lines.append(f"• <@{uid}> (**{name}**) : **{len(records)}회**")
    embed = discord.Embed(title="🚨 [관리자] 서버 경고 누적 유저 목록", description=f"총 **{len(warn_lines)}명**의 유저가 경고를 보유 중입니다.\n\n" + "\n".join(warn_lines), color=discord.Color.red())
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="경고차감", description="[관리자 전용] 경고를 차감합니다.")
@app_commands.describe(유저="대상 멤버", 개수="차감할 개수 (1 이상)")
async def remove_warn(interaction: discord.Interaction, 유저: discord.Member, 개수: Optional[int] = 1):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    count_to_deduct = 개수 if 개수 is not None else 1
    if count_to_deduct <= 0:
        await interaction.followup.send("❌ 차감할 개수는 **1 이상**의 양수여야 합니다.", ephemeral=True)
        return
    warnings, _ = await fetch_database_from_channel(interaction.guild)
    u_id = str(유저.id)
    if u_id not in warnings or len(warnings[u_id]) == 0:
        await interaction.followup.send("❌ 차감할 경고가 없습니다.", ephemeral=True)
        return
    deduct = min(count_to_deduct, len(warnings[u_id]))
    await log_warning_action(interaction.guild, 유저.id, "remove", {"count": deduct})
    new_count = len(warnings[u_id]) - deduct
    await interaction.followup.send(f"✅ **{유저.display_name}** 님의 경고가 **{deduct}회** 차감되었습니다. (현재: **{new_count}회**)", ephemeral=True)

@bot.tree.command(name="경고초기화", description="[관리자 전용] 경고를 초기화합니다.")
@app_commands.describe(유저="대상 멤버")
async def clear_warn(interaction: discord.Interaction, 유저: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    await log_warning_action(interaction.guild, 유저.id, "clear", {})
    await interaction.followup.send(f"🧹 **{유저.display_name}** 님의 모든 경고가 초기화되었습니다.", ephemeral=True)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    err_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    await send_error_log(interaction.guild, f"Command Error: /{interaction.command.name if interaction.command else 'Unknown'}", err_text)
    msg = f"❌ 명령어 실행 중 오류가 발생했습니다: `{error}`"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

@bot.command(name="동기화")
@commands.has_permissions(administrator=True)
async def manual_sync(ctx):
    try:
        ctx.bot.tree.copy_global_to(guild=ctx.guild)
        synced = await ctx.bot.tree.sync(guild=ctx.guild)
        _, v_records = await fetch_database_from_channel(ctx.guild)
        VOICE_DAILY_CACHE[ctx.guild.id] = v_records
        await ctx.send(f"✅ 이 서버에 **{len(synced)}개**의 슬래시 명령어를 등록했습니다! 디스코드 새로고침(Ctrl+R) 후 확인해 보세요.")
    except Exception as e:
        await ctx.send(f"❌ 동기화 실패: {e}")

@bot.command(name="초기화")
@commands.has_permissions(administrator=True)
async def clean_sync(ctx):
    try:
        await ctx.bot.http.bulk_upsert_global_commands(ctx.bot.user.id, [])
        await ctx.send("🧹 전역에 남아있던 중복 찌꺼기 명령어를 삭제했습니다! 이제 `!동기화`를 입력하세요.")
    except Exception as e:
        await ctx.send(f"❌ 초기화 실패: {e}")

@manual_sync.error
@clean_sync.error
async def sync_permission_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 관리자(Administrator) 권한이 있는 멤버만 이 명령어를 사용할 수 있습니다.")

@bot.event
async def on_ready():
    bot.add_view(LoadoutSelectView())
    bot.add_view(ApplyView())
    bot.add_view(ReviewView())

    if not daily_voice_check.is_running():
        daily_voice_check.start()
    if not update_member_counter_loop.is_running():
        update_member_counter_loop.start()

    await asyncio.sleep(2)
    now = datetime.now(KST)

    for guild in bot.guilds:
        try:
            _, v_records = await fetch_database_from_channel(guild)
            VOICE_DAILY_CACHE[guild.id] = v_records
            await update_member_counter(guild)

            for vc in guild.voice_channels:
                if is_afk_channel(vc):
                    continue
                humans = [m for m in vc.members if not m.bot]
                has_enough = len(humans) >= 2
                for m in humans:
                    if is_target_member(m):
                        is_deaf = m.voice.self_deaf or m.voice.deaf if m.voice else False
                        should_track = has_enough and not is_deaf
                        if m.id in VOICE_ACTIVE_SESSIONS:
                            VOICE_ACTIVE_SESSIONS[m.id]["active"] = should_track
                            if should_track and not VOICE_ACTIVE_SESSIONS[m.id].get("start_time"):
                                VOICE_ACTIVE_SESSIONS[m.id]["start_time"] = now
                        else:
                            VOICE_ACTIVE_SESSIONS[m.id] = {
                                "active": should_track,
                                "start_time": now if should_track else None,
                                "guild_id": guild.id
                            }
        except Exception:
            await send_error_log(guild, "on_ready Setup Error", traceback.format_exc())
            
    print(f"로그인 완료: {bot.user.name}")

if __name__ == "__main__":
    keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    bot.run(token)
