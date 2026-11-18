"""Exercise the training page and voice application in a disposable WebView home."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from config import AppConfig
    from voice_dataset import atomic_json
    from playwright.sync_api import sync_playwright
    report = ROOT / "maintenance_reports/voice-training-20261116"
    with tempfile.TemporaryDirectory(prefix="qqbot-training-ui-",ignore_cleanup_errors=True) as temp:
        home = Path(temp).resolve()
        config=AppConfig()
        config.voice.catalog_path=str(home/"data/voice/catalog.json")
        config.save(str(home/"config.yaml"))
        atomic_json(home/"data/voice/catalog.json",{"version":1,"characters":{
            "atri":{"name":"ATRI","emotions":{"neutral":{}}},
            "popukar":{"name":"泡普卡","emotions":{"neutral":{}}}}})
        # Serve the source UI via the real desktop host, with a disposable config.
        import socket
        with socket.socket() as listener:
            listener.bind(("127.0.0.1",0))
            port=listener.getsockname()[1]
        script=f"import sys; sys.path.insert(0,{str(ROOT)!r}); from runtime_paths import configure_runtime; from ui.webview_app import launch; launch(configure_runtime(sys.argv[1]))"
        process=subprocess.Popen([sys.executable,"-X","utf8","-c",script,str(home)],cwd=ROOT,
            env={**os.environ,"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":f"--remote-debugging-port={port}"},
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        browser=None
        try:
            with sync_playwright() as p:
                for _ in range(100):
                    try:
                        browser=p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}",timeout=1000)
                        break
                    except Exception:
                        time.sleep(.15)
                if not browser:
                    raise RuntimeError("Disposable WebView did not start")
                for _ in range(100):
                    candidates=[page for c in browser.contexts for page in c.pages]
                    if candidates:
                        break
                    time.sleep(.1)
                page=candidates[0]
                page.wait_for_url("**/index.html",timeout=20000)
                errors=[]
                page.on("pageerror",lambda e:errors.append(str(e)))
                try:
                    page.wait_for_function("window.studio?.ready",timeout=20000)
                except Exception:
                    diagnostic=page.evaluate("({url:location.href,text:document.body.innerText,studio:typeof window.studio,training:typeof trainingActions,scripts:[...document.scripts].map(s=>s.src)})")
                    (report/"ui-diagnostic.json").write_text(json.dumps({"errors":errors,"page":diagnostic},ensure_ascii=False,indent=2),encoding="utf-8")
                    raise
                page.locator('[data-page="voice"]').click()
                page.wait_for_selector('[data-config="voice.character"] option[value="popukar"]',state="attached")
                page.locator('[data-config="voice.character"]').select_option("popukar")
                page.wait_for_function("S.savedConfig.voice.character==='popukar'",timeout=10000)
                assert AppConfig.load(str(home/"config.yaml")).voice.character=="popukar"
                page.locator('[data-page="voice-training"]').click()
                page.wait_for_selector('[data-view="voice-training"] h1')
                assert page.locator('[data-config="voice.training.version"]').input_value()=="v2ProPlus"
                rows=[]
                for width,height in ((1320,860),(1000,720)):
                    page.set_viewport_size({"width":width,"height":height})
                    row=page.evaluate("({width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth,content:document.querySelector('#main-content').scrollWidth>document.querySelector('#main-content').clientWidth+1})")
                    rows.append(row)
                    assert not row["overflow"] and not row["content"],row
                page.screenshot(path=str(report/"training-ui.png"),full_page=True)
                assert not errors,errors
                result={"ok":True,"version":page.evaluate("window.studio.version"),"auto_apply_verified":True,"layout":rows,"javascript_errors":errors,"no_qq_send":True}
                (report/"ui-verification.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
                from playwright.sync_api import Error as BrowserError
                try:
                    page.evaluate("() => rpc('window_action','close')")
                except BrowserError:
                    pass  # WebView can close before resolving the bridge promise.
                browser.close()
            process.wait(timeout=15)
        finally:
            if process.poll() is None:
                # This process has never started a Bot; only the disposable UI exists.
                process.terminate()
                process.wait(timeout=10)
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    main()
