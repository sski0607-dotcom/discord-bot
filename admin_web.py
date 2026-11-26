import asyncio
import hmac
import os
import secrets
from datetime import datetime, timedelta, date
from functools import wraps
from urllib.parse import urlencode

import discord
import requests
from flask import abort, jsonify, redirect, render_template, request, session, url_for

DISCORD_API = 'https://discord.com/api/v10'


def init_admin_web(app, bot, b):
    secret = os.environ.get('WEB_SESSION_SECRET', '')
    if len(secret) < 32:
        raise RuntimeError('WEB_SESSION_SECRET 환경변수에 32자 이상의 무작위 문자열을 설정하세요.')
    if not all(os.environ.get(k) for k in ('DISCORD_CLIENT_ID', 'DISCORD_CLIENT_SECRET', 'DISCORD_REDIRECT_URI')):
        raise RuntimeError('DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI 설정이 필요합니다.')
    redirect_uri = os.environ['DISCORD_REDIRECT_URI']
    if not redirect_uri.startswith('https://'):
        raise RuntimeError('DISCORD_REDIRECT_URI는 HTTPS 주소여야 합니다.')
    if not int(b['GUILD_ID']):
        raise RuntimeError('GUILD_ID 환경변수 설정이 필요합니다.')

    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=16 * 1024
    )
    oauth_id = os.environ['DISCORD_CLIENT_ID']
    oauth_secret = os.environ['DISCORD_CLIENT_SECRET']
    guild_id = b['GUILD_ID']
    allowed_ids = {int(x.strip()) for x in os.environ.get('ADMIN_ALLOWED_USER_IDS', '').split(',') if x.strip().isdigit()}

    def on_bot(coro):
        if not bot.is_ready() or bot.loop is None or not bot.loop.is_running():
            raise RuntimeError('디스코드 봇이 아직 준비되지 않았습니다.')
        return asyncio.run_coroutine_threadsafe(coro, bot.loop).result(timeout=35)

    async def auth_member(uid):
        guild = bot.get_guild(guild_id)
        if guild is None:
            return None
        try:
            member = guild.get_member(uid) or await guild.fetch_member(uid)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        perms = member.guild_permissions
        if not (perms.administrator or perms.manage_guild or perms.moderate_members or perms.kick_members):
            return None
        if allowed_ids and uid not in allowed_ids:
            return None
        return member

    def require_admin(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            uid = session.get('uid')
            if not uid:
                if request.path.startswith('/admin/api/'):
                    return jsonify(error='로그인이 필요합니다.'), 401
                return redirect(url_for('admin_login'))
            try:
                member = on_bot(auth_member(int(uid)))
            except (ValueError, RuntimeError, TimeoutError) as exc:
                return jsonify(error=str(exc)), 503
            if member is None:
                session.clear()
                if request.path.startswith('/admin/api/'):
                    return jsonify(error='관리자 권한이 없습니다.'), 403
                return redirect(url_for('admin_denied'))
            if request.method == 'POST':
                csrf = request.headers.get('X-CSRF-Token', '')
                if not csrf or not hmac.compare_digest(csrf, str(session.get('csrf', ''))):
                    return jsonify(error='CSRF 검증 실패. 새로고침 후 다시 시도하세요.'), 403
                if not request.is_json:
                    return jsonify(error='JSON 요청만 허용됩니다.'), 415
            return fn(*args, **kwargs)
        return inner

    @app.after_request
    def security_headers(resp):
        if request.path.startswith('/admin'):
            resp.headers['Cache-Control'] = 'no-store'
            resp.headers['X-Content-Type-Options'] = 'nosniff'
            resp.headers['X-Frame-Options'] = 'DENY'
            resp.headers['Referrer-Policy'] = 'same-origin'
            resp.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self' https://discord.com; frame-ancestors 'none'"
        return resp

    @app.route('/admin/login')
    def admin_login():
        state = secrets.token_urlsafe(32)
        session.clear()
        session['oauth_state'] = state
        session.permanent = True
        params = {
            'client_id': oauth_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': 'identify',
            'state': state,
            'prompt': 'none' if request.args.get('silent') == '1' else 'consent'
        }
        return redirect('https://discord.com/oauth2/authorize?' + urlencode(params))

    @app.route('/admin/callback')
    def admin_callback():
        state = session.pop('oauth_state', None)
        if not state or not hmac.compare_digest(str(state), request.args.get('state', '')):
            return 'OAuth2 state 검증 실패. /admin/login에서 다시 시도하세요.', 403
        code = request.args.get('code', '')
        if not code:
            return 'Discord 로그인이 취소되었습니다.', 400
        try:
            r = requests.post(DISCORD_API + '/oauth2/token', data={
                'client_id': oauth_id,
                'client_secret': oauth_secret,
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': redirect_uri
            }, timeout=12)
            r.raise_for_status()
            access_token = r.json()['access_token']
            u = requests.get(DISCORD_API + '/users/@me', headers={'Authorization': 'Bearer ' + access_token}, timeout=12)
            u.raise_for_status()
            user = u.json()
            uid = int(user['id'])
            member = on_bot(auth_member(uid))
            if member is None:
                session.clear()
                return redirect(url_for('admin_denied'))
            session.clear()
            session.permanent = True
            session['uid'] = uid
            session['display'] = user.get('global_name') or user.get('username') or str(uid)
            session['csrf'] = secrets.token_urlsafe(32)
            return redirect(url_for('admin_index'))
        except (requests.RequestException, KeyError, ValueError, RuntimeError, TimeoutError):
            session.clear()
            return '로그인 처리에 실패했습니다. Discord 설정 및 봇 연결 상태를 확인하세요.', 502

    @app.route('/admin/denied')
    def admin_denied():
        return '접근이 거부되었습니다. 이 서버의 관리 권한이 있는 Discord 계정만 사용할 수 있습니다.', 403

    @app.route('/admin/logout', methods=['POST'])
    @require_admin
    def admin_logout():
        session.clear()
        return jsonify(ok=True)

    @app.route('/admin')
    @require_admin
    def admin_index():
        return render_template('dashboard.html', user=session.get('display'), csrf=session['csrf'])

    async def get_snapshot(force_sync=False):
        guild = bot.get_guild(guild_id)
        if guild is None:
            raise RuntimeError('대상 Discord 서버를 찾을 수 없습니다.')

        if force_sync or guild.id not in b['VOICE_DAILY_CACHE'] or 'WARN_CACHE' not in b:
            warnings, records = await b['fetch_database_from_channel'](guild)
            b['VOICE_DAILY_CACHE'][guild.id] = records
            b['WARN_CACHE'] = warnings
        else:
            records = b['VOICE_DAILY_CACHE'][guild.id]
            warnings = b['WARN_CACHE']

        now = datetime.now(b['KST'])
        now_date = now.date()
        cur_weekday = now.weekday()
        monday_dt = now_date - timedelta(days=cur_weekday)
        day_objs = [monday_dt + timedelta(days=i) for i in range(7)]
        days = [d.strftime('%Y-%m-%d') for d in day_objs]
        cutoff_date = b.get('DAILY_RULE_CUTOFF_DATE', date(2026, 11, 14))
        weekdays_ko = b.get('WEEKDAYS_KO', ['월', '화', '수', '목', '금', '토', '일'])
        base_prob_days = b['get_probation_days'](guild_id) if 'get_probation_days' in b else 7
        prob_recs = b.get('PROBATION_RECORDS', {}).get(guild_id, {})
        prob_map = b.get('PROBATION_ROLE_MAP', {})

        members = []
        for m in guild.members:
            if m.bot or not b['is_target_member'](m):
                continue
            u_str = str(m.id)
            daily = [records.get(day, {}).get(u_str, 0) for day in days]
            sess = b['VOICE_ACTIVE_SESSIONS'].get(m.id)
            if sess and sess.get('active') and sess.get('guild_id') == guild_id and sess.get('start_time'):
                daily[cur_weekday] += max(0, int((now - sess['start_time']).total_seconds()))

            target, reason = b['get_user_weekly_goal'](guild_id, m.id, now)
            ex = b['is_voice_exempt'](guild_id, m.id, now_date)

            daily_req = b['requires_daily_one_hour'](guild_id, m) if 'requires_daily_one_hour' in b else False
            join_dt = b['get_member_effective_join_date'](guild_id, m) if 'get_member_effective_join_date' in b else monday_dt
            rec_excluded = b.get('VOICE_RECURRING_DAYS', {}).get(guild_id, {}).get(u_str, {}).get('days', [])

            is_prob = any(r.name in prob_map for r in m.roles)
            prob_desc = ''
            if is_prob:
                rec = prob_recs.get(u_str, {})
                extra_d = int(rec.get('extra_days', 0))
                total_req_d = base_prob_days + extra_d
                elapsed_d = (now_date - join_dt).days
                remain_d = total_req_d - elapsed_d
                ext_note = f' (+{extra_d}일 연장)' if extra_d > 0 else ''
                if remain_d <= 0:
                    prob_desc = f'🔔승급대상 ({elapsed_d}/{total_req_d}일){ext_note}'
                else:
                    prob_desc = f'D-{remain_d} ({elapsed_d}/{total_req_d}일){ext_note}'

            daily_flags = []
            missed_days = []
            for idx, d_obj in enumerate(day_objs):
                day_ex = b['is_voice_exempt'](guild_id, m.id, d_obj)
                if not daily_req or idx > cur_weekday:
                    daily_flags.append('')
                elif day_ex or d_obj < join_dt or d_obj < cutoff_date or idx in rec_excluded:
                    daily_flags.append('exempt')
                elif daily[idx] >= 3600:
                    daily_flags.append('ok')
                else:
                    daily_flags.append('fail')
                    missed_days.append(weekdays_ko[idx])

            exempt_period = (f"{ex['start']}~{ex['until']}" if ex.get('start') else f"~{ex['until']}") if ex else ''
            members.append({
                'id': u_str,
                'name': m.display_name,
                'roles': [r.name for r in m.roles if r.name in b['TARGET_ROLES']],
                'daily_secs': daily,
                'week_sec': sum(daily),
                'goal_sec': target,
                'goal_reason': reason or '',
                'daily_req': daily_req,
                'daily_flags': daily_flags,
                'missed_days': missed_days,
                'is_probation': is_prob,
                'prob_desc': prob_desc,
                'join_date': join_dt.strftime('%Y-%m-%d'),
                'is_exempt': bool(ex),
                'exempt_desc': (f"{exempt_period} | {ex['reason']}" if ex else ''),
                'voice': m.voice.channel.name if m.voice and m.voice.channel else None,
                'warn_count': len(warnings.get(u_str, []))
            })
        members.sort(key=lambda x: (x['is_exempt'], -x['week_sec']))
        global_goal = b['VOICE_GLOBAL_GOALS'].get(guild_id)
        base = int(global_goal['target_sec']) if global_goal and global_goal.get('week_start') == b['get_current_week_monday'](now) else b['DEFAULT_WEEKLY_GOAL_SEC']

        ledger_map = b.get('LEDGER_RECORDS', {}).get(guild_id, {})
        ledger_list = sorted(ledger_map.values(), key=lambda r: int(r['id']), reverse=True)

        return {
            'members': members,
            'ledger': ledger_list,
            'week_days': days,
            'week_goal_sec': base,
            'updated_at': now.strftime('%Y-%m-%d %H:%M:%S')
        }

    @app.route('/admin/api/snapshot')
    @require_admin
    def snapshot():
        try:
            force = request.args.get('sync') == '1'
            return jsonify(on_bot(get_snapshot(force_sync=force)))
        except Exception:
            app.logger.exception('snapshot failed')
            return jsonify(error='길드 데이터 조회에 실패했습니다. 잠시 후 새로고침하세요.'), 503

    async def run_action(action, uid, value, actor):
        guild = bot.get_guild(guild_id)
        if guild is None:
            raise ValueError('길드가 연결되어 있지 않습니다.')
        now = datetime.now(b['KST'])

        if action in ('ledger_done', 'ledger_delete'):
            lid = int(value.get('ledger_id', 0))
            rec = b.get('LEDGER_RECORDS', {}).get(guild_id, {}).get(lid)
            if not rec:
                raise ValueError(f'#{lid}번 장부 내역을 찾을 수 없습니다.')
            if action == 'ledger_done':
                await b['log_ledger_action'](guild, 'done', {'id': lid, 'date': now.strftime('%Y-%m-%d %H:%M')})
                return {'ok': True, 'message': f'장부 #{lid}번이 상환 완료 처리되었습니다.'}
            else:
                await b['log_ledger_action'](guild, 'delete', {'id': lid})
                return {'ok': True, 'message': f'장부 #{lid}번 내역이 삭제되었습니다.'}

        member = guild.get_member(uid) if uid else None
        if action != 'notice' and (not member or member.bot or not b['is_target_member'](member)):
            raise ValueError('관리 대상 길드원이 아닙니다.')

        if action == 'warn_add':
            reason = str(value.get('reason', '')).strip()
            if not 1 <= len(reason) <= 250:
                raise ValueError('경고 사유를 1~250자로 입력하세요.')
            warn_entry = {'reason': reason, 'moderator': actor, 'date': now.strftime('%Y-%m-%d %H:%M')}
            await b['log_warning_action'](guild, uid, 'add', warn_entry)

        elif action == 'warn_remove':
            try:
                count = int(value.get('count', 1))
            except (TypeError, ValueError):
                count = 1
            if count < 1:
                raise ValueError('차감할 개수는 1 이상이어야 합니다.')
            u_str = str(uid)
            cur_warns = b.get('WARN_CACHE', {}).get(u_str, [])
            if not cur_warns:
                raise ValueError('현재 차감할 경고가 없습니다.')
            deduct = min(count, len(cur_warns))
            await b['log_warning_action'](guild, uid, 'remove', {'count': deduct})

        elif action == 'warn_clear':
            await b['log_warning_action'](guild, uid, 'clear', {})

        elif action == 'prob_promote':
            prob_map = b.get('PROBATION_ROLE_MAP', {})
            promoted_to = None
            for role in list(member.roles):
                if role.name in prob_map:
                    promoted_to = prob_map[role.name]
                    try:
                        await member.remove_roles(role)
                    except discord.Forbidden:
                        raise ValueError('봇의 역할 순위가 낮아 수습생 역할을 제거할 수 없습니다.')
                    new_role = discord.utils.get(guild.roles, name=promoted_to) or await guild.create_role(name=promoted_to)
                    if new_role:
                        await member.add_roles(new_role)
                    break
            if not promoted_to:
                raise ValueError('해당 멤버는 현재 수습생 역할을 가지고 있지 않습니다.')
            await b['log_probation_action'](guild, uid, 'promoted')
            try:
                await member.send(f"🎉 축하합니다! **{guild.name}** 서버에서 **[{promoted_to}]**(으)로 정식 승급되었습니다!")
            except Exception:
                pass
            return {'ok': True, 'message': f'[{promoted_to}](으)로 정식 승급 완료되었습니다!'}

        elif action == 'prob_extend':
            prob_map = b.get('PROBATION_ROLE_MAP', {})
            if not any(r.name in prob_map for r in member.roles):
                raise ValueError('해당 멤버는 현재 수습생 역할을 가지고 있지 않습니다.')
            try:
                days = int(value.get('days', 7))
            except (TypeError, ValueError):
                days = 7
            if not 1 <= days <= 90:
                raise ValueError('연장 일수는 1~90일 사이여야 합니다.')
            eff_join = b['get_member_effective_join_date'](guild_id, member).strftime('%Y-%m-%d')
            await b['log_probation_action'](guild, uid, 'extend', {'add_days': days, 'join_date': eff_join})
            return {'ok': True, 'message': f'수습 기간이 +{days}일 연장되었습니다.'}

        elif action == 'exempt_add':
            start = str(value.get('start') or now.strftime('%Y-%m-%d')).strip()
            until = str(value.get('until', '')).strip()
            try:
                start_date = datetime.strptime(start, '%Y-%m-%d').date()
                until_date = datetime.strptime(until, '%Y-%m-%d').date()
            except ValueError:
                raise ValueError('시작일과 종료일은 YYYY-MM-DD 형식이어야 합니다.')
            if until_date < start_date or (until_date - start_date).days > 365:
                raise ValueError('종료일은 시작일 이후 365일 이내여야 합니다.')
            reason = str(value.get('reason', '')).strip()
            if not 1 <= len(reason) <= 200:
                raise ValueError('휴가 사유는 1~200자로 입력하세요.')
            await b['log_voice_exempt_action'](guild, uid, 'add', {'start': start, 'until': until, 'reason': reason})

        elif action == 'exempt_remove':
            await b['log_voice_exempt_action'](guild, uid, 'remove', {})

        elif action == 'goal_set':
            try:
                hours = float(value.get('hours'))
            except (TypeError, ValueError):
                raise ValueError('목표 시간을 숫자로 입력하세요.')
            if not 0 <= hours <= 11 or hours != hours:
                raise ValueError('목표는 0~11시간이어야 합니다.')
            reason = str(value.get('reason', '')).strip()
            if len(reason) > 200:
                raise ValueError('사유는 200자 이하여야 합니다.')
            await b['log_voice_goal_action'](guild, uid, 'set', {
                'week_start': b['get_current_week_monday'](now),
                'target_sec': int(hours * 3600),
                'reason': reason
            })

        elif action == 'notice':
            title = str(value.get('title', '')).strip()
            content = str(value.get('content', '')).strip()
            everyone = value.get('everyone', False)
            if not isinstance(everyone, bool):
                raise ValueError('멘션 설정이 올바르지 않습니다.')
            if not 1 <= len(title) <= 100 or not 1 <= len(content) <= 1700:
                raise ValueError('공지 제목 1~100자, 내용 1~1700자로 입력하세요.')
            channel = guild.get_channel(b['ANNOUNCEMENT_CHANNEL_ID'])
            if channel is None or not isinstance(channel, discord.TextChannel):
                raise ValueError('공지 채널을 찾지 못했습니다.')
            msg = f'# 📢 [공지] {discord.utils.escape_mentions(title)}\n\n{discord.utils.escape_mentions(content)}'
            if everyone:
                msg += '\n\n@everyone'
            await channel.send(msg, allowed_mentions=discord.AllowedMentions(everyone=everyone, users=False, roles=False))
        else:
            raise ValueError('지원하지 않는 작업입니다.')
        return {'ok': True, 'message': '정상 처리되었습니다.'}

    @app.route('/admin/api/action', methods=['POST'])
    @require_admin
    def action():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error='올바른 JSON 데이터가 아닙니다.'), 400
        action_type = data.get('action')
        try:
            uid = int(data.get('user_id', 0)) if action_type not in ('notice', 'ledger_done', 'ledger_delete') else 0
            if uid < 0:
                raise ValueError('잘못된 사용자 ID')
            result = on_bot(run_action(action_type, uid, data, session.get('display', '관리자') + ' (' + str(session['uid']) + ')'))
            return jsonify(result)
        except (ValueError, TypeError) as exc:
            return jsonify(error=str(exc)), 400
        except Exception:
            app.logger.exception('admin action failed')
            return jsonify(error='작업에 실패했습니다. Discord 권한과 서버 로그를 확인하세요.'), 503
