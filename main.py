# -*- coding: utf-8 -*-
"""
Main Entry Point for QQ Bot + Antigravity Gemini Relay.
"""

from __future__ import annotations
import asyncio
import logging
import os
import sys

# 强制设置 Windows 终端标准流为 UTF-8，防止中文乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import AppConfig
from antigravity_client import AntigravityClient
from model_backend import create_ai_client
from persona_manager import PersonaManager


def setup_logging():
    """统一配置全局及各子模块的控制台日志输出等级"""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        formatter = logging.Formatter("[%(levelname)s]\t(%(filename)s:%(lineno)d)%(funcName)s\t%(message)s")
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
    else:
        for h in root_logger.handlers:
            h.setLevel(logging.INFO)

    # 显式激活所有项目子模块日志输出
    for name in ("Main", "OneBotClient", "QQBot", "AntigravityClient", "GroupMemory", "ImageHandler", "NapCatManager"):
        sub_l = logging.getLogger(name)
        sub_l.setLevel(logging.INFO)


setup_logging()
logger = logging.getLogger("Main")



def print_banner():
    banner = r"""
  ==============================================================
   ____    ____        ____          _   
  / __ \  / __ \      |  _ \        | |  
 | |  | || |  | |     | |_) |  ___  | |_ 
 | |  | || |  | |     |  _ <  / _ \ | __|
 | |__| || |__| |     | |_) || (_) || |_ 
  \___\_\ \___\_\     |____/  \___/  \__|
                                         
   Powered by Google Antigravity & Gemini 3.8
   Official QQ Bot SDK (qq-botpy)
  ==============================================================
"""
    print(banner)


def check_configuration(config: AppConfig) -> bool:
    """检查配置完整性"""
    if not config.qq.appid or not config.qq.secret:
        logger.warning("=" * 60)
        logger.warning("⚠️  检测到尚未配置 QQ 机器人的 AppID 或 Secret！")
        logger.warning("请打开并编辑配置文件: config.yaml")
        logger.warning("1. 前往腾讯 QQ 开放平台: https://q.qq.com/#/apps/bot")
        logger.warning("2. 创建/选择机器人，在「开发设置」中获取 AppID 与 AppSecret")
        logger.warning("3. 填入 config.yaml 中的 qq.appid 与 qq.secret 字段")
        logger.warning("=" * 60)
        return False
    return True


async def diagnose_antigravity(ai_client: AntigravityClient) -> bool:
    """自检 Antigravity 服务与 Google 账号 Gemini 配额"""
    backend = "DeepSeek Harness" if type(ai_client).__name__ == "DeepSeekHarnessClient" else "Antigravity"
    logger.info("正在自检 %s 模型服务...", backend)
    ok, msg = await ai_client.probe(timeout=15.0)
    if not ok:
        logger.error("%s 服务自检失败: %s", backend, msg)
        return False

    logger.info("%s 服务自检成功: %s", backend, msg)
    return True


def main():
    print_banner()
    sys.stdout.flush()

    # 1. 加载配置
    config_file = "config.yaml"
    config = AppConfig.load(config_file)
    logger.info(f"配置文件已加载: {os.path.abspath(config_file)}")

    if (config.bot.mode or "").lower() == "agent":
        from agent_core.cli import serve
        try:
            asyncio.run(serve(config, config_file=config_file))
        except KeyboardInterrupt:
            logger.info("角色服务已停止。")
        return

    # 2. 初始化 Antigravity 客户端并进行配额自检
    ai_client = create_ai_client(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    probe_ok = loop.run_until_complete(diagnose_antigravity(ai_client))

    persona_mgr = PersonaManager()
    active_p = persona_mgr.get_active_persona()
    logger.info(f"🎭 当前激活人设预设: 【{active_p.name}】")

    from prompt_builder import compose_system_prompt
    ai_client.system_prompt = compose_system_prompt(
        base_prompt=active_p.prompt,
        max_reply_length=config.bot.max_reply_length,
        enable_action_descriptions=config.bot.enable_action_descriptions
    )

    run_mode = (config.bot.mode or "official").lower()
    logger.info(f"🌐 当前激活运行通道: 【{run_mode.upper()}】")

    # 3. 如果是纯 OneBot 本地小号模式
    if run_mode == "onebot":
        from onebot_client import OneBotClient
        from napcat_manager import NapCatManager
        import urllib.parse

        active_acc = config.onebot.get_active_account()
        ws_url = active_acc.ws_url or config.onebot.ws_url or "ws://127.0.0.1:3001"
        logger.info(f"🚀 准备启动本地 QQNT (NapCat / OneBot v11) 个人小号服务...")
        logger.info(f"📌 目标账号: 【{active_acc.name}】 (WS 节点: {ws_url})")

        # 预检 NapCat 运行状态
        parsed = urllib.parse.urlparse(ws_url)
        target_port = parsed.port or 3001
        target_host = parsed.hostname or "127.0.0.1"

        mgr = NapCatManager()
        if not mgr.is_port_open(target_host, target_port):
            logger.warning(f"⚠️ 未检测到 NapCat 核心服务 (端口 {target_port} 未开放)！")
            if mgr.is_installed():
                logger.info(f"🚀 检测到本地 NapCat 已安装，正在自动拉起 NapCatQQ (账号: {active_acc.account_id or '默认'})...")
                ok, msg = mgr.ensure_running(account_qq=active_acc.account_id, host=target_host, port=target_port, max_wait=12.0)
                if not ok:
                    logger.warning(f"⚠️ 自动拉起 NapCat 未在预期时间内就绪: {msg}")
                    logger.info("💡 提示: 您可以手动运行 napcat\\napcat.bat，或在 GUI 中启动 NapCat。")
            else:
                logger.info("💡 提示: 本地尚未安装 napcat，请在 GUI 中点击「一键下载并配置 NapCat」或手动部署。")
        else:
            logger.info(f"✅ 检测到 NapCat 核心服务正在运行中 (端口 {target_port} 处于监听状态)")

        ob = OneBotClient(
            config=config,
            antigravity_client=ai_client,
            persona_manager=persona_mgr
        )
        logger.info("💬 正在建立 WebSocket 长连接，即将进入消息监听循环...")
        try:
            loop.run_until_complete(ob.start())
        except KeyboardInterrupt:
            logger.info("OneBot 机器人已被用户手动停止。")
        except asyncio.CancelledError:
            logger.info("OneBot 机器人已平滑停止。")
        except Exception as e:
            logger.critical(f"OneBot 运行异常崩溃: {e}", exc_info=True)
        finally:
            try:
                loop.run_until_complete(ob.graceful_stop())
            except Exception as ge:
                logger.warning(f"OneBot 关闭清理异常: {ge}")
            if hasattr(ai_client, "aclose"):
                loop.run_until_complete(ai_client.aclose())
        return

    # 4. 检查 QQ 官方开放平台凭据 (官方或双通道模式需要)
    has_qq_credentials = check_configuration(config)
    if not probe_ok and has_qq_credentials:
        logger.warning("⚠️ Antigravity 配额自检未通过，机器人仍将继续连接 QQ 网关，但 AI 对话可能暂时无法响应。")
    if not has_qq_credentials:
        print("\n" + "=" * 60)
        if probe_ok:
            print("🎉 Antigravity Gemini 账号配额测试通过！本地中继已就绪。")
        print("👉 请在 config.yaml 中填入您的 QQ 机器人 AppID 和 Secret，保存后即可启动！")
        print("👉 或者在 GUI 中切换运行模式为「本地 QQNT 个人小号 (OneBot)」直接免 AppID 运行！")
        print("=" * 60 + "\n")
        return

    # 若为双通道模式，在后台线程中同步拉起 OneBot
    ob_client_box = [None]
    ob_thread_box = [None]

    if run_mode == "dual":
        import threading
        from onebot_client import OneBotClient

        def _run_ob_background():
            ob_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ob_loop)
            # Each event loop owns its AI locks, session file, and persona state.
            ob_config = config.model_copy(deep=True)
            ob_ai = create_ai_client(ob_config,
                system_prompt=ai_client.system_prompt,
                sessions_file="data/onebot_sessions.json",
            )
            ob_ai._executable = ai_client._executable
            ob_ai._is_ready = ai_client._is_ready
            ob_ai.ls_address, ob_ai.csrf_token = ai_client.ls_address, ai_client.csrf_token
            ob_ai.project_id = ai_client.project_id
            ob_client = OneBotClient(
                config=ob_config,
                antigravity_client=ob_ai,
                persona_manager=PersonaManager()
            )
            ob_client_box[0] = ob_client
            try:
                ob_loop.run_until_complete(ob_client.start())
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
            except Exception as e:
                logger.error(f"[OneBot] 后台协程异常: {e}")
            finally:
                try:
                    ob_loop.run_until_complete(ob_client.graceful_stop())
                except Exception as ge:
                    logger.warning(f"[OneBot] 退出整理群记忆异常: {ge}")
                if hasattr(ob_ai, "aclose"):
                    ob_loop.run_until_complete(ob_ai.aclose())
                try:
                    ob_loop.close()
                except Exception:
                    pass

        ob_thread = threading.Thread(target=_run_ob_background, daemon=False, name="OneBotDualThread")
        ob_thread_box[0] = ob_thread
        ob_thread.start()
        logger.info("🎉 [Dual-Mode] 本地 QQNT 个人小号后台监听已启动！")

    # Only the official channel needs the QQ SDK; OneBot avoids importing its
    # client and registering its logging handlers during startup.
    import botpy
    from bot_client import GeminiQQBot

    # 5. 设置 QQ 机器人意图并启动官方机器人
    intents = botpy.Intents.default()
    intents.public_messages = True
    intents.direct_message = True

    bot = GeminiQQBot(
        config=config,
        antigravity_client=ai_client,
        persona_manager=persona_mgr,
        intents=intents
    )

    logger.info("🚀 正在连接腾讯 QQ 开放平台网关...")
    try:
        bot.run(
            appid=config.qq.appid,
            secret=config.qq.secret
        )
    except KeyboardInterrupt:
        logger.info("机器人已被用户手动停止。")
    except Exception as e:
        logger.critical(f"机器人运行异常崩溃: {e}", exc_info=True)
    finally:
        # 统一停机协调：通知 OneBot 后台通道退出
        if ob_client_box[0]:
            try:
                ob_client_box[0].stop()
            except Exception as se:
                logger.warning(f"通知 OneBot 停止异常: {se}")

        if config.bot.memory_enabled and hasattr(bot, "memory_mgr") and not getattr(bot, "_graceful_stopped", False):
            try:
                logger.info("🛑 官方通道正在关闭，大模型开始整理本次会话群记忆与群人物刻写...")
                loop.run_until_complete(bot.memory_mgr.summarize_and_save_session(ai_client))
                bot.memory_mgr.close_run()
            except Exception as me:
                logger.warning(f"官方通道关闭整理群记忆异常: {me}")

        if ob_thread_box[0] and ob_thread_box[0].is_alive():
            logger.info("⏳ 正在等待 OneBot 后台线程完成优雅停机与群记忆归档...")
            ob_thread_box[0].join(timeout=15.0)
            if ob_thread_box[0].is_alive():
                logger.warning("⚠️ OneBot 后台线程停机超时 (15s)，进程继续退出。")
        if hasattr(ai_client, "aclose"):
            loop.run_until_complete(ai_client.aclose())


if __name__ == "__main__":
    main()
