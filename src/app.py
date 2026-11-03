# SPDX-License-Identifier: Apache-2.0
"""朋友圈后台监控助手：打开时保持停止，用户点击开始后才自动点赞。"""
from pathlib import Path
from datetime import datetime, timezone, timedelta
import argparse, ctypes, json, queue, sys, threading, time, tkinter as tk
from dataclasses import asdict
from collections import deque
from tkinter import messagebox
from engine import Config, Engine
from ledger import Ledger
from local_reader import LocalReader
from native_client import NativeClient
from upgrade import restore_previous
from app_version import VERSION
from ui_layout import DashboardUI
from album_ui import AlbumUI
HOME = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))

class Application(DashboardUI, AlbumUI):

    def __init__(self, root, home=HOME, data_home=None):
        self.root = root
        self.binary_home = Path(home)
        self.home = Path(data_home or home)
        (self.home / 'data').mkdir(parents=True, exist_ok=True)
        self.management = None
        restored = restore_previous(self.home) if data_home is None else False
        self.ledger = Ledger(self.home / 'data/state.sqlite3')
        self.events = queue.Queue()
        self.worker = None
        self.engine = None
        self.closing = False
        self.interval = tk.StringVar(value='60')
        self.auto = tk.BooleanVar(value=True)
        self.account = tk.StringVar()
        self.delay_min = tk.StringVar(value='1')
        self.delay_max = tk.StringVar(value='120')
        self.backfill = tk.BooleanVar(value=True)
        self.backfill_hours = tk.StringVar(value='24')
        self.status = tk.StringVar(value='已停止 · 点击“开始监控”后才会自动点赞')
        self.found = tk.StringVar(value='0')
        self.likes = tk.StringVar(value='0')
        self.waiting = tk.StringVar(value='0')
        self.total = tk.StringVar(value='0')
        self.today = tk.StringVar(value='0')
        self.last_like = tk.StringVar(value='等待下一次点赞 · 确认成功后会显示在这里')
        self.like_events = set()
        self.like_order = deque()
        self.view_account = None
        self.dashboard_due = 0
        self.last_check = tk.StringVar(value='尚未检查')
        self.native_test = False
        self.update_status = tk.StringVar(value='版本 ' + VERSION + '')
        self.init_dashboard()
        self.refresh_accounts()
        self.load()
        self.update_rules()
        self.refresh_history()
        self.log('程序已就绪；点击“开始监控”后，按界面设置补查并监控。当前不会点赞。')
        if restored:
            self.log('已保留旧版设置、处理记录和计划点赞时间。')
        root.bind('<F8>', lambda event: self.pause())
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(150, self.tick)

    def refresh_accounts(self):
        try:
            values = [item['account'] for item in LocalReader.accounts()]
        except Exception:
            values = []
        self.account_box.configure(values=values)
        if len(values) == 1:
            self.account.set(values[0])

    def load(self):
        path = self.home / 'data/settings.json'
        if not path.exists():
            return
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            self.interval.set(str(value.get('interval', 60)))
            self.auto.set(bool(value.get('auto_like', True)))
            self.delay_min.set(str(value.get('delay_min_minutes', 1)))
            self.delay_max.set(str(value.get('delay_max_minutes', 120)))
            self.animations.set(bool(value.get('animations', True)))
            hours = value.get('backfill_hours', 24)
            self.backfill.set(bool(hours))
            self.backfill_hours.set(str(hours or 24))
            self.excluded.insert('1.0', '\n'.join(value.get('excluded', [])))
            if value.get('account') in self.account_box.cget('values'):
                self.account.set(value['account'])
        except Exception:
            self.log('保存的设置无法读取，请重新填写；历史记录保留。')

    def config(self):
        try:
            interval = int(self.interval.get())
        except ValueError:
            raise ValueError('检查间隔请填写整数秒数') from None
        try:
            minimum = int(self.delay_min.get())
            maximum = int(self.delay_max.get())
        except ValueError:
            raise ValueError('最短等待和最长等待请填写整数分钟') from None
        hours = 0
        if self.backfill.get():
            try:
                hours = int(self.backfill_hours.get())
            except ValueError:
                raise ValueError('启动补查请填写整数小时数') from None
            if not 1 <= hours <= 168:
                raise ValueError('启动补查请填写 1～168 小时')
        value = Config(self.account.get().strip(), interval, self.auto.get(), tuple((v.strip() for v in self.excluded.get('1.0', 'end').splitlines() if v.strip())), minimum, maximum, hours)
        value.validate()
        if not value.account:
            raise ValueError('请选择当前登录的微信账号')
        return value

    def save(self):
        try:
            self.store_settings(self.config())
            self.update_rules()
            self.settings_feedback.set('已保存 · 下次开始时生效')
            self.log('设置已保存，下次开始监控时用于新计划；已有计划时间不变。')
        except Exception as exc:
            messagebox.showerror('无法保存', str(exc), parent=self.root)

    def store_settings(self, config):
        value = asdict(config)
        value['excluded'] = list(config.excluded)
        value['animations'] = self.animations.get()
        path = self.home / 'data/settings.json'
        temp = path.with_suffix('.partial')
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def set_busy(self, value):
        self.album_busy(value)
        for button in (self.start_button, self.save_button, self.connect_button):
            button.configure(state='disabled' if value else 'normal')
        self.pause_button.configure(state='normal' if value and (not self.native_test) else 'disabled')
        self.account_box.configure(state='disabled' if value else 'readonly')
        self.interval_box.configure(state='disabled' if value else 'normal')
        self.auto_box.configure(state='disabled' if value else 'normal')
        self.delay_min_box.configure(state='disabled' if value else 'normal')
        self.delay_max_box.configure(state='disabled' if value else 'normal')
        self.backfill_box.configure(state='disabled' if value else 'normal')
        self.backfill_hours_box.configure(state='disabled' if value else 'normal')
        self.excluded.configure(state='disabled' if value else 'normal')

    def start(self):
        if self.busy() or self.closing:
            return
        try:
            config = self.config()
            self.store_settings(config)
        except (ValueError, OSError) as exc:
            messagebox.showerror('请检查设置', str(exc), parent=self.root)
            self.tabs.select(self.settings_tab)
            return
        self.pause_requested = False
        self.native_test = False
        self.engine = Engine(self.binary_home, config, self.ledger, self.events.put, reader_home=self.home)
        self.found.set('0')
        self.likes.set('0')
        self.session.set('本次发现 0 条\u3000·\u3000本次点赞 0 条')
        self.update_rules()
        self.set_busy(True)
        self.set_phase('connecting', '正在连接微信…')
        self.worker = threading.Thread(target=self.engine.run, name='MomentsMonitor', daemon=False)
        self.worker.start()
        self.album_busy(True)

    def pause(self):
        self.pause_requested = True
        self.album_queued_start = False
        self.album_pending = None
        if self.engine and self.busy():
            self.pause_requested = True
            self.engine.cancel()
            self.set_phase('pausing', '正在完成当前任务…')
            self.log('已请求暂停，不再提交新的点赞。')
            self.album_busy(True)

    def check_connection(self):
        if self.busy():
            return
        self.native_test = True
        self.pause_requested = False
        self.set_busy(True)
        self.set_phase('checking', '正在检查连接…')

        def check():
            try:
                client = NativeClient(self.binary_home, self.home)
                client.check()
                value = client.refresh()
                self.events.put({'kind': 'log', 'text': f'后台连接成功；本次刷新取得 {value.get('refresh_items', 0)} 条动态，本机记录已同步。未执行点赞。'})
            except Exception as exc:
                self.events.put({'kind': 'error', 'text': str(exc)})
            finally:
                self.events.put({'kind': 'stopped'})
        self.worker = threading.Thread(target=check, name='MomentsCheck', daemon=False)
        self.worker.start()

    def log(self, text):
        line = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S') + '  ' + str(text)
        self.output.configure(state='normal')
        self.output.insert('end', line + '\n')
        self.output.see('end')
        if int(self.output.index('end-1c').split('.')[0]) > 1000:
            self.output.delete('1.0', '100.0')
        self.output.configure(state='disabled')
        try:
            path = self.home / 'data/events.log'
            if path.exists() and path.stat().st_size > 1024 * 1024:
                path.replace(self.home / 'data/events.previous.log')
            with path.open('a', encoding='utf-8') as file:
                file.write(line + '\n')
        except OSError:
            pass

    def close(self):
        if self.closing:
            return False
        self.closing = True
        self.effects.stop()
        if self.contacts_task:self.contacts_task.cancel()
        if self.busy() or self.contacts_busy():
            self.pause()
            self.set_phase('pausing', '正在核对当前任务，然后关闭程序。')
        else:
            self.ledger.close()
            self.root.destroy()
            return True
        return False

def main():
    parser = argparse.ArgumentParser(description='WeChat Moments Monitor — standalone Windows application')
    parser.add_argument('--self-check', action='store_true', help='check packaged components without opening WeChat or starting monitoring')
    args = parser.parse_args()
    from app_version import NATIVE_DLL, WECHAT_VERSION
    binary_home = HOME if getattr(sys, 'frozen', False) else HOME / 'native'
    if args.self_check:
        files = ('MomentsBridge.exe', NATIVE_DLL)
        return 0 if all(((binary_home / name).is_file() for name in files)) else 2
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    handle = kernel.CreateMutexW(None, False, 'Local\\WeChatMomentsMonitorV1')
    already = ctypes.get_last_error() == 183
    root = tk.Tk()
    if already:
        root.withdraw()
        messagebox.showinfo('程序已经打开', '朋友圈监控助手已经运行，请使用已打开的窗口。', parent=root)
        root.destroy()
        return 0
    try:
        Application(root, binary_home, data_home=None if getattr(sys, 'frozen', False) else HOME)
        root.mainloop()
    finally:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
