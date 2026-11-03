# SPDX-License-Identifier: Apache-2.0
"""Single-friend historical scan controls; workers never touch Tk widgets."""
import hashlib,threading,tkinter as tk
from datetime import datetime
from tkinter import ttk,messagebox
from album import AlbumTask,ContactsTask,PERIODS,date_range
from engine import Engine
from album_store import AlbumStore
from batch_engine import BatchEngine
from ui_model import JST,timestamp
from ui_model import task_state
import time

class AlbumUI:
    def build_album(self,parent):
        self.album_contacts={};self.album_account=None;self.album_batch=None;self.album_queued_start=False;self.album_finished_text=''
        self.contacts_worker=None;self.contacts_task=None;self.contacts_loading=False;self.album_pending=None
        self.album_search=tk.StringVar();self.album_person=tk.StringVar();self.album_period=tk.StringVar(value='最近1年')
        self.album_start=tk.StringVar(value=datetime.now(JST).strftime('%Y-01-01'));self.album_end=tk.StringVar(value=datetime.now(JST).strftime('%Y-%m-%d'))
        self.album_summary=tk.StringVar(value='选择好友后预览朋友圈')
        self.album_scope=tk.StringVar(value='预览后开始补赞，已点赞的会跳过。')
        ttk.Label(parent,text='指定好友补赞',font=('Microsoft YaHei UI',15,'bold')).pack(anchor='w',pady=(0,5))
        row=ttk.Frame(parent);row.pack(fill='x',pady=(15,8))
        self.album_read=ttk.Button(row,text='读取好友',command=self.album_read_contacts);self.album_read.pack(side='left')
        ttk.Label(row,text='搜索好友').pack(side='left',padx=(15,0))
        ttk.Entry(row,textvariable=self.album_search,width=23).pack(side='left',padx=(15,8))
        self.album_search.trace_add('write',lambda *_:self.album_filter())
        row=ttk.Frame(parent);row.pack(fill='x',pady=7)
        ttk.Label(row,text='好友').pack(side='left');self.album_person_box=ttk.Combobox(row,textvariable=self.album_person,state='readonly',width=53);self.album_person_box.pack(side='left',padx=12)
        ttk.Label(row,text='时间').pack(side='left',padx=(12,0));self.album_period_box=ttk.Combobox(row,textvariable=self.album_period,values=PERIODS,width=14,state='readonly');self.album_period_box.pack(side='left',padx=12)
        row=ttk.Frame(parent);row.pack(fill='x',pady=7)
        ttk.Label(row,text='自定义日期').pack(side='left');self.album_start_box=ttk.Entry(row,textvariable=self.album_start,width=13);self.album_start_box.pack(side='left',padx=12)
        ttk.Label(row,text='至').pack(side='left');self.album_end_box=ttk.Entry(row,textvariable=self.album_end,width=13);self.album_end_box.pack(side='left',padx=12)
        ttk.Label(row,text='YYYY-MM-DD',foreground='#617469').pack(side='left')
        row=ttk.Frame(parent);row.pack(fill='x',pady=(9,14))
        self.album_scan=ttk.Button(row,text='预览朋友圈',command=self.album_scan_new,style='Primary.TButton');self.album_scan.pack(side='left')
        self.album_resume=ttk.Button(row,text='继续预览',command=self.album_scan_resume);self.album_resume.pack(side='left',padx=10)
        self.album_enqueue=ttk.Button(row,text='开始补赞',command=self.album_add);self.album_enqueue.pack(side='left')
        self.album_run=ttk.Button(row,text='继续补赞',command=self.album_continue);self.album_run.pack(side='left',padx=10)
        ttk.Label(parent,textvariable=self.album_scope,foreground='#617469',wraplength=1000).pack(anchor='w',pady=(0,9))
        card=tk.Frame(parent,bg='#ffffff',highlightbackground='#dce5df',highlightthickness=1);card.pack(fill='x')
        tk.Label(card,textvariable=self.album_summary,bg='#ffffff',fg='#243c32',font=('Microsoft YaHei UI',11),justify='left',anchor='w',wraplength=950,padx=16,pady=15).pack(fill='x')
        self.album_table=self.make_table(parent,[('published','发布时间',180),('summary','内容摘要',470),('category','扫描结果',220)],height=5)
        ttk.Label(parent,text='补赞沿用设置中的随机等待时间。',foreground='#617469',justify='left').pack(anchor='w',pady=10)
        self.album_render(AlbumStore(self.ledger).get())
    def album_filter(self):
        word=self.album_search.get().strip().casefold()
        values=[label for label in self.album_contacts if not word or word in label.casefold()]
        self.album_person_box.configure(values=values)
        if self.album_person.get() not in values:self.album_person.set(values[0] if len(values)==1 else '')
    def album_busy(self,value):
        if not hasattr(self,'album_read'):return
        switching=bool(value and type(self.engine) is Engine and not self.pause_requested and not self.native_test and not self.album_pending)
        locked=bool(value and not switching) or bool(self.album_pending)
        self.album_read.configure(state='disabled' if self.contacts_loading or self.closing else 'normal',text='读取中…' if self.contacts_loading else '读取好友')
        for box in (self.album_person_box,self.album_period_box):box.configure(state='disabled' if locked else 'readonly')
        for box in (self.album_start_box,self.album_end_box):box.configure(state='disabled' if locked else 'normal')
        batch=self.album_batch or {}
        available=not locked and not self.closing
        self.album_scan.configure(state='normal' if available else 'disabled',text='暂停监控并预览' if switching else '预览朋友圈')
        self.album_resume.configure(state='normal' if available and batch.get('state') in ('paused','scanning') else 'disabled',text='暂停并继续预览' if switching else '继续预览')
        self.album_enqueue.configure(state='normal' if available and batch.get('state')=='ready' and sum(batch.get('counts',{}).get(k,0) for k in ('candidate','planned')) else 'disabled',text='暂停监控并补赞' if switching else '开始补赞')
        self.album_run.configure(state='normal' if available and batch.get('state')=='queued' else 'disabled',text='暂停并继续补赞' if switching else '继续补赞')
    def album_read_contacts(self):
        if self.closing or self.contacts_loading:return
        self.contacts_loading=True;self.album_scope.set('正在读取好友…');self.album_busy(self.busy())
        self.contacts_task=ContactsTask(self.home,self.account.get().strip(),self.events.put)
        self.contacts_worker=threading.Thread(target=self.contacts_task.run,name='MomentsContacts',daemon=False)
        self.contacts_worker.start()
    def contacts_busy(self):return self.contacts_worker is not None and self.contacts_worker.is_alive()
    def album_switch(self,action,selection=None,batch_id=None):
        if not self.busy():return False
        if type(self.engine) is Engine and not self.pause_requested and not self.native_test and not self.closing:
            self.pause()
            self.album_pending=(action,selection,batch_id)
            self.album_busy(True)
            self.root.after(100,self.album_switch_ready)
        return True
    def album_switch_ready(self):
        if not self.album_pending:return
        if self.closing or self.ui_state=='error':self.album_pending=None;self.album_busy(self.busy());return
        if self.busy():self.root.after(100,self.album_switch_ready);return
        action,selection,batch_id=self.album_pending;self.album_pending=None
        if action=='continue':self.album_continue()
        else:self.album_launch(action,selection,batch_id)
    def album_scan_new(self):
        try:
            selected=self.album_contacts.get(self.album_person.get())
            if not selected:raise ValueError('请选择一位好友')
            since,until=date_range(self.album_period.get(),self.album_start.get(),self.album_end.get())
        except ValueError as exc:messagebox.showerror('请检查选择',str(exc),parent=self.root);return
        self.album_launch('scan',selection=(self.album_account,selected['user'],since,until))
    def album_scan_resume(self):
        if self.album_batch:self.album_launch('scan',batch_id=self.album_batch['id'])
    def album_add(self):
        if self.album_batch:self.album_launch('enqueue',batch_id=self.album_batch['id'])
    def album_launch(self,action,selection=None,batch_id=None):
        if action=='contacts':self.album_read_contacts();return
        if self.closing or self.album_switch(action,selection,batch_id):return
        try:config=self.config()
        except (ValueError,OSError) as exc:messagebox.showerror('请检查设置',str(exc),parent=self.root);return
        self.native_test=False;self.pause_requested=False;self.album_queued_start=False;self.album_finished_text='';self.set_busy(True)
        self.set_phase('checking',{'contacts':'正在读取好友…','scan':'正在读取朋友圈…','enqueue':'正在安排补赞…'}[action])
        self.engine=AlbumTask(self.binary_home,self.home,config,self.ledger,self.events.put,action,selection,batch_id)
        self.worker=threading.Thread(target=self.engine.run,name='MomentsAlbum',daemon=False);self.worker.start()
        self.album_busy(True)
    def album_event(self,value):
        kind=value.get('kind','')
        if kind=='contacts_error':
            self.album_scope.set('好友读取失败：'+value['text']);self.log('好友读取失败：'+value['text']);return True
        if kind=='contacts_finished':
            self.contacts_loading=False;self.album_busy(self.busy());return True
        if kind in ('album_contacts','contacts_loaded'):
            if self.closing:return True
            if kind=='contacts_loaded' and value['requested_account']!=self.account.get().strip():
                self.album_scope.set('账号已切换，请重新读取好友');return True
            self.album_account=value['account'];contacts=value['contacts'];counts={}
            for c in contacts:counts[c['label']]=counts.get(c['label'],0)+1
            self.album_contacts={}
            for c in sorted(contacts,key=lambda c:(c['label'].casefold(),c['user'])):
                label=c['label']
                if counts[label]>1:label+=f"（{c['nick']} · {hashlib.sha256(c['user'].encode()).hexdigest()[:6]}）"
                self.album_contacts[label]=c
            self.album_filter();self.album_scope.set(f'{len(contacts)} 位好友')
        elif kind=='album_progress':self.album_render(value['batch'])
        elif kind=='album_queued':self.album_queued_start=bool(value['count']) and not self.pause_requested and not self.closing
        elif kind=='album_finished':self.album_finished_text=value['text']
        else:return False
        return True
    def album_render(self,batch):
        self.album_batch=batch
        if not batch:self.album_busy(self.busy());return
        counts=batch['counts'];labels={'candidate':'待加入','already':'已点过，跳过','planned':'已有计划','attention':'待核对','outside':'不在日期范围'}
        state={'scanning':'扫描中','paused':'扫描已暂停','ready':'预览完成','queued':'已加入计划'}[batch['state']]
        period=('全部可见' if not batch['since'] else timestamp(batch['since'],full=True))+' ～ '+timestamp(batch['until'],full=True)
        plans={r['id']:r for r in AlbumStore(self.ledger).plans(batch['id'])} if batch['state']=='queued' else {}
        progress=(f"本次计划 {len(plans)} 条　已确认 {sum(r['status'] in ('liked','already') for r in plans.values())}　待执行 {sum(r['status']=='pending' for r in plans.values())}　需核对 {sum(r['status'] in ('uncertain','submitted','unreadable') for r in plans.values())}"
            if batch['state']=='queued' else f"已检查 {sum(counts.values())} 条 / {batch['pages']} 页　待加入 {counts.get('candidate',0)}　已点过 {counts.get('already',0)}　已有计划 {counts.get('planned',0)}　需跳过 {counts.get('attention',0)}")
        self.album_summary.set(f"{batch['label']} · {state}\n{period}\n{progress}\n"+(batch['reason'] if batch['state']!='queued' else '计划时间可在“任务记录”查看。'))
        self.album_table.delete(*self.album_table.get_children())
        for item in AlbumStore(self.ledger).items(batch['id'])[-500:][::-1]:
            status=task_state(plans[item['id']],time.time(),running=self.busy()) if item['id'] in plans else '未加入计划' if batch['state']=='queued' and item['category'] in ('candidate','planned') else labels[item['category']]
            self.album_table.insert('', 'end',values=(timestamp(item['created'],full=True),item.get('summary',''),status))
        self.album_busy(self.busy())
    def album_start_after_queue(self):
        if not self.album_queued_start or self.closing:return
        if self.busy():self.root.after(100,self.album_start_after_queue);return
        self.album_queued_start=False;self.album_continue()
    def album_continue(self):
        if self.closing or not self.album_batch or self.album_batch['state']!='queued':return
        if self.album_switch('continue'):return
        try:config=self.config()
        except ValueError as exc:messagebox.showerror('请检查设置',str(exc),parent=self.root);return
        self.native_test=False;self.pause_requested=False;self.album_finished_text='';self.set_busy(True)
        self.engine=BatchEngine(self.binary_home,config,self.ledger,self.events.put,reader_home=self.home,batch_id=self.album_batch['id'])
        self.set_phase('connecting','正在继续补赞…')
        self.worker=threading.Thread(target=self.engine.run,name='MomentsBatch',daemon=False);self.worker.start()
        self.album_busy(True)
