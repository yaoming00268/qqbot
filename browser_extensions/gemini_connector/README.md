# QQBot Gemini 浏览器连接

在 QQBot 的“媒体功能与任务 → Gemini 网页端”点“连接当前浏览器”，连接页会说明安装步骤。

1. 使用已登录 Gemini 的 Chrome / Edge 配置，打开 `chrome://extensions` / `edge://extensions`。
2. 开启开发者模式，选择“加载已解压的扩展程序”，加载本文件所在目录。
3. 刷新 QQBot 打开的连接页。连接成功后返回 QQBot，模型列表会自动更新。

之后每分钟和登录 Cookie 更新时自动同步。扩展只读取 `https://gemini.google.com/` 对应的 `__Secure-1PSID` 与 `__Secure-1PSIDTS`，发送到通过连接页确认的本机 QQBot。扩展存储连接地址和连接令牌，不保存 Cookie 值，不操作 Gemini 页面、不发送对话请求。

多个浏览器配置分别安装扩展；同一 QQBot 使用最后主动连接的配置。更换配置时回到 QQBot 点连接并在所需配置中打开连接页。不要把连接页地址发送给他人。手动登录环境变量或 Cookie 文件仍有优先权。
