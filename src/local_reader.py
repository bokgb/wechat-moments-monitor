# SPDX-License-Identifier: Apache-2.0
"""仅读取选定账号的联系人和朋友圈库；密钥仅保留在内存。"""
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
import hashlib,html,io,re,shutil,tempfile
from read_backend import db as backend

class ReaderError(RuntimeError):pass

class _Scope(backend.WeChatDB):
    def _collect_db_files(self):
        return [entry for entry in super()._collect_db_files() if Path(entry[1]).name in {'contact.db','sns.db'}]
    def _load_or_extract_keys(self,master_key=None):
        self.master_key=self.cfg_dword=None
        self._keys.update(self.extract_keys())
        self.unkeyed=[rel for rel,_,_ in self._db_files if not self._key_works(rel)]
    def _save_keys(self):pass
    def _stable_key_file(self,account=None):return None
    def _try_other_accounts(self):return False
    def _select_account_by_keys(self,candidates):return False
    def extract_master_key(self):return None

def tag(xml,name):
    match=re.search(r'<'+re.escape(name)+r'>(.*?)</'+re.escape(name)+r'>',xml,re.S)
    if not match:return None
    value=re.sub(r'^<!\[CDATA\[(.*?)\]\]>$',r'\1',match[1],flags=re.S)
    return html.unescape(value)

def parse_feed(row,owner,include_preview=False):
    xml=row['content'];xml=xml.decode('utf-8') if isinstance(xml,bytes) else xml
    feed_id=tag(xml,'id')
    if not feed_id or not feed_id.isdecimal() or int(feed_id)!=(int(row['tid'])&((1<<64)-1)):
        raise ReaderError('朋友圈编号核对失败，已停止')
    author=tag(xml,'username') or row['user_name']
    if author!=row['user_name']:raise ReaderError('朋友圈作者核对失败，已停止')
    raw_time=tag(xml,'createTime')
    likes=tag(xml,'like_user_list') or ''
    mine=any(tag(block,'username')==owner for block in re.findall(r'<user_comment>(.*?)</user_comment>',likes,re.S))
    result={'id':feed_id,'author':author,'created':int(raw_time) if raw_time and raw_time.isdecimal() else 0,
            'liked':mine,'like_flag':tag(xml,'like_flag'),'own':author==owner}
    if include_preview:result['summary']=' '.join((tag(xml,'contentDesc') or '[无文字动态]').split())[:180]
    return result

class LocalReader:
    @staticmethod
    def accounts():return backend.list_accounts()
    def __init__(self,home,account=None):
        self.cache_root=(Path(home)/'data'/'cache').resolve();self.cache_root.mkdir(parents=True,exist_ok=True)
        self.scratch=Path(tempfile.mkdtemp(prefix='session-',dir=self.cache_root)).resolve();self.db=None
        try:
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.db=_Scope(account=account or None,workdir=str(self.scratch),keys_file=str(self.scratch/'unused.json'))
            if self.db.unkeyed:raise ReaderError('暂时无法读取此账号，请确认微信已登录并选择正确账号')
            self.owner=self.db.get_self_info().get('username')
            if not self.owner:raise ReaderError('无法确认本机登录账号')
            self.account_hash=hashlib.sha256(self.owner.encode()).hexdigest()
            self.sns=next((rel for rel,path,_ in self.db._db_files if Path(path).name=='sns.db'),None)
            if not self.sns:raise ReaderError('本机没有找到朋友圈库，请在电脑版微信查看过朋友圈后再开始')
            self.refresh_contacts()
        except Exception:
            self.close();raise
    def refresh_contacts(self):
        conn=self.db._contact_conn()
        if conn is None:raise ReaderError('联系人暂时无法读取')
        try:
            self.contact_columns=[row[1] for row in conn.execute('PRAGMA table_info(contact)')]
            if 'local_type' not in self.contact_columns:raise ReaderError('无法确认当前版本的好友名单格式')
            extra=[name for name in ('local_type','verify_flag','is_friend','delete_flag') if name in self.contact_columns]
            query='SELECT username,remark,nick_name'+(' ,'+','.join(extra) if extra else '')+' FROM contact'
            rows=[dict(row) for row in conn.execute(query)]
        finally:conn.close()
        self.contacts={}
        for row in rows:
            user=row['username']
            if not user or user==self.owner or user.endswith('@chatroom') or user.startswith('gh_') or user in {'weixin','filehelper'}:continue
            if row.get('local_type')!=1 or row.get('delete_flag',0):continue
            if row.get('verify_flag',0):continue
            if 'is_friend' in row and not row['is_friend']:continue
            self.contacts[user]=row
    def all_ids(self):
        conn=self.db._open(self.sns)
        try:return [str(int(row[0])&((1<<64)-1)) for row in conn.execute('SELECT tid FROM SnsTimeLine')]
        finally:conn.close()
    def refresh_snapshot(self):
        # Invalidate only our own decrypted snapshot stamp. The source database
        # and its WAL remain read-only; the backend rebuilds a checked snapshot.
        stamp=Path(self.db.workdir)/(self.sns.replace('\\','__')+'.stamp')
        if not stamp.resolve().is_relative_to(self.scratch):raise ReaderError('读取缓存路径核对失败')
        with self.db._build_lock_for(self.sns):
            stamp.unlink(missing_ok=True)
            conn=self.db._open_unlocked(self.sns)
            conn.close()
    def feeds(self,limit=400):
        conn=self.db._open(self.sns)
        try:
            conn.execute('PRAGMA query_only=ON')
            rows=conn.execute('SELECT tid,user_name,content FROM SnsTimeLine ORDER BY (tid<0) DESC,tid DESC LIMIT ?',(limit,)).fetchall()
            return [parse_feed(row,self.owner) for row in rows]
        finally:conn.close()
    def get_preview(self,feed_id):return self.get(feed_id,include_preview=True)
    def get(self,feed_id,include_preview=False):
        tid=int(feed_id);tid=tid-(1<<64) if tid>=1<<63 else tid
        conn=self.db._open(self.sns)
        try:
            conn.execute('PRAGMA query_only=ON')
            row=conn.execute('SELECT tid,user_name,content FROM SnsTimeLine WHERE tid=?',(tid,)).fetchone()
            return parse_feed(row,self.owner,include_preview) if row else None
        finally:conn.close()
    def label(self,user):
        row=self.contacts.get(user,{})
        return row.get('remark') or row.get('nick_name') or '好友'
    def excluded_users(self,names):
        names=set(names)
        return {user for user,row in self.contacts.items() if names.intersection((user,row.get('remark'),row.get('nick_name')))}
    def close(self):
        if self.db is not None:self.db._keys.clear();self.db=None
        if self.scratch.is_relative_to(self.cache_root) and self.scratch.name.startswith('session-'):
            shutil.rmtree(self.scratch,ignore_errors=True)
