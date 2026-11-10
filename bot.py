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

# 🚨 [중요] 경고 및 통화 기록을 보관할 비공개 채널 ID
WARN_LOG_CHANNEL_ID = 1569705414839242884

# 📢 공지 확인 대상 채널 ID
ANNOUNCEMENT_CHANNEL_ID = 1559080610335817779

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

def is_target_member(member: discord.Member) -> bool:
    """지정된 길드원/수습생 역할을 하나라도 가지고 있는지 확인"""
    return any(role.name in TARGET_ROLES for role in member.roles)

# 서버별 통화 알림 채널 매핑 {guild_id: channel_id}
VOICE_NOTICE_CHANNELS: Dict[int, int] = {}

# 통화방 세션 메모리 {user_id: {"active": bool, "start_time": datetime, "guild_id": int}}
VOICE_ACTIVE_SESSIONS: Dict[int, dict] = {}
# DB에서 복원된 누적 통화 시간 {guild_id: {date_str(YYYY-MM-DD): {user_id_str: seconds}}}
VOICE_DAILY_CACHE: Dict[int, Dict[str, Dict[str, int]]] = {}

# 서버별 정산 실행 플래그 {guild_id: {"night": date_str, "daily": date_str, "weekly": date_str}}
SERVER_AUDIT_FLAGS: Dict[int, Dict[str, str]] = {}

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

    try:
        # 최신 5,000건을 역순으로 수집 후 시간순(과거->최신)으로 정렬 재생
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

                # 1. 경고 데이터 복원
                if footer_text.startswith("WARN_DATA:"):
                    try:
                        raw_data = footer_text.replace("WARN_DATA:", "").strip()
                        data = json.loads(raw_data)
                        u_id = str(data["user_id"])
                        action = data.get("action", "add")
                        
                        if action == "add":
                            if u_id not in warnings:
                                warnings[u_id] = []
                            warnings[u_id].append({
                                "reason": data["reason"],
                                "moderator": data["moderator"],
                                "date": data["date"]
                            })
                        elif action == "remove":
                            deduct = data.get("count", 1)
                            if u_id in warnings:
                                warnings[u_id] = warnings[u_id][:-deduct]
                        elif action == "clear":
                            if u_id in warnings:
                                warnings[u_id] = []
                    except Exception:
                        continue

                # 2. 통화방 누적 시간 복원
                elif footer_text.startswith("VOICE_LOG:"):
                    try:
                        raw_data = footer_text.replace("VOICE_LOG:", "").strip()
                        data = json.loads(raw_data)
                        d_str = data["date"]
                        u_id = str(data["user_id"])
                        dur = int(data.get("duration", 0))

                        if d_str not in voice_records:
                            voice_records[d_str] = {}
                        voice_records[d_str][u_id] = voice_records[d_str].get(u_id, 0) + dur
                    except Exception:
                        continue

                # 3. 서버별 통화 알림 채널 ID 복원
                elif footer_text.startswith("VOICE_CONFIG:"):
                    try:
                        raw_data = footer_text.replace("VOICE_CONFIG:", "").strip()
                        cfg = json.loads(raw_data)
                        g_id = cfg.get("guild_id", guild.id)
                        VOICE_NOTICE_CHANNELS[g_id] = cfg.get("channel_id")
                    except Exception:
                        continue

                # 4. 정산 플래그 복원
                elif footer_text.startswith("AUDIT_FLAG:"):
                    try:
                        raw_data = footer_text.replace("AUDIT_FLAG:", "").strip()
                        flag_data = json.loads(raw_data)
                        g_id = flag_data.get("guild_id", guild.id)
                        if g_id not in SERVER_AUDIT_FLAGS:
                            SERVER_AUDIT_FLAGS[g_id] = {}
                        SERVER_AUDIT_FLAGS[g_id][flag_data["type"]] = flag_data["date"]
                    except Exception:
                        continue

    except Exception as e:
        await send_error_log(guild, "fetch_database_from_channel", traceback.format_exc())

    return warnings, voice_records

async def log_warning_action(guild: discord.Guild, user_id: int, action: str, data: dict):
    if WARN_LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
        except Exception:
            return

    embed = discord.Embed(
        title=f"🔒 [경고 DB 로그] {action.upper()}",
        color=discord.Color.dark_grey()
    )
    embed.add_field(name="Target User ID", value=str(user_id), inline=True)
    embed.add_field(name="Action", value=action, inline=True)
    
    payload = {"user_id": user_id, "action": action, **data}
    embed.set_footer(text=f"WARN_DATA:{json.dumps(payload, ensure_ascii=False)}")
    await channel.send(embed=embed)

# --- [개선 ③] DB 채널 전송 성공 후 캐시 누적 갱신 ---
async def log_voice_duration(guild: discord.Guild, user_id: int, date_str: str, duration_sec: int):
    if WARN_LOG_CHANNEL_ID == 0 or duration_sec <= 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
        except Exception:
            return

    embed = discord.Embed(
        title="🎙️ [통화 DB 로그] RECORD",
        color=discord.Color.dark_blue()
    )
    embed.add_field(name="User ID", value=str(user_id), inline=True)
    embed.add_field(name="Date", value=date_str, inline=True)
    embed.add_field(name="Duration", value=f"{duration_sec}초 ({duration_sec // 60}분)", inline=True)

    payload = {"date": date_str, "user_id": user_id, "duration": duration_sec}
    embed.set_footer(text=f"VOICE_LOG:{json.dumps(payload, ensure_ascii=False)}")

    # DB 전송 성공 후 캐시 갱신
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
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
        except Exception:
            return

    embed = discord.Embed(
        title="🎙️ [통화 설정 DB 로그] NOTICE_CHANNEL",
        color=discord.Color.blue()
    )
    embed.add_field(name="Guild ID", value=str(guild.id), inline=True)
    embed.add_field(name="Notice Channel ID", value=str(channel_id), inline=True)
    payload = {"guild_id": guild.id, "channel_id": channel_id}
    embed.set_footer(text=f"VOICE_CONFIG:{json.dumps(payload, ensure_ascii=False)}")
    await channel.send(embed=embed)
    VOICE_NOTICE_CHANNELS[guild.id] = channel_id

# --- [개선 ①] DB 채널 전송 성공 후 메모리 플래그 갱신 ---
async def log_audit_flag(guild: discord.Guild, flag_type: str, date_str: str):
    if WARN_LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(WARN_LOG_CHANNEL_ID)
        except Exception:
            return

    embed = discord.Embed(
        title=f"📋 [정산 플래그 로그] {flag_type.upper()}",
        color=discord.Color.dark_purple()
    )
    embed.add_field(name="Guild", value=guild.name, inline=True)
    embed.add_field(name="Type", value=flag_type, inline=True)
    embed.add_field(name="Date", value=date_str, inline=True)
    payload = {"guild_id": guild.id, "type": flag_type, "date": date_str}
    embed.set_footer(text=f"AUDIT_FLAG:{json.dumps(payload, ensure_ascii=False)}")

    # DB 전송 성공 후 메모리 플래그 설정
    await channel.send(embed=embed)

    if guild.id not in SERVER_AUDIT_FLAGS:
        SERVER_AUDIT_FLAGS[guild.id] = {}
    SERVER_AUDIT_FLAGS[guild.id][flag_type] = date_str


# --- 👥 음성 채널 인원(최소 2인 이상) 상태 점검 및 세션 갱신 헬퍼 ---
async def update_voice_channel_members(channel: Optional[discord.VoiceChannel]):
    if not channel:
        return

    now = datetime.now(KST)
    human_members = [m for m in channel.members if not m.bot]
    has_enough_members = len(human_members) >= 2

    for m in human_members:
        if not is_target_member(m):
            continue

        is_deaf = m.voice.self_deaf or m.voice.deaf if m.voice else False
        should_track = has_enough_members and not is_deaf

        sess = VOICE_ACTIVE_SESSIONS.get(m.id)

        # 1. 측정 중이었는데 멈춰야 하는 경우 (인원 1명으로 감소 or 헤드셋 끔)
        if sess and sess["active"] and not should_track:
            if sess["start_time"]:
                duration = int((now - sess["start_time"]).total_seconds())
                date_str = sess["start_time"].strftime("%Y-%m-%d")
                await log_voice_duration(channel.guild, m.id, date_str, duration)
            sess["active"] = False
            sess["start_time"] = None

        # 2. 멈춰있었는데 측정을 시작해야 하는 경우 (2명 이상 충족 & 헤드셋 끔 해제)
        elif should_track and (not sess or not sess["active"]):
            VOICE_ACTIVE_SESSIONS[m.id] = {
                "active": True,
                "start_time": now,
                "guild_id": channel.guild.id
            }


# --- 🎙️ 통화방 출결 및 상태 변경 리스너 ---
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

    now = datetime.now(KST)

    # 1. 방 퇴장
    if before.channel and not after.channel:
        if member.id in VOICE_ACTIVE_SESSIONS:
            sess = VOICE_ACTIVE_SESSIONS.pop(member.id)
            if sess["active"] and sess["start_time"]:
                duration = int((now - sess["start_time"]).total_seconds())
                date_str = sess["start_time"].strftime("%Y-%m-%d")
                await log_voice_duration(member.guild, member.id, date_str, duration)
        await update_voice_channel_members(before.channel)
        return

    # 2. 방 입장
    if not before.channel and after.channel:
        await update_voice_channel_members(after.channel)
        return

    # 3. 방 이동 또는 헤드셋 끔 상태 변경
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


# --- 🏷️ 역할 변경 시 통화 세션 즉시 정산 및 종료 ---
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


# --- 🕛 밤 9시(사전 경고) & 자정(확정 정산) 루프 [개선 ②: 일일/주간 완전 분리] ---
@tasks.loop(minutes=1)
async def daily_voice_check():
    now = datetime.now(KST)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    exact_midnight = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=KST)

    for guild in bot.guilds:
        try:
            if guild.id not in VOICE_DAILY_CACHE:
                _, v_records = await fetch_database_from_channel(guild)
                VOICE_DAILY_CACHE[guild.id] = v_records

            if guild.id not in SERVER_AUDIT_FLAGS:
                SERVER_AUDIT_FLAGS[guild.id] = {}

            guild_flags = SERVER_AUDIT_FLAGS[guild.id]

            # 서버별 알림 채널 조회
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

            if not channel:
                continue

            guild_data = VOICE_DAILY_CACHE.get(guild.id, {})
            target_members = [m for m in guild.members if not m.bot and is_target_member(m)]

            # -------------------------------------------------------------
            # 1. 밤 9시 사전 알림 (21:00~23:59 사이, 서버별 1회 보장)
            # -------------------------------------------------------------
            if now.hour >= 21 and now.hour < 24 and guild_flags.get("night") != today_str:
                past_7_days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
                pending_members = []
                mention_list = []

                for member in target_members:
                    today_spent = guild_data.get(today_str, {}).get(str(member.id), 0)
                    if member.id in VOICE_ACTIVE_SESSIONS and VOICE_ACTIVE_SESSIONS[member.id]["active"]:
                        today_spent += int((now - VOICE_ACTIVE_SESSIONS[member.id]["start_time"]).total_seconds())

                    week_spent = 0
                    for d_str in past_7_days:
                        week_spent += guild_data.get(d_str, {}).get(str(member.id), 0)
                    if member.id in VOICE_ACTIVE_SESSIONS and VOICE_ACTIVE_SESSIONS[member.id]["active"]:
                        week_spent += int((now - VOICE_ACTIVE_SESSIONS[member.id]["start_time"]).total_seconds())

                    if today_spent < 3600:
                        needed_min = max(0, (3600 - today_spent) // 60)
                        pending_members.append(
                            f"• {member.mention} ({member.display_name}) - 오늘 `{format_seconds(today_spent)}` (앞으로 `{needed_min}분` 필요 / 이번 주: `{format_seconds(week_spent)}`)"
                        )
                        mention_list.append(member.mention)

                if pending_members:
                    embed_reminder = discord.Embed(
                        title="⏰ [통화방 출결 사전 알림] 자정 마감 전 리마인드!",
                        description=(
                            "오늘 자정(24:00)까지 **최소 1시간** 통화방 참여를 완료해야 합니다!\n"
                            "아직 1시간을 채우지 못한 분들은 자정 전에 통화방에 접속해 주세요.\n\n"
                            "*(※ 통화방에 최소 2명 이상 있어야 하며, 혼자 있거나 헤드셋을 끈 상태는 인정되지 않습니다!)*"
                        ),
                        color=discord.Color.gold()
                    )
                    cut_list = pending_members[:25]
                    text = "\n".join(cut_list)
                    if len(pending_members) > 25:
                        text += f"\n...외 {len(pending_members) - 25}명"
                    embed_reminder.add_field(name=f"⚠️ 오늘 참여 시간 부족 ({len(pending_members)}명)", value=text, inline=False)
                    embed_reminder.set_footer(text="매일 밤 9시 자동 발송")

                    mentions_pings = " ".join(mention_list[:40])
                    await channel.send(content=f"📢 {mentions_pings}", embed=embed_reminder)

                await log_audit_flag(guild, "night", today_str)

            # -------------------------------------------------------------
            # 2. 자정 일일 정산 (00:00~00:59 사이, 서버별 1회 보장)
            # -------------------------------------------------------------
            if now.hour == 0 and guild_flags.get("daily") != yesterday_str:
                # 접속 중 세션 자정 분할
                for u_id, sess in list(VOICE_ACTIVE_SESSIONS.items()):
                    if sess["active"] and sess["start_time"] and sess["guild_id"] == guild.id:
                        before_midnight = int((exact_midnight - sess["start_time"]).total_seconds())
                        if before_midnight > 0:
                            await log_voice_duration(guild, u_id, yesterday_str, before_midnight)
                        sess["start_time"] = exact_midnight

                yest_records = guild_data.get(yesterday_str, {})
                daily_failed = []
                daily_pings = []
                for member in target_members:
                    spent = yest_records.get(str(member.id), 0)
                    if spent < 3600:
                        daily_failed.append(f"• {member.mention} ({member.display_name}) - `{format_seconds(spent)}` / 1시간")
                        daily_pings.append(member.mention)

                embed_daily = discord.Embed(
                    title=f"📋 [일일 통화시간 정산] {yesterday_str}",
                    description="길드원/수습생 대상 하루 최소 **1시간** 통화방 참여 미달자 명단입니다.",
                    color=discord.Color.red() if daily_failed else discord.Color.green()
                )
                if daily_failed:
                    cut_list = daily_failed[:30]
                    more_cnt = len(daily_failed) - 30
                    embed_text = "\n".join(cut_list)
                    if more_cnt > 0:
                        embed_text += f"\n...외 {more_cnt}명"
                    embed_daily.add_field(name=f"🚨 일일 미달 확정자 (총 {len(daily_failed)}명)", value=embed_text, inline=False)
                    await channel.send(content=" ".join(daily_pings[:40]), embed=embed_daily)
                else:
                    embed_daily.add_field(name="🎉 전원 달성!", value="어제 모든 대상 멤버가 1시간 이상 통화방에 참여했습니다.", inline=False)
                    await channel.send(embed=embed_daily)

                await log_audit_flag(guild, "daily", yesterday_str)

            # -------------------------------------------------------------
            # 3. [개선 ②] 매주 월요일 주간 정산 (독립 실행 블록)
            # -------------------------------------------------------------
            if now.hour == 0 and now.weekday() == 0 and guild_flags.get("weekly") != yesterday_str:
                weekly_failed = []
                weekly_pings = []
                past_7_days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]

                for member in target_members:
                    week_spent = 0
                    for d_str in past_7_days:
                        week_spent += guild_data.get(d_str, {}).get(str(member.id), 0)

                    if week_spent < 39600:
                        weekly_failed.append(f"• {member.mention} ({member.display_name}) - `{format_seconds(week_spent)}` / 11시간")
                        weekly_pings.append(member.mention)

                embed_weekly = discord.Embed(
                    title="📊 [주간 통화시간 결산] 지난주 종합",
                    description="길드원/수습생 대상 최근 7일 최소 **11시간** 통화방 참여 미달자 명단입니다.",
                    color=discord.Color.dark_red() if weekly_failed else discord.Color.blue()
                )
                if weekly_failed:
                    cut_w = weekly_failed[:30]
                    more_w = len(weekly_failed) - 30
                    w_text = "\n".join(cut_w)
                    if more_w > 0:
                        w_text += f"\n...외 {more_w}명"
                    embed_weekly.add_field(name=f"🚨 주간 미달 확정자 (총 {len(weekly_failed)}명)", value=w_text, inline=False)
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
        new_w_role = discord.utils.get(guild.roles, name=weapon_name)
        if not new_w_role:
            try:
                new_w_role = await guild.create_role(name=weapon_name)
            except discord.Forbidden:
                new_w_role = None
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
            new_j_role = discord.utils.get(guild.roles, name=job_name)
            if not new_j_role:
                try:
                    new_j_role = await guild.create_role(name=job_name)
                except discord.Forbidden:
                    new_j_role = None
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

    chosen_tag = None
    if active_job and active_job in JOBS:
        chosen_tag = active_job
    elif active_weapon:
        chosen_tag = active_weapon

    prefix = f"[{chosen_tag}] " if chosen_tag else ""
    cleaned_name = re.sub(r'^\[.*?\]\s*', '', member.display_name).strip()
    new_nickname = f"{prefix}{cleaned_name}"

    if len(new_nickname) > 32:
        new_nickname = new_nickname[:32]

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

# --- ⚔️ 무기 & 직업 드롭다운 View (영구 View: 고유 custom_id 명시) ---
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

# --- 📝 모달 UI ---
class ProfileModal(discord.ui.Modal, title="자기소개 입력"):
    name = discord.ui.TextInput(label="이름 (또는 별명)", placeholder="예: 쨈", required=True, max_length=15)
    mc_name = discord.ui.TextInput(label="마인크래프트 닉네임", placeholder="예: _s2_jammy", required=True, max_length=25)
    guild_name = discord.ui.TextInput(label="길드 이름", placeholder="예: 월영", required=True, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        current_nick = user.display_name

        prefix_match = re.match(r'^(\[.*?\])\s*', current_nick)
        prefix = f"{prefix_match.group(1)} " if prefix_match else ""

        new_nick = f"{prefix}{self.name.value.strip()} / {self.mc_name.value.strip()} / {self.guild_name.value.strip()}"
        if len(new_nick) > 32:
            new_nick = new_nick[:32]

        try:
            await user.edit(nick=new_nick)
            nickname_msg = f"닉네임이 **{new_nick}**(으)로 설정되었습니다!"
        except discord.Forbidden:
            nickname_msg = "⚠️ 서버장 또는 봇보다 높은 역할이라 닉네임 자동 변경이 불가능합니다."

        await interaction.followup.send(f"✅ 등록이 완료되었습니다!\n{nickname_msg}", ephemeral=True)

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


# --- 슬래시 명령어 세트 ---

# 📢 [공지 이모지 미반응자 확인]
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

    embed = discord.Embed(
        title="📢 [공지 이모지 확인 현황]",
        description=f"🔗 [해당 공지 바로가기]({msg_link})\n📝 **공지 요약:** {summary_content or '(임베드 공지)'}",
        color=discord.Color.orange() if unreacted_members else discord.Color.green()
    )
    embed.add_field(name="👥 전체 대상 인원", value=f"{len(target_members)}명", inline=True)
    embed.add_field(name="✅ 확인 (이모지 등록)", value=f"{len(target_members) - len(unreacted_members)}명", inline=True)
    embed.add_field(name="❌ 미확인 (미반응)", value=f"**{len(unreacted_members)}명**", inline=True)

    if unreacted_members:
        unreacted_lines = [f"• {m.mention} ({m.display_name})" for m in unreacted_members]
        cut_text = "\n".join(unreacted_lines[:35])
        if len(unreacted_members) > 35:
            cut_text += f"\n...외 {len(unreacted_members) - 35}명"
        embed.add_field(name="⚠️ 미반응 멤버 명단", value=cut_text, inline=False)
    else:
        embed.add_field(name="🎉 전원 확인 완료!", value="모든 길드원/수습생이 공지에 이모지를 남겼습니다.", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

# 🪜 [사다리 타기]
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

# 🎙️ [통화 시간 조회]
@bot.tree.command(name="통화시간", description="오늘의 통화방 참여 시간 및 이번 주 누적 시간을 확인합니다.")
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

    if target.id in VOICE_ACTIVE_SESSIONS:
        sess = VOICE_ACTIVE_SESSIONS[target.id]
        if sess["active"] and sess["start_time"]:
            cur_diff = int((now - sess["start_time"]).total_seconds())
            today_sec += cur_diff
            current_status = f"🟢 참여 중 (+{format_seconds(cur_diff)})"
        else:
            if target.voice and target.voice.channel:
                humans = [m for m in target.voice.channel.members if not m.bot]
                if len(humans) < 2:
                    current_status = "⏸️ 혼자 접속 중 (2명 이상 모여야 측정)"
                else:
                    current_status = "⏸️ 헤드셋 끔 (잠수 중 / 측정 일시정지)"
            else:
                current_status = "⏸️ 측정 대기 중"

    past_7_days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    week_sec = sum(guild_data.get(d_str, {}).get(u_str, 0) for d_str in past_7_days)
    if target.id in VOICE_ACTIVE_SESSIONS and VOICE_ACTIVE_SESSIONS[target.id]["active"]:
        week_sec += int((now - VOICE_ACTIVE_SESSIONS[target.id]["start_time"]).total_seconds())

    embed = discord.Embed(title=f"🎙️ [{target.display_name}] 님의 통화방 시간표", color=discord.Color.teal())
    embed.add_field(name="📅 오늘 참여 시간", value=f"**{format_seconds(today_sec)}** (기준: 1시간)", inline=True)
    embed.add_field(name="📊 최근 7일 누적", value=f"**{format_seconds(week_sec)}** (기준: 11시간)", inline=True)
    embed.add_field(name="📌 실시간 상태", value=current_status, inline=False)
    embed.set_footer(text="통화방에 2명 이상 모여야 측정되며, 혼자 있거나 헤드셋을 끄면 측정되지 않습니다.")
    await interaction.followup.send(embed=embed, ephemeral=True)

# 🎙️ [통화 기준 미달자 확인 - 관리자용]
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
            spent = today_records.get(str(member.id), 0)
            if member.id in VOICE_ACTIVE_SESSIONS and VOICE_ACTIVE_SESSIONS[member.id]["active"]:
                spent += int((now - VOICE_ACTIVE_SESSIONS[member.id]["start_time"]).total_seconds())

            if spent < 3600:
                failed_lines.append(f"• {member.mention} ({member.display_name}) : `{format_seconds(spent)}`")

        title = f"📋 [오늘 길드 통화방 1시간 미달자] ({len(failed_lines)}명)"
    else:
        past_7_days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        for member in target_members:
            spent = sum(guild_data.get(d_str, {}).get(str(member.id), 0) for d_str in past_7_days)
            if member.id in VOICE_ACTIVE_SESSIONS and VOICE_ACTIVE_SESSIONS[member.id]["active"]:
                spent += int((now - VOICE_ACTIVE_SESSIONS[member.id]["start_time"]).total_seconds())

            if spent < 39600:
                failed_lines.append(f"• {member.mention} ({member.display_name}) : `{format_seconds(spent)}`")

        title = f"📊 [최근 7일 통화방 11시간 미달자] ({len(failed_lines)}명)"

    if not failed_lines:
        await interaction.followup.send("🎉 전원 기준 달성 완료! 미달자가 없습니다.", ephemeral=True)
        return

    embed = discord.Embed(title=title, description="\n".join(failed_lines[:40]), color=discord.Color.red())
    if len(failed_lines) > 40:
        embed.set_footer(text=f"외 {len(failed_lines) - 40}명 생략됨")
    await interaction.followup.send(embed=embed, ephemeral=True)

# 🎙️ [통화 알림 채널 설정 - 관리자 전용 / 서버별 분리]
@bot.tree.command(name="통화알림채널설정", description="[관리자 전용] 밤 9시 사전 알림 및 자정 정산 브리핑이 전송될 채널을 설정합니다.")
@app_commands.describe(채널="브리핑이 올라갈 텍스트 채널")
async def set_voice_notice_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    await log_voice_config(interaction.guild, 채널.id)
    await interaction.response.send_message(f"✅ 매일 밤 9시 사전 태그 및 자정 미달자 브리핑이 {채널.mention} 채널로 전송됩니다!")

@bot.tree.command(name="자기소개", description="이름, 마크 닉네임, 길드 이름을 입력하여 닉네임을 설정합니다.")
async def profile(interaction: discord.Interaction):
    await interaction.response.send_modal(ProfileModal())

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

# 📢 [공지 작성 - 관리자 전용]
@bot.tree.command(name="공지", description="[관리자 전용] 팝업창을 열어 공지사항을 작성합니다.")
async def create_notice(interaction: discord.Interaction):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 공지를 작성할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.send_modal(NoticeModal())

# 🧹 [청소 - 관리자 전용]
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

# 📋 [멤버 멘션 추출 - 관리자 전용]
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

# ⚠️ [경고 부여 - 관리자 전용]
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

    embed = discord.Embed(
        title="⚠️ 경고가 부여되었습니다",
        description=f"{유저.mention} 님에게 경고가 1회 누적되었습니다.",
        color=discord.Color.red()
    )
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
    embed = discord.Embed(
        title=f"📋 [{target.display_name}] 님의 경고 내역",
        description=f"총 누적: **{count}회**\n\n" + "\n".join(lines),
        color=discord.Color.orange()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

# 🚨 [경고 목록 - 관리자 전용]
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

    embed = discord.Embed(
        title="🚨 [관리자] 서버 경고 누적 유저 목록",
        description=f"총 **{len(warn_lines)}명**의 유저가 경고를 보유 중입니다.\n\n" + "\n".join(warn_lines),
        color=discord.Color.red()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

# 🔽 [경고 차감 - 음수 방지 및 관리자 전용]
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

# 🧹 [경고 초기화 - 관리자 전용]
@bot.tree.command(name="경고초기화", description="[관리자 전용] 경고를 초기화합니다.")
@app_commands.describe(유저="대상 멤버")
async def clear_warn(interaction: discord.Interaction, 유저: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
        
    await log_warning_action(interaction.guild, 유저.id, "clear", {})
    await interaction.followup.send(f"🧹 **{유저.display_name}** 님의 모든 경고가 초기화되었습니다.", ephemeral=True)


# --- 🚨 슬래시 명령어 전역 오류 핸들러 ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    err_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    await send_error_log(interaction.guild, f"Command Error: /{interaction.command.name if interaction.command else 'Unknown'}", err_text)
    
    msg = f"❌ 명령어 실행 중 오류가 발생했습니다: `{error}`"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# --- 🚀 [보안 강화] 관리자 전용 단일 서버 직접 동기화 & 전역 찌꺼기 정리 ---
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


# --- 🌟 봇 시작 이벤트 (영구 View 등록 및 세션 보존) ---
@bot.event
async def on_ready():
    bot.add_view(LoadoutSelectView())

    if not daily_voice_check.is_running():
        daily_voice_check.start()

    await asyncio.sleep(2)
    now = datetime.now(KST)

    for guild in bot.guilds:
        try:
            _, v_records = await fetch_database_from_channel(guild)
            VOICE_DAILY_CACHE[guild.id] = v_records

            for vc in guild.voice_channels:
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
