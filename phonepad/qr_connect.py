"""PhonePad QR launcher; QR version 5-L, byte encoding, no downloads."""
from pathlib import Path
import re
import os
import shutil
import secrets
import queue
import threading
import json
import importlib.util
import tkinter as tk
from tkinter import filedialog, ttk, messagebox

def multiply(x,y):
    result=0
    for _ in range(8):
        if y&1: result^=x
        y>>=1; x<<=1
        if x&256: x^=0x11d
    return result

def matrix(text):
    data=text.encode('utf-8')
    if len(data)>106: raise ValueError('连接地址过长（最多106字节）')
    bits=[0,1,0,0]+[(len(data)>>i)&1 for i in range(7,-1,-1)]
    for value in data: bits.extend((value>>i)&1 for i in range(7,-1,-1))
    bits.extend([0]*min(4,864-len(bits)))
    while len(bits)%8: bits.append(0)
    words=[sum(bits[i+j]<<(7-j) for j in range(8)) for i in range(0,len(bits),8)]
    pad=0
    while len(words)<108:
        words.append((0xec,0x11)[pad%2]); pad+=1
    generator=[1]; root=1
    for _ in range(26):
        new=[0]*(len(generator)+1)
        for j,value in enumerate(generator):
            new[j]^=value; new[j+1]^=multiply(value,root)
        generator=new; root=multiply(root,2)
    remainder=words+[0]*26
    for i in range(108):
        factor=remainder[i]
        for j,value in enumerate(generator): remainder[i+j]^=multiply(value,factor)
    stream=[]
    for value in words+remainder[-26:]: stream.extend((value>>i)&1 for i in range(7,-1,-1))
    size=37
    cells=[[False]*size for _ in range(size)]
    reserved=[[False]*size for _ in range(size)]
    def put(x,y,value):
        if 0<=x<size and 0<=y<size:
            cells[y][x]=bool(value); reserved[y][x]=True
    for i in range(size): put(6,i,i%2==0); put(i,6,i%2==0)
    for cx,cy in ((3,3),(size-4,3),(3,size-4)):
        for dy in range(-4,5):
            for dx in range(-4,5): put(cx+dx,cy+dy,max(abs(dx),abs(dy)) not in (2,4))
    for dy in range(-2,3):
        for dx in range(-2,3): put(30+dx,30+dy,max(abs(dx),abs(dy))!=1)
    value=8  # error correction L (01), mask 0
    rem=value
    for _ in range(10): rem=(rem<<1)^((rem>>9)*0x537)
    fmt=((value<<10)|rem)^0x5412
    bit=lambda i:(fmt>>i)&1
    for i in range(6): put(8,i,bit(i))
    put(8,7,bit(6)); put(8,8,bit(7)); put(7,8,bit(8))
    for i in range(9,15): put(14-i,8,bit(i))
    for i in range(8): put(size-1-i,8,bit(i))
    for i in range(8,15): put(8,size-15+i,bit(i))
    put(8,size-8,True)
    index=0; right=size-1; up=True
    while right>=1:
        if right==6: right=5
        for vertical in range(size):
            y=size-1-vertical if up else vertical
            for x in (right,right-1):
                if not reserved[y][x]:
                    value=stream[index] if index<len(stream) else 0
                    cells[y][x]=bool(value)^((x+y)%2==0)
                    index+=1
        right-=2; up=not up
    assert index==1079
    return cells

def addresses(path):
    values=re.findall(r'http://[^\s]+',path.read_text(encoding='utf-8-sig'))
    return [value for value in values if not value.startswith('http://127.')]

def enable_file_drop(root, callback):
    """Windows 原生文件拖放，无额外依赖。返回关闭时的清理函数。"""
    if os.name != 'nt': return lambda: None
    import ctypes
    from ctypes import wintypes
    user=ctypes.WinDLL('user32',use_last_error=True)
    shell=ctypes.WinDLL('shell32',use_last_error=True)
    proc_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)
    setter=user.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p)==8 else user.SetWindowLongW
    setter.argtypes=[wintypes.HWND,ctypes.c_int,ctypes.c_void_p]; setter.restype=ctypes.c_void_p
    user.CallWindowProcW.argtypes=[ctypes.c_void_p,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
    user.CallWindowProcW.restype=ctypes.c_ssize_t
    user.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]; user.GetAncestor.restype=wintypes.HWND
    shell.DragAcceptFiles.argtypes=[wintypes.HWND,wintypes.BOOL]
    shell.DragQueryFileW.argtypes=[wintypes.HANDLE,wintypes.UINT,wintypes.LPWSTR,wintypes.UINT]
    shell.DragQueryFileW.restype=wintypes.UINT
    shell.DragFinish.argtypes=[wintypes.HANDLE]
    root.update_idletasks()
    hwnd=user.GetAncestor(root.winfo_id(),2)
    previous=None
    @proc_type
    def wndproc(window,message,wparam,lparam):
        if message==0x0233:  # WM_DROPFILES
            paths=[]
            try:
                count=shell.DragQueryFileW(wparam,0xffffffff,None,0)
                for i in range(count):
                    length=shell.DragQueryFileW(wparam,i,None,0)
                    buffer=ctypes.create_unicode_buffer(length+1)
                    shell.DragQueryFileW(wparam,i,buffer,length+1)
                    paths.append(buffer.value)
            finally: shell.DragFinish(wparam)
            root.after_idle(lambda: callback(paths))
            return 0
        return user.CallWindowProcW(previous,window,message,wparam,lparam)
    previous=setter(hwnd,-4,ctypes.cast(wndproc,ctypes.c_void_p))
    if not previous: raise ctypes.WinError(ctypes.get_last_error())
    root._drop_callback=wndproc
    shell.DragAcceptFiles(hwnd,True)
    def cleanup():
        shell.DragAcceptFiles(hwnd,False)
        setter(hwnd,-4,previous)
    return cleanup

def main():
    root=tk.Tk(); root.title('手机工作台 v1.6 · 扫码与互传')
    try:
        local=json.loads((Path(__file__).parent/'desktop.local.json').read_text(encoding='utf-8-sig'))
        if Path(local.get('icon_path','')).is_file(): root.iconbitmap(local['icon_path'])
    except (OSError,ValueError,tk.TclError): pass
    bg='#f2f5fa'; ink='#172b46'; muted='#66758a'; blue='#315fe9'; line='#e2e8f1'
    root.configure(bg=bg)
    width=min(740,root.winfo_screenwidth()-60); height=min(660,root.winfo_screenheight()-80)
    root.geometry(f'{width}x{height}+{max(0,(root.winfo_screenwidth()-width)//2)}+{max(0,(root.winfo_screenheight()-height)//2)}')
    root.minsize(min(640,width),min(580,height))
    root.option_add('*Font',('Microsoft YaHei UI',10))
    style=ttk.Style(root); style.theme_use('clam')
    style.configure('Transfer.TButton',padding=(18,12),background='white',foreground=ink,borderwidth=0)
    style.map('Transfer.TButton',background=[('active','#e7edff')])
    style.configure('TransferPrimary.TButton',padding=(18,12),background=blue,foreground='white',borderwidth=0)
    style.map('TransferPrimary.TButton',background=[('active','#244dd0'),('disabled','#a7b6da')])
    header=tk.Frame(root,bg='#172b46'); header.pack(fill='x')
    heading=tk.Frame(header,bg='#172b46'); heading.pack(fill='x',padx=24,pady=(16,0))
    tk.Label(heading,text='手机工作台',font=('Microsoft YaHei UI',22,'bold'),fg='white',bg='#172b46').pack(side='left')
    tk.Label(header,text='扫码连接  ·  文字与图片双向互传',fg='#b9c9e1',bg='#172b46').pack(anchor='w',padx=25,pady=(0,16))
    nav=tk.Frame(root,bg=bg); nav.pack(fill='x',padx=20,pady=(16,12))
    body=tk.Frame(root,bg='white',highlightbackground=line,highlightthickness=1)
    body.pack(fill='both',expand=True,padx=20,pady=(0,12))
    pages={key:tk.Frame(body,bg='white') for key in ('connect','files','text','voice')}
    voice_app=None
    voice_loading=False
    def load_voice():
        nonlocal voice_app,voice_loading
        try:
            config=json.loads((Path(__file__).parent/'desktop.local.json').read_text(encoding='utf-8-sig'))
            script=Path(config['asr_launcher']).parent/'dictation.py'
            spec=importlib.util.spec_from_file_location('phonepad_local_dictation',script)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            voice_app=module.App(host=pages['voice'])
            voice_hint.destroy()
        except Exception as exc:
            if voice_hint.winfo_exists(): voice_hint.configure(text='本地语音暂未启动：'+str(exc))
        finally: voice_loading=False
    voice_hint=tk.Label(pages['voice'],text='正在准备本地语音输入…',bg='white',fg=muted)
    voice_hint.pack(pady=40)
    nav_buttons={}
    def show_page(key):
        nonlocal voice_loading
        for name,page in pages.items():
            page.pack_forget()
            nav_buttons[name].configure(bg=blue if name==key else 'white',fg='white' if name==key else ink)
        pages[key].pack(fill='both',expand=True)
        if key=='text': editor.focus_set()
        if key=='voice' and voice_app is None and not voice_loading:
            voice_loading=True
            root.after(20,load_voice)
    for i,(key,title,hint) in enumerate((('connect','连接手机','扫码互传'),('files','发送文件','图片与文档'),('text','发送文字','粘贴即同步'),('voice','语音输入','F8 开始 / 结束'))):
        nav.columnconfigure(i,weight=1,uniform='navigation')
        btn=tk.Button(nav,text=title+'\n'+hint,command=lambda name=key:show_page(name),font=('Microsoft YaHei UI',11,'bold'),relief='flat',bd=0,pady=13,cursor='hand2',activebackground='#dfe8ff',activeforeground=ink)
        btn.grid(row=0,column=i,sticky='ew',padx=(0,8) if i<3 else 0)
        nav_buttons[key]=btn
    def label(parent,text,size=10,bold=False,fg=ink,**kwargs):
        return tk.Label(parent,text=text,bg='white',fg=fg,font=('Microsoft YaHei UI',size,'bold' if bold else 'normal'),**kwargs)
    connection=pages['connect']; file_page=pages['files']; text_page=pages['text']
    label(connection,'手机扫一扫，直接开始',17,True).pack(anchor='w',padx=22,pady=(18,4))
    label(connection,'首次扫码连接；已连接的手机在原页面继续互传，无需重复扫码。',fg=muted).pack(anchor='w',padx=22)
    qrrow=tk.Frame(connection,bg='white'); qrrow.pack(fill='both',expand=True,padx=20,pady=10)
    canvas=tk.Canvas(qrrow,width=250,height=250,bg='white',highlightthickness=0)
    canvas.pack(side='left',padx=(0,20))
    steps=tk.Frame(qrrow,bg='white'); steps.pack(side='left',fill='both',expand=True)
    label(steps,'手机扫码打开',14,True).pack(anchor='w',pady=(30,10))
    label(steps,'1. 电脑连接手机热点或同一局域网\n\n2. 用手机浏览器扫一扫\n\n3. 保持手机页面打开，即可接收',fg=muted,justify='left',wraplength=300).pack(anchor='w')
    status=tk.StringVar()
    label(connection,'',fg=muted,textvariable=status,wraplength=650).pack(pady=(0,14))
    folder=Path(__file__).parent/'发给手机'
    transfer_status=tk.StringVar(value='支持图片、文档等文件。选好后，手机从原页面下载。')
    results=queue.Queue()
    def open_folder():
        folder.mkdir(exist_ok=True); os.startfile(str(folder))
    def open_received():
        target=Path(__file__).parent/'received_images'
        target.mkdir(exist_ok=True); os.startfile(str(target))
    transferring=False
    def choose_files(paths=None):
        nonlocal transferring
        if transferring:
            transfer_status.set('上一批文件仍在准备，请完成后再添加。'); return
        if paths is None: paths=filedialog.askopenfilenames(parent=root,title='选择要发给手机的文件')
        if not paths: return
        paths=[value for value in paths if Path(value).is_file()]
        if not paths:
            transfer_status.set('请拖入图片或文件，暂不支持整个文件夹。'); return
        show_page('files')
        transferring=True
        send_button.configure(state='disabled'); transfer_status.set('正在准备文件…')
        def copy_files():
            count=0
            try:
                folder.mkdir(exist_ok=True)
                for value in paths:
                    source=Path(value)
                    if source.resolve().parent==folder.resolve(): count+=1; continue
                    target=folder/source.name
                    if target.exists(): target=folder/(source.stem+'-'+secrets.token_hex(3)+source.suffix)
                    with source.open('rb') as src,target.open('xb') as dst: shutil.copyfileobj(src,dst)
                    count+=1
                results.put(f'已准备 {count} 个文件。手机点“从电脑下载文件”，刷新列表后下载。')
            except Exception as exc: results.put(f'已准备 {count} 个文件；其余准备失败：{exc}')
        threading.Thread(target=copy_files,daemon=True).start()
    label(file_page,'把文件发到手机',17,True).pack(anchor='w',padx=24,pady=(14,4))
    label(file_page,'选择电脑上的文件，手机在同一个页面里领取。',fg=muted).pack(anchor='w',padx=24)
    drop_area=tk.Frame(file_page,bg='#f2f6ff',highlightbackground='#bed0fa',highlightthickness=2)
    drop_area.pack(fill='x',padx=24,pady=(10,0))
    tk.Label(drop_area,text='把图片或文件拖进这个窗口',font=('Microsoft YaHei UI',12,'bold'),bg='#f2f6ff',fg=blue).pack(pady=(10,3))
    tk.Label(drop_area,text='拖入即加入待发送列表，也可以用下方按钮选择',bg='#f2f6ff',fg=muted).pack(pady=(0,10))
    send_button=ttk.Button(file_page,text='＋  选择文件发给手机',command=choose_files,style='TransferPrimary.TButton')
    send_button.pack(anchor='w',padx=24,pady=(10,6))
    label(file_page,'',fg=muted,textvariable=transfer_status,wraplength=620,justify='left').pack(anchor='w',padx=24,pady=(0,6))
    tools=tk.Frame(file_page,bg='white'); tools.pack(fill='x',padx=24,pady=4)
    ttk.Button(tools,text='打开待发送文件夹',command=open_folder,style='Transfer.TButton').pack(side='left',padx=(0,12))
    ttk.Button(tools,text='查看手机传来的图片',command=open_received,style='Transfer.TButton').pack(side='left')
    top=tk.Frame(text_page,bg='white'); top.pack(fill='x',padx=22,pady=(18,6))
    label(top,'粘贴文字，手机自动接收',17,True).pack(side='left')
    label(text_page,'在下方按 Ctrl+V，修改后自动同步。手机收到后点“复制文字”。',fg=muted).pack(anchor='w',padx=22,pady=(0,12))
    editor_frame=tk.Frame(text_page,bg='white',highlightbackground=line,highlightthickness=1)
    editor_frame.pack(fill='both',expand=True,padx=22,pady=(0,12))
    editor=tk.Text(editor_frame,wrap='word',font=('Microsoft YaHei UI',12),undo=True,bd=0,padx=14,pady=12,bg='#f8faff',fg=ink,insertbackground=blue,height=5)
    scroll=ttk.Scrollbar(editor_frame,command=editor.yview); scroll.pack(side='right',fill='y')
    editor.configure(yscrollcommand=scroll.set); editor.pack(fill='both',expand=True)
    text_status=tk.StringVar(value='等待粘贴文字 · 手机需保持 PhonePad 页面打开')
    label(text_page,'',fg=muted,textvariable=text_status,wraplength=650).pack(anchor='w',padx=22)
    shared_path=Path(__file__).parent/'.shared-text.local.json'; pending=None
    def publish_text():
        nonlocal pending
        pending=None; content=editor.get('1.0','end-1c')
        if len(content)>100000: text_status.set('文字超过十万字，请缩短后再同步。'); return
        temporary=shared_path.with_name('.shared-text-'+secrets.token_hex(4)+'.tmp')
        try:
            temporary.write_text(json.dumps({'version':secrets.token_hex(12),'text':content},ensure_ascii=False),encoding='utf-8')
            os.replace(temporary,shared_path)
            text_status.set(f'已发布 {len(content)} 字 · 手机页面约两秒内自动接收' if content else '已清空同步文字')
        except OSError as exc:
            text_status.set('同步失败：'+str(exc)); temporary.unlink(missing_ok=True)
    def modified(event=None):
        nonlocal pending
        if not editor.edit_modified(): return
        editor.edit_modified(False)
        if pending is not None: root.after_cancel(pending)
        pending=root.after(600,publish_text)
    try: editor.insert('1.0',json.loads(shared_path.read_text(encoding='utf-8')).get('text',''))
    except (OSError,ValueError): pass
    editor.edit_modified(False); editor.bind('<<Modified>>',modified)
    bottom=tk.Frame(text_page,bg='white'); bottom.pack(fill='x',padx=22,pady=(8,16))
    ttk.Button(bottom,text='清空同步文字',command=lambda:editor.delete('1.0','end'),style='Transfer.TButton').pack(side='left')
    label(bottom,'文字保留至清空',fg=muted).pack(side='right')
    tk.Label(root,text='局域网直连  ·  同一个手机页面接收文字、下载文件、上传图片',bg=bg,fg=muted,font=('Microsoft YaHei UI',9)).pack(pady=(0,12))
    def poll_results():
        nonlocal transferring
        try:
            transfer_status.set(results.get_nowait()); send_button.configure(state='normal'); transferring=False
        except queue.Empty: pass
        root.after(200,poll_results)
    poll_results()
    path=Path(__file__).with_name('手机地址.txt'); last=None
    def refresh():
        nonlocal last
        try:
            urls=addresses(path)
            if not urls: raise ValueError('暂无局域网地址，请启动 PhonePad 并连接手机热点')
            url=urls[0]
            if url!=last:
                cells=matrix(url); canvas.delete('all'); scale=5; offset=(250-37*scale)//2
                for y,row in enumerate(cells):
                    for x,black in enumerate(row):
                        if black: canvas.create_rectangle(offset+x*scale,offset+y*scale,offset+(x+1)*scale,offset+(y+1)*scale,fill='#172b46',outline='')
                last=url
            status.set('电脑地址  '+url.split('/')[2]+'  ·  换网络后自动更新')
        except Exception as exc: canvas.delete('all'); status.set(str(exc)); last=None
        root.after(2000,refresh)
    refresh(); show_page('connect')
    try:
        cleanup_drop=enable_file_drop(root,choose_files)
    except OSError:
        cleanup_drop=lambda: None
        transfer_status.set('系统未能启用文件拖放，请用“选择文件”添加。')
    def close():
        if voice_app is not None: voice_app.close()
        cleanup_drop(); root.destroy()
    root.protocol('WM_DELETE_WINDOW',close)
    root.mainloop()

if __name__=='__main__': main()
