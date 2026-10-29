import os
import re
import json
import random
import threading
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from flask import Flask
import discord
from discord.ext import commands
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

# 🚨 [중요] 경고 데이터를 영구 보관할 비공개 채널 ID
WARN_LOG_CHANNEL_ID = 1542004718606487672

# --- 보부상 타이머 메모리 관리 ---
MERCHANT_TASKS: Dict[str, asyncio.Task] = {}
MERCHANT_DATA: Dict[str, dict] = {}

# --- 채널 기반 영구 경고 데이터 관리 ---
async def fetch_warnings_from_channel(guild: discord.Guild) -> dict:
    if WARN_LOG_CHANNEL_ID == 0:
        return {}
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await guild.fetch_channel(WARN_LOG_CHANNEL_ID)
        except Exception:
            return {}

    warnings = {}
    async for message in channel.history(limit=500, oldest_first=True):
        if message.author == bot.user and message.embeds:
            for embed in message.embeds:
                if embed.footer and embed.footer.text and embed.footer.text.startswith("WARN_DATA:"):
                    try:
                        raw_data = embed.footer.text.replace("WARN_DATA:", "").strip()
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
    return warnings

async def log_warning_action(guild: discord.Guild, user_id: int, action: str, data: dict):
    if WARN_LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(WARN_LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await guild.fetch_channel(WARN_LOG_CHANNEL_ID)
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

def is_admin_or_mod(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild or perms.moderate_members or perms.kick_members

def find_guild_member(guild: discord.Guild, query: str) -> Optional[discord.Member]:
    query = query.strip()
    if not query:
        return None
        
    match = re.match(r'<@!?(\d+)>', query)
    if match:
        return guild.get_member(int(match.group(1)))
        
    query_lower = query.lower()
    for m in guild.members:
        if m.bot:
            continue
        if query_lower in m.display_name.lower() or query_lower in m.name.lower():
            return m
    return None

# --- 무기 및 직업 목록 ---
WEAPONS = ["검", "창", "도끼", "활", "클로", "메이스"]
JOBS = ["요리사", "대장장이", "낚시꾼"]

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

    current_weapon = weapon_name
    current_job = job_name

    if not current_weapon:
        for w in WEAPONS:
            r = discord.utils.get(guild.roles, name=w)
            if r and r in member.roles:
                current_weapon = w
                break

    if not current_job:
        for j in JOBS:
            r = discord.utils.get(guild.roles, name=j)
            if r and r in member.roles:
                current_job = j
                break

    tag_parts = [p for p in [current_weapon, current_job] if p]
    prefix = f"[{'/'.join(tag_parts)}] " if tag_parts else ""

    original_name = member.display_name
    cleaned_name = re.sub(r'^\[.*?\]\s*', '', original_name).strip()
    new_nickname = f"{prefix}{cleaned_name}"

    if len(new_nickname) > 32:
        new_nickname = new_nickname[:32]

    try:
        await member.edit(nick=new_nickname)
        nickname_msg = f"닉네임이 **{new_nickname}**(으)로 변경되었습니다!"
    except discord.Forbidden:
        nickname_msg = "(⚠️ 서버장 또는 봇보다 높은 역할이라 닉네임 자동 변경 불가)"

    summary = []
    if current_weapon:
        summary.append(f"무기: **{current_weapon}**")
    if current_job:
        summary.append(f"직업: **{current_job}**")

    await interaction.followup.send(
        f"✅ 설정 완료! ({', '.join(summary)})\n{nickname_msg}",
        ephemeral=True
    )


# --- ⚔️ 무기 & 직업 드롭다운 View ---
class WeaponSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=w, description=f"{w} 무기 선택") for w in WEAPONS]
        super().__init__(placeholder="🗡️ 무기를 선택하세요", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_weapon = self.values[0]
        await interaction.response.send_message(f"무기 **[{self.values[0]}]**(을)를 선택하셨습니다. (선택 완료 버튼을 눌러 확정하세요)", ephemeral=True)

class JobSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=j, description=f"{j} 직업 선택") for j in JOBS]
        super().__init__(placeholder="🛠️ 직업을 선택하세요", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_job = self.values[0]
        await interaction.response.send_message(f"직업 **[{self.values[0]}]**(을)를 선택하셨습니다. (선택 완료 버튼을 눌러 확정하세요)", ephemeral=True)

class LoadoutConfirmButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✅ 선택 완료 및 적용", style=discord.ButtonStyle.success, row=2)

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


# --- 📝 자기소개 모달 UI (소개글 전송 X, 역할 자동부여 X, 닉네임만 변경) ---
class ProfileModal(discord.ui.Modal, title="자기소개 입력"):
    name = discord.ui.TextInput(label="이름 (또는 별명)", placeholder="예: 쨈", required=True, max_length=15)
    mc_name = discord.ui.TextInput(label="마인크래프트 닉네임", placeholder="예: _s2_jammy", required=True, max_length=25)
    guild_name = discord.ui.TextInput(label="길드 이름", placeholder="예: 월영", required=True, max_length=20)
    gender = discord.ui.TextInput(label="성별 (선택)", placeholder="예: 남자 / 여자", required=False, max_length=10)

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

        await interaction.followup.send(
            f"✅ 등록이 완료되었습니다!\n{nickname_msg}", 
            ephemeral=True
        )


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


# 🎯 대규모 인원 추첨 팝업창 모달
class LotteryModal(discord.ui.Modal, title="🎯 대규모 인원 추첨"):
    event_name = discord.ui.TextInput(label="이벤트명", placeholder="예: 균열석 기부자 추첨", required=True, max_length=50)
    winner_count = discord.ui.TextInput(label="당첨 인원수 (숫자만)", placeholder="예: 5", required=True, max_length=4)
    participants_input = discord.ui.TextInput(
        label="참여자 목록 (닉네임/멘션, 줄바꿈 또는 쉼표)", 
        placeholder="루트, 김민수, 우기\n또는 멘션 나열",
        style=discord.TextStyle.paragraph, 
        required=True, 
        max_length=4000
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        try:
            w_count = int(self.winner_count.value.strip())
        except ValueError:
            await interaction.followup.send("❌ 당첨 인원수에는 숫자만 입력해 주세요!", ephemeral=True)
            return

        raw_names = [name.strip() for name in re.split(r'[,\n]', self.participants_input.value) if name.strip()]
        participants = []
        for name in raw_names:
            mem = find_guild_member(interaction.guild, name)
            if mem and mem not in participants:
                participants.append(mem)

        total_count = len(participants)
        if total_count < 2:
            await interaction.followup.send("❌ 최소 2명 이상의 유저를 입력해 주세요!", ephemeral=True)
            return

        if w_count <= 0 or w_count > total_count:
            await interaction.followup.send(f"❌ 당첨 인원은 1명 이상, 입력된 총 인원({total_count}명) 이하여야 합니다.", ephemeral=True)
            return

        winners = random.sample(participants, w_count)
        winner_mentions = [w.mention for w in winners]

        embed = discord.Embed(
            title=f"🎉 [추첨 결과] {self.event_name.value}",
            description=f"총 **{total_count}명** 중 **{w_count}명**이 당첨되었습니다!\n\n👑 **당첨자 명단:**\n" + "\n".join([f"• {w.mention} ({w.display_name})" for w in winners]),
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"주최자: {interaction.user.display_name}")

        await interaction.followup.send(content=" ".join(winner_mentions), embed=embed)


class PollButton(discord.ui.Button):
    def __init__(self, label: str, index: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=f"poll_opt_{index}")
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        view: PollView = self.view
        user_id = interaction.user.id
        view.votes[user_id] = self.index
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class PollView(discord.ui.View):
    def __init__(self, question: str, options: List[str], author: discord.Member):
        super().__init__(timeout=None)
        self.question = question
        self.options = options
        self.author = author
        self.votes: Dict[int, int] = {}
        for idx, opt in enumerate(options):
            self.add_item(PollButton(label=f"{idx+1}. {opt}", index=idx))

    def make_embed(self) -> discord.Embed:
        total_votes = len(self.votes)
        counts = [0] * len(self.options)
        for opt_idx in self.votes.values():
            counts[opt_idx] += 1

        desc_lines = []
        for idx, (opt, count) in enumerate(zip(self.options, counts)):
            pct = (count / total_votes * 100) if total_votes > 0 else 0
            bar_len = int(pct / 10)
            bar = "█" * bar_len + "░" * (10 - bar_len)
            desc_lines.append(f"**{idx+1}. {opt}**\n`{bar}` **{count}표** ({pct:.1f}%)")

        embed = discord.Embed(title=f"📊 투표: {self.question}", description="\n\n".join(desc_lines), color=discord.Color.teal())
        embed.set_footer(text=f"주최:
