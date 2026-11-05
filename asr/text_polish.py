"""Offline script conversion and optional punctuation-only restoration."""
from pathlib import Path
import re
from opencc import OpenCC

class TextPolisher:
    def __init__(self,model_path,load_punctuation=True):
        self.converter=OpenCC('t2s')
        self.punctuation=None
        self.note='简体转换已开启；使用 Whisper 标点，缺失的中文句末标点自动补全'
        self.path=Path(model_path).parent/'punc_ct-transformer_zh-cn-common-vocab272727-pytorch'
        if load_punctuation: self.load_punctuation()

    def load_punctuation(self):
        path=self.path
        if not all((path/f).is_file() for f in ('model.pt','config.yaml','tokens.json')): return
        try:
            import yaml
            from funasr import AutoModel
            config=yaml.safe_load((path/'config.yaml').read_text(encoding='utf-8'))
            if 'model_conf' not in config: raise ValueError('Missing local model configuration')
            config['init_param']=str(path/'model.pt')
            config['tokenizer_conf']['token_list']=str(path/'tokens.json')
            config.update(device='cpu',ncpu=2,disable_pbar=True,disable_log=True)
            # model_conf bypasses FunASR's download/config-discovery path.
            punctuation=AutoModel(**config)
            punctuation.generate(input='你好今天测试语音输入你听清楚了吗',disable_pbar=True)
            self.punctuation=punctuation
            self.note='简体转换和本地标点模型已就绪'
        except Exception as exc:
            self.note='简体转换已开启；本地标点模型不可用：'+type(exc).__name__
            self.punctuation=None

    def apply(self,text):
        text=self.converter.convert(text).strip()
        if not text or not re.search(r'[\u4e00-\u9fff]',text): return text
        if not self.punctuation:
            # Only add a missing final full stop; do not guess internal grammar.
            if not re.search(r'[:/\\=<>`\n]',text) and re.search(r'[\u4e00-\u9fff]$',text):
                text+='。'
            return text
        # Preserve existing punctuation and code/URLs rather than rewriting them.
        if re.search(r'[，。？！；：!?;:/\\=<>\n]',text): return text
        try:
            result=self.punctuation.generate(input=text,disable_pbar=True)[0]['text']
            strip=lambda s:re.sub(r'[\s，。？！、,.?!]','',s)
            # Accept punctuation insertion only, never silently change words.
            if strip(result)==strip(text): return result
        except Exception: pass
        return text
