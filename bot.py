import os
import re
import json
import random
import threading
import asyncio
from datetime import datetime
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

# --- 경고 데이터 관리 ---
WARNINGS_FILE = "warnings.json"

def load_warnings() -> dict:
    if os.path.exists(WARNINGS_FILE):
        try:
            with open(WARNINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_warnings(data: dict):
    with open(WARNINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_admin_or_mod(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild or perms.moderate_members or perms.kick_members

def find_guild_member(guild: discord.Guild, query: str) -> Optional[discord.Member]:
    query = query.strip()
    if not query:
        return None
        
    # 멘션 형태 (<@123456>) 체크
    match = re.match(r'<@!?(\d+)>', query)
    if match:
        return guild.get_member(int(match.group(1)))
        
    # 닉네임, 이름, 별명 부분 일치 검색
    for m in guild.members:
        if m.bot:
            continue
        if query.lower() in m.display_name.lower() or query.lower() in m.name.lower():
            return m
    return None

# --- 직업 목록 ---
JOBS = [
    "검호", "정식기사", "추적자", "암살자", "위자드",
    "창성", "진혼자", "바바리안", "비스트테이머", "클레릭",
    "월영의 그림자", "드루이드", "백야기사", "근위대장",
    "중급 대장장이", "검성", "광부"
]

async def process_job_selection(interaction: discord.Interaction, job_name: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    member = interaction.user
    prefix = f"[{job_name}]"
    
    for j in JOBS:
        role = discord.utils.get(guild.roles, name=j)
        if role and role in member.roles:
            await member.remove_roles(role)
            
    new_role = discord.utils.get(guild.roles, name=job_name)
    if not new_role:
        new_role = await guild.create_role(name=job_name)
    await member.add_roles(new_role)
    
    original_name = member.display_name
    for j in JOBS:
        tag = f"[{j}]"
        if original_name.startswith(tag):
            original_name = original_name.replace(tag, "").strip()
            
    new_nickname = f"{prefix} {original_name}"
    try:
        await member.edit(nick=new_nickname)
        nickname_msg = f"닉네임이 **{new_nickname}**(으)로 변경되었습니다!"
    except discord.Forbidden:
        nickname_msg = "(⚠️ 닉네임 자동 변경 권한 부족)"

    await interaction.followup.send(f"✅ **[{job_name}]** 직업을 선택하셨습니다!\n{nickname_msg}", ephemeral=True)


# --- UI 컴포넌트 ---
class ProfileModal(discord.ui.Modal, title="자기소개 입력"):
    name = discord.ui.TextInput(label="이름 (또는 별명)", placeholder="예: 쨈", required=True, max_length=15)
    mc_name = discord.ui.TextInput(label="마인크래프트 닉네임", placeholder="예: _s2_jammy", required=True, max_length=25)
    birth_year = discord.ui.TextInput(label="출생 연도 (두 자리)", placeholder="예: 06", required=True, max_length=4)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user
        current_nick = user.display_name
        
        prefix = ""
        if "]" in current_nick and current_nick.startswith("["):
            prefix = current_nick.split("]")[0] + "] "
            
        new_nick = f"{prefix}{self.name.value} / {self.mc_name.value} / {self.birth_year.value}"
        try:
            await user.edit(nick=new_nick)
            nick_msg = f"✅ 닉네임이 변경되었습니다!\n**{new_nick}**"
        except discord.Forbidden:
            nick_msg = "⚠️ (닉네임 수정 권한 없음)"

        ROLE_NAME = "수습 담이🐣"
        target_role = discord.utils.get(guild.roles, name=ROLE_NAME)
        if target_role:
            try:
                await user.add_roles(target_role)
                role_msg = f"\n🔰 **[{ROLE_NAME}]** 역할이 부여되었습니다!"
            except discord.Forbidden:
                role_msg = f"\n⚠️ 역할 부여 실패"
        else:
            role_msg = f"\n⚠️ **[{ROLE_NAME}]** 역할 없음"

        await interaction.followup.send(f"{nick_msg}{role_msg}", ephemeral=True)


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
        placeholder="홍길동, 우랄라, 뽀삐\n또는 멘션 나열",
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


class JobButton(discord.ui.Button):
    def __init__(self, job_name: str):
        super().__init__(label=job_name, style=discord.ButtonStyle.primary, custom_id=f"job_button_{job_name}")
        self.job_name = job_name

    async def callback(self, interaction: discord.Interaction):
        await process_job_selection(interaction, self.job_name)

class JobButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for job in JOBS[:25]:
            self.add_item(JobButton(job))


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


# --- 이치방쿠지 데이터 및 UI ---
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
            await interaction.response.send_message("❌ 진행 중인 쿠지 판 정보를 찾을 수 없습니다. (종료되었거나 초기화됨)", ephemeral=True)
            return

        user_id = interaction.user.id

        if user_id not in game["allowed_users"]:
            await interaction.response.send_message("❌ 오늘의 균열석 기부자 명단에 등록되지 않아 참여할 수 없습니다!", ephemeral=True)
            return

        remaining_tickets = game["allowed_users"][user_id]
        if remaining_tickets <= 0:
            await interaction.response.send_message("❌ 보유한 뽑기 티켓을 모두 사용하셨습니다!", ephemeral=True)
            return

        if len(game["box"]) == 0:
            await interaction.response.send_message("❌ 모든 쿠지가 이미 소진되었습니다!", ephemeral=True)
            return

        # 뽑기 진행
        game["allowed_users"][user_id] -= 1
        picked_prize = game["box"].pop(random.randint(0, len(game["box"]) - 1))
        
        is_finished = len(game["box"]) == 0

        now_time = datetime.now().strftime("%H:%M")
        game["history"].append(f"• **{interaction.user.display_name}** ➔ **{picked_prize}** ({now_time})")

        embed = self.build_embed(game)
        
        if is_finished:
            self.item.disabled = True
            self.item.label = "쿠지 매진 (종료)"
            self.item.style = discord.ButtonStyle.secondary

        view = discord.ui.View(timeout=None)
        view.add_item(self.item)

        await interaction.response.edit_message(embed=embed, view=view)
        
        # 🎯 꽝 여부 판별하여 알림 멘트 다르게 출력
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
            prize_status.append(f"• **{prize}**: `{left_count} / {count}`개 남음")

        desc = (
            f"**📢 {game['title']}**\n"
            f"균열석 기부자 전용 이치방쿠지 뽑기판입니다!\n\n"
            f"📊 **남은 수량:** `{total_left} / {total_init}`장\n"
        )

        embed = discord.Embed(
            title="🎪 [이치방쿠지] 균열석 기부자 뽑기판",
            description=desc,
            color=discord.Color.gold() if total_left > 0 else discord.Color.dark_grey()
        )

        embed.add_field(name="🎁 상품 현황", value="\n".join(prize_status), inline=False)
        
        if game["history"]:
            recent_history = game["history"][-5:]
            embed.add_field(name="📜 최근 당첨 내역", value="\n".join(reversed(recent_history)), inline=False)

        allowed_mentions = [f"<@{uid}>({cnt}회)" for uid, cnt in game["allowed_users"].items() if cnt > 0]
        if allowed_mentions:
            embed.add_field(name="🎫 참여 가능 기부자", value=" ".join(allowed_mentions), inline=False)
        else:
            embed.add_field(name="🎫 참여 가능 기부자", value="모든 기부자가 티켓을 사용했습니다.", inline=False)

        embed.set_footer(text=f"주최: {game['host']} • 1회 뽑기당 티켓 1장 소모")
        return embed


class KujiCreateModal(discord.ui.Modal, title="🎪 이치방쿠지 뽑기판 생성"):
    kuji_title = discord.ui.TextInput(label="쿠지 제목", placeholder="예: 균열석 기부 감사 쿠지", required=True, max_length=50)
    prizes = discord.ui.TextInput(
        label="당첨 상품 라인업 (상품명:수량 줄바꿈)",
        placeholder="A상 10만골드:1\nB상 네더라이트 곡괭이:2\nC상 다이아 32개:5\n(※ 꽝은 적지 않아도 총 인원에 맞춰 자동 생성)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    allowed_donors = discord.ui.TextInput(
        label="참여 가능 기부자 목록 (닉네임/멘션, 줄바꿈 또는 쉼표)",
        placeholder="홍길동, 알랄라, 뽀삐\n또는 줄바꿈으로 나열",
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

        # 당첨 상품 파싱
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

        # 🎯 부족한 수량은 꽝으로 자동 채움
        current_prize_count = len(box)
        lose_name = self.default_lose_name.value.strip() if self.default_lose_name.value else "❌ 꽝"

        if current_prize_count < total_required_tickets:
            remaining_lose_count = total_required_tickets - current_prize_count
            initial_prizes[lose_name] = initial_prizes.get(lose_name, 0) + remaining_lose_count
            box.extend([lose_name] * remaining_lose_count)
        elif current_prize_count > total_required_tickets:
            await interaction.followup.send(
                f"⚠️ 입력한 당첨 상품 총 개수({current_prize_count}개)가 참여자 인원수({total_required_tickets}명)보다 많습니다! 수량을 조절해 주세요.",
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
        await interaction.followup.send(content=f"📢 **균열석 기부자 쿠지 뽑기판이 열렸습니다!**\n{mention_pings}", embed=embed, view=view)


# --- 슬래시 명령어 ---

@bot.tree.command(name="자기소개", description="이름, 마크 닉네임, 출생 연도를 입력하여 닉네임을 설정합니다.")
async def profile(interaction: discord.Interaction):
    await interaction.response.send_modal(ProfileModal())

@bot.tree.command(name="직업선택", description="버튼 형태의 직업 선택 메뉴를 출력합니다.")
async def job_select_menu(interaction: discord.Interaction):
    embed = discord.Embed(title="⚔️ 직업 선택", description="아래 버튼을 누르면 닉네임 앞에 `[직업]` 태그가 붙습니다!", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, view=JobButtonView())

@bot.tree.command(name="직업", description="직업 이름을 직접 입력하여 변경합니다.")
@app_commands.describe(직업명="선택할 직업 이름")
async def choose_job(interaction: discord.Interaction, 직업명: str):
    await interaction.response.defer(ephemeral=True)
    if 직업명 not in JOBS:
        await interaction.followup.send(f"❌ 존재하지 않는 직업입니다. 선택 가능: {', '.join(JOBS)}", ephemeral=True)
        return
    await process_job_selection(interaction, 직업명)

@bot.tree.command(name="직업목록", description="선택 가능한 모든 직업 목록을 확인합니다.")
async def job_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    job_text = "\n".join([f"• **{job}**" for job in JOBS])
    embed = discord.Embed(title="⚔️ 선택 가능한 직업 목록 ⚔️", description=job_text, color=discord.Color.blue())
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="직업추가", description="선택 목록에 새로운 직업을 추가합니다.")
@app_commands.describe(직업명="추가할 새로운 직업 이름")
async def add_job(interaction: discord.Interaction, 직업명: str):
    await interaction.response.defer(ephemeral=True)
    if 직업명 in JOBS:
        await interaction.followup.send(f"❌ **{직업명}**은(는) 이미 존재합니다.", ephemeral=True)
        return
    JOBS.append(직업명)
    await interaction.followup.send(f"✅ 직업 **[{직업명}]**이(가) 추가되었습니다!", ephemeral=True)

@bot.tree.command(name="직업삭제", description="선택 목록에서 직업을 삭제합니다.")
@app_commands.describe(직업명="삭제할 직업 이름")
async def remove_job(interaction: discord.Interaction, 직업명: str):
    await interaction.response.defer(ephemeral=True)
    if 직업명 not in JOBS:
        await interaction.followup.send(f"❌ **{직업명}** 직업을 찾을 수 없습니다.", ephemeral=True)
        return
    JOBS.remove(직업명)
    await interaction.followup.send(f"🗑️ 직업 **[{직업명}]**이(가) 삭제되었습니다!", ephemeral=True)

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

# 🎪 이치방쿠지 명령어
@bot.tree.command(name="쿠지생성", description="[관리자] 균열석 기부자를 위한 이치방쿠지 뽑기판을 생성합니다.")
async def create_kuji(interaction: discord.Interaction):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 쿠지 판을 생성할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.send_modal(KujiCreateModal())

@bot.tree.command(name="쿠지티켓지급", description="[관리자] 특정 유저에게 쿠지 뽑기 기회(티켓)를 추가 지급하거나 참여자로 등록합니다.")
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
        f"✅ **{유저.display_name}** 님에게 쿠지 뽑기 티켓 **{횟수}장**을 지급했습니다! (현재 잔여: **{game['allowed_users'][유저.id]}장**)",
        ephemeral=True
    )

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

# --- 경고 시스템 ---
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

    warnings = load_warnings()
    u_id = str(유저.id)
    if u_id not in warnings:
        warnings[u_id] = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    warnings[u_id].append({"reason": 사유, "moderator": interaction.user.display_name, "date": now_str})
    save_warnings(warnings)

    count = len(warnings[u_id])
    embed = discord.Embed(
        title="⚠️ 경고가 부여되었습니다",
        description=f"{유저.mention} 님에게 경고가 1회 누적되었습니다.",
        color=discord.Color.red()
    )
    embed.add_field(name="👤 대상자", value=유저.display_name, inline=True)
    embed.add_field(name="🚨 누적 횟수", value=f"**{count}회**", inline=True)
    embed.add_field(name="📝 사유", value=사유, inline=False)
    await interaction.channel.send(content=유저.mention, embed=embed)
    await interaction.followup.send(f"✅ {유저.display_name} 님에게 경고를 부여했습니다.", ephemeral=True)

@bot.tree.command(name="경고확인", description="경고 내역을 확인합니다.")
@app_commands.describe(유저="조회할 대상 멤버 (비워두면 본인)")
async def check_warnings(interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
    await interaction.response.defer(ephemeral=True)
    target = 유저 if 유저 else interaction.user
    warnings = load_warnings()
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

@bot.tree.command(name="경고차감", description="[관리자 전용] 경고를 차감합니다.")
@app_commands.describe(유저="대상 멤버", 개수="차감할 개수 (기본 1개)")
async def remove_warn(interaction: discord.Interaction, 유저: discord.Member, 개수: Optional[int] = 1):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    warnings = load_warnings()
    u_id = str(유저.id)
    if u_id not in warnings or len(warnings[u_id]) == 0:
        await interaction.followup.send(f"❌ 차감할 경고가 없습니다.", ephemeral=True)
        return

    deduct = min(개수 if 개수 else 1, len(warnings[u_id]))
    warnings[u_id] = warnings[u_id][:-deduct]
    save_warnings(warnings)
    await interaction.followup.send(f"✅ **{유저.display_name}** 님의 경고가 **{deduct}회** 차감되었습니다. (현재: **{len(warnings[u_id])}회**)", ephemeral=True)

@bot.tree.command(name="경고초기화", description="[관리자 전용] 경고를 초기화합니다.")
@app_commands.describe(유저="대상 멤버")
async def clear_warn(interaction: discord.Interaction, 유저: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_or_mod(interaction):
        await interaction.followup.send("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    warnings = load_warnings()
    u_id = str(유저.id)
    if u_id in warnings:
        warnings[u_id] = []
        save_warnings(warnings)
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
    bot.add_view(JobButtonView())
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
