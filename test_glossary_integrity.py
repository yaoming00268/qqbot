# -*- coding: utf-8 -*-
"""Deep verification test for data/slang_glossary.md."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import unittest
from slang_glossary import SlangGlossary, SLANG_GLOSSARY_PATH


@unittest.skipUnless(SLANG_GLOSSARY_PATH.is_file(), "Local glossary data is not bundled with the source repository")
class TestSlangGlossaryIntegrity(unittest.TestCase):
    def test_slang_glossary_content_and_retrieval(self):
        test_slang_glossary_content_and_retrieval()


@unittest.skipUnless(SLANG_GLOSSARY_PATH.is_file(), "Local glossary data is not bundled with the source repository")
def test_slang_glossary_content_and_retrieval():
    glossary = SlangGlossary(SLANG_GLOSSARY_PATH)
    entries = glossary.entries()
    assert len(entries) >= 120, f"Expected at least 120 entries, got {len(entries)}"

    # Ensure no empty explanations
    empty_explanations = [e.term for e in entries if e.needs_explanation]
    assert not empty_explanations, f"Found entries without explanation: {empty_explanations}"

    # Ensure no hallucinated placeholder sources
    fake_sources = [e.term for e in entries if any("1000000" in str(s) for s in e.sources)]
    assert not fake_sources, f"Found entries with fake placeholder URLs: {fake_sources}"

    # Verify key osu! terms exist
    osu_terms = {"osu!", "osu!std", "osu!mania", "osu!taiko", "osu!catch", "osu!lazer", "peppy", "pippi",
                 "PP", "AR", "OD", "CS", "HP", "UR", "BPM", "Star Rating", "Offset",
                 "HD", "HR", "DT", "NC", "FL", "EZ", "NF", "HT", "HDHR", "HDDT",
                 "Jump", "Stream", "Single Tap", "Alternate", "SliderBreak", "Choke",
                 "LN", "Jack", "Trill", "SV", "红线与绿线", "数位板", "拖笔与悬空", "Rapid Trigger", "KPS",
                 "727", "农图", "Sotarks", "Dan Course", "皆传", "Reform Dan", "LN Dan",
                 "Accuracy", "Combo", "Miss", "Spinner", "Aim", "OWC", "Ranked", "Loved",
                 "BN", "Modding", "Kiai", "Skin", "Cookiezi", "mrekk", "Tech", "ScoreV2"}
    existing_terms = {e.term for e in entries}
    assert osu_terms.issubset(existing_terms), f"Missing osu! terms: {osu_terms - existing_terms}"

    # Verify key slang terms exist
    slang_terms = {"骨科", "典", "急", "乐", "蚌埠住了", "赢", "破防", "摆烂", "摸鱼", "电子阳痿",
                   "小丑竟是我自己", "打灰", "查成分", "逆天", "细说", "省流", "疯狂星期四",
                   "尊嘟假嘟", "哈基米", "想你了牢大", "纯度", "草", "开盒", "岁月史书",
                   "现充", "厨", "战犯", "人机", "抽象", "润", "鸽子", "社恐与社牛", "凡尔赛", "吃瓜",
                   "草台班子", "电子榨菜", "硬控", "汗流浃背了", "遥遥领先"}
    assert slang_terms.issubset(existing_terms), f"Missing slang terms: {slang_terms - existing_terms}"

    # Verify rhythm game terms
    rhythm_terms = {"FC", "AP", "收歌", "初见杀", "底力", "手癖", "糊", "越级", "逃课",
                    "目押与音押", "BMS", "差分", "鸟加", "擦板与滑键", "蛇", "判定线", "跨手",
                    "Satellite", "PTT", "RKS"}
    assert rhythm_terms.issubset(existing_terms), f"Missing rhythm terms: {rhythm_terms - existing_terms}"

    # Ensure ordinary Chinese sentences do NOT trigger single CJK character slang false positives
    non_slang_sentences = [
        "大家听听这首古典音乐，曲调非常优美，给人带来快乐。",
        "明天去公园看草坪上的花，晚上去厨房做饭，不要着急。",
        "这道题有点模糊，含糊其辞是不对的。",
        "贪吃蛇大作战真好玩。",
        "这把比赛我们全力以赴，追求双赢。",
        "给机械加点润滑油。",
    ]
    for text in non_slang_sentences:
        matches = [m.term for m in glossary.match(text)]
        assert not matches, f"Unexpected false positive matches for '{text}': {matches}"

    # Test phrase matching for various scenarios (terms, aliases, boundaries)
    match_cases = [
        ("今天来打一把osu！", "osu!"),
        ("打了个HDDT直接起飞", "HDDT"),
        ("经典太典了", "典"),
        ("太典了，典！", "典"),
        ("破防了家人们", "破防"),
        ("看到727立刻WYSI", "727"),
        ("德国骨科名不虚传", "骨科"),
        ("这是LN12的谱面", "LN Dan"),
        ("今天是疯狂星期四谁V我50", "疯狂星期四"),
        ("去工地打灰了", "打灰"),
        ("这谱面完全是初见杀", "初见杀"),
        ("纯度极高", "纯度"),
        ("又Choke了太难受了", "Choke"),
        ("这个滑条SliderBreak断了", "SliderBreak"),
        ("猫咪哈基米", "哈基米"),
        ("想你了牢大启动", "想你了牢大"),
        ("尊嘟假嘟这也太可爱了", "尊嘟假嘟"),
        ("他急了直接开始胡言乱语", "急"),
        ("急急急！", "急"),
        ("大伙都乐了", "乐"),
        ("我蚌埠住了", "蚌埠住了"),
        ("赢麻了属于是", "赢"),
        ("这就开润了", "润"),
        ("这完全是生草操作", "草"),
        ("厨力放出太强了", "厨"),
        ("这串我直接糊过去了", "糊"),
        ("Arcaea红蛇蓝蛇掉蛇了", "蛇"),
        ("打了一把osu！Acc高达99.5%", "Accuracy"),
        ("全连FC可惜断连Max Combo", "Combo"),
        ("终盘暴毙1xMiss太搞了", "Miss"),
        ("这个大转盘手都摇断了", "Spinner"),
        ("瞄准Aim完全跟不上这大跳", "Aim"),
        ("中国队在OWC世界杯夺冠了", "OWC"),
        ("新曲终于过审进Ranked了", "Ranked"),
        ("这图虽然没Rank但是进Loved了", "Loved"),
        ("找个BN摸图挑刺", "BN"),
        ("Modding摸图太专业了", "Modding"),
        ("副歌Kiai段落特效太炫了", "Kiai"),
        ("求一个好看的自制皮肤", "Skin"),
        ("永远的音游之神Cookiezi", "Cookiezi"),
        ("mrekk又刷新世界最高PP纪录了", "mrekk"),
        ("这首完全是硬核Tech技术图", "Tech"),
        ("mania打到了Satellite段位", "Satellite"),
        ("Arcaea定数推分提升PTT", "PTT"),
        ("Phigros满分刷新RKS", "RKS"),
        ("世界是个巨大的草台班子", "草台班子"),
        ("吃午饭看个电子榨菜下饭视频", "电子榨菜"),
        ("这个视频直接硬控我30秒", "硬控"),
        ("被怼得这就汗流浃背了", "汗流浃背了"),
        ("遥遥领先全网", "遥遥领先"),
    ]
    for text, expected_term in match_cases:
        matches = glossary.match(text)
        matched_terms = [m.term for m in matches]
        assert expected_term in matched_terms, f"Failed to match '{expected_term}' in '{text}', got: {matched_terms}"

    # Test tag search
    for tag in ["osu", "音游", "网络热梗", "抽象", "模组", "段位", "硬件", "手法", "人物", "选手", "机制"]:
        results = glossary.search(f"#{tag}")
        assert len(results) > 0, f"No results for tag #{tag}"

    # Test prompt section formatting
    section = glossary.prompt_section("打一把osu！然后去打灰")
    assert "【已确认的群聊黑话】" in section
    assert "osu!" in section
    assert "打灰" in section

    # Ensure roundtrip write integrity
    text = glossary._read()
    assert "<!-- entries:start -->" in text
    assert "<!-- entries:end -->" in text
    assert "<!-- index:start -->" in text
    assert "<!-- index:end -->" in text
