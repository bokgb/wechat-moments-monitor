# SPDX-License-Identifier: Apache-2.0
"""Pure presentation helpers; do not infer network health from counters."""
from datetime import datetime,timezone,timedelta
JST=timezone(timedelta(hours=9))
GROUPS={'全部记录':'all','等待点赞':'waiting','补赞重试':'retry','需要处理':'attention','今日已点赞':'liked','已点赞':'liked','已跳过':'skipped'}
PHASES={
    'stopped':('已停止','#607168'),'connecting':('正在连接','#276088'),
    'checking':('正在检查','#276088'),'backfill':('正在补查','#276088'),
    'watching':('正在监控','#197653'),'recovering':('正在补取旧动态','#996018'),
    'sync_wait':('等待同步','#996018'),'refresh_wait':('刷新失败，等待重试','#996018'),'liking':('正在点赞','#197653'),
    'pausing':('正在暂停','#996018'),'error':('已暂停','#b13c35')}

def timestamp(value,now=None,full=False):
    if value is None or value<=0:return '未记录'
    date=datetime.fromtimestamp(value,JST)
    if full:return date.strftime('%Y-%m-%d %H:%M:%S')
    current=datetime.fromtimestamp(now,JST) if now is not None else datetime.now(JST)
    return ('今天 ' if date.date()==current.date() else date.strftime('%m-%d '))+date.strftime('%H:%M:%S')

def task_state(row,now,running=False,monitor_only=False):
    status=row['status']
    if status=='pending':
        if row.get('pending_note'):return row['pending_note']
        if row.get('scheduled_at') is None:return '等待安排'
        if row['scheduled_at']<=now:return '到期，等待处理' if running and not monitor_only else '已到期，等待开启自动点赞'
        return '等待计划时间'
    return {'liked':'已点赞','submitted':'正在核对结果','uncertain':'结果待核对，不会自动重发',
        'unreadable':'点赞状态不明确，已跳过','already':'原有点赞，已跳过','observed':'仅监控记录',
        'excluded':'在不点赞名单中','not_friend':'本人或非好友，已跳过','baseline':'首次基线'}.get(status,status)

def task_next(row,now,running=False,monitor_only=False):
    if row['status']=='pending':
        when=row.get('retry_at') if row.get('pending_note') else row.get('scheduled_at')
        if when is None:return '开始后安排'
        if not running or monitor_only:return '等待开启自动点赞'
        seconds=int(when-now)
        if seconds<=0:return '下一轮重试' if row.get('pending_note') else '下一轮处理'
        minutes=max(1,(seconds+59)//60)
        return f'{minutes//60}小时{minutes%60}分后' if minutes>=60 else f'{minutes}分钟后'
    if row['status']=='submitted':return '确认中，不重复提交'
    if row['status']=='uncertain':return '请到微信核对原动态'
    if row['status']=='unreadable':return '请到微信查看点赞状态'
    return timestamp(row.get('confirmed_at'),now) if row['status']=='liked' else '—'
