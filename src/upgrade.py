# SPDX-License-Identifier: Apache-2.0
"""Import the last version's state once, after the app's instance guard succeeds."""
from pathlib import Path
import shutil,sqlite3
from app_version import VERSION

def restore_previous(home):
    home=Path(home);target=home/'data';destination=target/'state.sqlite3'
    previous=next((home.parent.parent/f'朋友圈后台监控助手1.{version}/MomentsMonitor/data'
        for version in range(int(VERSION.split('.')[1])-1,-1,-1) if (home.parent.parent/f'朋友圈后台监控助手1.{version}/MomentsMonitor/data/state.sqlite3').is_file()),None)
    if previous is None:return False
    source=previous/'state.sqlite3'
    if destination.exists() or not source.is_file():return False
    target.mkdir(parents=True,exist_ok=True)
    temporary=target/'state.importing.sqlite3'
    src=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)
    dst=sqlite3.connect(temporary)
    try:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('旧版记录校验失败，请保留旧程序目录')
    finally:dst.close();src.close()
    temporary.replace(destination)
    for name in ('settings.json','events.log','events.previous.log'):
        old=previous/name;new=target/name
        if old.is_file() and not new.exists():shutil.copy2(old,new)
    return True
