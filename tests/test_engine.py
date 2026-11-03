from pathlib import Path
import json,sys,tempfile,unittest,sqlite3
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT/'src'))
from engine import Config,Engine
from ledger import Ledger
from native_client import NativeClient,NativeError,RefreshUnavailable,Cancelled,validate_native_result
from local_reader import LocalReader,ReaderError,parse_feed
from upgrade import restore_previous

def feed(value,author='friend',liked=False,created=None):
    return {'id':str(value),'author':author,'created':created if created is not None else value,
            'liked':liked,'like_flag':'1' if liked else '0','own':author=='self'}
class FakeReader:
    def __init__(self):
        self.owner='self';self.account_hash='account-A';self.contacts={'friend':{'remark':'朋友'},'excluded':{'remark':'不赞'}}
        self.rows={'90':feed(90)}
    def all_ids(self):return list(self.rows)
    def feeds(self,limit=400):return sorted(self.rows.values(),key=lambda f:int(f['id']),reverse=True)[:limit]
    def get(self,value):return self.rows.get(value)
    def label(self,value):return self.contacts.get(value,{}).get('remark','好友')
    def excluded_users(self,names):return {key for key,value in self.contacts.items() if value['remark'] in names}
    def refresh_contacts(self):pass
    def close(self):pass
class FakeNative:
    def __init__(self,reader):self.reader=reader;self.sent=[];self.refreshes=[];self.error=None;self.cancelled=False
    def check(self,alive=lambda:True):return {'code':0}
    def refresh(self,max_id='0',alive=lambda:True):
        self.refreshes.append(max_id)
        ids=sorted((int(value) for value in self.reader.rows if not int(max_id) or int(value)<int(max_id)),reverse=True)[:10]
        return {'refresh_completed':True,'feed_ids':list(map(str,ids))}
    def find_models(self,ids,alive=lambda:True):return {value:0x123 for value in ids}
    def like(self,value,model,alive=lambda:True):
        if self.error:raise self.error
        self.sent.append(value);self.reader.rows[value]['liked']=True;self.reader.rows[value]['like_flag']='1'
        return {'moment_like_enqueued':True}
    def cancel(self):self.cancelled=True
class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ledger=Ledger(Path(self.temp.name)/'state.db')
        self.reader=FakeReader();self.native=FakeNative(self.reader);self.now=0
        self.monotonic=patch('engine.time.monotonic',lambda:self.now);self.sleep=patch('engine.time.sleep',self.advance)
        self.monotonic.start();self.sleep.start()
    def tearDown(self):self.sleep.stop();self.monotonic.stop();self.ledger.close();self.temp.cleanup()
    def advance(self,seconds):self.now+=seconds
    def engine(self,auto=True,excluded=(),minimum=1,maximum=120,backfill=0):
        return Engine(self.temp.name,Config('account',60,auto,excluded,minimum,maximum,backfill),self.ledger,
            reader_factory=lambda *args:self.reader,native_factory=lambda *args:self.native,clock=lambda:100,delay_picker=lambda low,high:0)
    def baseline(self,**options):
        engine=self.engine(**options);engine.step();self.assertEqual(self.native.sent,[]);return engine
    def test_first_start_ignores_existing(self):
        e=self.baseline();e.step();self.assertEqual(self.native.sent,[]);self.assertEqual(self.ledger.get('account-A','90')['status'],'baseline')
    def test_new_post_once_and_restart(self):
        e=self.baseline();self.reader.rows['101']=feed(101);e.step();e.step()
        self.assertEqual(self.native.sent,['101'])
        new=self.engine();new.step();self.assertEqual(self.native.sent,['101'])
    def test_skip_already_liked_excluded_nonfriend_and_own(self):
        e=self.baseline(excluded=('不赞',))
        self.reader.rows.update({str(i):f for i,f in [(101,feed(101,liked=True)),(102,feed(102,'excluded')),(103,feed(103,'stranger')),(104,feed(104,'self'))]})
        e.step();self.assertEqual(self.native.sent,[])
        self.assertEqual([self.ledger.get('account-A',str(i))['status'] for i in (101,102,103,104)],['already','excluded','not_friend','not_friend'])
    def test_late_loaded_old_post_is_ignored(self):
        e=self.baseline();self.reader.rows['99']=feed(99);e.step();self.assertEqual(self.native.sent,[])
    def test_monitor_only_never_likes(self):
        e=self.baseline(auto=False);self.reader.rows['101']=feed(101);e.step();self.assertEqual(self.native.sent,[])
        self.assertEqual(self.ledger.get('account-A','101')['status'],'observed')
    def test_pause_before_submit(self):
        e=self.baseline();self.reader.rows['101']=feed(101);e.cancel()
        with self.assertRaises(Cancelled):e.step()
        self.assertEqual(self.native.sent,[])
    def test_uncertain_result_is_not_retried(self):
        e=self.baseline();self.reader.rows['101']=feed(101);self.native.error=NativeError('unknown',{'code':50})
        with self.assertRaises(NativeError):e.step()
        self.assertEqual(self.ledger.get('account-A','101')['status'],'uncertain')
        self.native.error=None;self.engine().step();self.assertEqual(self.native.sent,[])
    def test_crash_after_submitted_is_not_retried(self):
        e=self.baseline();self.reader.rows['101']=feed(101);e.ingest([self.reader.rows['101']])
        self.ledger.transition('account-A','101','submitted');self.engine().step();self.assertEqual(self.native.sent,[])
    def test_known_unsent_can_wait_without_unknown_resend(self):
        e=self.baseline();self.reader.rows['101']=feed(101);self.native.error=NativeError('stale',{'code':42,'moment_like_enqueued':False})
        e.step();self.assertEqual(self.ledger.get('account-A','101')['status'],'pending')
        self.native.error=None;e.step();self.assertEqual(self.native.sent,['101'])
    def test_more_than_one_page_is_fetched(self):
        e=self.baseline();self.reader.rows.update({str(i):feed(i) for i in range(101,126)})
        e.step();self.assertGreater(len(self.native.refreshes),2);self.assertEqual(len(self.native.sent),25)
    def test_network_success_without_local_sync_does_not_advance_or_like(self):
        e=self.baseline()
        previous=self.ledger.account('account-A')['watermark']
        self.native.refresh=lambda *args,**kwargs:{'refresh_completed':True,'feed_ids':['101','90']}
        e.stop.wait=lambda seconds:self.advance(seconds) or False
        e.step()
        self.assertEqual(self.ledger.account('account-A')['watermark'],previous)
        self.assertEqual(self.native.sent,[])
        self.assertEqual(e.sync_failures,1)
        self.reader.rows['101']=feed(101);e.step()
        self.assertEqual(self.native.sent,['101'])
        self.assertEqual(e.sync_failures,0)
    def test_successful_check_event_is_not_emitted_for_unsynced_cycle(self):
        e=self.baseline();events=[];e.emit=events.append
        self.native.refresh=lambda *args,**kwargs:{'refresh_completed':True,'feed_ids':['101','90']}
        e.stop.wait=lambda seconds:False;e.step()
        self.assertFalse(any(event['kind']=='checked' for event in events))
        self.assertEqual([v['phase'] for v in events if v['kind']=='phase'][-1],'sync_wait')
        self.reader.rows['101']=feed(101);events.clear();e.step()
        self.assertEqual(len([v for v in events if v['kind']=='checked']),1)
    def test_stale_snapshot_is_rebuilt_without_extra_native_refresh(self):
        e=self.baseline();calls=[]
        self.native.refresh=lambda *args,**kwargs:calls.append(1) or {'refresh_completed':True,'feed_ids':['101','90']}
        self.reader.refresh_snapshot=lambda:self.reader.rows.update({'101':feed(101)})
        e.step()
        self.assertEqual(calls,[1]);self.assertEqual(self.native.sent,['101'])
    def test_delayed_sync_retries_and_then_recovers(self):
        e=self.baseline();waits=[]
        self.native.refresh=lambda *args,**kwargs:{'refresh_completed':True,'feed_ids':['101','90']}
        def wait(seconds):
            waits.append(seconds)
            if seconds==3:self.reader.rows['101']=feed(101)
            return False
        e.stop.wait=wait;e.step()
        self.assertEqual(waits,[0,1,3]);self.assertEqual(self.native.sent,['101'])
    def test_persistent_sync_failure_pauses_after_three_cycles_without_progress(self):
        e=self.baseline();previous=self.ledger.account('account-A')['watermark']
        self.native.refresh=lambda *args,**kwargs:{'refresh_completed':True,'feed_ids':['101','90']}
        e.stop.wait=lambda seconds:False
        e.step();e.step()
        with self.assertRaisesRegex(ReaderError,'连续 3 轮'):e.step()
        self.assertEqual(self.ledger.account('account-A')['watermark'],previous)
        self.assertIsNone(self.ledger.get('account-A','101'));self.assertEqual(self.native.sent,[])
    def test_pause_during_sync_retry_never_submits(self):
        e=self.baseline()
        self.native.refresh=lambda *args,**kwargs:{'refresh_completed':True,'feed_ids':['101','90']}
        e.stop.wait=lambda seconds:True
        with self.assertRaises(Cancelled):e.step()
        self.assertEqual(self.native.sent,[])
    def test_snapshot_rebuild_only_removes_our_stamp(self):
        from unittest.mock import Mock
        from contextlib import nullcontext
        reader=LocalReader.__new__(LocalReader);reader.scratch=Path(self.temp.name)
        reader.sns='sns\\sns.db';reader.db=Mock(workdir=self.temp.name)
        reader.db._build_lock_for.return_value=nullcontext()
        stamp=Path(self.temp.name)/'sns__sns.db.stamp';stamp.write_text('old')
        original=Path(self.temp.name)/'sns__sns.db';original.write_text('keep')
        reader.refresh_snapshot()
        self.assertFalse(stamp.exists());self.assertEqual(original.read_text(),'keep')
        reader.db._open_unlocked.assert_called_once_with(reader.sns)
    def test_native_refresh_rejects_component_without_sync_confirmation(self):
        client=NativeClient.__new__(NativeClient)
        client.rpc=lambda *args:{'refresh_completed':True,'feed_ids':['101']}
        with self.assertRaisesRegex(NativeError,'本地同步'):client.refresh()
        client.rpc=lambda *args:{'refresh_completed':True,'cache_synced':True,'feed_ids':['101'],'feed_models':{'101':'0x10000'}}
        self.assertEqual(client.refresh()['feed_ids'],['101'])
        client.ensure_process=lambda:None
        self.assertEqual(client.find_models(['101','102']),{'101':0x10000})
    def test_pending_older_than_latest_page_is_refetched_after_restart(self):
        e=self.baseline()
        self.reader.rows.update({str(i):feed(i) for i in range(101,131)})
        e.ingest(list(self.reader.rows.values()));self.ledger.watermark('account-A',list(self.reader.rows))
        self.native.refreshes=[]
        e.fetch()
        self.assertEqual(self.native.refreshes,['0','121','111'])
        self.assertEqual(self.native.sent,[])
    def refresh_failure(self):
        result={'code':70,'stage':13,'exception_code':0,'moment_like_enqueued':False,
            'refresh_completed':False,'transport_code':-1,'api_code':0}
        try:validate_native_result({'mode':'refresh'},result,70)
        except RefreshUnavailable as exc:return exc
        self.fail('RefreshUnavailable expected')
    def test_completed_read_only_refresh_error_is_retryable_and_keeps_native_codes(self):
        error=self.refresh_failure()
        self.assertIn('传输码 -1',str(error));self.assertIn('服务码 0',str(error))
    def test_send_error_or_unconfirmed_operation_is_never_classified_as_refresh_retry(self):
        original=self.refresh_failure().result
        for mode,change in [('like',{}),('refresh',{'moment_like_enqueued':True}),('refresh',{'exception_code':50}),('refresh',{'stage':12})]:
            with self.subTest(mode=mode,change=change),self.assertRaises(NativeError) as caught:
                validate_native_result({'mode':mode},{**original,**change},70)
            self.assertNotIsInstance(caught.exception,RefreshUnavailable)
    def test_refresh_error_diagnostic_does_not_store_post_ids_models_or_text(self):
        client=NativeClient.__new__(NativeClient);client.requests=Path(self.temp.name)/'requests';client.requests.mkdir()
        client.save_refresh_failure({'mode':'refresh','max_id':'123'},dict(self.refresh_failure().result,feed_ids=['123'],feed_models={'123':'secret'},text='private'))
        data=json.loads((Path(self.temp.name)/'refresh-error.json').read_text())
        self.assertEqual(data['transport_code'],-1);self.assertEqual(data['operation'],'pagination_or_recovery')
        self.assertFalse({'feed_ids','feed_models','text','max_id'} & data.keys())
    def test_main_refresh_failure_retries_twice_then_pauses_without_advancing(self):
        e=self.baseline();events=[];e.emit=events.append;waits=[];watermark=self.ledger.account('account-A')['watermark']
        def fail():raise self.refresh_failure()
        e.step=fail;e.stop.wait=lambda seconds:waits.append(seconds) or False;e.run()
        self.assertEqual(waits,[60,120]);self.assertEqual(self.native.sent,[])
        self.assertEqual(self.ledger.account('account-A')['watermark'],watermark)
        self.assertIn('连续 3 次',next(v['text'] for v in events if v['kind']=='error'))
    def test_main_refresh_recovers_without_false_error_or_new_plan(self):
        e=self.baseline();events=[];e.emit=events.append;waits=[];calls=[]
        def step():
            calls.append(1)
            if len(calls)<3:raise self.refresh_failure()
            e.cancel()
        e.step=step;e.stop.wait=lambda seconds:waits.append(seconds) or e.stop.is_set();e.run()
        self.assertEqual(waits[:2],[60,120]);self.assertEqual(len(calls),3)
        self.assertFalse(any(v['kind']=='error' for v in events));self.assertEqual(self.native.sent,[])
    def test_pause_during_refresh_retry_exits_without_further_requests(self):
        e=self.baseline();calls=[]
        def fail():calls.append(1);raise self.refresh_failure()
        e.step=fail;e.stop.wait=lambda seconds:True;e.run();self.assertEqual(calls,[1])
    def test_like_failure_is_never_retried_by_refresh_loop(self):
        e=self.baseline();self.reader.rows['101']=feed(101);self.native.error=NativeError('send unknown',{'code':70})
        waits=[];e.stop.wait=lambda seconds:waits.append(seconds) or False;e.run()
        self.assertEqual(waits,[]);self.assertEqual(self.ledger.get('account-A','101')['status'],'uncertain')
    def prepare_stale_plan(self,value='101',created=10000):
        self.ledger.baseline('account-A',[],1)
        self.reader.rows[value]=feed(int(value),created=created)
        self.ledger.record('account-A',self.reader.rows[value],'朋友','pending',detected_at=10010,scheduled_at=10060)
        e=self.backfill_engine();e.prepare()
        return e
    def test_overdue_outside_24h_is_recovered_by_target_cursor_and_liked_once(self):
        e=self.prepare_stale_plan();models={};calls=[]
        self.native.find_models=lambda ids,alive: {v:models[v] for v in ids if v in models}
        def refresh(cursor,alive):
            calls.append(cursor);models['101']=0x123
            return {'feed_ids':['101']}
        self.native.refresh=refresh
        e.process_pending();e.process_pending()
        self.assertEqual(calls,['102']);self.assertEqual(self.native.sent,['101'])
        saved=self.ledger.get('account-A','101')
        self.assertEqual((saved['detected_at'],saved['scheduled_at']),(10010,10060))
        self.assertEqual(saved['status'],'liked')
    def test_failed_old_post_refresh_does_not_block_other_due_posts(self):
        e=self.prepare_stale_plan();f=feed(102);self.reader.rows['102']=f
        self.ledger.record('account-A',f,'其他好友','pending',detected_at=10010,scheduled_at=10070)
        self.native.find_models=lambda ids,alive:{v:0x123 for v in ids if v=='102'}
        def fail(*args,**kwargs):raise self.refresh_failure()
        self.native.refresh=fail;e.process_pending()
        saved=self.ledger.get('account-A','101')
        self.assertEqual(self.native.sent,['102']);self.assertEqual(saved['status'],'pending')
        self.assertEqual((saved['scheduled_at'],saved['retry_at']),(10060,101800))
        self.assertEqual(saved['pending_note'],'旧动态补取请求失败')
    def test_unavailable_plan_retains_original_times_and_retry_survives_restart(self):
        e=self.prepare_stale_plan();calls=[]
        self.native.find_models=lambda ids,alive:{}
        self.native.refresh=lambda cursor,alive:calls.append(cursor) or {'feed_ids':[]}
        e.process_pending();saved=self.ledger.get('account-A','101')
        self.assertEqual(saved['status'],'pending');self.assertEqual(saved['retry_at'],101800)
        self.assertEqual(saved['scheduled_at'],10060);self.assertTrue(saved['pending_note'])
        self.assertFalse(self.ledger.claim_due('account-A','101',100001))
        self.ledger.close();self.ledger=Ledger(Path(self.temp.name)/'state.db')
        e=self.backfill_engine();e.prepare();e.process_pending()
        self.assertEqual(calls,['102']);self.assertEqual(self.native.sent,[])
        self.assertEqual(self.ledger.next_due('account-A',100000),101800)
        self.advance(1800);e.process_pending();self.assertEqual(calls,['102','102'])
    def test_missing_old_plans_do_not_block_new_due_plan_and_requests_are_bounded(self):
        e=self.prepare_stale_plan()
        for value in range(102,106):
            f=feed(value,created=10000);self.reader.rows[str(value)]=f
            self.ledger.record('account-A',f,'朋友','pending',detected_at=10010,scheduled_at=10060+value)
        self.native.find_models=lambda ids,alive:{v:0x123 for v in ids if v=='105'}
        calls=[];self.native.refresh=lambda cursor,alive:calls.append(cursor) or {'feed_ids':[]}
        e.process_pending()
        self.assertEqual(len(calls),3);self.assertEqual(self.native.sent,['105'])
        e.process_pending();self.assertEqual(len(calls),4)
        self.assertEqual(self.ledger.get('account-A','104')['pending_note'],'暂未取到原动态')
    def test_target_model_without_local_record_never_sends(self):
        e=self.prepare_stale_plan();self.reader.rows.pop('101')
        e.process_pending();self.assertEqual(self.native.sent,[])
        self.assertEqual(self.ledger.get('account-A','101')['pending_note'],'原动态尚未在本机核对到')
    def test_already_liked_old_post_with_no_model_is_removed_from_pending(self):
        e=self.prepare_stale_plan();self.reader.rows['101']['liked']=True
        self.native.find_models=lambda ids,alive:{}
        self.native.refresh=lambda *args:{'feed_ids':[]}
        e.process_pending();self.assertEqual(self.native.sent,[])
        self.assertEqual(self.ledger.get('account-A','101')['status'],'already')
    def test_invalid_model_address_is_rejected(self):
        client=NativeClient.__new__(NativeClient)
        client.rpc=lambda *args:{'refresh_completed':True,'cache_synced':True,'feed_ids':['101'],'feed_models':{'101':'0x1'}}
        with self.assertRaisesRegex(NativeError,'对象无效'):client.refresh()
    def test_poll_interval_includes_work_time(self):
        e=self.engine();waits=[]
        e.step=lambda:self.advance(8)
        e.stop.wait=lambda seconds:waits.append(seconds) or True
        e.run();self.assertEqual(waits,[52])
    def test_account_namespace(self):
        self.baseline();self.assertIsNone(self.ledger.get('account-B','90'))
    def test_three_times_survive_retries_and_restart(self):
        f=feed(101)
        self.ledger.record('timing',f,'朋友','pending',detected_at=120)
        self.ledger.transition('timing','101','submitted',when=130)
        self.assertIsNone(self.ledger.get('timing','101')['confirmed_at'])
        self.ledger.transition('timing','101','pending',when=140)
        self.ledger.record('timing',f,'朋友','pending',detected_at=150)
        self.ledger.transition('timing','101','liked',when=160)
        self.ledger.transition('timing','101','liked',when=170)
        self.ledger.close();self.ledger=Ledger(Path(self.temp.name)/'state.db')
        saved=self.ledger.get('timing','101')
        self.assertEqual((saved['created'],saved['detected_at'],saved['confirmed_at']),(101,120,160))
    def test_failed_or_existing_like_does_not_invent_confirmation_time(self):
        self.ledger.record('timing',feed(101),'朋友','pending',detected_at=120)
        self.ledger.transition('timing','101','uncertain',when=140)
        self.assertIsNone(self.ledger.get('timing','101')['confirmed_at'])
        self.ledger.record('timing',feed(102,liked=True),'朋友','already',detected_at=130)
        self.assertIsNone(self.ledger.get('timing','102')['confirmed_at'])
    def test_old_schema_migration_preserves_unknown_times_and_dedup(self):
        path=Path(self.temp.name)/'old.db';old=sqlite3.connect(path)
        old.execute("CREATE TABLE posts(account TEXT,id TEXT,author TEXT DEFAULT '',label TEXT DEFAULT '',created INTEGER DEFAULT 0,status TEXT,updated REAL,PRIMARY KEY(account,id))")
        old.execute("INSERT INTO posts VALUES('old','101','','朋友',101,'liked',150)")
        old.commit();old.close();migrated=Ledger(path)
        try:
            saved=migrated.get('old','101')
            self.assertEqual(saved['status'],'liked')
            self.assertEqual(saved['created'],101)
            self.assertIsNone(saved['detected_at']);self.assertIsNone(saved['confirmed_at'])
        finally:migrated.close()
    def test_engine_records_detection_and_confirmation_separately(self):
        e=self.baseline();e.clock=lambda:110+self.now
        self.reader.rows['101']=feed(101);e.step()
        saved=self.ledger.get('account-A','101')
        self.assertEqual(saved['created'],101)
        self.assertEqual(saved['detected_at'],110)
        self.assertEqual(saved['confirmed_at'],114)
    def test_random_plan_waits_until_due_and_survives_restart(self):
        e=self.baseline();calls=[]
        e.delay_picker=lambda low,high:calls.append((low,high)) or 60
        self.reader.rows['101']=feed(101);e.step();e.step()
        self.assertEqual(calls,[(60,7200)])
        self.assertEqual(self.native.sent,[])
        self.assertEqual(self.ledger.get('account-A','101')['scheduled_at'],160)
        restarted=self.engine();restarted.clock=lambda:159;restarted.step()
        self.assertEqual(self.native.sent,[])
        restarted.clock=lambda:160;restarted.step()
        self.assertEqual(self.native.sent,['101'])
        self.assertEqual(self.ledger.get('account-A','101')['scheduled_at'],160)
    def test_maximum_delay_and_confirmation_remain_separate(self):
        e=self.baseline();e.delay_picker=lambda low,high:high
        self.reader.rows['101']=feed(101);e.step()
        self.assertEqual(self.ledger.get('account-A','101')['scheduled_at'],7300)
        e.clock=lambda:7299;e.step();self.assertEqual(self.native.sent,[])
        e.clock=lambda:7300;e.step();saved=self.ledger.get('account-A','101')
        self.assertEqual(self.native.sent,['101'])
        self.assertEqual((saved['detected_at'],saved['scheduled_at'],saved['confirmed_at']),(100,7300,7300))
    def test_due_task_is_not_blocked_by_an_older_future_task(self):
        e=self.baseline()
        for value,planned in ((101,7200),(102,160)):
            self.reader.rows[str(value)]=feed(value)
            self.ledger.record('account-A',feed(value),'朋友','pending',detected_at=100,scheduled_at=planned)
        self.assertEqual([row['id'] for row in self.ledger.pending('account-A',1,due_before=160)],['102'])
        e.clock=lambda:160;e.step()
        self.assertEqual(self.native.sent,['102'])
        self.assertEqual(self.ledger.get('account-A','101')['status'],'pending')
    def test_unscheduled_legacy_pending_is_planned_once_on_resume(self):
        self.baseline();self.reader.rows['101']=feed(101)
        self.ledger.record('account-A',feed(101),'朋友','pending',detected_at=90)
        e=self.engine();e.delay_picker=lambda low,high:60;e.prepare()
        self.assertEqual(self.ledger.get('account-A','101')['scheduled_at'],160)
        self.assertFalse(self.ledger.schedule('account-A','101',7200))
        self.engine().step()
        self.assertEqual(self.native.sent,[])
        self.assertEqual(self.ledger.get('account-A','101')['scheduled_at'],160)
    def test_future_plans_do_not_force_old_page_refresh(self):
        e=self.baseline();e.delay_picker=lambda low,high:60
        self.reader.rows.update({str(i):feed(i) for i in range(101,131)})
        e.ingest(list(self.reader.rows.values()));self.ledger.watermark('account-A',list(self.reader.rows))
        self.native.refreshes=[];e.fetch()
        self.assertEqual(self.native.refreshes,['0'])
        e.clock=lambda:160;self.native.refreshes=[];e.fetch()
        self.assertEqual(self.native.refreshes,['0','121','111'])
    def test_claim_rejects_early_or_unscheduled_tasks(self):
        self.ledger.record('timing',feed(101),'朋友','pending',detected_at=100,scheduled_at=160)
        self.assertFalse(self.ledger.claim_due('timing','101',159))
        self.assertTrue(self.ledger.claim_due('timing','101',160))
        self.assertFalse(self.ledger.claim_due('timing','101',161))
        self.ledger.record('timing',feed(102),'朋友','pending',detected_at=100)
        self.assertFalse(self.ledger.claim_due('timing','102',9999))
    def test_timer_wakes_at_plan_instead_of_waiting_a_full_poll(self):
        e=self.engine();e.account='timing';e.clock=lambda:130+self.now
        self.ledger.record('timing',feed(101),'朋友','pending',detected_at=100,scheduled_at=160)
        waits=[];e.step=lambda:self.advance(8);e.stop.wait=lambda seconds:waits.append(seconds) or True
        e.run();self.assertEqual(waits,[22])
    def test_custom_range_is_used_only_for_new_plans(self):
        e=self.baseline(minimum=3,maximum=8);calls=[]
        e.delay_picker=lambda low,high:calls.append((low,high)) or high
        self.reader.rows['101']=feed(101);e.step()
        self.assertEqual(calls,[(180,480)])
        self.assertEqual(self.ledger.get('account-A','101')['scheduled_at'],580)
        restarted=self.engine(minimum=10,maximum=20);restarted.delay_picker=lambda low,high:low
        self.reader.rows['102']=feed(102);restarted.step()
        self.assertEqual(self.ledger.get('account-A','101')['scheduled_at'],580)
        self.assertEqual(self.ledger.get('account-A','102')['scheduled_at'],700)
        self.assertEqual(self.native.sent,[])
    def test_equal_delay_bounds_are_valid(self):
        e=self.baseline(minimum=5,maximum=5);seen=[]
        e.delay_picker=lambda low,high:seen.append((low,high)) or low
        self.reader.rows['101']=feed(101);e.step()
        self.assertEqual(seen,[(300,300)])
        self.assertEqual(self.ledger.get('account-A','101')['scheduled_at'],400)
    def test_invalid_delay_ranges_are_rejected(self):
        for minimum,maximum in ((0,120),(5,4),(1,1441),(1.5,120),('1',120)):
            with self.subTest(minimum=minimum,maximum=maximum):
                with self.assertRaises(ValueError):Config(delay_min_minutes=minimum,delay_max_minutes=maximum).validate()
    def test_upgrade_preserves_latest_plans_and_never_overwrites_existing_state(self):
        base=Path(self.temp.name)/'upgrade';old=base/'朋友圈后台监控助手1.8/MomentsMonitor/data'
        old.mkdir(parents=True)
        source=Ledger(old/'state.sqlite3')
        source.record('timing',feed(101),'朋友','pending',detected_at=120,scheduled_at=600)
        source.close()
        settings={'delay_min_minutes':3,'delay_max_minutes':8}
        (old/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
        home=base/'朋友圈后台监控助手1.9/MomentsMonitor'
        self.assertTrue(restore_previous(home))
        target=Ledger(home/'data/state.sqlite3')
        self.assertEqual(target.get('timing','101')['scheduled_at'],600)
        target.transition('timing','101','liked',when=610);target.close()
        self.assertFalse(restore_previous(home))
        target=Ledger(home/'data/state.sqlite3')
        try:self.assertEqual(target.get('timing','101')['confirmed_at'],610)
        finally:target.close()
        self.assertEqual(json.loads((home/'data/settings.json').read_text(encoding='utf-8')),settings)
        original=sqlite3.connect((old/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
        try:self.assertEqual(original.execute('SELECT status FROM posts').fetchone()[0],'pending')
        finally:original.close()
    def test_unsigned_feed_id_and_author_guard(self):
        value=(1<<63)+123;tid=value-(1<<64)
        xml=f'<SnsDataItem><id>{value}</id><username>friend</username><createTime>101</createTime><like_flag>0</like_flag></SnsDataItem>'
        self.assertEqual(parse_feed({'tid':tid,'user_name':'friend','content':xml},'self')['id'],str(value))
        with self.assertRaises(Exception):parse_feed({'tid':tid,'user_name':'wrong','content':xml},'self')
    def backfill_engine(self,auto=True):
        e=self.engine(auto=auto,backfill=24);e.clock=lambda:100000+self.now
        e.delay_picker=lambda low,high:60
        return e
    def test_first_start_backfills_only_last_24_hours_including_boundary(self):
        self.reader.rows={'90':feed(90,created=13599),'101':feed(101,created=13600),'102':feed(102,created=95000)}
        e=self.backfill_engine();e.step()
        self.assertEqual(self.native.sent,[])
        self.assertEqual([self.ledger.get('account-A',v)['status'] for v in ('90','101','102')],['baseline','pending','pending'])
        self.assertEqual(self.ledger.get('account-A','101')['scheduled_at'],100060)
        self.assertFalse(e.backfill_active)
    def test_backfill_reconsiders_old_baseline_without_changing_completed_or_unknown(self):
        self.baseline()
        for value,status in ((101,'baseline'),(102,'liked'),(103,'already'),(104,'uncertain'),(105,'submitted'),(106,'pending')):
            f=feed(value,created=95000);self.reader.rows[str(value)]=f
            self.ledger.record('account-A',f,'朋友',status,detected_at=96000,scheduled_at=200000 if status=='pending' else None)
        e=self.backfill_engine();e.step()
        self.assertEqual(self.native.sent,[])
        self.assertEqual(self.ledger.get('account-A','101')['status'],'pending')
        self.assertEqual([self.ledger.get('account-A',v)['status'] for v in ('102','103','104','105')],['liked','already','uncertain','uncertain'])
        self.assertEqual(self.ledger.get('account-A','106')['scheduled_at'],200000)
        self.assertEqual(self.ledger.get('account-A','101')['detected_at'],100000)
    def test_startup_scan_ignores_watermark_and_checks_older_pages(self):
        self.baseline()
        self.reader.rows.update({str(i):feed(i,created=90000+i) for i in range(101,131)})
        self.ledger.watermark('account-A',['130']);self.native.refreshes=[]
        e=self.backfill_engine();e.step()
        self.assertEqual(self.native.refreshes,['0','121','111','101'])
        self.assertEqual(len(self.ledger.pending('account-A',100)),30)
    def test_offline_unknown_posts_outside_window_are_not_liked(self):
        self.baseline()
        self.reader.rows.update({'101':feed(101,created=10000),'102':feed(102,created=95000)})
        e=self.backfill_engine();e.step();self.advance(60);e.step()
        self.assertEqual(self.native.sent,['102'])
        self.assertEqual(self.ledger.get('account-A','101')['status'],'baseline')
    def test_backfill_skips_current_likes_self_nonfriends_and_exclusions(self):
        self.baseline()
        for value,author,liked in ((101,'friend',True),(102,'self',False),(103,'stranger',False),(104,'excluded',False)):
            self.reader.rows[str(value)]=feed(value,author,liked,created=95000)
        e=self.engine(excluded=('不赞',),backfill=24);e.clock=lambda:100000;e.step()
        self.assertEqual(self.native.sent,[])
        self.assertEqual([self.ledger.get('account-A',str(i))['status'] for i in range(101,105)],['already','not_friend','not_friend','excluded'])
    def test_backfill_monitor_only_then_enable_likes_keeps_first_detection(self):
        self.baseline();self.reader.rows['101']=feed(101,created=95000)
        e=self.backfill_engine(auto=False);e.step()
        self.assertEqual(self.native.sent,[])
        self.assertEqual(self.ledger.get('account-A','101')['status'],'observed')
        self.advance(100);resumed=self.backfill_engine();resumed.step()
        saved=self.ledger.get('account-A','101')
        self.assertEqual(saved['status'],'pending');self.assertEqual(saved['detected_at'],100000)
        self.assertEqual(saved['scheduled_at'],100160)
        resumed.step();self.assertEqual(self.ledger.get('account-A','101')['scheduled_at'],100160)
    def test_disabling_backfill_keeps_old_baseline_ignored(self):
        self.baseline();f=feed(101,created=95000);self.reader.rows['101']=f
        self.ledger.record('account-A',f,'朋友','baseline')
        e=self.engine();e.clock=lambda:100000;e.step()
        self.assertEqual(self.native.sent,[]);self.assertEqual(self.ledger.get('account-A','101')['status'],'baseline')
    def test_large_backfill_continues_next_cycle_and_does_not_advance_early(self):
        self.baseline();self.native.refreshes=[]
        self.reader.rows.update({str(i):feed(i,created=90000+i) for i in range(101,226)})
        e=self.backfill_engine();e.step()
        self.assertTrue(e.backfill_active);self.assertEqual(e.backfill_cursor,'106')
        self.assertEqual(self.ledger.account('account-A')['watermark'],'90')
        first=self.ledger.get('account-A','225')['scheduled_at'];e.step()
        self.assertFalse(e.backfill_active);self.assertEqual(self.native.refreshes[-1],'106')
        self.assertEqual(self.ledger.account('account-A')['watermark'],'225')
        self.assertEqual(self.ledger.get('account-A','225')['scheduled_at'],first)
        self.assertEqual(len(self.ledger.pending('account-A',200)),125)
    def test_backfill_sync_failure_does_not_commit_cursor_or_progress(self):
        self.baseline();e=self.backfill_engine();e.stop.wait=lambda seconds:False
        self.native.refresh=lambda max_id='0',*args,**kwargs:{'feed_ids':['101','90'] if max_id=='0' else []}
        e.step()
        self.assertTrue(e.backfill_active);self.assertEqual(e.backfill_cursor,'0')
        self.assertEqual(self.ledger.account('account-A')['watermark'],'90')
        self.reader.rows['101']=feed(101,created=95000);e.step()
        self.assertFalse(e.backfill_active);self.assertEqual(self.ledger.get('account-A','101')['status'],'pending')
    def test_normal_catchup_also_continues_beyond_twelve_pages(self):
        e=self.baseline(auto=False);self.native.refreshes=[]
        self.reader.rows.update({str(i):feed(i) for i in range(101,226)})
        e.step()
        self.assertEqual(e.normal_cursor,'106');self.assertEqual(self.ledger.account('account-A')['watermark'],'90')
        e.step()
        self.assertEqual(self.native.refreshes[-1],'106');self.assertEqual(e.normal_cursor,'0')
        self.assertEqual(self.ledger.account('account-A')['watermark'],'225')
        self.assertEqual(self.native.sent,[])
    def test_backfill_cutoff_is_fixed_at_start_even_if_scan_takes_time(self):
        self.baseline();self.reader.rows['101']=feed(101,created=13601)
        e=self.backfill_engine();original=self.native.refresh
        def refresh(*args,**kwargs):self.advance(7200);return original(*args,**kwargs)
        self.native.refresh=refresh;e.step()
        self.assertEqual(e.backfill_cutoff,13600)
        self.assertEqual(self.ledger.get('account-A','101')['status'],'pending')
    def test_restart_preserves_backfill_plan_and_never_relikes_liked(self):
        self.baseline();self.reader.rows['101']=feed(101,created=95000)
        e=self.backfill_engine();e.step();resumed=self.backfill_engine();resumed.step()
        self.assertEqual(self.ledger.get('account-A','101')['scheduled_at'],100060)
        self.advance(60);resumed.step();self.assertEqual(self.native.sent,['101'])
        self.backfill_engine().step();self.assertEqual(self.native.sent,['101'])
    def test_invalid_backfill_range_is_rejected(self):
        for hours in (-1,169,1.5,'24',True):
            with self.subTest(hours=hours):
                with self.assertRaises(ValueError):Config(backfill_hours=hours).validate()
    def test_like_totals_survive_restart_and_use_confirmation_day(self):
        for value,stamp in ((101,999),(102,1000),(103,1999),(104,2000),(105,None)):
            self.ledger.record('totals',feed(value),'朋友','liked')
            if stamp is not None:self.ledger.transition('totals',str(value),'liked',when=stamp)
        for value,status in ((106,'already'),(107,'pending'),(108,'uncertain')):
            self.ledger.record('totals',feed(value),'朋友',status)
        self.assertEqual(self.ledger.like_totals(1000,2000),{'total':5,'today':2})
        self.ledger.close();self.ledger=Ledger(Path(self.temp.name)/'state.db')
        self.assertEqual(self.ledger.like_totals(1000,2000),{'total':5,'today':2})
    def test_like_effect_event_only_follows_confirmed_success_once(self):
        e=self.baseline();events=[];e.emit=events.append
        self.reader.rows['101']=feed(101);self.reader.rows['102']=feed(102,liked=True)
        e.step();e.step()
        effects=[v for v in events if v['kind']=='liked']
        self.assertEqual(len(effects),1);self.assertEqual(effects[0]['id'],'101')
        self.assertEqual(effects[0]['confirmed_at'],self.ledger.get('account-A','101')['confirmed_at'])
        self.assertEqual(self.ledger.like_totals(0,1000)['total'],1)
    def test_unknown_like_result_has_no_success_effect_or_counter(self):
        e=self.baseline();events=[];e.emit=events.append
        self.reader.rows['101']=feed(101);self.native.error=NativeError('unknown',{'code':50})
        with self.assertRaises(NativeError):e.step()
        self.assertEqual([v for v in events if v['kind']=='liked'],[])
        self.assertEqual(self.ledger.like_totals(0,1000)['total'],0)
    def test_upcoming_plans_are_sorted_and_can_be_scoped_to_account(self):
        for value,planned in ((101,300),(102,None),(103,100)):
            self.ledger.record('plans',feed(value),str(value),'pending',scheduled_at=planned)
        self.ledger.record('other',feed(104),'other','pending',scheduled_at=50)
        self.assertEqual([r['label'] for r in self.ledger.upcoming('plans')],['103','101','102'])
        self.assertEqual(self.ledger.upcoming(limit=1)[0]['label'],'other')
    def test_upgrade_can_skip_an_unused_intermediate_version(self):
        base=Path(self.temp.name)/'skipped';old=base/'朋友圈后台监控助手1.7/MomentsMonitor/data'
        source=Ledger(old/'state.sqlite3');source.record('old',feed(101),'朋友','liked');source.close()
        home=base/'朋友圈后台监控助手1.9/MomentsMonitor'
        self.assertTrue(restore_previous(home));target=Ledger(home/'data/state.sqlite3')
        try:self.assertEqual(target.like_totals(0,1000)['total'],1)
        finally:target.close()
    def test_upgrade_prefers_1_10_over_1_9_and_keeps_retry_plan(self):
        base=Path(self.temp.name)/'latest'
        for version,planned in (('1.9',200),('1.10',300)):
            source=Ledger(base/f'朋友圈后台监控助手{version}/MomentsMonitor/data/state.sqlite3')
            source.record('old',feed(101),'朋友','pending',detected_at=120,scheduled_at=planned)
            source.defer_pending('old','101',400,'暂未取到原动态');source.close()
        home=base/'朋友圈后台监控助手1.11/MomentsMonitor'
        self.assertTrue(restore_previous(home));target=Ledger(home/'data/state.sqlite3')
        try:
            row=target.get('old','101')
            self.assertEqual((row['scheduled_at'],row['retry_at'],row['pending_note']),(300,400,'暂未取到原动态'))
        finally:target.close()
if __name__=='__main__':unittest.main(verbosity=2)
