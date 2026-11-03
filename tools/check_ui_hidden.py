"""Exercise actual Tk controls in a withdrawn isolated window. No WeChat calls."""
from pathlib import Path
from unittest.mock import patch
import json,sys,tempfile,time,tkinter as tk
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT/'src'))
from app import Application
from album import ContactsTask
from engine import Engine,Config
with tempfile.TemporaryDirectory() as folder:
    root=tk.Tk();root.withdraw()
    try:
        with patch('app.LocalReader.accounts',return_value=[]):app=Application(root,Path(folder))
        root.update_idletasks()
        assert not root.winfo_viewable() and len(app.tabs.tabs())==4
        assert root.winfo_reqheight()<=860
        assert app.total.get()=='0' and app.ui_state=='stopped'
        now=time.time()
        for ident,status in (('101','pending'),('102','pending'),('103','uncertain'),('104','liked')):
            app.ledger.record('test',{'id':ident,'author':'friend','created':now-86400},'测试好友'+ident,status,detected_at=now-100,scheduled_at=now-10)
        app.ledger.defer_pending('test','102',now+1800,'暂未取到原动态')
        app.ledger.transition('test','104','liked',when=now)
        app.refresh_history()
        assert (app.waiting.get(),app.retry_count.get(),app.attention.get())==('1','1','1')
        assert len(app.upcoming.get_children())==1
        app.overview_group.set('补赞重试');app.refresh_overview()
        row=app.upcoming.item(app.upcoming.get_children()[0])['values']
        assert '暂未取到' in row[1] and '等待开启' in row[2]
        app.upcoming.selection_set(app.upcoming.get_children()[0]);app.open_queue_record()
        assert app.record_group.get()=='补赞重试' and len(app.history_rows)==1
        assert '下次重试' in app.detail.get('1.0','end') and '原计划点赞' in app.detail.get('1.0','end')
        app.show_records('今日已点赞');assert len(app.history_rows)==1
        app.search.set('不存在');app.refresh_history();assert len(app.history_rows)==0
        app.reset_filters();assert len(app.history_rows)==4
        app.handle_event({'kind':'checked','account':'test','at':now})
        app.handle_event({'kind':'phase','account':'test','phase':'sync_wait','text':'等待同步测试'})
        app.handle_event({'kind':'stats','account':'test','discovered':2,'liked':1,'checked':now+60})
        assert app.ui_state=='sync_wait' and app.last_success==now
        app.handle_event({'kind':'error','text':'请登录微信'})
        app.handle_event({'kind':'stopped'})
        assert app.ui_state=='error' and app.state_label.cget('fg')=='#b13c35'
        app.open_diagnostics();root.update_idletasks();assert app.diagnostic_open
        app.animations.set(False)
        with patch.object(app.effects,'play') as play:
            app.confirmed_like({'account':'test','id':'104','label':'测试好友','confirmed_at':now});play.assert_not_called()
        app.effects.winfo_viewable=lambda:True;app.effects.play('测试好友',preview=True)
        assert len(app.effects.find_all())>5;app.effects.stop();app.effects.idle()
        # Read contacts while a monitor remains active: isolated event routing,
        # controls and worker ownership must survive success and failure.
        app.account.set('selected');app.pause_requested=False;app.native_test=False
        monitor=Engine(Path(folder),Config('selected'),app.ledger,app.events.put)
        app.engine=monitor;app.worker=SimpleNamespace(is_alive=lambda:True)
        app.set_phase('watching','自动点赞已开启');app.set_busy(True)
        assert str(app.album_read.cget('state'))=='normal'
        assert str(app.album_person_box.cget('state'))=='readonly'
        assert app.album_scan.cget('text')=='暂停监控并预览'
        reader=SimpleNamespace(account_hash='test',contacts={'friend':{'nick_name':'昵称'}},label=lambda user:'朋友',close=lambda:None)
        with patch('album_ui.ContactsTask',side_effect=lambda home,account,emit:ContactsTask(home,account,emit,reader_factory=lambda *args:reader)):
            app.album_read_contacts();app.contacts_worker.join(5)
        assert not app.contacts_busy()
        while not app.events.empty():app.handle_event(app.events.get_nowait())
        assert app.engine is monitor and app.worker.is_alive()
        assert app.ui_state=='watching' and app.status.get()=='自动点赞已开启'
        assert '朋友' in app.album_contacts and str(app.album_read.cget('state'))=='normal'
        app.handle_event({'kind':'contacts_error','text':'读取失败'})
        assert app.ui_state=='watching' and app.engine is monitor
        before=dict(app.album_contacts)
        app.handle_event({'kind':'contacts_loaded','requested_account':'previous','account':'other','contacts':[]})
        assert app.album_contacts==before
        # Explicit task switching waits for the old monitor; F8 cancels it.
        assert app.album_switch('scan',('test','friend',0,1))
        assert monitor.stop.is_set() and app.album_pending
        app.worker=SimpleNamespace(is_alive=lambda:False)
        with patch.object(app,'album_launch') as launch:
            app.album_switch_ready();launch.assert_called_once_with('scan',('test','friend',0,1),None)
        app.album_pending=('scan',('test','friend',0,1),None)
        app.pause()
        with patch.object(app,'album_launch') as launch:
            app.album_switch_ready();launch.assert_not_called()
        app.worker=None;app.engine=None
        assert not root.winfo_viewable()
        geometry={'requested_width':root.winfo_reqwidth(),'requested_height':root.winfo_reqheight(),'overview_height':app.overview_tab.winfo_reqheight()}
        app.closing=True;app.ledger.close()
        print(json.dumps({'hidden_window':True,'tabs':4,'queue_filters':True,'detail_timeline':True,'today_confirmation_filter':True,'health_not_overwritten_by_stats':True,'error_persists':True,'animation_optional':True,'contacts_while_monitoring':True,'contact_failure_isolated':True,'stale_account_rejected':True,'task_switch_waits':True,'f8_cancels_switch':True,'wechat_calls':0,**geometry},ensure_ascii=False))
    finally:root.destroy()
