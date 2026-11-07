# -*- coding: utf-8 -*-
"""
PhonePad —— 把手机上的文字，直接打进电脑当前窗口。

纯本地：手机浏览器 -> 局域网 -> 本机，不经过任何外部服务器。
文字用 Unicode 直接注入（SendInput），不占用剪贴板，不依赖输入法。

用法：
    python server.py            # 默认 0.0.0.0:8765
    python server.py --dry-run  # 只打印，不真的打字（自测用）
    python server.py --port 9000
"""

import argparse
import html
import hashlib
import io
import shutil
import ctypes
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
def load_access_token():
    path = HERE / '.access-token.local'
    if path.exists():
        token = path.read_text(encoding='utf-8').strip()
        if token: return token
    token = secrets.token_urlsafe(24)
    path.write_text(token, encoding='utf-8')
    return token

TOKEN = load_access_token()
DRY_RUN = False

LOG_FILE = HERE / "phone.log"
LOG_MAX = 512 * 1024    # 超过 512KB 就截断重来


def log_meta(msg: str) -> None:
    """只记时间、来源 IP、字数这类元信息，绝不记你说了什么。"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX:
            LOG_FILE.unlink()
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def utf8_console() -> None:
    """让 Windows 控制台正确显示中文。"""
    if os.name != "nt":
        return
    os.system("chcp 65001 >nul 2>&1")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------- 最近发送

RECENT: list[dict] = []      # 只在内存里，不写磁盘
RECENT_MAX = 6
RECENT_LOCK = threading.Lock()
SENT_LOCK = threading.Lock()
TEXT_LOCK = threading.Lock()
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')

def sent_status():
    folder=HERE/'发给手机'
    records=[]
    if folder.exists():
        for item in folder.iterdir():
            if item.name.startswith('.') or item.is_symlink() or not item.is_file():continue
            try:
                stat=item.stat();records.append((item.name,stat.st_size,stat.st_mtime_ns))
            except FileNotFoundError:continue
    encoded=json.dumps(sorted(records),ensure_ascii=False).encode('utf-8')
    return {'ok':True,'count':len(records),'version':hashlib.sha256(encoded).hexdigest()}


def remove_sent_files(name=None, clear_images=False, clear_all=False):
    """只移动待发送副本，回收目录保留原文件名。"""
    folder = HERE / '发给手机'
    trash = HERE / '.removed_sent'
    if folder.is_symlink() or trash.is_symlink():
        raise ValueError('目录无效')
    with SENT_LOCK:
        folder.mkdir(exist_ok=True)
        if clear_all:
            items = [p for p in folder.iterdir() if not p.name.startswith('.') and not p.is_symlink() and p.is_file()]
        elif clear_images:
            items = [p for p in folder.iterdir() if not p.is_symlink() and p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
        else:
            if not isinstance(name, str) or not name or Path(name).name != name:
                raise ValueError('文件名无效')
            item = folder / name
            if item.is_symlink() or item.resolve().parent != folder.resolve():
                raise ValueError('文件名无效')
            if not item.is_file():
                raise FileNotFoundError('文件已移除，请刷新列表')
            items = [item]
        if not items: return 0, 0
        destination = trash / (time.strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(3))
        destination.mkdir(parents=True)
        removed = failed = 0
        for item in items:
            try:
                item.rename(destination / item.name)
                removed += 1
            except OSError:
                failed += 1
        return removed, failed


def remember(text: str, ip: str) -> None:
    """记下最近发过的内容，万一打丢了能一键重发。"""
    with RECENT_LOCK:
        RECENT.insert(0, {
            "t": time.strftime("%H:%M:%S"),
            "text": text,
            "len": len(text),
        })
        del RECENT[RECENT_MAX:]

# ---------------------------------------------------------------- 文字注入

user32 = ctypes.WinDLL("user32", use_last_error=True)

KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(ch: str, keyup: bool) -> None:
    code = ord(ch)
    # 非 BMP 字符拆成代理对
    units = [code] if code <= 0xFFFF else [
        0xD800 + ((code - 0x10000) >> 10),
        0xDC00 + ((code - 0x10000) & 0x3FF),
    ]
    for u in units:
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki = KEYBDINPUT(0, u, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if keyup else 0), 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


# ---------------------------------------------------------------- 前台窗口

class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


# 这些前台窗口肯定收不了字：桌面、任务栏、开始菜单
NON_INPUT_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow", "ForegroundStaging",
}


def foreground_info() -> dict:
    """返回当前前台窗口的类名、标题，以及是否检测到光标（插入符）。"""
    info = {"hwnd": 0, "cls": "", "title": "", "caret": False}
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return info
        info["hwnd"] = int(hwnd)

        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        info["cls"] = buf.value

        buf2 = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf2, 512)
        info["title"] = buf2.value

        pid = wintypes.DWORD()
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        gui = GUITHREADINFO()
        gui.cbSize = ctypes.sizeof(GUITHREADINFO)
        if tid and user32.GetGUIThreadInfo(tid, ctypes.byref(gui)):
            info["caret"] = bool(gui.hwndCaret) or bool(gui.hwndFocus)
    except Exception:                                   # noqa: BLE001
        pass
    return info


def cannot_type(info: dict) -> str:
    """返回拒绝理由；空字符串表示可以打。"""
    if not info["hwnd"]:
        return "没有前台窗口"
    if info["cls"] in NON_INPUT_CLASSES:
        return "焦点在桌面或任务栏"
    return ""


def type_text(text: str) -> None:
    """逐字注入，遇到换行发 Enter。"""
    if DRY_RUN:
        print(f"[dry-run] 收到 {len(text)} 字（未输入）", flush=True)
        return

    for ch in text:
        if ch in "\r\n":
            _send("\r", False)
            _send("\r", True)
        else:
            _send(ch, False)
            _send(ch, True)
        time.sleep(0.001)


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = "PhonePad/1.0"

    def _send_bytes(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict) -> None:
        self._send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == '/send-images':
            from urllib.parse import parse_qs
            query = parse_qs(self.path.split('?', 1)[1] if '?' in self.path else '')
            if query.get('t', [''])[0] != TOKEN:
                self._send_bytes(403, '连接参数已失效，请返回原手机输入页，点“传图”重新进入。'.encode('utf-8'), 'text/plain; charset=utf-8')
                return
            self._send_bytes(200, (HERE / 'send_images.html').read_bytes(), 'text/html; charset=utf-8')
            return
        if path == "/shared-text":
            from urllib.parse import parse_qs
            query = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            if query.get("t", [""])[0] != TOKEN:
                self._send_json(403, {"ok": False, "error": "token"})
                return
            try:
                shared = json.loads((HERE / '.shared-text.local.json').read_text(encoding='utf-8'))
            except FileNotFoundError:
                shared = {"version": "", "text": ""}
            except (OSError, ValueError):
                self._send_json(503, {"ok": False, "error": "文字暂时不可用"})
                return
            self._send_json(200, {"ok": True, "version": shared.get("version", ""), "text": shared.get("text", "")})
            return
        if path in ("/downloads", "/download", "/preview", "/sent-status"):
            from urllib.parse import parse_qs, quote
            query = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            if query.get("t", [""])[0] != TOKEN:
                self._send_json(403, {"ok": False, "error": "token"})
                return
            folder = HERE / "发给手机"
            folder.mkdir(exist_ok=True)
            if path=='/sent-status':
                self._send_json(200,sent_status())
                return
            if path == "/downloads":
                links = []
                def added(item):
                    stat = item.stat()
                    return stat.st_ctime if os.name == 'nt' else stat.st_mtime
                candidates = [p for p in folder.iterdir() if not p.name.startswith('.') and not p.is_symlink() and p.is_file()]
                for item in sorted(candidates, key=lambda p: (added(p), p.name.lower()), reverse=True):
                    if item.is_file() and not item.is_symlink():
                        url = "/download?t=" + quote(TOKEN) + "&name=" + quote(item.name)
                        preview = "/preview?t=" + quote(TOKEN) + "&name=" + quote(item.name)
                        stat = item.stat()
                        size = f'{stat.st_size / (1024 * 1024):.1f} MB' if stat.st_size >= 1024 * 1024 else f'{max(1, stat.st_size // 1024)} KB'
                        stamp = time.strftime('%m-%d %H:%M:%S', time.localtime(added(item)))
                        is_image = item.suffix.lower() in IMAGE_SUFFIXES
                        picture = ('<a class="picture" target="_blank" rel="noopener" href="' + html.escape(preview + '&full=1', quote=True) + '"><img loading="lazy" src="' + html.escape(preview, quote=True) + '" alt="图片预览" onerror="this.outerHTML=\'<span>预览暂不可用，可下载原图</span>\'"></a>') if is_image else '<div class="picture placeholder">文件</div>'
                        remove_button = '<button class="remove" data-name="' + html.escape(item.name, quote=True) + '" onclick="removeFile(this.dataset.name)">移除</button>'
                        links.append('<article>' + picture + '<div class="info"><div class="name">' + html.escape(item.name) + '</div><p class="meta">加入于 ' + stamp + ' · ' + size + '</p><a class="download" href="' + html.escape(url, quote=True) + '">' + ('下载原图' if is_image else '下载文件') + '</a> ' + remove_button + '</div></article>')
                page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>电脑发来的文件</title><style>
*{box-sizing:border-box}body{margin:0;background:#f2f5fa;color:#172b46;font:16px/1.5 system-ui,sans-serif;padding:22px 16px}header{max-width:860px;margin:0 auto 20px}h1{font-size:24px;margin:0 0 6px}.hint,.meta{color:#66758a;font-size:13px}.toolbar{display:flex;gap:12px;align-items:center;margin-top:14px}button,.download{border:0;border-radius:10px;padding:10px 18px;background:#315fe9;color:white;font:inherit;text-decoration:none;display:inline-block}button{cursor:pointer}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,250px),1fr));gap:16px;max-width:860px;margin:auto}article{background:white;border:1px solid #e2e8f1;border-radius:16px;overflow:hidden;min-width:0}.picture{display:flex;align-items:center;justify-content:center;height:220px;background:#e9eef5;color:#66758a;text-decoration:none;padding:8px}.picture img{width:100%;height:100%;object-fit:contain}.placeholder{font-size:32px}.info{padding:16px}.name{font-weight:600;overflow-wrap:anywhere}.meta{margin:6px 0 14px}.empty{padding:30px;background:white;border-radius:14px}.toolbar a{color:#315fe9;text-decoration:none}
</style><header><h1>电脑发来的文件</h1><div class="hint">最新加入的在最前 · 点图片查看大图，确认后下载原图</div><div class="toolbar"><button onclick="location.reload()">刷新列表</button><a href="/?t=''' + quote(TOKEN) + '''">返回输入页面</a></div></header><main class="grid">''' + (''.join(links) or '<p class="empty">暂无文件。在电脑选择文件后，点击上方刷新。</p>') + '</main></html>'
                page = page.replace('</style>', '.remove{background:#fff0f0;color:#ad3434;border:1px solid #f2cece}.toolbar{flex-wrap:wrap}button:disabled{opacity:.5}</style>')
                page = page.replace('<button onclick="location.reload()">刷新列表</button>', '<button onclick="location.reload()">刷新列表</button><button class="remove" onclick="clearFiles()">清空全部文件</button>')
                script = '''<script>
const localToken = ''' + json.dumps(TOKEN) + ''';
let removing = false;
let listVersion = '';
let refreshingList = false;
async function refreshList() {
  if (document.hidden || removing || refreshingList) return;
  refreshingList = true;
  try {
    const response = await fetch('/sent-status?t='+encodeURIComponent(localToken),{cache:'no-store'});
    if (!response.ok) throw new Error('HTTP '+response.status);
    const info = await response.json();
    if (info.version !== listVersion) {
      const page = await fetch(location.href,{cache:'no-store'});
      if (!page.ok) throw new Error('HTTP '+page.status);
      const parsed = new DOMParser().parseFromString(await page.text(),'text/html');
      if (!removing) {
        document.querySelector('.grid').replaceChildren(...parsed.querySelector('.grid').childNodes);
        listVersion = info.version;
      }
    }
    document.getElementById('liveState').textContent='自动更新 · '+info.count+' 个待接收文件';
  } catch(e) { document.getElementById('liveState').textContent='连接暂时中断，恢复后自动重试'; }
  finally { refreshingList = false; }
}
setInterval(refreshList,2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshList();});
refreshList();
async function removeSent(payload) {
  if (removing) return;
  removing = true;
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const response = await fetch('/remove_sent', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...payload,token:localToken})});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || '移除失败');
    if (result.failed) alert('已移除 ' + result.removed + ' 项，部分文件正在使用，请稍后重试。');
    location.reload();
  } catch (e) {
    alert(e.message);
    removing = false;
    document.querySelectorAll('button').forEach(b => b.disabled = false);
  }
}
function removeFile(name) {
  if (confirm('从待发送列表移除这项？电脑原文件不会被删除。\\n' + name)) removeSent({name});
}
function clearFiles() {
  if (confirm('清空全部待接收文件？包括图片、PDF、电子书和其他文件。仅移除电脑待发送副本，电脑原文件及手机已下载文件不受影响。')) removeSent({action:'clear_all'});
}
</script>'''
                page = page.replace('</main></html>', '</main>' + script + '</html>')
                page = page.replace('<div class="toolbar">','<p class="hint" id="liveState">自动更新中…</p><div class="toolbar">')
                self._send_bytes(200, page.encode('utf-8'), 'text/html; charset=utf-8')
                return
            name = query.get("name", [""])[0]
            item = folder / name
            if not name or Path(name).name != name or item.is_symlink() or item.resolve().parent != folder.resolve() or not item.is_file():
                self._send_bytes(404, b"not found", "text/plain")
                return
            if path == '/preview':
                with item.open('rb') as source:
                    signature = source.read(16)
                kind = ('image/png' if signature.startswith(b'\x89PNG\r\n\x1a\n') else
                        'image/jpeg' if signature.startswith(b'\xff\xd8\xff') else
                        'image/gif' if signature.startswith((b'GIF87a', b'GIF89a')) else
                        'image/webp' if signature[:4] == b'RIFF' and signature[8:12] == b'WEBP' else
                        'image/bmp' if signature.startswith(b'BM') else None)
                if not kind:
                    self._send_bytes(415, b'not a supported image', 'text/plain')
                    return
                if query.get('full', [''])[0] != '1':
                    try:
                        from PIL import Image, ImageOps
                        with Image.open(item) as original:
                            thumb = ImageOps.exif_transpose(original)
                            thumb.thumbnail((640, 480))
                            if thumb.mode in ('RGBA', 'LA') or 'transparency' in thumb.info:
                                rgba = thumb.convert('RGBA')
                                thumb = Image.new('RGB', rgba.size, 'white')
                                thumb.paste(rgba, mask=rgba.getchannel('A'))
                            else:
                                thumb = thumb.convert('RGB')
                            buffer = io.BytesIO()
                            thumb.save(buffer, format='JPEG', quality=82)
                        self._send_bytes(200, buffer.getvalue(), 'image/jpeg')
                        return
                    except Exception:
                        pass  # 现有环境没有缩略图支持时，仍可预览原图。
                with item.open('rb') as stream:
                    self.send_response(200)
                    self.send_header('Content-Type', kind)
                    self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    shutil.copyfileobj(stream, self.wfile)
                return
            with item.open('rb') as stream:
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(item.name))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                shutil.copyfileobj(stream, self.wfile)
            return
        if path in ("/", "/index.html"):
            page_bytes = (HERE / "index.html").read_bytes()
            self._send_bytes(200, page_bytes, "text/html; charset=utf-8")
            ip = self.client_address[0]
            if not ip.startswith("127."):
                print(f"[{time.strftime('%H:%M:%S')}] 手机打开了页面：{ip}", flush=True)
                log_meta(f"打开页面  {ip}")
        elif path == "/ping":
            self._send_json(200, {"ok": True, "pid": __import__("os").getpid()})
        elif path == "/recent":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            token = ""
            for part in qs.split("&"):
                if part.startswith("t="):
                    token = part[2:]
            if token != TOKEN:
                self._send_json(403, {"ok": False, "error": "token"})
                return
            self._send_json(200, {"ok": True, "items": list(RECENT)})
        else:
            self._send_bytes(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        route = self.path.split("?")[0]
        if route == "/upload":
            from urllib.parse import parse_qs
            token = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "").get("t", [""])[0]
            if token != TOKEN:
                self._send_json(403, {"ok": False, "error": "token"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= 20 * 1024 * 1024:
                self._send_json(413, {"ok": False, "error": "图片需小于 20 MB"})
                return
            self.connection.settimeout(60)
            try:
                image = self.rfile.read(length)
            except OSError:
                self._send_json(408, {"ok": False, "error": "传输超时，请保持手机页面打开后重试"})
                return
            if len(image) != length:
                self._send_json(400, {"ok": False, "error": "图片没有完整收到，请重试"})
                return
            if image.startswith(b"\x89PNG\r\n\x1a\n"):
                ext = ".png"
            elif image.startswith(b"\xff\xd8\xff"):
                ext = ".jpg"
            elif image[:4] == b"RIFF" and image[8:12] == b"WEBP":
                ext = ".webp"
            else:
                self._send_json(400, {"ok": False, "error": "请选择 PNG、JPG 或 WebP 图片"})
                return
            folder = HERE / "received_images"
            try:
                folder.mkdir(exist_ok=True)
                name = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3) + ext
                (folder / name).write_bytes(image)
            except OSError as exc:
                self._send_json(500, {"ok": False, "error": f"保存失败：{exc}"})
                return
            log_meta(f"收到图片 {len(image)} 字节  来自 {self.client_address[0]}")
            self._send_json(200, {"ok": True, "name": name})
            return

        if route not in ("/type", "/recent_clear", "/remove_sent", "/shared-text-clear"):
            self._send_bytes(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if route in ('/remove_sent', '/shared-text-clear') and not 0 < length <= 16384:
                self._send_json(413, {"ok": False, "error": "请求过大或为空"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:                       # noqa: BLE001
            self._send_json(400, {"ok": False, "error": f"bad request: {exc}"})
            return

        if not isinstance(data, dict) or data.get("token") != TOKEN:
            self._send_json(403, {"ok": False, "error": "token"})
            return

        if route == '/shared-text-clear':
            with TEXT_LOCK:
                shared_path=HERE/'.shared-text.local.json'
                temporary=HERE/('.shared-text-'+secrets.token_hex(8)+'.tmp')
                try:
                    current=json.loads(shared_path.read_text(encoding='utf-8')) if shared_path.exists() else {'version':''}
                    if not isinstance(current,dict):raise ValueError('invalid shared text')
                    if data.get('version') != current.get('version',''):
                        self._send_json(409,{'ok':False,'error':'电脑文字刚刚更新，请查看新内容后再点清空'})
                        return
                    cleared={'version':secrets.token_hex(12),'text':'','source':'phone-clear'}
                    temporary.write_text(json.dumps(cleared),encoding='utf-8')
                    os.replace(temporary,shared_path)
                except (OSError,ValueError):
                    self._send_json(503,{'ok':False,'error':'暂时无法清空，请稍后重试'})
                    return
                finally:temporary.unlink(missing_ok=True)
            self._send_json(200,{'ok':True,**cleared})
            return

        if route == '/remove_sent':
            if data.get('action') not in (None, 'clear_images', 'clear_all'):
                self._send_json(400, {"ok": False, "error": "操作无效"})
                return
            try:
                removed, failed = remove_sent_files(data.get('name'), data.get('action') == 'clear_images', data.get('action') == 'clear_all')
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            except FileNotFoundError as exc:
                self._send_json(404, {"ok": False, "error": str(exc)})
                return
            except OSError:
                self._send_json(409, {"ok": False, "error": "暂时无法移除，请稍后重试"})
                return
            log_meta(f'移除待发送副本 {removed} 项，失败 {failed} 项')
            self._send_json(200, {"ok": True, "removed": removed, "failed": failed})
            return

        if route == "/recent_clear":
            with RECENT_LOCK:
                RECENT.clear()
            print(f"[{time.strftime('%H:%M:%S')}] 最近记录已清空", flush=True)
            log_meta("清空最近记录")
            self._send_json(200, {"ok": True})
            return

        text = (data.get("text") or "").strip()
        if not text:
            self._send_json(200, {"ok": True, "typed": 0})
            return

        # 先看一眼焦点在不在能收字的地方，不在就别打，也别让手机清空
        info = foreground_info()
        reason = cannot_type(info)
        if reason:
            print(f"[{time.strftime('%H:%M:%S')}] 拒收（{reason}）：焦点是「{info['title'] or info['cls']}」",
                  flush=True)
            log_meta(f"拒收 {len(text):>3} 字  原因：{reason}")
            self._send_json(200, {
                "ok": False, "blocked": True, "reason": reason,
                "window": info["title"] or info["cls"],
            })
            return

        try:
            type_text(text)
        except Exception as exc:                       # noqa: BLE001
            self._send_json(500, {"ok": False, "error": str(exc)})
            return

        remember(text, self.client_address[0])

        ip = self.client_address[0]
        print(f"[{time.strftime('%H:%M:%S')}] 来自 {ip} 上屏 {len(text)} 字", flush=True)
        log_meta(f"上屏 {len(text):>3} 字  来自 {ip}")
        self._send_json(200, {
            "ok": True, "typed": len(text),
            "window": info["title"] or info["cls"],
            "caret": info["caret"],
        })

    def log_message(self, fmt, *args):                 # 安静一点
        return


def _ipconfig_ips() -> set[str]:
    """把 ipconfig 里所有 IPv4 都捞出来，覆盖热点、USB 共享网络这类适配器。"""
    found: set[str] = set()
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, timeout=6).stdout
        text = out.decode("mbcs", "ignore")
    except Exception:                                   # noqa: BLE001
        return found
    for m in re.finditer(r"IPv4[^\n\r]*?:\s*(\d{1,3}(?:\.\d{1,3}){3})", text):
        found.add(m.group(1))
    return found


def local_ips() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))                   # 不会真的发包，只为取出口网卡
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ips.add(info[4][0])
    ips |= _ipconfig_ips()
    return sorted(i for i in ips if not i.startswith("127.") and not i.startswith("169.254."))


def selftest() -> int:
    """3 秒后把一行测试文字打到当前光标处，用来验证注入是否正常。"""
    sample = "自检成功：中文、English、数字 123。"
    print("3 秒内把光标点进记事本，然后不要动……", flush=True)
    for i in (3, 2, 1):
        print(f"  {i}", flush=True)
        time.sleep(1)
    type_text(sample)
    print("已发送。如果记事本里出现了上面这句话，就说明注入正常。", flush=True)
    return 0


class PhonePadServer(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 会让第二个进程也能绑同一个端口，
    # 结果是两个实例抢连接。这里必须关掉。
    allow_reuse_address = False
    daemon_threads = True


def already_running(port: int) -> bool:
    """已经有实例在跑吗？"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5) as s:
            s.settimeout(1.5)
            s.sendall(b"GET /ping HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            buf = b""
            while len(buf) < 4096:
                chunk = s.recv(1024)
                if not chunk:
                    break
                buf += chunk
                if b'"ok"' in buf:
                    return True
            return b'"ok"' in buf
    except OSError:
        return False


def main() -> int:
    global DRY_RUN
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="把一行测试文字打到当前光标处")
    args = ap.parse_args()
    DRY_RUN = args.dry_run

    if args.selftest:
        return selftest()

    if already_running(args.port):
        print(f"\n手机输入板已经在运行了（端口 {args.port}）。")
        print("地址见同目录下的「手机地址.txt」")
        print("要重启就先双击 stop.bat。")
        time.sleep(3)
        return 0

    try:
        srv = PhonePadServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"\n端口 {args.port} 被占用了（可能已经有一个在跑）：{exc}")
        print("地址见同目录下的「手机地址.txt」")
        time.sleep(3)
        return 0

    # 绑定成功后再写地址，避免启动失败也覆盖掉
    def write_address(ips: list[str]) -> list[str]:
        lines = [f"http://{ip}:{args.port}/?t={TOKEN}" for ip in ips]
        try:
            (HERE / "手机地址.txt").write_text(
                "手机浏览器打开下面这个地址（手机要和电脑在同一个网络里）：\n\n"
                + "\n".join(lines + [f"http://127.0.0.1:{args.port}/?t={TOKEN}"])
                + "\n", encoding="utf-8-sig")
        except OSError:
            pass
        return lines

    def watch_network() -> None:
        """换 WiFi、插热点、USB 共享网络时，地址会自动跟着更新。"""
        last = tuple(ips)
        while True:
            time.sleep(5)
            try:
                now = tuple(local_ips())
            except Exception:                          # noqa: BLE001
                continue
            if now != last:
                last = now
                changed = write_address(list(now))
                stamp = time.strftime("%H:%M:%S")
                if changed:
                    print(f"\n[{stamp}] 网络变了，新地址：")
                    for line in changed:
                        print(f"  {line}")
                    print()
                log_meta(f"网络变化  新地址 {', '.join(changed) if changed else '无'}")

    ips = local_ips()
    lines = write_address(ips)

    print("=" * 56)
    print(" PhonePad 已启动" + ("  [dry-run 不真的打字]" if DRY_RUN else ""))
    print("=" * 56)
    for line in lines:
        print(f"  手机浏览器打开： {line}")
    print(f"  本机自测：       http://127.0.0.1:{args.port}/?t={TOKEN}")
    print("  Ctrl+C 退出")
    print("=" * 56)
    log_meta(f"启动  端口 {args.port}  地址 {', '.join(lines) if lines else '无'}")

    threading.Thread(target=watch_network, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
