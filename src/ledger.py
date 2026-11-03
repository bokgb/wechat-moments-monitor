# SPDX-License-Identifier: Apache-2.0
"""持久去重：提交前落盘，未知结果不会自动重发。"""
from pathlib import Path
import sqlite3,time,threading,hashlib
from album_store import initialize

class Ledger:
    def __init__(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS accounts(account TEXT PRIMARY KEY,started REAL NOT NULL,watermark TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS posts(account TEXT NOT NULL,id TEXT NOT NULL,author TEXT NOT NULL DEFAULT '',
             label TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,updated REAL NOT NULL,
             PRIMARY KEY(account,id));''')
        with self.db:
            columns={row[1] for row in self.db.execute('PRAGMA table_info(posts)')}
            for name in ('detected_at','confirmed_at','scheduled_at'):
                if name not in columns:self.db.execute(f'ALTER TABLE posts ADD COLUMN {name} REAL')
            if 'retry_at' not in columns:self.db.execute('ALTER TABLE posts ADD COLUMN retry_at REAL')
            if 'pending_note' not in columns:self.db.execute("ALTER TABLE posts ADD COLUMN pending_note TEXT NOT NULL DEFAULT ''")
        initialize(self.db)
    def account(self,account):
        with self.lock:
            row=self.db.execute('SELECT * FROM accounts WHERE account=?',(account,)).fetchone()
            return dict(row) if row else None
    def baseline(self,account,ids,started):
        ids=list(ids)
        with self.lock,self.db:
            self.db.executemany('INSERT OR IGNORE INTO posts(account,id,status,updated) VALUES(?,?,?,?)',[(account,i,'baseline',started) for i in ids])
            self.db.execute('INSERT OR IGNORE INTO accounts VALUES(?,?,?)',(account,started,str(max(map(int,ids),default=0))))
    def record(self,account,feed,label,status,detected_at=None,scheduled_at=None,reconsider=False):
        with self.lock,self.db:
            now=time.time()
            detected=None if status=='baseline' else now if detected_at is None else detected_at
            self.db.execute('INSERT OR IGNORE INTO posts(account,id,author,label,created,status,updated,detected_at,scheduled_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (account,feed['id'],hashlib.sha256(feed['author'].encode()).hexdigest(),label,feed['created'],status,now,detected,scheduled_at))
            if reconsider:
                self.db.execute("UPDATE posts SET author=?,label=?,created=?,status=?,updated=?,detected_at=COALESCE(detected_at,?),scheduled_at=? WHERE account=? AND id=? AND status IN ('baseline','observed','excluded','not_friend','unreadable')",
                    (hashlib.sha256(feed['author'].encode()).hexdigest(),label,feed['created'],status,now,detected,scheduled_at,account,feed['id']))
            return self.get(account,feed['id'])
    def get(self,account,feed_id):
        with self.lock:
            row=self.db.execute('SELECT * FROM posts WHERE account=? AND id=?',(account,feed_id)).fetchone()
            return dict(row) if row else None
    def transition(self,account,feed_id,status,expected=None,when=None):
        with self.lock,self.db:
            now=time.time() if when is None else when
            sql='UPDATE posts SET status=?,updated=?';bind=[status,now]
            if status=='liked':sql+=',confirmed_at=COALESCE(confirmed_at,?)';bind.append(now)
            sql+=' WHERE account=? AND id=?';bind.extend((account,feed_id))
            if expected is not None:sql+=' AND status=?';bind.append(expected)
            return self.db.execute(sql,bind).rowcount==1
    def watermark(self,account,feed_ids):
        with self.lock,self.db:
            previous=self.account(account)
            highest=max([int(previous['watermark'])]+[int(i) for i in feed_ids])
            self.db.execute('UPDATE accounts SET watermark=? WHERE account=?',(str(highest),account))
    def pending(self,account,limit=25,due_before=None,batch_id=None):
        with self.lock:
            sql="SELECT posts.*,(SELECT username FROM post_targets t WHERE t.account=posts.account AND t.id=posts.id) AS source_user FROM posts WHERE account=? AND status='pending'";bind=[account]
            if batch_id is not None:
                sql+=' AND EXISTS(SELECT 1 FROM album_plan_members t WHERE t.account=posts.account AND t.id=posts.id AND t.batch=?)';bind.append(batch_id)
            if due_before is not None:
                sql+=' AND scheduled_at IS NOT NULL AND scheduled_at<=? AND (retry_at IS NULL OR retry_at<=?)';bind.extend((due_before,due_before))
            sql+=' ORDER BY COALESCE(scheduled_at,created),created,id LIMIT ?';bind.append(limit)
            return [dict(row) for row in self.db.execute(sql,bind)]
    def unscheduled(self,account):
        with self.lock:return [dict(row) for row in self.db.execute("SELECT * FROM posts WHERE account=? AND status='pending' AND scheduled_at IS NULL",(account,))]
    def schedule(self,account,feed_id,when):
        with self.lock,self.db:
            return self.db.execute("UPDATE posts SET scheduled_at=? WHERE account=? AND id=? AND status='pending' AND scheduled_at IS NULL",(when,account,feed_id)).rowcount==1
    def next_due(self,account,after):
        with self.lock:return self.db.execute("SELECT MIN(MAX(scheduled_at,COALESCE(retry_at,scheduled_at))) FROM posts WHERE account=? AND status='pending' AND MAX(scheduled_at,COALESCE(retry_at,scheduled_at))>?",(account,after)).fetchone()[0]
    def defer_pending(self,account,feed_id,retry_at,note):
        with self.lock,self.db:
            return self.db.execute("UPDATE posts SET retry_at=?,pending_note=? WHERE account=? AND id=? AND status='pending'",(retry_at,note,account,feed_id)).rowcount==1
    def claim_due(self,account,feed_id,when):
        with self.lock,self.db:
            return self.db.execute("UPDATE posts SET status='submitted',updated=?,retry_at=NULL,pending_note='' WHERE account=? AND id=? AND status='pending' AND scheduled_at IS NOT NULL AND scheduled_at<=? AND (retry_at IS NULL OR retry_at<=?)",(when,account,feed_id,when,when)).rowcount==1
    def unresolved(self,account):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM posts WHERE account=? AND status IN ('submitted','uncertain')",(account,))]
    def history(self,limit=200):
        with self.lock:return [dict(row) for row in self.db.execute("SELECT * FROM posts WHERE status!='baseline' ORDER BY updated DESC LIMIT ?",(limit,))]
    def counts(self,account):
        with self.lock:return dict(self.db.execute('SELECT status,COUNT(*) FROM posts WHERE account=? GROUP BY status',(account,)))
    def like_totals(self,day_start,day_end,account=None):
        with self.lock:
            sql="SELECT COUNT(*),COALESCE(SUM(CASE WHEN confirmed_at>=? AND confirmed_at<? THEN 1 ELSE 0 END),0) FROM posts WHERE status='liked'";bind=[day_start,day_end]
            if account:sql+=' AND account=?';bind.append(account)
            row=self.db.execute(sql,bind).fetchone()
            return {'total':int(row[0]),'today':int(row[1])}
    def task_counts(self,account=None):
        with self.lock:
            sql="SELECT status,pending_note,COUNT(*) AS n FROM posts WHERE status!='baseline'";bind=[]
            if account:sql+=' AND account=?';bind.append(account)
            sql+=' GROUP BY status,pending_note'
            result=dict(waiting=0,retry=0,attention=0,liked=0,all=0)
            for row in self.db.execute(sql,bind):
                result['all']+=row['n']
                if row['status']=='pending':result['retry' if row['pending_note'] else 'waiting']+=row['n']
                elif row['status'] in ('uncertain','unreadable'):result['attention']+=row['n']
                elif row['status']=='liked':result['liked']+=row['n']
            return result
    def tasks(self,account=None,group='all',search='',since=None,limit=500,confirmed_since=None):
        groups={'all':"status!='baseline'",'waiting':"status='pending' AND pending_note=''",
            'retry':"status='pending' AND pending_note!=''",'attention':"status IN ('uncertain','unreadable')",
            'liked':"status='liked'",'skipped':"status IN ('already','excluded','not_friend')"}
        if group not in groups:raise ValueError('未知任务分组')
        with self.lock:
            sql='SELECT * FROM posts WHERE ('+groups[group]+')';bind=[]
            if account:sql+=' AND account=?';bind.append(account)
            if search:sql+=' AND instr(lower(label),lower(?))>0';bind.append(search)
            if since is not None:sql+=' AND created>=?';bind.append(since)
            if confirmed_since is not None:sql+=' AND confirmed_at>=? AND confirmed_at<?';bind.extend((confirmed_since,confirmed_since+86400))
            sql+=' ORDER BY '+('COALESCE(retry_at,scheduled_at,created),created' if group in ('waiting','retry') else 'updated DESC')+' LIMIT ?'
            bind.append(limit)
            return [dict(row) for row in self.db.execute(sql,bind)]
    def upcoming(self,account=None,limit=8):
        with self.lock:
            sql="SELECT label,scheduled_at,retry_at,pending_note FROM posts WHERE status='pending'";bind=[]
            if account:sql+=' AND account=?';bind.append(account)
            sql+=' ORDER BY (scheduled_at IS NULL),MAX(scheduled_at,COALESCE(retry_at,scheduled_at)),created LIMIT ?';bind.append(limit)
            return [dict(row) for row in self.db.execute(sql,bind)]
    def close(self):
        with self.lock:self.db.close()
