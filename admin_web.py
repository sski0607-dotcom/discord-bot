'use strict';
const app = document.querySelector('#app');
const csrf = app.dataset.csrf;
const $ = s => document.querySelector(s);
let data = null, page = 'ranking', selected = null;
let currentFilter = 'ALL', searchQuery = '';

const pages = {ranking:'주간 통화 랭킹',daily:'요일별 상세 기록',roster:'길드원 접속 명부',notice:'공지 발송'};
const secs = n => Math.floor(n / 3600) + '시간 ' + Math.floor((n % 3600) / 60) + '분';
const esc = t => String(t ?? '').replaceAll('&', '&').replaceAll('<', '<').replaceAll('>', '>').replaceAll('"', '"');
const showError = e => {$('#alert').textContent = e; window.scrollTo({top:0,behavior:'smooth'});};

function formatRoleLabel(v) {
  let base = v.roles.join(', ');
  if (v.is_probation && v.prob_desc) {
    base += ' [' + v.prob_desc + ']';
  }
  if (v.daily_req) {
    base += ' (일1h)';
  }
  return base;
}

function formatDayCell(v, sec, idx) {
  const flag = (v.daily_flags && v.daily_flags[idx]) ? v.daily_flags[idx] : '';
  if (flag === 'ok') return '✅ ' + secs(sec);
  if (flag === 'fail') return '⚠️ ' + secs(sec);
  if (flag === 'exempt') return '➖ ' + secs(sec);
  return secs(sec);
}

function ensureFilterBar() {
  const tablePanel = $('#table-panel');
  if (!tablePanel || $('#filter-bar')) return;

  const bar = document.createElement('div');
  bar.id = 'filter-bar';
  bar.className = 'actions';
  bar.style.marginTop = '0';
  bar.style.marginBottom = '16px';

  const filters = [
    ['ALL', '전체'],
    ['월영', '월영'],
    ['영월', '영월'],
    ['은월', '은월'],
    ['PROB', '🌱 수습생만']
  ];

  filters.forEach(f => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.dataset.filter = f[0];
    btn.textContent = f[1];
    if (f[0] === currentFilter) btn.className = 'primary';
    btn.addEventListener('click', () => {
      currentFilter = f[0];
      bar.querySelectorAll('[data-filter]').forEach(b => {
        b.className = (b.dataset.filter === currentFilter) ? 'primary' : '';
      });
      render();
    });
    bar.appendChild(btn);
  });

  const searchInput = document.createElement('input');
  searchInput.type = 'text';
  searchInput.id = 'member-search';
  searchInput.placeholder = '🔍 닉네임 검색...';
  searchInput.style.marginTop = '0';
  searchInput.style.maxWidth = '240px';
  searchInput.addEventListener('input', e => {
    searchQuery = e.target.value.trim().toLowerCase();
    render();
  });
  bar.appendChild(searchInput);

  tablePanel.insertBefore(bar, tablePanel.firstChild);
}

function getFilteredMembers(members) {
  return members.filter(v => {
    const roleStr = v.roles.join(' ');
    if (currentFilter === 'PROB' && !v.is_probation) return false;
    if (currentFilter !== 'ALL' && currentFilter !== 'PROB' && !roleStr.includes(currentFilter)) return false;
    if (searchQuery && !v.name.toLowerCase().includes(searchQuery)) return false;
    return true;
  });
}

async function api(path, options={}) {
  const r = await fetch('/admin/api/'+path, {credentials:'same-origin',...options});
  const d = await r.json().catch(()=>({error:'서버 응답 오류'}));
  if(!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

async function load() {
  $('#alert').textContent=''; $('#refresh').disabled=true;
  try {data=await api('snapshot'); render();}
  catch(e){showError(e.message);}
  finally{$('#refresh').disabled=false;}
}

function render() {
  ensureFilterBar();
  $('#page-title').textContent=pages[page];
  $('#last-updated').textContent=data?('최종 조회: ' + data.updated_at + ' (한국시간)'):'데이터 없음';
  $('#notice-panel').classList.toggle('hidden',page!=='notice');
  $('#table-panel').classList.toggle('hidden',page==='notice');
  $('#summary').classList.toggle('hidden',page==='notice');
  document.querySelectorAll('[data-page]').forEach(b=>b.classList.toggle('active',b.dataset.page===page));
  if(!data || page==='notice') return;

  const allM = data.members;
  const m = getFilteredMembers(allM);
  const probCount = allM.filter(v=>v.is_probation).length;
  const cards=[
    ['표시 인원 / 전체', m.length + '명 / ' + allM.length + '명'],
    ['현재 통화 접속', allM.filter(v=>v.voice).length + '명'],
    ['수습생 / 휴가자', '수습 ' + probCount + '명 · 휴가 ' + allM.filter(v=>v.is_exempt).length + '명'],
    ['기본 주간 목표', secs(data.week_goal_sec)]
  ];
  $('#summary').innerHTML=cards.map(c=>'<div class="stat"><span>'+esc(c[0])+'</span><strong>'+esc(c[1])+'</strong></div>').join('');

  let head=[], rows=[];
  if(page==='ranking') {
    head=['순위','닉네임','소속 / 수습·기준','누적 시간','주간 목표','달성 상태','경고'];
    let rank=0;
    rows=m.map(v=>{
      if(!v.is_exempt)rank++;
      let pct=v.goal_sec?Math.min(100,Math.floor(v.week_sec/v.goal_sec*100)):100;
      let statusText = v.is_exempt ? '면제' : (pct + '%');
      if(!v.is_exempt && v.daily_req && v.missed_days && v.missed_days.length > 0){
        statusText += ' (⚠️일일미달: ' + v.missed_days.join(',') + ')';
      }
      let goalText = secs(v.goal_sec) + (v.daily_req ? ' + 일1h' : '');
      return [v.is_exempt?'휴가':String(rank),v.name,formatRoleLabel(v),secs(v.week_sec),goalText,statusText,v.warn_count+'회'];
    });
  }else if(page==='daily'){
    head=['닉네임','소속 / 수습·기준',...data.week_days.map((x,i)=>'월화수목금토일'[i]+' ('+x.slice(5)+')'),'주간 합계'];
    rows=m.map(v=>[v.name,formatRoleLabel(v),...v.daily_secs.map((sec,idx)=>formatDayCell(v,sec,idx)),secs(v.week_sec)]);
  }else{
    head=['닉네임','소속 / 수습·기준','현재 음성 채널','출결 상태','경고'];
    rows=m.map(v=>{
      let st = v.is_exempt ? ('휴가 ' + v.exempt_desc) : '정상 집계 (주간 11h)';
      if(!v.is_exempt && v.daily_req){
        st = (v.missed_days && v.missed_days.length > 0)
          ? ('⚠️ 일일 1h 미달 (' + v.missed_days.join(',') + ')')
          : '✅ 일일 1h 정상 달성 중';
      }
      return [v.name,formatRoleLabel(v),v.voice||'미접속',st,v.warn_count+'회'];
    });
  }
  $('#table-head').innerHTML='<tr>'+head.map(v=>'<th>'+esc(v)+'</th>').join('')+'</tr>';
  $('#table-body').innerHTML=rows.map((row,i)=>'<tr class="member-row" data-index="'+i+'">'+row.map(v=>'<td>'+esc(v)+'</td>').join('')+'</tr>').join('');
  document.querySelectorAll('[data-index]').forEach(tr=>tr.addEventListener('click',()=>openMember(m[Number(tr.dataset.index)])));
}

function openMember(m){
  selected=m;
  $('#dialog-title').textContent=m.name+' · 관리';
  const ruleText = m.daily_req ? '일일 1시간 + 주간 ' + secs(m.goal_sec) + ' (11/14 이후 수습)' : '주간 ' + secs(m.goal_sec) + ' (일일 제한 없음)';
  const dailyStatus = m.daily_req
    ? ((m.missed_days && m.missed_days.length > 0) ? ('⚠️ 미달 요일: ' + m.missed_days.join(', ')) : '✅ 미달 요일 없음')
    : '해당 없음 (정식 길드원)';
  const probStatus = m.is_probation
    ? ('🌱 ' + m.prob_desc + ' (입단: ' + m.join_date + ')')
    : '정식 길드원';

  const fields=[
    ['소속',m.roles.join(', ')],
    ['수습 상태',probStatus],
    ['적용 출결 규칙',ruleText],
    ['주간 누적',secs(m.week_sec)],
    ['일일 1시간 체크',dailyStatus],
    ['경고',m.warn_count+'회'],
    ['음성 채널',m.voice||'미접속'],
    ['휴가 상태',m.is_exempt?m.exempt_desc:'정상']
  ];
  $('#member-content').innerHTML='<div class="detail-grid">'+fields.map(f=>'<div><span>'+esc(f[0])+'</span>'+esc(f[1])+'</div>').join('')+'</div><h4>요일별 시간</h4><p>'+m.daily_secs.map((v,i)=>'월화수목금토일'[i]+': '+formatDayCell(m,v,i)).join(' / ')+'</p>';

  const btnPromote = document.querySelector('[data-action="prob_promote"]');
  const btnExtend = document.querySelector('[data-action="prob_extend"]');
  if (btnPromote) btnPromote.classList.toggle('hidden', !m.is_probation);
  if (btnExtend) btnExtend.classList.toggle('hidden', !m.is_probation);

  $('#member-dialog').showModal();
}

async function post(action, extra, userId=0){
  try{
    const r=await api('action',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({action,user_id:userId,...extra})});
    alert(r.message); $('#member-dialog').close(); await load();
  }catch(e){showError(e.message);}
}

async function handleMemberAction(action){
  if(!selected)return;
  let extra={};
  if(action==='warn_add'){const v=prompt('경고 사유 (1~250자)');if(v===null)return;extra.reason=v;}
  if(action==='warn_remove'){
    if(!selected.warn_count){alert('현재 차감할 경고가 없습니다.');return;}
    const v=prompt('차감할 경고 횟수를 입력하세요 (현재 '+selected.warn_count+'회):','1');
    if(v===null)return;
    extra.count=Number(v)||1;
  }
  if(action==='warn_clear'){
    if(!selected.warn_count){alert('현재 초기화할 경고가 없습니다.');return;}
    if(!confirm('['+selected.name+'] 님의 모든 경고('+selected.warn_count+'회)를 초기화할까요?'))return;
  }
  if(action==='prob_promote'){
    if(!confirm('['+selected.name+'] 님을 정식 길드원으로 즉시 승급할까요?'))return;
  }
  if(action==='prob_extend'){
    const v=prompt('연장할 수습 일수를 입력하세요 (기본 7일):','7');
    if(v===null)return;
    extra.days=Number(v)||7;
  }
  if(action==='exempt_add'){const until=prompt('휴가 종료일 (YYYY-MM-DD)');if(until===null)return;const reason=prompt('휴가 사유');if(reason===null)return;extra={until,reason};}
  if(action==='goal_set'){const hours=prompt('주간 목표 시간 (0~11)');if(hours===null)return;const reason=prompt('조정 사유','개별 조정');if(reason===null)return;extra={hours,reason};}
  if(action==='exempt_remove'&&!confirm('이 길드원의 휴가를 해제할까요?'))return;
  await post(action,extra,selected.id);
}

const actionBox = document.querySelector('#member-dialog .actions');
if(actionBox && !actionBox.querySelector('[data-action="warn_remove"]')){
  const extraButtons = [
    ['warn_remove', '➖ 경고 차감', ''],
    ['warn_clear', '🧹 경고 초기화', ''],
    ['prob_promote', '⬆️ 정식 승급', 'primary'],
    ['prob_extend', '⏳ 수습 +7일 연장', '']
  ];
  extraButtons.forEach(item => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.dataset.action = item[0];
    btn.textContent = item[1];
    if (item[2]) btn.className = item[2];
    actionBox.appendChild(btn);
  });
}

document.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>{page=b.dataset.page;render();}));
$('#refresh').addEventListener('click',load);
document.querySelectorAll('[data-action]').forEach(b=>b.addEventListener('click',()=>handleMemberAction(b.dataset.action)));
$('#send-notice').addEventListener('click',async()=>{
  const title=$('#notice-title').value.trim(),content=$('#notice-content').value.trim(),everyone=$('#mention-everyone').checked;
  if(!title||!content)return showError('제목과 내용을 입력하세요.');
  if(!confirm('공지 채널에 발송할까요? '+(everyone?'(@everyone 포함)':'')))return;
  await post('notice',{title,content,everyone});
  $('#notice-title').value='';$('#notice-content').value='';
});
$('#logout').addEventListener('click',async()=>{
  try {await fetch('/admin/logout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:'{}'});} finally {location.href='/admin/login';}
});
load();
