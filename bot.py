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
DEFAULT_MERCHANT_CHANNEL_ID: Optional[int] = None

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

    # 1. 무기 역할 갱신
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

    # 2. 직업 역할 갱신
    if job_name:
        for j in JOBS:
            r = discord.utils.get(guild.roles, name=j)
            if r and r in member.roles:
                try:
                    await member.remove_roles(r)
                except discord.Forbidden:
                    pass
        
        # '없음 (전투직)'이 아닌 경우에만 새 역할 부여
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

    # 3. 현재 무기/직업 상태 파악
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

    # 4. 닉네임 태그 우선순위 판별 (직업 우선 -> 직업 없으면 무기)
    chosen_tag = None
    if active_job and active_job in JOBS:
        chosen_tag = active_job
    elif active_weapon:
        chosen_tag = active_weapon

    prefix = f"[{chosen_tag}] " if chosen_tag else ""

    # 기존 닉네임에서 앞의 [태그] 부분만 안전하게 교체
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


# --- ⚔️ 무기 & 직업 드롭다운 View ---
class WeaponSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=w, description=f"{w} 무기 선택") for w in WEAPONS]
        super().__init__(placeholder="🗡️ 무기를 선택하세요", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_weapon = self.values[0]
        await interaction.response.send_message(f"무기 **[{self.values[0]}]**(을)를 선택하셨습니다.", ephemeral=True)

class JobSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="없음 (전투직)", description="생활직업 없음 (무기 태그 우선 적용)", value="없음 (전투직)")
        ] + [discord.SelectOption(label=j, description=f"{j} 직업 선택", value=j) for j in JOBS]
        super().__init__(placeholder="🛠️ 직업을 선택하세요", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_job = self.values[0]
        await interaction.response.send_message(f"직업 **[{self.values[0]}]**(을)를 선택하셨습니다.", ephemeral=True)

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


# --- 📝 자기소개 모달 UI ---
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
        embed.set_footer(text=f"주최: {self.author.display_name} • 총 {total_votes}명 참여 (버튼으로 투표/변경)")
        return embed


# --- 은행 대여 반납 버튼 UI ---
class DynamicLoanButton(discord.ui.DynamicItem[discord.ui.Button], template=r'loan_return:(?P<lender_id>[0-9]+):(?P<borrower_id>[0-9]+)'):
    def __init__(self, lender_id: int, borrower_id: int):
        super().__init__(
            discord.ui.Button(
                label="반납 완료",
                style=discord.ButtonStyle.success,
                emoji="✅",
                custom_id=f"loan_return:{lender_id}:{borrower_id}"
            )
        )
        self.lender_id = lender_id
        self.borrower_id = borrower_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        lender_id = int(match.group("lender_id"))
        borrower_id = int(match.group("borrower_id"))
        return cls(lender_id, borrower_id)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.lender_id and interaction.user.id != self.borrower_id and not is_admin_or_mod(interaction):
            await interaction.response.send_message("❌ 빌려준 사람, 빌린 사람 또는 관리자만 반납 처리를 할 수 있습니다.", ephemeral=True)
            return

        if not interaction.message.embeds:
            await interaction.response.send_message("❌ 메시지 정보를 읽을 수 없습니다.", ephemeral=True)
            return

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        orig_embed = interaction.message.embeds[0]
        new_embed = orig_embed.copy()
        new_embed.color = discord.Color.green()
        new_embed.title = "🏦 [은행] 상환 완료"
        
        for i, field in enumerate(new_embed.fields):
            if field.name == "📌 상태":
                new_embed.set_field_at(i, name="📌 상태", value="✅ **반납 완료**", inline=True)
                break
                
        new_embed.add_field(name="🎉 반납 일시", value=f"{now_str} (확인: {interaction.user.display_name})", inline=False)
        
        self.item.disabled = True
        self.item.label = "반납 완료됨"
        
        view = discord.ui.View(timeout=None)
        view.add_item(self.item)

        await interaction.response.edit_message(embed=new_embed, view=view)
        await interaction.followup.send(f"✅ **{interaction.user.display_name}** 님이 상환 완료 처리했습니다!", ephemeral=False)


class LoanView(discord.ui.View):
    def __init__(self, lender_id: int, borrower_id: int):
        super().__init__(timeout=None)
        self.add_item(DynamicLoanButton(lender_id, borrower_id))


# --- 정해진 수량만큼 뽑히는 쿠지 데이터 및 UI ---
KUJI_GAMES: Dict[str, dict] = {}

class DynamicKujiButton(discord.ui.DynamicItem[discord.ui.Button], template=r'kuji_draw:(?P<game_id>[a-zA-Z0-9_-]+)'):
    def __init__(self, game_id: str):
        super().__init__(
            discord.ui.Button(
                label="🎫 쿠지 뽑기!",
                style=discord.ButtonStyle.primary,
                emoji="🎁",
                custom_id=f"kuji_draw:{game_id}"
            )
        )
        self.game_id = game_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        game_id = match.group("game_id")
        return cls(game_id)

    async def callback(self, interaction: discord.Interaction):
        game = KUJI_GAMES.get(self.game_id)
        if not game:
            await interaction.response.send_message("❌ 진행 중인 쿠지 판 정보를 찾을 수 없습니다.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id not in game["allowed_users"]:
            await interaction.response.send_message("❌ 참여 대상자가 아닙니다!", ephemeral=True)
            return

        remaining_tickets = game["allowed_users"][user_id]
        if remaining_tickets <= 0:
            await interaction.response.send_message("❌ 보유한 뽑기 티켓을 모두 사용하셨습니다!", ephemeral=True)
            return

        if len(game["box"]) == 0:
            await interaction.response.send_message("❌ 모든 쿠지가 이미 소진되었습니다!", ephemeral=True)
            return

        game["allowed_users"][user_id] -= 1
        picked_prize = game["box"].pop(random.randint(0, len(game["box"]) - 1))
        
        is_finished = len(game["box"]) == 0
        now_time = datetime.now().strftime("%H:%M")
        
        record = {
            "user_id": user_id,
            "user_name": interaction.user.display_name,
            "prize": picked_prize,
            "time": now_time
        }
        game["history"].append(record)

        embed = self.build_embed(game)
        
        if is_finished:
            self.item.disabled = True
            self.item.label = "쿠지 매진 (종료)"
            self.item.style = discord.ButtonStyle.secondary

        view = discord.ui.View(timeout=None)
        view.add_item(self.item)

        await interaction.response.edit_message(embed=embed, view=view)
        
        lose_name = game.get("lose_name", "❌ 꽝")
        if picked_prize == lose_name or "꽝" in picked_prize:
            result_comment = f"💨 아쉽네요... {interaction.user.mention} 님은 **[{picked_prize}]** (다음 기회에!)"
        else:
            result_comment = f"🎉 축하합니다! {interaction.user.mention} 님이 **[{picked_prize}]** 에 당첨되셨습니다!"

        await interaction.followup.send(
            f"{result_comment} (남은 티켓: {game['allowed_users'][user_id]}장)",
            ephemeral=False
        )

    @staticmethod
    def build_embed(game: dict) -> discord.Embed:
        total_left = len(game["box"])
        total_init = game["total_initial"]
        
        prize_status = []
        for prize, count in game["initial_prizes"].items():
            left_count = game["box"].count(prize)
            prize_status.append(f"• **{prize}**: 남은 수량 `{left_count} / {count}`개")

        desc = (
            f"**📢 {game['title']}**\n"
            f"균열석 기부자 전용 뽑기판입니다!\n\n"
            f"📊 **전체 잔여 쿠지:** `{total_left} / {total_init}`장\n"
        )

        embed = discord.Embed(
            title="🎪 [균열석 기부자] 뽑기판",
            description=desc,
            color=discord.Color.gold() if total_left > 0 else discord.Color.dark_grey()
        )

        embed.add_field(name="🎁 상품 잔여 현황", value="\n".join(prize_status), inline=False)
        
        if game["history"]:
            recent_lines = [f"• **{r['user_name']}** ➔ **{r['prize']}** ({r['time']})" for r in game["history"][-5:]]
            embed.add_field(name="📜 최근 당첨 내역", value="\n".join(reversed(recent_lines)), inline=False)

        allowed_mentions = [f"<@{uid}>({cnt}회)" for uid, cnt in game["allowed_users"].items() if cnt > 0]
        if allowed_mentions:
            embed.add_field(name="🎫 참여 가능 기부자", value=" ".join(allowed_mentions), inline=False)
        else:
            embed.add_field(name="🎫 참여 상태", value="✅ 모든 기부자가 참여를 완료했습니다!", inline=False)

        embed.set_footer(text=f"주최: {game['host']} • 1회 뽑기당 티켓 1장 소모")
        return embed


class KujiCreateModal(discord.ui.Modal, title="🎪 균열석 기부자 뽑기판 생성"):
    kuji_title = discord.ui.TextInput(label="뽑기 제목", placeholder="예: 균열석 기부 감사 뽑기", required=True, max_length=50)
    prizes = discord.ui.TextInput(
        label="당첨 상품 라인업 (상품명:수량 줄바꿈)",
        placeholder="기어 4개:2\n기어 2개:2\n(※ 꽝은 적지 않아도 총 인원에 맞춰 자동 생성)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    allowed_donors = discord.ui.TextInput(
        label="참여 가능 기부자 목록 (닉네임/멘션, 줄바꿈 또는 쉼표)",
        placeholder="루트, 김민수, 우기\n또는 줄바꿈으로 나열",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000
    )
    default_lose_name = discord.ui.TextInput(label="꽝 상품 이름 (기본값: ❌ 꽝)", placeholder="❌ 꽝", default="❌ 꽝", required=False, max_length=30)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        raw_names = [name.strip() for name in re.split(r'[,\n]', self.allowed_donors.value) if name.strip()]
        matched_members = []
        for name in raw_names:
            mem = find_guild_member(interaction.guild, name)
            if mem and mem not in matched_members:
                matched_members.append(mem)

        if not matched_members:
            await interaction.followup.send("❌ 참여 가능한 기부자를 찾지 못했습니다. 닉네임이나 멘션을 확인해 주세요!", ephemeral=True)
            return

        total_required_tickets = len(matched_members)

        box = []
        initial_prizes = {}
        for line in self.prizes.value.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                parts = line.rsplit(":", 1)
                p_name = parts[0].strip()
                try:
                    p_count = int(parts[1].strip())
                except ValueError:
                    p_count = 1
            else:
                p_name = line
                p_count = 1

            initial_prizes[p_name] = p_count
            box.extend([p_name] * p_count)

        current_prize_count = len(box)
        lose_name = self.default_lose_name.value.strip() if self.default_lose_name.value else "❌ 꽝"

        if current_prize_count < total_required_tickets:
            remaining_lose_count = total_required_tickets - current_prize_count
            initial_prizes[lose_name] = initial_prizes.get(lose_name, 0) + remaining_lose_count
            box.extend([lose_name] * remaining_lose_count)
        elif current_prize_count > total_required_tickets:
            await interaction.followup.send(
                f"⚠️ 입력한 당첨 상품 총 개수({current_prize_count}개)가 참여자 인원수({total_required_tickets}명)보다 많습니다!",
                ephemeral=True
            )
            return

        game_id = f"kuji_{int(datetime.now().timestamp())}_{random.randint(100, 999)}"
        allowed_users = {m.id: 1 for m in matched_members}

        KUJI_GAMES[game_id] = {
            "title": self.kuji_title.value,
            "host": interaction.user.display_name,
            "box": box,
            "initial_prizes": initial_prizes,
            "total_initial": len(box),
            "allowed_users": allowed_users,
            "lose_name": lose_name,
            "history": []
        }

        embed = DynamicKujiButton.build_embed(KUJI_GAMES[game_id])
        view = discord.ui.View(timeout=None)
        view.add_item(DynamicKujiButton(game_id))

        mention_pings = " ".join([m.mention for m in matched_members])
        await interaction.followup.send(content=f"📢 **균열석 기부자 뽑기판이 열렸습니다!**\n{mention_pings}", embed=embed, view=view)


# --- 보부상 알림 비동기 태스크 ---
async def merchant_timer_task(channel: discord.TextChannel, location: str, target_time: datetime):
    # 1. 5분 전 알림 대기
    pre_notify_time = target_time - timedelta(minutes=5)
    now = datetime.now()
    wait_pre = (pre_notify_time - now).total_seconds()

    if wait_pre > 0:
        await asyncio.sleep(wait_pre)
        embed_pre = discord.Embed(
            title="⏰ [보부상] 등장 5분 전!",
            description=f"**[{location}]** 보부상이 약 **5분 뒤**에 도착합니다! 준비해 주세요.",
            color=discord.Color.yellow()
        )
        try:
            await channel.send(content="@everyone", embed=embed_pre)
        except Exception:
            pass

    # 2. 정각 알림 대기
    now = datetime.now()
    wait_final = (target_time - now).total_seconds()
    if wait_final > 0:
        await asyncio.sleep(wait_final)

    embed_arrive = discord.Embed(
        title="🔔 [보부상] 지금 등장!",
        description=f"**[{location}]**에 보부상이 도착했습니다! 지금 방문해 보세요.",
        color=discord.Color.green()
    )
    try:
        await channel.send(content="@everyone", embed=embed_arrive)
    except Exception:
        pass

    MERCHANT_TASKS.pop(location, None)
    MERCHANT_DATA.pop(location, None)


# --- 슬래시 명령어 ---

@bot.tree.command(name="자기소개", description="이름, 마크 닉네임, 길드 이름을 입력하여 닉네임을 설정합니다.")
async def profile(interaction: discord.Interaction):
    await interaction.response.send_modal(ProfileModal())

# ⚔️ [개선] 무기 및 직업 선택 드롭다운 메뉴 (직업 우선 / 전투직은 무기 적용)
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
    await interaction.response.send_message(embed=embed, view=LoadoutSelectView())

# ⚔️ 직접 입력용 명령어
@bot.tree.command(name="무기직업", description="무기와 직업을 직접 텍스트로 지정합니다.")
@app_commands.describe(무기="선택할 무기 (검, 창, 도끼, 활, 클로, 메이스)", 직업="선택할 직업 (요리사, 대장장이, 낚시꾼, 또는 전투직)")
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

# 🔔 [보부상 기본 알림 채널 지정]
@bot.tree.command(name="보부상채널설정", description="[관리자] 보부상 알림을 기본으로 수신할 고정 채널을 설정합니다.")
@app_commands.describe(채널="보부상 알림이 울릴 텍스트 채널")
async def set_merchant_default_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    global DEFAULT_MERCHANT_CHANNEL_ID
    DEFAULT_MERCHANT_CHANNEL_ID = 채널.id
    await interaction.response.send_message(f"✅ 앞으로 별도 지정이 없으면 {채널.mention} 채널로 보부상 알림이 전송됩니다!")

# 🔔 [보부상 타이머 등록]
@bot.tree.command(name="보부상등록", description="보부상 등장 예정 시간을 계산해 예약 알림을 설정합니다.")
@app_commands.describe(
    지역="보부상이 등장하는 지역 이름 (예: 플로라)",
    시간="남은 시간 (시간 단위, 없으면 0)",
    분="남은 시간 (분 단위, 예: 21)",
    알림채널="알림을 받을 채널 (지정하지 않으면 기본 채널 또는 현재 채널)"
)
async def register_merchant(
    interaction: discord.Interaction, 
    지역: str, 
    시간: int = 0, 
    분: int = 0, 
    알림채널: Optional[discord.TextChannel] = None
):
    await interaction.response.defer()
    
    total_minutes = (시간 * 60) + 분
    if total_minutes <= 0:
        await interaction.followup.send("❌ 남은 시간을 1분 이상으로 입력해 주세요!", ephemeral=True)
        return

    target_channel = 알림채널
    if not target_channel and DEFAULT_MERCHANT_CHANNEL_ID:
        target_channel = interaction.guild.get_channel(DEFAULT_MERCHANT_CHANNEL_ID)
    if not target_channel:
        target_channel = interaction.channel

    now = datetime.now()
    target_time = now + timedelta(minutes=total_minutes)
    target_str = target_time.strftime("%H시 %M분")

    if 지역 in MERCHANT_TASKS:
        MERCHANT_TASKS[지역].cancel()

    MERCHANT_DATA[지역] = {
        "target_time": target_time,
        "channel_id": target_channel.id,
        "registrant": interaction.user.display_name
    }

    task = asyncio.create_task(merchant_timer_task(target_channel, 지역, target_time))
    MERCHANT_TASKS[지역] = task

    embed = discord.Embed(
        title="🧭 [보부상] 알림 예약 완료",
        description=f"**[{지역}]** 보부상 등장 알림이 예약되었습니다!",
        color=discord.Color.gold()
    )
    embed.add_field(name="⏳ 남은 시간", value=f"{시간}시간 {분}분 뒤", inline=True)
    embed.add_field(name="🎯 도착 예정 시각", value=f"**{target_str}** 경", inline=True)
    embed.add_field(name="📢 수신 채널", value=target_channel.mention, inline=True)
    embed.add_field(name="📌 알림 규칙", value="등장 5분 전과 도착 정각에 `@everyone` 알림이 발송됩니다.", inline=False)
    embed.set_footer(text=f"등록자: {interaction.user.display_name}")

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="보부상목록", description="현재 대기 중인 보부상 등장 예정 목록을 확인합니다.")
async def list_merchants(interaction: discord.Interaction):
    if not MERCHANT_DATA:
        await interaction.response.send_message("🧭 현재 등록된 보부상 알림이 없습니다.", ephemeral=True)
        return

    now = datetime.now()
    lines = []
    for loc, info in MERCHANT_DATA.items():
        diff = info["target_time"] - now
        total_sec = int(diff.total_seconds())
        if total_sec > 0:
            hrs = total_sec // 3600
            mins = (total_sec % 3600) // 60
            time_left = f"{hrs}시간 {mins}분 남음" if hrs > 0 else f"{mins}분 남음"
            lines.append(f"• **{loc}**: <#{info['channel_id']}> | {info['target_time'].strftime('%H:%M')} 도착 예정 (`{time_left}` / 등록: {info['registrant']})")

    if not lines:
        await interaction.response.send_message("🧭 현재 대기 중인 보부상이 없습니다.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🧭 대기 중인 보부상 목록",
        description="\n".join(lines),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="보부상취소", description="등록되어 있는 특정 지역의 보부상 알림을 취소합니다.")
@app_commands.describe(지역="취소할 지역 이름")
async def cancel_merchant(interaction: discord.Interaction, 지역: str):
    if 지역 in MERCHANT_TASKS:
        MERCHANT_TASKS[지역].cancel()
        MERCHANT_TASKS.pop(지역, None)
        MERCHANT_DATA.pop(지역, None)
        await interaction.response.send_message(f"🗑️ **[{지역}]** 보부상 알림이 취소되었습니다.")
    else:
        await interaction.response.send_message(f"❌ 등록된 **[{지역}]** 보부상 정보를 찾을 수 없습니다.", ephemeral=True)

@bot.tree.command(name="사다리", description="통화방 인원으로 사다리 타기를 진행합니다.")
@app_commands.describe(당첨인원="당첨될 인원수", 당첨항목="당첨 항목 이름 (기본값: 🎉 당첨)", 꽝항목="꽝 항목 이름 (기본값: ❌ 꽝)")
async def ladder_game(interaction: discord.Interaction, 당첨인원: int, 당첨항목: str = "🎉 당첨", 꽝항목: str = "❌ 꽝"):
    await interaction.response.defer()
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("❌ 통화방에 먼저 접속한 뒤 입력해 주세요!", ephemeral=True)
        return

    voice_channel = interaction.user.voice.channel
    players = [m.display_name for m in voice_channel.members if not m.bot]
    total_count = len(players)

    if total_count < 2:
        await interaction.followup.send("❌ 음성 채널에 최소 2명 이상 있어야 합니다.", ephemeral=True)
        return

    if 당첨인원 <= 0 or 당첨인원 >= total_count:
        await interaction.followup.send(f"❌ 당첨 인원은 1명 이상 {total_count - 1}명 이하여야 합니다.", ephemeral=True)
        return

    lose_count = total_count - 당첨인원
    results = [당첨항목] * 당첨인원 + [꽝항목] * lose_count
    random.shuffle(results)

    result_lines = []
    winners = []
    for player, res in zip(players, results):
        if res == 당첨항목:
            result_lines.append(f"👑 **{player}** ➔ **{res}**")
            winners.append(player)
        else:
            result_lines.append(f"👤 {player} ➔ {res}")

    embed = discord.Embed(title=f"🪜 [{voice_channel.name}] 사다리 타기 결과 🪜", description="\n".join(result_lines), color=discord.Color.purple())
    embed.add_field(name="🏆 최종 당첨자", value=", ".join(winners), inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="기부알림", description="지정한 유저들에게 골드 기부 요청 알림을 발송합니다.")
@app_commands.describe(
    참여자="멘션할 유저들을 나열해 주세요 (예: @유저1 @유저2 @유저3 ...)",
    추가메시지="알림에 덧붙일 내용 (선택)"
)
async def manual_gold_reminder(
    interaction: discord.Interaction,
    참여자: str,
    추가메시지: Optional[str] = None
):
    await interaction.response.defer()
    raw_ids = list(set([int(uid) for uid in re.findall(r'<@!?(\d+)>', 참여자)]))
    
    if not raw_ids:
        await interaction.followup.send("❌ 멘션된 유저를 찾을 수 없습니다. `@유저` 형태로 입력해 주세요!", ephemeral=True)
        return

    mentions_text = " ".join([f"<@{uid}>" for uid in raw_ids])
    desc = "길드 성장을 위해 **일일 골드 기부**를 진행해 주세요! ✨"
    if 추가메시지:
        desc += f"\n\n💬 **전달사항:** {추가메시지}"

    embed = discord.Embed(title="💰 일일 골드 기부 알림", description=desc, color=discord.Color.gold())
    embed.set_footer(text=f"발송자: {interaction.user.display_name}")
    await interaction.followup.send(content=mentions_text, embed=embed)

@bot.tree.command(name="공지", description="팝업창을 열어 공지사항을 작성합니다.")
async def create_notice(interaction: discord.Interaction):
    await interaction.response.send_modal(NoticeModal())

@bot.tree.command(name="청소", description="지정한 개수만큼 메시지를 삭제합니다. (누구나 사용 가능)")
@app_commands.describe(개수="삭제할 메시지 개수 (1~100개)")
async def clear_messages(interaction: discord.Interaction, 개수: int):
    if 개수 < 1 or 개수 > 100:
        await interaction.response.send_message("❌ 1~100개 사이로 지정해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=개수)
        await interaction.followup.send(f"🧹 메시지 **{len(deleted)}개**를 삭제했습니다.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("⚠️ 봇에 '메시지 관리' 권한이 없습니다.", ephemeral=True)

@bot.tree.command(name="투표", description="버튼식 실시간 투표를 생성합니다. (최대 5개)")
async def create_poll(
    interaction: discord.Interaction,
    질문: str,
    항목1: str,
    항목2: str,
    항목3: Optional[str] = None,
    항목4: Optional[str] = None,
    항목5: Optional[str] = None
):
    raw_options = [항목1, 항목2, 항목3, 항목4, 항목5]
    options = [opt for opt in raw_options if opt]
    if len(options) < 2:
        await interaction.response.send_message("❌ 최소 2개 이상 입력하세요.", ephemeral=True)
        return
    view = PollView(question=질문, options=options, author=interaction.user)
    await interaction.response.send_message(embed=view.make_embed(), view=view)

@bot.tree.command(name="대여", description="[은행] 돈이나 물건을 빌려준 내역 박스를 생성합니다.")
@app_commands.describe(
    빌린사람="돈이나 물건을 빌려간 멤버",
    항목="대여 내용 (예: 5만 골드, 네더라이트 곡괭이 등)",
    비고="추가 메모 (선택)"
)
async def loan_card(interaction: discord.Interaction, 빌린사람: discord.Member, 항목: str, 비고: Optional[str] = None):
    if 빌린사람.id == interaction.user.id:
        await interaction.response.send_message("❌ 자기 자신에게 빌려줄 수는 없습니다!", ephemeral=True)
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    embed = discord.Embed(
        title="🏦 [은행] 대여 등록",
        description=f"{interaction.user.mention} ➔ {빌린사람.mention}",
        color=discord.Color.gold()
    )
    embed.add_field(name="📦 대여 항목", value=f"**{항목}**", inline=False)
    embed.add_field(name="👤 빌려준 사람", value=interaction.user.display_name, inline=True)
    embed.add_field(name="🙋 빌린 사람", value=빌린사람.display_name, inline=True)
    embed.add_field(name="📌 상태", value="⏳ **미반납**", inline=True)
    if 비고:
        embed.add_field(name="📝 메모", value=비고, inline=False)
    embed.set_footer(text=f"등록 일시: {now_str} • 상환 완료 후 아래 버튼을 클릭하세요.")

    view = LoanView(lender_id=interaction.user.id, borrower_id=빌린사람.id)
    await interaction.response.send_message(content=f"{빌린사람.mention}", embed=embed, view=view)

# 🎯 대규모 인원 추첨 명령어
@bot.tree.command(name="추첨", description="팝업창을 열어 많은 인원을 복사/붙여넣기로 간편하게 추첨합니다.")
async def open_lottery_modal(interaction: discord.Interaction):
    await interaction.response.send_modal(LotteryModal())

# 🎪 쿠지 명령어
@bot.tree.command(name="쿠지생성", description="[관리자] 균열석 기부자를 위한 뽑기판을 생성합니다.")
async def create_kuji(interaction: discord.Interaction):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 쿠지 판을 생성할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.send_modal(KujiCreateModal())

@bot.tree.command(name="쿠지티켓지급", description="[관리자] 특정 유저에게 뽑기 기회(티켓)를 추가 지급하거나 참여자로 등록합니다.")
@app_commands.describe(유저="티켓을 지급할 대상", 횟수="추가할 뽑기 횟수 (기본 1회)")
async def add_kuji_ticket(interaction: discord.Interaction, 유저: discord.Member, 횟수: Optional[int] = 1):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    if not KUJI_GAMES:
        await interaction.response.send_message("❌ 진행 중인 쿠지 판이 없습니다.", ephemeral=True)
        return

    latest_game_id = list(KUJI_GAMES.keys())[-1]
    game = KUJI_GAMES[latest_game_id]

    game["allowed_users"][유저.id] = game["allowed_users"].get(유저.id, 0) + (횟수 if 횟수 else 1)

    await interaction.response.send_message(
        f"✅ **{유저.display_name}** 님에게 뽑기 티켓 **{횟수}장**을 지급했습니다! (현재 잔여: **{game['allowed_users'][유저.id]}장**)",
        ephemeral=True
    )

# 🏆 가장 최근 쿠지 당첨자 명단 조회 명령어
@bot.tree.command(name="쿠지당첨자", description="가장 최근에 진행된 쿠지의 당첨자 명단 및 결과를 확인합니다.")
async def show_kuji_winners(interaction: discord.Interaction):
    await interaction.response.defer()
    if not KUJI_GAMES:
        await interaction.followup.send("❌ 진행 중이거나 등록된 쿠지 판이 없습니다.")
        return

    latest_game_id = list(KUJI_GAMES.keys())[-1]
    game = KUJI_GAMES[latest_game_id]
    history = game.get("history", [])
    lose_name = game.get("lose_name", "❌ 꽝")

    if not history:
        await interaction.followup.send(f"📢 **{game['title']}**\n아직 아무도 뽑기를 진행하지 않았습니다.")
        return

    winners_list = []
    losers_list = []
    for r in history:
        line = f"• <@{r['user_id']}> ({r['user_name']}) ➔ **{r['prize']}** *({r['time']})*"
        if r['prize'] == lose_name or "꽝" in r['prize']:
            losers_list.append(line)
        else:
            winners_list.append(line)

    embed = discord.Embed(
        title=f"🏆 [최근 뽑기 결과] {game['title']}",
        description=f"총 `{len(history)}회` 뽑기가 진행되었습니다.",
        color=discord.Color.gold()
    )

    if winners_list:
        embed.add_field(name="🎁 당첨자 명단", value="\n".join(winners_list), inline=False)
    else:
        embed.add_field(name="🎁 당첨자 명단", value="아직 당첨자가 없습니다.", inline=False)

    if losers_list:
        embed.add_field(name="💨 꽝 명단", value="\n".join(losers_list), inline=False)

    not_played = [f"<@{uid}>" for uid, count in game["allowed_users"].items() if count > 0]
    if not_played:
        embed.add_field(name="⏳ 미참여 인원", value=" ".join(not_played), inline=False)

    await interaction.followup.send(embed=embed)

# 📋 멤버 닉네임 추출 명령어
@bot.tree.command(name="멤버멘션추출", description="[관리자] 특정 역할을 가진 멤버들의 닉네임을 복사용 텍스트로 출력합니다.")
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
    
    msg = f"📋 **{역할.name}** 멤버 닉네임 목록 (총 {len(members)}명):\n\n"
    msg += f"```text\n{name_list}\n```"
    
    await interaction.response.send_message(msg, ephemeral=True)

# --- 영구 보존 경고 시스템 ---
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

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    warn_entry = {"reason": 사유, "moderator": interaction.user.display_name, "date": now_str}
    
    await log_warning_action(interaction.guild, 유저.id, "add", warn_entry)
    
    warnings = await fetch_warnings_from_channel(interaction.guild)
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
    
    warnings = await fetch_warnings_from_channel(interaction.guild)
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

@bot.tree.command(name="경고목록", description="[관리자 전용] 현재 경고를 보유 중인 모든 유저의 목록을 확인합니다.")
async def show_all_warnings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    warnings = await fetch_warnings_from_channel(interaction.guild)
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

@bot.tree.command(name="경고차감", description="[관리자 전용] 경고를 차감합니다.")
@app_commands.describe(유저="대상 멤버", 개수="차감할 개수 (기본 1개)")
async def remove_warn(interaction: discord.Interaction, 유저: discord.Member, 개수: Optional[int] = 1):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
        
    warnings = await fetch_warnings_from_channel(interaction.guild)
    u_id = str(유저.id)
    if u_id not in warnings or len(warnings[u_id]) == 0:
        await interaction.followup.send("❌ 차감할 경고가 없습니다.", ephemeral=True)
        return

    deduct = min(개수 if 개수 else 1, len(warnings[u_id]))
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

# --- 백업용 즉시 동기화 일반 명령어 ---
@bot.command(name="동기화")
async def manual_sync(ctx):
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"✅ 이 서버에 **{len(synced)}개**의 슬래시 명령어를 즉시 동기화했습니다! 잠시 후 `/`를 쳐보세요.")
    except Exception as e:
        await ctx.send(f"❌ 동기화 실패: {e}")

# --- 봇 시작 및 동기화 ---
@bot.event
async def on_ready():
    bot.add_view(LoadoutSelectView())
    bot.add_dynamic_items(DynamicLoanButton)
    bot.add_dynamic_items(DynamicKujiButton)
    
    try:
        synced = await bot.tree.sync()
        print(f"✅ 전역 명령어 {len(synced)}개 동기화 완료!")
    except Exception as e:
        print(f"❌ 동기화 오류: {e}")
            
    print(f"로그인 완료: {bot.user.name}")

if __name__ == "__main__":
    keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    bot.run(token)
