// SPDX-License-Identifier: Apache-2.0
// 向本人微信短暂加载原生发送组件，完成检查或发送后卸载；没有输入模拟。
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <string>
#include <iostream>
#include <vector>
#include <cwchar>
#include <cstdint>

#include "native_protocol.h"
#include <algorithm>
#include "json.hpp"
#include <fstream>
#include <iterator>
static bool same_user(HANDLE process) {
    HANDLE target{}, self{};
    if (!OpenProcessToken(process,TOKEN_QUERY,&target) || !OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&self)) {
        if (target) CloseHandle(target); if (self) CloseHandle(self); return false;
    }
    DWORD n{},m{}; GetTokenInformation(target,TokenUser,nullptr,0,&n); GetTokenInformation(self,TokenUser,nullptr,0,&m);
    std::vector<BYTE> a(n),b(m);
    bool ok = GetTokenInformation(target,TokenUser,a.data(),n,&n)
        && GetTokenInformation(self,TokenUser,b.data(),m,&m)
        && EqualSid(reinterpret_cast<TOKEN_USER*>(a.data())->User.Sid,reinterpret_cast<TOKEN_USER*>(b.data())->User.Sid);
    CloseHandle(target); CloseHandle(self); return ok;
}
static uintptr_t remote_module(DWORD pid,const std::wstring& path) {
    HANDLE snapshot=CreateToolhelp32Snapshot(TH32CS_SNAPMODULE|TH32CS_SNAPMODULE32,pid);
    if (snapshot==INVALID_HANDLE_VALUE) return 0;
    MODULEENTRY32W entry{}; entry.dwSize=sizeof(entry); uintptr_t found{};
    if (Module32FirstW(snapshot,&entry)) do {
        if (!_wcsicmp(entry.szExePath,path.c_str()) || !_wcsicmp(entry.szModule,path.c_str())) {
            found=reinterpret_cast<uintptr_t>(entry.modBaseAddr);break;
        }
    } while(Module32NextW(snapshot,&entry));
    CloseHandle(snapshot);return found;
}
// 某些 kernel32 导出会转发到 KernelBase；按函数实际所属模块换算远程地址。
static uintptr_t remote_export(DWORD pid,const char* name) {
    auto function=GetProcAddress(GetModuleHandleW(L"kernel32.dll"),name);
    HMODULE owner{};
    if(!function || !GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(function),&owner))return 0;
    wchar_t path[32768]{};
    if(!GetModuleFileNameW(owner,path,32768))return 0;
    std::wstring filename=path;
    auto base=remote_module(pid,filename.substr(filename.find_last_of(L"\\/")+1));
    return base?base+(reinterpret_cast<uintptr_t>(function)-reinterpret_cast<uintptr_t>(owner)):0;
}
static DWORD call(HANDLE process,uintptr_t function,void* parameter) {
    HANDLE thread=CreateRemoteThread(process,nullptr,0,reinterpret_cast<LPTHREAD_START_ROUTINE>(function),parameter,0,nullptr);
    if (!thread) throw GetLastError();
    DWORD wait=WaitForSingleObject(thread,60000);
    if(wait!=WAIT_OBJECT_0){ CloseHandle(thread); throw DWORD(ERROR_TIMEOUT); }
    DWORD result{};GetExitCodeThread(thread,&result);CloseHandle(thread);return result;
}
static DWORD find_wechat() {
    HANDLE snapshot=CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS,0);
    if(snapshot==INVALID_HANDLE_VALUE)return 0;
    PROCESSENTRY32W entry{};entry.dwSize=sizeof(entry);DWORD found{};
    if(Process32FirstW(snapshot,&entry))do {
        if(!_wcsicmp(entry.szExeFile,L"Weixin.exe")&&remote_module(entry.th32ProcessID,L"Weixin.dll")) {
            if(found){found=0;break;}found=entry.th32ProcessID;
        }
    }while(Process32NextW(snapshot,&entry));
    CloseHandle(snapshot);return found;
}
int wmain(int argc,wchar_t** argv) {
    if(argc!=7 || wcscmp(argv[1],L"--pid") || wcscmp(argv[3],L"--request") || wcscmp(argv[5],L"--report")) {
        std::wcerr<<L"NativeBridge.exe --pid PID --request JSON --report JSON\n";return 2;
    }
    wchar_t* tail{};DWORD pid=wcstoul(argv[2],&tail,10);if(!pid||!tail||*tail)return 2;
    NativeArgs args{};args.protocol=NATIVE_PROTOCOL;
    try {
        std::ifstream input(argv[4],std::ios::binary);if(!input)return 12;
        std::string raw((std::istreambuf_iterator<char>(input)),{});if(raw.size()>65536)return 12;
        auto value=nlohmann::json::parse(raw);
        std::string mode=value.at("mode").get<std::string>();if(mode!="check"&&mode!="like"&&mode!="refresh"&&mode!="user_page")return 12;
        args.flags=mode=="check"?CHECK_ONLY:mode=="refresh"?REFRESH_TIMELINE:mode=="user_page"?REFRESH_USER:0;
        if(mode=="refresh"||mode=="user_page"){
            auto max_id=value.value("max_id",std::string("0"));
            if(max_id.empty()||max_id.size()>20||max_id.find_first_not_of("0123456789")!=std::string::npos)return 12;
            memcpy(args.recipient,max_id.c_str(),max_id.size()+1);
        }
        if(mode=="user_page"){
            auto user=value.at("username").get<std::string>();
            if(user.empty()||user.size()>=256||user.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")!=std::string::npos)return 12;
            memcpy(args.text,user.c_str(),user.size()+1);
        }
        if(!args.flags) {
            auto peer=value.at("recipient").get<std::string>(),text=value.at("text").get<std::string>();
            if(peer.empty()||peer.size()>=256||text.empty()||text.size()>=3072||peer.find('\0')!=std::string::npos||text.find('\0')!=std::string::npos)return 12;
            memcpy(args.recipient,peer.c_str(),peer.size()+1);memcpy(args.text,text.c_str(),text.size()+1);
            auto allowed=value.at("allowlist").get<std::vector<std::string>>();if(allowed.empty()||allowed.size()>8)return 12;
            args.allow_count=static_cast<DWORD>(allowed.size());
            for(size_t i=0;i<allowed.size();++i){if(allowed[i].empty()||allowed[i].size()>=256||allowed[i].find('\0')!=std::string::npos)return 12;memcpy(args.allowlist[i],allowed[i].c_str(),allowed[i].size()+1);}
            auto event=value.at("cancel_event").get<std::string>();
            if(event.empty()||event.find('\0')!=std::string::npos||MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,event.c_str(),-1,args.cancel_event,128)==0)return 12;
        }
    }catch(...){std::wcerr<<L"Invalid native request.\n";return 12;}
    wchar_t exe[32768]{};GetModuleFileNameW(nullptr,exe,32768);
    std::wstring dll=exe;dll=dll.substr(0,dll.find_last_of(L"\\/"))+L"\\MomentsNativeV10.dll";
    wchar_t full_report[32768]{};if(!GetFullPathNameW(argv[6],32768,full_report,nullptr)||wcslen(full_report)>=768)return 3;
    if(GetFileAttributesW(full_report)!=INVALID_FILE_ATTRIBUTES){std::wcerr<<L"Report already exists; refusing overwrite.\n";return 4;}
    HANDLE process=OpenProcess(PROCESS_QUERY_INFORMATION|PROCESS_VM_READ|PROCESS_VM_WRITE|PROCESS_VM_OPERATION|PROCESS_CREATE_THREAD,FALSE,pid);
    if(!process){std::wcerr<<L"OpenProcess failed: "<<GetLastError()<<L"\n";return 5;}
    wchar_t image[32768]{};DWORD image_length=32768;
    if(!QueryFullProcessImageNameW(process,0,image,&image_length)||!same_user(process)) {CloseHandle(process);return 6;}
    std::wstring target=image;
    if(_wcsicmp(target.substr(target.find_last_of(L"\\/")+1).c_str(),L"Weixin.exe")||!remote_module(pid,L"Weixin.dll")) {CloseHandle(process);return 7;}
    // 监控组件可复用；它承载微信任务队列中的刷新回调，保持加载直至微信退出。
    HMODULE local=LoadLibraryExW(dll.c_str(),nullptr,DONT_RESOLVE_DLL_REFERENCES);
    if(!local){CloseHandle(process);return 9;}
    auto entry=GetProcAddress(local,"MomentsNativeCall");
    if(!entry){FreeLibrary(local);CloseHandle(process);return 10;}
    uintptr_t rva=reinterpret_cast<uintptr_t>(entry)-reinterpret_cast<uintptr_t>(local);FreeLibrary(local);
    uintptr_t load=remote_export(pid,"LoadLibraryW"),unload=remote_export(pid,"FreeLibrary");
    if(!load||!unload){CloseHandle(process);return 11;}
    void* path=nullptr;void* parameter=nullptr;uintptr_t module{};DWORD code=99;bool timeout=false;
    try {
        SIZE_T bytes=(dll.size()+1)*sizeof(wchar_t),written{};
        path=VirtualAllocEx(process,nullptr,bytes,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
        if(!path||!WriteProcessMemory(process,path,dll.c_str(),bytes,&written)||written!=bytes)throw GetLastError();
        module=remote_module(pid,dll);
        if(!module){call(process,load,path);module=remote_module(pid,dll);}
        if(!module)throw DWORD(ERROR_MOD_NOT_FOUND);
        wcscpy_s(args.report,full_report);
        parameter=VirtualAllocEx(process,nullptr,sizeof(args),MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
        if(!parameter||!WriteProcessMemory(process,parameter,&args,sizeof(args),&written)||written!=sizeof(args))throw GetLastError();
        code=call(process,module+rva,parameter);
    } catch(DWORD error) {
        code=error;timeout=(error==ERROR_TIMEOUT);std::wcerr<<L"Native probe failed: "<<error<<L"\n";
    }
    // 组件保持加载，后台任务可安全执行其回调；没有自动循环，暂停后不会产生新请求。
    if(!timeout) {
        if(parameter)VirtualFreeEx(process,parameter,0,MEM_RELEASE);
        if(path)VirtualFreeEx(process,path,0,MEM_RELEASE);
    }
    CloseHandle(process);
    std::wcout<<L"Probe exit code: "<<code<<L"\n";
    return code?1:0;
}
