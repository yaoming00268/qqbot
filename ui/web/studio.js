/* QQBot Studio. All UI assets are local; untrusted content is rendered as text. */
"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const clone = (value) => JSON.parse(JSON.stringify(value));
const iconPaths = {
  music: '<path d="M9 18V5l12-2v13M9 9l12-2"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="18" cy="16" rx="3" ry="3"/>',
  mic: '<rect x="8" y="2" width="8" height="13" rx="4"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
  chat: '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8z"/><path d="M8 10h8m-8 4h5"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a7 7 0 0 1 7-7h2a7 7 0 0 1 7 7v2"/>',
  book: '<path d="M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1zm0 0v15M6 8h3m6 0h3M6 12h3m6 0h3"/>',
  compass: '<circle cx="12" cy="12" r="9"/><path d="m16 8-3 5-5 3 3-5z"/>',
  folder:
    '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  heart:
    '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8z"/>',
  settings:
    '<path d="m9 3-1 3-3 1-2 3 2 2v3l3 2 1 4h6l1-4 3-2v-3l2-2-2-3-3-1-1-3z"/><circle cx="12" cy="12" r="3"/>',
  palette:
    '<circle cx="9" cy="7" r=".7"/><circle cx="15" cy="7" r=".7"/><circle cx="6" cy="12" r=".7"/><path d="M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1-3.7 1.5 1.5 0 0 1 .8-2.8H17A4 4 0 0 0 21 11a9 9 0 0 0-9-8z"/>',
  terminal:
    '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3m6 0h4"/>',
  play: '<path d="m8 4 12 8-12 8z"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  refresh:
    '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M7 5a8 8 0 0 1 12 4M5 15a8 8 0 0 0 12 4"/>',
  arrow: '<path d="m9 5 7 7-7 7"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-9v.2"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  layers: '<path d="m12 3 10 5-10 5L2 8zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
  spark:
    '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z"/>',
  globe:
    '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18M5 6h14M5 18h14"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  edit: '<path d="m15 4 5 5M4 15l12-12 5 5L9 20l-6 1z"/>',
  shield:
    '<path d="m12 3 9 4v6c0 5-9 9-9 9s-9-4-9-9V7z"/><path d="m8 12 3 3 5-6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
  chart: '<path d="M3 3v18h18M6 16l4-6 4 3 6-8"/>',
  file: '<path d="M14 2H5v20h14V7zm0 0v6h5M8 12h8m-8 4h6"/>',
};
function icon(name, cls = "") {
  return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${iconPaths[name] || iconPaths.grid}</svg>`;
}
function icons(root = document) {
  $$("[data-icon]", root).forEach((el) => {
    el.innerHTML = icon(el.dataset.icon);
  });
}
const pages = [
  ["overview", "概览", "grid"],
  ["messages", "对话监控", "chat"],
  ["conversations", "对话与代理", "layers"],
  ["personas", "角色预设", "user"],
  ["memory", "记忆与通讯录", "book"],
  ["autonomy", "自主活动", "compass"],
  ["resources", "资源与任务", "folder"],
  ["emotion", "情感工作室", "heart"],
  ["voice", "语音工作室", "mic"],
  ["voice-training", "声音训练", "chart"],
  ["singing", "唱歌工作室", "music"],
  ["broadcast", "直播与运营", "globe"],
];
const pageNames = Object.fromEntries([
  ...pages,
  ["settings", "设置"],
  ["appearance", "外观与个性化"],
  ["logs", "运行日志"],
]);
const S = {
  ready: false,
  page: "overview",
  config: {},
  savedConfig: {},
  schema: {},
  revision: "",
  preferences: {},
  status: { state: "stopped" },
  personas: { items: [] },
  cursor: 0,
  events: [],
  records: {},
  settingTab: "connection",
  memoryTab: "events",
  emotionTab: "analysis",
  resourceTab: "tasks",
  dataset: [],
  datasetDirty: false,
  history: [],
  predictions: [],
  models: [],
  voiceCatalog: null,
  voiceText: "こんにちは。いっしょにほんをよみましょう。",
  voiceEmotion: "neutral",
  voicePreview: null,
  voiceCheck: null,
  singingRequest: {mode: "cover", song: "", lyrics: "", accompaniment: "", vocals_only: false, emotion: "neutral", language: "ja", style: "Expressive clear solo female singing, melodic and natural.", pitch_shift: 0, song_output: "file", seed: 42},
  singingResult: null,
  singingStatus: "",
  dshCheck: "",
  invalid: new Set(),
  personaDraft: null,
  personaDirty: false,
  conversationDraft: null,
  conversationDirty: false,
  glossary: null,
  glossaryDirty: false,
};
const accentColors = {
  blue: "#4268cf",
  green: "#438978",
  violet: "#8375a6",
  slate: "#697a91",
};
const fieldNames = {
  "broadcast_voice.enabled": "启用直播专用语音",
  "broadcast_voice.provider": "直播合成接口",
  "broadcast_voice.character": "直播角色声音",
  "broadcast_voice.endpoint": "直播推理服务地址",
  "broadcast_voice.delivery_mode": "直播播放模式",
  "broadcast_voice.max_ahead_seconds": "播放器提前缓冲（秒）",
  "broadcast_voice.queue_blocks": "音频队列块数",
  "broadcast_voice.denoise.enabled": "直播参考音频降噪",
  "vtuber.enabled": "启用虚拟主播",
  "vtuber.model_path": "Live2D 模型路径",
  "vtuber.width": "画面宽度",
  "vtuber.height": "画面高度",
  "vtuber.vts_url": "VTube Studio 连接地址",
  "vtuber.obs_url": "OBS 连接地址",
  "vtuber.obs_scene": "OBS 场景名称",
  "vtuber.obs_source": "OBS 角色来源名称",
  "bilibili.enabled": "启用 B 站运营",
  "bilibili.room_id": "直播间号",
  "bilibili.area_id": "直播分区 ID",
  "bilibili.actor_id": "直播角色 ID",
  "bilibili.auto_listen": "启动时连接弹幕",
  "bilibili.auto_reply": "角色自主回复弹幕",
  "bilibili.allow_publish": "允许实际发布与开关播",
  streaming: "流式试听",
  stream_chunk_ms: "流式音频块（毫秒）",
  crossfade_ms: "句中切换淡化（毫秒）",
  sample_steps: "语音采样步数",
  reuse_models: "复用已加载的角色权重",
  reuse_audio: "复用完整语音缓存",
  reuse_separation: "复用已验证的歌曲分轨",
  local_bridge: "使用本机 VoiceStudio 集成",
  source_directory: "VoiceStudio 源码目录",
  backend_endpoint: "原生 GPT-SoVITS 地址",
  engine: "VoiceStudio 合成引擎",
  fallback_enabled: "连接失败时使用原生接口",
  "voice.denoise.enabled": "语音降噪",
  "voice.singing.denoise.enabled": "歌声降噪",
  reference: "清理参考音频底噪",
  strength_db: "降噪强度（dB）",
  noise_floor_db: "噪声基准（dB）",
  "voice.singing.enabled": "允许主代理唱歌",
  generation_python: "歌声生成 Python",
  generation_directory: "歌声生成引擎目录",
  max_input_mb: "歌曲输入上限（MB）",
  max_lyrics_chars: "歌词字数上限",
  diffusion_steps: "音色转换步数",
  generation_steps: "歌声生成步数",
  vocal_gain_db: "人声音量（dB）",
  accompaniment_gain_db: "伴奏音量（dB）",
  max_voice_seconds: "QQ 语音最长秒数",
  suspend_owned_tts: "唱歌时临时释放本项目 TTS 显存",
  "voice.provider": "合成接口",
  "voice.character": "角色声音",
  "deepseek_harness.provider": "模型服务商",
  speed: "语速倍率",
  model: "模型 ID",
  max_tokens: "单次输出上限（0 沿用默认）",
  base_url: "API 服务地址（可留空）",
  initialize_timeout_seconds: "运行时启动时限（秒）",
  model_aliases: "模型切换别名",
  max_text_chars: "朗读字数上限",
  ffmpeg_path: "ffmpeg 路径",
  backend: "模型后端",
  default_language: "语音语言",
  character: "当前角色 ID",
  catalog_path: "模型与情绪样本目录",
  endpoint: "推理服务地址",
  auto_start: "按需启动本地引擎",
  engine_python: "语音环境 Python",
  engine_directory: "推理引擎目录",
  engine_config: "推理模型配置",
  chunk_chars: "每句合成字数上限",
  max_duration_seconds: "单条语音最长秒数",
  pause_ms: "句间停顿（毫秒）",
  dictionary: "日语专名读音词典",
  home: "DSH 专用 Home",
  workspace: "DSH 工作区",
  api_key_env: "API Key 环境变量名",
  reasoning_effort: "推理强度（可留空）",
  mode: "运行模式",
  bot_name: "机器人名称",
  master_id: "主人 QQ / OpenID",
  master_title: "主人专属称谓",
  admin_ids: "管理员 ID",
  default_model: "默认模型",
  timeout_seconds: "接口等待时间（秒）",
  session_timeout_minutes: "会话保留时间（分钟）",
  appid: "App ID",
  secret: "App Secret",
  is_sandbox: "使用沙箱环境",
  enabled: "启用",
  ws_url: "WebSocket 地址",
  access_token: "访问令牌",
  active_account: "当前账号",
  accounts: "账号配置",
  actors: "角色配置",
  bindings: "角色绑定",
  autonomous_model_enabled: "自主模型决策",
  autonomous_enabled: "允许自主参与",
  never_sleep: "持续活动",
  idle_sleep_minutes: "空闲休眠（分钟）",
  proactive_chat_enabled: "主动发起话题",
  proactive_interval_minutes: "主动话题间隔（分钟）",
  self_activity_enabled: "启用自主探索",
  self_activity_interval_minutes: "自主探索间隔（分钟）",
  self_activity_roots: "已授权资料目录",
  search_enabled: "网络搜索",
  image_filter_enabled: "图片内容过滤",
  qq_window_capture: "QQ 窗口截图",
  memory_enabled: "启用长期记忆",
  memory_max_entries: "每群记忆条目上限",
  memory_history_count: "历史记忆加载数量",
  context_message_count: "上下文消息数量",
  memory_archive_interval_minutes: "记忆归档间隔（分钟）",
  idle_recall_minutes: "空闲回顾间隔（分钟）",
  max_reply_length: "单个气泡建议长度",
  reply_char_limit: "每轮回复总字数（0 不限制）",
  strip_mentions: "清理提及标记",
  enable_action_descriptions: "允许动作描写",
  chat_buffer_seconds: "群聊缓存窗口（秒）",
  chat_settle_seconds: "连续消息安静等待（秒）",
  chat_max_wait_seconds: "单批最长等待（秒）",
  backlog_interrupt_messages: "积压中断阈值（条，0 关闭）",
  passive_backlog: "繁忙时保留的参聊机会",
  system_prompt: "全局系统预设",
  group_settings: "群聊独立配置",
  work_permissions: "工作权限",
  runtime_database_path: "运行数据库",
  provider: "分析方式",
  lms_host: "LM Studio 服务地址",
  llm_model: "语言模型",
  embedding_model: "嵌入模型",
  classifier_path: "本地分类器路径",
  bert_service_file: "BERT 推理服务配置",
  dataset_path: "标注数据路径",
  labels: "情感标签集",
  training_features: "训练特征",
  feature_dim: "特征维度",
  epochs: "训练轮数",
  batch_size: "批大小",
  learning_rate: "学习率",
  l2: "L2 正则项",
  validation_fraction: "验证集比例",
  seed: "随机种子",
  directory: "资料目录",
  cooldown_seconds: "冷却间隔（秒）",
  interval_minutes: "活动间隔（分钟）",
  startup_timeout_seconds: "启动等待时间（秒）",
  max_actions: "每次动作上限",
  profiles: "应用活动配置",
  browser_profiles: "浏览器配置",
  browser_executable: "浏览器可执行文件",
  galgame_reading: "视觉小说阅读",
  vm: "虚拟机",
  database_path: "数据库路径",
  image_enabled: "图片检索与发送",
  image_generation_enabled: "图片生成",
  video_enabled: "视频发送",
  video_generation_enabled: "视频生成",
  file_enabled: "文件与文档发送",
  local_library_roots: "本地素材目录",
  cache_dir: "缓存目录",
  cache_max_mb: "缓存容量（MB）",
  task_timeout_seconds: "下载任务时限（秒，0 不限制）",
  max_concurrent_downloads: "下载并发数",
  search_provider: "搜索服务",
  search_api_url: "搜索 API 地址",
  gemini_web: "Gemini 网页连接",
  transport: "连接方式",
  page_profile_dir: "浏览器登录资料目录",
  image_generation_provider: "图片生成服务",
  image_generation_model: "图片生成模型",
  video_generation_provider: "视频生成服务",
  default_image_count: "默认图片数量",
  max_image_count: "图片数量上限",
  max_image_mb: "单张图片上限（MB）",
  max_video_mb: "视频文件上限（MB）",
  max_file_mb: "下载文件上限（MB）",
  default_video_max_seconds: "视频时长上限（秒）",
  require_explicit_group_request: "群媒体请求需要明确触发",
  napcat_stream_enabled: "NapCat 分块上传",
  official_file_enabled: "官方通道文件发送",
  ffmpeg_path: "FFmpeg 路径",
  ffprobe_path: "FFprobe 路径",
  napcat_path: "NapCat 路径",
  reconnect_interval: "重连间隔（秒）",
  message_send_timeout_seconds: "消息回执等待（秒）",
  expire_seconds: "发送有效期（秒）",
  max_content_chars: "阅读内容字数上限",
  max_concurrent_activities: "自主活动并发数",
  action_timeout_ms: "页面操作时限（毫秒）",
  reflection_timeout_seconds: "复盘等待（秒）",
  language_server_path: "模型服务程序",
  ls_address: "模型服务地址",
  csrf_token: "CSRF 令牌",
  project_id: "项目 ID",
  delivery_batch_size: "每批交付文件数",
  delivery_max_file_mb: "交付文件上限（MB）",
  image_generation_api_url: "图片生成 API",
  image_generation_api_key_env: "图片生成密钥环境变量",
  search_api_key_env: "搜索密钥环境变量",
  publish_upload_url: "文件发布地址",
  publish_token_env: "文件发布密钥环境变量",
  commons_api_url: "Commons API 地址",
  download_timeout_seconds: "单次下载等待（秒）",
  upload_timeout_seconds: "单次上传等待（秒）",
  cache_ttl_hours: "缓存保留（小时）",
  result_ttl_minutes: "结果保留（分钟）",
  per_conversation_queue_limit: "单会话队列兼容项",
  max_pending_jobs: "待处理任务兼容项",
  preset_reminder_interval: "预设提示间隔",
  reply_probability: "参聊概率兼容项",
  mention_probability: "提及概率兼容项",
  delayed_quote_seconds: "延迟引用兼容项",
};
const enumNames = {
  antigravity: "Antigravity",
  deepseek_harness: "DeepSeek Harness（官方 SDK）",
  gpt_sovits_v2: "GPT-SoVITS api_v2",
  voicestudio: "VoiceStudio 流式服务",
  gpt_sovits_inference: "GPT-SoVITS-Inference 角色情绪接口",
  ja: "日语（完整假名）",
  zh: "中文",
  en: "英语",
  mixed: "混合语言",
  official: "官方 QQ",
  onebot: "OneBot / NapCat",
  dual: "双通道",
  agent: "独立角色",
  main: "主会话分析",
  bert: "BERT 本地服务",
  lmstudio: "LM Studio",
  local_classifier: "本地分类器",
  char_hash: "本地字符特征",
  lmstudio_embedding: "LM Studio 嵌入",
  flash: "Flash",
  pro: "Pro",
  auto: "自动",
  disabled: "关闭",
  browser: "浏览器",
  cookie: "Cookie 连接",
  desktop: "桌面",
  vm: "虚拟机",
};
function pathKeys(path) {
  return path.split(".").map(decodeURIComponent);
}
function get(path, object = S.config) {
  return pathKeys(path).reduce((v, k) => v?.[k], object);
}
function put(path, value, object = S.config) {
  const keys = pathKeys(path);
  let target = object;
  keys.slice(0, -1).forEach((k) => {
    target = target[k];
  });
  target[keys.at(-1)] = value;
}
function schemaAt(path) {
  let schema = S.schema;
  for (const key of pathKeys(path)) {
    if (schema.$ref) schema = S.schema.$defs[schema.$ref.split("/").at(-1)];
    schema = schema.properties?.[key] || schema.additionalProperties || {};
  }
  if (schema.$ref) schema = S.schema.$defs[schema.$ref.split("/").at(-1)];
  return schema;
}
function dirty() {
  return (
    JSON.stringify(S.config) !== JSON.stringify(S.savedConfig) ||
    S.invalid.size > 0
  );
}
function hasEdits() {
  return dirty() || S.conversationDirty || S.personaDirty || S.datasetDirty || S.glossaryDirty || Object.keys(S.voiceAnnotationEdits || {}).length > 0;
}
function markDirty() {
  $("#dirty-indicator").hidden = !hasEdits();
  $("#save-global").disabled = !hasEdits();
}
async function rpc(method, ...args) {
  if (!window.pywebview?.api) throw new Error("桌面桥接尚未就绪，请稍后重试。");
  const result = await window.pywebview.api[method](...args);
  if (!result?.ok) throw new Error(result?.error || "操作未完成");
  return result.data;
}
function toast(text, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = text;
  $("#toast-region").append(node);
  setTimeout(() => node.remove(), type === "error" ? 9500 : 4000);
}
async function attempt(operation) {
  try {
    return await operation();
  } catch (error) {
    toast(error.message, "error");
    return null;
  }
}
function date(value, withTime = true) {
  if (!value) return "未记录时间";
  const d = new Date(typeof value === "number" ? value * 1000 : value);
  return isNaN(d)
    ? esc(value)
    : d.toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        month: "2-digit",
        day: "2-digit",
        ...(withTime
          ? { hour: "2-digit", minute: "2-digit" }
          : { year: "numeric" }),
      });
}
function time(value) {
  return new Date((value || Date.now() / 1000) * 1000).toLocaleTimeString(
    "zh-CN",
    { timeZone: "Asia/Shanghai", hour12: false },
  );
}
function empty(title, description, ic = "layers", action = "") {
  return `<div class="empty"><div class="empty-icon">${icon(ic)}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`;
}
function head(title, subtitle, eyebrow, actions = "") {
  return `<div class="page-head"><div><div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></div><div class="page-head-actions">${actions}</div></div>`;
}
function button(label, action, ic = "", cls = "", extra = "") {
  return `<button type="button" class="button ${cls}" data-action="${action}" ${extra}>${ic ? icon(ic) : ""}${esc(label)}</button>`;
}
function segmented(values, current, kind) {
  return `<div class="segmented">${values.map(([key, title]) => `<button data-${kind}="${key}" class="${current === key ? "active" : ""}">${esc(title)}</button>`).join("")}</div>`;
}
function info(text) {
  return `<div class="info-strip">${icon("info")}<div>${text}</div></div>`;
}
function checked(value) {
  return value ? " checked" : "";
}
function parse(value, fallback = {}) {
  try {
    return typeof value === "string" ? JSON.parse(value) : value || fallback;
  } catch {
    return fallback;
  }
}
function field(path, options = {}) {
  let schema = schemaAt(path),
    value = get(path);
  const key = pathKeys(path).at(-1);
  const title =
    options.label ||
    fieldNames[path] ||
    fieldNames[key] ||
    schema.description?.split(/[；。\n]/)[0] ||
    key;
  const description = options.hint ?? schema.description ?? "";
  let type = schema.type;
  const complex =
    Array.isArray(value) || (value !== null && typeof value === "object");
  const secret = /secret|token|api_key$/.test(key);
  let control = "";
  if (typeof value === "boolean") {
    return `<div class="toggle-row"><div><strong>${esc(title)}</strong>${description ? `<p>${esc(description)}</p>` : ""}</div><label class="switch"><input type="checkbox" data-config="${esc(path)}" aria-label="${esc(title)}"${checked(value)}><span></span></label></div>`;
  }
  const voiceCharacters = path === "voice.character" ? S.voiceCatalog?.catalog?.characters
    : path === "broadcast_voice.character" ? S.broadcastVoiceCatalog?.catalog?.characters : null;
  const voiceChoices = voiceCharacters && Object.keys(voiceCharacters).length
    ? [...new Set([...Object.keys(voiceCharacters), ...(value ? [value] : [])])]
    : null;
  const choiceNames = options.choiceNames || (path === "broadcast_voice.delivery_mode" ? {streaming:"即时流式 · 优先首音",validated:"先校验片段 · 优先完整性"} : voiceCharacters
    ? Object.fromEntries(Object.entries(voiceCharacters).map(([id, character]) => [id, `${character.name || id} · ${id}`]))
    : {});
  const enumValues =
    options.choices ||
    voiceChoices ||
    schema.enum ||
    schema.anyOf?.find((item) => item.enum)?.enum;
  if (enumValues) {
    control = `<select data-config="${esc(path)}" aria-label="${esc(title)}">${enumValues.map((v) => `<option value="${esc(v)}"${value === v ? " selected" : ""}>${esc(choiceNames[v] || enumNames[v] || v)}</option>`).join("")}</select>`;
  } else if (complex) {
    control = `<textarea class="json-field" data-config="${esc(path)}" data-type="json" spellcheck="false" aria-label="${esc(title)}">${esc(JSON.stringify(value, null, 2))}</textarea>`;
  } else if (options.multiline || key === "system_prompt") {
    control = `<textarea data-config="${esc(path)}" aria-label="${esc(title)}">${esc(value)}</textarea>`;
  } else {
    control = `<input type="${typeof value === "number" ? "number" : secret ? "password" : "text"}" value="${esc(value)}" data-config="${esc(path)}" ${typeof value === "number" ? `step="${type === "integer" ? "1" : "any"}" ${schema.minimum !== undefined ? `min="${schema.minimum}"` : ""} ${schema.maximum !== undefined ? `max="${schema.maximum}"` : ""}` : ""} aria-label="${esc(title)}" autocomplete="off" spellcheck="false" ${key === "llm_model" ? 'list="lms-models"' : key === "embedding_model" ? 'list="lms-embeddings"' : ""}>`;
  }
  return `<div class="field ${complex || options.full ? "full" : ""}" data-field="${esc(path)}"><label>${esc(title)}</label>${control}${description ? `<span class="hint">${esc(description)}</span>` : ""}</div>`;
}
function fields(paths) {
  return `<div class="form-grid">${paths
    .filter((p) => get(p) !== undefined && typeof get(p) !== "boolean")
    .map((p) => field(p))
    .join("")}</div>${paths
    .filter((p) => typeof get(p) === "boolean")
    .map((p) => field(p))
    .join("")}`;
}
function formCard(title, subtitle, paths) {
  return `<section class="card"><div class="card-head"><div><h2>${esc(title)}</h2>${subtitle ? `<small>${esc(subtitle)}</small>` : ""}</div></div>${fields(paths)}</section>`;
}

function applyAppearance(p) {
  const theme =
    p.theme === "system"
      ? matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light"
      : p.theme;
  document.documentElement.dataset.theme = theme;
  const style = document.documentElement.style;
  style.setProperty("--density", p.surface_opacity / 100);
  style.setProperty("--blur", p.blur + "px");
  style.setProperty("--font-size", p.font_size + "px");
  const color = accentColors[p.accent] || accentColors.blue;
  style.setProperty("--accent", color);
  style.setProperty(
    "--accent-rgb",
    color
      .slice(1)
      .match(/../g)
      .map((h) => parseInt(h, 16))
      .join(","),
  );
  document.body.classList.toggle("reduced-motion", p.reduced_motion);
}
function updateStatus() {
  const voiceLabel = $("#voice-live-status");
  if (voiceLabel) voiceLabel.textContent = voiceLiveLabel();
  const state = S.status.state,
    run = state === "running",
    stop = state === "stopping";
  document.body.classList.toggle("running", run);
  document.body.classList.toggle("stopping", stop);
  const label = run ? "核心运行中" : stop ? "正在保存并停止" : "核心未运行";
  $("#top-status").textContent = label;
  $("#side-status").textContent = run ? "运行中" : stop ? "正在停止" : "已就绪";
  const btn = $("#start-button");
  btn.innerHTML =
    icon(run ? "stop" : "play") +
    (run ? "停止 Bot" : stop ? "正在停止" : "启动 Bot");
  btn.dataset.action = run ? "stop" : "start";
  btn.disabled = stop || S.status.closing;
  $("#footer-state").textContent = S.status.closing
    ? "正在等待任务退出，请稍候"
    : run
      ? `核心 PID ${S.status.pid} · 持续运行 ${Math.max(0, Math.floor((Date.now() / 1000 - S.status.started_at) / 60))} 分钟`
      : "本地工作区 · 所有更改保存在本机";
}
async function navigate(page) {
  if (!pageNames[page]) return;
  if (S.page === "voice" && page !== "voice") stopVoicePlayback();
  S.page = page;
  $$("[data-page]").forEach((el) =>
    el.classList.toggle("active", el.dataset.page === page),
  );
  $("#breadcrumb").textContent = pageNames[page];
  render();
  $("#main-content").scrollTop = 0;
  if (["overview", "resources", "memory", "autonomy", "voice", "singing", "voice-training", "broadcast", "conversations"].includes(page))
    await loadRecords(page);
}
function render() {
  const renderers = {
    overview: overviewPage,
    messages: messagesPage,
    conversations: conversationsPage,
    personas: personasPage,
    memory: memoryPage,
    autonomy: autonomyPage,
    resources: resourcesPage,
    emotion: emotionPage,
    voice: voicePage,
    "voice-training": voiceTrainingPage,
    broadcast: broadcastPage,
    singing: () => head("唱歌工作室", "给一首歌和歌词，让当前角色开口演唱。", "SINGING STUDIO", button("选择角色声音", "go-voice", "mic")) + singingPanel(),
    settings: settingsPage,
    appearance: appearancePage,
    logs: logsPage,
  };
  $("#main-content").innerHTML =
    `<div class="page" data-view="${S.page}">${renderers[S.page]()}</div>`;
  icons($("#main-content"));
  markDirty();
  if (S.page === "logs") scrollLogs();
}
async function loadRecords(page) {
  await attempt(async () => {
    if (page === "conversations") S.conversations = await rpc("conversations", "list");
    if (["overview", "resources"].includes(page))
      S.records.tasks = await rpc("records", "tasks", "", 100);
    if (["overview", "memory"].includes(page))
      S.records.memory = await rpc("records", "memory", "", 100);
    if (page === "memory" && !S.glossaryDirty)
      S.glossary = await rpc("glossary", "read");
    if (page === "autonomy")
      S.records.autonomy = await rpc("records", "autonomy", "", 100);
    if (page === "resources" && S.resourceTab === "stickers")
      S.records.stickers = await rpc("records", "stickers", "", 100);
    if (["voice", "singing"].includes(page))
      S.voiceCatalog = await rpc("voice", "catalog", { config: S.config.voice });
    if (page === "voice-training") {
      S.voiceTrainingInfo = await rpc("voice_training", "status", {config:S.config.voice});
    }
    if (page === "broadcast") await broadcastRefresh(false);
    if (S.page === page) render();
  });
}
function tasks() {
  const rec = S.records.tasks || {};
  return [
    ...(rec.work || []).map((r) => ({ ...r, origin: "后台工作" })),
    ...(rec.media || []).map((r) => ({ ...r, origin: "媒体任务" })),
    ...(rec.actors || []).map((r) => ({ ...r, origin: "网站活动" })),
  ].sort((a, b) => (b.created || 0) - (a.created || 0));
}
function activeTasks() {
  return tasks().filter(
    (r) =>
      ![
        "completed",
        "accepted",
        "failed",
        "cancelled",
        "delivered",
        "confirmed",
        "unknown",
        "interrupted",
      ].includes(r.state),
  );
}
function recentMessages() {
  return S.events.filter((item) => ["message", "reply"].includes(item.kind));
}
function overviewPage() {
  const running = S.status.state === "running",
    persona = S.personas.items.find((p) => p.name === S.personas.active);
  const mem = S.records.memory || {};
  const metrics = [
    [
      "运行状态",
      running ? "运行中" : "未启动",
      running ? "对话与任务正在处理" : "准备好后即可启动",
      "layers",
      "logs",
    ],
    ["待处理任务", activeTasks().length, "最近任务队列", "clock", "resources"],
    [
      "结构化记忆",
      (mem.events || []).filter((r) => !r.deleted).length,
      "最近载入的事件记录",
      "book",
      "memory",
    ],
    [
      "角色预设",
      S.personas.items.length,
      "保留身份与上下文",
      "user",
      "personas",
    ],
  ];
  return (
    head(
      "工作空间",
      "把对话、记忆与自主活动，放在一个清晰的地方。",
      "YOUR COMPANION, CONNECTED",
      `<span class="tag neutral">${date(Date.now() / 1000, false)}</span>`,
    ) +
    `<section class="card hero"><div class="hero-copy"><div class="hero-kicker"><span class="status-dot"></span>${running ? "陪伴正在继续" : "一切就绪，随时开始"}</div><h2>${running ? "让对话自然发生。" : "从一段新的对话开始。"}</h2><p>当前角色 <strong>${esc(persona?.name || "尚未配置")}</strong><br>会话与长期记忆持续保留，自主探索沿用上次的发现。</p><div class="hero-actions">${button(running ? "查看实时对话" : "启动 Bot", running ? "go-messages" : "start", running ? "chat" : "play", "primary")}${button("管理角色", "go-personas", "user", "ghost")}</div></div><div class="hero-art"><svg viewBox="0 0 220 190"><defs><radialGradient id="orb"><stop stop-color="#dfeaf4"/><stop offset="1" stop-color="#b4cadd" stop-opacity=".15"/></radialGradient></defs><circle class="orb" cx="113" cy="94" r="61" style="fill:url(#orb)"/><ellipse cx="113" cy="94" rx="93" ry="37" transform="rotate(-30 113 94)"/><ellipse cx="113" cy="94" rx="82" ry="47" transform="rotate(42 113 94)"/><circle cx="113" cy="94" r="36"/><circle cx="174" cy="47" r="4" style="fill:#8eadd0"/><circle cx="42" cy="132" r="2.5" style="fill:#8eadd0"/></svg></div></section><div class="grid four metrics">${metrics.map(([title, num, note, ic, page]) => `<button class="card metric" data-page="${page}"><div class="metric-top">${title}<span class="metric-icon">${icon(ic)}</span></div><div class="metric-number" style="${typeof num === "string" ? "font-size:1.64286rem;margin-top:8px" : ""}">${num}${typeof num === "number" ? "<span>项</span>" : ""}</div><div class="metric-note">${note}</div></button>`).join("")}</div><div class="grid asymmetric"><section class="card"><div class="card-head"><div><h2>最近对话</h2><small>来自当前核心的实时消息</small></div><button class="button text" data-page="messages">查看全部 ${icon("arrow")}</button></div><div id="overview-feed">${recentMessages().length ? recentMessages().slice(-3).map(messageCard).join("") : empty("此刻，安静也很好", "启动 Bot 后，新的消息与回复会出现在这里。", "chat")}</div></section><section class="card dashboard-secondary"><div class="card-head"><div><h2>工作区速览</h2><small>常用功能，触手可及</small></div><span class="tag neutral">本地</span></div>${[
      ["memory", "book", "事件与人物", "按时间和标签整理每一次经历"],
      ["emotion", "heart", "情感工作室", "本地分析、逐句标注与模型训练"],
      ["appearance", "palette", "调成你喜欢的样子", "毛玻璃、透明度和明暗主题"],
    ]
      .map(
        ([page, ic, title, sub]) =>
          `<div class="quick-item" data-page="${page}" role="button" tabindex="0"><span class="quick-icon">${icon(ic)}</span><div><strong>${title}</strong><p>${sub}</p></div>${icon("arrow", "arrow")}</div>`,
      )
      .join("")}</section></div>`
  );
}
function messageCard(item) {
  const r = item.data,
    bot = item.kind === "reply";
  return `<article class="message ${bot ? "bot" : ""}"><div class="message-header"><span class="avatar" style="width:25px;height:25px;border-radius:8px;font-size:0.71429rem">${esc((bot ? "B" : r.sender || "用").slice(0, 1))}</span><strong>${esc(bot ? "Bot" : r.sender || r.user_id || "用户")}</strong><span class="tag neutral">${esc(r.group_id ? "群 " + r.group_id : "私聊")}</span><time>${esc(r.time || time(item.time))}</time></div><div class="message-text">${esc(bot ? r.reply || r.reason || "本轮暂未发言" : r.content || "[非文本消息]")}</div>${!bot && S.page === "messages" ? `<div class="message-actions">${button("设置主人", "master", "", "text", `data-seq="${item.seq}"`)}${button("复制", "copy-message", "copy", "ghost small", `data-seq="${item.seq}"`)}</div>` : ""}</article>`;
}
function messagesPage() {
  return (
    head("对话监控", "跟进每条消息，也看见 Bot 如何回应。", "CONVERSATIONS") +
    `<section class="card"><div class="messages-tools"><div class="search-input">${icon("search")}<input id="message-query" placeholder="搜索消息、昵称或用户 ID" aria-label="搜索对话"></div><input id="message-group" placeholder="按群号筛选" aria-label="按群号筛选" style="width:160px;font-size:0.78571rem"><label class="row muted" style="font-size:0.78571rem;margin-left:auto"><input type="checkbox" id="feed-autoscroll" checked>自动跟随</label>${button("清空视图", "clear-feed", "trash", "small ghost")}</div><div id="message-feed" class="message-list">${recentMessages().length ? recentMessages().map(messageCard).join("") : empty("等待新的消息", "这里显示本次启动的实时对话。清空视图不会删除聊天记录或长期记忆。", "chat")}</div></section>`
  );
}
function personasPage() {
  if (!S.personaDraft) {
    S.personaDraft = clone(
      S.personas.items.find((p) => p.name === S.personas.active) ||
        S.personas.items[0] || {
          name: "",
          prompt: "",
          description: "",
          tags: [],
          aliases: [],
          greeting: "",
          avatar: "",
        },
    );
    S.personaOriginal = S.personaDraft.name;
  }
  const p = S.personaDraft;
  return (
    head(
      "角色预设",
      "稳定的身份，持续的记忆，以及自己的性格。",
      "PERSONAS",
      button("新建角色", "new-persona", "plus"),
    ) +
    `<div class="personas-layout"><section class="card persona-list">${S.personas.items.map((r, i) => `<button class="persona-item ${r.name === S.personaOriginal ? "active" : ""}" data-persona-index="${i}"><span class="avatar">${esc(r.name.slice(0, 1))}</span><span class="grow"><strong>${esc(r.name)}</strong><p>${esc(r.description || "暂无角色简介")}</p>${r.name === S.personas.active ? '<span class="tag" style="margin-top:7px">当前角色</span>' : ""}</span></button>`).join("")}</section><section class="card persona-editor"><div class="card-head"><div><h2>${esc(S.personaOriginal ? "编辑角色" : "新的角色")}</h2><small>保存角色时保留原有身份 ID 和记忆关联</small></div>${p.id ? `<span class="tag neutral">${esc(p.id.slice(0, 12))}</span>` : ""}</div><div class="form-grid">${[
      ["name", "角色名称"],
      ["description", "一句话简介"],
    ]
      .map(
        ([key, label]) =>
          `<div class="field"><label>${label}</label><input data-persona-field="${key}" value="${esc(p[key])}" aria-label="${label}"></div>`,
      )
      .join(
        "",
      )}<div class="field full"><label>角色预设</label><textarea class="prompt" data-persona-field="prompt" aria-label="角色预设" spellcheck="false">${esc(p.prompt)}</textarea><span class="hint">描述角色的性格、关系和表达习惯。自主成长会在原有预设上持续积累。</span></div>${[
      ["tags", "风格标签"],
      ["aliases", "昵称与简称"],
      ["greeting", "切换问候语"],
      ["avatar", "头像路径或 URL"],
    ]
      .map(
        ([key, label]) =>
          `<div class="field"><label>${label}</label><input data-persona-field="${key}" value="${esc(Array.isArray(p[key]) ? p[key].join("，") : p[key] || "")}" aria-label="${label}">${["tags", "aliases"].includes(key) ? '<span class="hint">多项使用逗号分隔</span>' : ""}</div>`,
      )
      .join(
        "",
      )}</div><div class="separator"></div><div class="row wrap">${button("保存角色", "save-persona", "check", "primary")}${button("设为当前角色", "activate-persona", "user", "", S.personaOriginal ? "" : "disabled")}<span class="grow"></span>${button("删除角色", "delete-persona", "trash", "danger", S.personaOriginal ? "" : "disabled")}</div></section></div>`
  );
}
function memoryPage() {
  const r = S.records.memory || {},
    q = (S.memoryQuery || "").toLowerCase(),
    filter = (rows) =>
      (rows || []).filter(
        (x) =>
          !x.deleted && (!q || JSON.stringify(x).toLowerCase().includes(q)),
      );
  let content = "";
  if (S.memoryTab === "glossary") {
    content = `<div class="field full"><label for="glossary-text">话术表（Markdown）</label><textarea id="glossary-text" class="code" spellcheck="false" style="min-height:360px">${esc(S.glossary?.text || "")}</textarea><span class="hint">沿用现有 Markdown 格式。保存时检测版本，避免覆盖机器人刚写入的内容。</span></div><div class="row mt">${button("保存话术表", "save-glossary", "check", "primary")}${S.glossaryDirty ? '<span class="muted">有未保存的修改</span>' : ""}</div>`;
  } else if (S.memoryTab === "events") {
    const rows = filter(r.events);
    content = rows.length
      ? `<div class="timeline">${rows
          .map((row) => {
            const d = parse(row.data);
            return `<article class="record"><div class="row between"><h3>${esc(d.title || row.id)}</h3><span class="tag neutral">${date(d.started_at || row.updated)}</span></div><p>${esc(d.summary || d.description || d.text || "")}</p><div class="record-meta">${(d.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}<span class="scope" title="${esc(row.scope)}">${esc(row.scope)}</span><span>修订 ${row.revision}</span></div></article>`;
          })
          .join("")}</div>`
      : empty(
          "事件会慢慢积累",
          "每次记忆归档后，事件、人物与时间线会在这里分区呈现。",
          "book",
        );
  } else if (S.memoryTab === "contacts") {
    const rows = filter(r.contacts);
    content = rows.length
      ? `<div class="grid two">${rows
          .map((row) => {
            const d = parse(row.profile);
            return `<article class="message"><div class="row"><span class="avatar">${esc((d.preferred_name || row.observed || "用").slice(0, 1))}</span><div><h3>${esc(d.preferred_name || row.observed || row.user_id)}</h3><small>${esc(row.user_id)}</small></div></div><div class="record-meta">${(d.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div><p class="section-note">${esc([...(d.facts || []), ...(d.preferences || []), d.notes || ""].filter(Boolean).join("；") || "尚未形成详细人物记录")}</p><div class="record-meta"><span class="scope">${esc(row.scope)}</span><span>${date(row.updated)}</span></div></article>`;
          })
          .join("")}</div>`
      : empty(
          "每段关系，从认识开始",
          "真实用户身份、称谓、偏好和互动经历将在归档时持续更新。",
          "user",
        );
  } else {
    const rows = filter(r.growth);
    content = rows.length
      ? rows
          .map(
            (row) =>
              `<article class="record"><div class="row between"><h3>个人成长预设</h3><span class="tag neutral">修订 ${row.revision}</span></div><p>${esc(row.text)}</p><div class="record-meta"><span class="scope">${esc(row.owner)}</span><span>${date(row.updated)}</span></div></article>`,
          )
          .join("")
      : empty(
          "成长需要真实的经历",
          "主代理对归档进行复盘后，成长内容会保存在这里。",
          "compass",
        );
  }
  return (
    head(
      "记忆与通讯录",
      "事件、人物和成长，各有自己的位置。",
      "MEMORY & RELATIONSHIPS",
      button("刷新资料", "refresh-records", "refresh"),
    ) +
    `<section class="card"><div class="card-head">${segmented(
      [
        ["events", "事件簿"],
        ["contacts", "通讯录"],
        ["growth", "自主成长"],
        ["glossary", "话术表"],
      ],
      S.memoryTab,
      "memory-tab",
    )}<div class="search-input">${icon("search")}<input id="memory-query" placeholder="搜索内容、时间或标签" aria-label="搜索记忆" value="${esc(S.memoryQuery || "")}"></div></div><div id="memory-records">${content}</div><p class="section-note">显示最近 100 条本地记录。资料按账号、人设与会话保留来源。</p>${(r.errors || []).length ? info(esc(r.errors.join("；"))) : ""}</section>`
  );
}
function autonomyPage() {
  return (
    head(
      "自主活动",
      "让好奇心有方向，也让每次探索有所延续。",
      "AUTONOMY",
      button("刷新活动", "refresh-records", "refresh"),
    ) +
    `<div class="grid two">${formCard("参与和主动交流", "由现有活动开关控制，不打断正常聊天", ["bot.autonomous_enabled", "bot.never_sleep", "bot.idle_sleep_minutes", "bot.proactive_chat_enabled", "bot.proactive_interval_minutes"])}${formCard("探索与回顾", "本机资料仅使用 bot 工作区和已授权目录", ["bot.self_activity_enabled", "bot.self_activity_interval_minutes", "bot.idle_recall_minutes", "bot.memory_archive_interval_minutes", "bot.self_activity_roots"])}</div><div class="mt">${formCard("桌面、浏览器与应用", "配置浏览器资料目录及允许活动的应用", ["sandbox.enabled", "sandbox.autonomous_enabled", "sandbox.mode", "sandbox.interval_minutes", "sandbox.browser_executable", "sandbox.profiles", "sandbox.browser_profiles"])}</div><details class="card settings-group mt"><summary>阅读、虚拟机与其余活动设置</summary>${fields(["sandbox.directory", "sandbox.startup_timeout_seconds", "sandbox.max_actions", "sandbox.galgame_reading", "sandbox.vm"])}</details><div class="mt">${info("更改保存在本地配置。探索与交流需同时启用自主参与；活动间隔、资料目录和浏览器配置会沿用已有授权。")}</div>${activityHistory()}`
  );
}
function activityHistory() {
  const r = S.records.autonomy || {},
    sessions = r.sessions || [],
    notes = r.notes || [];
  return `<section class="card mt"><div class="card-head"><div><h2>最近活动与阅读记录</h2><small>沿用现有软件、浏览器和游玩记录</small></div></div>${
    sessions.length
      ? sessions
          .slice(0, 12)
          .map(
            (row) =>
              `<article class="record"><div class="row between wrap"><h3>${esc(row.title)}</h3><span class="tag neutral">${esc(stateNames[row.state] || row.state)}</span></div><p>${esc(row.summary || "本轮尚无总结。")}</p><div class="record-meta"><span>${date(row.created)}</span><span>${row.actions || 0} 次操作</span><span class="scope">${esc(row.scope)}</span></div>${["running", "paused", "failed", "interrupted"].includes(row.state) ? `<div class="row mt">${button("跳过当前内容", "recover-skip", "arrow", "small", `data-session="${esc(row.id)}"`)}${button("重启执行分支", "recover-restart", "refresh", "small", `data-session="${esc(row.id)}"`)}</div>` : ""}</article>`,
          )
          .join("")
      : empty(
          "还没有自主活动记录",
          "阅读、软件使用和游玩完成后，会在这里留下过程与总结。",
          "compass",
        )
  }${
    notes.length
      ? `<div class="separator"></div><h3>观察与感受</h3>${notes
          .slice(0, 20)
          .map(
            (row) =>
              `<article class="record"><div class="row between wrap"><strong>${esc(row.title)}</strong><span class="tag neutral">${esc({ plot: "虚构剧情", observation: "页面观察", reflection: "主观感受", preference: "个人偏好" }[row.kind] || row.kind)}</span></div><p>${esc(row.text)}</p><small>${date(row.created)}</small></article>`,
          )
          .join("")}`
      : ""
  }${(r.errors || []).length ? info(esc(r.errors.join("；"))) : ""}</section>`;
}
function browserPanel() {
  const pageMode = S.config.media.gemini_web.transport === "browser";
  const browser = S.config.media.gemini_web.browser;
  const choices = [...new Set(["auto", "chrome", "edge", browser])];
  const modelHint = pageMode ? `<p class="section-note">浏览器页面模式支持 auto、flash、thinking 和 pro 模型。网页提供临时对话时会使用该功能，否则可能产生普通对话历史。</p>` : "";
  const modeFields = pageMode
    ? `<div class="form-grid">${field("media.gemini_web.browser", { choices })}${field("media.gemini_web.page_profile_dir")}</div>`
    : fields(["media.gemini_web.browser", "media.gemini_web.browser_profile", "media.gemini_web.auto_browser"]);
  const status = S.browserStatus || (pageMode
    ? "请打开浏览器并手动登录 Gemini，关闭登录窗口后读取账号模型。关闭窗口会释放专用登录资料目录。支持自动选择、Chrome 和 Edge。"
    : "首次连接会打开本机连接页，在已登录 Gemini 的浏览器中完成。");
  return `<section class="card mt"><div class="card-head"><div><h2>Gemini 网页连接</h2><small>选择连接方式并检查账号可用模型</small></div></div>${fields(["media.gemini_web.transport", "media.gemini_web.model"])}${modeFields}${modelHint}<div class="row mt wrap">${button(pageMode ? "打开浏览器登录" : "连接当前浏览器", "connect-browser", "globe")}${button("读取账号模型", "inspect-browser", "refresh")}${pageMode ? "" : button("打开扩展目录", "open-extension", "folder", "ghost")}</div><p class="section-note" id="browser-status">${esc(status)}</p></section>`;
}
const stateNames = {
  running: "运行中",
  queued: "排队中",
  pending: "等待中",
  completed: "已完成",
  accepted: "已受理",
  failed: "失败",
  cancelled: "已取消",
  unknown: "结果不确定",
  ready: "等待交付",
  delivered: "已交付",
  interrupted: "已中断",
  confirmed: "已确认",
  submitted: "已提交",
  searching: "搜索中",
  downloading: "下载中",
  verifying: "分析中",
  uploading: "发送中",
  cancel_requested: "正在取消",
};
function taskTable(rows) {
  if (!rows.length)
    return empty(
      "暂时没有任务",
      "启动 Bot 后，媒体处理、子代理工作和网站活动会显示在这里。",
      "folder",
    );
  return `<div class="table-wrap"><table><thead><tr><th>任务 / 来源</th><th>内容</th><th>状态</th><th>时间</th></tr></thead><tbody>${rows.map((r) => `<tr><td><strong>${esc(r.origin)}</strong><br><small>${esc((r.id || "").slice(0, 14))}</small></td><td>${esc(r.label || r.query || r.kind || r.detail || "—")}<br><small class="scope">${esc(r.scope || r.account_id || "")}</small></td><td><span class="tag ${["completed", "accepted", "confirmed", "delivered"].includes(r.state) ? "green" : ["failed", "unknown", "interrupted"].includes(r.state) ? "amber" : "neutral"}">${esc(stateNames[r.state] || r.state)}</span></td><td class="muted">${date(r.created || r.updated)}</td></tr>`).join("")}</tbody></table></div>`;
}
function resourcesPage() {
  let content = "";
  if (S.resourceTab === "tasks") {
    content = `<section class="card">${taskTable(tasks())}</section>`;
  } else if (S.resourceTab === "media") {
    const first = [
      "enabled",
      "image_enabled",
      "image_generation_enabled",
      "video_enabled",
      "video_generation_enabled",
      "file_enabled",
      "search_provider",
      "local_library_roots",
      "max_image_mb",
      "max_video_mb",
      "max_file_mb",
      "task_timeout_seconds",
      "cache_dir",
    ];
    content =
      formCard(
        "媒体能力与素材目录",
        "使用现有检索、生成、下载及交付接口",
        first.map((k) => "media." + k),
      ) +
      browserPanel() +
      `<details class="card settings-group mt"><summary>生成服务、浏览器与高级媒体配置</summary>${fields(
        Object.keys(S.config.media)
          .filter((k) => !first.includes(k))
          .map((k) => "media." + k),
      )}</details>`;
  } else if (S.resourceTab === "actors") {
    content = formCard(
      "角色与网站账号",
      "配置网站身份、阅读入口和发布控件；独立服务使用 agent 运行模式",
      Object.keys(S.config.agent).map((k) => "agent." + k),
    );
  } else {
    const rows = S.records.stickers?.items || [];
    content = `<section class="card"><div class="card-head"><div><h2>表情素材库</h2><small>用标签和语境管理素材</small></div>${button("导入素材", "import-sticker", "plus")}</div>${rows.length ? `<div class="table-wrap"><table><thead><tr><th>素材 ID</th><th>标签</th><th>语境</th><th></th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.id.slice(0, 14))}</td><td>${esc(parse(r.tags, []).join("、"))}</td><td>${esc(r.description)}</td><td>${button("移除", "remove-sticker", "trash", "small ghost", `data-id="${esc(r.id)}"`)}</td></tr>`).join("")}</tbody></table></div>` : empty("还没有素材", "导入图片或动图，为它添加标签和适用语境。", "folder")}</section><div class="mt">${formCard(
      "素材发送设置",
      "使用原有素材库，不复制账号资料",
      Object.keys(S.config.stickers).map((k) => "stickers." + k),
    )}</div>`;
  }
  return (
    head(
      "资源与任务",
      "从准备到交付，每一项工作都有迹可循。",
      "ASSETS & TASKS",
      button("刷新任务", "refresh-records", "refresh"),
    ) +
    `<div class="setting-tabs">${[
      ["tasks", "任务队列"],
      ["media", "媒体设置"],
      ["actors", "角色与网站"],
      ["stickers", "表情素材"],
    ]
      .map(
        ([key, title]) =>
          `<button data-resource-tab="${key}" class="${S.resourceTab === key ? "active" : ""}">${title}</button>`,
      )
      .join("")}</div>${content}`
  );
}
function connectionSettings() {
  const accounts = S.config.onebot.accounts || {},
    active = S.config.onebot.active_account;
  const path =
    "onebot.accounts." + encodeURIComponent(active).replace(/\./g, "%2E");
  return `<section class="card"><div class="card-head"><div><h2>运行通道</h2><small>选择要连接的平台</small></div></div><div class="form-grid">${field("bot.mode", { choices: ["official", "onebot", "dual", "agent"] })}</div>${field("onebot.enabled")}</section><div class="grid two mt"><section class="card"><div class="card-head"><div><h2>OneBot / NapCat 账号</h2><small>连接信息分别保存在每个账号下</small></div></div><div class="form-grid">${field("onebot.active_account", { choices: Object.keys(accounts), choiceNames: Object.fromEntries(Object.entries(accounts).map(([k, v]) => [k, v.name || k])), full: true })}</div><div class="separator"></div>${accounts[active] ? fields(["name", "account_id", "ws_url", "access_token", "enabled"].map((key) => path + "." + key)) : info("请在完整配置中添加账号，或选择已有账号。")}<div class="separator"></div>${fields(["onebot.napcat_path", "onebot.reconnect_interval"])}</section>${formCard("官方 QQ 机器人", "使用 QQ 开放平台凭据", ["qq.appid", "qq.secret", "qq.is_sandbox"])}</div>`;
}
function settingsPage() {
  const sections = {
    connection: ["连接与账号", []],
    behavior: [
      "对话与回复",
      [
        "bot.bot_name",
        "bot.master_id",
        "bot.master_title",
        "bot.admin_ids",
        "bot.max_reply_length",
        "bot.reply_char_limit",
        "bot.strip_mentions",
        "bot.enable_action_descriptions",
        "bot.search_enabled",
        "bot.image_filter_enabled",
      ],
    ],
    queue: [
      "消息排队",
      [
        "bot.backlog_interrupt_messages",
        "bot.chat_buffer_seconds",
        "bot.chat_settle_seconds",
        "bot.chat_max_wait_seconds",
        "bot.passive_backlog",
        "bot.qq_window_capture",
      ],
    ],
    memory: [
      "记忆与上下文",
      [
        "bot.memory_enabled",
        "bot.memory_archive_interval_minutes",
        "bot.memory_max_entries",
        "bot.memory_history_count",
        "bot.context_message_count",
        "bot.group_settings",
      ],
    ],
    model: [
      "模型服务",
      [
        "model_backend.backend",
        "deepseek_harness.home",
        "deepseek_harness.workspace",
        "deepseek_harness.provider",
        "deepseek_harness.model",
        "deepseek_harness.reasoning_effort",
        "deepseek_harness.max_tokens",
        "deepseek_harness.base_url",
        "deepseek_harness.api_key_env",
        "antigravity.default_model",
        "antigravity.timeout_seconds",
        "antigravity.session_timeout_minutes",
        "antigravity.ls_address",
        "antigravity.csrf_token",
        "antigravity.project_id",
        "antigravity.language_server_path",
        "bot.system_prompt",
      ],
    ],
    voice: ["语音输出", ["voice.enabled", "voice.provider", "voice.endpoint", "voice.default_language", "voice.character", "voice.auto_start"]],
    advanced: ["完整配置", []],
  };
  const [title, paths] = sections[S.settingTab];
  return (
    head(
      "设置",
      "常用选项清晰呈现，完整配置仍然保留。",
      "PREFERENCES",
      button("重新载入", "reload", "refresh"),
    ) +
    `<div class="setting-tabs">${Object.entries(sections)
      .map(
        ([key, [label]]) =>
          `<button data-settings-tab="${key}" class="${S.settingTab === key ? "active" : ""}">${label}</button>`,
      )
      .join(
        "",
      )}<button data-action="go-appearance">外观与个性化 ${icon("arrow")}</button></div>${
      S.settingTab === "connection"
        ? connectionSettings()
        : S.settingTab === "advanced"
          ? Object.keys(S.config)
              .map(
                (section, i) =>
                  `<details class="card settings-group" ${i === 0 ? "open" : ""}><summary>${esc({ bot: "机器人行为", onebot: "OneBot 账号", qq: "官方 QQ", antigravity: "模型服务", media: "媒体与交付", stickers: "表情素材", sandbox: "自主活动", emotion: "情感分析与训练", agent: "角色与网站" }[section] || section)}</summary>${fields(
                    Object.keys(S.config[section])
                      .filter(
                        (key) =>
                          section !== "onebot" ||
                          !["ws_url", "access_token"].includes(key),
                      )
                      .map((key) => section + "." + key),
                  )}</details>`,
              )
              .join("")
          : formCard(
              title,
              "保存时会校验参数，并保留当前未修改的配置字段",
              paths,
            )
    }${S.settingTab === "model" ? `<section class="card mt"><div class="row wrap">${button("检查 DeepSeek Harness", "check-dsh", "refresh", "", S.status.task?.state === "running" ? "disabled" : "")}${button("导出官方 MCP 配置", "export-dsh", "download", "ghost")}</div><p class="section-note mt">${esc(S.dshCheck || "使用官方 SDK 自带运行时，主代理通过 QQBot MCP 工作；会话原地恢复，后端切换分别保留历史。检查不会发送模型请求。")}</p></section>` : ""}${S.settingTab === "voice" ? `<div class="mt">${button("打开语音工作室", "go-voice", "mic")}</div>` : ""}${S.settingTab === "queue" ? `<div class="mt">${info("积压达到阈值后，中断当前自然回复，将原问题与补充消息交给 AI 统一判断。已发送记录会传给下一轮，后台工作继续运行。设为 0 可关闭。")}</div>` : ""}<div class="mt">${info("凭据仅保存在本地。涉及连接方式或服务路径的更改，保存后重启 Bot 生效；普通行为设置沿用核心已有的重载机制。")}</div>`
  );
}
let voiceAudioContext = null, voiceAudioTail = 0, voiceAudioTask = "", voiceAudioNodes = new Set();
function stopVoicePlayback() {
  for (const source of voiceAudioNodes) { try { source.stop(); } catch {} }
  voiceAudioNodes.clear();
  voiceAudioTail = 0;
  voiceAudioTask = "";
}
async function beginVoicePlayback() {
  stopVoicePlayback();
  if (!S.config.voice.streaming) return;
  const Audio = window.AudioContext || window.webkitAudioContext;
  if (!Audio) throw new Error("当前 WebView 不支持实时音频，请关闭流式试听后重试。");
  voiceAudioContext ||= new Audio();
  await voiceAudioContext.resume();
}
function playVoiceBlock(data) {
  if (!voiceAudioContext || data.task_id !== voiceAudioTask || S.page !== "voice") return;
  const raw = atob(data.pcm_base64);
  const channels = data.channels, frames = raw.length / 2 / channels;
  if (!Number.isInteger(frames) || frames <= 0) return;
  const buffer = voiceAudioContext.createBuffer(channels, frames, data.sample_rate);
  for (let channel = 0; channel < channels; channel++) {
    const samples = buffer.getChannelData(channel);
    for (let index = 0; index < frames; index++) {
      const offset = (index * channels + channel) * 2;
      let value = raw.charCodeAt(offset) | raw.charCodeAt(offset + 1) << 8;
      if (value >= 32768) value -= 65536;
      samples[index] = value / 32768;
    }
  }
  const source = voiceAudioContext.createBufferSource();
  source.buffer = buffer; source.connect(voiceAudioContext.destination);
  voiceAudioNodes.add(source);
  source.onended = () => voiceAudioNodes.delete(source);
  const start = Math.max(voiceAudioContext.currentTime + .04, voiceAudioTail);
  source.start(start); voiceAudioTail = start + buffer.duration;
}
function speechPage() {
  const character = S.voiceCatalog?.catalog?.characters?.[S.config.voice.character];
  const emotions = Object.entries(character?.emotions || {});
  if (!emotions.some(([tag]) => tag === S.voiceEmotion)) S.voiceEmotion = emotions[0]?.[0] || "";
  const busy = S.status.task?.state === "running";
  const preview = S.voicePreview;
  return head("语音工作室", "一句台词可以有多种心情，边生成边试听。", "VOICE STUDIO", button("重新载入样本", "reload-voice", "refresh")) +
    `<div class="grid two">${formCard("声音与语言", "主代理按语境选择文字或语音，语音必须带情绪标签", ["voice.enabled", "voice.default_language", "voice.character", "voice.speed"])}<section class="card"><div class="card-head"><div><h2>试听台词</h2><small>仅在本机试听，最长 30 秒</small></div><span class="tag neutral">本地预览</span></div><div class="field full"><label for="voice-text">朗读台词</label><textarea id="voice-text" style="min-height:115px" placeholder="[emotion:neutral]こんにちは。[emotion:sad]少し寂しいね。[emotion:]でも大丈夫。">${esc(S.voiceText)}</textarea></div><div class="field mt"><label for="voice-emotion">默认情绪／要插入的标签</label><select id="voice-emotion">${emotions.map(([tag, ref]) => `<option value="${esc(tag)}"${tag === S.voiceEmotion ? " selected" : ""}>${esc(tag + (ref.description ? " · " + ref.description : ""))}</option>`).join("")}</select></div><div class="row wrap mt">${button("插入情绪标记", "insert-voice-emotion", "plus", "ghost")}${button("拖长音", "insert-voice-prosody", "plus", "ghost small", `data-prosody="stretch"`)}${button("快读", "insert-voice-prosody", "plus", "ghost small", `data-prosody="fast"`)}${button("慢读", "insert-voice-prosody", "plus", "ghost small", `data-prosody="slow"`)}${button("合成试听", "preview-voice", "play", "primary", busy || !emotions.length ? "disabled" : "")}${button("检查接口", "check-voice", "refresh", "", busy ? "disabled" : "")}${button("停止任务", "cancel-voice", "stop", "ghost", busy ? "" : "disabled")}</div><p class="section-note mt">模型按语境决定是否快读、慢读或拖长短词，无需逐次设置。选中文字后点击标记按钮即可试听：[fast]快读[/fast]、[slow]慢读[/slow]、[stretch]语气音[/stretch]。情绪可用 [emotion:标签] 切换，[emotion:] 恢复默认。标签不朗读，不额外插入停顿；QQ 在完整 SILK 编码后发送。</p><p id="voice-progress" class="section-note mt">${esc(S.voiceCheck || "")}</p>${preview ? `<div class="voice-preview mt"><audio controls preload="metadata" src="data:audio/wav;base64,${esc(preview.wav_base64)}"></audio><p class="section-note">${esc(preview.character)} · ${esc(preview.emotion)} · ${Number(preview.duration).toFixed(2)} 秒 · QQ SILK 已生成</p><details><summary class="section-note">查看分词与朗读文本</summary><pre class="voice-transcript">${esc(preview.chunks.map(c => c.language + " / " + c.emotion + " / " + (c.prosody || "normal") + " ×" + Number(c.speed || 1).toFixed(2) + ": " + c.text).join("\n"))}</pre><p class="section-note">${esc(preview.manifest_path)}</p><p class="section-note">${preview.cache_hit ? "完整音频缓存命中" : `首段 ${Number(preview.metrics?.first_audio_ms || 0).toFixed(0)} ms · 总耗时 ${Number(preview.metrics?.total_ms || 0).toFixed(0)} ms`}</p></details></div>` : ""}</section></div>` +
    `<section class="card mt"><div class="card-head"><div><h2>角色与情绪参考</h2><small>${esc(character?.name || S.config.voice.character)} · 每个标签绑定一个真实切片</small></div><div class="row wrap">${button("编辑角色", "edit-voice-character", "edit", "small")}${button("添加角色", "add-voice-character", "plus", "small")}${button("添加情绪", "edit-voice-reference", "plus", "small", character ? "" : "disabled")}</div></div>${emotions.length ? `<div class="table-wrap"><table><thead><tr><th>情绪</th><th>参考文字</th><th>来源切片</th><th>操作</th></tr></thead><tbody>${emotions.map(([tag, ref]) => `<tr><td><strong>${esc(tag)}</strong><br><small>${esc(ref.description || "")}</small></td><td>${esc(ref.prompt_text || "由推理服务管理")}</td><td class="voice-path">${esc(ref.reference_audio || "服务端样本")}</td><td>${button("编辑", "edit-voice-reference", "edit", "small ghost", `data-emotion="${esc(tag)}"`)}${button("删除", "delete-voice-reference", "trash", "small ghost", `data-emotion="${esc(tag)}"`)}</td></tr>`).join("")}</tbody></table></div>` : empty("还没有情绪切片", "添加一个角色及其参考音频，或从角色接口同步标签。", "mic")}${character?.source ? `<p class="section-note mt">来源：${esc(character.source)}<br>${esc(character.attribution || "")} · ${esc(character.license || "")}</p>` : ""}${button("同步服务端角色情绪", "sync-voice-characters", "refresh", "ghost small", S.remoteVoiceCharacters ? "" : "disabled")}</section>` +
    `<details class="card settings-group mt"><summary>本地引擎、切句与读音词典</summary>${fields(["voice.provider", "voice.endpoint", "voice.auto_start", "voice.streaming", "voice.stream_chunk_ms", "voice.crossfade_ms", "voice.sample_steps", "voice.reuse_models", "voice.reuse_audio", "voice.denoise.enabled", "voice.denoise.reference", "voice.denoise.strength_db", "voice.denoise.noise_floor_db", "voice.voicestudio.local_bridge", "voice.voicestudio.source_directory", "voice.voicestudio.backend_endpoint", "voice.voicestudio.engine", "voice.voicestudio.fallback_enabled", "voice.voicestudio.api_key_env", "voice.catalog_path", "voice.dictionary", "voice.chunk_chars", "voice.pause_ms", "voice.max_duration_seconds", "voice.max_text_chars", "voice.timeout_seconds", "voice.ffmpeg_path", "voice.engine_python", "voice.engine_directory", "voice.engine_config"])}<div class="row wrap mt">${button("启动本地引擎", "start-voice-engine", "play", "", busy ? "disabled" : "")}${button("停止本地引擎", "stop-voice-engine", "stop", "ghost", busy ? "disabled" : "")}</div></details><div class="mt">${info("日语台词会完整转为假名，专名可在词典中修正。混语可显式标记语言，纯汉字的日文请使用 [ja] 标记。ATRI 样板仅供非商业本地测试，请保留模型来源与署名。保存设置后，下一轮代理会获得新的语音选项。")}</div>`;
}

function voicePage() {
  return `<section class="card"><div class="row wrap"><strong id="voice-live-status">${esc(voiceLiveLabel())}</strong>${button("应用语音设置到 Bot", "apply-voice-settings", "check", "primary small")}${button("训练专属声音", "go-voice-training", "chart", "small")}</div><p class="section-note mt">切换角色会立即保存并应用。其他语音设置编辑完成后点击应用；试听使用当前编辑值。</p></section><div class="mt">` + speechPage() + `</div><div class="mt">${button("打开唱歌工作室", "go-singing", "music")}</div>`;
}

const singingStages = {denoising: "清理人声底噪", queued: "等待推理资源", preparing: "检查歌曲和参考音频", separating: "分离人声与伴奏", generating: "根据歌词创作演唱", converting: "转换为角色声音", mixing: "混合歌声与伴奏", "encoding-voice": "转为 QQ 语音", ready: "演唱已完成"};
function singingPanel() {
  const r = S.singingRequest, busy = S.status.task?.state === "running";
  const character = S.voiceCatalog?.catalog?.characters?.[S.config.voice.character];
  const emotions = Object.keys(character?.emotions || {});
  if (!emotions.includes(r.emotion)) r.emotion = emotions[0] || "neutral";
  const options = (items, current) => items.map(([key, label]) => `<option value="${key}"${key === current ? " selected" : ""}>${label}</option>`).join("");
  return `<section class="card mt" id="singing-studio"><div class="card-head"><div><h2>唱歌工作室</h2><small>选择歌曲，填入歌词，用当前角色的声音演唱</small></div><span class="tag neutral">本机生成</span></div>
    <div class="grid two"><div>
    <div class="field"><label for="sing-mode">输入类型</label><select id="sing-mode" data-sing-field="mode">${options([["cover", "原唱 / 导唱＋歌词"], ["compose", "伴奏＋歌词，创作演唱"]], r.mode)}</select></div>
    <div class="field mt"><label for="sing-song">${r.mode === "cover" ? "原唱或导唱" : "纯伴奏"}</label><div class="row"><input id="sing-song" data-sing-field="song" value="${esc(r.song)}" placeholder="选择本机音频文件">${button("选择文件", "choose-sing-song", "folder", "small")}</div></div>
    ${r.mode === "cover" ? `<label class="row mt"><input type="checkbox" data-sing-field="vocals_only"${r.vocals_only ? " checked" : ""}>输入已是纯人声，跳过分离</label><div class="field mt"><label for="sing-backing">独立伴奏（可选，须与导唱对齐）</label><div class="row"><input id="sing-backing" data-sing-field="accompaniment" value="${esc(r.accompaniment)}" placeholder="留空时从原唱分离">${button("选择", "choose-sing-backing", "folder", "small")}</div></div>` : `<div class="field mt"><label for="sing-style">唱法与风格</label><input id="sing-style" data-sing-field="style" value="${esc(r.style)}"></div>`}
    <div class="form-grid mt"><div class="field"><label for="sing-language">歌词语言</label><select id="sing-language" data-sing-field="language">${options([["ja", "日语（生成前转假名）"], ["zh", "中文"], ["en", "英语"], ["unknown", "其他 / 混合"]], r.language)}</select></div><div class="field"><label for="sing-emotion">情绪参考</label><select id="sing-emotion" data-sing-field="emotion">${emotions.map(tag => `<option value="${esc(tag)}"${tag === r.emotion ? " selected" : ""}>${esc(tag)}</option>`).join("")}</select></div><div class="field"><label for="sing-pitch">移调（半音）</label><input id="sing-pitch" type="number" min="-12" max="12" data-sing-field="pitch_shift" value="${r.pitch_shift}"></div><div class="field"><label for="sing-output">输出形式</label><select id="sing-output" data-sing-field="song_output">${options([["file", "MP3 文件"], ["voice", "QQ 语音（SILK）"], ["both", "文件与语音"]], r.song_output)}</select></div></div>
    </div><div><div class="field"><div class="row between"><label for="sing-lyrics">完整歌词</label>${button("载入 TXT / LRC", "choose-sing-lyrics", "file", "small ghost")}</div><textarea id="sing-lyrics" data-sing-field="lyrics" style="min-height:260px" placeholder="按行填写歌词，可保留 [Verse]、[Chorus] 等段落标记">${esc(r.lyrics)}</textarea></div><p class="section-note mt">${r.mode === "cover" ? "翻唱保留原唱发音与旋律，歌词用于留档；换词请使用伴奏创唱。" : "根据伴奏和歌词生成新的演唱旋律，不保证原曲唱法。日语生成歌词转为假名，原歌词另行保存。"}</p></div></div>
    <div class="row wrap mt">${button("开始演唱", "render-singing", "play", "primary", busy || !emotions.length ? "disabled" : "")}${button("检查唱歌引擎", "check-singing", "refresh", "", busy ? "disabled" : "")}${button("停止任务", "cancel-singing", "stop", "ghost", busy ? "" : "disabled")}</div>
    <p id="singing-status" class="section-note mt" role="status">${esc(S.singingStatus || "本页只生成本地试听，不发送 QQ。主代理的唱歌任务会在完成后提交成果，等待交付。")}</p>
    ${S.singingResult ? `<div class="voice-preview mt"><audio controls preload="metadata" src="data:audio/mpeg;base64,${esc(S.singingResult.mp3_base64)}"></audio><p class="section-note">${esc(S.singingResult.character)} · ${Number(S.singingResult.duration).toFixed(1)} 秒${S.singingResult.silk_path ? " · QQ 语音已转码" : ""}</p><p class="section-note voice-path">${esc(S.singingResult.mp3_path)}</p>${button("打开成果目录", "open-singing-result", "folder", "small ghost")}</div>` : ""}
    </section><details class="card settings-group mt"><summary>唱歌权限、混音与推理设置</summary>${fields(["voice.singing.enabled", "voice.singing.denoise.enabled", "voice.singing.denoise.reference", "voice.singing.denoise.strength_db", "voice.singing.denoise.noise_floor_db", "voice.singing.reuse_separation", "voice.singing.max_duration_seconds", "voice.singing.max_input_mb", "voice.singing.max_voice_seconds", "voice.singing.vocal_gain_db", "voice.singing.accompaniment_gain_db", "voice.singing.diffusion_steps", "voice.singing.generation_steps", "voice.singing.suspend_owned_tts", "voice.singing.engine_python", "voice.singing.engine_directory", "voice.singing.generation_python", "voice.singing.generation_directory", "voice.singing.timeout_seconds"])}</details>`;
}

async function saveVoiceCatalog(catalog) {
  S.voiceCatalog = await rpc("voice", "save_catalog", { config: S.config.voice, catalog, revision: S.voiceCatalog?.revision || "" });
  render();
  toast("声音样本已保存。");
}

async function editVoiceCharacter(add = false) {
  if (!S.voiceCatalog) await loadRecords("voice");
  const original = add ? {} : S.voiceCatalog?.catalog?.characters?.[S.config.voice.character] || {};
  const values = await modal(add ? "添加声音角色" : "编辑声音角色", "模型路径和参考切片须能被推理服务读取。", {
    confirm: "保存角色",
    fields: `<label class="dialog-field">角色 ID<input name="id" required value="${esc(add ? "" : S.config.voice.character)}" ${add ? "" : "readonly"}></label><label class="dialog-field">显示名称<input name="name" value="${esc(original.name || "")}"></label><label class="dialog-field">GPT 权重 .ckpt<input name="gpt_weights" value="${esc(original.gpt_weights || "")}"></label><label class="dialog-field">SoVITS 权重 .pth<input name="sovits_weights" value="${esc(original.sovits_weights || "")}"></label><label class="dialog-field">来源链接<input name="source" value="${esc(original.source || "")}"></label><label class="dialog-field">许可与用途<input name="license" value="${esc(original.license || "")}"></label><label class="dialog-field">作者署名<input name="attribution" value="${esc(original.attribution || "")}"></label>`
  });
  if (!values) return;
  if (!/^[A-Za-z0-9_-]{1,80}$/.test(values.id) || ["__proto__", "constructor", "prototype"].includes(values.id)) throw new Error("角色 ID 请使用字母、数字、下划线或短横线。");
  const catalog = clone(S.voiceCatalog.catalog);
  if (add && catalog.characters[values.id]) throw new Error("角色 ID 已存在。");
  const id = values.id;
  delete values.id;
  catalog.characters[id] = { ...original, ...values, emotions: original.emotions || {} };
  await saveVoiceCatalog(catalog);
  put("voice.character", id);
  render();
}

async function editVoiceReference(el) {
  if (!S.voiceCatalog) await loadRecords("voice");
  const character = S.voiceCatalog.catalog.characters[S.config.voice.character];
  if (!character) throw new Error("请先添加当前角色。");
  const original = character.emotions[el.dataset.emotion] || {};
  const values = await modal("情绪参考切片", "参考文字应与音频逐字对应。建议选用干净的 3 至 10 秒单人切片。", {
    confirm: "保存切片",
    fields: `<label class="dialog-field">情绪标签<select name="tag" data-voice-choice="emotions">${voiceChoiceOptions("emotions",el.dataset.emotion || "neutral")}</select><small class="voice-choice-description">${esc(voiceChoiceDescription("emotions",el.dataset.emotion || "neutral"))}</small></label><div class="form-grid"><label class="dialog-field">说话方式<select name="delivery" data-voice-choice="deliveries">${voiceChoiceOptions("deliveries",original.delivery || "normal")}</select><small class="voice-choice-description">${esc(voiceChoiceDescription("deliveries",original.delivery || "normal"))}</small></label><label class="dialog-field">情绪强度<select name="intensity" data-voice-choice="intensities">${voiceChoiceOptions("intensities",original.intensity || "medium")}</select><small class="voice-choice-description">${esc(voiceChoiceDescription("intensities",original.intensity || "medium"))}</small></label></div><label class="dialog-field">情绪说明<input name="description" value="${esc(original.description || "")}"></label><label class="dialog-field">参考 WAV 路径<div class="row"><input name="reference_audio" id="voice-reference-file" value="${esc(original.reference_audio || "")}">${button("选择", "choose-voice-reference", "folder", "small")}</div></label><label class="dialog-field">参考文字<textarea name="prompt_text">${esc(original.prompt_text || "")}</textarea></label><label class="dialog-field">参考语言<select name="prompt_language">${["ja", "zh", "en"].map(x => `<option value="${x}"${(original.prompt_language || "ja") === x ? " selected" : ""}>${enumNames[x]}</option>`).join("")}</select></label>`
  });
  if (!values) return;
  if (!values.tag.trim() || ["__proto__", "constructor", "prototype"].includes(values.tag)) throw new Error("请填写有效情绪标签。");
  const catalog = clone(S.voiceCatalog.catalog);
  const refs = catalog.characters[S.config.voice.character].emotions;
  if (el.dataset.emotion && el.dataset.emotion !== values.tag) delete refs[el.dataset.emotion];
  const tag = values.tag;
  delete values.tag;
  refs[tag] = {...original, ...values};
  await saveVoiceCatalog(catalog);
}

function appearancePage() {
  const p = S.preferences;
  const ranges = [
    [
      "surface_opacity",
      "玻璃浓度",
      35,
      100,
      "%",
      "降低浓度，让背景更通透；正文保持清晰。",
    ],
    ["blur", "模糊强度", 0, 48, "px", "调整界面层之间的背景模糊。"],
    [
      "window_opacity",
      "窗口不透明度",
      65,
      100,
      "%",
      "调整整个窗口，包括文字；100% 最清晰。",
    ],
    [
      "font_size",
      "界面字号",
      12,
      17,
      "px",
      "调整内容字号，适合不同显示器和阅读距离。",
    ],
  ];
  return (
    head(
      "外观与个性化",
      "轻一点，清晰一点，调成你喜欢的样子。",
      "MAKE IT YOURS",
      button("恢复默认", "reset-appearance", "refresh"),
    ) +
    `<div class="appearance-layout"><section class="card"><h2>选择主题</h2><p class="section-note">在柔和的浅色与安静的深色之间切换。</p><div class="theme-options">${[
      ["light", "雾白"],
      ["dark", "墨色"],
      ["system", "跟随系统"],
    ]
      .map(
        ([key, label]) =>
          `<button class="theme-option ${p.theme === key ? "active" : ""}" data-theme-choice="${key}" aria-pressed="${p.theme === key}"><div class="theme-swatch ${key}"></div>${label}</button>`,
      )
      .join(
        "",
      )}</div><h3>强调色</h3><div class="accent-options">${Object.entries(
      accentColors,
    )
      .map(
        ([key, color]) =>
          `<button class="accent-option ${p.accent === key ? "active" : ""}" style="--swatch:${color}" data-accent="${key}" title="${{ blue: "钛蓝", green: "松绿", violet: "雾紫", slate: "石板" }[key]}" aria-label="${key}" aria-pressed="${p.accent === key}"></button>`,
      )
      .join(
        "",
      )}</div><div class="separator"></div>${ranges.map(([key, label, min, max, unit, hint]) => `<div class="range-setting"><div class="range-label"><label for="range-${key}">${label}</label><output id="output-${key}">${p[key]}${unit}</output></div><input id="range-${key}" type="range" min="${min}" max="${max}" value="${p[key]}" data-preference="${key}" data-unit="${unit}"><p>${hint}</p></div>`).join("")}<div class="separator"></div>${[
      [
        "native_glass",
        "Windows 原生毛玻璃",
        "使用系统 Acrylic 材质；不可用时保留界面玻璃效果。",
      ],
      ["reduced_motion", "减少动态效果", "关闭界面过渡与动画，让操作更安静。"],
    ]
      .map(
        ([key, label, hint]) =>
          `<div class="toggle-row"><div><strong>${label}</strong><p>${hint}</p></div><label class="switch"><input type="checkbox" data-preference="${key}" aria-label="${label}"${checked(p[key])}><span></span></label></div>`,
      )
      .join(
        "",
      )}</section><div class="preview-column"><section class="card"><div class="card-head"><div><h2>实时预览</h2><small>每一次调整，都会立刻呈现</small></div>${icon("sun")}</div><div class="appearance-preview"><div class="preview-orb"></div><div class="preview-window"><div class="preview-sidebar"><div class="preview-line active"></div><div class="preview-line"></div><div class="preview-line"></div><div class="preview-line"></div></div><div class="preview-main"><span class="tag" style="font-size:0.57143rem">你的工作空间</span><h3>让陪伴自然发生。</h3><p>清晰的层次，恰到好处的通透。</p><div class="preview-panels"><div></div><div></div></div></div></div></div><div class="stat-line"><span>渲染方式</span><span>WebView2</span></div><div class="stat-line"><span>系统材质</span><span id="native-status">${nativeLabel()}</span></div><div class="stat-line"><span>偏好保存</span><span>自动保存到本机</span></div></section><div class="mt">${info("玻璃浓度只改变界面底色，窗口不透明度会影响整个窗口。建议优先调整玻璃浓度，让文字保持清晰。")}</div></div></div>`
  );
}
function nativeLabel() {
  return S.status.native?.effect === "acrylic"
    ? "Windows Acrylic"
    : S.status.native?.effect === "off"
      ? "已关闭"
      : "界面玻璃";
}
function logsPage() {
  return (
    head(
      "运行日志",
      "查看核心输出，定位问题与追踪状态。",
      "CONSOLE",
      button("导出日志", "export-logs", "download"),
    ) +
    `<section class="card"><div class="messages-tools"><div class="search-input">${icon("search")}<input id="log-query" placeholder="搜索日志内容" aria-label="搜索日志"></div><select id="log-level" aria-label="日志级别"><option value="all">全部级别</option><option value="error">错误</option><option value="warning">警告</option></select><label class="row muted" style="font-size:0.78571rem;margin-left:auto"><input id="log-autoscroll" type="checkbox" checked>自动滚动</label>${button("清空视图", "clear-logs", "trash", "small ghost")}</div><div id="log-terminal" class="log-terminal">${logEntries()}</div><div class="row mt">${button("重启核心", "restart", "refresh")}${button("请求停止", "stop", "stop")}${button("强制结束", "force-stop", "stop", "danger")}<span class="actions-note">正常停止会等待记忆保存完成。</span></div></section>`
  );
}
function logEntries() {
  const q = ($("#log-query")?.value || "").toLowerCase(),
    level = $("#log-level")?.value || "all";
  return (
    S.events
      .filter(
        (e) =>
          e.kind === "log" &&
          (!q || e.data.text.toLowerCase().includes(q)) &&
          (level === "all" || e.data.level === level),
      )
      .map(
        (e) =>
          `<div class="log-entry ${e.data.level}"><time>${time(e.time)}</time>${esc(e.data.text)}</div>`,
      )
      .join("") ||
    '<span style="color:#6a8098">等待核心日志。启动 Bot 后将在此显示实时输出。</span>'
  );
}
function scrollLogs() {
  const box = $("#log-terminal");
  if (box && $("#log-autoscroll")?.checked) box.scrollTop = box.scrollHeight;
}

function emotionPage() {
  const busy = S.status.task?.state === "running";
  let content = "";
  if (S.emotionTab === "analysis") {
    content = `<div class="grid two"><section class="card"><div class="card-head"><div><h2>分析一段文字</h2><small>按原文逐句分析表达的情绪</small></div><span class="tag neutral">本地模型</span></div>${fields(["emotion.provider", "emotion.lms_host", "emotion.llm_model", "emotion.bert_service_file", "emotion.classifier_path"])}<div class="field full mt"><label for="analysis-text">待分析文本</label><textarea id="analysis-text" placeholder="输入或粘贴需要分析的文本…" style="min-height:155px">${esc(S.analysisText || "")}</textarea></div><div class="row mt wrap">${button("开始分析", "analyze-emotion", "play", "primary", busy ? "disabled" : "")}${button("读取已有模型", "list-models", "refresh", "", busy ? "disabled" : "")}${button("检查分类器", "inspect-model", "file", "ghost", busy ? "disabled" : "")}${button("打开 BERT 工作台", "open-bert", "folder", "ghost")}</div><div id="model-list-note" class="section-note">${S.models.length ? `已读取 ${S.models.length} 个模型：${esc(S.models.map((r) => r.name || r.key).join("、"))}` : "连接 LM Studio 后可读取已下载的语言模型和嵌入模型。"}</div></section><section class="card"><div class="card-head"><div><h2>分析结果</h2><small>结果来自实际模型输出</small></div>${button("导出", "export-analysis", "download", "small ghost")}</div><div id="analysis-results">${analysisResults()}</div></section></div>`;
  } else if (S.emotionTab === "dataset") {
    content = `<section class="card"><div class="card-head"><div><h2>样本与标注</h2><small>共 ${S.dataset.length} 条样本 · ${S.dataset.filter((r) => r.label).length} 条已标注${S.datasetDirty ? " · 未保存" : ""}</small></div><div class="row">${button("导入文件", "import-dataset", "folder")}${button("保存数据", "save-dataset", "check", "primary")}</div></div><div class="form-grid"><div class="field full"><label>情感标签集（JSON 数组）</label>${field("emotion.labels").match(/<textarea[\s\S]*?<\/textarea>/)?.[0] || ""}</div></div><div class="row mt"><input id="sample-text" class="grow" placeholder="添加一句待标注文本" aria-label="新样本文本"><select id="sample-label" aria-label="新样本标签"><option value="">未标注</option>${S.config.emotion.labels.map((l) => `<option>${esc(l)}</option>`).join("")}</select>${button("添加", "add-sample", "plus")}</div><div class="table-wrap mt" style="max-height:450px"><table><thead><tr><th style="width:55%">文本</th><th>情感标签</th><th></th></tr></thead><tbody>${S.dataset.length ? S.dataset.map((r, i) => `<tr><td><input data-sample-text="${i}" value="${esc(r.text)}" aria-label="样本 ${i + 1}"></td><td><select data-sample-label="${i}" aria-label="样本 ${i + 1} 标签"><option value="">未标注</option>${S.config.emotion.labels.map((l) => `<option${r.label === l ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></td><td>${button("删除", "remove-sample", "trash", "small ghost", `data-index="${i}"`)}</td></tr>`).join("") : '<tr><td colspan="3" class="table-empty">导入 CSV、JSONL、JSON 或 TXT 文件，开始标注。</td></tr>'}</tbody></table></div></section>`;
  } else {
    content = `<div class="grid two">${formCard("训练设置", "使用已标注样本训练分类头；嵌入模型保持冻结", ["emotion.training_features", "emotion.embedding_model", "emotion.feature_dim", "emotion.epochs", "emotion.batch_size", "emotion.learning_rate", "emotion.l2", "emotion.validation_fraction", "emotion.seed", "emotion.classifier_path"])}<section class="card"><div class="card-head"><div><h2>训练进度</h2><small id="training-status">${esc(S.status.task?.kind === "training" ? stateNames[S.status.task.state] || S.status.task.state : "等待开始训练")}</small></div><span class="tag neutral">${S.dataset.filter((r) => r.label).length} 条标注</span></div><div id="training-chart">${trainingChart()}</div><div class="row mt wrap">${button("开始训练", "train", "play", "primary", busy ? "disabled" : "")}${button("继续训练", "warm-train", "refresh", "", busy ? "disabled" : "")}${button("停止任务", "cancel-model", "stop", "", busy ? "" : "disabled")}</div><p class="section-note">损失与准确率来自实际训练。停止后保留已完成轮次的最佳模型。</p><div id="training-result">${S.trainingResult ? `<div class="separator"></div><div class="stat-line"><span>最佳轮次</span><span>${S.trainingResult.best_epoch || "—"}</span></div><div class="stat-line"><span>模型路径</span><span class="scope" title="${esc(S.trainingResult.model_path)}">${esc(S.trainingResult.model_path || "尚未保存")}</span></div>` : ""}</div></section></div>`;
  }
  return (
    head(
      "情感工作室",
      "从理解一句话，到训练适合自己的模型。",
      "EMOTION LAB",
      busy
        ? button("停止当前任务", "cancel-model", "stop")
        : button("模型与标签设置", "emotion-settings", "settings"),
    ) +
    `<div class="setting-tabs">${[
      ["analysis", "文本分析"],
      ["dataset", "数据与标注"],
      ["training", "本地训练"],
    ]
      .map(
        ([key, title]) =>
          `<button data-emotion-tab="${key}" class="${S.emotionTab === key ? "active" : ""}">${title}</button>`,
      )
      .join("")}</div><datalist id="lms-models">${S.models
      .filter((r) => r.type === "llm")
      .map((r) => `<option value="${esc(r.key)}">${esc(r.name)}</option>`)
      .join("")}</datalist><datalist id="lms-embeddings">${S.models
      .filter((r) => r.type === "embedding")
      .map((r) => `<option value="${esc(r.key)}">${esc(r.name)}</option>`)
      .join("")}</datalist>${content}`
  );
}
function analysisResults() {
  return S.predictions.length
    ? S.predictions
        .map(
          (r, i) =>
            `<article class="record"><div class="row between"><span class="tag">${esc(r.label)}</span><small>${r.confidence == null ? "" : (r.confidence * 100).toFixed(1) + "%"}</small></div><p>${i + 1}. ${esc(r.text)}</p>${r.explanation ? `<p style="font-size:0.71429rem">${esc(r.explanation)}</p>` : ""}</article>`,
        )
        .join("")
    : empty(
        "等待一次真实的分析",
        "选择已配置的本地模型，结果会逐句显示在这里。",
        "heart",
      );
}
function chart(records, key, validation) {
  const data = records.filter((r) => Number.isFinite(r[key]));
  if (data.length < 1) return "";
  const width = 400,
    height = 145,
    left = 32,
    top = 12,
    bottom = 118;
  const values = data
      .flatMap((r) => [r[key], r[validation]])
      .filter(Number.isFinite),
    max = Math.max(...values, 0.01),
    min = key.includes("accuracy") ? 0 : Math.min(0, ...values);
  const point = (r, i, k) =>
    `${left + (i / Math.max(1, data.length - 1)) * (width - left - 12)},${bottom - ((r[k] - min) / (max - min)) * (bottom - top)}`;
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${key.includes("loss") ? "训练损失" : "训练准确率"}曲线"><path class="axis" d="M${left} ${top}V${bottom}H390"/><text x="0" y="${top + 4}">${max.toFixed(2)}</text><text x="7" y="${bottom}">0</text><text x="${left}" y="138">第 1 轮</text><text x="338" y="138">第 ${data.at(-1).epoch} 轮</text><polyline class="train" points="${data.map((r, i) => point(r, i, key)).join(" ")}"/>${
    data.some((r) => r[validation] != null)
      ? `<polyline class="validation" points="${data
          .map((r, i) => (r[validation] != null ? point(r, i, validation) : ""))
          .filter(Boolean)
          .join(" ")}"/>`
      : ""
  }</svg>`;
}
function trainingChart() {
  if (!S.history.length)
    return empty(
      "还没有训练曲线",
      "载入标注数据并开始训练，查看真实损失与准确率变化。",
      "chart",
    );
  const last = S.history.at(-1);
  return `<div class="row between"><h3>损失</h3><span class="tag">${last.train_loss.toFixed(4)}</span></div>${chart(S.history, "train_loss", "val_loss")}<div class="row between mt"><h3>准确率</h3><span class="tag">${((last.val_accuracy ?? last.train_accuracy) * 100).toFixed(1)}%</span></div>${chart(S.history, "train_accuracy", "val_accuracy")}<div class="chart-legend"><span>训练集</span><span>验证集</span></div>${last.confusion_matrix ? `<details class="mt"><summary class="section-note">查看混淆矩阵</summary><div class="table-wrap"><table><thead><tr><th>实际 / 预测</th>${S.config.emotion.labels.map((l) => `<th>${esc(l)}</th>`).join("")}</tr></thead><tbody>${last.confusion_matrix.map((row, i) => `<tr><th>${esc(S.config.emotion.labels[i])}</th>${row.map((v) => `<td>${v}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>` : ""}`;
}

async function modal(
  title,
  text,
  { confirm = "确定", danger = false, fields = "" } = {},
) {
  const dialog = $("#dialog");
  $("#dialog-body").innerHTML =
    `<h2>${esc(title)}</h2><p>${esc(text)}</p>${fields}`;
  $("#dialog-confirm").textContent = confirm;
  $("#dialog-confirm").className = "button " + (danger ? "danger" : "primary");
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener(
      "close",
      () => {
        const values = {};
        $$("[name]", dialog).forEach(
          (el) =>
            (values[el.name] = el.type === "checkbox" ? el.checked : el.value),
        );
        resolve(dialog.returnValue === "confirm" ? values : null);
      },
      { once: true },
    );
  });
}
async function saveConfig() {
  if (S.invalid.size)
    throw new Error("请先修正标红的配置字段。JSON 字段需要有效的对象或数组。");
  const changes = {};
  for (const section of Object.keys(S.config)) {
    const edited = Object.fromEntries(
      Object.keys(S.config[section])
        .filter(
          (k) =>
            JSON.stringify(S.config[section][k]) !==
            JSON.stringify(S.savedConfig[section][k]),
        )
        .map((k) => [k, S.config[section][k]]),
    );
    if (Object.keys(edited).length) changes[section] = edited;
  }
  if (!Object.keys(changes).length) return;
  const result = await rpc("save_config", changes, S.revision);
  S.config = clone(result.config);
  S.savedConfig = clone(result.config);
  S.revision = result.revision;
  markDirty();
  if (S.page === "settings") render();
  toast("更改已保存到本机。");
}
async function reload() {
  if (
    hasEdits() &&
    !(await modal(
      "重新载入工作区",
      "未保存的配置、角色编辑和标注更改将被替换。",
      { confirm: "重新载入" },
    ))
  )
    return;
  const data = await rpc("bootstrap");
  Object.assign(S, {
    config: clone(data.config),
    savedConfig: clone(data.config),
    schema: data.schema,
    revision: data.revision,
    personas: data.personas,
    preferences: data.preferences,
    status: data.status,
    personaDraft: null,
    conversationDraft: null,
    conversationDirty: false,
    personaDirty: false,
    datasetDirty: false,
    glossary: null,
    glossaryDirty: false,
  });
  S.invalid.clear();
  applyAppearance(S.preferences);
  try {
    S.dataset = await rpc("emotion", "load_dataset", {
      config: S.config.emotion,
    });
  } catch {
    S.dataset = [];
  }
  render();
  if (["overview", "memory", "autonomy", "resources", "conversations"].includes(S.page))
    await loadRecords(S.page);
  updateStatus();
  toast("已重新载入配置与角色。");
}
async function saveAll() {
  await voiceApplyChain.catch(() => {});
  await saveConfig();
  if (S.conversationDirty) await conversationSave();
  if (S.personaDirty) await actions["save-persona"]();
  if (S.datasetDirty) await actions["save-dataset"]();
  if (S.glossaryDirty) await actions["save-glossary"]();
  if (Object.keys(S.voiceAnnotationEdits || {}).length) await trainingSaveAnnotations();
  markDirty();
}
async function startModel(operation, params) {
  const result = await rpc("emotion", operation, params);
  S.status.task = { ...result, kind: operation };
  render();
}
async function recoverActivity(el, operation) {
  const values = await modal(
    operation === "skip" ? "跳过当前内容" : "重启执行分支",
    "保留已有窗口、登录态和任务上下文。",
    {
      confirm: "继续",
      fields:
        '<label class="dialog-field">替代入口（可选）<input name="url" placeholder="https://…"></label>',
    },
  );
  if (!values) return;
  const result = await rpc(
    "recover_activity",
    el.dataset.session,
    operation,
    values.url,
  );
  toast(result.message || result.hint || "操作已提交，请刷新查看回执。");
  await loadRecords("autonomy");
}
async function saveAppearance(values, persist = true) {
  Object.assign(S.preferences, values);
  applyAppearance(S.preferences);
  const result = await rpc("preferences", S.preferences, persist);
  S.status.native = result.native;
  if ($("#native-status")) $("#native-status").textContent = nativeLabel();
}
let appearanceTimer;
async function requestClose() {
  if ($("#dialog").open) return;
  const active =
    S.status.state !== "stopped" || S.status.task?.state === "running";
  const unsaved = hasEdits();
  if (active || unsaved) {
    const answer = await modal(
      "关闭工作空间",
      `${active ? "会先请求核心保存记忆并停止，同时取消正在运行的模型任务。\n" : ""}${unsaved ? "还有未保存的编辑，关闭后将丢失这些更改。" : ""}`,
      { confirm: active ? "停止并关闭" : "关闭窗口" },
    );
    if (!answer) return;
  }
  S.status = await rpc("window_action", "close");
  updateStatus();
}
const actions = {
  ...trainingActions,
  ...broadcastActions,
  "go-singing": () => navigate("singing"),
  "choose-sing-song": async () => { const path = await rpc("choose_file", "open", ["Audio (*.wav;*.mp3;*.flac;*.m4a;*.ogg)", "All files (*.*)"]); if (path) { S.singingRequest.song = path; render(); } },
  "choose-sing-backing": async () => { const path = await rpc("choose_file", "open", ["Audio (*.wav;*.mp3;*.flac;*.m4a;*.ogg)", "All files (*.*)"]); if (path) { S.singingRequest.accompaniment = path; render(); } },
  "choose-sing-lyrics": async () => { const path = await rpc("choose_file", "open", ["Lyrics (*.txt;*.lrc)", "All files (*.*)"]); if (path) { S.singingRequest.lyrics = (await rpc("singing", "load_lyrics", {path, config: S.config.voice})).text; render(); } },
  "render-singing": async () => {
    if (!S.singingRequest.song || !S.singingRequest.lyrics.trim()) throw new Error("请选择歌曲并填写歌词。");
    const request = {...S.singingRequest};
    if (request.mode === "compose") { request.accompaniment = ""; request.vocals_only = false; }
    S.singingResult = null;
    const result = await rpc("singing", "render", {config: S.config.voice, request});
    S.status.task = {...result, kind: "singing-render"}; S.singingStatus = "正在检查歌曲并准备演唱…"; render();
  },
  "check-singing": async () => { const result = await rpc("singing", "check", {config: S.config.voice}); S.status.task = {...result, kind: "singing-check"}; S.singingStatus = "正在检查两个推理环境…"; render(); },
  "cancel-singing": async () => { await rpc("singing", "cancel"); S.singingStatus = "正在停止当前演唱并释放推理资源…"; render(); },
  "open-singing-result": async () => { if (S.singingResult) await rpc("singing", "open_result", {directory: S.singingResult.directory, config: S.config.voice}); },
  "go-voice": () => navigate("voice"),
  "reload-voice": () => loadRecords("voice"),
  "add-voice-character": () => editVoiceCharacter(true),
  "edit-voice-character": () => editVoiceCharacter(false),
  "edit-voice-reference": editVoiceReference,
  "choose-voice-reference": async () => {
    const path = await rpc("choose_file", "open", ["WAV audio (*.wav)", "All files (*.*)"]);
    if (path && $("#voice-reference-file")) $("#voice-reference-file").value = path;
  },
  "delete-voice-reference": async el => {
    if (!(await modal("删除情绪切片绑定", "删除标签 " + el.dataset.emotion + " 的绑定，原始音频保留。", { confirm: "删除绑定", danger: true }))) return;
    const catalog = clone(S.voiceCatalog.catalog);
    delete catalog.characters[S.config.voice.character].emotions[el.dataset.emotion];
    await saveVoiceCatalog(catalog);
  },
  "sync-voice-characters": async () => {
    const catalog = clone(S.voiceCatalog?.catalog || {version: 1, characters: {}});
    for (const [name, tags] of Object.entries(S.remoteVoiceCharacters || {})) {
      if (["__proto__", "constructor", "prototype"].includes(name) || !Array.isArray(tags)) continue;
      const character = catalog.characters[name] || {name, emotions: {}};
      for (const tag of tags) {
        if (typeof tag !== "string" || ["__proto__", "constructor", "prototype"].includes(tag)) continue;
        character.emotions[tag] ||= {description: "推理服务管理的参考切片"};
      }
      catalog.characters[name] = character;
    }
    await saveVoiceCatalog(catalog);
  },
  "preview-voice": async () => {
    await beginVoicePlayback();
    S.voiceText = $("#voice-text").value;
    S.voiceEmotion = $("#voice-emotion").value;
    const result = await rpc("voice", "preview", {config: S.config.voice, text: S.voiceText, emotion: S.voiceEmotion});
    S.status.task = {...result, kind: "voice-preview"};
    voiceAudioTask = result.id;
    render();
  },
  "check-voice": async () => {
    const result = await rpc("voice", "check", {config: S.config.voice});
    S.status.task = {...result, kind: "voice-check"};
    render();
  },
  "cancel-voice": async () => { stopVoicePlayback(); await rpc("voice", "cancel"); toast("正在停止语音任务。"); },
  "insert-voice-emotion": () => {
    const text = $("#voice-text"), tag = $("#voice-emotion").value;
    const mark = `[emotion:${tag}]`;
    text.setRangeText(mark, text.selectionStart, text.selectionEnd, "end");
    S.voiceText = text.value; text.focus();
  },
  "insert-voice-prosody": (el) => {
    const text = $("#voice-text"), tag = el.dataset.prosody;
    if (!["fast", "slow", "stretch"].includes(tag)) return;
    const start = text.selectionStart, selected = text.value.slice(start, text.selectionEnd);
    const sample = S.config.voice.default_language === "ja" ? (tag === "stretch" ? "ねえ" : "いっしょにいこう。") : S.config.voice.default_language === "en" ? (tag === "stretch" ? "Oh" : "Let's go together.") : (tag === "stretch" ? "呀" : "我们一起出发吧。");
    const body = selected || sample, prefix = `[${tag}]`;
    text.setRangeText(`${prefix}${body}[/${tag}]`, start, text.selectionEnd, "end");
    text.setSelectionRange(start + prefix.length, start + prefix.length + body.length);
    S.voiceText = text.value; text.focus();
  },
  "start-voice-engine": async () => {
    const result = await rpc("voice", "engine_start", {config: S.config.voice});
    S.status.task = {...result, kind: "voice-engine"};
    render();
  },
  "stop-voice-engine": async () => {
    const result = await rpc("voice", "engine_stop");
    S.status.task = {...result, kind: "voice-engine"};
    render();
  },
  "check-dsh": async () => {
    const result = await rpc("dsh", "check", {config: S.config.deepseek_harness});
    S.status.task = {...result, kind: "dsh-check"};
    S.dshCheck = "正在检查官方 SDK 与模型路由…";
    render();
  },
  "export-dsh": async () => {
    const result = await rpc("dsh", "export_mcp", {config: S.config.deepseek_harness});
    S.dshCheck = "MCP 配置已导出：" + result.path;
    render();
    toast("官方 MCP 配置已导出。");
  },
  "save-config": saveAll,
  reload,
  "save-glossary": async () => {
    S.glossary = await rpc(
      "glossary",
      "save",
      S.glossary?.text || "",
      S.glossary?.revision || "",
    );
    S.glossaryDirty = false;
    render();
    toast("话术表已保存。");
  },
  "recover-skip": (el) => recoverActivity(el, "skip"),
  "recover-restart": (el) => recoverActivity(el, "restart"),
  "connect-browser": async () => {
    await rpc("browser_tools", "connect", S.config.media.gemini_web);
    S.browserStatus = S.config.media.gemini_web.transport === "browser"
      ? "已打开浏览器，请手动登录 Gemini，关闭该窗口以释放登录资料目录，然后读取账号模型。"
      : "已打开本机连接页，完成连接后可读取账号模型。";
    render();
  },
  "inspect-browser": async () => {
    const transport = S.config.media.gemini_web.transport;
    const task = await rpc("browser_tools", "inspect", S.config.media.gemini_web);
    S.browserTask = { id: task.id, transport };
    if (S.config.media.gemini_web.transport !== transport) return;
    S.browserStatus = "正在读取账号可用模型。";
    if ($("#browser-status")) $("#browser-status").textContent = S.browserStatus;
    toast(S.browserStatus);
  },
  "open-extension": () => rpc("browser_tools", "extension"),
  "open-bert": () => rpc("browser_tools", "bert_studio"),
  start: async () => {
    if (hasEdits()) await saveAll();
    S.status = await rpc("control", "start");
    updateStatus();
    if (S.page === "overview") render();
  },
  stop: async () => {
    S.status = await rpc("control", "stop");
    updateStatus();
  },
  restart: async () => {
    if (hasEdits()) await saveAll();
    S.status = await rpc("control", "restart");
    updateStatus();
  },
  "force-stop": async () => {
    if (
      await modal(
        "强制结束核心",
        "这会立即结束当前核心，未完成的记忆保存可能中断。仅在正常停止长时间无响应时使用。",
        { confirm: "强制结束", danger: true },
      )
    ) {
      S.status = await rpc("control", "force_stop");
      updateStatus();
    }
  },
  "refresh-records": () => loadRecords(S.page),
  "go-messages": () => navigate("messages"),
  "go-personas": () => navigate("personas"),
  "conversation-new": conversationNew,
  "conversation-select": conversationSelect,
  "conversation-save": () => conversationSave(),
  "conversation-remove": () => conversationSave(true),
  "conversation-refresh": () => loadRecords("conversations"),
  "go-appearance": () => navigate("appearance"),
  "new-persona": async () => {
    if (
      S.personaDirty &&
      !(await modal("放弃当前角色编辑", "当前角色有尚未保存的修改。", {
        confirm: "新建角色",
      }))
    )
      return;
    S.personaDraft = {
      name: "",
      description: "",
      prompt: "",
      tags: [],
      aliases: [],
      greeting: "",
      avatar: "",
    };
    S.personaOriginal = "";
    S.personaDirty = false;
    render();
  },
  "save-persona": async () => {
    if (dirty()) await saveConfig();
    S.personas = await rpc(
      "save_persona",
      S.personaOriginal,
      S.personaDraft,
      S.personas.revision,
    );
    S.personaOriginal = S.personaDraft.name;
    S.personaDirty = false;
    const fresh = await rpc("bootstrap");
    S.config = clone(fresh.config);
    S.savedConfig = clone(fresh.config);
    S.revision = fresh.revision;
    toast("角色已保存，原有身份与记忆关联保留。");
    render();
  },
  "activate-persona": async () => {
    if (S.personaDirty) throw new Error("请先保存角色编辑，再切换当前角色。");
    S.personas = await rpc(
      "persona_action",
      "activate",
      S.personaOriginal,
      S.personas.revision,
    );
    toast("当前角色已切换。");
    render();
  },
  "delete-persona": async () => {
    if (
      !(await modal(
        "删除角色预设",
        `将删除「${S.personaOriginal}」的预设。已有记忆文件保留。`,
        { confirm: "删除预设", danger: true },
      ))
    )
      return;
    S.personas = await rpc(
      "persona_action",
      "delete",
      S.personaOriginal,
      S.personas.revision,
    );
    S.personaDraft = null;
    S.personaDirty = false;
    render();
  },
  "clear-feed": async () => {
    S.events = S.events.filter((e) => !["message", "reply"].includes(e.kind));
    render();
  },
  "clear-logs": async () => {
    S.events = S.events.filter((e) => e.kind !== "log");
    $("#log-terminal").innerHTML = logEntries();
  },
  "copy-message": async (el) => {
    const row = S.events.find((e) => e.seq === Number(el.dataset.seq));
    if (row)
      await navigator.clipboard.writeText(
        row.data.content || row.data.reply || "",
      );
    toast("消息已复制。");
  },
  master: async (el) => {
    const row = S.events.find((e) => e.seq === Number(el.dataset.seq))?.data;
    if (!row) return;
    if (dirty()) throw new Error("请先保存配置更改，再设置主人。");
    const value = await modal(
      "设置主人",
      `${row.sender || row.user_id} · ${row.user_id}`,
      {
        confirm: "保存设置",
        fields:
          '<label class="dialog-field">专属称谓<input name="title" value="主人" required></label><label class="row section-note"><input name="global" type="checkbox">设为全局主人（未勾选时仅用于当前群）</label>',
      },
    );
    if (!value) return;
    const result = await rpc(
      "set_master",
      String(row.user_id),
      row.sender || "",
      value.title,
      String(row.group_id || ""),
      value.global,
      false,
    );
    S.config = clone(result.config);
    S.savedConfig = clone(result.config);
    S.revision = result.revision;
    markDirty();
    toast("主人称谓已保存。");
  },
  "reset-appearance": async () => {
    await saveAppearance({
      theme: "light",
      surface_opacity: 78,
      window_opacity: 100,
      blur: 24,
      accent: "blue",
      font_size: 14,
      native_glass: true,
      reduced_motion: false,
    });
    render();
    toast("外观已恢复默认。");
  },
  "list-models": async () => {
    await startModel("models", { config: S.config.emotion });
    toast("正在读取 LM Studio 已有模型。");
  },
  "analyze-emotion": async () => {
    S.analysisText = $("#analysis-text").value;
    if (!S.analysisText.trim()) throw new Error("请先输入要分析的文本。");
    await startModel("analyze", {
      config: S.config.emotion,
      text: S.analysisText,
    });
    toast("已开始本地分析。");
  },
  "inspect-model": async () => {
    const result = await rpc("emotion", "load_model", {
      config: S.config.emotion,
    });
    await modal(
      "本地分类器",
      `标签：${result.labels.join("、")}\n特征：${result.features.kind}\n维度：${result.features.dimension || "—"}`,
      { confirm: "知道了" },
    );
  },
  "cancel-model": async () => {
    await rpc("emotion", "cancel");
    toast("已请求停止，等待当前操作退出。");
  },
  "emotion-settings": async () => {
    S.settingTab = "advanced";
    await navigate("settings");
  },
  "import-dataset": async () => {
    const path = await rpc("choose_file", "open", [
      "Dataset (*.csv;*.jsonl;*.json;*.txt)",
    ]);
    if (!path) return;
    S.dataset.push(
      ...(await rpc("emotion", "load_dataset", {
        path,
        config: S.config.emotion,
      })),
    );
    S.datasetDirty = true;
    render();
    toast("样本已导入，请完成标注后保存。");
  },
  "save-dataset": async () => {
    const result = await rpc("emotion", "save_dataset", {
      rows: S.dataset,
      config: S.config.emotion,
    });
    S.datasetDirty = false;
    toast(`已保存 ${result.count} 条样本。`);
    render();
  },
  "add-sample": async () => {
    const text = $("#sample-text").value.trim();
    if (!text) throw new Error("请输入样本文本。");
    S.dataset.push({ text, label: $("#sample-label").value });
    S.datasetDirty = true;
    render();
  },
  "remove-sample": async (el) => {
    S.dataset.splice(Number(el.dataset.index), 1);
    S.datasetDirty = true;
    render();
  },
  train: async () => {
    S.history = [];
    await startModel("train", { rows: S.dataset, config: S.config.emotion });
    toast("训练已开始。");
  },
  "warm-train": async () => {
    S.history = [];
    await startModel("train", {
      rows: S.dataset,
      config: S.config.emotion,
      warm_start: true,
    });
    toast("正在继续训练现有分类器。");
  },
  "export-logs": async () => {
    const path = await rpc("choose_file", "save", ["Log (*.txt)"]);
    if (path)
      await rpc(
        "export_file",
        path,
        "logs",
        S.events
          .filter((e) => e.kind === "log")
          .map((e) => `[${time(e.time)}] ${e.data.text}`)
          .join("\n"),
      );
    if (path) toast("日志已导出。");
  },
  "export-analysis": async () => {
    if (!S.predictions.length) throw new Error("暂时没有可导出的分析结果。");
    const path = await rpc("choose_file", "save", ["JSON (*.json)"]);
    if (path) await rpc("export_file", path, "json", S.predictions);
    if (path) toast("分析结果已导出。");
  },
  "import-sticker": async () => {
    const path = await rpc("choose_file", "open", [
      "Images (*.png;*.jpg;*.jpeg;*.gif;*.webp)",
    ]);
    if (!path) return;
    const values = await modal("导入素材", "为素材补充标签与适用语境。", {
      confirm: "导入",
      fields:
        '<label class="dialog-field">标签（逗号分隔）<input name="tags" placeholder="开心，安慰，问候"></label><label class="dialog-field">语境说明<input name="description"></label>',
    });
    if (!values) return;
    await rpc("stickers", "import", {
      path,
      tags: values.tags.split(/[,，、]/).filter(Boolean),
      description: values.description,
    });
    await loadRecords("resources");
    toast("素材已导入。");
  },
  "remove-sticker": async (el) => {
    if (
      await modal("移除素材", "从表情素材库中移除此项。", {
        confirm: "移除",
        danger: true,
      })
    ) {
      await rpc("stickers", "remove", { id: el.dataset.id });
      await loadRecords("resources");
    }
  },
};

document.addEventListener("click", (event) => {
  const el = event.target.closest("button,[data-page]");
  if (!el) return;
  attempt(async () => {
    if (el.dataset.window) {
      if (el.dataset.window === "close") return requestClose();
      await rpc("window_action", el.dataset.window);
      return;
    }
    if (el.dataset.page) return navigate(el.dataset.page);
    if (el.dataset.action && actions[el.dataset.action])
      return actions[el.dataset.action](el);
    if (el.dataset.settingsTab) {
      S.settingTab = el.dataset.settingsTab;
      render();
    }
    if (el.dataset.memoryTab) {
      S.memoryTab = el.dataset.memoryTab;
      render();
    }
    if (el.dataset.resourceTab) {
      S.resourceTab = el.dataset.resourceTab;
      render();
      await loadRecords("resources");
    }
    if (el.dataset.emotionTab) {
      S.emotionTab = el.dataset.emotionTab;
      if (S.emotionTab === "dataset" && !S.dataset.length) {
        try {
          S.dataset = await rpc("emotion", "load_dataset", {
            config: S.config.emotion,
          });
        } catch {}
      }
      render();
    }
    if (el.dataset.themeChoice) {
      await saveAppearance({ theme: el.dataset.themeChoice });
      render();
    }
    if (el.dataset.accent) {
      await saveAppearance({ accent: el.dataset.accent });
      render();
    }
    if (el.dataset.personaIndex !== undefined) {
      if (
        S.personaDirty &&
        !(await modal(
          "切换角色编辑",
          "当前角色有未保存的修改，切换后将丢失。",
          { confirm: "切换" },
        ))
      )
        return;
      S.personaDraft = clone(S.personas.items[Number(el.dataset.personaIndex)]);
      S.personaOriginal = S.personaDraft.name;
      S.personaDirty = false;
      render();
    }
  });
});
document.addEventListener("input", (event) => {
  const el = event.target;
  if (el.dataset.config) {
    let value =
      el.type === "checkbox"
        ? el.checked
        : el.type === "number"
          ? Number(el.value)
          : el.value;
    try {
      if (el.type === "number" && (el.value === "" || !el.validity.valid))
        throw new Error();
      if (el.dataset.type === "json") value = JSON.parse(el.value);
      put(el.dataset.config, value);
      if (el.dataset.config === "onebot.active_account") {
        const account = S.config.onebot.accounts[value];
        if (account) {
          S.config.onebot.ws_url = account.ws_url;
          S.config.onebot.access_token = account.access_token;
        }
        render();
      }
      S.invalid.delete(el.dataset.config);
      el.closest(".field")?.classList.remove("error");
    } catch {
      S.invalid.add(el.dataset.config);
      el.closest(".field")?.classList.add("error");
    }
    markDirty();
  }
  if (el.dataset.personaField) {
    const key = el.dataset.personaField;
    S.personaDraft[key] = ["tags", "aliases"].includes(key)
      ? el.value
          .split(/[,，、]/)
          .map((x) => x.trim())
          .filter(Boolean)
      : el.value;
    S.personaDirty = true;
  }
  if (el.id === "glossary-text") {
    S.glossary = { ...S.glossary, text: el.value };
    S.glossaryDirty = true;
  }
  if (el.dataset.sampleText !== undefined) {
    S.dataset[Number(el.dataset.sampleText)].text = el.value;
    S.datasetDirty = true;
  }
  if (el.dataset.sampleLabel !== undefined) {
    S.dataset[Number(el.dataset.sampleLabel)].label = el.value;
    S.datasetDirty = true;
  }
  if (el.dataset.preference) {
    const key = el.dataset.preference,
      value = el.type === "checkbox" ? el.checked : Number(el.value);
    S.preferences[key] = value;
    applyAppearance(S.preferences);
    const output = $("#output-" + key);
    if (output) output.textContent = value + el.dataset.unit;
    clearTimeout(appearanceTimer);
    appearanceTimer = setTimeout(
      () => attempt(() => saveAppearance({ [key]: value })),
      160,
    );
  }
  markDirty();
  if (el.id === "analysis-text") S.analysisText = el.value;
  if (el.id === "voice-text") S.voiceText = el.value;
  if (el.id === "voice-emotion") S.voiceEmotion = el.value;
  if (el.dataset.singField) S.singingRequest[el.dataset.singField] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
  if (el.id === "message-query" || el.id === "message-group") filterMessages();
  if (el.id === "log-query" || el.id === "log-level") {
    $("#log-terminal").innerHTML = logEntries();
    scrollLogs();
  }
  if (el.id === "memory-query") {
    S.memoryQuery = el.value;
    const position = el.selectionStart;
    render();
    const next = $("#memory-query");
    next.focus();
    next.setSelectionRange(position, position);
  }
});
document.addEventListener("change", (event) => {
  if (event.target.dataset.singField === "mode") render();
  if (event.target.dataset.config === "media.gemini_web.transport") {
    S.browserTask = null;
    S.browserStatus = event.target.value === "browser"
      ? "已切换到浏览器页面模式，请打开浏览器手动登录并保存配置。"
      : "已切换到 Cookie 模式，请检查原有连接并保存配置。";
    render();
  }
  if (event.target.dataset.config === "voice.character") {
    stopVoicePlayback();
    S.voicePreview = null;
    S.voiceCheck = null;
    const selectedCharacter = event.target.value;
    attempt(() => applyVoiceSettings({character:selectedCharacter}));
    if (["voice", "singing"].includes(S.page)) render();
  }
  if (event.target.id === "log-level") {
    $("#log-terminal").innerHTML = logEntries();
    scrollLogs();
  }
});
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    attempt(saveAll);
  }
  if (event.key === "Enter" && event.target.matches("[data-page][role=button]"))
    event.target.click();
});
function filterMessages() {
  const q = ($("#message-query")?.value || "").toLowerCase(),
    group = $("#message-group")?.value.trim() || "";
  const rows = recentMessages().filter(
    (e) =>
      (!q || JSON.stringify(e.data).toLowerCase().includes(q)) &&
      (!group || String(e.data.group_id || "").includes(group)),
  );
  $("#message-feed").innerHTML = rows.length
    ? rows.map(messageCard).join("")
    : empty("没有匹配的消息", "可以尝试其他关键词或群号。", "search");
}
function handleEvent(event) {
  if (trainingEvent(event)) return;
  if (event.kind === "voice_audio") { playVoiceBlock(event.data); return; }
  if (event.kind === "browser_connected" && S.config.media.gemini_web.transport === "cookie") {
    S.browserStatus = "浏览器已连接：" + event.data.source;
    toast(S.browserStatus);
    if ($("#browser-status"))
      $("#browser-status").textContent = S.browserStatus;
  }
  if (event.kind === "model_progress") {
    if (event.data.type === "stage" && S.status.task?.kind === "voice-preview") {
      const data = event.data;
      S.voiceCheck = ({starting: "正在准备语音服务", retrying: "片段异常短，正在重新合成完整台词", encoding: "正在编码 QQ 语音", ready: "台词已合成", "cache-hit": "已复用完整语音缓存"})[data.stage] || `正在合成 ${Number(data.index) + 1}/${data.count} · ${data.emotion}`;
      if ($("#voice-progress")) $("#voice-progress").textContent = S.voiceCheck;
      if (data.stage === "synthesizing" && voiceAudioContext && data.pause_before_ms && voiceAudioTail) voiceAudioTail += data.pause_before_ms / 1000;
    }
    if (event.data.stage) {
      S.singingStatus = (singingStages[event.data.stage] || event.data.stage) + (event.data.message ? " · " + event.data.message : "");
      if ($("#singing-status")) $("#singing-status").textContent = S.singingStatus;
    }
    if (event.data.type === "epoch") {
      S.history.push(event.data);
      if ($("#training-chart"))
        $("#training-chart").innerHTML = trainingChart();
      if ($("#training-status"))
        $("#training-status").textContent = `已完成第 ${event.data.epoch} 轮`;
    }
  }
  if (event.kind === "model_result") {
    const data = event.data;
    if (data.kind === "gemini" && (S.browserTask?.id !== data.id || S.browserTask?.transport !== S.config.media.gemini_web.transport)) return;
    if (data.state === "failed") {
      if (data.kind === "voice-preview") stopVoicePlayback();
      toast(data.error, "error");
      if (data.kind === "gemini") {
        S.browserStatus = data.error;
        if ($("#browser-status")) $("#browser-status").textContent = S.browserStatus;
      }
      if (data.kind === "dsh-check") S.dshCheck = data.error;
      if (data.kind?.startsWith("voice-")) S.voiceCheck = data.error;
      if (data.kind?.startsWith("singing-")) S.singingStatus = data.error;
      if (["emotion", "voice", "singing", "settings"].includes(S.page)) render();
      return;
    }
    if (data.kind === "singing-render") {
      if (data.state === "completed") S.singingResult = data.result;
      S.singingStatus = data.state === "cancelled" ? "演唱已停止，任务文件已保留。" : "演唱已完成，可在本机试听。";
      if (["voice", "singing"].includes(S.page)) render();
    }
    if (data.kind === "singing-check") {
      const result = data.result || {};
      S.singingStatus = result.healthy ? "原唱翻唱与伴奏创唱引擎均可用。" : JSON.stringify({翻唱: result.conversion, 创唱: result.generation});
      if (["voice", "singing"].includes(S.page)) render();
    }
    if (data.kind === "voice-preview") {
      if (data.state === "completed") S.voicePreview = data.result;
      if (["voice", "singing"].includes(S.page)) render();
      toast(data.state === "cancelled" ? "语音任务已停止。" : "语音已合成并完成 SILK 转码。");
    }
    if (data.kind === "voice-check") {
      S.voiceCheck = data.result?.reachable ? "接口可用，角色与情绪已核对。" : "接口检查未完成。";
      S.remoteVoiceCharacters = data.result?.server_characters;
      if (["voice", "singing"].includes(S.page)) render();
    }
    if (data.kind === "voice-engine") {
      S.voiceCheck = data.result?.running ? "本地引擎已就绪。" : "本地引擎已停止，或当前服务由其他进程管理。";
      if (["voice", "singing"].includes(S.page)) render();
    }
    if (data.kind === "dsh-check") {
      S.dshCheck = data.result?.message || "运行时检查结束。";
      if (!data.result?.api_key_present) S.dshCheck += " API Key 环境变量未设置；可在专用 DSH Home 中配置账户。";
      if (S.page === "settings") render();
    }
    if (data.kind === "gemini") {
      if (data.state !== "completed") {
        S.browserStatus = "账号模型检查已停止。";
        if ($("#browser-status")) $("#browser-status").textContent = S.browserStatus;
        return;
      }
      S.browserStatus =
        "登录有效；可用模型：" +
        ((data.result?.models || [])
          .filter((m) => m.available)
          .map((m) => m.name)
          .join("、") || "账号默认模型");
      if ($("#browser-status"))
        $("#browser-status").textContent = S.browserStatus;
      toast("账号模型已读取。");
    }
    if (data.kind === "models") {
      S.models = data.result || [];
      toast(`已读取 ${S.models.length} 个本地模型。`);
      if (S.page === "emotion") render();
    }
    if (data.kind === "analysis") {
      S.predictions = data.result || [];
      if ($("#analysis-results"))
        $("#analysis-results").innerHTML = analysisResults();
      toast(data.state === "cancelled" ? "分析已停止。" : "分析已完成。");
      if (S.page === "emotion") render();
    }
    if (data.kind === "training") {
      S.trainingResult = data.result;
      S.history = data.result?.history || S.history;
      if (S.page === "emotion") render();
      toast(
        data.state === "cancelled"
          ? "训练已停止，已完成轮次已保留。"
          : "训练完成，最佳模型已保存。",
      );
    }
  }
}
let pollBusy = false;
async function poll() {
  if (!S.ready || pollBusy) return;
  pollBusy = true;
  try {
    const data = await rpc("poll", S.cursor);
    S.cursor = data.cursor;
    S.status = data.status;
    for (const item of data.events) {
      S.events.push(item);
      handleEvent(item);
    }
    if (S.events.length > 1600) S.events.splice(0, S.events.length - 1600);
    updateStatus();
    if (data.events.some((e) => ["message", "reply"].includes(e.kind))) {
      if (S.page === "messages") {
        const box = $("#message-feed"),
          follow = $("#feed-autoscroll")?.checked;
        filterMessages();
        if (follow) box.scrollTop = box.scrollHeight;
      }
      if (S.page === "overview" && $("#overview-feed"))
        $("#overview-feed").innerHTML = recentMessages()
          .slice(-3)
          .map(messageCard)
          .join("");
    }
    if (S.page === "logs" && data.events.some((e) => e.kind === "log")) {
      $("#log-terminal").innerHTML = logEntries();
      scrollLogs();
    }
  } catch (error) {
    $("#footer-state").textContent = "界面连接暂时中断，正在重试";
  } finally {
    pollBusy = false;
  }
}
async function boot() {
  if (S.ready) return;
  try {
    const data = await rpc("bootstrap");
    Object.assign(S, {
      ready: true,
      config: clone(data.config),
      savedConfig: clone(data.config),
      schema: data.schema,
      revision: data.revision,
      preferences: data.preferences,
      personas: data.personas,
      status: data.status,
      home: data.home,
    });
    applyAppearance(S.preferences);
    $("#workspace-name").textContent =
      data.home.split(/[\\/]/).filter(Boolean).at(-1) || "本地工作区";
    $("#workspace-name").title = data.home;
    $("#footer-engine").textContent = data.engine;
    $("#navigation").innerHTML = pages
      .map(
        ([key, label, ic]) =>
          `<button class="nav-item ${key === "overview" ? "active" : ""}" data-page="${key}">${icon(ic)}<span>${label}</span>${key === "emotion" ? '<span class="nav-count">LAB</span>' : ""}</button>`,
      )
      .join("");
    icons();
    updateStatus();
    await navigate("overview");
    setInterval(poll, 650);
    await poll();
  } catch (error) {
    $("#main-content").innerHTML =
      `<section class="card error-panel"><h2>工作空间暂时无法载入</h2><p class="section-note">${esc(error.message)}</p>${button("重新载入", "reload", "refresh")}</section>`;
  }
}
window.addEventListener("pywebviewready", boot);
if (window.pywebview?.api) boot();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (S.preferences.theme === "system")
    attempt(() => saveAppearance({}, false));
});
window.studio = {
  version: "20261117-speech-settings",
  get ready() {
    return S.ready;
  },
  requestClose,
  async smoke() {
    await loadRecords("voice");
    const views = [];
    for (const [key] of [...pages, ["appearance"], ["settings"], ["logs"]]) {
      S.page = key;
      render();
      views.push({
        page: key,
        heading: $("#main-content h1")?.textContent,
        rendered: Boolean($("#main-content h1")),
        overflow: document.documentElement.scrollWidth > innerWidth || $("#main-content").scrollWidth > $("#main-content").clientWidth + 1,
      });
    }
    for (const tab of ["model", "voice"]) {
      S.page = "settings";
      S.settingTab = tab;
      render();
      views.push({page: "settings-" + tab, heading: $("#main-content h1")?.textContent,
        rendered: Boolean($("#main-content h1")),
        overflow: document.documentElement.scrollWidth > innerWidth || $("#main-content").scrollWidth > $("#main-content").clientWidth + 1});
    }
    S.page = "overview";
    render();
    return {
      ok: views.every((r) => r.rendered && !r.overflow),
      pages: views,
      preferences: S.preferences,
      version: window.studio.version,
      bridge: S.ready,
      emoji_free: !/[\p{Extended_Pictographic}]/u.test(
        $("#navigation").textContent,
      ),
      overflow: views.some((r) => r.overflow),
    };
  },
};
