"""Apply the sample-table markup change without touching surrounding forms."""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / "ui/web/voice_training.js"
text = path.read_text(encoding="utf-8")
start = text.index('      ${data ? `<div class="table-wrap mt">')
end = text.index('<div class="row wrap mt">${button("上一页"', start)
text = text[:start] + '''      ${data ? `<div class="field mt"><label for="training-reference-character">参考音频绑定到角色</label><select id="training-reference-character">${(info.characters||[]).map(c=>`<option value="${esc(c.id)}"${c.id===S.voiceReferenceCharacter?" selected":""}>${esc(c.name)} · ${esc(c.id)}</option>`).join("")}</select></div><p class="section-note mt">每条训练切片都可以用作参考，绑定时会保存台词及情绪、说话方式、强度。1–3 秒切片只在副本尾部补静音，超过 10 秒需重新切分。下拉标签用于标注和参考选择；只有实际绑定的标签才会提供给主代理。待复核样本仍需听辨，新增标签不等于模型已学会该情绪。</p><div class="table-wrap mt"><table class="voice-training-table"><thead><tr><th>试听</th><th>台词</th><th>情绪与发声属性</th><th>复核与参考</th></tr></thead><tbody>${page.map(trainingSampleRow).join("")}</tbody></table></div>''' + text[end:]
path.write_text(text, encoding="utf-8")

path = root / "ui/web/studio.js"
text = path.read_text(encoding="utf-8")
old = '<label class="dialog-field">情绪标签<input name="tag" required value="${esc(el.dataset.emotion || "")}"></label>'
new = '<label class="dialog-field">情绪标签<select name="tag" data-voice-choice="emotions">${voiceChoiceOptions("emotions",el.dataset.emotion || "neutral")}</select><small class="voice-choice-description">${esc(voiceChoiceDescription("emotions",el.dataset.emotion || "neutral"))}</small></label><div class="form-grid"><label class="dialog-field">说话方式<select name="delivery" data-voice-choice="deliveries">${voiceChoiceOptions("deliveries",original.delivery || "normal")}</select><small class="voice-choice-description">${esc(voiceChoiceDescription("deliveries",original.delivery || "normal"))}</small></label><label class="dialog-field">情绪强度<select name="intensity" data-voice-choice="intensities">${voiceChoiceOptions("intensities",original.intensity || "medium")}</select><small class="voice-choice-description">${esc(voiceChoiceDescription("intensities",original.intensity || "medium"))}</small></label></div>'
assert old in text
text = text.replace(old, new, 1)
text = text.replace('  refs[tag] = values;', '  refs[tag] = {...original, ...values};', 1)
path.write_text(text, encoding="utf-8")
