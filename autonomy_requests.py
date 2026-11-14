"""Recognize an owner's direct invitation to do something independently."""
import re


def direct_self_request(text, *, is_master=False, directed=False):
    if not is_master or len(text) > 1200:
        return False
    text = re.sub(r"\s+", "", str(text))
    text = re.sub(r"(?:直到|等到|除非).{0,8}(?:叫你|让你|要求你)?(?:停止|回来)", "", text)
    if (any(mark in text for mark in ('"', "“", "”", "`", "「", "」", "『", "』"))
            or re.search(r"(?:别|不要|不用|禁止|停止|取消)|我(?:想|要|会)?自己|怎么|如何|假如|如果|例如|示例|他说|她说|有人说|[？?]|(?:了吗|过吗)", text)):
        return False
    invitation = bool(re.search(r"(?:我让你|允许你|你应该|你可以|自由活动|不限(?:制)?时间.{0,5}(?:上网|浏览)|随便逛)", text))
    if not directed and not invitation and not re.match(r"(?:那|好|行|嗯|现在|先|就)*你(?:就|可以)?自己", text):
        return False
    return bool(re.search(r"(?:你(?:就|可以)?自己|自己去|自主去|自由活动).{0,12}(?:玩|逛|看|浏览|学|研究|探索)", text)
        or invitation and re.search(r"(?:玩|逛|看|浏览|上网|学|研究|探索|自由活动)", text))
