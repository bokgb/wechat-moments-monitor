# SPDX-License-Identifier: Apache-2.0
"""Execute only the selected album batch, without a friends-homepage monitor."""
from dataclasses import replace
from engine import Engine
from album_store import AlbumStore
from native_client import Cancelled,NativeError
from local_reader import ReaderError

class BatchEngine(Engine):
    def __init__(self,*args,batch_id,**kwargs):
        super().__init__(*args,**kwargs);self.config=replace(self.config,auto_like=True,backfill_hours=0)
        self.batch_id=batch_id;self.store=AlbumStore(self.ledger)
    def pending_rows(self):return self.ledger.pending(self.account,25,due_before=self.clock(),batch_id=self.batch_id)
    def run(self):
        try:
            self.native=self.native_factory(self.home,self.reader_home);self.native.check(self.alive)
            self.reader=self.reader_factory(self.reader_home,self.config.account or None);self.account=self.reader.account_hash
            batch=self.store.get(self.batch_id)
            if not batch or batch['state']!='queued' or batch['account']!=self.account:raise ReaderError('补赞任务或登录账号不匹配，未执行点赞')
            if batch['username'] not in self.reader.contacts:raise ReaderError('所选对象不在当前好友名单中，未执行点赞')
            self.log(f"仅执行 {batch['label']} 的本次指定日期补赞；不会启动所有好友监控。")
            self.discovered=len(self.store.plans(self.batch_id))
            for row in self.store.plans(self.batch_id):
                if row['status']=='pending' and row['scheduled_at'] is None:
                    self.ledger.schedule(self.account,row['id'],self.clock()+self.random_delay())
                if row['status'] not in ('submitted','uncertain'):continue
                feed=self.reader.get(row['id'])
                if feed and feed['liked'] and feed['like_flag']=='1':self.ledger.transition(self.account,row['id'],'liked',when=self.clock())
                else:self.ledger.transition(self.account,row['id'],'uncertain')
            while self.alive():
                self.reader.refresh_contacts()
                rows=self.store.plans(self.batch_id);pending=[r for r in rows if r['status']=='pending']
                if not pending:
                    unknown=sum(r['status'] in ('uncertain','submitted','unreadable') for r in rows)
                    message=f"{batch['label']}：本次补赞任务已结束，无待执行计划。"+(f'有 {unknown} 条结果需核对，未自动重发。' if unknown else '')
                    self.emit({'kind':'album_finished','text':message});self.log(message);self.stats();return
                self.phase('checking',f"{batch['label']} · 剩余 {len(pending)} 条")
                self.process_pending();self.stats()
                pending=[r for r in self.store.plans(self.batch_id) if r['status']=='pending']
                if not pending:continue
                now=self.clock();due=min(max(r['scheduled_at'] or now,r['retry_at'] or 0) for r in pending)
                wait=min(60,max(1,due-now))
                self.phase('watching',f"{batch['label']} · 剩余 {len(pending)} 条，等待计划时间")
                if self.stop.wait(wait):break
        except Cancelled:pass
        except Exception as exc:
            text=str(exc) if isinstance(exc,(NativeError,ReaderError,ValueError)) else type(exc).__name__
            self.emit({'kind':'error','text':text});self.log('本次补赞已暂停：'+text)
        finally:self.close();self.emit({'kind':'stopped'})
