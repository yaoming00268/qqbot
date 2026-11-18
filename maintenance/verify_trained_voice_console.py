"""Preview the trained character through the running Studio; never send to QQ."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from playwright.sync_api import sync_playwright
    report = ROOT / "maintenance_reports/voice-training-20261116"
    activation = json.loads((report / "activation.json").read_text(encoding="utf-8"))
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{activation['debug_port']}")
        page = next(page for context in browser.contexts for page in context.pages if "index.html" in page.url)
        original = page.evaluate("S.savedConfig.voice.character")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        task = page.evaluate("""() => rpc('voice','preview', {
            config:{...S.config.voice,character:'chieri-experimental'},
            text:'こんにちは。今日は一緒に本を読みましょう。',emotion:'neutral'
        })""")
        page.wait_for_function("id=>S.status.task?.id===id && S.status.task.state!=='running'", arg=task["id"], timeout=180000)
        result = page.evaluate("""() => {
            const task=S.status.task,r=task.result||{};
            return {state:task.state,error:task.error,character:r.character,emotion:r.emotion,
                effective_provider:r.effective_provider,duration:r.duration,metrics:r.metrics,
                wav_path:r.wav_path,silk_path:r.silk_path,manifest_path:r.manifest_path,
                chunks:(r.chunks||[]).map(c=>({text:c.text,language:c.language,emotion:c.emotion,stream:c.stream}))};
        }""")
        assert result["state"] == "completed", result
        assert result["character"] == "chieri-experimental", result
        assert result["effective_provider"] == "voicestudio", result
        assert all(c["stream"]["blocks"] > 1 for c in result["chunks"]), result
        assert page.evaluate("S.savedConfig.voice.character") == original
        assert page.evaluate("S.status.voice.character") == original
        assert not errors, errors
        result.update(ok=True, saved_character_preserved=original, no_qq_send=True, javascript_errors=errors)
        (report / "trained-console-preview.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        page.locator('[data-page="voice-training"]').click()
        page.wait_for_selector('#training-dataset option[value="dataset-ea33a1df09de"]', state="attached")
        page.locator('#training-dataset').select_option("dataset-ea33a1df09de")
        page.wait_for_function("S.voiceTrainingDataset?.rows?.length===8", timeout=10000)
        # Allow the page transition to finish before taking the visible desktop.
        page.wait_for_timeout(500)
        page.screenshot(path=str(report / "active-training-console.png"), full_page=True)
        page.locator('.voice-training-table').scroll_into_view_if_needed()
        page.screenshot(path=str(report / "active-training-samples.png"), full_page=True)
        browser.close()
    print(json.dumps({k:result[k] for k in ("ok","character","duration","effective_provider","metrics","saved_character_preserved","no_qq_send")},ensure_ascii=False))


if __name__ == "__main__":
    main()
