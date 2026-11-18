/* Conversation identities are selected explicitly; shared sources retain their own grants. */
"use strict";

function conversationLabel(item) {
  const id = item.source.replace(/^(?:onebot_(?:group|private)|group|c2c)_/, "");
  return item.label || `${item.is_group ? "群聊" : "私聊"} ${id}`;
}
function conversationDraft(item) {
  return {...clone(item), target_id: item.source.replace(/^(?:onebot_(?:group|private)|group|c2c)_/, "")};
}
function conversationSource(draft) {
  const prefix = draft.account.startsWith("onebot:") ? (draft.is_group ? "onebot_group_" : "onebot_private_") : (draft.is_group ? "group_" : "c2c_");
  return prefix + (draft.target_id || "").trim();
}
function conversationOptions(items, current) {
  return items.map(([value,label]) => `<option value="${esc(value)}"${value === current ? " selected" : ""}>${esc(label)}</option>`).join("");
}
function conversationsPage() {
  const data = S.conversations || {items: [], accounts: [], conversations: 0, contexts: 0};
  if (!S.conversationDraft && data.items.length) S.conversationDraft = conversationDraft(data.items[0]);
  const d = S.conversationDraft;
  const list = data.items.map((item,index) => `<button class="persona-item ${d && item.account === d.account && item.source === d.source ? "active" : ""}" data-action="conversation-select" data-index="${index}"><span class="avatar">${item.is_group ? "群" : "私"}</span><span class="grow"><strong>${esc(conversationLabel(item))}</strong><p>${esc(item.effective_persona)} · ${esc({independent:"独立",merge:"合并",follow:"跟随"}[item.mode])}</p><small>${esc(data.accounts.find(a=>a.id===item.account)?.label || item.account)}</small></span></button>`).join("");
  let editor = empty("选择一个对话", "可以从已有对话中选择，也可以手动添加群号或私聊 ID。", "chat");
  if (d) {
    const source = conversationSource(d);
    const targets = data.items.filter(item=>item.account === d.account && item.source !== source);
    const accountOptions = data.accounts.map(item=>[item.id,item.label]);
    const personaOptions = [["","沿用全局 / 原群设置"],...S.personas.items.map(item=>[item.id,item.name])];
    const targetOptions = [["","请选择主对话"],...targets.map(item=>[item.source,conversationLabel(item)])];
    if (d.target && !targets.some(item=>item.source===d.target)) targetOptions.push([d.target,d.target]);
    editor = `<div class="card-head"><div><h2>${esc(d.label || "对话设置")}</h2><small>人设与共享方式随配置保存，下一条消息生效</small></div></div><div class="form-grid">
      <div class="field"><label>账号</label><select data-conversation-field="account" aria-label="对话账号" ${d.source ? "disabled" : ""}>${conversationOptions(accountOptions,d.account)}</select></div>
      <div class="field"><label>对话类型</label><select data-conversation-field="is_group" aria-label="对话类型" ${d.source ? "disabled" : ""}>${conversationOptions([["group","群聊"],["private","私聊"]],d.is_group ? "group" : "private")}</select></div>
      <div class="field"><label>群号 / 私聊用户 ID</label><input data-conversation-field="target_id" aria-label="对话 ID" value="${esc(d.target_id)}" ${d.source ? "readonly" : ""}></div>
      <div class="field"><label>对话备注</label><input data-conversation-field="label" aria-label="对话备注" maxlength="80" value="${esc(d.label || "")}"></div>
      <div class="field full"><label>运行方式</label><select data-conversation-field="mode" aria-label="对话运行方式">${conversationOptions([["independent","独立：单独人设、对话与记忆"],["merge","合并：共同使用对话与记忆"],["follow","跟随：沿用主对话的代理"]],d.mode)}</select></div>
      ${d.mode === "independent" ? `<div class="field full"><label>此对话的人设</label><select data-conversation-field="persona_id" aria-label="对话人设">${conversationOptions(personaOptions,d.persona_id || "")}</select></div>` : `<div class="field full"><label>主对话</label><select data-conversation-field="target" aria-label="主对话">${conversationOptions(targetOptions,d.target || "")}</select><span class="hint">合并后统一使用主对话的人设，复用其已有模型上下文。可先添加需要的主对话。</span></div>`}
      </div><p class="section-note mt">${d.mode === "independent" ? "独立对话按所选人设分别保留历史。恢复独立后可继续使用原来的会话。" : d.mode === "follow" ? "跟随对话复用主代理的人设、上下文与记忆，不单独触发空闲自由活动。" : "成员对话共用一个代理与记忆索引，每组只保留一次空闲自由活动调度。"}</p>
      ${d.mode !== "independent" ? `<p class="section-note">这些对话的内容与记忆会供同一代理使用；回复目标、主人身份和工具权限仍按本轮来源判断。已有记录保留原处，按需读取并去重，不逐轮拼接历史全文。</p>` : ""}
      ${d.error ? `<p class="section-note">${esc(d.error)}</p>` : ""}
      <div class="separator"></div><div class="row wrap">${button("保存对话设置","conversation-save","check","primary")}${button("刷新对话列表","conversation-refresh","refresh")}${d.source ? button("恢复默认设置","conversation-remove","refresh","ghost") : ""}</div>`;
  }
  return head("对话与代理", "为群聊和私聊选择人设，并按需要共用一个持续对话。", "CONVERSATIONS", button("添加对话","conversation-new","plus")) +
    `<div class="card mb"><div class="row wrap"><strong>${data.conversations} 个对话</strong><span>共使用 ${data.contexts} 个持久上下文</span><span class="tag neutral">共享后减少重复预设与记忆加载</span></div></div><div class="personas-layout conversations-layout"><section class="card persona-list">${list || empty("还没有已知对话","收到消息后会出现在这里，也可以手动添加。","chat")}</section><section class="card persona-editor">${editor}</section></div>${(data.errors || []).map(message=>info(esc(message))).join("")}`;
}
async function conversationNew() {
  if (S.conversationDirty && !(await modal("添加对话","当前对话有未保存的设置。",{confirm:"放弃更改并添加"}))) return;
  if (!S.conversations) S.conversations = await rpc("conversations","list");
  S.conversationDraft = {account: S.conversations.accounts.find(a=>a.id===`onebot:${S.config.onebot.active_account}`)?.id || S.conversations.accounts[0]?.id || "",source:"",is_group:true,target_id:"",label:"",mode:"independent",persona_id:"",target:""};
  S.conversationDirty = false;
  render();
}
async function conversationSelect(el) {
  if (S.conversationDirty && !(await modal("切换对话编辑","当前对话有未保存的设置。",{confirm:"放弃更改并切换"}))) return;
  S.conversationDraft = conversationDraft(S.conversations.items[Number(el.dataset.index)]);
  S.conversationDirty = false;
  render();
}
async function conversationSave(remove=false) {
  const d = S.conversationDraft;
  if (!d) return;
  if (!d.target_id.trim()) throw new Error("请填写群号或私聊用户 ID。");
  await saveConfig();
  const source = conversationSource(d);
  const result = await rpc("conversations",remove ? "remove" : "save",{account:d.account,source,label:d.label || "",mode:d.mode,persona_id:d.mode === "independent" ? d.persona_id || "" : "",target:d.mode === "independent" ? "" : d.target || "",revision:S.revision});
  S.conversations = result;
  S.config.bot.conversations = clone(result.settings);
  S.savedConfig.bot.conversations = clone(result.settings);
  S.revision = result.revision;
  const saved = result.items.find(item=>item.source===source && item.account===d.account);
  S.conversationDraft = saved ? conversationDraft(saved) : null;
  S.conversationDirty = false;
  render();
  toast("对话设置已保存，下一条消息按新方式处理。");
}
document.addEventListener("input", event=>{
  const field = event.target.dataset.conversationField;
  if (!field || !S.conversationDraft) return;
  const d = S.conversationDraft;
  d[field] = field === "is_group" ? event.target.value === "group" : event.target.value;
  if (field === "account" || field === "is_group") {d.source="";d.target="";}
  if (field === "mode" && d.mode === "independent" && !d.persona_id) d.persona_id = d.separate_persona_id || "";
  S.conversationDirty = true;
  if (["account","is_group","mode"].includes(field)) render();
  else markDirty();
});
