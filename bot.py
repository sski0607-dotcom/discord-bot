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

# 🚨 [중요] 경고, 통화 기록, 관리자 승인 검토 채널 ID
WARN_LOG_CHANNEL_ID = 1569705414839242884

# 📢 공지 확인 대상 채널 ID
ANNOUNCEMENT_CHANNEL_ID = 1559080610335817779

# 👋 신규 멤버 환영 메시지 설정
# Render 환경변수 또는 .env에 WELCOME_CHANNEL_ID를 설정하세요.
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "1559051383980818533"))
WELCOME_TEXT = os.getenv("WELCOME_TEXT", "두 개의 길드, 하나의 밤.")

# 💤 [중요] 시간 측정을 제외할 잠수방 음성 채널 ID
AFK_VOICE_CHANNEL_ID = 1569698118004514836

# 🎫 승인 시 기본으로 부여할 역할 이름
DEFAULT_VERIFIED_ROLE = "월영 길드원"

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

# 통화방 세션 메모리 {user_id: {"active": bool, "start_time": datetime, "guild_id": int}}
VOICE_ACTIVE_SESSIONS: Dict[int, dict] = {}
# DB에서 복원된 누적 통화 시간 {guild_id: {date_str(YYYY-MM-DD): {user_id_str: seconds}}}
VOICE_DAILY_CACHE: Dict[int, Dict[str, Dict[str, int]]] = {}

# 서버별 정산 실행 플래그 {guild_id: {"night": date_str, "daily": date_str, "weekly": date_str}}
SERVER_AUDIT_FLAGS: Dict[int, Dict[str, str]] = {}

def is_admin_or_mod(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions
