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

# --- 0. Render 슬립 방지용 백그라운드 웹 서버 ---
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

# 1. 봇 권한 및 객체 설정
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Render 환경 변수 설정
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# --- 경고 데이터 관리 (JSON 저장) ---
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
    """관리자 권한 또는 멤버 관리 권한 확인"""
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild or perms.moderate_members or perms.kick_members

# 2. 기본 직업 목록 정의
JOBS = [
    "검호", "정식기사", "추적자", "암살자", "위자드",
    "창성", "진혼자", "바바리안", "비스트테이머", "클레릭",
    "월영의 그림자", "드루이드", "백야기사", "근위대장",
    "중급 대장장이", "검성", "광부"
]

# 공통 직업 처리 함수
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
        nickname_msg = "(⚠️ 봇 권한 부족 또는 최고권한자 계정이라 닉네임 자동 변경은 건너뛰었습니다.)"

    await interaction.followup.send(f"✅ **[{job_name}]** 직업을 선택하셨습니다!\n{nickname_msg}", ephemeral=True)


# --- 자기소개 모달 UI ---
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
            nick_msg = "⚠️ (봇 권한 부족 또는 최고권한자 계정이라 닉네임 수정은 건너뛰었습니다.)"

        ROLE_NAME = "수습 담이🐣"
        target_role = discord.utils.get(guild.roles, name=ROLE_NAME)
        if target_role:
            try:
                await user.add_roles(target_role)
                role_msg = f"\n🔰 **[{ROLE_NAME}]** 역할이 자동으로 부여되었습니다!"
            except discord.Forbidden:
                role_msg = f"\n⚠️ 봇의 역할 순위가 낮아 **[{ROLE_NAME}]** 역할을 부여하지 못했습니다."
        else:
            role_msg = f"\n⚠️ 서버에 **[{ROLE_NAME}]** 역할이 존재하지 않아 역할 부여를 건너뛰었습니다."

        await interaction.followup.send(f"{nick_msg}{role_msg}", ephemeral=True)


# --- 공지사항 모달 UI ---
class NoticeModal(discord.ui.Modal, title="📢 공지사항 작성"):
    notice_title = discord.ui.TextInput(label="공지 제목", placeholder="예: 길드 레이드 일정 안내", required=True, max_length=50)
    notice_content = discord.ui.TextInput(label="공지 본문", placeholder="공지할 내용을 자세히 적어주세요.", style=discord.TextStyle.paragraph, required=True, max_length=1500)
    notice_footer = discord.ui.TextInput(label="추가 안내 / 태그 (선택)", placeholder="예: @everyone 또는 필독 부탁드립니다!", required=False, max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(title=f"📢 {self.notice_title.value}", description=self.notice_content.value, color=discord.Color.blue())
        if self.notice_footer.value:
            embed.add_field(name="📌 전달사항", value=self.notice_footer.value, inline=False)
        embed.set_footer(text=f"작성자: {interaction.user.display_name} • {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
        await interaction.response.send_message(embed=embed)


# --- 직업 선택 버튼 UI ---
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


# --- 실시간 투표 UI ---
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
        embed.set_footer(text=f"주최: {self.author.display_name} • 총 {total_votes}명 참여 (버튼을 눌러 투표/변경)")
        return embed


# --- 슬래시 명령어 정의 ---

# 1. 자기소개 팝업
@bot.tree.command(name="자기소개", description="이름, 마크 닉네임, 출생 연도를 입력하여 닉네임을 설정합니다.")
async def profile(interaction: discord.Interaction):
    await interaction.response.send_modal(ProfileModal())

# 2. 버튼 직업 선택 메뉴 출력
@bot.tree.command(name="직업선택", description="버튼 형태의 직업 선택 메뉴를 출력합니다.")
async def job_select_menu(interaction: discord.Interaction):
    embed = discord.Embed(title="⚔️ 직업 선택", description="아래 버튼을 누르면 닉네임 앞에 `[직업]` 태그가 붙습니다!", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, view=JobButtonView())

# 3. 직업 직접 입력 선택
@bot.tree.command(name="직업", description="직업 이름을 직접 입력하여 변경합니다.")
@app_commands.describe(직업명="선택할 직업 이름")
async def choose_job(interaction: discord.Interaction, 직업명: str):
    if 직업명 not in JOBS:
        await interaction.response.send_message(f"❌ 존재하지 않는 직업입니다. 다시 확인해주세요!\n선택 가능: {', '.join(JOBS)}", ephemeral=True)
        return
    await process_job_selection(interaction, 직업명)

# 4. 직업 목록 확인
@bot.tree.command(name="직업목록", description="선택 가능한 모든 직업 목록을 확인합니다.")
async def job_list(interaction: discord.Interaction):
    job_text = "\n".join([f"• **{job}**" for job in JOBS])
    embed = discord.Embed(title="⚔️ 선택 가능한 직업 목록 ⚔️", description="`/직업선택` 또는 `/직업 [직업명]`을 통해 고를 수 있습니다.\n\n" + job_text, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 5. 직업 추가
@bot.tree.command(name="직업추가", description="선택 목록에 새로운 직업을 추가합니다.")
@app_commands.describe(직업명="추가할 새로운 직업 이름")
async def add_job(interaction: discord.Interaction, 직업명: str):
    await interaction.response.defer(ephemeral=True)
    if 직업명 in JOBS:
        await interaction.followup.send(f"❌ **{직업명}**은(는) 이미 존재합니다.", ephemeral=True)
        return
    JOBS.append(직업명)
    await interaction.followup.send(f"✅ 직업 **[{직업명}]**이(가) 추가되었습니다!\n새로운 버튼 목록을 보려면 `/직업선택`을 다시 입력하세요.", ephemeral=True)

# 6. 직업 삭제
@bot.tree.command(name="직업삭제", description="선택 목록에서 직업을 삭제합니다.")
@app_commands.describe(직업명="삭제할 직업 이름")
async def remove_job(interaction: discord.Interaction, 직업명: str):
    await interaction.response.defer(ephemeral=True)
    if 직업명 not in JOBS:
        await interaction.followup.send(f"❌ **{직업명}** 직업을 찾을 수 없습니다.", ephemeral=True)
        return
    JOBS.remove(직업명)
    await interaction.followup.send(f"🗑️ 직업 **[{직업명}]**이(가) 삭제되었습니다!\n새로운 버튼 목록을 보려면 `/직업선택`을 다시 입력하세요.", ephemeral=True)

# 7. 🪜 통화방 인원 사다리 타기
@bot.tree.command(name="사다리", description="현재 통화방 인원으로 당첨 인원수를 지정하여 사다리 타기를 진행합니다.")
@app_commands.describe(
    당첨인원="당첨될 인원수 (숫자로 입력)",
    당첨항목="당첨 항목 이름 (기본값: 🎉 당첨)",
    꽝항목="꽝 항목 이름 (기본값: ❌ 꽝)"
)
async def ladder_game(interaction: discord.Interaction, 당첨인원: int, 당첨항목: str = "🎉 당첨", 꽝항목: str = "❌ 꽝"):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ 음성 채널(통화방)에 먼저 접속한 뒤 명령어를 입력해 주세요!", ephemeral=True)
        return

    voice_channel = interaction.user.voice.channel
    players = [m.display_name for m in voice_channel.members if not m.bot]
    total_count = len(players)

    if total_count < 2:
        await interaction.response.send_message(f"❌ **[{voice_channel.name}]** 통화방에 최소 2명 이상의 인원이 있어야 사다리를 탈 수 있습니다!", ephemeral=True)
        return

    if 당첨인원 <= 0:
        await interaction.response.send_message("❌ 당첨 인원은 최소 1명 이상이어야 합니다!", ephemeral=True)
        return
    if 당첨인원 >= total_count:
        await interaction.response.send_message(f"❌ 당첨 인원({당첨인원}명)이 통화방 전체 인원({total_count}명)보다 많거나 같을 수 없습니다.", ephemeral=True)
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

    embed = discord.Embed(
        title=f"🪜 [{voice_channel.name}] 사다리 타기 결과 🪜",
        description="\n".join(result_lines),
        color=discord.Color.purple()
    )
    embed.add_field(name="🏆 최종 당첨자", value=", ".join(winners), inline=False)
    embed.set_footer(text=f"주최: {interaction.user.display_name} • 총 {total_count}명 중 {당첨인원}명 당첨")
    
    await interaction.response.send_message(embed=embed)

# 8. 💰 골드 기부 수동 호출 명령어
@bot.tree.command(name="기부알림", description="지정한 유저들에게 일일 골드 기부 요청 알림을 발송합니다.")
@app_commands.describe(
    유저1="기부 요청할 멤버 (필수)",
    유저2="추가 멤버 (선택)",
    유저3="추가 멤버 (선택)",
    유저4="추가 멤버 (선택)",
    유저5="추가 멤버 (선택)",
    유저6="추가 멤버 (선택)",
    유저7="추가 멤버 (선택)",
    유저8="추가 멤버 (선택)",
    유저9="추가 멤버 (선택)",
    유저10="추가 멤버 (선택)",
    추가메시지="알림에 덧붙일 내용 (예: 오늘 23시까지 부탁드립니다!)"
)
async def manual_gold_reminder(
    interaction: discord.Interaction,
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
    추가메시지: Optional[str] = None
):
    raw_users = [유저1, 유저2, 유저3, 유저4, 유저5, 유저6, 유저7, 유저8, 유저9, 유저10]
    target_users = []
    for u in raw_users:
        if u and u not in target_users:
            target_users.append(u)

    mentions_text = " ".join([u.mention for u in target_users])
    
    desc = "길드 성장을 위해 **오늘의 일일 골드 기부**를 잊지 말고 진행해 주세요! ✨"
    if 추가메시지:
        desc += f"\n\n💬 **전달사항:** {추가메시지}"

    embed = discord.Embed(
        title="💰 일일 골드 기부 알림",
        description=desc,
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"발송자: {interaction.user.display_name}")

    await interaction.response.send_message(content=mentions_text, embed=embed)

# 9. 📢 공지사항 작성 팝업
@bot.tree.command(name="공지", description="팝업창을 열어 깔끔한 박스형 공지사항을 작성합니다.")
async def create_notice(interaction: discord.Interaction):
    await interaction.response.send_modal(NoticeModal())

# 10. 🧹 채팅 청소 (누구나 사용 가능)
@bot.tree.command(name="청소", description="지정한 개수만큼 현재 채널의 최근 메시지를 삭제합니다. (누구나 사용 가능)")
@app_commands.describe(개수="삭제할 메시지 개수 (1~100개)")
async def clear_messages(interaction: discord.Interaction, 개수: int):
    if 개수 < 1 or 개수 > 100:
        await interaction.response.send_message("❌ 삭제 개수는 1개부터 100개 사이로 지정해 주세요.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=개수)
        await interaction.followup.send(f"🧹 메시지 **{len(deleted)}개**를 삭제했습니다.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("⚠️ 봇에게 **메시지 관리 권한(Manage Messages)**이 없어 삭제하지 못했습니다. 서버 설정에서 봇 권한을 확인해 주세요!", ephemeral=True)

# 11. 📊 실시간 버튼 투표
@bot.tree.command(name="투표", description="버튼을 눌러 실시간 집계되는 투표를 생성합니다. (최대 5개 항목)")
@app_commands.describe(
    질문="투표 주제 (예: 오늘 몇 시에 모일까요?)",
    항목1="첫 번째 선택지 (필수)",
    항목2="두 번째 선택지 (필수)",
    항목3="세 번째 선택지 (선택)",
    항목4="네 번째 선택지 (선택)",
    항목5="다섯 번째 선택지 (선택)"
)
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
        await interaction.response.send_message("❌ 투표 항목은 최소 2개 이상 입력해야 합니다!", ephemeral=True)
        return

    view = PollView(question=질문, options=options, author=interaction.user)
    embed = view.make_embed()
    await interaction.response.send_message(embed=embed, view=view)


# --- 12. ⚠️ 경고 시스템 ---

# 12-1. 경고 부여 (관리자 전용)
@bot.tree.command(name="경고", description="[관리자 전용] 유저에게 경고를 부여하고 사유를 기록합니다.")
@app_commands.describe(
    유저="경고를 부여할 대상 멤버",
    사유="경고 사유 (예: 규칙 위반, 비매너 등)"
)
async def warn_user(interaction: discord.Interaction, 유저: discord.Member, 사유: str):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 경고 부여는 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    if 유저.bot:
        await interaction.response.send_message("❌ 봇에게는 경고를 부여할 수 없습니다.", ephemeral=True)
        return

    warnings = load_warnings()
    user_id_str = str(유저.id)
    
    if user_id_str not in warnings:
        warnings[user_id_str] = []

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    warnings[user_id_str].append({
        "reason": 사유,
        "moderator": interaction.user.display_name,
        "date": now_str
    })
    save_warnings(warnings)

    count = len(warnings[user_id_str])

    embed = discord.Embed(
        title="⚠️ 경고가 부여되었습니다",
        description=f"{유저.mention} 님에게 경고가 1회 누적되었습니다.\n규칙을 준수해 주시기 바랍니다.",
        color=discord.Color.red()
    )
    embed.add_field(name="👤 대상자", value=유저.display_name, inline=True)
    embed.add_field(name="🚨 누적 경고 횟수", value=f"**{count}회**", inline=True)
    embed.add_field(name="📝 사유", value=사유, inline=False)
    embed.set_footer(text=f"담당자: {interaction.user.display_name} • 일시: {now_str}")

    await interaction.response.send_message(content=유저.mention, embed=embed)


# 12-2. 경고 확인 (누구나 본인/타인 확인 가능)
@bot.tree.command(name="경고확인", description="지정한 유저(또는 본인)의 누적 경고 횟수 및 내역을 확인합니다.")
@app_commands.describe(유저="조회할 대상 멤버 (비워두면 본인의 경고 내역을 조회합니다)")
async def check_warnings(interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
    target = 유저 if 유저 else interaction.user
    warnings = load_warnings()
    user_id_str = str(target.id)

    records = warnings.get(user_id_str, [])
    count = len(records)

    if count == 0:
        await interaction.response.send_message(f"✨ **{target.display_name}** 님은 받은 경고가 없습니다! (누적 0회)", ephemeral=True)
        return

    desc_lines = []
    for idx, r in enumerate(records, 1):
        desc_lines.append(f"**{idx}.** {r['reason']} *(by {r['moderator']} • {r['date']})*")

    embed = discord.Embed(
        title=f"📋 [{target.display_name}] 님의 경고 내역",
        description=f"총 누적 경고: **{count}회**\n\n" + "\n".join(desc_lines),
        color=discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# 12-3. 경고 차감 (관리자 전용)
@bot.tree.command(name="경고차감", description="[관리자 전용] 유저의 경고를 지정한 횟수만큼 최근 기록부터 차감합니다.")
@app_commands.describe(
    유저="경고를 차감할 대상 멤버",
    개수="차감할 경고 횟수 (기본 1개)"
)
async def remove_warn(interaction: discord.Interaction, 유저: discord.Member, 개수: Optional[int] = 1):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 경고 차감은 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    warnings = load_warnings()
    user_id_str = str(유저.id)

    if user_id_str not in warnings or len(warnings[user_id_str]) == 0:
        await interaction.response.send_message(f"❌ **{유저.display_name}** 님은 차감할 경고가 없습니다.", ephemeral=True)
        return

    current_count = len(warnings[user_id_str])
    deduct_count = min(개수, current_count)
    
    warnings[user_id_str] = warnings[user_id_str][:-deduct_count]
    save_warnings(warnings)

    new_count = len(warnings[user_id_str])
    await interaction.response.send_message(f"✅ **{유저.display_name}** 님의 경고가 **{deduct_count}회** 차감되었습니다. (현재 누적: **{new_count}회**)")


# 12-4. 경고 초기화 (관리자 전용)
@bot.tree.command(name="경고초기화", description="[관리자 전용] 해당 유저의 모든 경고 기록을 완전히 초기화(0회)합니다.")
@app_commands.describe(유저="경고를 전체 초기화할 대상 멤버")
async def clear_warn(interaction: discord.Interaction, 유저: discord.Member):
    if not is_admin_or_mod(interaction):
        await interaction.response.send_message("❌ 경고 초기화는 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    warnings = load_warnings()
    user_id_str = str(유저.id)

    if user_id_str in warnings:
        warnings[user_id_str] = []
        save_warnings(warnings)

    await interaction.response.send_message(f"🧹 **{유저.display_name}** 님의 모든 경고 기록이 초기화되었습니다. (누적 0회)")


# --- 봇 준비 및 동기화 ---
@bot.event
async def on_ready():
    bot.add_view(JobButtonView())
    
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"✅ 현재 서버에 {len(synced)}개의 명령어 동기화 완료!")
    except Exception as e:
        print(f"❌ 동기화 중 오류 발생: {e}")
        
    print(f"로그인 완료: {bot.user.name}")


# 실행부
if __name__ == "__main__":
    keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    bot.run(token)
