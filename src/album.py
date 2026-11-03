# SPDX-License-Identifier: Apache-2.0
"""Read a single friend's native album, then explicitly enqueue a frozen draft."""
from datetime import datetime,timedelta
import time,threading,random
from local_reader import LocalReader,ReaderError
from native_client import NativeClient,NativeError,Cancelled
from album_store import AlbumStore
from ui_model import JST

PERIODS=('最近1年','最近30天','最近90天','全部可见','自定义日期')
def date_range(period,start='',end='',now=None):
    now=now or datetime.now(JST)
    if period=='自定义日期':
        try:
            since=datetime.strptime(start,'%Y-%m-%d').replace(tzinfo=JST)
            until=datetime.strptime(end,'%Y-%m-%d').replace(tzinfo=JST)+timedelta(days=1)
        except ValueError:raise ValueError('日期请填写 YYYY-MM-DD，例如 2025-11-01') from None
        if since>=until or since>now:raise ValueError('开始日期不能晚于结束日期或今天')
        return since.timestamp(),min(now.timestamp(),until.timestamp()-0.001)
    if period=='全部可见':return 0,now.timestamp()
    if period=='最近1年':
        try:since=now.replace(year=now.year-1)
        except ValueError:since=now.replace(year=now.year-1,day=28)
    elif period in ('最近30天','最近90天'):since=now-timedelta(days=30 if period=='最近30天' else 90)
    else:raise ValueError('请选择时间范围')
    return since.timestamp(),now.timestamp()

def category(feed,batch,ledger):
    if feed['author']!=batch['username'] or feed['own']:raise ReaderError('个人相册返回了其他作者，已停止扫描')
    if not batch['since']<=feed['created']<=batch['until']:return 'outside'
    if feed['liked'] or feed['like_flag']=='1':return 'already'
    old=ledger.get(batch['account'],feed['id'])
    if old and old['status'] in ('liked','already'):return 'already'
    if old and old['status']=='pending':return 'planned'
    if feed['like_flag']!='0' or (old and old['status'] in ('submitted','uncertain')):return 'attention'
    return 'candidate'

class ContactsTask:
    """Independent read-only contact snapshot; no native calls or task ledger."""
    def __init__(self,home,account,emit,reader_factory=LocalReader):
        self.home=home;self.account=account;self.emit=emit;self.reader_factory=reader_factory
        self.stop=threading.Event()
    def cancel(self):self.stop.set()
    def run(self):
        reader=None
        try:
            reader=self.reader_factory(self.home,self.account or None)
            contacts=[{'user':user,'label':reader.label(user),'nick':row.get('nick_name') or ''} for user,row in reader.contacts.items()]
            if not self.stop.is_set():self.emit({'kind':'contacts_loaded','requested_account':self.account,'account':reader.account_hash,'contacts':contacts})
        except Exception as exc:
            if not self.stop.is_set():self.emit({'kind':'contacts_error','text':str(exc) if isinstance(exc,(ValueError,ReaderError)) else type(exc).__name__})
        finally:
            try:
                if reader:reader.close()
            finally:self.emit({'kind':'contacts_finished'})

class AlbumTask:
    def __init__(self,home,data_home,config,ledger,emit,action,selection=None,batch_id=None,reader_factory=LocalReader,native_factory=NativeClient,clock=time.time,delay_picker=None):
        self.home=home;self.data_home=data_home;self.config=config;self.ledger=ledger;self.emit=emit;self.action=action
        self.selection=selection;self.batch_id=batch_id;self.reader_factory=reader_factory;self.native_factory=native_factory
        self.clock=clock;self.delay_picker=delay_picker or random.SystemRandom().randint
        self.stop=threading.Event();self.reader=None;self.native=None;self.store=AlbumStore(ledger)
    def alive(self):return not self.stop.is_set()
    def cancel(self):
        self.stop.set()
        if self.native:self.native.cancel()
    def log(self,text):self.emit({'kind':'log','text':text})
    def progress(self):self.emit({'kind':'album_progress','batch':self.store.get(self.batch_id)})
    def check_batch(self):
        batch=self.store.get(self.batch_id)
        if not batch or batch['account']!=self.reader.account_hash:raise ReaderError('登录账号与扫描账号不一致，请重新读取好友')
        if batch['username'] not in self.reader.contacts:raise ReaderError('此人已不在当前好友名单中，未执行补赞')
        if batch['username'] in self.reader.excluded_users(self.config.excluded):raise ReaderError('此人在“不点赞名单”中，请先在设置里修改')
        return batch
    def scan(self):
        if not self.batch_id:
            account,user,since,until=self.selection
            if account!=self.reader.account_hash or user not in self.reader.contacts:raise ReaderError('好友名单已变化，请重新读取并选择好友')
            self.batch_id=self.store.create(account,user,self.reader.label(user),since,until)
        batch=self.check_batch()
        if batch['state'] not in ('scanning','paused'):raise ReaderError('此扫描已完成，请使用“加入计划”或重新扫描')
        self.native=self.native_factory(self.home,self.data_home);self.native.check(self.alive)
        self.store.state(self.batch_id,'scanning');self.progress()
        cursor=batch['cursor']
        for _ in range(500):
            if not self.alive():raise Cancelled()
            result=self.native.refresh_user(batch['username'],cursor,self.alive)
            ids=list(dict.fromkeys(result['feed_ids']));n=result.get('refresh_items')
            if type(n) is not int or not 0<=n<=len(ids):raise ReaderError('个人相册分页结构核对失败')
            feeds=[]
            for delay in (0,1,3):
                if self.stop.wait(delay):raise Cancelled()
                self.reader.refresh_snapshot();read=getattr(self.reader,'get_preview',self.reader.get);feeds=[read(i) for i in ids]
                if all(feeds):break
            if not all(feeds):raise ReaderError('个人相册记录尚未同步，扫描进度已保留；可稍后继续扫描')
            categories=[category(f,batch,self.ledger) for f in feeds]
            next_cursor=result['page_cursor']
            main=feeds[:n]
            if cursor=='0' and not ids:
                raise ReaderError('个人相册首页返回空列表，无法确认是否读全；未标记完成，也未建立点赞计划。请保留日志。')
            if main and (int(next_cursor)!=min(int(f['id']) for f in main) or (cursor!='0' and int(next_cursor)>=int(cursor))):raise ReaderError('个人相册分页未前进，未跳过任何记录')
            covered=bool(main and batch['since'] and all(0<f['created']<batch['since'] for f in main))
            done=not n or covered
            if not n and next_cursor!='0':raise ReaderError('个人相册分页结束标记异常')
            reason='已读到所选日期以前' if covered else '微信未返回更多可见动态' if not n else ''
            if not self.alive():raise Cancelled()
            self.store.page(self.batch_id,feeds,categories,next_cursor,done,reason);self.progress()
            if done:
                self.log(f"{batch['label']}：个人相册扫描完成，只生成预览，未点赞。{reason}。");return
            cursor=next_cursor
            if self.stop.wait(1):raise Cancelled()
        self.store.state(self.batch_id,'paused','本轮已检查500页；点击继续扫描可接着读取');self.progress()
    def enqueue(self):
        batch=self.check_batch()
        if batch['state']!='ready':raise ReaderError('请先完成扫描；已加入的计划不会重复加入')
        self.reader.refresh_snapshot();feeds={}
        for row in self.store.items(self.batch_id):
            if row['category'] not in ('candidate','planned'):continue
            f=self.reader.get(row['id'])
            if not f:raise ReaderError('部分原动态暂不可核对，请重新扫描；本次未加入任何计划')
            feeds[f['id']]=f
        if not self.alive():raise Cancelled()
        count=self.store.enqueue(self.batch_id,feeds,self.clock,lambda:self.delay_picker(self.config.delay_min_minutes*60,self.config.delay_max_minutes*60))
        self.progress();self.log(f"{batch['label']}：已加入 {count} 条补赞计划，按 {self.config.delay_min_minutes}～{self.config.delay_max_minutes} 分钟随机等待；已有计划与已点赞记录不会重复加入。")
        self.emit({'kind':'album_queued','count':count})
    def run(self):
        try:
            self.reader=self.reader_factory(self.data_home,self.config.account or None)
            if self.action=='contacts':
                contacts=[{'user':user,'label':self.reader.label(user),'nick':row.get('nick_name') or ''} for user,row in self.reader.contacts.items()]
                self.emit({'kind':'album_contacts','account':self.reader.account_hash,'contacts':contacts})
            elif self.action=='scan':self.scan()
            elif self.action=='enqueue':self.enqueue()
            else:raise ValueError('未知操作')
        except Cancelled:
            if self.batch_id:self.store.state(self.batch_id,'paused','扫描已暂停，进度保留');self.progress()
        except Exception as exc:
            text=str(exc) if isinstance(exc,(ValueError,ReaderError,NativeError)) else type(exc).__name__
            if self.batch_id:
                batch=self.store.get(self.batch_id)
                if batch and batch['state'] in ('scanning','paused'):self.store.state(self.batch_id,'paused',text)
                self.progress()
            self.emit({'kind':'error','text':text})
        finally:
            if self.native:self.native.cancel()
            if self.reader:self.reader.close()
            self.emit({'kind':'stopped'})
