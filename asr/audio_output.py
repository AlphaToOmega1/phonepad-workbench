"""Windows playback inspection and explicit, reversible default-output changes."""
import ctypes as C
from ctypes import wintypes as W
import uuid
import json
import argparse
import winreg
from pathlib import Path

class GUID(C.Structure):
    _fields_=[('data',C.c_ubyte*16)]
    def __init__(self,value):
        super().__init__()
        self.data[:]=uuid.UUID(value).bytes_le

ole=C.OleDLL('ole32')
ole.CoTaskMemFree.argtypes=[C.c_void_p]

def method(obj,index,result,*args):
    table=C.cast(obj,C.POINTER(C.POINTER(C.c_void_p))).contents
    return C.WINFUNCTYPE(result,C.c_void_p,*args)(table[index])

def check(hr):
    if hr<0: raise OSError(f'Windows audio HRESULT: 0x{hr & 0xffffffff:08x}')

def create(clsid,iid):
    obj=C.c_void_p()
    check(ole.CoCreateInstance(C.byref(GUID(clsid)),None,1,C.byref(GUID(iid)),C.byref(obj)))
    return obj

def release(obj):
    if obj: method(obj,2,W.ULONG)(obj)

def device_id(device):
    pointer=C.c_void_p()
    check(method(device,5,C.c_long,C.POINTER(C.c_void_p))(device,C.byref(pointer)))
    try: return C.wstring_at(pointer)
    finally: ole.CoTaskMemFree(pointer)

def label(identifier):
    endpoint=identifier.rsplit('.',1)[-1]
    path=r'SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render'+'\\'+endpoint+'\\Properties'
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,path,0,winreg.KEY_READ|winreg.KEY_WOW64_64KEY) as key:
            return winreg.QueryValueEx(key,'{a45c254e-df1c-4efd-8020-67d146a850e0},2')[0]+' ('+winreg.QueryValueEx(key,'{b3f8fa53-0004-438e-9003-51a46e139bfc},6')[0]+')'
    except OSError: return identifier

def inspect():
    enum=create('bcde0395-e52f-467c-8e3d-c4579291692e','a95664d2-9614-4f35-a746-de8db63617e6')
    result={'defaults':{},'active':[]}
    try:
        for role in range(3):
            device=C.c_void_p()
            check(method(enum,4,C.c_long,C.c_int,C.c_int,C.POINTER(C.c_void_p))(enum,0,role,C.byref(device)))
            try: result['defaults'][str(role)]=device_id(device)
            finally: release(device)
        collection=C.c_void_p()
        check(method(enum,3,C.c_long,C.c_int,W.DWORD,C.POINTER(C.c_void_p))(enum,0,1,C.byref(collection)))
        try:
            count=W.UINT()
            check(method(collection,3,C.c_long,C.POINTER(W.UINT))(collection,C.byref(count)))
            for i in range(count.value):
                device=C.c_void_p()
                check(method(collection,4,C.c_long,W.UINT,C.POINTER(C.c_void_p))(collection,i,C.byref(device)))
                try:
                    identifier=device_id(device)
                    result['active'].append({'id':identifier,'name':label(identifier)})
                finally: release(device)
        finally: release(collection)
        return result
    finally: release(enum)

def set_defaults(mapping):
    policy=create('870af99c-171d-4f9e-af0d-e63df40c2bc9','f8679f50-850a-41cf-9c72-430f290290c8')
    try:
        for role,identifier in mapping.items():
            check(method(policy,13,C.c_long,W.LPCWSTR,C.c_int)(policy,identifier,int(role)))
    finally: release(policy)

class TemporaryMute:
    """Hold the original endpoint and restore its original mute state."""
    def __init__(self):
        self.volume=None
        self.original=W.BOOL()
    def start(self):
        ole.CoInitialize(None)
        enum=None
        device=C.c_void_p()
        volume=C.c_void_p()
        try:
            enum=create('bcde0395-e52f-467c-8e3d-c4579291692e','a95664d2-9614-4f35-a746-de8db63617e6')
            check(method(enum,4,C.c_long,C.c_int,C.c_int,C.POINTER(C.c_void_p))(enum,0,1,C.byref(device)))
            iid=GUID('5cdf2c82-841e-4546-9722-0cf74078229a')
            check(method(device,3,C.c_long,C.POINTER(GUID),W.DWORD,C.c_void_p,C.POINTER(C.c_void_p))(device,C.byref(iid),23,None,C.byref(volume)))
            check(method(volume,15,C.c_long,C.POINTER(W.BOOL))(volume,C.byref(self.original)))
            check(method(volume,14,C.c_long,W.BOOL,C.c_void_p)(volume,True,None))
            self.volume=volume
        except Exception:
            release(volume)
            ole.CoUninitialize()
            raise
        finally:
            release(device)
            release(enum)
    def restore(self):
        if self.volume:
            volume,self.volume=self.volume,None
            try: check(method(volume,14,C.c_long,W.BOOL,C.c_void_p)(volume,self.original,None))
            finally:
                release(volume)
                ole.CoUninitialize()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--realtek',action='store_true')
    parser.add_argument('--rollback',type=Path)
    args=parser.parse_args()
    ole.CoInitialize(None)
    try:
        before=inspect()
        if args.realtek:
            matches=[d for d in before['active'] if 'Realtek' in d['name']]
            if len(matches)!=1: raise RuntimeError('Expected exactly one active Realtek output; no changes made')
            import datetime
            backup=Path(__file__).parent/('audio-backup-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')
            backup.write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf-8')
            try: set_defaults({r:matches[0]['id'] for r in before['defaults']})
            except Exception:
                set_defaults(before['defaults'])
                raise
            print('Backup:',backup)
        elif args.rollback:
            set_defaults(json.loads(args.rollback.read_text(encoding='utf-8'))['defaults'])
        print(json.dumps(inspect(),ensure_ascii=False,indent=2))
    finally: ole.CoUninitialize()
