import os
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
import ctypes
from ctypes import wintypes
import threading
import queue
import time
import json
from pathlib import Path
import sys
import subprocess
# 某些嵌入式 Python 使用 ._pth，直接启动脚本时不会加入脚本目录。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tkinter as tk
from tkinter import ttk
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from audio_output import TemporaryMute
from text_polish import TextPolisher
from local_corrector import correct
from status_hud import StatusHUD
from speech_chunks import SpeechChunker
VERSION='0.7 · 语音与传图'
PREFS_PATH=Path(__file__).with_name('preferences.local.json')
try: PREFS=json.loads(PREFS_PATH.read_text(encoding='utf-8')) if PREFS_PATH.exists() else {}
except (ValueError,OSError): PREFS={}

CONFIG_PATH = Path(__file__).with_name('config.local.json')
CONFIG = json.loads(CONFIG_PATH.read_text(encoding='utf-8')) if CONFIG_PATH.exists() else {}
CONFIG.setdefault('phonepad_path', str(Path(__file__).resolve().parent.parent/'phonepad'))
MODEL = os.environ.get('LOCAL_ASR_MODEL', CONFIG.get('model_path', 'models/faster-whisper-large-v3-turbo'))
MODEL = str((Path(__file__).parent / MODEL).resolve())
user32 = ctypes.WinDLL('user32', use_last_error=True)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
user32.GetAncestor.restype=wintypes.HWND
user32.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
PTR = ctypes.c_size_t
class KI(ctypes.Structure):
    _fields_ = [('vk', wintypes.WORD), ('scan', wintypes.WORD), ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('extra', PTR)]
class U(ctypes.Union):
    _fields_ = [('ki', KI), ('padding', ctypes.c_byte * (32 if ctypes.sizeof(PTR)==8 else 24))]
class INPUT(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('u', U)]

def clipboard_text(text,owner):
    """Write only our result; never read existing clipboard contents."""
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GlobalAlloc.argtypes=[wintypes.UINT,ctypes.c_size_t]
    kernel.GlobalAlloc.restype=wintypes.HGLOBAL
    kernel.GlobalLock.argtypes=[wintypes.HGLOBAL]
    kernel.GlobalLock.restype=ctypes.c_void_p
    kernel.GlobalUnlock.argtypes=[wintypes.HGLOBAL]
    kernel.GlobalFree.argtypes=[wintypes.HGLOBAL]
    user32.OpenClipboard.argtypes=[wintypes.HWND]
    user32.SetClipboardData.argtypes=[wintypes.UINT,wintypes.HANDLE]
    user32.SetClipboardData.restype=wintypes.HANDLE
    raw=(text+'\0').encode('utf-16-le')
    handle=kernel.GlobalAlloc(0x42,len(raw))
    if not handle: raise RuntimeError('剪贴板内存分配失败')
    owned=True
    try:
        ptr=kernel.GlobalLock(handle)
        if not ptr: raise RuntimeError('剪贴板内存锁定失败')
        ctypes.memmove(ptr,raw,len(raw)); kernel.GlobalUnlock(handle)
        if not user32.OpenClipboard(owner): raise RuntimeError('剪贴板正忙，请稍后重试')
        try:
            if not user32.EmptyClipboard(): raise RuntimeError('剪贴板不可写')
            if not user32.SetClipboardData(13,handle): raise RuntimeError('复制识别结果失败')
            owned=False
        finally: user32.CloseClipboard()
    finally:
        if owned: kernel.GlobalFree(handle)

class App:
    def __init__(self, host=None):
        self.embedded = host is not None
        self.root = host if self.embedded else tk.Tk()
        if not self.embedded:
            self.root.title('手机工作台 v'+VERSION)
            self.root.geometry('840x700')
            self.root.minsize(780,620)
        icon=Path(__file__).with_name('workbench.ico')
        if not self.embedded and icon.exists(): self.root.iconbitmap(str(icon))
        self.root.configure(bg='#f3f5fa')
        style=ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.',font=('Microsoft YaHei UI',10),background='#f3f5fa',foreground='#24324a')
        style.configure('TButton',padding=(16,10),background='#ffffff',borderwidth=0)
        style.map('TButton',background=[('active','#e5edff')])
        style.configure('Primary.TButton',background='#3264e8',foreground='white',font=('Microsoft YaHei UI',12,'bold'),padding=(24,12))
        style.map('Primary.TButton',background=[('active','#2455d4'),('disabled','#c4cde1')])
        style.configure('TCheckbutton',padding=3)
        style.configure('TProgressbar',background='#3975e8',troughcolor='#e0e7f3')
        header=ttk.Frame(self.root); header.pack(fill='x',padx=24,pady=(16,6))
        ttk.Label(header,text='手机工作台',font=('Microsoft YaHei UI',22,'bold')).pack(side='left')
        ttk.Label(header,text='本地语音 · 扫码传图',foreground='#63718b').pack(side='left',padx=16)
        actions=ttk.Frame(self.root); actions.pack(fill='x',padx=24,pady=6)
        ttk.Button(actions,text='手机连接与传图',command=self.open_images).pack(side='left',padx=(0,10))
        ttk.Button(actions,text='打开收到的图片',command=self.open_image_folder).pack(side='left')
        ttk.Button(actions,text='PhonePad 扫码连接',command=self.open_phonepad).pack(side='left',padx=10)
        self.events = queue.Queue()
        self.model = None
        self.polisher = None
        self.stream = None
        self.busy = True
        self.frames = []
        self.samples = 0
        self.peak = 0.0
        self.last_audio = 0.0
        self.record_started = 0.0
        self.target = None
        self.generation=0
        self.decode_started=0
        self.elapsed=''
        self.output_mute=TemporaryMute()
        self.raw_result=''
        self.continuous=tk.BooleanVar(value=PREFS.get('continuous',self.embedded))
        self.session_continuous=False
        self.chunker=None
        self.segment_jobs=queue.Queue()
        self.segment_pending=0
        self.segment_lock=threading.Lock()
        self.inference_lock=threading.Lock()
        self.session_transcript=''
        self.correction_busy=False
        self.state=tk.StringVar(value='加载模型')
        ttk.Label(self.root,textvariable=self.state,font=('Microsoft YaHei UI',18,'bold')).pack(pady=6)
        ttk.Label(self.root,text='F8 按一下开始，再按一下结束（不用长按） · F9 清空本轮',font=('Microsoft YaHei UI',11,'bold')).pack()
        self.hotkey_status=tk.StringVar(value='正在检查快捷键…')
        ttk.Label(self.root,textvariable=self.hotkey_status).pack()
        self.status = tk.StringVar(value='正在离线加载模型…')
        ttk.Label(self.root, textvariable=self.status, wraplength=620).pack(padx=12,pady=12)
        devices = sd.query_devices()
        self.devices = [i for i,d in enumerate(devices) if d['max_input_channels'] > 0]
        self.device = ttk.Combobox(self.root, state='readonly', width=85,
            values=[f'{i}: {devices[i]["name"]} [{sd.query_hostapis(devices[i]["hostapi"])["name"]}]' for i in self.devices])
        self.device.pack(padx=12)
        preferred = next((i for i in self.devices if 'Virtual Mic' in devices[i]['name'] and 'WASAPI' in sd.query_hostapis(devices[i]['hostapi'])['name']), self.devices[0] if self.devices else None)
        if preferred is not None: self.device.current(self.devices.index(preferred))
        ttk.Button(self.root,text='刷新设备（重连后使用）',command=self.refresh_devices).pack(pady=4)
        self.meter=ttk.Progressbar(self.root,maximum=100,length=600)
        self.meter.pack(pady=4)
        self.meter_text=tk.StringVar(value='麦克风未开启；按 F8 后显示音量。')
        ttk.Label(self.root,textvariable=self.meter_text).pack()
        self.record_button=ttk.Button(self.root,text='开始录音（F8）',command=self.toggle,state='disabled')
        self.record_button.pack(pady=10)
        options=ttk.Frame(self.root)
        options.pack()
        ttk.Label(options,text='识别语言：').pack(side='left')
        self.language=ttk.Combobox(options,state='readonly',width=20,values=['中文优先','英文','自动／中英混合'])
        self.language.current(0)
        self.language.pack(side='left')
        ttk.Button(options,text='清空本轮（F9）',command=self.clear_round).pack(side='left',padx=10)
        settings=ttk.Frame(self.root)
        settings.pack(pady=6)
        self.quality=ttk.Combobox(settings,state='readonly',width=16,values=['均衡','速度优先','准确优先'])
        self.quality.current(0)
        self.quality.pack(side='left',padx=4)
        self.delivery=ttk.Combobox(settings,state='readonly',width=34,values=['自动粘贴（会更新剪贴板）','逐字输入（保留剪贴板）'])
        self.delivery.current(0)
        self.delivery.pack(side='left',padx=4)
        self.quiet=tk.BooleanVar(value=False)
        ttk.Checkbutton(self.root,text='防外放干扰：仅录音期间静音电脑，停止后恢复',variable=self.quiet).pack()
        self.auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.root,text='自动上屏：留在聊天输入框，F8 开始 → F8 停止 → 自动粘贴',variable=self.auto).pack()
        self.show_hud=tk.BooleanVar(value=PREFS.get('hud',True))
        ttk.Checkbutton(self.root,text='屏幕顶部显示录音状态（不抢输入焦点）',variable=self.show_hud).pack()
        for widget,key in ((self.language,'language'),(self.quality,'quality'),(self.delivery,'delivery')):
            if PREFS.get(key) in widget['values']: widget.set(PREFS[key])
        self.auto.set(PREFS.get('auto',True)); self.quiet.set(PREFS.get('quiet',False))
        self.hud=StatusHUD(self.root)
        self.show_hud.trace_add('write',lambda *_: self.hud.hide() if not self.show_hud.get() else None)
        self.text = tk.Text(self.root,height=9,wrap='word')
        self.text.pack(fill='both',expand=True,padx=12,pady=10)
        ttk.Button(self.root,text='3 秒后输入到当前窗口（点击后切换目标）',command=self.manual).pack(pady=8)
        ttk.Button(self.root,text='本地 AI 纠错预览（可选，较慢，不自动替换已输入文字）',command=self.correct_preview).pack(pady=4)
        if not self.embedded: self.root.protocol('WM_DELETE_WINDOW',self.close)
        if self.embedded: self.organize_compact()
        else: self.organize_layout()
        threading.Thread(target=self.load,daemon=True).start()
        threading.Thread(target=self.hotkeys,daemon=True).start()
        threading.Thread(target=self.segment_worker,daemon=True).start()
        self.root.after(50,self.poll)
        if not self.embedded: self.root.mainloop()

    def organize_compact(self):
        for widget in self.root.winfo_children():
            if not isinstance(widget,tk.Toplevel): widget.pack_forget()
        self.root.configure(bg='white')
        style=ttk.Style(self.root)
        style.configure('Voice.TLabel',background='white',foreground='#172b46')
        style.configure('Voice.TFrame',background='white')
        ttk.Label(self.root,text='F8 开始录音 · 再按 F8 结束',font=('Microsoft YaHei UI',17,'bold'),style='Voice.TLabel').pack(anchor='w',padx=22,pady=(18,8))
        ttk.Label(self.root,textvariable=self.state,style='Voice.TLabel').pack(anchor='w',padx=22)
        row=ttk.Frame(self.root,style='Voice.TFrame');row.pack(fill='x',padx=22,pady=8)
        self.record_button.configure(style='Primary.TButton')
        self.record_button.pack(in_=row,side='left')
        self.record_button.tk.call('raise',self.record_button._w)
        ttk.Button(row,text='清空（F9）',command=self.clear_round).pack(side='right')
        controls=ttk.Frame(self.root,style='Voice.TFrame');controls.pack(fill='x',padx=22)
        ttk.Checkbutton(controls,text='停顿自动出字',variable=self.continuous).pack(side='left')
        ttk.Checkbutton(controls,text='录音期间静音外放',variable=self.quiet).pack(side='left',padx=12)
        self.meter.pack(fill='x',padx=22,pady=4)
        ttk.Label(self.root,textvariable=self.status,wraplength=640,style='Voice.TLabel').pack(anchor='w',padx=22,pady=4)
        self.text.configure(bg='#f8faff',fg='#172b46',insertbackground='#315fe9',relief='flat',padx=12,pady=10,font=('Microsoft YaHei UI',11),height=4)
        self.text.pack(fill='both',expand=True,padx=22,pady=8)
        self.device.configure(width=42)
        self.device.pack(fill='x',padx=22,pady=(0,5))
        ttk.Label(self.root,textvariable=self.hotkey_status,style='Voice.TLabel').pack(anchor='w',padx=22,pady=(0,10))

    def organize_layout(self):
        children=[w for w in self.root.winfo_children() if not isinstance(w,tk.Toplevel)]
        for w in children: w.pack_forget()
        header,actions=children[:2]
        header.pack(fill='x',padx=28,pady=(22,10))
        actions.pack(fill='x',padx=28,pady=(0,14))
        # Existing controls are direct root children. Repacking into a newer
        # sibling frame can place that frame over them on Windows/Tk.
        main=self.root
        state_labels=[]
        for w in children:
            if isinstance(w,ttk.Label) and str(w.cget('textvariable')) in (str(self.state),str(self.status)):
                state_labels.append(w)
        for w in state_labels: w.pack(in_=main,anchor='w',padx=28,pady=5)
        self.record_button.configure(style='Primary.TButton')
        self.record_button.pack(in_=main,anchor='w',padx=28,pady=(14,10))
        self.meter.pack(in_=main,fill='x',padx=28,pady=(4,16))
        ttk.Label(self.root,text='本轮文字',font=('Microsoft YaHei UI',12,'bold')).pack(anchor='w',padx=28,pady=(8,8))
        self.text.configure(bg='#ffffff',fg='#20304b',insertbackground='#3264e8',
            relief='flat',borderwidth=0,padx=18,pady=16,font=('Microsoft YaHei UI',12),spacing1=4,spacing3=7)
        self.text.pack(fill='both',expand=True,padx=28,pady=(0,12))
        footer=ttk.Frame(self.root); footer.pack(fill='x',padx=28,pady=(0,12))
        advanced=ttk.Frame(self.root)
        excluded=set([header,actions,self.record_button,self.meter,self.text]+state_labels)
        for w in children:
            if w not in excluded: w.pack(in_=advanced,anchor='w',fill='x',pady=3)
        def expand():
            if advanced.winfo_manager(): advanced.pack_forget(); toggle.configure(text='设置与更多操作 ▾')
            else:
                advanced.pack(fill='x',padx=28,pady=(0,18))
                for control in children:
                    if control not in excluded: control.tk.call('raise',control._w)
                toggle.configure(text='收起设置 ▴')
        toggle=ttk.Button(footer,text='设置与更多操作 ▾',command=expand)
        toggle.pack(side='left')
        ttk.Label(footer,text='F8 开始 / 结束     F9 清空',foreground='#62728d').pack(side='right')

    def open_image_folder(self):
        folder=Path(CONFIG.get('phonepad_path','phonepad'))/'received_images'
        folder.mkdir(exist_ok=True)
        os.startfile(str(folder))

    def open_phonepad(self):
        script=Path(CONFIG.get('phonepad_path','phonepad'))/'qr_connect.py'
        if not script.is_file():
            self.status.set('未找到 PhonePad 二维码工具，请在配置中设置 phonepad_path。'); return
        if getattr(self,'phonepad_process',None) is not None and self.phonepad_process.poll() is None:
            self.status.set('PhonePad 二维码窗口已经打开，请在任务栏查看。'); return
        try:
            self.phonepad_process=subprocess.Popen([sys.executable,'-B',str(script)],creationflags=0x08000000)
        except OSError as exc: self.status.set('二维码窗口启动失败：'+str(exc))

    def open_images(self):
        self.open_phonepad()
        self.status.set('手机已连接时，在手机原输入页点“传图”即可发送图片，无需再次扫码。')

    def hint(self,text,seconds=None):
        if self.show_hud.get(): self.hud.show(text,seconds)

    def correct_preview(self):
        if self.correction_busy or self.busy or self.stream is not None:
            self.status.set('请先完成当前录音或识别。'); return
        text=self.text.get('1.0','end-1c').strip()
        if not text: self.status.set('请先录一句话。'); return
        self.correction_busy=True
        self.status.set('正在本机纠错，原文保持不变；此功能不会影响下一次录音。')
        generation=self.generation
        def worker():
            try: self.events.put(('corrected',(generation,text,correct(text,CONFIG.get('correction_model','qwen2.5:7b-gpu')))))
            except Exception as exc: self.events.put(('correction-error',type(exc).__name__))
        threading.Thread(target=worker,daemon=True).start()

    def show_correction(self,original,result):
        if self.polisher: result=self.polisher.converter.convert(result)
        window=tk.Toplevel(self.root); window.title('纠错预览 · 请核对原意'); window.geometry('700x430')
        ttk.Label(window,text='原始识别（保留，不自动替换目标窗口）').pack()
        raw=tk.Text(window,height=5,wrap='word'); raw.pack(fill='x',padx=10)
        raw.insert('1.0',original); raw.configure(state='disabled')
        ttk.Label(window,text='AI 建议（可编辑；数字、专有名词及否定词请重点核对）').pack()
        edited=tk.Text(window,height=7,wrap='word'); edited.pack(fill='both',expand=True,padx=10)
        edited.insert('1.0',result)
        def copy_result():
            try:
                clipboard_text(edited.get('1.0','end-1c'),user32.GetAncestor(window.winfo_id(),2))
                self.status.set('修正版已复制，请在目标框手动替换。原文仍保留。')
                window.destroy()
            except Exception as exc: self.status.set(str(exc))
        ttk.Button(window,text='复制修正版（不自动上屏）',command=copy_result).pack(pady=8)

    def load(self):
        try:
            if not Path(MODEL,'model.bin').is_file():
                raise RuntimeError('未找到模型。请在 config.local.json 设置 model_path。')
            device=CONFIG.get('device','cuda')
            self.model = WhisperModel(MODEL,device=device,compute_type=CONFIG.get('compute_type','float16' if device=='cuda' else 'int8'),local_files_only=True)
            # 预热 GPU 和解码器，让首次录音后的等待更稳定。
            segments,_=self.model.transcribe(np.zeros(16000,dtype=np.float32),language='zh',beam_size=1,temperature=0.0)
            list(segments)
            self.polisher=TextPolisher(MODEL,load_punctuation=False)
            self.events.put(('ready',None))
            if CONFIG.get('enable_extra_punctuation',False):
                threading.Thread(target=self.prepare_punctuation,daemon=True).start()
        except Exception as e: self.events.put(('error',str(e)))

    def prepare_punctuation(self):
        self.polisher.load_punctuation()
        self.events.put(('polish-ready',self.polisher.note))

    def hotkeys(self):
        registered=[]
        messages=[]
        for identifier,vk,name in [(1,0x77,'F8'),(2,0x78,'F9')]:
            if user32.RegisterHotKey(None,identifier,0x4000,vk):
                registered.append(identifier); messages.append(name+' 已就绪')
            else: messages.append(name+' 被其他程序占用！请关闭旧版工具')
        self.events.put(('hotkeys',' / '.join(messages)))
        if not registered: return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg),None,0,0)>0:
            if msg.message == 0x0312:
                self.events.put(('toggle' if msg.wParam==1 else 'clear',None))

    def poll(self):
        if self.busy and self.decode_started:
            self.state.set(f'识别中 · {time.monotonic()-self.decode_started:.1f} 秒')
        if self.stream is not None:
            self.meter['value']=min(100,max(0,(20*np.log10(max(self.peak,1e-7))+60)/60*100))
            seconds=int(time.monotonic()-self.record_started)
            if time.monotonic()-self.last_audio>2:
                self.meter_text.set(f'录音 {seconds} 秒 · 暂无有效音量；检查电脑 AudioRelay 是否为 Mic 模式')
            else:
                self.meter_text.set(f'录音 {seconds} 秒 · 已收到声音（不代表一定是语音）')
        try:
            while True:
                kind,value = self.events.get_nowait()
                if kind=='toggle': self.toggle()
                elif kind=='clear': self.clear_round()
                elif kind=='finish-session':
                    if value==self.generation and self.stream is not None:self.toggle()
                elif kind=='segment-result':
                    generation,text,elapsed,error=value
                    if generation!=self.generation:continue
                    with self.segment_lock:
                        self.segment_pending=max(0,self.segment_pending-1)
                        pending=self.segment_pending
                    if error:self.status.set('这一句未完成：'+error+'；后续录音继续保留。')
                    elif text:
                        prefix=' ' if self.session_transcript and self.session_transcript[-1].isascii() and text[0].isascii() else ''
                        self.session_transcript+=prefix+text
                        self.text.insert('end',prefix+text);self.text.see('end')
                        self.raw_result=self.session_transcript
                        self.elapsed=f'上一句识别 {elapsed:.1f} 秒'
                        self.status.set(self.elapsed+(' · 还有 '+str(pending)+' 句待处理' if pending else ' · 继续说话即可'))
                        if self.auto.get():self.inject(prefix+text,self.target)
                    if self.stream is None and not pending:
                        self.busy=False;self.decode_started=0
                        self.state.set('录音已结束');self.record_button.configure(state='normal',text='开始录音（F8）')
                        self.hint('录音已结束 · F8 开始下一轮',3)
                elif kind=='ready':
                    self.busy=False
                    self.state.set('准备好了')
                    self.record_button.configure(state='normal',text='开始录音（F8）')
                    self.status.set('F8 开始后，停顿自动出字；再按 F8 结束。' if self.continuous.get() else self.polisher.note+'。F8 开始，再按 F8 结束。')
                elif kind=='notice': self.status.set(value)
                elif kind=='hotkeys': self.hotkey_status.set(value)
                elif kind=='polish-ready':
                    if not self.busy and self.stream is None: self.status.set(value)
                elif kind=='error':
                    self.busy=False
                    self.decode_started=0
                    self.state.set('本轮未完成')
                    self.record_button.configure(state='normal' if self.model else 'disabled',text='重新录音（F8）')
                    self.status.set('错误：'+value)
                    self.hint('本轮未完成 · 请查看工具窗口',4)
                elif kind=='correction-error':
                    self.correction_busy=False
                    self.status.set('本机纠错不可用或超时（'+value+'），原文保持不变。请确认 Ollama 已启动且模型已安装；工具不会自动下载。')
                elif kind=='corrected':
                    self.correction_busy=False
                    generation,original,result=value
                    if generation==self.generation: self.show_correction(original,result)
                    else: self.status.set('旧一轮纠错结果已丢弃。')
                elif kind=='result':
                    self.busy=False
                    self.decode_started=0
                    self.record_button.configure(state='normal',text='录下一句（F8）')
                    generation,value,elapsed=value
                    if generation!=self.generation:
                        self.state.set('已取消 · 可以录下一句')
                        continue
                    self.elapsed=f'识别耗时 {elapsed:.1f} 秒'
                    self.state.set('完成 · '+self.elapsed)
                    self.text.delete('1.0','end')
                    self.text.insert('1.0',value)
                    self.raw_result=value
                    self.status.set('识别完成。录音仅在内存中处理，没有保存。' if value else '收到音频，但未识别到语音。请检查音量并重新说一句。')
                    if value and self.auto.get(): self.inject(value,self.target)
                    self.hint('本轮完成 · '+self.elapsed+' · F8 录下一句',4)
        except queue.Empty: pass
        self.root.after(50,self.poll)

    def toggle(self):
        if self.busy or self.model is None: return
        if self.stream is not None:
            stream,self.stream=self.stream,None
            try: stream.stop(); stream.close()
            finally: self.output_mute.restore()
            if self.session_continuous:
                tail=self.chunker.flush()
                if tail is not None:self.enqueue_segment(tail)
                with self.segment_lock:pending=self.segment_pending
                self.device.configure(state='readonly')
                self.busy=bool(pending)
                self.decode_started=time.monotonic() if pending else 0
                self.state.set('正在处理最后一句…' if pending else '录音已结束')
                self.record_button.configure(state='disabled' if pending else 'normal',text='正在处理…' if pending else '开始录音（F8）')
                self.status.set('最后一句识别完成后自动输入。' if pending else 'F8 开始下一轮。')
                return
            self.busy=True
            self.decode_started=time.monotonic()
            self.state.set('识别中')
            self.record_button.configure(state='disabled',text='正在识别…')
            self.device.configure(state='readonly')
            self.meter['value']=0
            self.meter_text.set('录音已停止。')
            self.status.set('正在本地识别…')
            self.hint('正在本地识别… · F9 可取消本轮')
            audio=np.concatenate(self.frames).reshape(-1) if self.frames else np.zeros(0,dtype=np.float32)
            self.frames=[]
            language={'中文优先':'zh','英文':'en','自动／中英混合':None}[self.language.get()]
            beam={'均衡':3,'速度优先':1,'准确优先':5}[self.quality.get()]
            threading.Thread(target=self.recognize,args=(audio,self.generation,language,beam),daemon=True).start()
            return
        self.target=user32.GetForegroundWindow()
        self.generation+=1
        self.text.delete('1.0','end')
        self.elapsed=''
        self.frames=[]
        self.samples=0
        self.peak=0.0
        self.last_audio=0.0
        self.record_started=time.monotonic()
        self.session_continuous=self.continuous.get()
        self.session_transcript=''
        with self.segment_lock:self.segment_pending=0
        self.session_language={'中文优先':'zh','英文':'en','自动／中英混合':None}[self.language.get()]
        try:
            if not self.devices or self.device.current()<0: raise RuntimeError('没有输入设备，请刷新设备。')
            index=self.devices[self.device.current()]
            self.rate=int(sd.query_devices(index)['default_samplerate'])
            self.chunker=SpeechChunker(self.rate)
            extra=sd.WasapiSettings(exclusive=False) if 'WASAPI' in sd.query_hostapis(sd.query_devices(index)['hostapi'])['name'] else None
            self.stream=sd.InputStream(device=index,samplerate=self.rate,channels=1,dtype='float32',callback=self.capture,extra_settings=extra)
            if self.quiet.get(): self.output_mute.start()
            self.stream.start()
            self.state.set('● 持续录音 · 停顿自动出字' if self.session_continuous else '● 录音中 · 再按 F8 才会识别')
            self.record_button.configure(text='结束录音（F8）' if self.session_continuous else '停止并识别（F8）')
            self.device.configure(state='disabled')
            self.status.set('停顿约 0.8 秒自动识别，识别时继续收音；F8 结束，F9 取消。' if self.session_continuous else '正在录音，按 F8 停止（最长 120 秒）。')
            self.hint('● 持续录音 · 停顿出字 · F8 结束' if self.session_continuous else '● 正在录音 · 再按 F8 结束 · F9 取消')
        except Exception as e:
            self.output_mute.restore()
            if self.stream is not None: self.stream.close()
            self.stream=None
            self.state.set('麦克风打开失败')
            self.status.set('无法录音：'+str(e))

    def capture(self,data,frames,timing,status):
        self.peak=float(np.max(np.abs(data),initial=0))
        if self.peak>0.001: self.last_audio=time.monotonic()
        if self.session_continuous:
            for audio in self.chunker.feed(data):self.enqueue_segment(audio)
        else:self.frames.append(data.copy())
        self.samples+=frames
        if self.samples>=self.rate*(1800 if self.session_continuous else 120):
            self.events.put(('notice','已达到本轮录音时长上限，正在处理最后一句。'))
            self.events.put(('finish-session',self.generation))
            raise sd.CallbackStop

    def enqueue_segment(self,audio):
        if audio is None or not audio.size:return
        with self.segment_lock:self.segment_pending+=1
        self.segment_jobs.put((audio,self.generation,self.rate,self.session_language))

    def segment_worker(self):
        while True:
            audio,generation,rate,language=self.segment_jobs.get()
            try:
                if generation!=self.generation:continue
                started=time.monotonic()
                text=self.transcribe_audio(audio,rate,language,1,filter_noise=True)
                self.events.put(('segment-result',(generation,text,time.monotonic()-started,None)))
            except Exception as exc:
                self.events.put(('segment-result',(generation,'',0,str(exc))))
            finally:self.segment_jobs.task_done()

    def refresh_devices(self):
        if self.stream is not None or self.busy:
            self.status.set('请先停止录音并等待识别结束。'); return
        try:
            sd._terminate(); sd._initialize()
            devices=sd.query_devices()
            self.devices=[i for i,d in enumerate(devices) if d['max_input_channels']>0]
            self.device['values']=[f'{i}: {devices[i]["name"]} [{sd.query_hostapis(devices[i]["hostapi"])["name"]}]' for i in self.devices]
            preferred=next((i for i in self.devices if 'Virtual Mic' in devices[i]['name'] and 'WASAPI' in sd.query_hostapis(devices[i]['hostapi'])['name']),None)
            if self.devices: self.device.current(self.devices.index(preferred) if preferred is not None else 0)
            self.status.set('已找到 AudioRelay 虚拟麦克风。' if preferred is not None else '未找到 AudioRelay WASAPI 麦克风，请检查连接。')
        except Exception as e: self.status.set('刷新失败：'+str(e))

    def clear_round(self):
        self.generation+=1
        if self.session_continuous:
            self.busy=False;self.decode_started=0
            with self.segment_lock:self.segment_pending=0
        self.text.delete('1.0','end')
        if self.stream is not None:
            stream,self.stream=self.stream,None
            try: stream.stop(); stream.close()
            finally: self.output_mute.restore()
            self.frames=[]
            self.meter['value']=0
            self.device.configure(state='readonly')
            self.record_button.configure(text='开始录音（F8）')
        self.status.set('本轮已清空。已输入到其他应用的文字不会被删除。')
        self.hint('本轮已清空 · F8 开始录音',3)
        if not self.busy: self.state.set('准备好了')

    def recognize(self,audio,generation,language,beam):
        try:
            started=time.monotonic()
            text=self.transcribe_audio(audio,self.rate,language,beam)
            self.events.put(('result',(generation,text,time.monotonic()-started)))
        except Exception as e: self.events.put(('error',str(e)))

    def transcribe_audio(self,audio,rate,language,beam,filter_noise=False):
        with self.inference_lock:
            if len(audio)<rate*0.25 or np.max(np.abs(audio),initial=0)<0.0001:
                if filter_noise:return ''
                raise RuntimeError('没有收到有效声音。请检查 AudioRelay 是否正在传输到虚拟麦克风。')
            if rate!=16000:
                from scipy.signal import resample_poly
                import math
                g=math.gcd(rate,16000)
                audio=resample_poly(audio,16000//g,rate//g).astype(np.float32)
            if filter_noise:
                from scipy.signal import butter,sosfilt
                audio=sosfilt(butter(2,85,btype='highpass',fs=16000,output='sos'),audio).astype(np.float32)
            prompt='这是一段简体中文口述转写，包含自然的逗号、句号和问号。' if language=='zh' else None
            segments,_=self.model.transcribe(audio,language=language,beam_size=beam,best_of=1,temperature=0.0,initial_prompt=prompt,
                vad_filter=True,condition_on_previous_text=False)
            text=''.join(s.text for s in segments).strip()
            if self.polisher: text=self.polisher.apply(text)
            return text

    def manual(self):
        value=self.text.get('1.0','end-1c')
        if value:
            self.status.set('请在 3 秒内切换到目标输入框。')
            generation=self.generation
            self.root.after(3000,lambda:self.inject(value,user32.GetForegroundWindow()) if generation==self.generation else None)

    def inject(self,text,target):
        if self.polisher: text=self.polisher.converter.convert(text)
        pid=wintypes.DWORD()
        if target: user32.GetWindowThreadProcessId(target,ctypes.byref(pid))
        if pid.value==os.getpid():
            self.status.set('请留在聊天输入框按 F8 开始、F8 停止，无需切回本工具。结果已保留。')
            return
        if not target or user32.GetForegroundWindow()!=target:
            self.status.set('焦点已变化，未输入；识别结果保留在本窗口。')
            return
        name=ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(target),name,256)
        if name.value in ('Progman','WorkerW','Shell_TrayWnd','Shell_SecondaryTrayWnd'):
            self.status.set('当前是桌面或任务栏，未输入。')
            return
        if self.delivery.current()==0:
            # 只在目标仍为前台且没有其他修饰键按下时粘贴。
            if any(user32.GetAsyncKeyState(vk)&0x8000 for vk in (0x10,0x11,0x12,0x5B,0x5C)):
                self.status.set('请松开 Ctrl/Shift/Alt/Win 键；结果已保留。'); return
            try:
                owner=user32.GetAncestor(self.root.winfo_id(),2)
                clipboard_text(text.replace('\r',' ').replace('\n',' '),owner)
                if user32.GetForegroundWindow()!=target:
                    self.status.set('焦点已变化，结果已复制；请到目标框 Ctrl+V。'); return
                keys=(INPUT*4)()
                for i,(vk,flags) in enumerate(((0x11,0),(0x56,0),(0x56,2),(0x11,2))):
                    keys[i].type=1
                    keys[i].u.ki=KI(vk,0,flags,0,0)
                if user32.SendInput(4,keys,ctypes.sizeof(INPUT))!=4:
                    # 确保部分注入失败时不留下按住的修饰键。
                    releases=(INPUT*2)()
                    for i,vk in enumerate((0x56,0x11)):
                        releases[i].type=1; releases[i].u.ki=KI(vk,0,2,0,0)
                    user32.SendInput(2,releases,ctypes.sizeof(INPUT))
                    raise RuntimeError('Windows 阻止自动粘贴，请在目标框 Ctrl+V')
                self.status.set('已发送粘贴指令 · '+self.elapsed+' · 可直接按 F8 录下一句')
            except Exception as exc: self.status.set(str(exc))
            return
        # 不发送 Enter，避免聊天框因换行自动提交。
        raw=text.replace('\r',' ').replace('\n',' ').encode('utf-16-le')
        for i in range(0,len(raw),2):
            if user32.GetForegroundWindow()!=target:
                self.status.set('输入过程中焦点变化，已停止；请检查是否部分输入。')
                return
            unit=int.from_bytes(raw[i:i+2],'little')
            pair=(INPUT*2)()
            for j,flag in enumerate((4,6)):
                pair[j].type=1
                pair[j].u.ki=KI(0,unit,flag,0,0)
            if user32.SendInput(2,pair,ctypes.sizeof(INPUT))!=2:
                self.status.set('Windows 拒绝输入，可能目标程序以管理员权限运行；请手动复制结果。')
                return
        self.status.set('已发送文字，请确认目标框实际收到。')

    def close(self):
        try:
            preferences={'language':self.language.get(),'quality':self.quality.get(),
                'delivery':self.delivery.get(),'auto':self.auto.get(),'quiet':self.quiet.get(),'hud':self.show_hud.get(),'continuous':self.continuous.get()}
            try: PREFS_PATH.write_text(json.dumps(preferences,ensure_ascii=False,indent=2),encoding='utf-8')
            except OSError: pass
            if self.stream is not None: self.stream.stop(); self.stream.close()
        finally:
            self.output_mute.restore()
            self.root.destroy()

if __name__=='__main__':
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    mutex=kernel.CreateMutexW(None,False,'Local\\LocalDictationDesktopV05')
    if not mutex: raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error()==183:
        from tkinter import messagebox
        messagebox.showinfo('本地语音输入','新版工具已在运行。请使用已有窗口；更新后请先关闭旧窗口，再启动。')
        kernel.CloseHandle(mutex)
    else:
        try: App()
        finally: kernel.CloseHandle(mutex)
