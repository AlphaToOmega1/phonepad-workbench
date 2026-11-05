"""Lightweight energy endpointing, not speaker identification. Audio stays in RAM."""
from collections import deque
import numpy as np

class SpeechChunker:
    def __init__(self, rate, silence=0.8, max_seconds=15):
        self.rate=rate
        self.silence=int(rate*silence)
        self.limit=int(rate*max_seconds)
        self.pre_limit=int(rate*0.25)
        self.tail=int(rate*0.2)
        self.pre=deque();self.pre_samples=0
        self.parts=[];self.samples=0;self.last_voice=0
        self.noise=0.0005

    def feed(self, data):
        data=np.asarray(data,dtype=np.float32).reshape(-1).copy()
        rms=float(np.sqrt(np.mean(data*data))) if data.size else 0
        voiced=rms>=max(0.003,self.noise*3)
        if not self.parts:
            self.pre.append(data);self.pre_samples+=data.size
            while self.pre and self.pre_samples-self.pre[0].size>=self.pre_limit:
                self.pre_samples-=self.pre.popleft().size
            if not voiced:
                self.noise=0.97*self.noise+0.03*min(rms,0.01)
                return []
            self.parts=list(self.pre);self.samples=self.pre_samples
            self.pre.clear();self.pre_samples=0;self.last_voice=self.samples
        else:
            self.parts.append(data);self.samples+=data.size
            if voiced:self.last_voice=self.samples
        if self.samples-self.last_voice>=self.silence or self.samples>=self.limit:
            return [self.flush()]
        return []

    def flush(self):
        if not self.parts:return None
        audio=np.concatenate(self.parts)[:min(self.samples,self.last_voice+self.tail)]
        self.parts=[];self.samples=0;self.last_voice=0
        return audio
