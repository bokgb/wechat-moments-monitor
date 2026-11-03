from pathlib import Path
from datetime import datetime
import sys,tempfile,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT/'src'))
from album import AlbumTask,date_range,category
from album_store import AlbumStore
from engine import Config,Engine
from batch_engine import BatchEngine
from ledger import Ledger
from native_client import NativeClient,NativeError,RefreshUnavailable,validate_native_result
from local_reader import ReaderError
from ui_model import JST

def feed(i,created=None,author='friend',liked=False,flag=None):
    return dict(id=str(i),created=i if created is None else created,author=author,own=author=='self',liked=liked,like_flag=flag if flag is not None else '1' if liked else '0')
class Reader:
    account_hash='account';owner='self';contacts={'friend':{'remark':'朋友'}}
    def __init__(self):self.rows={};self.closed=False
    def get(self,i):return self.rows.get(i)
    def label(self,user):return '朋友'
    def excluded_users(self,names):return {'friend'} if '朋友' in names else set()
    def refresh_snapshot(self):pass
    def close(self):self.closed=True
class Native:
    def __init__(self,reader,pages):self.reader=reader;self.pages=pages;self.calls=[];self.sent=[]
    def check(self,*args):pass
    def cancel(self):pass
    def refresh_user(self,user,cursor,alive):
        self.calls.append((user,cursor));feeds,n,next_cursor=self.pages[cursor];self.reader.rows.update({f['id']:f for f in feeds})
        return dict(feed_ids=[f['id'] for f in feeds],refresh_items=n,page_cursor=next_cursor)
    def like(self,*args):self.sent.append(args);raise AssertionError('Scan must never like')
class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ledger=Ledger(Path(self.temp.name)/'state.sqlite3');self.store=AlbumStore(self.ledger);self.reader=Reader();self.events=[]
    def tearDown(self):self.ledger.close();self.temp.cleanup()
    def batch(self,since=0,until=1000,state='ready'):
        key=self.store.create('account','friend','朋友',since,until);self.store.state(key,state);return key
    def task(self,pages,selection=('account','friend',0,1000),key=None,excluded=(),action='scan'):
        self.native=Native(self.reader,pages)
        result=AlbumTask(self.temp.name,self.temp.name,Config('fake',excluded=excluded),self.ledger,self.events.append,action,selection,key,
            reader_factory=lambda *args:self.reader,native_factory=lambda *args:self.native,clock=lambda:2000,delay_picker=lambda a,b:a)
        result.stop.wait=lambda duration:result.stop.is_set()
        return result
    def test_year_is_calendar_year_and_leap_day_valid(self):
        start,end=date_range('最近1年',now=datetime(2024,2,29,18,tzinfo=JST))
        self.assertEqual(datetime.fromtimestamp(start,JST),datetime(2023,2,28,18,tzinfo=JST));self.assertGreater(end,start)
    def test_custom_includes_entire_end_day_but_not_future(self):
        a,b=date_range('自定义日期','2025-01-01','2025-01-02',datetime(2026,1,1,tzinfo=JST))
        self.assertEqual(datetime.fromtimestamp(b,JST).strftime('%Y-%m-%d %H:%M:%S'),'2025-01-02 23:59:59')
    def test_bad_dates_rejected(self):
        for start,end in [('abc','2025-01-01'),('2025-02-30','2025-03-01'),('2025-02-02','2025-01-01')]:
            with self.assertRaises(ValueError):date_range('自定义日期',start,end)
    def test_all_visible_no_cutoff(self):self.assertEqual(date_range('全部可见')[0],0)
    def test_empty_homepage_is_not_reported_as_complete(self):
        t=self.task({'0':([],0,'0')});t.run();b=self.store.get()
        self.assertEqual(b['state'],'paused');self.assertEqual(b['pages'],0);self.assertIn('首页返回空列表',b['reason'])
        self.assertEqual(self.ledger.pending('account'),[])
    def test_legacy_empty_complete_scan_becomes_resumable_on_upgrade(self):
        key=self.batch(state='ready');self.ledger.close();self.ledger=Ledger(Path(self.temp.name)/'state.sqlite3');self.store=AlbumStore(self.ledger)
        b=self.store.get(key);self.assertEqual((b['state'],b['cursor'],b['pages']),('paused','0',0));self.assertIn('继续',b['reason'])
    def test_batch_engine_executes_only_its_own_plan_without_homepage(self):
        key=self.draft([feed(90)]);self.store.enqueue(key,{'90':feed(90)},lambda:1,lambda:0)
        self.ledger.record('account',feed(80),'其他好友','pending',scheduled_at=1)
        self.reader.rows={'90':feed(90),'80':feed(80)};self.reader.refresh_contacts=lambda:None
        n=Native(self.reader,{});n.find_models=lambda ids,alive:{i:0x123 for i in ids}
        n.refresh=lambda *args:(_ for _ in ()).throw(AssertionError('Must not read friends homepage'))
        def like(i,*args):n.sent.append(i);self.reader.rows[i]['liked']=True;self.reader.rows[i]['like_flag']='1';return {'moment_like_enqueued':True}
        n.like=like;monotonic=[0]
        def advance(seconds):monotonic[0]+=seconds
        e=BatchEngine(self.temp.name,Config('fake'),self.ledger,self.events.append,reader_factory=lambda *a:self.reader,native_factory=lambda *a:n,clock=lambda:2000,batch_id=key)
        with patch('engine.time.monotonic',lambda:monotonic[0]),patch('engine.time.sleep',advance):e.run()
        self.assertEqual(n.sent,['90']);self.assertEqual(self.ledger.get('account','80')['status'],'pending')
        self.assertTrue(any(v['kind']=='album_finished' for v in self.events))
    def test_two_pages_and_empty_end_only_make_draft(self):
        t=self.task({'0':([feed(90),feed(80)],2,'80'),'80':([feed(70)],1,'70'),'70':([],0,'0')});t.run()
        b=self.store.get();self.assertEqual(b['state'],'ready');self.assertEqual(b['counts'],{'candidate':3})
        self.assertEqual(self.native.sent,[]);self.assertEqual(self.ledger.pending('account'),[]);self.assertTrue(self.reader.closed)
    def test_pinned_old_post_does_not_stop_recent_page_scan(self):
        t=self.task({'0':([feed(90,900),feed(10,10)],1,'90'),'90':([feed(80,800)],1,'80'),'80':([feed(70,400)],1,'70')},selection=('account','friend',500,1000));t.run()
        self.assertEqual(len(self.native.calls),3);self.assertEqual(self.store.get()['counts'],{'candidate':2,'outside':2})
    def test_all_older_main_page_stops_range(self):
        t=self.task({'0':([feed(40,40),feed(20,20)],2,'20')},selection=('account','friend',50,1000));t.run()
        self.assertEqual(self.store.get()['state'],'ready');self.assertIn('日期',self.store.get()['reason'])
    def test_wrong_account_does_not_scan(self):
        t=self.task({},selection=('other','friend',0,1000));t.run();self.assertFalse(self.native.calls);self.assertIsNone(self.store.get())
    def test_excluded_friend_does_not_scan(self):
        t=self.task({},excluded=('朋友',));t.run();self.assertFalse(self.native.calls);self.assertEqual(self.store.get()['state'],'paused')
    def test_wrong_author_page_not_committed(self):
        t=self.task({'0':([feed(90,author='someone')],1,'90')});t.run();b=self.store.get();self.assertEqual(b['state'],'paused');self.assertEqual(b['pages'],0)
    def test_missing_local_sync_does_not_advance(self):
        t=self.task({'0':([feed(90)],1,'90')});self.reader.get=lambda i:None;t.run();self.assertEqual(self.store.get()['cursor'],'0');self.assertEqual(self.store.get()['pages'],0)
    def test_nonadvancing_cursor_preserves_last_good_page(self):
        t=self.task({'0':([feed(90)],1,'90'),'90':([feed(90)],1,'90')});t.run();b=self.store.get();self.assertEqual((b['state'],b['pages'],b['cursor']),('paused',1,'90'))
    def test_restart_resumes_committed_cursor(self):
        key=self.batch(state='scanning');self.store.page(key,[feed(90)],['candidate'],'90',False);self.store.state(key,'paused')
        t=self.task({'90':([],0,'0')},key=key);t.run();self.assertEqual(self.native.calls,[('friend','90')]);self.assertEqual(self.store.get(key)['counts']['candidate'],1)
    def test_pause_before_request_never_fetches(self):
        t=self.task({});t.cancel();t.run();self.assertFalse(self.native.calls);self.assertEqual(self.store.get()['state'],'paused')
    def test_dedup_overlapping_pages(self):
        t=self.task({'0':([feed(90),feed(80)],2,'80'),'80':([feed(70),feed(90)],1,'70'),'70':([],0,'0')});t.run()
        # Duplicate pinned post is harmless, but not a second candidate.
        self.assertEqual(self.store.get()['counts'],{'candidate':3})
    def test_category_unknown_and_existing_plans_skipped(self):
        key=self.batch();b=self.store.get(key)
        self.ledger.record('account',feed(1),'朋友','pending',scheduled_at=50)
        self.ledger.record('account',feed(2),'朋友','uncertain')
        self.assertEqual(category(feed(1),b,self.ledger),'planned');self.assertEqual(category(feed(2),b,self.ledger),'attention')
        self.assertEqual(category(feed(3,flag='2'),b,self.ledger),'attention');self.assertEqual(category(feed(4,liked=True),b,self.ledger),'already')
    def draft(self,feeds):
        key=self.batch(state='scanning');self.store.page(key,feeds,['candidate']*len(feeds),'0',True);return key
    def test_enqueue_random_plan_once_and_restart_preserves(self):
        key=self.draft([feed(90)]);n=self.store.enqueue(key,{'90':feed(90)},lambda:2000,lambda:60)
        self.assertEqual(n,1);self.assertEqual(self.ledger.get('account','90')['scheduled_at'],2060)
        self.assertEqual(self.ledger.pending('account')[0]['source_user'],'friend')
        with self.assertRaises(ValueError):self.store.enqueue(key,{'90':feed(90)},lambda:3000,lambda:60)
        self.assertEqual(self.ledger.get('account','90')['scheduled_at'],2060)
        self.assertIsNone(self.ledger.account('account'))
    def test_enqueue_keeps_existing_plan_and_uncertain(self):
        key=self.draft([feed(90),feed(80)])
        self.ledger.record('account',feed(90),'朋友','pending',scheduled_at=12);self.ledger.record('account',feed(80),'朋友','uncertain')
        self.assertEqual(self.store.enqueue(key,{'90':feed(90),'80':feed(80)},lambda:2000,lambda:60),1)
        self.assertEqual(self.ledger.get('account','90')['scheduled_at'],12);self.assertEqual(self.ledger.get('account','80')['status'],'uncertain')
    def test_baseline_can_be_explicitly_enqueued_without_other_posts(self):
        self.ledger.baseline('account',['90','80'],100);key=self.draft([feed(90)])
        self.store.enqueue(key,{'90':feed(90)},lambda:2000,lambda:60)
        self.assertEqual(self.ledger.get('account','90')['status'],'pending');self.assertEqual(self.ledger.get('account','80')['status'],'baseline')
    def test_enqueue_author_mismatch_rolls_back_whole_batch(self):
        key=self.draft([feed(90),feed(80)])
        with self.assertRaises(ValueError):self.store.enqueue(key,{'90':feed(90),'80':feed(80,author='other')},lambda:2000,lambda:60)
        self.assertEqual(self.ledger.pending('account'),[]);self.assertEqual(self.store.get(key)['state'],'ready')
    def test_enqueue_requires_complete_scan(self):
        key=self.batch(state='paused')
        with self.assertRaises(ValueError):self.store.enqueue(key,{},lambda:2000,lambda:60)
    def test_enqueue_second_batch_same_post_is_idempotent(self):
        key=self.draft([feed(90)]);self.store.enqueue(key,{'90':feed(90)},lambda:2000,lambda:60)
        other=self.draft([feed(90)]);self.assertEqual(self.store.enqueue(other,{'90':feed(90)},lambda:3000,lambda:120),1)
        self.assertEqual(len(self.ledger.pending('account')),1)
        self.assertEqual(len(self.store.plans(key)),1);self.assertEqual(len(self.store.plans(other)),1)
    def test_unchanged_homepage_falls_back_without_losing_pinned_items(self):
        client=NativeClient.__new__(NativeClient)
        def result(ids,n,cursor):return dict(refresh_completed=True,cache_synced=True,feed_ids=ids,feed_models={i:'0x10000' for i in ids},refresh_items=n,page_cursor=cursor)
        with patch.object(client,'rpc',side_effect=[result(['9'],0,'0'),result(['20','19'],2,'19')]) as rpc:
            r=client.refresh_user('friend')
            self.assertEqual(r['feed_ids'],['20','19','9']);self.assertEqual(r['refresh_items'],2)
            self.assertEqual(rpc.call_args_list[1].args[0]['max_id'],str((1<<64)-1))
    def test_nonempty_homepage_needs_no_extra_request(self):
        client=NativeClient.__new__(NativeClient)
        r=dict(refresh_completed=True,cache_synced=True,feed_ids=['20'],feed_models={'20':'0x10000'},refresh_items=1,page_cursor='20')
        with patch.object(client,'rpc',return_value=r) as rpc:client.refresh_user('friend');self.assertEqual(rpc.call_count,1)
    def test_native_user_validation_never_calls_rpc_for_bad_identity(self):
        client=NativeClient.__new__(NativeClient)
        with patch.object(client,'rpc') as rpc:
            for name in ('x@chatroom','',None,'friend\x00extra'):
                with self.assertRaises(NativeError):client.refresh_user(name)
            rpc.assert_not_called()
    def test_userpage_transport_error_is_recoverable_without_send(self):
        result=dict(code=70,moment_like_enqueued=False,stage=13,exception_code=0,refresh_completed=False)
        with self.assertRaises(RefreshUnavailable):validate_native_result({'mode':'user_page'},result)
    def test_due_old_personal_task_uses_user_album_recovery(self):
        key=self.draft([feed(90)]);self.store.enqueue(key,{'90':feed(90)},lambda:1,lambda:0)
        native=self.task({'91':([feed(90,liked=True)],1,'90')}).native_factory()
        # Generic monitor recovery must keep the exact source friend, rather
        # than searching the friends' homepage for a year-old post.
        native.find_models=lambda ids,alive:{}
        e=Engine(self.temp.name,Config('fake',backfill_hours=0),self.ledger,reader_factory=lambda *a:self.reader,native_factory=lambda *a:native,clock=lambda:2000)
        e.reader=self.reader;e.account='account';e.native=native
        e.process_pending();self.assertEqual(native.calls,[('friend','91')]);self.assertEqual(self.ledger.get('account','90')['status'],'already');self.assertFalse(native.sent)
if __name__=='__main__':unittest.main(verbosity=2)
