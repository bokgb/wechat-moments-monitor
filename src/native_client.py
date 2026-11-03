# SPDX-License-Identifier: Apache-2.0
"""微信后台刷新/点赞桥；没有截图、界面操作或模拟输入。"""
from pathlib import Path
from ctypes import wintypes
import ctypes,json,subprocess,time,uuid,psutil,threading,hashlib
from app_version import WECHAT_VERSION,WECHAT_DLL_SHA256,NATIVE_DLL

class NativeError(RuntimeError):
    def __init__(self,message,result=None):super().__init__(message);self.result=result or {}
class RefreshUnavailable(NativeError):pass
class Cancelled(Exception):pass

def validate_native_result(request,result,returncode=0):
    if result.get('code')==60 and not result.get('moment_like_enqueued'):raise Cancelled()
    if returncode or result.get('code')!=0:
        if (request.get('mode') in ('refresh','user_page') and result.get('code')==70
            and result.get('moment_like_enqueued') is False and result.get('stage')==13
            and result.get('exception_code')==0 and result.get('refresh_completed') is False):
            transport=result.get('transport_code');api=result.get('api_code')
            transport=transport if type(transport) is int else '未记录';api=api if type(api) is int else '未记录'
            raise RefreshUnavailable(f'朋友圈刷新请求失败（传输码 {transport}，服务码 {api}；70）',result)
        if result.get('code')==78:raise NativeError('微信已返回朋友圈列表，但本地同步失败，已暂停',result)
        raise NativeError(f"微信后台返回 {result.get('code')}，已暂停",result)
    return result

def discover():
    matches=[]
    for process in psutil.process_iter(['name']):
        if (process.info['name'] or '').lower()!='weixin.exe':continue
        try:
            modules=[m for m in process.memory_maps(grouped=False) if m.path.lower().endswith('\\'+WECHAT_VERSION+'\\weixin.dll')]
            if modules:
                try:digest=hashlib.sha256(Path(modules[0].path).read_bytes()).hexdigest()
                except OSError:raise NativeError('无法核对微信程序文件，请确认微信安装完整') from None
                if digest!=WECHAT_DLL_SHA256:raise NativeError('微信文件与已适配版本不一致，停止调用接口；请保留当前版本信息以便适配')
                matches.append((process.pid,process.create_time(),min(int(m.addr.split('-')[0],16) for m in modules)))
        except psutil.Error:continue
    if len(matches)!=1:raise NativeError(f'请登录微信 {WECHAT_VERSION}；暂未找到匹配的微信主进程')
    return matches[0]

class StopEvent:
    def __init__(self):
        self.name='Local\\MomentsStop_'+uuid.uuid4().hex
        self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        self.k.CreateEventW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.BOOL,wintypes.LPCWSTR];self.k.CreateEventW.restype=wintypes.HANDLE
        self.k.SetEvent.argtypes=[wintypes.HANDLE];self.k.CloseHandle.argtypes=[wintypes.HANDLE]
        self.handle=self.k.CreateEventW(None,True,False,self.name)
        if not self.handle:raise NativeError('无法创建暂停信号')
    def stop(self):
        if self.handle:self.k.SetEvent(self.handle)
    def close(self):
        self.stop()
        if self.handle:self.k.CloseHandle(self.handle);self.handle=None

class NativeClient:
    def __init__(self,home,data_home=None):
        self.home=Path(home);self.pid,self.created,self.base=discover();self.active=None;self.lock=threading.Lock();self.models={}
        self.requests=Path(data_home or home)/'data/requests';self.requests.mkdir(parents=True,exist_ok=True)
        self.exe=self.home/'MomentsBridge.exe'
        if not self.exe.is_file() or not (self.home/NATIVE_DLL).is_file():raise NativeError('组件缺失，请完整解压新版程序文件夹')
    def ensure_process(self):
        try:
            if psutil.Process(self.pid).create_time()!=self.created:raise NativeError('微信已重启，请重新开始监控')
        except psutil.Error:raise NativeError('微信已退出，请登录后重新开始') from None
    def cancel(self):
        with self.lock:
            if self.active:self.active.stop()
    def rpc(self,value,alive=lambda:True):
        if not alive():raise Cancelled()
        self.ensure_process();token=uuid.uuid4().hex
        request=self.requests/(token+'.request.json');report=self.requests/(token+'.result.json')
        request.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        process=None
        try:
            process=subprocess.Popen([str(self.exe),'--pid',str(self.pid),'--request',str(request),'--report',str(report)],
                stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
            deadline=time.monotonic()+65
            while process.poll() is None:
                if not alive():self.cancel()
                if time.monotonic()>deadline:raise NativeError('后台任务状态尚未确认，已暂停；不会自动重复点赞')
                time.sleep(.1)
            process.communicate()
            if not report.exists():raise NativeError('后台组件没有返回确认，已暂停')
            result=json.loads(report.read_text(encoding='utf-8'))
            if value.get('mode')=='refresh' and result.get('code')==70:
                self.save_refresh_failure(value,result)
            return validate_native_result(value,result,process.returncode)
        finally:
            # 只清理本次创建的请求；不接触微信目录。
            if process is None or process.poll() is not None:
                request.unlink(missing_ok=True);report.unlink(missing_ok=True)
    def save_refresh_failure(self,request,result):
        # Keep only numeric/boolean diagnostics. Never save models, IDs or text.
        allowed=('code','stage','exception_code','transport_code','api_code','refresh_completed','cache_synced','moment_like_enqueued')
        data={key:result.get(key) if type(result.get(key)) in (int,bool) else None for key in allowed}
        data.update(recorded_at=time.time(),operation='homepage' if request.get('max_id')=='0' else 'pagination_or_recovery')
        path=self.requests.parent/'refresh-error.json';temporary=path.with_suffix('.'+uuid.uuid4().hex+'.partial')
        try:
            temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)
        except OSError:pass
        finally:temporary.unlink(missing_ok=True)
    def check(self,alive=lambda:True):return self.rpc({'mode':'check'},alive)
    def refresh(self,max_id='0',alive=lambda:True):
        result=self.rpc({'mode':'refresh','max_id':str(max_id)},alive)
        return self.accept_page(result)
    def refresh_user(self,username,max_id='0',alive=lambda:True):
        import re
        if not isinstance(username,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,255}',username):raise NativeError('好友标识无效，请重新选择好友')
        result=self.rpc({'mode':'user_page','username':username,'max_id':str(max_id)},alive)
        if str(max_id)=='0' and result.get('refresh_items')==0:
            # The native homepage includes a saved first-page hash. An
            # unchanged success can contain no posts while the album exists.
            # Seek before the unsigned upper bound without that hash; do not
            # erase native settings or mistake cache reuse for the last page.
            self.accept_page(result)
            fresh=self.rpc({'mode':'user_page','username':username,'max_id':str((1<<64)-1)},alive)
            fresh['feed_ids']=list(dict.fromkeys(list(fresh.get('feed_ids',[]))+list(result.get('feed_ids',[]))))
            fresh['feed_models']={**result.get('feed_models',{}),**fresh.get('feed_models',{})}
            result=fresh
        self.accept_page(result)
        cursor=result.get('page_cursor')
        if not isinstance(cursor,str) or not cursor.isdecimal() or not 0<=int(cursor)<1<<64:raise NativeError('个人相册没有返回有效分页位置，已停止扫描')
        return result
    def accept_page(self,result):
        if not result.get('refresh_completed'):raise NativeError('朋友圈刷新尚未确认，已暂停')
        if not result.get('cache_synced'):raise NativeError('朋友圈列表已取得，但本地同步尚未确认，已暂停；请使用完整新版组件')
        if not isinstance(result.get('feed_models'),dict):raise NativeError('后台组件尚未提供可用帖子，请使用完整新版组件')
        if not hasattr(self,'models'):self.models={}
        ids=set(map(str,result.get('feed_ids',[])))
        for feed_id,raw in result['feed_models'].items():
            if feed_id not in ids:continue
            try:model=int(raw,16)
            except (TypeError,ValueError):raise NativeError('后台返回的帖子对象无效，已暂停') from None
            if not 0x10000<=model<0x7fffffffffff or model%8:raise NativeError('后台返回的帖子对象无效，已暂停')
            self.models.pop(feed_id,None);self.models[feed_id]=model
        while len(self.models)>1024:self.models.pop(next(iter(self.models)))
        return result
    def like(self,feed_id,model,alive=lambda:True):
        event=StopEvent()
        with self.lock:self.active=event
        try:
            return self.rpc({'mode':'like','recipient':feed_id,'text':hex(model),'allowlist':[feed_id],'cancel_event':event.name},alive)
        finally:
            with self.lock:self.active=None
            event.close()
    def find_models(self,ids,alive=lambda:True):
        if not alive():raise Cancelled()
        self.ensure_process()
        return {str(value):self.models[str(value)] for value in ids if str(value) in self.models}
