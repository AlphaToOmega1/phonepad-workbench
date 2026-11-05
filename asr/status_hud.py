"""A small Windows status indicator that does not activate its window."""
import ctypes
from ctypes import wintypes
import tkinter as tk

class StatusHUD:
    def __init__(self,root):
        self.root=root
        self.window=tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.label=tk.Label(self.window,font=('Microsoft YaHei UI',12,'bold'),
                            bg='#202735',fg='white',padx=18,pady=9)
        self.label.pack()
        self.window.update_idletasks()
        self.u=ctypes.WinDLL('user32',use_last_error=True)
        self.u.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
        self.u.GetAncestor.restype=wintypes.HWND
        self.hwnd=self.u.GetAncestor(self.window.winfo_id(),2)
        self.u.GetWindowLongW.argtypes=[wintypes.HWND,ctypes.c_int]
        self.u.SetWindowLongW.argtypes=[wintypes.HWND,ctypes.c_int,ctypes.c_long]
        style=self.u.GetWindowLongW(self.hwnd,-20)
        self.u.SetWindowLongW(self.hwnd,-20,style|0x08000000|0x80|0x20)
        self.u.SetWindowPos.argtypes=[wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT]
        self.timer=None
    def show(self,text,seconds=None):
        if self.timer: self.root.after_cancel(self.timer); self.timer=None
        self.label.configure(text=text)
        self.window.update_idletasks()
        width=self.window.winfo_reqwidth(); height=self.window.winfo_reqheight()
        x=max(0,(self.root.winfo_screenwidth()-width)//2)
        self.u.SetWindowPos(self.hwnd,wintypes.HWND(-1),x,30,width,height,0x10|0x40)
        if seconds: self.timer=self.root.after(int(seconds*1000),self.hide)
    def hide(self):
        self.u.ShowWindow.argtypes=[wintypes.HWND,ctypes.c_int]
        self.u.ShowWindow(self.hwnd,0)
        self.timer=None
