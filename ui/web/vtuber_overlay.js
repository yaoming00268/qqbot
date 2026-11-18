(() => {
  'use strict';
  const params = new URLSearchParams(location.search);
  const fragment = new URLSearchParams(location.hash.slice(1));
  let token = fragment.get('token') || sessionStorage.getItem('qqbot-vtuber-token') || '';
  if (token) sessionStorage.setItem('qqbot-vtuber-token', token);
  // Remove the bearer capability from address-bar history after reading it.
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  const preview = params.get('preview') === '1';
  const capture = params.get('capture') === '1';
  document.body.classList.toggle('preview', preview);
  document.body.classList.toggle('capture', capture);
  document.getElementById('capture-controls').hidden = !capture;
  if (params.get('avatar') === '0') document.getElementById('stage').style.visibility = 'hidden';
  document.getElementById('preview').hidden = !preview;
  const $ = id => document.getElementById(id);
  const state = {model: null, modelInfo: null, emotion: 'neutral', mouth: 0, targetMouth: 0, manualUntil: 0,
    socket: null, loaded: false, lastAppliedMouth: 0, activeSources: new Set(), audio: null,
    audioContext: null, analyser: null, pcmNext: 0, audioEpoch: 0, motionIndex: 0, lastReport: 0,
    audioQueue: [], draining: false, audioFinish: null};
  // Diagnostics intentionally contain no credentials; used by the local browser verification.
  window.vtuberDebug = state;
  function send(event) { if (state.socket?.readyState === WebSocket.OPEN) state.socket.send(JSON.stringify(event)); }
  function error(message) { $('error').textContent = String(message); send({type: 'error', message: String(message).slice(0, 1000)}); }
  const headers = () => ({'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'});
  async function action(event) {
    const response = await fetch('/api/event', {method: 'POST', headers: headers(), body: JSON.stringify(event)});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || response.statusText);
    return result.result;
  }
  function audioGraph() {
    if (!state.audioContext) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      state.audioContext = new AudioContext({latencyHint: 'interactive'});
      state.analyser = state.audioContext.createAnalyser();
      state.analyser.fftSize = 1024;
      state.analyser.smoothingTimeConstant = 0;
      state.analyser.connect(state.audioContext.destination);
      state.waveform = new Float32Array(state.analyser.fftSize);
    }
    return state.audioContext;
  }
  async function enableAudio() {
    const ctx = audioGraph();
    await ctx.resume();
    if (ctx.state !== 'running') { send({type: 'audio_blocked'}); throw new Error('请点击“启用声音”；OBS 浏览器源通常会自动允许播放。'); }
    $('audio-enable').textContent = '声音已启用';
    return ctx;
  }
  function stopAudio() {
    state.audioEpoch++;
    state.audioQueue.length = 0;
    if (state.audioFinish) state.audioFinish();
    if (state.audio) { state.audio.pause(); state.audio.src = ''; state.audio = null; }
    for (const source of state.activeSources) { try { source.stop(); } catch (_) {} }
    state.activeSources.clear();
    state.pcmNext = 0; state.targetMouth = 0; state.manualUntil = 0;
    send({type: 'audio_ended'});
  }
  async function playFile(event) {
    const epoch = state.audioEpoch;
    await enableAudio();
    const response = await fetch(event.url, {headers: {'Authorization': 'Bearer ' + token}});
    if (!response.ok) throw new Error('Audio download failed: ' + response.status);
    const blob = await response.blob();
    if (epoch !== state.audioEpoch) return;
    const blobURL = URL.createObjectURL(blob);
    const element = new Audio(blobURL);
    const source = state.audioContext.createMediaElementSource(element);
    source.connect(state.analyser);
    state.audio = element;
    $('caption').textContent = event.text || ''; state.emotion = event.emotion || 'neutral';
    return new Promise((resolve, reject) => {
      let finished = false;
      const finish = failure => {
        if (finished) return; finished = true;
        URL.revokeObjectURL(blobURL); source.disconnect();
        if (state.audio === element) { element.pause(); state.audio = null; state.audioFinish = null; state.targetMouth = 0; send({type: 'audio_ended'}); }
        if (failure instanceof Error) reject(failure); else resolve();
      };
      state.audioFinish = () => finish();
      element.addEventListener('ended', () => finish(), {once: true});
      element.addEventListener('error', () => finish(new Error('浏览器不能解码此音频文件。')), {once: true});
      element.play().then(() => send({type: 'audio_started'})).catch(e => { send({type: 'audio_blocked'}); finish(e); });
    });
  }
  function queueFile(event) {
    if (event.interrupt) stopAudio();
    if (state.audioQueue.length >= 32) throw new Error('Audio queue is full (32 items).');
    state.audioQueue.push(event);
    if (!state.draining) {
      state.draining = true;
      (async () => {
        try { while (state.audioQueue.length) { try { await playFile(state.audioQueue.shift()); } catch (e) { error(e.message); } } }
        finally { state.draining = false; }
      })();
    }
  }
  async function playPCM(event) {
    const ctx = await enableAudio();
    const raw = atob(event.pcm_base64), bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const width = event.sample_width, channels = event.channels;
    const frames = bytes.length / width / channels;
    const buffer = ctx.createBuffer(channels, frames, event.sample_rate);
    const view = new DataView(bytes.buffer);
    for (let channel = 0; channel < channels; channel++) {
      const data = buffer.getChannelData(channel);
      for (let frame = 0; frame < frames; frame++) {
        const offset = (frame * channels + channel) * width;
        let value;
        if (width === 1) value = (bytes[offset] - 128) / 128;
        else if (width === 2) value = view.getInt16(offset, true) / 32768;
        else if (width === 4) value = view.getInt32(offset, true) / 2147483648;
        else { value = bytes[offset] | (bytes[offset + 1] << 8) | (bytes[offset + 2] << 16); if (value & 0x800000) value -= 0x1000000; value /= 8388608; }
        data[frame] = value;
      }
    }
    if (state.pcmNext > ctx.currentTime + 4) throw new Error('PCM queue exceeds four seconds; send chunks at the audio generation rate.');
    const source = ctx.createBufferSource(); source.buffer = buffer; source.connect(state.analyser);
    state.activeSources.add(source);
    const startAt = Math.max(ctx.currentTime + 0.025, state.pcmNext);
    state.pcmNext = startAt + buffer.duration;
    source.onended = () => {
      state.activeSources.delete(source); source.disconnect();
      if (!state.activeSources.size && !state.audio) send({type: 'audio_ended'});
    };
    source.start(startAt); send({type: 'audio_started'});
  }
  const emotionParameters = {
    neutral: {}, happy: {ParamEyeLSmile: 0.8, ParamEyeRSmile: 0.8, ParamMouthForm: 0.8, ParamBrowLY: 0.2, ParamBrowRY: 0.2},
    excited: {ParamEyeLSmile: 0.6, ParamEyeRSmile: 0.6, ParamMouthForm: 1, ParamBrowLY: 0.5, ParamBrowRY: 0.5},
    sad: {ParamMouthForm: -0.8, ParamBrowLY: -0.5, ParamBrowRY: -0.5, ParamBrowLAngle: -0.5, ParamBrowRAngle: -0.5},
    angry: {ParamMouthForm: -0.5, ParamBrowLAngle: -0.8, ParamBrowRAngle: -0.8},
    surprised: {ParamEyeLOpen: 1, ParamEyeROpen: 1, ParamBrowLY: 0.8, ParamBrowRY: 0.8, ParamMouthForm: 0},
    shy: {ParamEyeLSmile: 0.3, ParamEyeRSmile: 0.3, ParamMouthForm: 0.5, ParamCheek: 0.8},
    thinking: {ParamBrowLY: 0.35, ParamBrowRY: -0.2, ParamMouthForm: -0.2}
  };
  let app;
  function fitModel() {
    if (!state.model) return;
    const model = state.model;
    const internal = model.internalModel;
    const availableWidth = preview && innerWidth > 700 ? innerWidth - 340 : innerWidth;
    const scale = Math.min(availableWidth * 0.86 / internal.originalWidth, innerHeight * 0.94 / internal.originalHeight);
    model.scale.set(scale); model.anchor.set(0.5, 0.5);
    model.position.set((preview && innerWidth > 700 ? 340 : 0) + availableWidth / 2, innerHeight * 0.5);
  }
  async function loadModel(info) {
    if (!info.ready) throw new Error(info.error || 'Model missing. Run maintenance/vtuber_assets_setup.py or select a .model3.json.');
    if (state.modelInfo?.url === info.url && state.loaded) return;
    state.loaded = false;
    if (state.model) { app.stage.removeChild(state.model); state.model.destroy(); state.model = null; }
    state.modelInfo = info;
    state.model = await PIXI.live2d.Live2DModel.from(info.url, {autoInteract: false, autoFocus: false});
    app.stage.addChild(state.model);
    state.model.internalModel.on('beforeModelUpdate', () => {
      const core = state.model.internalModel.coreModel;
      core.setParameterValueById('ParamMouthOpenY', state.mouth);
      for (const [name, value] of Object.entries(emotionParameters[state.emotion] || {})) core.setParameterValueById(name, value);
      state.lastAppliedMouth = state.mouth;
    });
    fitModel(); state.loaded = true;
    $('model-name').textContent = info.attribution;
    $('notice').textContent = info.sample ? 'Hiyori 是 Live2D 官方演示素材，非你的专属角色。正式开播前可在 Studio 选择自己的模型。' : '已载入自选 Live2D 模型。口型跟随浏览器实际播放的音频。';
    send({type: 'ready'});
  }
  async function apply(event) {
    if (event.type === 'snapshot') {
      $('caption').textContent = event.state.text || ''; state.emotion = event.state.emotion || 'neutral';
      await loadModel(event.model); return;
    }
    if (event.type === 'config') { state.loaded = false; await loadModel(event.model); return; }
    if (typeof event.text === 'string' && event.type !== 'audio' && (event.type !== 'pcm' || event.text)) $('caption').textContent = event.text;
    if (event.emotion && event.type !== 'audio') { state.emotion = event.emotion; $('emotion').value = event.emotion; }
    if (event.type === 'audio') queueFile(event);
    else if (event.type === 'pcm') await playPCM(event);
    else if (event.type === 'mouth') { state.targetMouth = event.value; state.manualUntil = performance.now() + 1200; }
    else if (event.type === 'motion' && state.model) {
      const available = state.modelInfo.motions[event.group] || 0;
      if (event.index >= available) throw new Error('This model has no such motion: ' + event.group + '/' + event.index);
      await state.model.motion(event.group, event.index, 3);
    } else if (event.type === 'expression' && state.model) {
      if (!state.modelInfo.expressions.includes(event.name)) throw new Error('Unknown model expression: ' + event.name);
      await state.model.expression(event.name);
    } else if (event.type === 'speech_stop' || event.type === 'reset') {
      stopAudio();
      if (event.type === 'reset') { state.emotion = 'neutral'; $('caption').textContent = ''; }
    }
    // pcm_end marks generation completion; already scheduled audio drains naturally.
  }
  function connect() {
    if (!token) { error('缺少本机访问凭据。请使用 Studio 的预览按钮，或运行 python vtuber_service.py url。'); return; }
    const socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    state.socket = socket;
    socket.onopen = () => { socket.send(JSON.stringify({type: 'auth', token})); $('connection').textContent = '● 已连接 · 透明画布'; };
    // Keep PCM scheduling in arrival order even when AudioContext.resume() resolves asynchronously.
    let pending = Promise.resolve();
    socket.onmessage = event => { pending = pending.then(() => apply(JSON.parse(event.data))).catch(e => error(e.message)); };
    socket.onclose = event => {
      $('connection').textContent = '服务连接断开';
      if (event.code !== 1008) setTimeout(connect, 1500); else error('访问凭据失效。请重新打开 Studio 预览。');
    };
  }
  function tick() {
    const now = performance.now();
    let target = state.manualUntil > now ? state.targetMouth : 0;
    if (state.analyser && (state.audio || state.activeSources.size)) {
      state.analyser.getFloatTimeDomainData(state.waveform);
      let energy = 0; for (const value of state.waveform) energy += value * value;
      const rms = Math.sqrt(energy / state.waveform.length);
      target = Math.max(0, Math.min(1, (rms - 0.012) * 5.5));
    }
    state.mouth += (target - state.mouth) * (target > state.mouth ? 0.65 : 0.28);
    if (state.mouth < 0.001) state.mouth = 0;
    if (now - state.lastReport > 50) { send({type: 'mouth', value: state.mouth}); state.lastReport = now; }
  }
  try {
    if (!window.PIXI?.live2d || !window.Live2DCubismCore) throw new Error('Live2D renderer missing. Run maintenance/vtuber_assets_setup.py.');
    app = new PIXI.Application({view: $('stage'), resizeTo: window, backgroundAlpha: 0, antialias: true, resolution: Math.min(devicePixelRatio, 2), autoDensity: true});
    app.ticker.add(tick); addEventListener('resize', fitModel); connect();
    $('audio-enable').onclick = () => enableAudio().catch(e => error(e.message));
    $('capture-audio').onclick = () => enableAudio().then(() => { $('capture-controls').hidden = true; }).catch(() => {
      $('capture-audio').textContent = '声音未启用，请再试一次';
    });
    if (capture) addEventListener('keydown', event => {
      if (event.key === 'F2') { event.preventDefault(); $('capture-controls').hidden = !$('capture-controls').hidden; }
    });
    $('emotion').onchange = event => action({type: 'emotion', emotion: event.target.value}).catch(e => error(e.message));
    $('caption-send').onclick = () => action({type: 'text', text: $('caption-input').value}).catch(e => error(e.message));
    $('reset').onclick = () => action({type: 'reset'}).catch(e => error(e.message));
    $('motion').onclick = () => {
      const groups = Object.keys(state.modelInfo?.motions || {}); const group = groups.find(key => key !== 'Idle') || groups[0];
      if (group) action({type: 'motion', group, index: state.motionIndex++ % state.modelInfo.motions[group]}).catch(e => error(e.message));
    };
    $('mouth-test').onclick = async () => {
      // Explicit, short diagnostic, never presented as speech-driven animation.
      for (const value of [0.7, 0.15, 0.9, 0.1, 0.55, 0]) { await action({type: 'mouth', value}); await new Promise(resolve => setTimeout(resolve, 150)); }
    };
  } catch (e) { error(e.message); }
})();
