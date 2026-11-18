# QQBot Live2D / OBS 本机运行服务

默认方案使用 `pixi-live2d-display` 的成熟 Cubism 前端，服务内保留 TTS 与歌声生成接口。浏览器负责实际播放音频、按播放时钟计算 RMS 并驱动 Live2D 口型；它可以直接作为 OBS 透明浏览器源。已有 VTube Studio 模型时，也提供官方公开 WebSocket API 认证、热键、表情与输入参数控制。OBS 使用官方 WebSocket v5。

参考了 Open-LLM-VTuber、AIRI 的浏览器角色/语音事件架构。本实现复用现有渲染器与官方协议，保留 QQBot 已有语音、歌曲、运营流程，没有运行另一套聊天代理。

## 独立预览

在项目目录使用现有 Python：

```powershell
& 'G:\shiyan\anaconda\python.exe' vtuber_service.py serve
```

另一个终端取得本机专用预览 URL，并用浏览器打开。URL 包含访问凭据，请只交给本机 GUI/浏览器，不能放入主代理上下文或公共截图。

```powershell
& 'G:\shiyan\anaconda\python.exe' vtuber_service.py url
```

普通浏览器首次播放需要点击“启用声音”。OBS 浏览器源通常允许自动播放。透明模式为 `/overlay`，带面板预览为 `/overlay?preview=1`。`avatar=0` 可隐藏浏览器角色，用浏览器仅提供字幕和音频，另由 VTube Studio 画面提供模型。

默认端口 `127.0.0.1:18765`。启动服务只开启本机渲染/控制 API，不会连接 OBS/VTS、发送消息或开播。没有已连接浏览器时，文件音频返回 `queued=false`，不会假称已经播放，也不会在重连时重播旧语音。

## 模型与依赖

本仓库已包含可离线运行的演示资源：

- PixiJS 6.5.10，MIT；pixi-live2d-display 0.4.0，MIT。
- Live2D 官方 Cubism Core，保留文件版权头及官方专有软件许可文本。
- Live2D 官方 Hiyori 演示模型，来源 CubismWebSamples `4-r.7`，按官方 Free Material License 和各模型条款提供，**不是用户专属角色**。

文件在 `ui/web/vtuber_assets/`，许可证及完整条款在 `licenses/`，实际下载 URL、字节数与 SHA-256 在 `manifest.json`。正式使用自有模型时设置 `VTuberConfig.model_path` 为完整 `.model3.json` 路径，保留其同目录的 moc3、贴图、动作、表情、物理等引用文件。`model.inspect` 检查缺失与目录逃逸。Cubism Core 及模型的商用/发行条件以附带官方条款为准。

重新获取浏览器资源：

```powershell
& 'G:\shiyan\anaconda\python.exe' maintenance\vtuber_assets_setup.py
```

Python 只需要项目已有的 `aiohttp`、`pydantic` 与 `requests`。预览验证另用已有 Playwright、Pillow 和本机 Chromium。服务运行时不访问 CDN。

## 集成接口

`VTuberConfig` 是 Pydantic BaseModel，可放入 `AppConfig.vtuber`，支持 `model_dump()` 与 JSON schema。其字段只包含非秘密配置、环境变量名、私有文件路径。

Studio/Core/CLI 建议调用共享 HTTP 传输，不分别创建渲染状态：

```python
from vtuber_host import ensure_server, execute, overlay_url, stop_server

ensure_server(config.vtuber)  # 本进程后台线程；若其他进程已经监听则认证并复用
status = execute("status", config=config.vtuber)
execute("event", {"type": "speech", "text": "你好", "emotion": "happy"}, config.vtuber)
execute("audio", {"path": "C:/audio/voice.wav", "text": "你好", "emotion": "happy", "interrupt": False}, config.vtuber)
url = overlay_url(config.vtuber, preview=True)  # 仅传给本机浏览器 UI
stop_server(config.vtuber)  # 只会停止当前进程拥有的服务，不能结束其他进程/Bot
```

`execute` 返回实际结果 dict；请求或协议错误抛 `RuntimeError`。`ensure_server` 返回 `{running, started, status}`。无导入副作用；只有调用 `ensure_server()` 或显式 `serve` 才启动。后台服务随宿主进程结束，下次调用会重新创建；凭据持久保存，已打开的 overlay 会重连。

已在同一异步事件循环的服务中集成时，可直接使用：

```python
from vtuber_service import VTuberRuntime, create_app
runtime = VTuberRuntime(config.vtuber)
app = create_app(runtime)
await runtime.publish_event({"type": "emotion", "emotion": "happy"})
await runtime.sync_audio(path, text="台词", emotion="happy", interrupt=False)
await runtime.push_pcm(pcm_bytes, sample_rate=24000, channels=1, sample_width=2)
await runtime.execute("obs.status", {})
await runtime.close()
```

`get_vtuber_runtime(config=None)` 提供进程内单例，仅适合同一事件循环。不要跨线程直接调用该对象；跨进程/线程使用 `vtuber_host.execute()`。

## 音频、口型与事件

文件支持 WAV/MP3/OGG/M4A/FLAC/AAC/WebM，浏览器最终负责解码。单文件上限 256 MiB。`interrupt=true` 立即停止当前音频并清空等待队列；`interrupt=false` 按顺序播放，最多等待 32 条，字幕和情绪在对应音频真正开始时切换。歌声/歌曲文件调用同一个 audio 接口。

流式 PCM：`execute('pcm', {'pcm_base64': ..., 'sample_rate': 24000, 'channels': 1, 'sample_width': 2, 'text': '', 'emotion': 'neutral'})`。支持 8/16/24/32 位 little-endian 整数 PCM，8 位为 unsigned，其余为 signed；1/2 声道，8–192 kHz，每块最多 2 秒。建议按生成速率发送 20–80 ms 小块。浏览器使用 WebAudio 连续排队，最大提前缓存 4 秒，RMS 与实际播放时钟同步。生成结束发送 `{type:'pcm_end'}`，尾部自然播放完；`speech_stop` 或 `reset` 立即中止。

VTube Studio 认证后，浏览器每 50 ms 报告真实播放 RMS，服务以约 20 Hz 注入 `MouthOpen` 参数。有 VTS、无浏览器且由调用方自行播放 PCM 时，服务用到达的 PCM RMS 注入口型，调用方应按音频播放速率推送小块。

事件类型：`text`（text）、`emotion`（emotion）、`speech`（两者）、`mouth`（value 0..1）、`motion`（group/index）、`expression`（模型实际表达式 name）、`speech_stop`、`pcm_end`、`reset`。内置情绪为 neutral/happy/sad/angry/surprised/shy/thinking/excited；模型参数不同可能使某些情绪变化较弱。`model.inspect` 返回模型实际动作组与表达式，不能假定自有模型具备 Hiyori 的动作。

## HTTP / WebSocket

控制与媒体读取使用 `Authorization: Bearer <local token>`：

| 方法 | 路径 | 请求 |
|---|---|---|
| GET | `/api/status` | 服务、模型、连接与依赖状态；不含凭据 |
| GET/POST | `/api/config` | 读取/更新非秘密配置 |
| POST | `/api/event` | 事件对象 |
| POST | `/api/audio` | `{path,text,emotion,interrupt}` |
| POST | `/api/pcm` | PCM 对象 |
| POST | `/api/execute` | `{action,payload}` |
| GET | `/media/{opaque-id}` | 已注册音频文件，需认证 |

返回 `{ok:true,result:...}` 或 `{ok:false,error:...}`，参数错误 HTTP 400，鉴权缺失 401，跨来源 403，外部协议/连接失败 503。`/ws` 建连后 5 秒内必须发送 `{type:'auth',token}`，之后收到 snapshot 与有序事件。重连只恢复当前字幕/情绪，不重播历史音频。

API token 自动保存到 `data/vtuber/credentials.json`，可由 `QQBOT_VTUBER_API_TOKEN` 覆盖。OBS 密码使用 `QQBOT_OBS_PASSWORD` 或该私有文件的 `obs_password`；不要把秘密写进主 AppConfig。VTS 认证 token 保存在 `data/vtuber/vts-token.json`。本机私有凭据文件不要上传、放入模型上下文或打包发布。

默认仅允许 localhost Host、同源浏览器请求与已认证 WebSocket。若显式启用 `allow_remote`，远程 OBS/VTS 必须 `wss://`，HTTP 服务应位于具有 TLS 的可信反向代理之后；服务不自动创建公网监听或端口映射。Host/端口/凭据路径的更改需要重启服务。

## OBS / VTube Studio 动作

所有动作均须显式调用。`obs.browser_source` 只创建/更新场景及透明浏览器源，设置 `reroute_audio=true`；不会切换正在输出的场景或开播。

| 动作 | 参数/行为 |
|---|---|
| `obs.connect` / `obs.disconnect` | v5 握手、认证 / 断开 |
| `obs.status` / `obs.scenes` / `obs.inputs` | 推流状态 / 场景 / 输入列表 |
| `obs.scene` | `{scene}` 切换节目场景 |
| `obs.browser_source` | 可选 `{scene,source}`，默认 QQBot Live / QQBot Live2D |
| `obs.audio_monitor` | 可选 `{source,mode}`，默认监听并输出；可选 NONE/MONITOR_ONLY/MONITOR_AND_OUTPUT 的 OBS 枚举全名 |
| `obs.start_stream` / `obs.stop_stream` | 显式开始/停止 OBS 推流；平台推流设置须预先在 OBS 完成 |
| `obs.start_record` / `obs.stop_record` | 显式开始/停止录制 |
| `vts.connect` | `{request_new:false}`；首次在 VTS 内批准插件请求 |
| `vts.disconnect` / `vts.status` | 断开 / 当前模型 |
| `vts.models` / `vts.hotkeys` / `vts.expressions` / `vts.parameters` | 官方模型/热键/表达式/参数列表 |
| `vts.hotkey` | `{hotkey_id}` |
| `vts.expression` | `{expression:'name.exp3.json',active:true}` |
| `vts.parameter` | `{id,value}` 注入当前模型输入参数 |
| `vts.mouth` | `{value:0.6}` 注入 MouthOpen |

独立 CLI 示例（需要服务已经运行）：

```powershell
& 'G:\shiyan\anaconda\python.exe' vtuber_service.py execute --action obs.status
& 'G:\shiyan\anaconda\python.exe' vtuber_service.py execute --action audio --payload-file local-audio-job.json
```

## 已执行验证与打包

```powershell
& 'G:\shiyan\anaconda\python.exe' -m pytest test_vtuber_runtime.py -q --junitxml=reports\vtuber-tests.xml
& 'G:\shiyan\anaconda\python.exe' maintenance\vtuber_preview_verify.py --output reports\vtuber-preview
```

pytest 按项目约定进入独立 checkout。测试覆盖 HTTP/WS 鉴权与输入验证、本机 VTS 认证/热键/表情/口型协议 fixture、本机 OBS v5 challenge-response 与场景/音频/开停播 fixture，并实际启动独立 Chromium 验证真实 Live2D WebGL 渲染、透明 alpha、音频及 PCM 口型、连续音频队列。报告与截图在指定目录。未接入真实 B站账号、真实 OBS/VTS 实例，也未开播。

可执行打包需加入 `vtuber_config.py`、`vtuber_clients.py`、`vtuber_service.py`、`vtuber_host.py` 的模块，以及 `ui/web/vtuber_overlay.*` 和整个 `ui/web/vtuber_assets/`（约 5.9 MB）。不打包运行期 `data/vtuber/` 私有文件。资源使用项目既有 `resource_path()`，可从 PyInstaller `_MEIPASS` 读取。

参考协议：[VTube Studio Public API](https://github.com/DenchiSoft/VTubeStudio)、[OBS WebSocket v5](https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md)、[pixi-live2d-display](https://github.com/guansss/pixi-live2d-display)、[Live2D CubismWebSamples](https://github.com/Live2D/CubismWebSamples/tree/4-r.7)。
