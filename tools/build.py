"""Portable Windows build. Never copies runtime data into the output."""
from pathlib import Path
import importlib.metadata
import os
import shutil
import struct
import subprocess
import sys

ROOT=Path(__file__).resolve().parent.parent
NATIVE=ROOT/'native'
DIST=ROOT/'dist/MomentsMonitor'

def find_vcvars():
    override=os.environ.get('VCVARS64')
    if override and Path(override).is_file():return Path(override)
    installer=Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
    if installer.is_file():
        result=subprocess.run([str(installer),'-latest','-products','*','-requires','Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
            '-property','installationPath'],check=True,capture_output=True,text=True)
        folder=result.stdout.strip()
        if folder:
            path=Path(folder)/'VC/Auxiliary/Build/vcvars64.bat'
            if path.is_file():return path
    raise RuntimeError('Install Visual Studio C++ Build Tools, or set VCVARS64 to vcvars64.bat.')

def copy_dependency_licenses():
    target=DIST/'THIRD_PARTY_LICENSES';target.mkdir(exist_ok=True)
    for package in ('cryptography','psutil','pyinstaller'):
        distribution=importlib.metadata.distribution(package)
        for item in distribution.files or ():
            name=str(item).replace('\\','/')
            if ('license' in name.lower() or 'copying' in name.lower()) and '.dist-info/' in name:
                source=Path(distribution.locate_file(item))
                if source.is_file():
                    destination=target/package/Path(name.split('.dist-info/',1)[1])
                    destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)
    python_license=Path(sys.base_prefix)/'LICENSE.txt'
    if python_license.exists():shutil.copy2(python_license,target/'Python-LICENSE.txt')

def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    if sys.platform!='win32':raise RuntimeError('Build on 64-bit Windows.')
    if struct.calcsize('P')!=8 or sys.version_info<(3,12):raise RuntimeError('Use 64-bit Python 3.12 or newer.')
    for process in __import__('psutil').process_iter(['name']):
        if (process.info['name'] or '').lower()!='weixin.exe':continue
        try:
            if any(Path(m.path).resolve() in {(NATIVE/'MomentsNativeV10.dll').resolve(),(DIST/'MomentsNativeV10.dll').resolve()} for m in process.memory_maps()):raise RuntimeError('The component is loaded in WeChat. Exit WeChat before rebuilding it.')
        except __import__('psutil').Error:pass
    if (DIST/'data').exists():raise RuntimeError('dist/MomentsMonitor contains runtime data; refusing to overwrite it.')
    vcvars=find_vcvars()
    command=NATIVE/'build.cmd'
    command.write_text('@echo off\nsetlocal\ncall "'+str(vcvars)+'" >nul\nif errorlevel 1 exit /b 1\ncd /d "%~dp0"\n'
        'cl /nologo /utf-8 /std:c++17 /EHsc /O2 /DNDEBUG /MT /LD native.cpp /Fe:MomentsNativeV10.dll /link user32.lib version.lib\n'
        'if errorlevel 1 exit /b 1\n'
        'cl /nologo /utf-8 /std:c++17 /EHsc /O2 /DNDEBUG /MT native_launcher.cpp /Fe:MomentsBridge.exe /link advapi32.lib\n'
        'if errorlevel 1 exit /b 1\n',encoding='utf-8')
    subprocess.run([str(command)],check=True)
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--windowed',
        '--name','朋友圈后台监控助手','--distpath',str(ROOT/'build/pyinstaller-dist'),'--workpath',str(ROOT/'build/pyinstaller-work'),
        '--specpath',str(ROOT/'build'),'--paths',str(ROOT/'src'),'--exclude-module','numpy','--exclude-module','pandas',
        '--exclude-module','matplotlib','--exclude-module','torch',str(ROOT/'src/app.py')],check=True)
    shutil.copytree(ROOT/'build/pyinstaller-dist/朋友圈后台监控助手',DIST,dirs_exist_ok=True)
    for name in ('MomentsBridge.exe','MomentsNativeV10.dll'):shutil.copy2(NATIVE/name,DIST/name)
    for name in ('LICENSE','NOTICE','README.md'):shutil.copy2(ROOT/name,DIST/name)
    shutil.copytree(ROOT/'LICENSES',DIST/'LICENSES',dirs_exist_ok=True)
    shutil.copytree(ROOT/'docs',DIST/'docs',dirs_exist_ok=True)
    shutil.copy2(ROOT/'docs/使用说明.md',DIST/'使用说明.md')
    copy_dependency_licenses()
    print('Built:',DIST/'朋友圈后台监控助手.exe')

if __name__=='__main__':main()
