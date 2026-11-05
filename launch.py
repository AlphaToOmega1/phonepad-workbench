"""Portable Windows entry. Creates only local config; never installs dependencies."""
from pathlib import Path
import importlib.util,json,os,subprocess,sys,time,urllib.request

ROOT=Path(__file__).resolve().parent
def configure():
    desktop=ROOT/'phonepad/desktop.local.json'
    if not desktop.exists():
        desktop.write_text(json.dumps({'asr_launcher':str(ROOT/'asr/start.bat'),'icon_path':str(ROOT/'asr/workbench.ico')},indent=2),encoding='utf-8')
    config=ROOT/'asr/config.local.json'
    if not config.exists():config.write_bytes((ROOT/'asr/config.example.json').read_bytes())

def running():
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open('http://127.0.0.1:8765/ping',timeout=1) as r:
            return r.headers.get('Server','').startswith('PhonePad/') and json.loads(r.read()).get('ok')
    except (OSError,ValueError):return False

def main():
    if os.name!='nt':raise RuntimeError('This application currently requires Windows.')
    configure()
    if not running():
        with (ROOT/'phonepad/runtime.local.log').open('ab') as log:
            subprocess.Popen([sys.executable,'-B',str(ROOT/'phonepad/server.py')],cwd=str(ROOT/'phonepad'),stdout=log,stderr=log,creationflags=0x08000000)
        for _ in range(30):
            if running():break
            time.sleep(.25)
        else:raise RuntimeError('PhonePad 启动失败：请检查端口8765与 phonepad/runtime.local.log。')
    spec=importlib.util.spec_from_file_location('phonepad_gui',ROOT/'phonepad/qr_connect.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.main()

if __name__=='__main__':
    try:main()
    except Exception as exc:
        import tkinter as tk
        from tkinter import messagebox
        root=tk.Tk();root.withdraw();messagebox.showerror('手机工作台启动失败',str(exc),parent=root);root.destroy()
