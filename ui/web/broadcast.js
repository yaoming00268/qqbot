/* Local VTuber/Bilibili production UI. External text is always escaped before rendering. */
function broadcastState() {
  S.broadcast ||= {enabled:false, drafts:[], events:[]};
  S.broadcastDraft ||= {broadcast_kind:"dynamic", draft_id:"", revision:0, title:"", text:"", tags:[], video_path:"", cover_path:"", category_id:0, copyright:1, source:""};
  S.broadcastText ||= {speech:"", spoken_text:"", emotion:"neutral", avatar_emotion:"neutral", danmaku:"", motion:"", performance_path:""};
  S.broadcastCursor ||= 0;
  S.bilibiliTools ||= {query:"", bvid:"", oid:0, resource_type:"video", root:0, parent:0, text:""};
}
function broadcastRequestKey(kind) {
  const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `studio-${kind}-${id}`;
}
async function broadcastSaveConfig() {
  await saveAll();
}
function broadcastJson(value) {
  return JSON.stringify(value ?? {}, null, 2);
}
async function broadcastShow(title, value) {
  return modal(title, broadcastJson(value), {confirm:"关闭"});
}
function broadcastEmotionOptions(current, avatar=false) {
  const catalog=S.broadcastVoiceCatalog?.catalog, character=catalog?.characters?.[S.config.broadcast_voice?.character], refs=character?.emotions||{};
  const presets=S.broadcastVoiceCatalog?.presets?.emotions||[];
  const faces={neutral:"平静",happy:"开心",sad:"难过",angry:"生气",surprised:"惊讶",shy:"害羞",thinking:"思考",excited:"兴奋"};
  const values=avatar?Object.keys(faces):(Object.keys(refs).length?Object.keys(refs):["neutral"]);
  return values.map(value=>{const p=presets.find(x=>x.value===value), label=avatar?faces[value]:(p?.label||refs[value]?.description||value);return `<option value="${esc(value)}"${value===current?" selected":""}>${esc(label)} · ${esc(value)}</option>`;}).join("");
}
function broadcastDraftBody(draft=S.broadcastDraft) {
  return {
    broadcast_kind:draft.broadcast_kind || "dynamic", draft_id:draft.draft_id || undefined,
    revision:draft.draft_id ? Number(draft.revision || 0) : undefined,
    title:draft.title || "", text:draft.text || "",
    tags:Array.isArray(draft.tags) ? draft.tags : String(draft.tags || "").split(/[,，\n]/).map(x=>x.trim()).filter(Boolean),
    video_path:draft.video_path || "", cover_path:draft.cover_path || "",
    category_id:Number(draft.category_id || 0), copyright:Number(draft.copyright || 1), source:draft.source || ""
  };
}
function broadcastLoadDraft(row) {
  S.broadcastDraft = {broadcast_kind:row.kind, draft_id:row.id, revision:row.revision, title:"", text:"", tags:[], video_path:"", cover_path:"", category_id:0, copyright:1, source:"", ...(row.body || {})};
}
function broadcastDraftEditor() {
  const d=S.broadcastDraft, video=d.broadcast_kind==="video";
  return `<section class="card"><div class="card-head"><div><h2>${d.draft_id?"编辑草稿":"新建草稿"}</h2><small>${d.draft_id?`${esc(d.draft_id)} · 版本 ${Number(d.revision||0)}`:"内容只保存在本机，发布必须再次确认"}</small></div>${d.draft_id?button("新草稿","broadcast-new-draft","plus","small ghost"):""}</div>
    <div class="form-grid"><div class="field"><label>草稿类型</label><select data-broadcast-draft="broadcast_kind"><option value="dynamic"${video?"":" selected"}>动态</option><option value="video"${video?" selected":""}>视频投稿</option></select></div>${video?`<div class="field"><label>标题</label><input data-broadcast-draft="title" value="${esc(d.title||"")}" maxlength="80"></div>`:""}</div>
    <div class="field mt"><label>${video?"简介":"动态正文"}</label><textarea data-broadcast-draft="text" style="min-height:150px" placeholder="先制作草稿，再人工核对">${esc(d.text||"")}</textarea></div>
    ${video?`<div class="form-grid mt"><div class="field"><label>标签（逗号或换行分隔）</label><input data-broadcast-draft="tags" value="${esc((d.tags||[]).join(", "))}"></div><div class="field"><label>分区 ID</label><input data-broadcast-draft="category_id" type="number" min="1" value="${Number(d.category_id||0)}"></div><div class="field"><label>视频文件</label><div class="row"><input data-broadcast-draft="video_path" value="${esc(d.video_path||"")}" readonly>${button("选择并导入","broadcast-import-video","folder","small")}</div></div><div class="field"><label>封面图片</label><div class="row"><input data-broadcast-draft="cover_path" value="${esc(d.cover_path||"")}" readonly>${button("选择并导入","broadcast-import-cover","folder","small")}</div></div><div class="field"><label>版权类型</label><select data-broadcast-draft="copyright"><option value="1"${Number(d.copyright||1)===1?" selected":""}>原创</option><option value="2"${Number(d.copyright||1)===2?" selected":""}>转载</option></select></div><div class="field"><label>转载来源</label><input data-broadcast-draft="source" value="${esc(d.source||"")}" ${Number(d.copyright||1)===1?"disabled":""}></div></div>`:""}
    <div class="row wrap mt">${button("保存草稿","broadcast-save-draft","check","primary")}${d.draft_id?button("浏览预览","broadcast-preview-draft","play","")+button("发布此版本","broadcast-publish-draft","upload","danger")+button("删除草稿","broadcast-delete-draft","trash","ghost"):""}</div>
    <p class="section-note mt">预览只打开后端生成的本机 URL。发布会展示草稿具体内容并再次确认；页面不会自动发布或重试结果不明的请求。</p></section>`;
}
function broadcastDraftRows() {
  const rows=S.broadcast?.drafts||[];
  if(!rows.length)return `<p class="section-note">还没有动态或投稿草稿。</p>`;
  return `<div class="table-wrap"><table><thead><tr><th>类型与内容</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows.map(row=>`<tr><td><strong>${row.kind==="video"?esc(row.body?.title||"未命名投稿"):"动态草稿"}</strong><br><small>${esc(String(row.body?.text||"").slice(0,100))}</small></td><td>${esc(row.state||"draft")}<br><small>版本 ${Number(row.revision||0)}</small></td><td>${button("编辑","broadcast-edit-draft","edit","small",`data-draft-id="${esc(row.id)}"`)}</td></tr>`).join("")}</tbody></table></div>`;
}
function broadcastEvents() {
  const events=S.broadcastEvents||[];
  if(!events.length)return `<p class="section-note">尚未读取弹幕事件。</p>`;
  return `<div class="table-wrap"><table><thead><tr><th>序号</th><th>类型</th><th>观众</th><th>内容</th></tr></thead><tbody>${events.slice(-30).reverse().map(e=>`<tr><td>${Number(e.seq||0)}</td><td>${esc(e.body?.kind||"")}</td><td>${esc(e.body?.name||"")}</td><td>${esc(e.body?.text||"")}</td></tr>`).join("")}</tbody></table></div>`;
}
function bilibiliAccountPanel() {
  const t=S.bilibiliTools||{}, a=S.broadcast?.autonomy||{};
  const configured=S.config.bilibili.autonomy, actor=S.config.agent.actors?.[configured.actor_id];
  return `<section class="card mt"><div class="card-head"><div><h2>B站登录与自主运营</h2><small>登录在独立内置浏览器完成；扫码、密码及验证码由你在 B站页面操作</small></div></div>
    <div class="row wrap">${button("内置浏览器登录","bili-login","globe","primary small")}${button("打开 B站","bili-browser","globe","small")}${button("检查登录","bili-login-check","check","small")}</div>
    <p class="section-note mt">${esc(S.bilibiliLoginMessage||"完成登录后自动保存到本机。Windows 登录态由当前用户加密保存；环境变量凭据优先。")}</p>
    <div class="row wrap mt">${button("一键浏览与草稿","bili-preset-browse","book","primary small")}${button("启用自主互动与动态","bili-preset-operate","globe","small")}${button("暂停自主运营","bili-preset-pause","stop","ghost small")}</div>
    <p class="section-note mt">预设自动绑定当前人设并开启所需服务。浏览与草稿只允许 AI 阅读、保存本机草稿；自主互动与动态同时允许实际评论、点赞和发动态，并开启共用的实际外发开关。视频投稿和签名单独在高级设置开启。</p>
    <p class="section-note" id="bili-preset-status">${configured.enabled?"自主运营已开启":"自主运营已暂停"} · ${esc(actor?.name||configured.actor_id||"尚未绑定角色")} · 活动间隔 ${Number(configured.interval_minutes)} 分钟。需启动 Bot 核心。</p>
    <div class="field mt"><label for="bili-interests">兴趣话题</label><input id="bili-interests" data-bili-interests value="${esc((configured.interests||[]).join("，"))}" placeholder="游戏，音乐，日常"><small>用逗号分隔即可，最多 20 个。</small></div>
    <div class="mt">${fields(["bilibili.autonomy.goal","bilibili.autonomy.promotion_text"])}</div>
    <details class="mt"><summary>高级权限、角色与频率</summary><div class="mt">${fields(["bilibili.enabled","bilibili.autonomy.enabled","bilibili.autonomy.actor_id","bilibili.autonomy.interval_minutes","bilibili.allow_publish","bilibili.autonomy.allow_comments","bilibili.autonomy.allow_likes","bilibili.autonomy.allow_dynamics","bilibili.autonomy.allow_video_uploads","bilibili.autonomy.allow_profile_update","bilibili.autonomy.max_actions_per_hour","bilibili.autonomy.max_comments_per_day","bilibili.autonomy.min_write_interval_seconds","bilibili.autonomy.max_thread_replies"])}</div><div class="row wrap mt">${button("绑定当前运营角色","bili-bind-actor","check","small")}${button("重新登录 / 换号","bili-relogin","refresh","small ghost")}</div></details>
    <div class="row wrap mt">${button("保存运营设置","save-config","check","primary small")}${button("执行一轮","bili-run","play","small")}${button("推荐流","bili-feed","refresh","small")}${button("回复与@","bili-notifications","refresh","small")}${button("操作记录","bili-operations","file","small")}</div>
    <p class="section-note mt">${a.last_error?esc(a.last_error):"允许有观点的讨论；宣传结合内容自然介绍账号，避免重复广告和纠缠同一讨论。"}</p>
    <details class="mt"><summary>内容查询与手动讨论</summary><div class="form-grid mt"><div class="field"><label>搜索话题</label><input data-bili="query" value="${esc(t.query||"")}"></div><div class="field"><label>BV 视频编号</label><input data-bili="bvid" value="${esc(t.bvid||"")}" placeholder="BV…"></div></div>
    <div class="row wrap mt">${button("搜索","bili-search","search","small")}${button("读取视频与字幕","bili-video","file","small")}</div>
    <details class="mt"><summary>查看评论 / 手动参与讨论</summary><div class="form-grid mt"><div class="field"><label>资源 ID（视频 aid / 动态 ID）</label><input data-bili="oid" value="${esc(t.oid||"")}" inputmode="numeric"></div><div class="field"><label>类型</label><select data-bili="resource_type"><option value="video"${t.resource_type==="video"?" selected":""}>视频</option><option value="dynamic"${t.resource_type==="dynamic"?" selected":""}>动态</option></select></div><div class="field"><label>根评论 rpid（根评论填 0）</label><input data-bili="root" value="${esc(t.root||0)}" inputmode="numeric"></div><div class="field"><label>被回复评论 rpid</label><input data-bili="parent" value="${esc(t.parent||0)}" inputmode="numeric"></div></div><div class="field mt"><label>评论正文</label><textarea data-bili="text">${esc(t.text||"")}</textarea></div><div class="row wrap mt">${button("读取评论","bili-comments","refresh","small")}${button("发送评论 / 回复","bili-comment","upload","danger small")}</div></details>
    </details>${S.bilibiliResult?`<pre class="mt" style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto">${esc(broadcastJson(S.bilibiliResult))}</pre>`:""}</section>
    <section class="card mt"><div class="card-head"><div><h2>哔哩哔哩直播姬</h2><small>使用直播姬负责平台登录与推流</small></div></div>${fields(["bilibili.livehime.executable","bilibili.livehime.transport","bilibili.livehime.audio_device"])}<div class="row wrap mt">${button("选择直播姬","bili-livehime-select","folder","small")}${button("检测与接入说明","bili-livehime-check","check","small")}${button("打开直播姬","bili-livehime-open","play","small")}${button("准备直播画面","bili-livehime-prepare","play","primary small")}</div><p class="section-note mt">window：在直播姬添加窗口捕获，选择 QQBot Live2D · 直播姬窗口捕获。obs_virtual_camera：添加摄像头并选择 OBS Virtual Camera。虚拟摄像头不传声音，音频需另外选择。准备画面后在直播姬内点击开播。</p></section>`;
}
function broadcastPage() {
  broadcastState();
  const b=S.broadcast||{}, t=S.broadcastText, sdk=b.sdk_ready?`SDK ${esc(b.sdk_version||"")} 已就绪`:`SDK ${esc(b.sdk_version||"未安装")} 不可用`;
  return head("虚拟主播与运营","本机制作 Live2D/OBS 画面，接收直播事件，并以草稿方式管理动态与投稿。","BROADCAST",button("刷新状态","broadcast-refresh","refresh"))+
    `<div class="grid two"><section class="card"><div class="card-head"><div><h2>运行状态</h2><small>${sdk} · ${b.credentials_present?"本机凭据已配置":"未配置凭据"}</small></div><span class="tag ${b.enabled?"success":"neutral"}">${b.enabled?"已启用":"未启用"}</span></div>
      <div class="stat-line"><span>直播间</span><span>${esc(String(b.room_id||"未设置"))}</span></div><div class="stat-line"><span>弹幕监听</span><span>${esc(b.listener||"stopped")}</span></div><div class="stat-line"><span>自动回复角色</span><span>${esc(b.actor_id||"未绑定")} · ${b.auto_reply?"开启":"关闭"}</span></div><div class="stat-line"><span>真实外发</span><span>${b.allow_publish?"已允许":"已关闭"}</span></div>${b.last_error?`<p class="section-note mt">${esc(b.last_error)}</p>`:""}
      <div class="row wrap mt">${button("读取账号","broadcast-account","refresh","small")}${button("读取房间","broadcast-room","refresh","small")}${button(b.listener==="running"?"停止监听":"启动监听",b.listener==="running"?"broadcast-listener-stop":"broadcast-listener-start",b.listener==="running"?"stop":"play","small")}${button("绑定当前角色","broadcast-bind-actor","check","small")}</div>
      <p class="section-note mt">使用下方内置浏览器登录，也可沿用 BILIBILI_* 环境变量或本机私有凭据文件。可先预览画面和保存草稿，再配置实际发布。</p></section>
    <section class="card"><div class="card-head"><div><h2>本机画面</h2><small>VTube Studio 与 OBS 均使用本机 WebSocket</small></div></div>
      <div class="row wrap">${button("连接 VTube Studio","broadcast-vts-connect","play","small")}${button("模型列表","broadcast-vts-models","file","small ghost")}${button("热键列表","broadcast-vts-hotkeys","file","small ghost")}${button("连接 OBS","broadcast-obs-connect","play","small")}${button("创建 OBS 来源","broadcast-obs-source","plus","small")}${button("场景列表","broadcast-obs-scenes","file","small ghost")}${button("本机预览","broadcast-preview","play","primary")}</div>
      <div class="row wrap mt">${button("开始录制","broadcast-record-start","play","small")}${button("停止录制","broadcast-record-stop","stop","small ghost")}${button("复位角色","broadcast-reset","refresh","small ghost")}</div></section></div>
    ${bilibiliAccountPanel()}
    <section class="card mt"><div class="card-head"><div><h2>直播专用语音</h2><small>独立后端、角色、语言与缓存；QQ 语音设置不会跟随改变</small></div></div>${fields(["broadcast_voice.enabled","broadcast_voice.character","broadcast_voice.default_language","broadcast_voice.speed"])}
      <details class="mt"><summary>流式与推理设置</summary><div class="mt">${fields(["broadcast_voice.provider","broadcast_voice.endpoint","broadcast_voice.auto_start","broadcast_voice.delivery_mode","broadcast_voice.stream_chunk_ms","broadcast_voice.max_ahead_seconds","broadcast_voice.queue_blocks","broadcast_voice.chunk_chars","broadcast_voice.pause_ms","broadcast_voice.crossfade_ms","broadcast_voice.catalog_path","broadcast_voice.cache_dir","broadcast_voice.engine_python","broadcast_voice.engine_directory","broadcast_voice.engine_config","broadcast_voice.voicestudio.backend_endpoint","broadcast_voice.voicestudio.local_bridge","broadcast_voice.voicestudio.source_directory","broadcast_voice.voicestudio.engine","broadcast_voice.denoise.enabled","broadcast_voice.denoise.reference"])}</div></details>
      <div class="row wrap mt">${button("保存直播语音","broadcast-voice-save","check","primary small")}${button("采用 QQ 当前音色","broadcast-voice-copy-character","file","small ghost")}${button("检查直播接口","broadcast-voice-check","refresh","small")}${button("启动直播引擎","broadcast-voice-start","play","small")}${button("停止直播引擎","broadcast-voice-stop","stop","small ghost")}</div><p class="section-note mt">即时流式模式接收 PCM 后开始播放；先校验模式适合容易提前截断的角色。默认使用独立端口 9882。VoiceStudio 前端地址应与下方原生后端地址不同，例如 9883 → 9882。</p>${b.voice?.shared_backend_with_qq?`<p class="section-note mt">当前直播与 QQ 指向同一个推理后端，模型切换会按锁依次执行。使用两个独立推理服务可避免彼此排队。</p>`:""}</section>
    <details class="card settings-group mt"><summary>Live2D、OBS 与直播设置</summary><div class="mt">${fields(["vtuber.enabled","vtuber.model_path","vtuber.width","vtuber.height","vtuber.vts_url","vtuber.obs_url","vtuber.obs_scene","vtuber.obs_source","bilibili.enabled","bilibili.room_id","bilibili.area_id","bilibili.auto_listen","bilibili.auto_reply","bilibili.actor_id","bilibili.allow_publish"])}</div><div class="row mt">${button("保存设置","save-config","check","primary small")}</div></details>
    <div class="grid two mt"><section class="card"><div class="card-head"><div><h2>语音与动作试听</h2><small>仅发送到本机角色与 OBS，不发布平台内容</small></div></div><div class="field"><label>台词</label><textarea data-broadcast-text="speech">${esc(t.speech||"")}</textarea></div><div class="form-grid mt"><div class="field"><label>语音情绪</label><select data-broadcast-text="emotion">${broadcastEmotionOptions(t.emotion||"neutral")}</select><small>${esc(S.broadcastVoiceCatalog?.presets?.emotions?.find(x=>x.value===(t.emotion||"neutral"))?.description||"标签来自当前声音角色与情绪目录。")}</small></div><div class="field"><label>角色表情</label><select data-broadcast-text="avatar_emotion">${broadcastEmotionOptions(t.avatar_emotion||"neutral",true)}</select></div><div class="field"><label>实际朗读文字（可选，可用 fast / slow / stretch 成对标记）</label><input data-broadcast-text="spoken_text" value="${esc(t.spoken_text||"")}"></div><div class="field"><label>动作组</label><input data-broadcast-text="motion" value="${esc(t.motion||"")}" placeholder="Idle"></div></div><div class="field mt"><label>已有表演媒体</label><div class="row"><input data-broadcast-text="performance_path" value="${esc(t.performance_path||"")}" readonly>${button("选择并导入","broadcast-import-performance","folder","small")}${button("播放表演","broadcast-perform","play","small ghost",t.performance_path?"":"disabled")}</div></div><div class="row wrap mt">${button("语音试听","broadcast-speak","mic","primary")}${button("发送文字到角色","broadcast-avatar-text","play","")}${button("执行动作","broadcast-motion","play","ghost")}</div></section>
    <section class="card"><div class="card-head"><div><h2>弹幕事件</h2><small>观众内容是不可信外部输入</small></div><div class="row">${button("读取新事件","broadcast-events","refresh","small")}</div></div>${broadcastEvents()}<div class="field mt"><label>人工发送弹幕</label><div class="row"><input data-broadcast-text="danmaku" value="${esc(t.danmaku||"")}" placeholder="需要 allow_publish 且必须确认">${button("发送","broadcast-send-danmaku","upload","danger small")}</div></div></section></div>
    <div class="grid two mt">${broadcastDraftEditor()}<section class="card"><div class="card-head"><div><h2>已有草稿</h2><small>状态不明的草稿禁止自动重发</small></div></div>${broadcastDraftRows()}</section></div>
    <details class="card settings-group mt"><summary>高级控制</summary><div class="row wrap mt">${button("切换到配置场景","broadcast-obs-scene","play","small")}${button("OBS 开始推流","broadcast-stream-start","upload","danger small")}${button("OBS 停止推流","broadcast-stream-stop","stop","danger small")}${button("开始直播","broadcast-live-start","upload","danger small")}${button("结束直播","broadcast-live-stop","stop","danger small")}</div><p class="section-note mt">开关播、弹幕和发布均属于真实外部动作，只在 allow_publish 开启后、由主人明确确认时执行。接口返回成功仍需在 OBS 与 B站页面人工核对。</p></details>`;
}
async function broadcastRefresh(redraw=true) {
  broadcastState();
  S.broadcast=await rpc("broadcast","status",{});
  try { S.broadcastVoiceCatalog=await rpc("broadcast","voice_catalog",{}); } catch (_) { S.broadcastVoiceCatalog=null; }
  if(redraw)render();
  return S.broadcast;
}
async function broadcastAvatar(cue, params={}) { return rpc("broadcast","avatar",{cue,...params}); }
async function broadcastImport(kind, filters) {
  await broadcastSaveConfig();
  const selected=await rpc("choose_file","open",filters);
  if(!selected)return;
  const result=await rpc("broadcast","import_media",{path:selected});
  S.broadcastDraft[kind]=result.path;
  render();
}
async function broadcastImportPerformance() {
  await broadcastSaveConfig();
  const selected=await rpc("choose_file","open",["Audio (*.wav;*.mp3;*.flac;*.ogg;*.m4a;*.aac;*.webm)","All files (*.*)"]);
  if(!selected)return;
  const result=await rpc("broadcast","import_media",{path:selected});
  S.broadcastText.performance_path=result.path;
  render();
}
async function bilibiliPreset(mode) {
  await broadcastSaveConfig();
  const r=await rpc("broadcast","account_preset",{mode,revision:S.revision});
  S.config=clone(r.config);S.savedConfig=clone(r.config);S.revision=r.revision;
  await broadcastRefresh();
  toast(mode==="pause"?"自主运营已暂停。":mode==="browse"?"已绑定当前人设：浏览与本机草稿。":"已绑定当前人设：允许实际评论、点赞和发动态。");
}
const broadcastActions={
  "bili-preset-browse":()=>bilibiliPreset("browse"),
  "bili-preset-operate":()=>bilibiliPreset("operate"),
  "bili-preset-pause":()=>bilibiliPreset("pause"),
  "bili-login":async()=>{await broadcastSaveConfig();await rpc("broadcast","login_open",{});S.bilibiliLoginMessage="已打开内置浏览器，请在 B站完成扫码或密码登录，再点击检查登录。";render();},
  "bili-relogin":async()=>{await broadcastSaveConfig();await rpc("broadcast","login_open",{reset:true});S.bilibiliLoginMessage="已打开重新登录窗口。请完成登录，新的登录态会替换本机旧登录态。";render();},
  "bili-browser":async()=>{await broadcastSaveConfig();return rpc("broadcast","browser_open",{});},
  "bili-login-check":async()=>{const r=await rpc("broadcast","login_status",{});S.bilibiliLoginMessage=r.credentials_present?`本机登录态已保存（${r.credentials_source==="environment"?"环境变量":"内置浏览器 / 本机文件"}），可点击读取账号核验平台状态。`:"尚未保存完整登录态，请在内置浏览器完成登录。";await broadcastRefresh();},
  "bili-bind-actor":async()=>{await broadcastSaveConfig();const r=await rpc("broadcast","bind_account_actor",{revision:S.revision});S.config=r.config;S.savedConfig=clone(r.config);S.revision=r.revision;await broadcastRefresh();},
  "bili-run":async()=>{await broadcastSaveConfig();S.bilibiliResult=await rpc("broadcast","autonomy_run",{});render();},
  "bili-feed":()=>bilibiliRead("feed"), "bili-search":()=>bilibiliRead("search"), "bili-video":()=>bilibiliRead("video"),
  "bili-comments":()=>bilibiliRead("comments"), "bili-notifications":()=>bilibiliRead("notifications"), "bili-operations":()=>bilibiliRead("operations"),
  "bili-comment":async()=>{await broadcastSaveConfig();const p=bilibiliParams("comment");if(!await modal("确认发送评论",p.text,{confirm:"发送"}))return;S.bilibiliResult=await rpc("broadcast","comment",{...p,request_key:broadcastRequestKey("comment")});render();},
  "bili-livehime-select":async()=>{const path=await rpc("choose_file","open",["直播姬 (livehime.exe;bililive.exe)","EXE (*.exe)"]);if(path){S.config.bilibili.livehime.executable=path;render();}},
  "bili-livehime-check":async()=>{await broadcastSaveConfig();return broadcastShow("直播姬接入",await rpc("broadcast","livehime_status",{}));},
  "bili-livehime-open":async()=>{await broadcastSaveConfig();return broadcastShow("直播姬启动",await rpc("broadcast","livehime_open",{}));},
  "bili-livehime-prepare":async()=>{await broadcastSaveConfig();return broadcastShow("直播画面接入",await rpc("broadcast","livehime_prepare",{}));},
  "broadcast-voice-save":async()=>{await broadcastSaveConfig();await broadcastRefresh();},
  "broadcast-voice-copy-character":async()=>{const src=S.config.voice,dst=S.config.broadcast_voice;for(const key of ["character","catalog_path","default_language","dictionary"])dst[key]=clone(src[key]);await broadcastSaveConfig();await broadcastRefresh();},
  "broadcast-voice-check":async()=>{await broadcastSaveConfig();return rpc("broadcast","voice_check",{});},
  "broadcast-voice-start":async()=>{await broadcastSaveConfig();return rpc("broadcast","voice_engine_start",{});},
  "broadcast-voice-stop":async()=>{await broadcastSaveConfig();return rpc("broadcast","voice_engine_stop",{});},
  "broadcast-refresh":()=>broadcastRefresh(),
  "broadcast-account":async()=>{await broadcastSaveConfig();const r=await rpc("broadcast","account",{});await broadcastShow("B站账号",r);},
  "broadcast-room":async()=>{await broadcastSaveConfig();const r=await rpc("broadcast","room",{});await broadcastShow("直播间信息",r);},
  "broadcast-listener-start":async()=>{await broadcastSaveConfig();await rpc("broadcast","listener",{mode:"start"});await broadcastRefresh();},
  "broadcast-listener-stop":async()=>{await broadcastSaveConfig();await rpc("broadcast","listener",{mode:"stop"});await broadcastRefresh();},
  "broadcast-events":async()=>{const r=await rpc("broadcast","events",{after:S.broadcastCursor||0,limit:100});S.broadcastEvents=[...(S.broadcastEvents||[]),...(r.events||[])].slice(-200);S.broadcastCursor=r.cursor||S.broadcastCursor;render();},
  "broadcast-bind-actor":async()=>{await broadcastSaveConfig();const r=await rpc("broadcast","bind_actor",{revision:S.revision});if(r.config){S.config=r.config;S.savedConfig=clone(r.config);}if(r.revision!==undefined)S.revision=r.revision;await broadcastRefresh();},
  "broadcast-vts-connect":async()=>{await broadcastSaveConfig();return broadcastAvatar("vts.connect");},
  "broadcast-vts-models":async()=>{await broadcastSaveConfig();return broadcastShow("VTube Studio 模型",await broadcastAvatar("vts.models"));},
  "broadcast-vts-hotkeys":async()=>{await broadcastSaveConfig();return broadcastShow("VTube Studio 热键",await broadcastAvatar("vts.hotkeys"));},
  "broadcast-obs-connect":async()=>{await broadcastSaveConfig();return broadcastAvatar("obs.connect");},
  "broadcast-obs-scenes":async()=>{await broadcastSaveConfig();return broadcastShow("OBS 场景",await broadcastAvatar("obs.scenes"));},
  "broadcast-obs-source":async()=>{await broadcastSaveConfig();return broadcastAvatar("obs.browser_source",{scene:S.config.vtuber.obs_scene,source:S.config.vtuber.obs_source});},
  "broadcast-obs-scene":async()=>{await broadcastSaveConfig();return broadcastAvatar("obs.scene",{scene:S.config.vtuber.obs_scene});},
  "broadcast-record-start":async()=>{await broadcastSaveConfig();return broadcastAvatar("obs.start_record");}, "broadcast-record-stop":async()=>{await broadcastSaveConfig();return broadcastAvatar("obs.stop_record");},
  "broadcast-preview":async()=>{await broadcastSaveConfig();const r=await rpc("broadcast","preview",{});if(r.url)window.open(r.url,"_blank","noopener,noreferrer");},
  "broadcast-reset":async()=>{await broadcastSaveConfig();return broadcastAvatar("reset");},
  "broadcast-avatar-text":async()=>{await broadcastSaveConfig();return broadcastAvatar("text",{text:S.broadcastText.speech,emotion:S.broadcastText.avatar_emotion});},
  "broadcast-motion":async()=>{await broadcastSaveConfig();return broadcastAvatar("motion",{group:S.broadcastText.motion||"Idle",index:0});},
  "broadcast-speak":async()=>{await broadcastSaveConfig();return rpc("broadcast","speak",{text:S.broadcastText.speech,emotion:S.broadcastText.emotion,spoken_text:S.broadcastText.spoken_text,avatar_emotion:S.broadcastText.avatar_emotion});},
  "broadcast-import-performance":broadcastImportPerformance, "broadcast-perform":async()=>{await broadcastSaveConfig();return rpc("broadcast","perform",{path:S.broadcastText.performance_path,text:S.broadcastText.speech,avatar_emotion:S.broadcastText.avatar_emotion});},
  "broadcast-new-draft":()=>{S.broadcastDraft={broadcast_kind:"dynamic",draft_id:"",revision:0,title:"",text:"",tags:[],video_path:"",cover_path:"",category_id:0,copyright:1,source:""};render();},
  "broadcast-import-video":()=>broadcastImport("video_path",["Video (*.mp4;*.mkv;*.mov;*.flv;*.webm)","All files (*.*)"]), "broadcast-import-cover":()=>broadcastImport("cover_path",["Images (*.png;*.jpg;*.jpeg;*.webp)","All files (*.*)"]),
  "broadcast-save-draft":async()=>{const r=await rpc("broadcast","draft",broadcastDraftBody());broadcastLoadDraft(r);await broadcastRefresh();},
  "broadcast-edit-draft":async el=>{const r=await rpc("broadcast","draft_show",{draft_id:el.dataset.draftId});broadcastLoadDraft(r);render();},
  "broadcast-preview-draft":async()=>{const d=await rpc("broadcast","draft_show",{draft_id:S.broadcastDraft.draft_id});await broadcastShow("草稿完整预览",{id:d.id,kind:d.kind,revision:d.revision,state:d.state,body:d.body});},
  "broadcast-delete-draft":async()=>{if(!await modal("删除草稿",`删除 ${esc(S.broadcastDraft.draft_id)} 的当前草稿版本。`,{confirm:"删除"}))return;await rpc("broadcast","draft_delete",{draft_id:S.broadcastDraft.draft_id,revision:S.broadcastDraft.revision});S.broadcastDraft={broadcast_kind:"dynamic",draft_id:"",revision:0,title:"",text:"",tags:[],video_path:"",cover_path:"",category_id:0,copyright:1,source:""};await broadcastRefresh();},
  "broadcast-publish-draft":async()=>{await broadcastSaveConfig();const d=await rpc("broadcast","draft_show",{draft_id:S.broadcastDraft.draft_id});const summary=d.kind==="video"?`投稿：${d.body?.title||""}\n版本：${d.revision}\n视频：${d.body?.video_path||""}\n封面：${d.body?.cover_path||""}\n分区：${Number(d.body?.category_id||0)}\n标签：${(d.body?.tags||[]).join("、")}\n版权：${Number(d.body?.copyright||1)===1?"原创":"转载"}\n来源：${d.body?.source||""}\n简介：\n${d.body?.text||""}`:`动态（版本 ${d.revision}）：\n${d.body?.text||""}`;if(!await modal("确认真实发布",summary,{confirm:"确认发布"}))return;await rpc("broadcast","publish",{draft_id:d.id,revision:d.revision,request_key:broadcastRequestKey("publish")});await broadcastRefresh();},
  "broadcast-send-danmaku":async()=>{await broadcastSaveConfig();const text=S.broadcastText.danmaku.trim();if(!text||!await modal("确认发送弹幕",text,{confirm:"发送"}))return;await rpc("broadcast","danmaku",{text,request_key:broadcastRequestKey("danmaku")});},
  "broadcast-stream-start":async()=>{await broadcastSaveConfig();if(!await modal("确认 OBS 开始推流","将控制本机 OBS 开始推流。请确认目标服务和推流密钥。",{confirm:"开始推流"}))return;return broadcastAvatar("obs.start_stream");},
  "broadcast-stream-stop":async()=>{await broadcastSaveConfig();if(!await modal("确认 OBS 停止推流","将控制本机 OBS 停止推流。",{confirm:"停止推流"}))return;return broadcastAvatar("obs.stop_stream");},
  "broadcast-live-start":async()=>{await broadcastSaveConfig();if(!await modal("确认开始直播","将请求 B站切换为开播状态。请同时核对 OBS 推流状态和直播间页面。",{confirm:"确认开播"}))return;await rpc("broadcast","live_start",{request_key:broadcastRequestKey("live-start")});await broadcastRefresh();},
  "broadcast-live-stop":async()=>{await broadcastSaveConfig();if(!await modal("确认结束直播","将请求 B站结束直播。之后仍需确认 OBS 已停止推流和录制。",{confirm:"确认关播"}))return;await rpc("broadcast","live_stop",{request_key:broadcastRequestKey("live-stop")});await broadcastRefresh();}
};
function bilibiliParams(operation) {
  const t=S.bilibiliTools||{};
  if(operation==="search")return {query:t.query,limit:10};
  if(operation==="video")return {bvid:t.bvid};
  if(["comments","comment"].includes(operation))return {oid:Number(t.oid),resource_type:t.resource_type||"video",root:Number(t.root||0),...(operation==="comment"?{parent:Number(t.parent||0),text:t.text}:{}),...(operation==="comments"?{limit:10}:{})};
  return {limit:10};
}
async function bilibiliRead(operation) {
  await broadcastSaveConfig();
  S.bilibiliResult=await rpc("broadcast",operation,bilibiliParams(operation));
  if(operation==="video"&&S.bilibiliResult.aid)S.bilibiliTools.oid=S.bilibiliResult.aid;
  render();
}
document.addEventListener("input",event=>{
  if(!event.target.hasAttribute("data-bili-interests"))return;
  S.config.bilibili.autonomy.interests=[...new Set(event.target.value.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean))];
  markDirty();
});
document.addEventListener("input",event=>{if(event.target.dataset.bili){broadcastState();S.bilibiliTools[event.target.dataset.bili]=event.target.value;}});
document.addEventListener("change",event=>{if(event.target.dataset.bili){broadcastState();S.bilibiliTools[event.target.dataset.bili]=event.target.value;}});
document.addEventListener("input",event=>{const el=event.target;if(el.dataset.broadcastDraft){broadcastState();let v=el.value;if(["category_id","copyright","revision"].includes(el.dataset.broadcastDraft))v=Number(v||0);if(el.dataset.broadcastDraft==="tags")v=v.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean);S.broadcastDraft[el.dataset.broadcastDraft]=v;}if(el.dataset.broadcastText){broadcastState();S.broadcastText[el.dataset.broadcastText]=el.value;}});
document.addEventListener("change",event=>{const el=event.target;if(["broadcast_kind","copyright"].includes(el.dataset.broadcastDraft)){if(el.dataset.broadcastDraft==="broadcast_kind")S.broadcastDraft.broadcast_kind=el.value;if(el.dataset.broadcastDraft==="copyright")S.broadcastDraft.copyright=Number(el.value);render();}});
document.addEventListener("change",event=>{
  if(event.target.dataset.config!=="broadcast_voice.character")return;
  queueMicrotask(()=>{
    broadcastState();
    const refs=S.broadcastVoiceCatalog?.catalog?.characters?.[S.config.broadcast_voice.character]?.emotions||{};
    if(!refs[S.broadcastText.emotion])S.broadcastText.emotion=Object.keys(refs)[0]||"neutral";
    if(S.page==="broadcast")render();
  });
});
