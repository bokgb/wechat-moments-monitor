from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT/'src'))
from ledger import Ledger
from ui_model import task_state,task_next,timestamp

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ledger=Ledger(Path(self.temp.name)/'state.sqlite3')
    def tearDown(self):self.ledger.close();self.temp.cleanup()
    def record(self,value,status='pending',account='A',label='朋友',created=100):
        self.ledger.record(account,{'id':str(value),'author':'friend','created':created},label,status,detected_at=110,scheduled_at=160)
        return self.ledger.get(account,str(value))
    def test_retry_and_waiting_counts_are_disjoint_and_submitted_is_not_manual_attention(self):
        for value,status in ((1,'pending'),(2,'pending'),(3,'uncertain'),(4,'unreadable'),(5,'submitted')):self.record(value,status)
        self.ledger.defer_pending('A','2',900,'暂未取到原动态')
        counts=self.ledger.task_counts('A')
        self.assertEqual((counts['waiting'],counts['retry'],counts['attention']),(1,1,2))
        self.assertEqual([r['id'] for r in self.ledger.tasks('A','retry')],['2'])
    def test_filters_run_before_limit_so_old_retries_are_reachable(self):
        self.record(1);self.ledger.defer_pending('A','1',900,'等待补取')
        for value in range(2,510):self.record(value,'liked')
        self.assertEqual([r['id'] for r in self.ledger.tasks('A','retry',limit=8)],['1'])
    def test_literal_name_search_does_not_treat_percent_as_wildcard(self):
        self.record(1,label='好友%');self.record(2,label='普通好友')
        self.assertEqual([r['id'] for r in self.ledger.tasks(search='%')],['1'])
    def test_account_scope_applies_to_totals_counts_and_records(self):
        self.record(1,'liked');self.ledger.transition('A','1','liked',when=200)
        self.record(2,'liked',account='B');self.record(3,account='B')
        self.assertEqual(self.ledger.like_totals(100,300,'A'),{'today':1,'total':1})
        self.assertEqual(self.ledger.task_counts('A')['waiting'],0)
        self.assertEqual(len(self.ledger.tasks('A')),1)
    def test_today_uses_confirmation_day_even_if_post_is_old(self):
        self.record(1,'liked',created=1);self.ledger.transition('A','1','liked',when=100000)
        self.record(2,'liked',created=100000);self.ledger.transition('A','2','liked',when=1)
        self.assertEqual([r['id'] for r in self.ledger.tasks('A','liked',confirmed_since=99999)],['1'])
    def test_publication_date_filter_is_explicit(self):
        self.record(1,created=100);self.record(2,created=200)
        self.assertEqual([r['id'] for r in self.ledger.tasks(since=150)],['2'])
    def test_stopped_overdue_plan_does_not_promise_automatic_action(self):
        row=self.record(1)
        self.assertIn('等待开启',task_next(row,200,running=False))
        self.assertIn('等待开启',task_state(row,200,running=True,monitor_only=True))
        self.assertEqual(task_next(row,200,running=True),'下一轮处理')
    def test_retry_countdown_uses_retry_time_not_old_original_schedule(self):
        self.record(1);self.ledger.defer_pending('A','1',2000,'等待补取');row=self.ledger.get('A','1')
        self.assertEqual(task_next(row,200,running=True),'30分钟后')
    def test_unknown_results_never_offer_automatic_retry(self):
        row=self.record(1,'uncertain');self.assertIn('微信核对',task_next(row,200,running=True))
    def test_dates_distinguish_today_from_older_days(self):
        self.assertTrue(timestamp(100000,now=100000).startswith('今天'))
        self.assertFalse(timestamp(100000,now=300000).startswith('今天'))
if __name__=='__main__':unittest.main(verbosity=2)
