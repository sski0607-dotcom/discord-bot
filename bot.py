import os
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

# --- 0. 백그라운드 웹 서버 ---
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
        nickname_msg = "(⚠️ 닉네임 자동 변경 건너뜀)"

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
        embed = discord.Embed(title=f"📢 {self.notice_title.value}", description=self.notice_content.value, color=discord.Color.blue())
        if self.notice_footer.value:
            embed.add_field(name="📌 전달사항", value=self.notice_footer.value, inline=False)
        embed.set_footer(text=f"작성자: {interaction.user.display_name} • {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
        await interaction.response.send_message(embed=embed)


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
class LoanView(discord.ui.View):
    def __init__(self, lender_id: int, borrower_id: int):
        super().__init__(timeout=None)
        self.lender_id = lender_id
        self.borrower_id = borrower_id

    @discord.ui.button(label="반납 완료", style=discord.ButtonStyle.success, emoji="✅", custom_id="loan_return_btn")
    async def return_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.lender_id and interaction.user.id != self.borrower_id and not is_admin_or_mod(interaction):
            await interaction.response.send_message("❌ 빌려준 사람, 빌린 사람 또는 관리자만 반납 처리를 할 수 있습니다.", ephemeral=True)
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
        button.disabled = True
        button.label = "반납 완료됨"
        
        await interaction.response.edit_message(embed=new_embed, view=self)
        await interaction.followup.send(f"✅ **{interaction.user.display_name}** 님이 상환 완료 처리했습니다!", ephemeral=False)


# --- 🎰 스크래치 복권 UI ---
class LotteryView(discord.ui.View):
    def __init__(self, title: str, participants: List[discord.Member], winner_ids: List[int], author: discord.Member):
        super().__init__(timeout=None)
        self.title = title
        self.participants = participants
        self.winner_ids = winner_ids
        self.author = author
        self.participant_ids = [p.id for p in participants]
        self.checked_users: Dict[int, bool] = {} # {user_id: is_winner}

    def make_embed(self) -> discord.Embed:
        desc = (
            f"🎁 **이벤트 명:** {self.title}\n"
            f"👥 **참여 대상:** 총 {len(self.participants)}명\n"
            f"👑 **당첨 인원:** {len(self.winner_ids)}명\n\n"
            f"👇 아래 **[ 긁어서 결과 확인! ]** 버튼을 눌러 본인의 당첨 여부를 확인하세요!\n"
            f"*(결과는 본인에게만 비공개로 즉시 표시됩니다)*\n\n"
            f"**[ 📋 실시간 확인 현황 ]**\n"
        )
        status_lines = []
        for p in self.participants:
            if p.id in self.checked_users:
                status_lines.append(f"• {p.display_name} ➔ 🔍 **확인 완료**")
            else:
                status_lines.append(f"• {p.display_name} ➔ ⏳ 미확인")
        
        desc += "\n".join(status_lines)

        embed = discord.Embed(
            title="🎰 스크래치 복권 이벤트",
            description=desc,
            color=discord.Color.purple()
        )
        embed.set_footer(text=f"주최자: {self.author.display_name} • 행운을 빕니다! ✨")
        return embed

    @discord.ui.button(label="긁어서 결과 확인!", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="lottery_scratch_btn")
    async def scratch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        if user.id not in self.participant_ids:
            await interaction.response.send_message("❌ 이번 복권 이벤트 대상자가 아닙니다!", ephemeral=True)
            return

        is_winner = user.id in self.winner_ids
        self.checked_users[user.id] = is_winner

        # 결과 메시지
        if is_winner:
            result_msg = (
                f"🎉🎉 **[당첨 축하드립니다!]** 🎉🎉\n\n"
                f"✨ **{self.title}** 복권에 **당첨**되셨습니다! 🎁\n"
                f"관리자에게 보상을 요청하세요!"
            )
        else:
            result_msg = (
                f"💨 **[아쉽게도 꽝입니다!]** 💨\n\n"
                f"아쉽게도 이번에는 당첨되지 않았습니다. 다음 기회에 도전해 보세요! 🍀"
            )

        await interaction.response.send_message(result_msg, ephemeral=True)
        # 메인 메시지 실시간 확인 현황 업데이트
        await interaction.message.edit(embed=self.make_embed(), view=self)


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
    if 직업명 not in JOBS:
        await interaction.response.send_message(f"❌ 존재하지 않는 직업입니다. 선택 가능: {', '.join(JOBS)}", ephemeral=True)
        return
    await process_job_selection(interaction, 직업명)

@bot.tree.command(name="직업목록", description="선택 가능한 모든 직업 목록을 확인합니다.")
async def job_list(interaction: discord.Interaction):
    job_text = "\n".join([f"• **{job}**" for job in JOBS])
    embed = discord.Embed(title="⚔️ 선택 가능한 직업 목록 ⚔️", description=job_text, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

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
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ 통화방에 먼저 접속한 뒤 입력해 주세요!", ephemeral=True)
        return

    voice_channel = interaction.user.voice.channel
    players = [m.display_name for m in voice_channel.members if not m.bot]
    total_count = len(players)

    if total_count < 2 or 당첨인원 <= 0 or 당첨인원 >= total_count:
        await interaction.response.send_message("❌ 인원수 및 당첨자 수를 확인해 주세요.", ephemeral=True)
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
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="기부알림", description="지정한 유저들에게 골드 기부 요청 알림을 발송합니다.")
async def manual_gold_reminder(
    interaction: discord.Interaction,
    유저1: discord.Member,
    유저2: Optional[discord.Member] = None,
    유저3: Optional[discord.Member] = None,
    유저4: Optional[discord.Member] = None,
    유저5: Optional[discord.Member] = None,
    추가메시지: Optional[str] = None
):
    raw_users = [유저1, 유저2, 유저3, 유저4, 유저5]
    target_users = [u for u in raw_users if u]
    mentions_text = " ".join([u.mention for u in target_users])
    desc = "길드 성장을 위해 **일일 골드 기부**를 진행해 주세요! ✨"
    if 추가메시지:
        desc += f"\n\n💬 **전달사항:** {추가메시지}"

    embed = discord.Embed(title="💰 일일 골드 기부 알림", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(content=mentions_text, embed=embed)

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
        await interaction.followup.send("⚠️ 메시지 관리 권한이 없습니다.", ephemeral=True)

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


# --- 🎰 복권 이벤트 생성 명령어 ---
@bot.tree.command(name="복권생성", description="지정한 인원들을 대상으로 스크래치 복권 이벤트를 엽니다.")
@app_commands.describe(
    이벤트명="복권 이벤트 이름 (예: 균열석 기부자 추첨)",
    당첨인원="당첨될 인원수 (숫자)",
    유저1="참여 대상자 (필수)",
    유저2="참여 대상자 (선택)",
    유저3="참여 대상자 (선택)",
    유저4="참여 대상자 (선택)",
    유저5="참여 대상자 (선택)",
    유저6="참여 대상자 (선택)",
    유저7="참여 대상자 (선택)",
    유저8="참여 대상자 (선택)",
    유저9="참여 대상자 (선택)",
    유저10="참여 대상자 (선택)",
    유저11="참여 대상자 (선택)",
    유저12="참여 대상자 (선택)",
    유저13="참여 대상자 (선택)",
    유저14="참여 대상자 (선택)",
    유저15="참여 대상자 (선택)"
)
async def create_lottery(
    interaction: discord.Interaction,
    이벤트명: str,
    당첨인원: int,
    유저1: discord.Member,
    유저2: Optional[discord.Member] = None,
    유저3: Optional[discord.Member] = None,
    유저4: Optional[discord.Member] = None,
    유저5: Optional[discord.Member] = None,
    유저6: Optional[discord.Member] = None,
    유저7: Optional[discord.Member] = None,
    유저8: Optional[discord.Member] = None,
    유저9: Optional[discord.Member] = None,
    유저10: Optional[discord.Member] = None,
    유저11: Optional[discord.Member] = None,
    유저12: Optional[discord.Member] = None,
    유저13: Optional[discord.Member] = None,
    유저14: Optional[discord.Member] = None,
    유저15: Optional[discord.Member] = None,
):
    raw_users = [유저1, 유저2, 유저3, 유저4, 유저5, 유저6, 유저7, 유저8, 유저9, 유저10, 유저11, 유저12, 유저13, 유저14, 유저15]
    participants = []
    for u in raw_users:
        if u and u not in participants and not u.bot:
            participants.append(u)

    total_count = len(participants)
    if total_count < 2:
        await interaction.response.send_message("❌ 최소 2명 이상의 참여자가 필요합니다!", ephemeral=True)
        return

    if 당첨인원 <= 0 or 당첨인원 >= total_count:
        await interaction.response.send_message(f"❌ 당첨 인원은 1명 이상, 전체 인원({total_count}명) 미만이어야 합니다.", ephemeral=True)
        return

    # 당첨자 무작위 사전 추첨
    winners = random.sample(participants, 당첨인원)
    winner_ids = [w.id for w in winners]

    mentions = " ".join([p.mention for p in participants])
    view = LotteryView(title=이벤트명, participants=participants, winner_ids=winner_ids, author=interaction.user)

    await interaction.response.send_message(content=f"🎉 **[복권 도착]** {mentions}", embed=view.make_embed(), view=view)


# --- 경고 시스템 ---
@bot.tree.command(name="경고", description="[관리자 전용] 유저에게 경고를 부여합니다.")
@app_commands.describe(유저="대상 멤버", 사유="경고 사유")
async def warn_user(interaction: discord.Interaction, 유저: discord.Member, 사유: str):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    if 유저.bot:
        await interaction.response.send_message("❌ 봇에게는 부여할 수 없습니다.", ephemeral=True)
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
    await interaction.response.send_message(content=유저.mention, embed=embed)

@bot.tree.command(name="경고확인", description="경고 내역을 확인합니다.")
@app_commands.describe(유저="조회할 대상 멤버 (비워두면 본인)")
async def check_warnings(interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
    target = 유저 if 유저 else interaction.user
    warnings = load_warnings()
    records = warnings.get(str(target.id), [])
    count = len(records)

    if count == 0:
        await interaction.response.send_message(f"✨ **{target.display_name}** 님은 경고가 없습니다! (0회)", ephemeral=True)
        return

    lines = [f"**{i}.** {r['reason']} *({r['moderator']} • {r['date']})*" for i, r in enumerate(records, 1)]
    embed = discord.Embed(
        title=f"📋 [{target.display_name}] 님의 경고 내역",
        description=f"총 누적: **{count}회**\n\n" + "\n".join(lines),
        color=discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="경고차감", description="[관리자 전용] 경고를 차감합니다.")
@app_commands.describe(유저="대상 멤버", 개수="차감할 개수 (기본 1개)")
async def remove_warn(interaction: discord.Interaction, 유저: discord.Member, 개수: Optional[int] = 1):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    warnings = load_warnings()
    u_id = str(유저.id)
    if u_id not in warnings or len(warnings[u_id]) == 0:
        await interaction.response.send_message("❌ 차감할 경고가 없습니다.", ephemeral=True)
        return

    deduct = min(개수, len(warnings[u_id]))
    warnings[u_id] = warnings[u_id][:-deduct]
    save_warnings(warnings)
    await interaction.response.send_message(f"✅ **{유저.display_name}** 님의 경고가 **{deduct}회** 차감되었습니다. (현재: **{len(warnings[u_id])}회**)")

@bot.tree.command(name="경고초기화", description="[관리자 전용] 경고를 초기화합니다.")
@app_commands.describe(유저="대상 멤버")
async def clear_warn(interaction: discord.Interaction, 유저: discord.Member):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    warnings = load_warnings()
    u_id = str(유저.id)
    if u_id in warnings:
        warnings[u_id] = []
        save_warnings(warnings)
    await interaction.response.send_message(f"🧹 **{유저.display_name}** 님의 모든 경고가 초기화되었습니다.")

# --- 봇 시작 및 동기화 ---
@bot.event
async def on_ready():
    bot.add_view(JobButtonView())
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"✅ {len(synced)}개의 명령어 동기화 완료!")
    except Exception as e:
        print(f"❌ 오류 발생: {e}")
    print(f"로그인 완료: {bot.user.name}")

if __name__ == "__main__":
    keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    bot.run(token)
