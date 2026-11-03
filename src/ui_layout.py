# SPDX-License-Identifier: Apache-2.0
"""Task-focused desktop layout and presentation. No WeChat operations here."""
import queue, time, tkinter as tk
from datetime import datetime, timedelta
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from ui_effects import LikeEffects
from ui_model import JST, GROUPS, PHASES, timestamp, task_state, task_next
from app_version import VERSION
BG = '#f6f7f6'
INK = '#243c32'
MUTED = '#617469'
GREEN = '#197653'

class DashboardUI:

    def init_dashboard(self):
        self.ui_state = 'stopped'
        self.pause_requested = False
        self.last_success = None
        self.history_rows = {}
        self.overview_rows = {}
        self.diagnostic_open = False
        for name, value in dict(state_title='已停止', scope='全部账号 · 日本时间', rules='', session='本次发现 0 条\u3000·\u3000本次点赞 0 条', retry_count='0', attention='0', alert_text='', last_like_note='', overview_group='等待点赞', record_group='全部记录', search='', date_filter='全部日期', record_hint='', queue_hint='', settings_feedback='').items():
            setattr(self, name, tk.StringVar(value=value))
        self.animations = tk.BooleanVar(value=True)
        self.last_check.set('上次检查：—')
        self.last_like.set('暂无点赞记录')
        self.status.set('')
        self.build_ui()

    def build_ui(self):
        root = self.root
        root.title('朋友圈助手 ' + VERSION)
        root.geometry('1200x860')
        root.minsize(1040, 820)
        root.configure(bg=BG)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=INK, font=('Microsoft YaHei UI', 10))
        style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=(13, 8), background='#ffffff', foreground=INK)
        style.map('TButton', background=[('active', '#e6eee9')], foreground=[('disabled', '#8a9b90')])
        style.configure('Primary.TButton', background=GREEN, foreground='#ffffff')
        style.map('Primary.TButton', background=[('disabled', '#b5c6bc'), ('active', '#125e42')], foreground=[('disabled', '#ffffff')])
        for item in ('TCheckbutton', 'TRadiobutton'):
            style.configure(item, background=BG, font=('Microsoft YaHei UI', 10))
        style.configure('TNotebook', background=BG, borderwidth=0)
        style.configure('TNotebook.Tab', font=('Microsoft YaHei UI', 11), padding=(26, 10), background='#e6ece8')
        style.map('TNotebook.Tab', background=[('selected', '#ffffff')], foreground=[('selected', GREEN)])
        style.configure('Treeview', font=('Microsoft YaHei UI', 10), rowheight=34, background='#ffffff', fieldbackground='#ffffff', borderwidth=0)
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'), background='#e6ece8', foreground='#506558', padding=(8, 9))
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        header = ttk.Frame(frame)
        header.pack(fill='x', pady=(0, 8))
        ttk.Label(header, text='朋友圈助手', font=('Microsoft YaHei UI', 16, 'bold')).pack(side='left')
        ttk.Label(header, text='v' + VERSION, foreground=MUTED).pack(side='right')
        card = tk.Frame(frame, bg='#ffffff', highlightbackground='#dce5df', highlightthickness=1)
        card.pack(fill='x')
        self.status_bar = tk.Frame(card, bg='#607168', width=5)
        self.status_bar.pack(side='left', fill='y')
        controls = tk.Frame(card, bg='#ffffff')
        controls.pack(side='right', padx=16, pady=12)
        self.start_button = ttk.Button(controls, text='开始监控', command=self.start, style='Primary.TButton')
        self.start_button.pack(side='left')
        self.pause_button = ttk.Button(controls, text='暂停（F8）', command=self.pause, state='disabled')
        self.pause_button.pack(side='left', padx=(8, 0))
        state = tk.Frame(card, bg='#ffffff')
        state.pack(side='left', fill='both', expand=True, padx=18, pady=8)
        self.state_label = tk.Label(state, textvariable=self.state_title, font=('Microsoft YaHei UI', 14, 'bold'), bg='#ffffff', fg='#607168', anchor='w')
        self.state_label.pack(fill='x')
        tk.Label(state, textvariable=self.status, font=('Microsoft YaHei UI', 10), bg='#ffffff', fg=INK, anchor='w', justify='left', wraplength=600).pack(fill='x', pady=(4, 5))
        tk.Label(state, textvariable=self.last_check, font=('Microsoft YaHei UI', 9), bg='#ffffff', fg=MUTED, anchor='w').pack(fill='x')
        ttk.Label(frame, textvariable=self.rules, foreground=MUTED).pack(anchor='w', pady=(6, 7))
        self.alert_box = tk.Frame(frame, bg='#fff0d7')
        tk.Label(self.alert_box, textvariable=self.alert_text, font=('Microsoft YaHei UI', 10), bg='#fff0d7', fg='#81521c', justify='left', anchor='w', wraplength=720).pack(side='left', fill='x', expand=True, padx=14, pady=10)
        self.alert_button = ttk.Button(self.alert_box, text='查看任务', command=self.open_attention)
        self.alert_button.pack(side='right', padx=12, pady=6)
        tabs = ttk.Notebook(frame)
        tabs.pack(fill='both', expand=True)
        overview = ttk.Frame(tabs, padding=12)
        history = ttk.Frame(tabs, padding=12)
        settings = ttk.Frame(tabs)
        album = ttk.Frame(tabs)
        album_canvas = tk.Canvas(album, bg=BG, highlightthickness=0)
        album_scroll = ttk.Scrollbar(album, orient='vertical', command=album_canvas.yview)
        album_scroll.pack(side='right', fill='y')
        album_canvas.pack(side='left', fill='both', expand=True)
        album_canvas.configure(yscrollcommand=album_scroll.set)
        album_body = ttk.Frame(album_canvas, padding=12)
        album_window = album_canvas.create_window((0, 0), window=album_body, anchor='nw')
        album_body.bind('<Configure>', lambda event: album_canvas.configure(scrollregion=album_canvas.bbox('all')))
        album_canvas.bind('<Configure>', lambda event: album_canvas.itemconfigure(album_window, width=event.width))
        self.album_canvas = album_canvas
        self.album_tab = album
        tabs.add(overview, text='总览')
        tabs.add(history, text='任务记录')
        tabs.add(album, text='指定好友补赞')
        tabs.add(settings, text='设置')
        self.tabs = tabs
        self.overview_tab = overview
        self.history_tab = history
        self.settings_tab = settings
        scope = ttk.Frame(overview)
        scope.pack(fill='x', pady=(0, 7))
        ttk.Label(scope, textvariable=self.scope, foreground=MUTED, font=('Microsoft YaHei UI', 9)).pack(side='left')
        ttk.Label(scope, textvariable=self.session, foreground=MUTED, font=('Microsoft YaHei UI', 9)).pack(side='right')
        cards = ttk.Frame(overview)
        cards.pack(fill='x', pady=(0, 9))
        for i, (title, value, color, group) in enumerate([('今日已点赞', self.today, GREEN, '今日已点赞'), ('累计已点赞', self.total, INK, '已点赞'), ('等待点赞', self.waiting, '#276088', '等待点赞'), ('补赞重试', self.retry_count, '#996018', '补赞重试'), ('需要处理', self.attention, '#b13c35', '需要处理')]):
            cards.columnconfigure(i, weight=1, uniform='cards')
            card = tk.Frame(cards, bg='#ffffff', highlightbackground='#dce5df', highlightthickness=1, cursor='hand2')
            card.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 5, 0 if i == 4 else 5))
            label = tk.Label(card, text=title, bg='#ffffff', fg=MUTED, font=('Microsoft YaHei UI', 10), cursor='hand2')
            label.pack(anchor='w', padx=14, pady=(8, 0))
            number = tk.Label(card, textvariable=value, bg='#ffffff', fg=color, font=('Segoe UI', 24, 'bold'), cursor='hand2')
            number.pack(anchor='w', padx=14, pady=(0, 6))
            for widget in (card, label, number):
                widget.bind('<Button-1>', lambda event, g=group: self.show_records(g))
        success = tk.Frame(overview, bg='#eaf2ed')
        success.pack(fill='x', pady=(0, 9))
        self.effects = LikeEffects(success, compact=True)
        self.effects.pack(side='right', padx=8)
        text = tk.Frame(success, bg='#eaf2ed')
        text.pack(side='left', fill='both', expand=True, padx=18, pady=8)
        tk.Label(text, text='最近点赞', bg='#eaf2ed', fg=MUTED, font=('Microsoft YaHei UI', 9)).pack(anchor='w')
        tk.Label(text, textvariable=self.last_like, bg='#eaf2ed', fg=INK, font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', pady=(3, 1))
        tk.Label(text, textvariable=self.last_like_note, bg='#eaf2ed', fg=MUTED, font=('Microsoft YaHei UI', 9)).pack(anchor='w')
        bar = ttk.Frame(overview)
        bar.pack(fill='x', pady=(0, 7))
        for label in ('等待点赞', '补赞重试', '需要处理'):
            ttk.Radiobutton(bar, text=label, variable=self.overview_group, value=label, command=self.refresh_overview).pack(side='left', padx=(0, 18))
        ttk.Button(bar, text='查看全部任务', command=lambda: self.show_records(self.overview_group.get())).pack(side='right')
        self.upcoming = self.make_table(overview, [('name', '好友', 210), ('status', '当前状态', 290), ('next', '下一步', 180), ('when', '计划 / 重试时间', 170)], height=3)
        self.upcoming.bind('<Double-1>', self.open_queue_record)
        self.upcoming.bind('<Return>', self.open_queue_record)
        ttk.Label(overview, textvariable=self.queue_hint, foreground=MUTED, font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(8, 0))
        self.build_records(history)
        self.build_settings(settings)
        self.build_album(album_body)

    def make_table(self, parent, columns, height=3):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=tuple((c[0] for c in columns)), show='headings', height=height, selectmode='browse')
        for name, title, width in columns:
            tree.heading(name, text=title)
            tree.column(name, width=width, minwidth=90, anchor='w')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        scroll.pack(side='right', fill='y')
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side='left', fill='both', expand=True)
        for tag, color in [('retry', '#87551c'), ('attention', '#a23931'), ('liked', GREEN)]:
            tree.tag_configure(tag, foreground=color)
        return tree

    def build_records(self, parent):
        filters = ttk.Frame(parent)
        filters.pack(fill='x', pady=(0, 10))
        ttk.Label(filters, text='任务').pack(side='left')
        group = ttk.Combobox(filters, textvariable=self.record_group, values=list(GROUPS), state='readonly', width=12)
        group.pack(side='left', padx=(8, 16))
        group.bind('<<ComboboxSelected>>', lambda event: self.refresh_history())
        ttk.Label(filters, text='好友').pack(side='left')
        search = ttk.Entry(filters, textvariable=self.search, width=20)
        search.pack(side='left', padx=(8, 16))
        search.bind('<Return>', lambda event: self.refresh_history())
        period = ttk.Combobox(filters, textvariable=self.date_filter, values=['全部日期', '今天发布', '最近24小时发布', '最近7天发布'], state='readonly', width=18)
        period.pack(side='left')
        period.bind('<<ComboboxSelected>>', lambda event: self.refresh_history())
        ttk.Button(filters, text='查找', command=self.refresh_history).pack(side='left', padx=8)
        ttk.Button(filters, text='重置', command=self.reset_filters).pack(side='right')
        self.history = self.make_table(parent, [('name', '好友', 220), ('status', '当前状态', 300), ('published', '发布时间', 180), ('next', '下一步 / 确认时间', 210)])
        self.history.bind('<<TreeviewSelect>>', self.show_detail)
        ttk.Label(parent, textvariable=self.record_hint, foreground=MUTED, font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(7, 8))
        ttk.Label(parent, text='任务详情', font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w', pady=(0, 5))
        self.detail = ScrolledText(parent, height=4, font=('Microsoft YaHei UI', 10), bg='#ffffff', fg=INK, wrap='word', relief='flat', padx=12, pady=10, state='disabled')
        self.detail.pack(fill='x')

    def build_settings(self, parent):
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        body = ttk.Frame(canvas, padding=20)
        window = canvas.create_window((0, 0), window=body, anchor='nw')
        body.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        self.settings_canvas = canvas
        ttk.Label(body, text='点赞设置', font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w', pady=(0, 10))
        row = ttk.Frame(body)
        row.pack(fill='x', pady=5)
        ttk.Label(row, text='微信账号').pack(side='left')
        self.account_box = ttk.Combobox(row, textvariable=self.account, width=38, state='readonly')
        self.account_box.pack(side='left', padx=12)
        self.auto_box = ttk.Checkbutton(row, text='自动点赞（取消后仅监控）', variable=self.auto)
        self.auto_box.pack(side='left')
        row = ttk.Frame(body)
        row.pack(fill='x', pady=7)
        ttk.Label(row, text='发现后随机等待').pack(side='left')
        self.delay_min_box = ttk.Spinbox(row, from_=1, to=1440, textvariable=self.delay_min, width=7)
        self.delay_min_box.pack(side='left', padx=(12, 6))
        ttk.Label(row, text='至').pack(side='left')
        self.delay_max_box = ttk.Spinbox(row, from_=1, to=1440, textvariable=self.delay_max, width=7)
        self.delay_max_box.pack(side='left', padx=6)
        ttk.Label(row, text='分钟后点赞').pack(side='left')
        row = ttk.Frame(body)
        row.pack(fill='x', pady=7)
        self.backfill_box = ttk.Checkbutton(row, text='每次开始时补查最近', variable=self.backfill)
        self.backfill_box.pack(side='left')
        self.backfill_hours_box = ttk.Spinbox(row, from_=1, to=168, textvariable=self.backfill_hours, width=7)
        self.backfill_hours_box.pack(side='left', padx=8)
        ttk.Label(row, text='小时的漏赞').pack(side='left')
        ttk.Label(body, text='等待时间仅用于新计划，已有计划不变。', foreground=MUTED).pack(anchor='w', pady=(3, 12))
        ttk.Label(body, text='不点赞名单', font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(body, text='每行一个备注名或昵称；同名好友一并排除。', foreground=MUTED).pack(anchor='w', pady=(3, 6))
        self.excluded = ScrolledText(body, height=3, font=('Microsoft YaHei UI', 10), relief='solid', borderwidth=1, wrap='word')
        self.excluded.pack(fill='x')
        row = ttk.Frame(body)
        row.pack(fill='x', pady=12)
        ttk.Checkbutton(row, text='点赞成功时播放动画', variable=self.animations, command=self.animation_changed).pack(side='left')
        ttk.Button(row, text='预览动效', command=self.preview_effect).pack(side='left', padx=12)
        self.save_button = ttk.Button(row, text='保存设置', command=self.save, style='Primary.TButton')
        self.save_button.pack(side='right')
        ttk.Label(body, textvariable=self.settings_feedback, foreground=MUTED).pack(anchor='w')
        ttk.Separator(body).pack(fill='x', pady=15)
        ttk.Button(body, text='高级设置', command=self.toggle_diagnostics).pack(anchor='w')
        self.diagnostic_frame = ttk.Frame(body)
        row = ttk.Frame(self.diagnostic_frame)
        row.pack(fill='x', pady=10)
        ttk.Label(row, text='检查间隔').pack(side='left')
        self.interval_box = ttk.Spinbox(row, from_=30, to=3600, textvariable=self.interval, width=7)
        self.interval_box.pack(side='left', padx=8)
        ttk.Label(row, text='秒').pack(side='left')
        self.connect_button = ttk.Button(row, text='检查连接', command=self.check_connection)
        self.connect_button.pack(side='left', padx=20)
        ttk.Label(self.diagnostic_frame, textvariable=self.update_status, foreground=MUTED, wraplength=900).pack(anchor='w', pady=(0, 10))
        ttk.Label(self.diagnostic_frame, text='运行日志', font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w')
        self.output = ScrolledText(self.diagnostic_frame, height=10, font=('Microsoft YaHei UI', 9), bg='#ffffff', fg=INK, relief='flat', state='disabled', wrap='word', padx=10, pady=10)
        self.output.pack(fill='x', pady=(6, 0))

    def set_phase(self, phase, text):
        self.ui_state = phase
        title, color = PHASES.get(phase, PHASES['stopped'])
        self.state_title.set(title)
        self.status.set(text)
        self.state_label.configure(fg=color)
        self.status_bar.configure(bg=color)
        self.update_alert()

    def update_rules(self):
        mode = '自动点赞' if self.auto.get() else '仅监控，不点赞'
        backfill = f'启动补查 {self.backfill_hours.get()} 小时' if self.backfill.get() else '不补查历史动态'
        self.rules.set(f'{mode}\u3000·\u3000随机等待 {self.delay_min.get()}～{self.delay_max.get()} 分钟\u3000·\u3000{backfill}')

    def update_alert(self):
        count = int(self.attention.get())
        if self.ui_state == 'error':
            self.alert_text.set('运行已暂停，详情见日志。')
            self.alert_button.configure(text='查看运行记录', command=self.open_diagnostics)
        elif count:
            self.alert_text.set(f'{count} 条点赞结果待核对')
            self.alert_button.configure(text='查看任务', command=self.open_attention)
        else:
            self.alert_box.pack_forget()
            return
        self.alert_box.pack(fill='x', pady=(0, 10), before=self.tabs)

    def animation_changed(self):
        if not self.animations.get():
            self.effects.stop()
            self.effects.idle()

    def preview_effect(self):
        self.tabs.select(self.overview_tab)
        self.root.after_idle(lambda: self.effects.play(preview=True) if not self.closing else None)

    def set_account_scope(self, account):
        if not account:
            return
        if self.view_account != account:
            self.last_success = None
            self.last_check.set('上次检查：—')
            self.last_like.set('暂无点赞记录')
            self.last_like_note.set('')
        self.view_account = account
        self.scope.set('当前账号 · 日本时间')

    def confirmed_like(self, value):
        key = (value.get('account'), value['id'])
        if key in self.like_events:
            return
        self.like_events.add(key)
        self.like_order.append(key)
        if len(self.like_order) > 1024:
            self.like_events.discard(self.like_order.popleft())
        self.set_account_scope(value.get('account'))
        self.last_like.set(f'{value['label']}\u3000+1')
        self.last_like_note.set(timestamp(value['confirmed_at'], full=True) + '')
        self.refresh_overview()
        if not self.closing and self.animations.get():
            self.effects.play(value['label'])

    def is_monitoring(self):
        return self.busy() and (not self.pause_requested) and (not self.native_test) and (self.ui_state not in ('error', 'pausing', 'stopped'))

    def refresh_overview(self):
        now = time.time()
        day = datetime.fromtimestamp(now, JST).replace(hour=0, minute=0, second=0, microsecond=0)
        if self.last_success is not None:
            self.last_check.set('上次检查：' + timestamp(self.last_success, now))
        totals = self.ledger.like_totals(day.timestamp(), (day + timedelta(days=1)).timestamp(), self.view_account)
        self.total.set(f'{totals['total']:,}')
        self.today.set(f'{totals['today']:,}')
        counts = self.ledger.task_counts(self.view_account)
        self.waiting.set(str(counts['waiting']))
        self.retry_count.set(str(counts['retry']))
        self.attention.set(str(counts['attention']))
        group = GROUPS[self.overview_group.get()]
        rows = self.ledger.tasks(self.view_account, group, limit=8)
        selected = self.upcoming.selection()
        selected = selected[0] if selected else None
        self.upcoming.delete(*self.upcoming.get_children())
        self.overview_rows = {}
        for row in rows:
            key = row['account'] + ':' + row['id']
            self.overview_rows[key] = row
            when = row.get('retry_at') if row.get('pending_note') else row.get('scheduled_at')
            self.upcoming.insert('', 'end', iid=key, values=(row['label'], task_state(row, now, self.is_monitoring(), not self.auto.get()), task_next(row, now, self.is_monitoring(), not self.auto.get()), timestamp(when, now)), tags=(group,))
        if selected in self.overview_rows:
            self.upcoming.selection_set(selected)
        count = counts[group]
        self.queue_hint.set(f'共 {count} 条 · 显示 {len(rows)} 条' if rows else {'waiting': '暂无等待中的点赞', 'retry': '暂无重试任务', 'attention': '暂无待处理任务'}[group])
        self.update_alert()
        self.dashboard_due = time.monotonic() + 15

    def since_filter(self):
        now = datetime.now(JST)
        choice = self.date_filter.get()
        if choice == '今天发布':
            return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return (now - timedelta(hours=24 if choice == '最近24小时发布' else 168)).timestamp() if choice != '全部日期' else None

    def refresh_history(self):
        selected = self.history.selection()
        selected = selected[0] if selected else None
        now = time.time()
        group = GROUPS[self.record_group.get()]
        day = datetime.fromtimestamp(now, JST).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self.ledger.tasks(self.view_account, group, self.search.get().strip(), self.since_filter(), confirmed_since=day.timestamp() if self.record_group.get() == '今日已点赞' else None)
        self.history.delete(*self.history.get_children())
        self.history_rows = {}
        for row in rows:
            key = row['account'] + ':' + row['id']
            self.history_rows[key] = row
            tag = 'retry' if row['status'] == 'pending' and row['pending_note'] else 'attention' if row['status'] in ('submitted', 'uncertain', 'unreadable') else row['status']
            self.history.insert('', 'end', iid=key, values=(row['label'], task_state(row, now, self.is_monitoring(), not self.auto.get()), timestamp(row['created'], now), task_next(row, now, self.is_monitoring(), not self.auto.get())), tags=(tag,))
        self.record_hint.set(f'显示 {len(rows)} 条 · 最多 500 条' if rows else '暂无符合条件的记录')
        if selected in self.history_rows:
            self.history.selection_set(selected)
        self.show_detail()
        self.refresh_overview()

    def show_detail(self, event=None):
        selected = self.history.selection()
        row = self.history_rows.get(selected[0]) if selected else None
        text = '选择一条记录查看详情'
        if row:
            stamp = lambda key: timestamp(row.get(key), full=True)
            text = f'好友：{row['label']}\u3000\u3000状态：{task_state(row, time.time(), self.is_monitoring(), not self.auto.get())}\n发布时间：{stamp('created')}\u3000\u3000首次检测：{stamp('detected_at')}\n原计划点赞：{stamp('scheduled_at')}\u3000\u3000下次重试：{(stamp('retry_at') if row['status'] == 'pending' and row['pending_note'] else '—')}\n成功确认：{stamp('confirmed_at')}\u3000\u3000说明：{(row['pending_note'] if row['status'] == 'pending' and row['pending_note'] else task_next(row, time.time(), self.is_monitoring(), not self.auto.get()))}'
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('end', text)
        self.detail.configure(state='disabled')

    def show_records(self, group='全部记录'):
        self.record_group.set(group)
        self.search.set('')
        self.date_filter.set('全部日期')
        self.tabs.select(self.history_tab)
        self.refresh_history()

    def reset_filters(self):
        self.show_records('全部记录')

    def open_attention(self):
        self.show_records('需要处理')

    def open_queue_record(self, event=None):
        selection = self.upcoming.selection()
        if not selection:
            return
        key = selection[0]
        if key not in self.overview_rows:
            return
        self.show_records(self.overview_group.get())
        if key in self.history_rows:
            self.history.selection_set(key)
            self.history.see(key)
            self.show_detail()

    def toggle_diagnostics(self):
        self.diagnostic_open = not self.diagnostic_open
        if self.diagnostic_open:
            self.diagnostic_frame.pack(fill='x', pady=(4, 0))
        else:
            self.diagnostic_frame.pack_forget()

    def open_diagnostics(self):
        self.tabs.select(self.settings_tab)
        if not self.diagnostic_open:
            self.toggle_diagnostics()
        self.root.after_idle(lambda: (self.settings_canvas.yview_moveto(1), self.output.see('end')))

    def handle_event(self, value):
        if self.album_event(value):
            return
        kind = value.get('kind')
        if kind == 'log':
            self.log(value['text'])
        elif kind == 'liked':
            self.confirmed_like(value)
        elif kind == 'phase':
            self.set_account_scope(value.get('account'))
            if not self.pause_requested and (not self.closing):
                self.set_phase(value['phase'], value['text'])
        elif kind == 'checked':
            self.set_account_scope(value.get('account'))
            self.last_success = value['at']
            self.last_check.set('上次检查：' + timestamp(value['at']))
        elif kind == 'stats':
            self.set_account_scope(value.get('account'))
            self.found.set(str(value['discovered']))
            self.likes.set(str(value['liked']))
            self.session.set(f'本次发现 {value['discovered']} 条\u3000·\u3000本次点赞 {value['liked']} 条')
            self.refresh_history()
            if self.album_batch and self.album_batch['state'] == 'queued':
                self.album_render(self.album_batch)
        elif kind == 'error':
            self.set_phase('error', value['text'])
            self.log('提示：' + value['text'])
        elif kind == 'stopped':
            self.set_busy(False)
            self.native_test = False
            if self.ui_state != 'error':
                self.set_phase('stopped', self.album_finished_text or '')
            self.refresh_history()
            if self.album_batch and self.album_batch['state'] == 'queued':
                self.album_render(self.album_batch)
            if self.album_queued_start:
                self.root.after(100, self.album_start_after_queue)

    def tick(self):
        try:
            while True:
                self.handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        if self.closing and (not self.busy()) and (not self.contacts_busy()):
            self.ledger.close()
            self.root.destroy()
            return
        if not self.closing and time.monotonic() >= self.dashboard_due:
            if self.tabs.select() == str(self.history_tab):
                self.refresh_history()
            else:
                self.refresh_overview()
        self.root.after(150, self.tick)
