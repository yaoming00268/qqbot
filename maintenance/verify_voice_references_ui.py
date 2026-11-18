"""Exercise Chinese annotations and short-reference binding in real WebView2."""
from pathlib import Path
import json
import math
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
REPORT=ROOT/"maintenance_reports/voice-completion-20261116-131025"


def main():
    from config import AppConfig
    from voice_dataset import atomic_json,audio_quality,file_hash,summarize
    from playwright.sync_api import sync_playwright,Error as BrowserError
    with tempfile.TemporaryDirectory(prefix="qqbot-reference-ui-",ignore_cleanup_errors=True) as temp:
        home=Path(temp).resolve()
        root=home/"data/voice/training"
        folder=root/"datasets/fixture"
        (folder/"audio").mkdir(parents=True)
        rows=[]
        for i,duration in enumerate((2.2,4.0,5.0)):
            path=folder/f"audio/clip-{i}.wav"
            with wave.open(str(path),"wb") as output:
                output.setparams((1,2,32000,0,"NONE","not compressed"))
                output.writeframes(b"".join(struct.pack("<h",int(5000*math.sin(k*math.tau*(220+37*i)/32000))) for k in range(round(duration*32000))))
            rows.append({"id":f"clip-{i}","audio":f"audio/clip-{i}.wav","sha256":file_hash(path),
                         "text":"一緒に本を読みましょう。","language":"ja","emotion":"custom-emotion" if i==2 else "neutral",
                         "speaker":"fixture","source_group":str(i),"reviewed":False,**audio_quality(path)})
        dataset={"id":"fixture","name":"参考音频测试","state":"ready","rows":rows,"rejected":[]}
        dataset["summary"]=summarize(dataset)
        atomic_json(folder/"manifest.json",dataset)
        atomic_json(root/"runs/run/manifest.json",{"id":"run","dataset_id":"fixture","state":"completed","published_character":"fixture","options":{}})
        config=AppConfig()
        config.voice.character="fixture"
        config.voice.catalog_path=str(home/"data/voice/catalog.json")
        config.voice.training.directory=str(root)
        config.save(str(home/"config.yaml"))
        atomic_json(config.voice.catalog_path,{"version":1,"characters":{"fixture":{"name":"测试角色","emotions":{"neutral":{"reference_audio":str(folder/rows[1]["audio"]),"prompt_text":rows[1]["text"]}}}}})
        with socket.socket() as listener:
            listener.bind(("127.0.0.1",0));port=listener.getsockname()[1]
        script=f"import sys; sys.path.insert(0,{str(ROOT)!r}); from runtime_paths import configure_runtime; from ui.webview_app import launch; launch(configure_runtime(sys.argv[1]))"
        log=(REPORT/"reference-ui.log").open("wb")
        process=subprocess.Popen([sys.executable,"-X","utf8","-c",script,str(home)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
            env={**os.environ,"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":f"--remote-debugging-port={port}"},creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        try:
            with sync_playwright() as p:
                browser=None
                for _ in range(100):
                    try:
                        browser=p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}",timeout=700);break
                    except Exception:
                        time.sleep(.15)
                assert browser,"WebView did not start"
                page=next(page for context in browser.contexts for page in context.pages)
                page.wait_for_url("**/index.html",timeout=20000)
                errors=[]
                page.on("pageerror",lambda e:errors.append(str(e)))
                page.wait_for_function("window.studio?.ready",timeout=20000)
                page.locator('[data-page="voice-training"]').click()
                page.wait_for_selector('#training-dataset option[value="fixture"]',state="attached")
                page.locator('#training-dataset').select_option("fixture")
                row=page.locator('.voice-training-table tbody tr').first
                row.wait_for()
                assert row.locator('[data-voice-annotation="emotion"] option').count()>=42
                assert page.locator('[data-voice-annotation="emotion"][data-sample-id="clip-2"]').input_value()=="custom-emotion"
                row.locator('[data-voice-annotation="emotion"]').select_option("affectionate")
                row.locator('[data-voice-annotation="delivery"]').select_option("whisper")
                row.locator('[data-voice-annotation="intensity"]').select_option("strong")
                assert "亲近" in row.locator('.voice-choice-description').first.inner_text()
                second=page.locator('.voice-training-table tbody tr').nth(1)
                second.locator('[data-voice-annotation="emotion"]').select_option("sad")
                assert page.evaluate("Object.keys(S.voiceAnnotationEdits).length")==2
                page.locator('[data-action="training-save-annotations"]').click()
                page.wait_for_function("Object.keys(S.voiceAnnotationEdits).length===0")
                saved=json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
                assert saved["rows"][0]["delivery"]=="whisper" and saved["rows"][0]["intensity"]=="strong"
                assert saved["rows"][1]["emotion"]=="sad" and not saved["rows"][0]["reviewed"]
                row=page.locator('.voice-training-table tbody tr').first
                row.locator('[data-action="training-use-reference"]').click()
                page.wait_for_function("S.voiceCatalog?.catalog.characters.fixture.emotions.affectionate")
                bound=json.loads(Path(config.voice.catalog_path).read_text(encoding="utf-8"))["characters"]["fixture"]["emotions"]["affectionate"]
                assert bound["padding_seconds"]==.9 and bound["delivery"]=="whisper" and not bound["reviewed"]
                assert Path(bound["reference_audio"]).is_file()
                assert (folder/"audio/clip-0.wav").is_file()
                page.wait_for_function("S.voiceTrainingInfo.characters.find(c=>c.id==='fixture').emotions.includes('affectionate')")
                layout=[]
                for width,height in ((1320,860),(1000,720)):
                    page.set_viewport_size({"width":width,"height":height})
                    measured=page.evaluate("({width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth,content:document.querySelector('#main-content').scrollWidth>document.querySelector('#main-content').clientWidth+1})")
                    assert not measured["overflow"] and not measured["content"],measured
                    layout.append(measured)
                page.set_viewport_size({"width":1320,"height":860})
                page.locator('.voice-training-table').evaluate("el=>el.scrollIntoView({block:'start',behavior:'instant'})")
                page.screenshot(path=str(REPORT/"reference-annotations-ui.png"),full_page=True)
                page.locator('[data-page="voice"]').click()
                page.wait_for_selector('[data-action="edit-voice-reference"][data-emotion="affectionate"]')
                page.locator('[data-action="edit-voice-reference"][data-emotion="affectionate"]').click()
                page.wait_for_selector('select[name="tag"]')
                assert page.locator('select[name="tag"] option').count()>=42
                page.locator('select[name="tag"]').select_option("shy")
                assert "羞涩" in page.locator('select[name="tag"]').locator('..').inner_text()
                page.screenshot(path=str(REPORT/"reference-editor-ui.png"),full_page=True)
                assert not errors,errors
                result={"ok":True,"chinese_emotions":43,"deliveries":16,"intensities":4,"multiple_edits_preserved":True,
                    "custom_tag_preserved":True,"reference_padding_seconds":bound["padding_seconds"],"original_preserved":True,
                    "unreviewed_stays_unreviewed":True,"layout":layout,"javascript_errors":errors,"no_qq_send":True}
                (REPORT/"reference-ui-verification.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
                # Cancel the draft dialog before normal close.
                page.keyboard.press("Escape")
                try:
                    page.evaluate("() => rpc('window_action','close')")
                except BrowserError:
                    pass
                browser.close()
            process.wait(timeout=15)
            print(json.dumps(result,ensure_ascii=False))
        finally:
            if process.poll() is None:
                # This disposable UI has never launched a Bot.
                process.terminate();process.wait(timeout=10)
            log.close()


if __name__=="__main__":
    main()
