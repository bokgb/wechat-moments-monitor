# SPDX-License-Identifier: Apache-2.0
"""Durable draft scans. Scanning never creates an executable like task."""
import uuid,time,hashlib,json

def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS album_batches(
        id TEXT PRIMARY KEY,account TEXT NOT NULL,username TEXT NOT NULL,label TEXT NOT NULL,
        since REAL NOT NULL,until REAL NOT NULL,created REAL NOT NULL,state TEXT NOT NULL,
        cursor TEXT NOT NULL DEFAULT '0',pages INTEGER NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '');
      CREATE TABLE IF NOT EXISTS album_items(
        batch TEXT NOT NULL,id TEXT NOT NULL,created INTEGER NOT NULL,category TEXT NOT NULL,
        PRIMARY KEY(batch,id));
      CREATE TABLE IF NOT EXISTS post_targets(
        account TEXT NOT NULL,id TEXT NOT NULL,username TEXT NOT NULL,batch TEXT NOT NULL,
        PRIMARY KEY(account,id));
      CREATE TABLE IF NOT EXISTS album_plan_members(
        batch TEXT NOT NULL,account TEXT NOT NULL,id TEXT NOT NULL,
        PRIMARY KEY(batch,account,id));
      INSERT OR IGNORE INTO album_plan_members SELECT batch,account,id FROM post_targets;
    ''')
    if 'summary' not in {r[1] for r in db.execute('PRAGMA table_info(album_items)')}:
        with db:db.execute("ALTER TABLE album_items ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
    with db:
        db.execute("UPDATE album_batches SET state='paused',cursor='0',pages=0,reason='旧版空相册结果尚未确认，请点击继续上次扫描' WHERE state='ready' AND NOT EXISTS(SELECT 1 FROM album_items i WHERE i.batch=album_batches.id)")

class AlbumStore:
    def __init__(self,ledger):self.ledger=ledger
    def create(self,account,user,label,since,until):
        if since<0 or until<since:raise ValueError('日期范围无效')
        key=uuid.uuid4().hex
        with self.ledger.lock,self.ledger.db:
            self.ledger.db.execute('INSERT INTO album_batches(id,account,username,label,since,until,created,state) VALUES(?,?,?,?,?,?,?,?)',
                (key,account,user,label,since,until,time.time(),'scanning'))
        return key
    def get(self,key=None):
        with self.ledger.lock:
            row=self.ledger.db.execute('SELECT * FROM album_batches '+('WHERE id=?' if key else 'ORDER BY created DESC LIMIT 1'),(key,) if key else ()).fetchone()
            if not row:return None
            result=dict(row);result['counts']=dict(self.ledger.db.execute('SELECT category,COUNT(*) FROM album_items WHERE batch=? GROUP BY category',(row['id'],)))
            return result
    def state(self,key,state,reason=''):
        with self.ledger.lock,self.ledger.db:self.ledger.db.execute("UPDATE album_batches SET state=?,reason=? WHERE id=? AND state!='queued'",(state,reason,key))
    def page(self,key,feeds,categories,cursor,done,reason=''):
        with self.ledger.lock,self.ledger.db:
            batch=self.get(key)
            if not batch or batch['state']!='scanning':raise ValueError('扫描任务已变化，请重新读取')
            self.ledger.db.executemany('INSERT OR IGNORE INTO album_items(batch,id,created,category,summary) VALUES(?,?,?,?,?)',[(key,f['id'],f['created'],c,f.get('summary','')) for f,c in zip(feeds,categories)])
            self.ledger.db.execute('UPDATE album_batches SET cursor=?,pages=pages+1,state=?,reason=? WHERE id=?',
                (cursor,'ready' if done else 'scanning',reason,key))
    def items(self,key):
        with self.ledger.lock:return [dict(r) for r in self.ledger.db.execute('SELECT * FROM album_items WHERE batch=? ORDER BY created,id',(key,))]
    def plans(self,key):
        with self.ledger.lock:
            return [dict(r) for r in self.ledger.db.execute('SELECT p.* FROM posts p JOIN album_plan_members t ON t.account=p.account AND t.id=p.id WHERE t.batch=? ORDER BY p.created,p.id',(key,))]
    def enqueue(self,key,feeds,clock,delay_picker):
        """One transaction, canonical per-account post key prevents duplicates."""
        with self.ledger.lock,self.ledger.db:
            batch=self.get(key)
            if not batch or batch['state']!='ready':raise ValueError('请先完成扫描；已加入的任务不会重复加入')
            expected={r['id'] for r in self.items(key) if r['category'] in ('candidate','planned')}
            if set(feeds)!=expected:raise ValueError('扫描结果核对不完整，请重新扫描')
            count=0;now=clock();author=hashlib.sha256(batch['username'].encode()).hexdigest()
            for feed_id,feed in feeds.items():
                if feed['author']!=batch['username'] or not batch['since']<=feed['created']<=batch['until'] or feed['own']:raise ValueError('好友或日期核对失败，未加入计划')
                old=self.ledger.get(batch['account'],feed_id)
                if old and old['author'] and old['author']!=author:raise ValueError('帖子作者核对失败，未加入计划')
                if old and old['status'] in ('liked','already','submitted','uncertain'):continue
                if feed['liked'] or feed['like_flag']=='1':continue
                if feed['like_flag']!='0':continue
                if old and old['status']=='pending':
                    self.ledger.db.execute('INSERT OR IGNORE INTO post_targets VALUES(?,?,?,?)',(batch['account'],feed_id,batch['username'],key))
                    self.ledger.db.execute('INSERT OR IGNORE INTO album_plan_members VALUES(?,?,?)',(key,batch['account'],feed_id))
                    count+=1;continue
                planned=now+delay_picker()
                if old:
                    self.ledger.db.execute("UPDATE posts SET author=?,label=?,created=?,status='pending',updated=?,detected_at=COALESCE(detected_at,?),scheduled_at=?,retry_at=NULL,pending_note='' WHERE account=? AND id=?",
                        (author,batch['label'],feed['created'],now,now,planned,batch['account'],feed_id))
                else:
                    self.ledger.db.execute("INSERT INTO posts(account,id,author,label,created,status,updated,detected_at,scheduled_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
                        (batch['account'],feed_id,author,batch['label'],feed['created'],now,now,planned))
                self.ledger.db.execute('INSERT OR REPLACE INTO post_targets VALUES(?,?,?,?)',(batch['account'],feed_id,batch['username'],key));count+=1
                self.ledger.db.execute('INSERT OR IGNORE INTO album_plan_members VALUES(?,?,?)',(key,batch['account'],feed_id))
            # Do not create/advance a normal timeline watermark here. If this
            # account is new, Engine.prepare will establish its usual baseline.
            self.ledger.db.execute("UPDATE album_batches SET state='queued',reason=? WHERE id=?",(json.dumps({'scheduled':count}),key))
            return count
