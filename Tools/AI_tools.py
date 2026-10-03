import re
import time
import random
from collections import defaultdict
from datetime import datetime

class StreamSplitter:
    def __init__(self):
        # # 正则表达式
        # self.number_re = re.compile(r'^\s*(\d+)[.)]\s+')
        self.full_content = ""
        
        # # 状态初始化（新增last_split_time）
        # self.last_number = None
        # self.pending_newlines = 0
        # self.message_buffer = []
        # self.newline_records = []
        self.last_split_time = time.time()  # 初始化时间戳
        
        self.forward_msg_num = 500
        self.enable_forward_msg_num = False
        self.check_forward_msg = False
        
        # 分割依据
        self.split_str = "\n\n\n\n"
        self.chunks = 0
        self.buffer = ""
        
    def split_stream(self, response_stream, type='gemini'):
        try:
            buffer_threshold = 200 if type == 'openai' else 50
            
            for chunk in response_stream:
                # 端砚等中转站会在流里插入 choices 为空数组的心跳/usage 分片，
                # 直接取 choices[0] 会 IndexError 让整轮回复失败，必须先挡住。
                if not getattr(chunk, "choices", None):
                    continue
                match type:
                    case 'gemini':
                        chunk_text = chunk.text
                    case 'openai':
                        delta = getattr(chunk.choices[0], "delta", None)
                        chunk_text = getattr(delta, "content", None) if delta is not None else None
                        
                if not chunk_text:
                    continue
                        
                self.full_content += chunk_text
                self.buffer += chunk_text
                self.chunks += 1
                if type == 'openai':
                    if len(self.buffer) < buffer_threshold:
                        continue
                
                if time.time() - self.last_split_time >= 1.5:
                    self.last_split_time = time.time()
                    for r in self.check_and_split():
                        if r.strip():
                            yield r, self.enable_forward_msg_num
                        
            for r in self.check_and_split(True):
                yield r, self.enable_forward_msg_num
                
            print(f"[{datetime.now()}] FULL_CONTENT: {repr(self.full_content)}")
        except:
            raise
    
    def check_and_split(self, last_response=False):
        if not self.check_forward_msg:
            if len(self.buffer) > self.forward_msg_num:
                self.enable_forward_msg_num = True
            self.check_forward_msg = True
            
        messages = []
        if self.split_str == "\n\n\n\n":
            for sep in (self.split_str[:i] for i in range(4, -1, -1)):
                if sep in self.buffer:
                    self.split_str = sep
                    break
                
        if self.split_str == "":
            self.split_str = "\n\n\n\n"
            message = self.buffer
            
        else:
            messages = self.buffer.split(self.split_str)
            
            if not last_response:
                message = messages[0]
            else:
                for m in messages:
                    if m.strip():  # 跳过空分片：空 Text 消息在 QQ 会显示为"该消息类型暂不支持查看"
                        yield m
                        time.sleep(random.uniform(0.5, 2.0))
                return
            
            if len(messages) == 1:
                self.buffer = messages[0].replace(self.buffer, "")     
            else:
                self.buffer = "\n".join(
                    msg + "\n" if self._needs_trailing_newline(msg) else msg 
                    for msg in messages[1:-1] 
                ) + messages[-1] 
                    
        split_repr = "string.none" if self.split_str == "" else self.split_str
        print(f"[{time.time()}] BUFFER: {repr(self.buffer)}， SPLIT_STR {repr(split_repr)}")
        
        if not self.is_balanced(message):
            self.buffer = message + self.buffer
            return

        if last_response or self.split_str != "\n\n\n\n":
            if message.strip():  # 同上：过滤空/纯空白消息
                yield message
            
    def is_balanced(self, text):
        brackets = {"(": ")", "[": "]", "{": "}", "「": "」"}
        stack = []
        for char in text:
            if char in brackets:
                stack.append(brackets[char])
            elif stack and char == stack[-1]:
                stack.pop()
        return len(stack) == 0 and not (text.endswith(("\n   -", "（", ":", "：")) or text.startswith((":", "：")))
    
    def _needs_trailing_newline(self, text: str) -> bool:
        return any([
            text.startswith((' - ', '• ', '* ')),  
            text.endswith(('：', ":")),   
            re.search(r'\n\s*[-\*•]', text)  
        ])