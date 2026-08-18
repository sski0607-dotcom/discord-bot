import os
import random
import threading
import datetime
from zoneinfo import ZoneInfo
from flask import Flask
import discord
from discord import app_commands
from discord.ext import commands, tasks

# --- 0. Render 슬립 방지용 백그라운드 웹 서버 ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run)
    t.start()
# -----------------------------------------------

# 1. 봇 권한 및 객체 설정
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True  # 음성 채널 멤버 감지 권한

bot = commands.Bot(command_prefix="!", intents=intents)

# Render 환경 변수 설정
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))

# 2. 기본 직업 목록 정의 (가변 리스트)
JOBS = [
    "검호", "정식기사", "추적자", "암살자", "위자드",
    "창성", "진혼자", "바바리안", "비스트테이머", "클레릭",
    "월영의 그림자", "드루이드", "백야기사", "근위대장", "중급 대장장이",
    "검성", "광부"
]


# 공통 직업 처리 함수
async def process_job_selection(interaction: discord.Interaction, job_name: str):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    member = interaction.user
    prefix = f"[{job_name}]"

    # 1. 기존 직업 역할 제거
    for j in JOBS:
        role = discord.utils.get(guild.roles, name=j)
        if role and role in member.roles:
            await member.remove_roles(role)

    # 2. 새 직업 역할 부여 (없으면 자동 생성)
    new_role = discord.utils.get(guild.roles, name=job_name)
    if not new_role:
        new_role = await guild.create_role(name=job_name)

    await member.add_roles(new_role)

    # 3. 닉네임 변경 처리 (기존 [직업] 제거 후 새 직업 부여)
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

    await interaction.followup.send(
        f"✅ **[{job_name}]** 직업을 선택하셨습니다!\n{nickname_msg}",
        ephemeral=True
    )


# --- 자기소개 모달 UI (팝업창) ---
class ProfileModal(discord.ui.Modal, title="자기소개 입력"):
    name = discord.ui.TextInput(
        label="이름 (또는 별명)",
        placeholder="예: 쨈",
        required=True,
        max_length=15
    )
    mc_name = discord.ui.TextInput(
        label="마인크래프트 닉네임",
        placeholder="예: _s2_jammy",
        required=True,
        max_length=25
    )
    birth_year = discord.ui.TextInput(
        label="출생 연도 (두 자리)",
        placeholder="예: 06",
        required=True,
        max_length=4
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        user = interaction.user
        current_nick = user.display_name

        # 기존 닉네임에 [직업] 태그가 붙어있으면 말머리 보존
        prefix = ""
        if "]" in current_nick and current_nick.startswith("["):
            prefix = current_nick.split("]")[0] + "] "

        # 결과 양식: [직업] 이름 / 마크닉네임 / 년생
        new_nick = f"{prefix}{self.name.value} / {self.mc_name.value} / {self.birth_year.value}"

        # 1. 닉네임 변경
        try:
            await user.edit(nick=new_nick)
            nick_msg = f"✅ 닉네임이 변경되었습니다!\n**{new_nick}**"
        except discord.Forbidden:
            nick_msg = "⚠️ (봇 권한 부족 또는 최고권한자 계정이라 닉네임 수정은 건너뛰었습니다.)"

        # 2. 수습주민 역할 자동 부여
        ROLE_NAME = "수습 담이🐣"
        role_msg = ""
        
        target_role = discord.utils.get(guild.roles, name=ROLE_NAME)
        if target_role:
            try:
                await user.add_roles(target_role)
                role_msg = f"\n🔰 **[{ROLE_NAME}]** 역할이 자동으로 부여되었습니다!"
            except discord.Forbidden:
                role_msg = f"\n⚠️ 봇의 역할 순위가 낮아 **[{ROLE_NAME}]** 역할을 부여하지 못했습니다."
        else:
            role_msg = f"\n⚠️ 서버에 **[{ROLE_NAME}]** 역할이 존재하지 않아 역할 부여를 건너뛰었습니다."

        await interaction.followup.send(
            f"{nick_msg}{role_msg}",
            ephemeral=True
        )


# --- 직업 선택 버튼 UI ---
class JobButton(discord.ui.Button):
    def __init__(self, job_name: str):
        super().__init__(
            label=job_name,
            style=discord.ButtonStyle.primary,
            custom_id=f"job_button_{job_name}"
        )
        self.job_name = job_name

    async def callback(self, interaction: discord.Interaction):
        await process_job_selection(interaction, self.job_name)


class JobButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for job in JOBS[:25]:
            self.add_item(JobButton(job))


# --- 슬래시 명령어 정의 ---

# 1. 자기소개 팝업
@bot.tree.command(name="자기소개", description="이름, 마크 닉네임, 출생 연도를 입력하여 닉네임을 설정합니다.")
async def profile(interaction: discord.Interaction):
    await interaction.response.send_modal(ProfileModal())


# 2. 버튼 메시지 출력
@bot.tree.command(name="직업선택", description="버튼 형태의 직업 선택 메뉴를 출력합니다.")
async def job_select_menu(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚔️ 직업 선택",
        description="아래 버튼을 누르면 닉네임 앞에 `[직업]` 태그가 붙습니다!",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, view=JobButtonView())


# 3. 직업 직접 입력 선택
@bot.tree.command(name="직업", description="직업 이름을 직접 입력하여 변경합니다.")
@app_commands.describe(직업명="선택할 직업 이름")
async def choose_job(interaction: discord.Interaction, 직업명: str):
    if 직업명 not in JOBS:
        await interaction.response.send_message(
            f"❌ 존재하지 않는 직업입니다. 다시 확인해주세요!\n선택 가능: {', '.join(JOBS)}",
            ephemeral=True
        )
        return
    await process_job_selection(interaction, 직업명)


# 4. 직업 목록 확인
@bot.tree.command(name="직업목록", description="선택 가능한 모든 직업 목록을 확인합니다.")
async def job_list(interaction: discord.Interaction):
    job_text = "\n".join([f"• **{job}**" for job in JOBS])
    embed = discord.Embed(
        title="⚔️ 선택 가능한 직업 목록 ⚔️",
        description=f"`/직업선택` 또는 `/직업 [직업명]`을 통해 고를 수 있습니다.\n\n{job_text}",
        color=discord.Color.blue()
    )
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
    await interaction.followup.send(
        f"✅ 직업 **[{직업명}]**이(가) 추가되었습니다!\n새로운 버튼 목록을 보려면 `/직업선택`을 다시 입력하세요.",
        ephemeral=True
    )


# 6. 직업 삭제
@bot.tree.command(name="직업삭제", description="선택 목록에서 직업을 삭제합니다.")
@app_commands.describe(직업명="삭제할 직업 이름")
async def remove_job(interaction: discord.Interaction, 직업명: str):
    await interaction.response.defer(ephemeral=True)

    if 직업명 not in JOBS:
        await interaction.followup.send(f"❌ **{직업명}** 직업을 찾을 수 없습니다.", ephemeral=True)
        return

    JOBS.remove(직업명)
    await interaction.followup.send(
        f"🗑️ 직업 **[{직업명}]**이(가) 삭제되었습니다!\n새로운 버튼 목록을 보려면 `/직업선택`을 다시 입력하세요.",
        ephemeral=True
    )


# 7. 🪜 통화방 인원 자동 사다리 타기
@bot.tree.command(name="사다리", description="현재 참가 중인 음성 통화방 인원으로 사다리 타기를 진행합니다.")
@app_commands.describe(결과="쉼표(,)로 구분하여 결과 항목 입력 (예: 당첨, 꽝)")
async def ladder_game(interaction: discord.Interaction, 결과: str):
    # 명령어를 친 유저가 음성 채널에 있는지 확인
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "❌ 음성 채널(통화방)에 먼저 접속한 뒤 명령어를 입력해 주세요!",
            ephemeral=True
        )
        return

    voice_channel = interaction.user.voice.channel
    # 통화방 멤버 중 봇을 제외한 실제 유저 닉네임 리스트
    players = [m.display_name for m in voice_channel.members if not m.bot]

    if len(players) < 2:
        await interaction.response.send_message(
            f"❌ **[{voice_channel.name}]** 통화방에 최소 2명 이상의 인원이 있어야 사다리를 탈 수 있습니다!",
            ephemeral=True
        )
        return

    # 결과 항목 분리
    results = [r.strip() for r in 결과.split(",") if r.strip()]

    # 결과 항목이 인원수보다 적으면 "통과"로 채우기
    while len(results) < len(players):
        results.append("통과")

    # 결과 항목이 인원수보다 많으면 인원수에 맞추기
    if len(results) > len(players):
        results = results[:len(players)]

    # 랜덤 셔플
    shuffled_results = results.copy()
    random.shuffle(shuffled_results)

    result_lines = []
    for player, res in zip(players, shuffled_results):
        result_lines.append(f"👤 **{player}** ➔ 🎁 **{res}**")

    embed = discord.Embed(
        title=f"🪜 [{voice_channel.name}] 사다리 타기 결과 🪜",
        description="\n".join(result_lines),
        color=discord.Color.purple()
    )
    embed.set_footer(text=f"주최: {interaction.user.display_name} • 총 {len(players)}명 참여")

    await interaction.response.send_message(embed=embed)


# --- ⏰ 매일 오전 12시(자정 00:00 KST) 일일 골드 기부 알림 태스크 ---
midnight_kst = datetime.time(hour=0, minute=0, tzinfo=ZoneInfo("Asia/Seoul"))

@tasks.loop(time=midnight_kst)
async def daily_gold_reminder():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(ANNOUNCE_CHANNEL_ID)
    if not channel:
        channel = guild.system_channel or guild.text_channels[0]

    role_dami = discord.utils.get(guild.roles, name="담이 🐥")
    role_trainee = discord.utils.get(guild.roles, name="수습 담이🐣")

    mention_list = []
    if role_dami:
        mention_list.append(role_dami.mention)
    if role_trainee:
        mention_list.append(role_trainee.mention)

    mentions_text = " ".join(mention_list) if mention_list else "@담이 🐥 @수습 담이🐣"

    embed = discord.Embed(
        title="💰 일일 골드 기부 안내",
        description="새로운 하루가 시작되었습니다!\n\n길드 성장을 위해 일일 골드 기부를 꼭 부탁드립니다.",
        color=discord.Color.gold()
    )

    await channel.send(content=mentions_text, embed=embed)


# --- 봇 준비 및 동기화 ---
@bot.event
async def on_ready():
    bot.add_view(JobButtonView())

    if not daily_gold_reminder.is_running():
        daily_gold_reminder.start()

    try:
        guild = discord.Object(id=GUILD_ID)

        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"✅ 현재 서버에 {len(synced)}개의 새 명령어 동기화 완료!")

        await bot.tree.sync(guild=None)
        print("🧹 옛날 글로벌 유령 명령어 청소 완료!")

    except Exception as e:
        print(f"❌ 동기화 중 오류 발생: {e}")

    print(f"로그인 완료: {bot.user.name}")


# 실행부
if __name__ == '__main__':
    keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    bot.run(token)
