"""Advisory answer depth; delivery preserves the full answer and natural bubbles."""
from __future__ import annotations
import re


def reply_style(text, messages=()):
    # Latest instructions win; unrelated earlier chatter cannot impose brevity.
    text = str(text or "")
    concise = r"(?:只用|就用|用)?一句话(?:回答|说明|概括|总结|说清|就行|即可)|简短(?:点|回答|说明)|少说(?:点)|不要太长|只要结论|\b(?:briefly|short answer)\b"
    expanded = (r"详细|展开|多说|解释|分析|为什么|为何|怎么办|怎么(?:做|办|用|改|解决|设置)|如何|步骤|教程|"
                r"对比|比较|原因|建议|讲讲|聊聊|推导|举例|难受|焦虑|委屈|崩溃|压力很大|求助|"
                r"\b(?:why|how|explain|compare|steps|detail|help)\b")
    if re.search(concise, text, re.I):
        return "concise"
    if re.search(expanded, text, re.I) or len(re.findall(r"[?？]", text)) > 1 or len(text) > 240:
        return "expanded"
    return "casual"
