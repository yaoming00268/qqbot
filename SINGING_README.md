# 唱歌接口

唱歌独立于 GPT-SoVITS 朗读。当前角色的情绪参考音频用于 Seed-VC 音色转换，无需另外训练角色歌声模型。

支持两条流程：

- `cover`：原唱与歌词 → Demucs 分离人声及伴奏 → Seed-VC F0 歌声转换 → 混音。保留原唱旋律、节奏、语言与发音；歌词留档，不会换成其他歌词。已有干声时指定 `--vocals-only`，独立伴奏用 `--accompaniment`。
- `compose`：纯伴奏与歌词 → ACE-Step 1.5 base 的 lego 人声生成 → Seed-VC 角色转换 → 与原伴奏混合。模型根据伴奏创作演唱旋律；要求严格还原原曲唱法时，请提供原唱或导唱，使用 cover。

默认交付 MP3 文件；`voice` 转为 24kHz 单声道 PCM 后编码为腾讯 SILK，作为 QQ `record` 发送；`both` 同时提供文件与语音。生成工作不会直接发送，主代理审阅后使用既有 `deliver` 接口。控制台本地试听永不发送 QQ。

参考、分离后的人声／创唱人声增加 FFmpeg `afftdn` 降噪，音色转换后再作最多 3dB 的轻量清理；伴奏保留原始内容，仍沿用用户设置的混音增益。`voice.singing.denoise` 可独立开关、调节强度及是否清理参考。默认 6dB，避免过强处理损伤气声、齿音和歌声泛音。

相同原曲、Demucs 参数和模型指纹的分离结果按内容缓存，复用前逐个核对 WAV 和哈希；损坏缓存重新分离。可关闭 `voice.singing.reuse_separation`。切换 VoiceStudio 后，朗读与唱歌仍锁定同一个原生 GPU 后端，唱歌结束或取消后恢复原生 TTS，VoiceStudio 门面继续提供服务。manifest 记录降噪阶段、实际滤镜和缓存命中情况，原歌词和输入保留。

## 控制台

左侧导航打开“唱歌工作室”：选择输入类型、歌曲文件、歌词、情绪切片和输出形式，点击“开始演唱”。支持载入 UTF-8/UTF-16 TXT、LRC；可停止当前任务并保留中间文件。展开“唱歌权限、混音与推理设置”开启主代理权限并保存。

## 主代理 CLI / MCP

```powershell
.\run_cli.bat sing status --context-file '<当前上下文>'
.\run_cli.bat sing cover --context-file '<当前上下文>' --song '<inbox文件ID/授权路径/公网直链>' --lyrics-file '<歌词.txt>' --emotion neutral --song-output both
.\run_cli.bat sing compose --context-file '<当前上下文>' --song '<伴奏文件ID/授权路径/公网直链>' --lyrics-file '<歌词.txt>' --language ja --emotion neutral --song-output file
.\run_cli.bat status --context-file '<当前上下文>' --request-id '<任务ID>'
.\run_cli.bat cancel --context-file '<当前上下文>' --request-id '<任务ID>'
.\run_cli.bat deliver --context-file '<当前上下文>' --request-id '<任务ID>'
```

MCP 使用 `qqbot_cli(action="sing", operation="cover"/"compose", context_file=当前文件, params={song, lyrics, ...})`。`help sing cover` 和 `help sing compose` 返回完整参数。只允许普通聊天上下文发起；沿用 `process` 工作权限和现有任务取消、幂等及交付账本。跨会话文件 ID 和授权目录之外的本机文件会被拒绝。

## 独立本地 CLI（不发消息）

```powershell
.\voice.bat sing status
.\voice.bat sing check
.\voice.bat sing cover --song 'G:\歌曲\原唱.wav' --lyrics-file 'G:\歌曲\歌词.txt' --emotion neutral --output both
.\voice.bat sing compose --song 'G:\歌曲\伴奏.wav' --lyrics-file 'G:\歌曲\歌词.txt' --language ja --output file
```

日语创唱前将正文转为假名，保留段落标记；原歌词及生成用歌词分别留档。LRC 的真实时间戳原样留在原歌词中，生成时去掉时间戳，不编造歌词对齐时间。翻唱不改变原唱语言，不把中文原唱自动翻译为日语。

## 安装与资源

```powershell
.\voice.bat sing setup --python '<Python 3.11/3.12 的 python.exe>'
# 已有匹配的 PyTorch 2.8 + CUDA 12.8 时可只读复用：
.\voice.bat sing setup --python '<python.exe>' --torch-site-packages '<含 torch 的 site-packages>'
```

引擎和模型放在 `tools/singing-engine`，不打入 Bot 运行包；独立环境隔离两套不同的 Transformers 版本。首次安装需要下载模型。固定源代码版本、模型来源、版本和 SHA256 记录在 `tools/singing-engine/installation.json`。迁移时保留该目录并重建环境路径。

推理依次运行分离、创唱、转换，完成一个阶段后退出子进程释放显存。默认串行运行，并在唱歌期间临时停止经身份核对、由本项目启动的 TTS 进程，结束或取消后恢复；不会结束外部 TTS 服务。8GB 显存使用 ACE-Step base 与 CPU offload，不加载可选 LM；长曲性能取决于显存、内存和歌曲时长。

每次任务在 `data/cache/singing/<ID>/manifest.json` 保存时间、输入哈希、歌词、阶段、中间音频和结果。超长或超大的输入会被拒绝，不静默裁剪。取消会停止本任务启动的进程树；失败保留日志，不以朗读充当歌唱成果。

上游：[Seed-VC](https://github.com/Plachtaa/seed-vc)、[Demucs](https://github.com/facebookresearch/demucs)、[ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5)。模型和参考音频沿用其各自许可与来源信息。
