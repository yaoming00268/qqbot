/* Training uses isolated workers; the shared Studio task runner supplies progress. */
function trainingState() {
  S.voiceTrainingInfo ||= {datasets: [], runs: []};
  S.voiceTrainingDraft ||= {speaker: "chieri", name: "", annotation: "", audio: "", language: "ja", allow_unreviewed: false};
  S.voiceAnnotationEdits ||= {};
  S.voiceTrainingMessages ||= [];
}
function voicePresets() {
  return S.voiceTrainingInfo?.presets || S.voiceCatalog?.presets || {emotions:[], deliveries:[], intensities:[]};
}
function voiceChoiceOptions(kind, current) {
  const choices = voicePresets()[kind] || [], groups = [...new Set(choices.map(c=>c.group))];
  const custom = current && !choices.some(c=>c.value===current)
    ? `<optgroup label="已有自定义标签"><option value="${esc(current)}" selected>${esc(current)}（自定义）</option></optgroup>` : "";
  return custom + groups.map(group=>`<optgroup label="${esc(group)}">${choices.filter(c=>c.group===group).map(c=>`<option value="${esc(c.value)}"${c.value===current?" selected":""}>${esc(c.label)} · ${esc(c.value)}</option>`).join("")}</optgroup>`).join("");
}
function voiceChoiceDescription(kind, current) {
  return voicePresets()[kind]?.find(c=>c.value===current)?.description || "已有自定义标签，保留原有含义。";
}
function trainingChoice(row, key, title, kind, fallback) {
  const value=row[key] || fallback;
  return `<label class="voice-annotation-choice">${title}<select data-voice-annotation="${key}" data-choice-kind="${kind}" data-sample-id="${esc(row.id)}" aria-label="${title}">${voiceChoiceOptions(kind,value)}</select><small class="voice-choice-description">${esc(voiceChoiceDescription(kind,value))}</small></label>`;
}
function trainingSampleRow(r) {
  return `<tr><td>${button(r.duration.toFixed(1)+"s", "training-play-sample", "play", "small ghost", `data-sample-id="${esc(r.id)}"`)}<small>${esc(r.language)} · ${r.rms_db.toFixed(1)} dB</small></td>
    <td><textarea data-voice-annotation="text" data-sample-id="${esc(r.id)}" aria-label="样本台词">${esc(r.text)}</textarea><small>${esc(r.annotation?.method || "imported")}${r.asr ? ` · ASR ${Math.round(r.asr.confidence*100)}%` : ""}</small></td>
    <td class="voice-annotation-attributes">${trainingChoice(r,"emotion","情绪","emotions","neutral")}${trainingChoice(r,"delivery","说话方式","deliveries","normal")}${trainingChoice(r,"intensity","情绪强度","intensities","medium")}</td>
    <td><label><input type="checkbox" data-voice-annotation="reviewed" data-sample-id="${esc(r.id)}"${checked(r.reviewed)}>已听辨</label><div class="mt">${button("用作参考", "training-use-reference", "mic", "small", `data-sample-id="${esc(r.id)}"`)}</div><small>绑定当前台词与标签</small></td></tr>`;
}
function voiceLiveLabel() {
  const live = S.status.voice || {}, saved = S.savedConfig.voice || {};
  const edited = S.config.voice?.character !== saved.character;
  if (edited) return `正在应用 ${S.config.voice.character} · 已保存 ${saved.character}`;
  if (live.state === "applied") return `Bot 已应用：${live.character} · ${live.enabled ? "语音开启" : "语音关闭"}`;
  if (S.status.state !== "running") return `已保存：${saved.character} · 下次启动生效`;
  return `已保存：${saved.character} · 等待 Bot 确认`;
}
let voiceApplyChain = Promise.resolve();
function applyVoiceSettings(changes) {
  const operation = async () => {
    if (S.invalid.size) throw new Error("请先修正无效配置字段。");
    const result = await rpc("voice", "apply", {changes, revision: S.revision});
    // Keep unrelated drafts, including other unsaved voice controls.
    for (const key of Object.keys(changes)) S.savedConfig.voice[key] = clone(result.config.voice[key]);
    S.revision = result.revision;
    S.status.voice = result.applied;
    markDirty();
    const label = $("#voice-live-status");
    if (label) label.textContent = voiceLiveLabel();
  };
  const next = voiceApplyChain.catch(() => {}).then(operation);
  voiceApplyChain = next;
  return next;
}
function voiceTrainingPage() {
  trainingState();
  const info = S.voiceTrainingInfo, draft = S.voiceTrainingDraft;
  const data = S.voiceTrainingDataset, busy = S.status.task?.state === "running";
  const selected = data?.id || "";
  const offset = S.voiceTrainingOffset || 0, page = data?.rows.slice(offset, offset+30) || [];
  const stages = S.voiceTrainingMessages.slice(-20).join("\n");
  if (data && S.voiceReferenceDataset !== data.id) {
    const run=info.runs.find(r=>r.dataset_id===data.id && r.published_character);
    S.voiceReferenceCharacter=run?.published_character || S.config.voice.character;
    S.voiceReferenceDataset=data.id;
  }
  const input = (key, label, value) => `<div class="field"><label>${label}</label><input data-training-field="${key}" value="${esc(value)}"></div>`;
  return head("声音训练", "从有标签音频生成角色权重，验证试听后应用到 Bot。", "VOICE TRAINING", button("刷新任务", "training-refresh", "refresh")) +
    `<div class="grid two"><section class="card"><div class="card-head"><div><h2>1. 准备语料</h2><small>CSV / JSONL / GPT-SoVITS .list，或音频自动切分转写</small></div></div>
      <div class="form-grid">${input("speaker", "角色 ID", draft.speaker)}${input("name", "数据集名称", draft.name)}</div>
      <div class="field mt"><label>已有标注文件</label><div class="row"><input data-training-field="annotation" value="${esc(draft.annotation)}" placeholder="选择 CSV、JSONL 或 .list">${button("选择", "training-choose-annotations", "folder", "small")}</div></div>
      <div class="row wrap mt">${button("导入标注数据", "training-import", "download", "", busy ? "disabled" : "")}${button("获取千绘莉官方样本并转写", "training-chieri", "mic", "ghost", busy ? "disabled" : "")}</div>
      <details class="mt"><summary>从本机音频自动切分与转写</summary><div class="field mt"><label>音频路径（每行一条）</label><textarea data-training-field="audio" placeholder="本地 WAV / MP3 / M4A">${esc(draft.audio)}</textarea></div><div class="row wrap mt">${button("选择音频", "training-choose-audio", "folder", "small")}${button("切分、转写并建议标签", "training-prepare", "play", "", busy ? "disabled" : "")}</div>${fields(["voice.training.asr_model", "voice.training.asr_device", "voice.training.asr_compute_type"])}</details>
      <p class="section-note mt">自动转写与情绪建议保留待复核状态。音频会归一为 32kHz 单声道，检查时长、静音、削波和重复。千绘莉素材来自官方普通试听页，保留原始文件与来源记录。</p></section>
    <section class="card"><div class="card-head"><div><h2>2. 训练参数</h2><small>默认适配 8GB 显存 · 训练时与本机语音、唱歌互斥</small></div></div>
      ${fields(["voice.training.version", "voice.training.batch_size", "voice.training.sovits_epochs", "voice.training.gpt_epochs", "voice.training.save_every", "voice.training.lora_rank", "voice.training.validation_ratio", "voice.training.seed"])}
      <label class="section-note row mt"><input type="checkbox" data-training-field="allow_unreviewed"${checked(draft.allow_unreviewed)}>使用未复核数据作实验训练</label>
      <div class="row wrap mt">${button("开始角色训练", "training-start", "play", "primary", busy || !data ? "disabled" : "")}${button("取消任务", "training-cancel", "stop", "ghost", busy ? "" : "disabled")}</div>
      <p class="section-note mt">v2ProPlus 全量微调；v4 使用 LoRA。GPT 与 SoVITS 都会训练并导出。按原始音源和相同台词隔离留出集。情绪标签用于分组与参考切片选择，不附加到台词中。</p></section></div>
    <section class="card mt"><div class="card-head"><div><h2>3. 样本复核</h2><small>${data ? `${data.summary.count} 条 · ${(data.summary.seconds/60).toFixed(1)} 分钟 · ${data.summary.unreviewed} 条待复核 · ${JSON.stringify(data.summary.emotions)}` : "选择一个已导入的数据集"}</small></div>${button("保存标注", "training-save-annotations", "check", "small", Object.keys(S.voiceAnnotationEdits).length ? "" : "disabled")}</div>
      <div class="field"><label>数据集</label><select id="training-dataset"><option value="">选择数据集</option>${info.datasets.map(d => `<option value="${esc(d.id)}"${d.id===selected ? " selected" : ""}>${esc(d.name || d.id)} · ${esc(d.state)}</option>`).join("")}</select></div>
      ${data ? `<div class="field mt"><label for="training-reference-character">参考音频绑定到角色</label><select id="training-reference-character">${(info.characters||[]).map(c=>`<option value="${esc(c.id)}"${c.id===S.voiceReferenceCharacter?" selected":""}>${esc(c.name)} · ${esc(c.id)}</option>`).join("")}</select></div><p class="section-note mt">每条训练切片都可以用作参考，绑定时会保存台词及情绪、说话方式、强度。1–3 秒切片只在副本尾部补静音，超过 10 秒需重新切分。下拉标签用于标注和参考选择；只有实际绑定的标签才会提供给主代理。待复核样本仍需听辨，新增标签不等于模型已学会该情绪。</p><div class="table-wrap mt"><table class="voice-training-table"><thead><tr><th>试听</th><th>台词</th><th>情绪与发声属性</th><th>复核与参考</th></tr></thead><tbody>${page.map(trainingSampleRow).join("")}</tbody></table></div><div class="row wrap mt">${button("上一页", "training-previous", "", "small", offset ? "" : "disabled")}<small>${offset+1}–${Math.min(offset+30,data.rows.length)} / ${data.rows.length}</small>${button("下一页", "training-next", "", "small", offset+30<data.rows.length ? "" : "disabled")}</div><audio id="training-sample-player" controls class="mt" hidden></audio>${data.rejected?.length ? `<details class="mt"><summary>查看 ${data.rejected.length} 条拒收记录</summary><pre class="voice-transcript">${esc(JSON.stringify(data.rejected,null,2))}</pre></details>` : ""}` : ""}</section>
    <section class="card mt"><div class="card-head"><div><h2>4. 训练产物与验证</h2><small>先验证留出文本合成，再注册新角色；应用后无需重启聊天</small></div></div>
      <pre id="voice-training-progress" class="voice-transcript">${esc(stages || "等待训练任务。")}</pre>
      <div class="table-wrap"><table><thead><tr><th>任务</th><th>状态</th><th>操作</th></tr></thead><tbody>${info.runs.map(r => `<tr><td><strong>${esc(r.id)}</strong><br><small>${esc(r.dataset_id)} · ${esc(r.options?.version)}</small>${r.error ? `<p class="section-note">${esc(r.error.slice(0,350))}</p>` : ""}</td><td>${esc(r.state)}<br><small>${esc(r.stage || "")}${r.validation?.state ? ` · 验证 ${esc(r.validation.state)}` : ""}</small></td><td><div class="row wrap">${button("日志", "training-show-log", "terminal", "small ghost", `data-run-id="${esc(r.id)}"`)}${["failed","cancelled"].includes(r.state) ? button("继续", "training-resume", "play", "small", `data-run-id="${esc(r.id)}" data-dataset-id="${esc(r.dataset_id)}" ${busy ? "disabled" : ""}`) : ""}${r.state==="completed" ? button("验证试听", "training-validate", "mic", "small", `data-run-id="${esc(r.id)}" ${busy ? "disabled" : ""}`) : ""}${r.validation?.state==="passed" ? button("听验证结果", "training-play-validation", "play", "small ghost", `data-run-id="${esc(r.id)}"`) : ""}${r.validation?.state==="passed" && !r.published_character ? button("注册角色", "training-publish", "plus", "small", `data-run-id="${esc(r.id)}"`) : ""}${r.published_character ? button("应用到 Bot", "training-apply", "check", "small primary", `data-character-id="${esc(r.published_character)}"`) : ""}</div></td></tr>`).join("")}</tbody></table></div><audio id="training-validation-player" controls class="mt" hidden></audio></section>
    <details class="card mt"><summary>训练环境与路径</summary><p class="section-note mt">${info.installed ? "独立训练环境已安装" : "训练环境尚未安装"} · ${esc(info.python || "")}</p><div class="row wrap mt">${button("安装训练环境", "training-setup", "download", "", busy ? "disabled" : "")}${button("下载官方预训练资产", "training-assets", "download", "", busy ? "disabled" : "")}</div>${fields(["voice.training.python","voice.training.directory","voice.training.timeout_seconds"])}</details>`;
}
async function trainingRefresh() {
  trainingState();
  S.voiceTrainingInfo = await rpc("voice_training", "status", {config: S.config.voice});
  render();
}
async function trainingTask(operation, params={}) {
  trainingState();
  const result = await rpc("voice_training", operation, {config:S.config.voice, ...params});
  S.status.task = {...result, kind:"voice-training-"+operation};
  S.voiceTrainingMessages.push("任务已开始："+operation);
  render();
}
async function trainingSaveAnnotations() {
  const edits = Object.values(S.voiceAnnotationEdits || {});
  if (!edits.length) return;
  const data = S.voiceTrainingDataset;
  await rpc("voice_training","save_annotations",{config:S.config.voice,dataset_id:data.id,edits,revision:data.revision});
  S.voiceTrainingDataset = await rpc("voice_training","dataset",{config:S.config.voice,dataset_id:data.id});
  S.voiceAnnotationEdits = {};
  markDirty(); render();
}
const trainingActions = {
  "go-voice-training": () => navigate("voice-training"),
  "apply-voice-settings": () => applyVoiceSettings(clone(S.config.voice)),
  "training-refresh": trainingRefresh,
  "training-setup": () => trainingTask("setup"),
  "training-assets": () => trainingTask("assets"),
  "training-chieri": () => trainingTask("chieri"),
  "training-cancel": () => rpc("voice_training","cancel"),
  "training-choose-annotations": async () => { const path=await rpc("choose_file","open",["Annotations (*.csv;*.jsonl;*.list)"]); if(path){S.voiceTrainingDraft.annotation=path;render();} },
  "training-choose-audio": async () => { const path=await rpc("choose_file","open",["Audio (*.wav;*.mp3;*.m4a;*.ogg;*.flac)"]); if(path){S.voiceTrainingDraft.audio=path;render();} },
  "training-import": () => trainingTask("import",{path:S.voiceTrainingDraft.annotation,speaker:S.voiceTrainingDraft.speaker,name:S.voiceTrainingDraft.name}),
  "training-prepare": () => trainingTask("prepare_audio",{paths:S.voiceTrainingDraft.audio.split(/\r?\n/).map(p=>p.trim()).filter(Boolean),speaker:S.voiceTrainingDraft.speaker,name:S.voiceTrainingDraft.name,language:S.voiceTrainingDraft.language}),
  "training-save-annotations": trainingSaveAnnotations,
  "training-start": async () => { await trainingSaveAnnotations(); await trainingTask("train",{dataset_id:S.voiceTrainingDataset.id,allow_unreviewed:S.voiceTrainingDraft.allow_unreviewed}); },
  "training-previous": () => {S.voiceTrainingOffset=Math.max(0,(S.voiceTrainingOffset||0)-30);render();},
  "training-next": () => {S.voiceTrainingOffset=(S.voiceTrainingOffset||0)+30;render();},
  "training-play-sample": async el => {const result=await rpc("voice_training","audio",{config:S.config.voice,dataset_id:S.voiceTrainingDataset.id,sample_id:el.dataset.sampleId});const audio=$("#training-sample-player");audio.hidden=false;audio.src="data:audio/wav;base64,"+result.wav_base64;await audio.play();},
  "training-use-reference": async el => {
    const sampleId=el.dataset.sampleId, characterId=S.voiceReferenceCharacter;
    if(!characterId)throw new Error("请先注册或选择目标角色。");
    await trainingSaveAnnotations();
    const data=S.voiceTrainingDataset, row=data.rows.find(r=>r.id===sampleId);
    const catalog=await rpc("voice","catalog",{config:S.config.voice});
    const target=catalog.catalog.characters[characterId];
    if(!target)throw new Error("目标角色已变化，请刷新。");
    const replace=!!target.emotions?.[row.emotion];
    if(replace && !await modal("替换情绪参考",`将 ${target.name||characterId} 的 ${row.emotion} 参考替换为所选切片。原始音频保留。${row.reviewed?"":"此样本尚未复核，请确认台词与音频对应。"}`,{confirm:"替换绑定"}))return;
    const result=await rpc("voice_training","use_reference",{config:S.config.voice,dataset_id:data.id,sample_id:sampleId,
      character_id:characterId,dataset_revision:data.revision,catalog_revision:catalog.revision,replace});
    S.voiceCatalog=await rpc("voice","catalog",{config:S.config.voice});
    await trainingRefresh();
    toast(`已绑定 ${row.emotion} 参考${result.reference.padding_seconds>0?`，副本补静音 ${result.reference.padding_seconds.toFixed(2)} 秒`:""}`);
  },
  "training-resume": el => trainingTask("train",{dataset_id:el.dataset.datasetId,resume:el.dataset.runId,allow_unreviewed:S.voiceTrainingDraft.allow_unreviewed}),
  "training-validate": el => trainingTask("validate",{run_id:el.dataset.runId}),
  "training-show-log": async el => {const result=await rpc("voice_training","log",{config:S.config.voice,run_id:el.dataset.runId});S.voiceTrainingMessages=[result.text];render();},
  "training-play-validation": async el => {const result=await rpc("voice_training","validation_audio",{config:S.config.voice,run_id:el.dataset.runId});const audio=$("#training-validation-player");audio.hidden=false;audio.src="data:audio/wav;base64,"+result.wav_base64;await audio.play();},
  "training-publish": async el => {
    const values=await modal("注册训练角色","保留现有声音，新角色使用独立 ID。",{confirm:"注册角色",fields:'<label class="dialog-field">角色 ID<input name="character_id" value="chieri"></label>'});
    if(!values)return;
    const catalog=await rpc("voice","catalog",{config:S.config.voice});
    await rpc("voice_training","publish",{config:S.config.voice,run_id:el.dataset.runId,character_id:values.character_id,revision:catalog.revision});
    await trainingRefresh();
  },
  "training-apply": async el => {S.config.voice.character=el.dataset.characterId;await applyVoiceSettings({character:el.dataset.characterId});await loadRecords("voice");await navigate("voice");},
};
document.addEventListener("input",event=>{
  const el=event.target;
  if(el.dataset.trainingField){trainingState();S.voiceTrainingDraft[el.dataset.trainingField]=el.type==="checkbox"?el.checked:el.value;}
  if(el.dataset.voiceAnnotation){
    const row=S.voiceTrainingDataset.rows.find(r=>r.id===el.dataset.sampleId);
    row[el.dataset.voiceAnnotation]=el.type==="checkbox"?el.checked:el.value;
    S.voiceAnnotationEdits[row.id]={id:row.id,text:row.text,emotion:row.emotion,delivery:row.delivery||"normal",intensity:row.intensity||"medium",reviewed:row.reviewed};
    if(el.dataset.choiceKind)el.closest("label").querySelector(".voice-choice-description").textContent=voiceChoiceDescription(el.dataset.choiceKind,el.value);
    markDirty();
    const save=$("[data-action=training-save-annotations]");if(save)save.disabled=false;
  }
});
document.addEventListener("change",event=>{
  if(event.target.id==="training-reference-character")S.voiceReferenceCharacter=event.target.value;
  if(event.target.dataset.voiceChoice){
    const el=event.target;
    el.closest("label").querySelector(".voice-choice-description").textContent=voiceChoiceDescription(el.dataset.voiceChoice,el.value);
    const form=el.closest("form");
    if(form){
      const label=(kind,value)=>voicePresets()[kind]?.find(c=>c.value===value)?.label||value;
      form.querySelector('[name="description"]').value=[label("emotions",form.querySelector('[name="tag"]').value),label("deliveries",form.querySelector('[name="delivery"]').value),label("intensities",form.querySelector('[name="intensity"]').value)].join(" · ");
    }
  }
  if(event.target.id==="training-dataset")attempt(async()=>{
    if(Object.keys(S.voiceAnnotationEdits||{}).length)await trainingSaveAnnotations();
    const identity=event.target.value;
    S.voiceTrainingDataset=identity?await rpc("voice_training","dataset",{config:S.config.voice,dataset_id:identity}):null;
    S.voiceTrainingOffset=0;render();
  });
});
function trainingEvent(event){
  if(!event.data?.kind?.startsWith("voice-training-") && !(event.kind==="model_progress" && S.status.task?.kind?.startsWith("voice-training-")))return false;
  trainingState();
  const data=event.data;
  if(event.kind==="model_progress"){
    S.voiceTrainingMessages.push(data.message || `${data.stage}${data.index!==undefined?" · "+(data.index+1)+"/"+data.total:""}${data.file?" · "+data.file:""}`);
    S.voiceTrainingMessages=S.voiceTrainingMessages.slice(-100);
    const box=$("#voice-training-progress");if(box)box.textContent=S.voiceTrainingMessages.slice(-20).join("\n");
  }
  if(event.kind==="model_result"){
    S.voiceTrainingMessages.push(data.state==="failed"?data.error:"任务状态："+data.state);
    if(data.state==="failed")toast(data.error,"error");
    if(data.result?.rows && data.state==="completed"){
      S.voiceTrainingDataset=data.result;S.voiceAnnotationEdits={};
      attempt(async()=>{S.voiceTrainingDataset=await rpc("voice_training","dataset",{config:S.config.voice,dataset_id:data.result.id});if(S.page==="voice-training")render();});
    }
    attempt(async()=>{S.voiceTrainingInfo=await rpc("voice_training","status",{config:S.config.voice});if(S.page==="voice-training")render();});
  }
  return true;
}
