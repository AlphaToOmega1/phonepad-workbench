import sys,time,queue,threading,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from speech_chunks import SpeechChunker
from dictation import App

RATE=16000
def feed(chunker,seconds,amplitude):
    results=[]
    block=(amplitude*np.sin(2*np.pi*220*np.arange(320)/RATE)).astype(np.float32)
    for _ in range(round(seconds/.02)):results.extend(chunker.feed(block))
    return results

class ContinuousTests(unittest.TestCase):
    def test_quiet_noise_does_not_trigger(self):
        chunker=SpeechChunker(RATE)
        self.assertEqual(feed(chunker,5,.0008),[])
        self.assertIsNone(chunker.flush())
        self.assertLessEqual(chunker.pre_samples,RATE*.3)

    def test_pause_preserves_onset_and_separates_next_sentence(self):
        chunker=SpeechChunker(RATE)
        feed(chunker,.3,0)
        self.assertEqual(feed(chunker,.5,.05),[])
        first=feed(chunker,.9,0)
        self.assertEqual(len(first),1)
        self.assertGreater(np.count_nonzero(first[0]),RATE*.45)
        self.assertEqual(feed(chunker,.4,.05),[])
        second=feed(chunker,.9,0)
        self.assertEqual(len(second),1)
        self.assertGreater(np.count_nonzero(second[0]),RATE*.35)

    def test_stop_flushes_last_words_without_pause(self):
        chunker=SpeechChunker(RATE)
        feed(chunker,.4,.05)
        last=chunker.flush()
        self.assertEqual(last.size,6400)
        self.assertIsNone(chunker.flush())

    def test_capture_continues_while_previous_sentence_decodes(self):
        app=App.__new__(App)
        app.rate=RATE;app.generation=1;app.session_language='zh';app.session_continuous=True
        app.segment_jobs=queue.Queue();app.events=queue.Queue();app.segment_pending=0
        app.segment_lock=threading.Lock();app.inference_lock=threading.Lock()
        app.chunker=SpeechChunker(RATE);app.samples=0;app.peak=0;app.last_audio=0;app.polisher=None
        entered=threading.Event();release=threading.Event();calls=[]
        class Model:
            def transcribe(self,audio,**options):
                calls.append(options);entered.set()
                if not release.wait(5):raise TimeoutError('test decode blocked')
                return [SimpleNamespace(text='句子'+str(len(calls)))],None
        app.model=Model()
        threading.Thread(target=app.segment_worker,daemon=True).start()
        voice=(.05*np.sin(2*np.pi*220*np.arange(320)/RATE)).astype(np.float32).reshape(-1,1)
        quiet=np.zeros((320,1),dtype=np.float32)
        def sentence():
            for block,count in ((voice,25),(quiet,45)):
                for _ in range(count):app.capture(block,len(block),None,None)
        sentence();self.assertTrue(entered.wait(3))
        sentence()
        self.assertEqual(app.segment_pending,2)
        self.assertEqual(app.samples,44800)
        release.set()
        results=[app.events.get(timeout=5),app.events.get(timeout=5)]
        self.assertEqual([event[1][1] for event in results],['句子1','句子2'])
        self.assertTrue(all(options['beam_size']==1 and options['vad_filter'] for options in calls))

    def test_cancel_pending_last_sentence_does_not_stay_busy(self):
        app=App.__new__(App)
        app.generation=7;app.session_continuous=True;app.busy=True;app.decode_started=1
        app.segment_lock=threading.Lock();app.segment_pending=2;app.stream=None
        app.text=SimpleNamespace(delete=lambda *args:None)
        app.status=SimpleNamespace(set=lambda text:None);app.state=SimpleNamespace(set=lambda text:None)
        app.hint=lambda *args:None
        app.clear_round()
        self.assertFalse(app.busy);self.assertEqual(app.segment_pending,0)
        app.events=queue.Queue();app.events.put(('segment-result',(7,'旧结果',1,None)))
        app.root=SimpleNamespace(after=lambda *args:None)
        app.poll()
        self.assertFalse(app.busy)

if __name__=='__main__':unittest.main()
