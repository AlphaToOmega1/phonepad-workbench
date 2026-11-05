import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json
import unittest
from local_corrector import correct,NoRedirect
from text_polish import TextPolisher

class Reply:
    def __init__(self,text): self.text=text
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self,*args): return json.dumps({'message':{'content':self.text}}).encode()
class FakeEndpoint:
    def __init__(self,text): self.text=text; self.request=None
    def open(self,request,timeout):
        self.request=request
        return Reply(self.text)

class CoreTests(unittest.TestCase):
    def test_script_and_existing_punctuation(self):
        p=TextPolisher('.',load_punctuation=False)
        self.assertEqual(p.apply('我是廣西人，今天測試一下。'),'我是广西人，今天测试一下。')
        self.assertEqual(p.apply('這個怎麼用'),'这个怎么用。')
        self.assertEqual(p.apply('Whisper 3.14'),'Whisper 3.14')
    def test_correction_is_loopback_and_preserves_digits(self):
        endpoint=FakeEndpoint('今天测试 123 次。')
        self.assertEqual(correct('今天測試 123 次',opener=endpoint),'今天测试 123 次。')
        self.assertEqual(endpoint.request.full_url,'http://127.0.0.1:11434/api/chat')
        body=json.loads(endpoint.request.data)
        self.assertFalse(body['stream'])
        self.assertEqual(body['options']['num_gpu'],0)
    def test_changed_number_is_rejected(self):
        with self.assertRaises(ValueError): correct('测试 12 次',opener=FakeEndpoint('测试 20 次。'))
    def test_empty_or_excessive_output_rejected(self):
        for answer in ('','啊'*1000):
            with self.assertRaises(ValueError): correct('今天测试',opener=FakeEndpoint(answer))
    def test_redirect_is_rejected(self):
        with self.assertRaises(RuntimeError): NoRedirect().redirect_request(None,None,None,None,None,None)

if __name__=='__main__': unittest.main()
