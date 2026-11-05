"""Read-only diagnostics: no recording, downloads, clipboard or network requests."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import json
from importlib.metadata import version,PackageNotFoundError

def main():
    print('Python:',sys.version.split()[0])
    print('Executable:',sys.executable)
    for name in ('faster-whisper','ctranslate2','sounddevice','numpy','OpenCC'):
        try: print(name,version(name))
        except PackageNotFoundError: print(name,'NOT INSTALLED')
    config=Path(__file__).with_name('config.local.json')
    try:
        cfg=json.loads(config.read_text(encoding='utf-8')) if config.exists() else {}
        model=Path(__file__).parent/cfg.get('model_path','models/faster-whisper-large-v3-turbo')
        print('Local model weights present:',(model/'model.bin').is_file())
        print('Compute:',cfg.get('device','cuda'),cfg.get('compute_type','float16'))
    except Exception as exc: print('Configuration error:',type(exc).__name__)
    try:
        import sounddevice as sd
        print('Audio input devices (not opened):')
        for i,d in enumerate(sd.query_devices()):
            if d['max_input_channels']>0 and ('AudioRelay' in d['name'] or ('Realtek' in d['name'] and sd.query_hostapis(d['hostapi'])['name']=='Windows WASAPI')):
                print(i,d['name'],sd.query_hostapis(d['hostapi'])['name'])
    except Exception as exc: print('Audio enumeration error:',type(exc).__name__)
    print('No microphone recording, model loading, network or clipboard access performed.')

if __name__=='__main__': main()
