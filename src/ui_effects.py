# SPDX-License-Identifier: Apache-2.0
"""Local canvas effects, scheduled only while visible and animating."""
import math,time,tkinter as tk

class LikeEffects(tk.Canvas):
    BG='#eaf2ed'
    def __init__(self,parent,compact=False):
        self.compact=compact
        super().__init__(parent,width=190 if compact else 270,height=78 if compact else 156,bg=self.BG,highlightthickness=0)
        self.job=None;self.started=0;self.message='';self.preview=False
        self.idle()
        self.bind('<Destroy>',self._destroy,add='+')
    def _destroy(self,event):
        if event.widget is self:self.stop()
    def stop(self):
        if self.job is not None:
            try:self.after_cancel(self.job)
            except tk.TclError:pass
            self.job=None
    def idle(self):
        self.delete('all')
        if self.compact:
            self.create_oval(106,14,164,72,fill='#dde9e1',outline='')
            self.create_text(135,44,text='♥',font=('Segoe UI Symbol',31),fill='#428165');return
        self.create_oval(151,18,239,106,fill='#dde9e1',outline='')
        self.create_text(195,63,text='♥',font=('Segoe UI Symbol',46),fill='#428165')
    def play(self,label='',preview=False):
        self.stop()
        if not self.winfo_viewable():self.idle();return
        self.message=str(label)[:16];self.preview=preview;self.started=time.monotonic()
        self.frame()
    def blend(self,color,opacity):
        opacity=max(0,min(1,opacity))
        return '#'+''.join(f'{round(int(self.BG[i:i+2],16)*(1-opacity)+int(color[i:i+2],16)*opacity):02x}' for i in (1,3,5))
    def frame(self):
        self.job=None
        if not self.winfo_viewable():self.idle();return
        t=(time.monotonic()-self.started)/1.35
        if t>=1:self.idle();return
        self.delete('all');x,y=(135,44) if self.compact else (195,63)
        self.create_oval(x-44,y-44,x+44,y+44,fill='#dde9e1',outline='')
        r=35+48*t
        self.create_oval(x-r,y-r,x+r,y+r,outline=self.blend('#428165',1-t),width=2)
        size=(31 if self.compact else 46)+round(10*math.sin(min(1,t*2)*math.pi))
        self.create_text(x,y,text='♥',font=('Segoe UI Symbol',size),fill='#428165')
        for i in range(5):
            angle=(i/5*2*math.pi)-math.pi/2
            px=x+math.cos(angle)*(30+58*t);py=y+math.sin(angle)*(30+40*t)-20*t
            self.create_text(px,py,text='♥' if i%2 else '·',font=('Segoe UI Symbol',10+i%3),fill=self.blend('#428165',1-t))
        self.create_text(50 if self.compact else 100,(40 if self.compact else 58)-24*t,text='+1',font=('Segoe UI',25,'bold'),fill=self.blend('#197653',min(1,(1-t)*2)))
        caption='动画预览 · 不计入统计' if self.preview else (self.message+' · 点赞成功')
        if not self.compact:self.create_text(145,128,text=caption,font=('Microsoft YaHei UI',9),fill='#617469',width=250)
        self.job=self.after(30,self.frame)
