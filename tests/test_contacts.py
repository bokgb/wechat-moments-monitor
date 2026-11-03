"""Contact reads cannot stop, pause or replace a running monitor."""
from pathlib import Path
import sys,threading,unittest
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT/'src'))
from album import ContactsTask
from local_reader import ReaderError

class Reader:
    account_hash='test';contacts={'friend':{'nick_name':'昵称'}}
    closed=False
    def label(self,user):return '朋友'
    def close(self):self.closed=True

class ContactTests(unittest.TestCase):
    def test_contact_events_are_isolated_and_reader_closed(self):
        reader=Reader();events=[]
        ContactsTask('unused','selected',events.append,reader_factory=lambda *args:reader).run()
        self.assertEqual([e['kind'] for e in events],['contacts_loaded','contacts_finished'])
        self.assertEqual(events[0]['requested_account'],'selected')
        self.assertEqual(events[0]['contacts'],[{'user':'friend','label':'朋友','nick':'昵称'}])
        self.assertTrue(reader.closed)
    def test_failure_does_not_emit_monitor_error_or_stopped(self):
        events=[]
        def failure(*args):raise ReaderError('读取失败')
        ContactsTask('unused','selected',events.append,reader_factory=failure).run()
        self.assertEqual([e['kind'] for e in events],['contacts_error','contacts_finished'])
    def test_close_during_read_suppresses_results_and_releases_reader(self):
        entered=threading.Event();release=threading.Event();events=[];reader=Reader()
        def factory(*args):entered.set();release.wait(3);return reader
        task=ContactsTask('unused','selected',events.append,reader_factory=factory)
        worker=threading.Thread(target=task.run);worker.start()
        try:
            self.assertTrue(entered.wait(3));task.cancel()
        finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertTrue(reader.closed)
        self.assertEqual([e['kind'] for e in events],['contacts_finished'])

if __name__=='__main__':unittest.main()
