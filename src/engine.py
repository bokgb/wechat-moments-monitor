# SPDX-License-Identifier: Apache-2.0
from dataclasses import dataclass
from datetime import datetime,timezone,timedelta
import hashlib,time,threading,random
from native_client import NativeClient,NativeError,RefreshUnavailable,Cancelled
from local_reader import LocalReader,ReaderError
JST=timezone(timedelta(hours=9))

@dataclass(frozen=True)
class Config:
    account:str=''
    interval:int=60
    auto_like:bool=True
    excluded:tuple=()
    delay_min_minutes:int=1
    delay_max_minutes:int=120
    backfill_hours:int=24
    def validate(self):
        if not 30<=self.interval<=3600:raise ValueError('检查间隔请填写 30～3600 秒')
        if len(self.excluded)>200:raise ValueError('不点赞名单最多填写200行')
        if not isinstance(self.delay_min_minutes,int) or not isinstance(self.delay_max_minutes,int):raise ValueError('等待时间请填写整数分钟')
        if not 1<=self.delay_min_minutes<=1440 or not 1<=self.delay_max_minutes<=1440:raise ValueError('等待时间请填写 1～1440 分钟')
        if self.delay_min_minutes>self.delay_max_minutes:raise ValueError('最短等待不能大于最长等待')
        if type(self.backfill_hours) is not int or not 0<=self.backfill_hours<=168:raise ValueError('启动补查请填写 1～168 小时；关闭时为 0')

class Engine:
    def __init__(self,home,config,ledger,emit=lambda value:None,reader_factory=LocalReader,native_factory=NativeClient,clock=time.time,delay_picker=None,reader_home=None):
        config.validate();self.home=home;self.config=config;self.ledger=ledger;self.emit=emit
        self.reader_factory=reader_factory;self.native_factory=native_factory;self.clock=clock
        self.reader_home=reader_home or home
        self.delay_picker=delay_picker or random.SystemRandom().randint
        self.stop=threading.Event();self.reader=None;self.native=None;self.account=None;self.cycles=0
        self.discovered=0;self.liked=0;self.initialized=False;self.waiting_models=set()
        self.sync_failures=0
        self.backfill_active=False;self.backfill_cutoff=None;self.backfill_cursor='0'
        self.backfill_time_covered=False;self.backfill_highest=0;self.backfill_count=0
        self.fetch_complete=True;self.fetch_next_cursor='0';self.fetch_time_covered=False
        self.normal_cursor='0';self.normal_highest=0
    def log(self,message):self.emit({'kind':'log','text':message})
    def phase(self,phase,text):self.emit({'kind':'phase','phase':phase,'text':text,'account':self.account})
    def alive(self):return not self.stop.is_set()
    def random_delay(self):return self.delay_picker(self.config.delay_min_minutes*60,self.config.delay_max_minutes*60)
    def cancel(self):
        self.stop.set()
        if self.native:self.native.cancel()
    def stats(self):
        counts=self.ledger.counts(self.account) if self.account else {}
        self.emit({'kind':'stats','account':self.account,'discovered':self.discovered,'liked':self.liked,
            'pending':counts.get('pending',0),'uncertain':counts.get('submitted',0)+counts.get('uncertain',0),
            'total_liked':counts.get('liked',0),'checked':self.clock()})
    def prepare(self):
        if not self.alive():raise Cancelled()
        self.native=self.native_factory(self.home) if self.reader_home==self.home else self.native_factory(self.home,self.reader_home)
        self.native.check(self.alive)
        self.reader=self.reader_factory(self.reader_home,self.config.account or None);self.account=self.reader.account_hash
        self.phase('checking','微信已连接，正在读取计划和处理记录。')
        self.log(f'微信已连接；监控范围：所有好友（{len(self.reader.contacts)}人），'+('自动点赞新动态' if self.config.auto_like else '仅监控，不点赞'))
        meta=self.ledger.account(self.account)
        if self.config.backfill_hours:
            self.backfill_active=True;self.backfill_cutoff=self.clock()-self.config.backfill_hours*3600
            cutoff=datetime.fromtimestamp(self.backfill_cutoff,JST).strftime('%Y-%m-%d %H:%M:%S')
            self.log(f'启动补查最近 {self.config.backfill_hours} 小时（{cutoff} 起）；已处理记录跳过，漏赞按原随机等待范围安排。')
        if meta is None:
            self.native.refresh(alive=self.alive)
            if not self.alive():raise Cancelled()
            self.ledger.baseline(self.account,self.reader.all_ids(),self.clock())
            self.log('首次基线已建立：接下来补查设定时间内的动态。' if self.backfill_active else '首次基线已建立：现有朋友圈已忽略，从现在起处理新发布的动态。')
            self.initialized=True
            if not self.backfill_active:
                self.stats();self.phase('watching','首次基线已建立，等待下一轮检查新动态。');return True
        self.initialized=True
        for row in self.ledger.unresolved(self.account):
            feed=self.reader.get(row['id'])
            if feed and feed['liked']:
                self.ledger.transition(self.account,row['id'],'liked',when=self.clock());self.log(f"{row['label']}：上次点赞已回读确认。")
            else:self.ledger.transition(self.account,row['id'],'uncertain')
        if self.ledger.unresolved(self.account):self.log('保留上次未确认任务；这些动态不会自动重复点赞，请自行核对。')
        if self.config.auto_like:
            for row in self.ledger.unscheduled(self.account):
                planned=self.clock()+self.random_delay()
                if self.ledger.schedule(self.account,row['id'],planned):self.log(f"{row['label']}：已安排随机延迟，计划 {datetime.fromtimestamp(planned,JST).strftime('%Y-%m-%d %H:%M:%S')} 点赞。")
            self.log(f'新动态在首次检测后随机等待 {self.config.delay_min_minutes}～{self.config.delay_max_minutes} 分钟；已有计划保持不变，重启后继续。')
        self.log('已恢复去重记录，继续后台检查。')
        return False
    def fetch(self):
        previous=int(self.ledger.account(self.account)['watermark']);cursor=self.normal_cursor;received=[]
        pending=[row for row in self.ledger.pending(self.account,25,due_before=self.clock()) if not row.get('source_user')]
        if self.backfill_active:
            # A saved watermark must not stop an explicit time-range scan.
            previous=min((int(row['id']) for row in pending),default=(1<<64)-1)
            cursor=self.backfill_cursor
        elif pending:previous=min(previous,min(int(row['id']) for row in pending))
        self.fetch_complete=False;self.fetch_next_cursor=cursor
        covered=self.backfill_time_covered
        for page in range(12):
            if not self.alive():raise Cancelled()
            result=self.native.refresh(cursor,self.alive)
            ids=[str(int(v)) for v in result.get('feed_ids',[]) if str(v).isdigit()]
            received.extend(ids)
            if self.backfill_active and ids:
                page_feeds=[self.reader.get(value) for value in ids]
                if all(feed is not None for feed in page_feeds):
                    covered=covered or any(0<feed['created']<=self.backfill_cutoff for feed in page_feeds)
            self.fetch_time_covered=covered
            if not ids or (any(int(v)<=previous for v in ids) and (not self.backfill_active or covered)):
                self.fetch_complete=True;return received
            next_id=str(min(map(int,ids)))
            if cursor!='0' and int(next_id)>=int(cursor):raise ReaderError('朋友圈分页未前进，已暂停')
            cursor=next_id
        # Commit this cursor only after all returned IDs have been verified.
        self.fetch_next_cursor=cursor;return received
    def ingest(self,feeds):
        meta=self.ledger.account(self.account);excluded=self.reader.excluded_users(self.config.excluded)
        for feed in feeds:
            existing=self.ledger.get(self.account,feed['id'])
            history=self.backfill_active and feed['created']>=self.backfill_cutoff
            reconsider=bool(existing and history and existing['status'] in ('baseline','observed','excluded','not_friend','unreadable'))
            if existing and not reconsider:continue
            if reconsider and existing['author'] and existing['author']!=hashlib.sha256(feed['author'].encode()).hexdigest():raise ReaderError('补查帖子作者核对失败，已暂停')
            label=self.reader.label(feed['author'])
            if (self.backfill_cutoff is not None and feed['created']<self.backfill_cutoff) or (feed['created']<=meta['started'] and not history):
                status='baseline'
            elif feed['own'] or feed['author'] not in self.reader.contacts:status='not_friend'
            elif feed['author'] in excluded:status='excluded'
            elif feed['liked'] or feed['like_flag']=='1':status='already'
            elif feed['like_flag']!='0':status='unreadable'
            else:status='pending' if self.config.auto_like else 'observed'
            if reconsider and existing['status']==status:continue
            detected=existing['detected_at'] if existing and existing['detected_at'] is not None else self.clock()
            planned=self.clock()+self.random_delay() if status=='pending' else None
            self.ledger.record(self.account,feed,label,status,detected_at=detected,scheduled_at=planned,reconsider=reconsider)
            if status not in ('baseline','not_friend'):
                self.discovered+=1
                suffix={'pending':'新朋友圈，等待计划时间','observed':'新朋友圈（仅监控）','already':'已点过赞，跳过',
                        'excluded':'在不点赞名单中，跳过','unreadable':'点赞状态不明确，跳过'}[status]
                if history:suffix='启动补查：'+suffix
                delay=max(0,int(detected-feed['created']))
                timing=f"发布于 {datetime.fromtimestamp(feed['created'],JST).strftime('%Y-%m-%d %H:%M:%S')}，检测于 {datetime.fromtimestamp(detected,JST).strftime('%Y-%m-%d %H:%M:%S')}，发现延迟 {delay//60}分{delay%60}秒"
                if planned is not None:timing+=f"，计划点赞 {datetime.fromtimestamp(planned,JST).strftime('%Y-%m-%d %H:%M:%S')}"
                self.log(f'{label}：{suffix}（{timing}）。')
    def pending_rows(self):return self.ledger.pending(self.account,25,due_before=self.clock())
    def process_pending(self):
        if not self.config.auto_like:return
        pending=self.pending_rows()
        if not pending:return
        models=self.native.find_models([row['id'] for row in pending],self.alive)
        missing={row['id'] for row in pending}-models.keys()
        # A saved plan may outlive the feed window returned from the homepage.
        # Seek immediately before its ID instead of paging from the homepage
        # forever. Bound extra requests; other due tasks must still get a turn.
        recovered=set();attempted=set()
        for row in pending:
            if row['id'] not in missing or row['id'] in recovered:continue
            if len(attempted)>=3:break
            if not self.alive():raise Cancelled()
            attempted.add(row['id'])
            self.phase('recovering',f"正在补取 {row['label']} 的旧动态，其他计划会继续处理。")
            if int(row['id'])<(1<<64)-1:
                try:
                    result=(self.native.refresh_user(row['source_user'],str(int(row['id'])+1),self.alive)
                        if row.get('source_user') else self.native.refresh(str(int(row['id'])+1),self.alive))
                except RefreshUnavailable as exc:
                    retry=self.clock()+1800
                    self.ledger.defer_pending(self.account,row['id'],retry,'旧动态补取请求失败')
                    self.log(f"{row['label']}：{exc}；保留原计划，{datetime.fromtimestamp(retry,JST).strftime('%Y-%m-%d %H:%M:%S')} 重试，不中断正常监控。")
                    # Don't issue further optional requests in the same cycle.
                    break
                returned={str(value) for value in result.get('feed_ids',[])}
                # Only use a model returned by this verified native refresh.
                found=self.native.find_models(list(missing & returned),self.alive)
                models.update(found);recovered.update(found)
            if row['id'] not in models:
                retry=self.clock()+1800
                self.ledger.defer_pending(self.account,row['id'],retry,'暂未取到原动态')
                self.log(f"{row['label']}：到期计划补取暂未返回原动态，保留原计划，{datetime.fromtimestamp(retry,JST).strftime('%Y-%m-%d %H:%M:%S')} 自动重试；不能据此判断已删除或不可见。")
        if recovered:self.log(f'已重新取得 {len(recovered)} 条到期计划的帖子对象，继续核对后补赞。')
        missing={row['id'] for row in pending}-models.keys()
        if missing-self.waiting_models:self.log(f'{len(missing)} 条到期计划等待后台补取；暂未取到的每 30 分钟重试，其他计划继续处理。')
        self.waiting_models=missing
        excluded=self.reader.excluded_users(self.config.excluded)
        for row in pending:
            if not self.alive():raise Cancelled()
            feed=self.reader.get(row['id'])
            if not feed:
                if row['id'] in models:
                    self.ledger.defer_pending(self.account,row['id'],self.clock()+1800,'原动态尚未在本机核对到')
                    self.log(f"{row['label']}：原动态尚未在本机核对到，保留原计划，30 分钟后重试；本次不点赞。")
                continue
            if hashlib.sha256(feed['author'].encode()).hexdigest()!=row['author']:raise ReaderError('帖子作者发生变化，已暂停')
            if feed['own'] or feed['author'] not in self.reader.contacts or feed['author'] in excluded:
                self.ledger.transition(self.account,row['id'],'excluded');continue
            if feed['liked'] or feed['like_flag']=='1':
                self.ledger.transition(self.account,row['id'],'already');self.log(f"{row['label']}：已点过赞，跳过。");continue
            if feed['like_flag']!='0':self.ledger.transition(self.account,row['id'],'unreadable');continue
            if row['id'] not in models:continue
            if not self.ledger.claim_due(self.account,row['id'],self.clock()):continue
            self.phase('liking',f"正在为 {row['label']} 点赞并核对结果。")
            try:
                result=self.native.like(row['id'],models[row['id']],self.alive)
                if not result.get('moment_like_enqueued'):raise NativeError('点赞提交尚未确认，已暂停',result)
            except Cancelled:
                self.ledger.transition(self.account,row['id'],'pending');raise
            except NativeError as exc:
                known_not_sent=exc.result.get('code') in (41,42,43,44,51) and not exc.result.get('moment_like_enqueued')
                self.ledger.transition(self.account,row['id'],'pending' if known_not_sent else 'uncertain')
                if known_not_sent:
                    self.log(f"{row['label']}：暂未取得可用帖子，保留等待。");continue
                raise
            deadline=time.monotonic()+25;stable=None
            while time.monotonic()<deadline:
                now=self.reader.get(row['id'])
                if now and now['liked'] and now['like_flag']=='1':
                    if stable is None:stable=time.monotonic()
                    if time.monotonic()-stable>=4:break
                else:stable=None
                time.sleep(.5)
            else:
                self.ledger.transition(self.account,row['id'],'uncertain')
                raise NativeError(f"{row['label']}：点赞结果暂未确认，已暂停；不会自动重发")
            self.ledger.transition(self.account,row['id'],'liked',when=self.clock());self.liked+=1
            saved=self.ledger.get(self.account,row['id'])
            self.emit({'kind':'liked','account':self.account,'id':row['id'],'label':row['label'],'confirmed_at':saved['confirmed_at']})
            fmt=lambda value:datetime.fromtimestamp(value,JST).strftime('%Y-%m-%d %H:%M:%S') if value is not None else '未记录'
            self.log(f"{row['label']}：已点赞，本机回读已确认（发布 {fmt(saved['created'])}；检测 {fmt(saved['detected_at'])}；计划 {fmt(saved['scheduled_at'])}；确认 {fmt(saved['confirmed_at'])}）。");self.stats()
    def verified_feeds(self,ids):
        feeds=self.reader.feeds(1000)
        local={feed['id']:feed for feed in feeds}
        # A successful network request is not proof that the reader can see the
        # response. Never advance the watermark over unseen native results.
        missing=[]
        for feed_id in dict.fromkeys(ids):
            if feed_id in local:continue
            feed=self.reader.get(feed_id)
            if feed:local[feed_id]=feed;feeds.append(feed)
            else:missing.append(feed_id)
        return feeds,missing
    def step(self):
        if not self.initialized and self.prepare():return
        if not self.alive():raise Cancelled()
        started=time.monotonic();self.cycles+=1;before=self.discovered
        self.phase('backfill' if self.backfill_active else 'checking',
            f'正在补查最近 {self.config.backfill_hours} 小时的动态，已核对 {self.backfill_count} 条。' if self.backfill_active else '正在后台检查新动态。')
        if self.cycles%5==0:self.reader.refresh_contacts()
        ids=self.fetch()
        if not self.alive():raise Cancelled()
        feeds,missing=self.verified_feeds(ids)
        if missing:
            self.log(f'有 {len(missing)} 条后台记录暂未核对到，正在重建本程序读取缓存并等待同步；不额外刷新、不点赞。')
            for delay in (0,1,3):
                if self.stop.wait(delay):raise Cancelled()
                if hasattr(self.reader,'refresh_snapshot'):self.reader.refresh_snapshot()
                feeds,missing=self.verified_feeds(ids)
                if not missing:break
        if missing:
            self.sync_failures+=1
            fingerprint=hashlib.sha256(','.join(sorted(missing)).encode()).hexdigest()[:12]
            detail=f'{len(missing)}/{len(set(ids))} 条，核对编号 {fingerprint}'
            if self.sync_failures>=3:
                raise ReaderError(f'连续 3 轮仍有朋友圈无法与本机记录核对（{detail}），已暂停；未推进进度，计划保留，请保留日志')
            self.log(f'本轮同步待核对（{detail}；连续 {self.sync_failures}/3 轮）；保留进度和点赞计划，下一检查周期重试。')
            self.phase('sync_wait','本轮动态尚未核对完成，保留原计划，下一轮自动重试。')
            self.stats();return
        if self.sync_failures:self.log('本机记录已重新核对通过，继续原有进度与点赞计划。')
        self.sync_failures=0
        self.ingest(feeds)
        if self.backfill_active:
            self.backfill_highest=max([self.backfill_highest]+[int(value) for value in ids])
            self.backfill_count+=len(set(ids))
            if self.fetch_complete:
                self.ledger.watermark(self.account,[str(self.backfill_highest)])
                self.backfill_active=False
                self.log(f'启动补查完成：已核对 {self.backfill_count} 条返回记录；漏赞已安排，已有计划不变。')
            else:
                self.backfill_cursor=self.fetch_next_cursor;self.backfill_time_covered=self.fetch_time_covered
                self.log(f'启动补查已核对 {self.backfill_count} 条返回记录，下轮继续向前补查；尚未推进检查进度。')
        else:
            self.normal_highest=max([self.normal_highest]+[int(value) for value in ids]+[int(feed['id']) for feed in feeds])
            if self.fetch_complete:
                self.ledger.watermark(self.account,[str(self.normal_highest)])
                self.normal_cursor='0';self.normal_highest=0
            else:
                self.normal_cursor=self.fetch_next_cursor
                self.log('本轮记录较多，已核对部分会保留；下一轮继续分页，尚未推进检查进度。')
        self.log(f'第 {self.cycles} 轮后台检查完成：返回 {len(set(ids))} 条，已核对同步，新增 {self.discovered-before} 条，用时 {time.monotonic()-started:.1f} 秒。')
        self.emit({'kind':'checked','at':self.clock(),'account':self.account})
        self.process_pending();self.stats()
        self.phase('backfill' if self.backfill_active else 'watching',
            f'补查已核对 {self.backfill_count} 条，下一轮继续向前查找。' if self.backfill_active else
            '仅监控' if not self.config.auto_like else '自动点赞已开启')
    def run(self):
        refresh_failures=0
        try:
            while self.alive():
                started=time.monotonic()
                try:self.step()
                except RefreshUnavailable as exc:
                    refresh_failures+=1
                    if not self.initialized:self.close()
                    if refresh_failures>=3:raise ReaderError(f'连续 3 次朋友圈刷新请求失败，已暂停；{exc}。请检查微信登录和网络后再开始，原计划保留。') from exc
                    wait=max(self.config.interval,60)*refresh_failures
                    self.log(f'{exc}；连续 {refresh_failures}/3 次，{wait} 秒后重试，未跳过检查进度。')
                    self.phase('refresh_wait',f'刷新请求失败，{wait} 秒后自动重试（{refresh_failures}/3）；原计划保留。')
                    self.stats()
                    if self.stop.wait(wait):break
                    continue
                if refresh_failures:self.log('朋友圈刷新已恢复，继续原有计划与检查进度。')
                refresh_failures=0
                wait=max(0,self.config.interval-(time.monotonic()-started))
                if self.config.auto_like and self.account:
                    now=self.clock();planned=self.ledger.next_due(self.account,now)
                    if planned is not None:wait=min(wait,max(0,planned-now))
                if self.stop.wait(wait):break
        except Cancelled:pass
        except Exception as exc:
            self.stop.set()
            message=str(exc) if isinstance(exc,(NativeError,ReaderError,ValueError)) else type(exc).__name__
            self.log('已暂停：'+message);self.emit({'kind':'error','text':message})
        finally:
            self.close();self.emit({'kind':'stopped'})
    def close(self):
        if self.native:self.native.cancel()
        if self.reader:self.reader.close();self.reader=None
