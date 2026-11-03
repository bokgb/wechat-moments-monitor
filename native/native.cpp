// SPDX-License-Identifier: Apache-2.0
// 独立的一次性朋友圈动态点赞验证组件。没有界面、鼠标、键盘或剪贴板操作。
#define WIN32_LEAN_AND_MEAN
#include "native_protocol.h"
#include "moments_signatures.h"
#include <winver.h>
#include <cstring>
#include <cstdlib>
#include <cstdio>
#include <cwchar>
#include <functional>

static bool readable(const void* p,size_t n) {
    MEMORY_BASIC_INFORMATION m{};
    auto start=reinterpret_cast<uintptr_t>(p);
    return start+n>=start&&VirtualQuery(p,&m,sizeof(m))&&m.State==MEM_COMMIT&&!(m.Protect&(PAGE_GUARD|PAGE_NOACCESS))
        &&start+n<=reinterpret_cast<uintptr_t>(m.BaseAddress)+m.RegionSize;
}
static bool version_ok(HMODULE module) {
    wchar_t path[32768]{};GetModuleFileNameW(module,path,32768);DWORD unused{},n=GetFileVersionInfoSizeW(path,&unused);
    if(!n||n>0x1000000)return false;auto data=static_cast<BYTE*>(HeapAlloc(GetProcessHeap(),0,n));if(!data)return false;
    VS_FIXEDFILEINFO* v{};UINT size{};bool ok=GetFileVersionInfoW(path,0,n,data)&&VerQueryValueW(data,L"\\",reinterpret_cast<void**>(&v),&size)
        &&size>=sizeof(*v)&&v->dwFileVersionMS==MAKELONG(2,4)&&v->dwFileVersionLS==MAKELONG(9,0);
    HeapFree(GetProcessHeap(),0,data);return ok;
}
static bool compatible(uintptr_t base) {
    for(const auto& signature:signatures) {
        auto p=reinterpret_cast<const void*>(base+signature.rva);
        if(!readable(p,16)||memcmp(p,signature.bytes,16))return false;
    }
    auto table=reinterpret_cast<uintptr_t*>(base+0x98c2778);
    return readable(table,24)&&table[2]==base+0x415a410;
}
static void release_shared(void* ptr) {
    auto p=static_cast<BYTE*>(ptr);if(!p)return;auto table=*reinterpret_cast<uintptr_t**>(p);
    if(InterlockedDecrement(reinterpret_cast<volatile LONG*>(p+8))==0) {
        reinterpret_cast<void(*)(void*)>(table[0])(ptr);
        if(InterlockedDecrement(reinterpret_cast<volatile LONG*>(p+12))==0)reinterpret_cast<void(*)(void*)>(table[1])(ptr);
    }
}
struct Outcome {DWORD stage{},exception{};bool enqueued{},held{},released{},refreshed{},synced{};int transport{},api{};size_t items{},id_count{};uint64_t page_cursor{};uint64_t ids[64]{},models[64]{};};
struct Pair {void* object;void* control;};
struct HeldPost {uint64_t id{},touched{};Pair post{};};
static SRWLOCK held_lock=SRWLOCK_INIT;
static HeldPost held_posts[1024]{};
// A timeline response owns these shared models only until the response is
// released. Keep a bounded strong reference instead of later scanning freed
// heap memory. All replacements release the previous native shared reference.
static uintptr_t retain_post(uintptr_t base,Pair post) {
    auto object=reinterpret_cast<uintptr_t>(post.object);
    if(!object||reinterpret_cast<uintptr_t>(post.control)!=object-16||!readable(post.control,32)
       ||*reinterpret_cast<uintptr_t*>(post.control)!=base+0x96162b8)return 0;
    auto refs=reinterpret_cast<volatile LONG*>(static_cast<BYTE*>(post.control)+8);
    if(*refs<=0||*refs>=100000)return 0;
    auto id=*reinterpret_cast<uint64_t*>(post.object);if(!id)return 0;
    InterlockedIncrement(refs);
    AcquireSRWLockExclusive(&held_lock);size_t slot=0;
    for(size_t i=0;i<1024;++i){
        if(held_posts[i].id==id||!held_posts[i].id){slot=i;break;}
        if(held_posts[i].touched<held_posts[slot].touched)slot=i;
    }
    void* previous=held_posts[slot].post.control;
    held_posts[slot]={id,GetTickCount64(),post};
    ReleaseSRWLockExclusive(&held_lock);
    release_shared(previous);return object;
}
static Pair acquire_post(uint64_t id,uintptr_t expected) {
    Pair post{};AcquireSRWLockShared(&held_lock);
    for(const auto& entry:held_posts)if(entry.id==id&&reinterpret_cast<uintptr_t>(entry.post.object)==expected){
        post=entry.post;
        InterlockedIncrement(reinterpret_cast<volatile LONG*>(static_cast<BYTE*>(post.control)+8));break;
    }
    ReleaseSRWLockShared(&held_lock);return post;
}
static void free_string(uintptr_t base,BYTE* value) {
    auto capacity=*reinterpret_cast<uint64_t*>(value+24);
    if(capacity<16)return;
    auto pointer=*reinterpret_cast<uintptr_t*>(value);size_t bytes=static_cast<size_t>(capacity+1);
    if(bytes>=0x1000){auto raw=*reinterpret_cast<uintptr_t*>(pointer-8);if(pointer-raw>0x27)return;pointer=raw;bytes+=0x27;}
    reinterpret_cast<void(*)(void*,size_t)>(base+0x7697100)(reinterpret_cast<void*>(pointer),bytes);
}
// Match the native GetNextPageData caller: its response alone does not persist
// SnsTimeLine. Queue the native storage task and await its boolean result before
// reporting a completed refresh. Never write SQLite or fabricate timeline rows.
static bool sync_timeline(uintptr_t base,void* service,void* page,uint64_t max_id) {
    if(!readable(static_cast<BYTE*>(service)+0x428,8)||!readable(page,24))return false;
    auto stores=*reinterpret_cast<void**>(static_cast<BYTE*>(service)+0x428);
    if(!readable(stores,8))return false;
    auto timeline=*reinterpret_cast<void**>(stores);
    if(!readable(timeline,24))return false;
    Pair future{};alignas(16) BYTE empty[32]{},unused[32]{};
    *reinterpret_cast<uint64_t*>(empty+24)=15;
    reinterpret_cast<void*(*)(void*,Pair*,void*,uint64_t,void*,int)>(base+0x58645e0)
        // The storage cursor must match GetNextPageData. Zero means replace
        // the newest range; using zero for an older page removes newer rows.
        (timeline,&future,page,max_id,empty,0);
    if(!future.object)return false;
    bool ok=reinterpret_cast<bool(*)(void*,Pair*)>(base+0x10f840)(unused,&future);
    release_shared(future.control);return ok;
}
static DWORD refresh(uintptr_t base,uint64_t max_id,Outcome& result) {
    __try {
        Pair session{},registry{},service{};alignas(16) BYTE response[0x100]{};
        result.stage=10;
        auto kernel=*reinterpret_cast<void**>(base+0xbcfce30);
        if(!readable(kernel,0x78))return 71;
        auto table=*reinterpret_cast<uintptr_t**>(kernel);
        reinterpret_cast<void*(*)(void*,Pair*)>(table[12])(kernel,&session);
        if(!session.object)return 72;
        reinterpret_cast<void*(*)(void*,Pair*)>(base+0x384730)(session.object,&registry);
        if(!registry.object){release_shared(session.control);return 73;}
        reinterpret_cast<void*(*)(void*,Pair*)>(base+0x10a3ee0)(registry.object,&service);
        if(!service.object){release_shared(registry.control);release_shared(session.control);return 74;}
        result.stage=11;
        reinterpret_cast<void*(*)(void*,void*,uint64_t,uint64_t,bool)>(base+0x3104cd0)(service.object,response,max_id,0,false);
        result.stage=12;result.transport=*reinterpret_cast<int*>(response);result.api=*reinterpret_cast<int*>(response+4);
        result.refreshed=result.transport==0&&result.api==0;
        auto page=*reinterpret_cast<uintptr_t*>(response+0x28);
        if(result.refreshed&&page&&readable(reinterpret_cast<void*>(page),24)){
            auto begin=*reinterpret_cast<uintptr_t*>(page),end=*reinterpret_cast<uintptr_t*>(page+8);
            if(end>=begin&&(end-begin)%16==0&&end-begin<=16000){
                result.items=(end-begin)/16;
                for(auto at=begin;at<end&&result.id_count<64;at+=16){
                    if(!readable(reinterpret_cast<void*>(at),16))break;
                    auto post=*reinterpret_cast<Pair*>(at);
                    if(post.object&&readable(post.object,8)){
                        result.ids[result.id_count]=*reinterpret_cast<uint64_t*>(post.object);
                        result.models[result.id_count]=retain_post(base,post);++result.id_count;
                    }
                }
                result.stage=14;
                result.synced=sync_timeline(base,service.object,reinterpret_cast<void*>(page),max_id);
            }
        }
        release_shared(*reinterpret_cast<void**>(response+0x30));free_string(base,response+8);
        release_shared(service.control);release_shared(registry.control);release_shared(session.control);
        result.released=true;result.stage=13;return !result.refreshed?70:result.synced?0:78;
    }__except(EXCEPTION_EXECUTE_HANDLER){result.exception=GetExceptionCode();return 50;}
}
// Verified against the 4.2.0.9 Album GetNextPageData caller 0x4411155:
// (service, response, max_id, min_id=0, const std::string& user, scene=0).
// Both homepage and album storage call 0x58645e0 with a vector<Pair> as
// argument 3. The album call uses the username key, never the homepage key.
static void borrowed_string(BYTE* value,const char* text) {
    size_t n=strlen(text);memset(value,0,32);
    if(n<16)memcpy(value,text,n+1);else *reinterpret_cast<const char**>(value)=text;
    *reinterpret_cast<size_t*>(value+16)=n;*reinterpret_cast<size_t*>(value+24)=n<16?15:n;
}
static bool equal_string(const BYTE* value,const char* text) {
    if(!readable(value,32))return false;size_t n=*reinterpret_cast<const size_t*>(value+16);
    if(n!=strlen(text))return false;
    const char* data=*reinterpret_cast<const size_t*>(value+24)<16?reinterpret_cast<const char*>(value):*reinterpret_cast<const char* const*>(value);
    return readable(data,n)&&!memcmp(data,text,n);
}
static bool collect_album(uintptr_t base,BYTE* vector,Outcome& result,bool cursor) {
    if(!readable(vector,24))return false;
    auto begin=*reinterpret_cast<uintptr_t*>(vector),end=*reinterpret_cast<uintptr_t*>(vector+8);
    if(end<begin||(end-begin)%16||(end-begin)>64*16||result.id_count+(end-begin)/16>64)return false;
    if(cursor)result.items=(end-begin)/16;
    for(auto at=begin;at<end;at+=16){
        if(!readable(reinterpret_cast<void*>(at),16))return false;
        Pair post=*reinterpret_cast<Pair*>(at);
        if(!readable(post.object,8))return false;
        uint64_t id=*reinterpret_cast<uint64_t*>(post.object);
        auto model=retain_post(base,post);if(!id||!model)return false;
        bool duplicate=false;for(size_t i=0;i<result.id_count;++i)duplicate|=result.ids[i]==id;
        if(!duplicate){result.ids[result.id_count]=id;result.models[result.id_count++]=model;}
        if(cursor&&(!result.page_cursor||id<result.page_cursor))result.page_cursor=id;
    }
    return true;
}
static DWORD refresh_user(uintptr_t base,uint64_t max_id,const char* user,Outcome& result) {
    __try {
        Pair session{},registry{},service{};alignas(16) BYTE response[0x100]{},username[32]{};
        borrowed_string(username,user);result.stage=10;
        auto kernel=*reinterpret_cast<void**>(base+0xbcfce30);
        if(!readable(kernel,0x78))return 71;
        auto table=*reinterpret_cast<uintptr_t**>(kernel);
        reinterpret_cast<void*(*)(void*,Pair*)>(table[12])(kernel,&session);
        if(!session.object)return 72;
        reinterpret_cast<void*(*)(void*,Pair*)>(base+0x384730)(session.object,&registry);
        if(!registry.object){release_shared(session.control);return 73;}
        reinterpret_cast<void*(*)(void*,Pair*)>(base+0x10a3ee0)(registry.object,&service);
        if(!service.object){release_shared(registry.control);release_shared(session.control);return 74;}
        result.stage=11;
        reinterpret_cast<void*(*)(void*,void*,uint64_t,uint64_t,void*,int)>(base+0x3110160)
            (service.object,response,max_id,0,username,0);
        result.stage=12;result.transport=*reinterpret_cast<int*>(response);result.api=*reinterpret_cast<int*>(response+4);
        result.refreshed=result.transport==0&&result.api==0;
        auto page=*reinterpret_cast<BYTE**>(response+0x28);
        bool shape=result.refreshed&&readable(page,0x210)&&equal_string(page+0x80,user);
        if(shape){
            shape=collect_album(base,page,result,true)&&collect_album(base,page+0x1d8,result,false);
            if(shape){
                result.stage=14;
                result.synced=(!result.items&&!max_id)||reinterpret_cast<bool(*)(void*,void*,uint64_t,void*,int)>(base+0x30f85e0)
                    (service.object,page,max_id,username,0);
                // Pinned posts have a separate native storage key, matching
                // the album UI caller. No SQLite writes are performed here.
                if(result.synced&&*reinterpret_cast<uintptr_t*>(page+0x1d8)!=*reinterpret_cast<uintptr_t*>(page+0x1e0)){
                    char top[280]{};strcpy_s(top,user);strcat_s(top,"_user_top");alignas(16) BYTE topkey[32]{};borrowed_string(topkey,top);
                    result.synced=reinterpret_cast<bool(*)(void*,void*,uint64_t,void*,int)>(base+0x30f85e0)
                        (service.object,page+0x1d8,0,topkey,0);
                }
            }
        }
        release_shared(*reinterpret_cast<void**>(response+0x30));free_string(base,response+8);
        release_shared(service.control);release_shared(registry.control);release_shared(session.control);
        result.released=true;result.stage=13;
        return !result.refreshed?70:!shape?79:result.synced?0:78;
    }__except(EXCEPTION_EXECUTE_HANDLER){result.exception=GetExceptionCode();return 50;}
}

struct RefreshWork {uintptr_t base;uint64_t max_id;char user[256];Outcome outcome;DWORD code;HANDLE done;};
static DWORD queue_refresh(uintptr_t base,uint64_t max_id,Outcome& result,const char* user=nullptr) {
    static_assert(sizeof(std::function<void()>)==64,"Native callable layout mismatch");
    auto work=static_cast<RefreshWork*>(HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,sizeof(RefreshWork)));
    if(!work)return 36;work->base=base;work->max_id=max_id;if(user)strcpy_s(work->user,user);work->done=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    if(!work->done){HeapFree(GetProcessHeap(),0,work);return 76;}
    auto kernel=*reinterpret_cast<void**>(base+0xbcfce30);Pair queue{},future{};
    reinterpret_cast<void*(*)(void*,Pair*)>(base+0x47310)(kernel,&queue);
    if(!queue.object){CloseHandle(work->done);HeapFree(GetProcessHeap(),0,work);return 77;}
    std::function<void()> callback=[work](){
        work->code=work->user[0]?refresh_user(work->base,work->max_id,work->user,work->outcome):refresh(work->base,work->max_id,work->outcome);
        SetEvent(work->done);
    };
    alignas(16) BYTE location[64]{};
    reinterpret_cast<void*(*)(void*,const char*,const char*,int)>(base+0x1a3700)(location,"MomentsMonitor","moments_native.cpp",1);
    reinterpret_cast<void*(*)(void*,Pair*,void*,void*,bool)>(base+0x311990)(queue.object,&future,location,&callback,true);
    release_shared(future.control);release_shared(queue.control);
    // DLL 保留加载，超时也不释放仍可能被微信原生任务使用的 work。
    if(WaitForSingleObject(work->done,45000)!=WAIT_OBJECT_0)return 75;
    result=work->outcome;DWORD code=work->code;CloseHandle(work->done);HeapFree(GetProcessHeap(),0,work);return code;
}
struct CapturedPost {uintptr_t unused,object,control;DWORD scene,padding;};
static DWORD perform(uintptr_t base,uint64_t id,uintptr_t object,HANDLE cancel,Outcome& result) {
    __try {
        if(WaitForSingleObject(cancel,0)==WAIT_OBJECT_0)return 60;
        result.stage=1;
        if(object<0x10000||object%8||!readable(reinterpret_cast<void*>(object-16),0x3b0))return 41;
        auto control=object-16;
        if(*reinterpret_cast<uintptr_t*>(control)!=base+0x96162b8||*reinterpret_cast<uint64_t*>(object)!=id)return 42;
        auto refs=reinterpret_cast<volatile LONG*>(control+8);LONG count=*refs;
        while(count>0&&count<100000) {
            LONG actual=InterlockedCompareExchange(refs,count+1,count);
            if(actual==count){result.held=true;break;}count=actual;
        }
        if(!result.held)return 43;
        result.stage=2;
        if(*reinterpret_cast<uintptr_t*>(control)!=base+0x96162b8||*reinterpret_cast<uint64_t*>(object)!=id) {
            release_shared(reinterpret_cast<void*>(control));result.released=true;return 44;
        }
        alignas(16) BYTE params[0xe8]{},cb1[64]{},cb2[64]{},cb3[64]{};uint64_t weak[2]{};
        auto ctor=reinterpret_cast<void*(*)(void*,void*,void*,void*,void*,uint64_t)>(base+0xf910);
        auto sequence=reinterpret_cast<volatile LONG64*>(base+0xb47cea0);
        ctor(params,cb1,cb2,cb3,weak,static_cast<uint64_t>(InterlockedIncrement64(sequence)));
        result.stage=3;
        if(WaitForSingleObject(cancel,0)==WAIT_OBJECT_0) {
            release_shared(*reinterpret_cast<void**>(params+8));release_shared(reinterpret_cast<void*>(control));result.released=true;return 60;
        }
        CapturedPost captured{0,object,control,0,0};
        auto queue_like=reinterpret_cast<void(*)(void*,void*)>(base+0x4159f70);
        // 此入口复制帖子和回调参数，再交给微信原生任务队列；本组件没有异步回调代码。
        result.stage=4;queue_like(&captured,params);result.enqueued=true;result.stage=5;
        release_shared(*reinterpret_cast<void**>(params+8));release_shared(reinterpret_cast<void*>(control));
        result.released=true;result.stage=6;return 0;
    }__except(EXCEPTION_EXECUTE_HANDLER){result.exception=GetExceptionCode();return 50;}
}
static DWORD run(NativeArgs& args) {
    HMODULE module=GetModuleHandleW(L"Weixin.dll");if(!module)return 10;
    auto base=reinterpret_cast<uintptr_t>(module);DWORD code=version_ok(module)&&compatible(base)?0:11;
    Outcome result{};auto foreground=GetForegroundWindow();HANDLE event{},mutex{};bool owned=false;
    if(!code&&(args.flags==REFRESH_TIMELINE||args.flags==REFRESH_USER)) {
        uint64_t max_id{};char* tail{};
        if(args.recipient[0]){max_id=_strtoui64(args.recipient,&tail,10);if(!tail||*tail)return 23;}
        wchar_t name[100]{};swprintf_s(name,L"Local\\WeChatMomentsNative_%lu",GetCurrentProcessId());
        mutex=CreateMutexW(nullptr,FALSE,name);auto wait=mutex?WaitForSingleObject(mutex,0):WAIT_FAILED;
        owned=wait==WAIT_OBJECT_0||wait==WAIT_ABANDONED;
        code=wait==WAIT_OBJECT_0?queue_refresh(base,max_id,result,args.flags==REFRESH_USER?args.text:nullptr):51;
    }else if(!code&&!(args.flags&CHECK_ONLY)) {
        char* tail{};uint64_t id=_strtoui64(args.recipient,&tail,10);
        if(!id||!tail||*tail)return 23;
        uintptr_t object=static_cast<uintptr_t>(_strtoui64(args.text,&tail,16));
        if(!object||!tail||*tail)return 23;
        event=OpenEventW(SYNCHRONIZE,FALSE,args.cancel_event);if(!event)code=61;
        wchar_t name[100]{};swprintf_s(name,L"Local\\WeChatMomentsNative_%lu",GetCurrentProcessId());
        if(!code){mutex=CreateMutexW(nullptr,FALSE,name);auto wait=mutex?WaitForSingleObject(mutex,0):WAIT_FAILED;
            owned=wait==WAIT_OBJECT_0||wait==WAIT_ABANDONED;if(wait!=WAIT_OBJECT_0)code=51;}
        if(!code){
            auto post=acquire_post(id,object);
            if(!post.object)code=42;
            else {code=perform(base,id,object,event,result);release_shared(post.control);}
        }
    }
    if(owned)ReleaseMutex(mutex);if(mutex)CloseHandle(mutex);if(event)CloseHandle(event);
    char ids[1800]{"["};size_t offset=1;
    for(size_t i=0;i<result.id_count;++i)offset+=sprintf_s(ids+offset,sizeof(ids)-offset,"%s\"%llu\"",i?",":"",result.ids[i]);
    sprintf_s(ids+offset,sizeof(ids)-offset,"]");
    char models[6400]{"{"};size_t model_offset=1;bool comma=false;
    for(size_t i=0;i<result.id_count;++i)if(result.models[i]){
        model_offset+=sprintf_s(models+model_offset,sizeof(models)-model_offset,"%s\"%llu\":\"0x%llx\"",comma?",":"",result.ids[i],result.models[i]);comma=true;
    }
    sprintf_s(models+model_offset,sizeof(models)-model_offset,"}");
    char report[16384]{};int length=sprintf_s(report,
        "{\"code\":%lu,\"stage\":%lu,\"exception_code\":%lu,\"moment_like_enqueued\":%s,\"refresh_completed\":%s,\"cache_synced\":%s,\"transport_code\":%d,\"api_code\":%d,\"page_cursor\":\"%llu\",\"refresh_items\":%zu,\"feed_ids\":%s,\"feed_models\":%s,\"model_reference_acquired\":%s,\"references_released\":%s,\"foreground_unchanged\":%s,\"screenshots\":0,\"simulated_input_events\":0}\n",
        code,result.stage,result.exception,result.enqueued?"true":"false",result.refreshed?"true":"false",result.synced?"true":"false",result.transport,result.api,result.page_cursor,result.items,ids,models,result.held?"true":"false",result.released?"true":"false",GetForegroundWindow()==foreground?"true":"false");
    HANDLE file=CreateFileW(args.report,GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
    if(file==INVALID_HANDLE_VALUE)return 13;DWORD written{};
    bool ok=length>0&&WriteFile(file,report,static_cast<DWORD>(length),&written,nullptr)&&written==length;CloseHandle(file);
    return ok?code:14;
}
extern "C" __declspec(dllexport) DWORD WINAPI MomentsNativeCall(void* input) {
    __try {
        if(!readable(input,sizeof(NativeArgs)))return 20;auto args=static_cast<NativeArgs*>(input);
        if(args->protocol!=NATIVE_PROTOCOL||(args->flags!=0&&args->flags!=CHECK_ONLY&&args->flags!=REFRESH_TIMELINE&&args->flags!=REFRESH_USER))return 21;
        if(!wmemchr(args->report,0,768)||!args->report[0]||!wmemchr(args->cancel_event,0,128))return 22;
        if(args->flags==REFRESH_USER){
            if(!memchr(args->text,0,256)||!args->text[0])return 23;
            for(const char* p=args->text;*p;++p)if(!((*p>='a'&&*p<='z')||(*p>='A'&&*p<='Z')||(*p>='0'&&*p<='9')||*p=='_'||*p=='-'))return 23;
        }
        if(!args->flags) {
            if(!memchr(args->recipient,0,256)||!memchr(args->text,0,3072)||!args->recipient[0]||!args->text[0]||args->allow_count<1||args->allow_count>5)return 23;
            bool allowed=false;for(DWORD i=0;i<args->allow_count;++i){if(!memchr(args->allowlist[i],0,256))return 23;allowed|=!strcmp(args->recipient,args->allowlist[i]);}
            if(!allowed)return 25;
            for(const char* p=args->recipient;*p;++p)if(*p<'0'||*p>'9')return 23;
        }
        return run(*args);
    }__except(EXCEPTION_EXECUTE_HANDLER){return 27;}
}
BOOL WINAPI DllMain(HINSTANCE module,DWORD reason,LPVOID){if(reason==DLL_PROCESS_ATTACH)DisableThreadLibraryCalls(module);return TRUE;}
