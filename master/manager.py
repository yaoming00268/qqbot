# -*- coding: utf-8 -*-
"""
Master Management Module for QQBot.
Provides persistent storage, group isolation, title/appellation configuration,
and runtime identification for bot masters.
"""

from __future__ import annotations
import os
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, Any

logger = logging.getLogger("MasterManager")


@dataclass
class MasterRecord:
    """主人记录信息"""
    user_id: str
    nickname: str = ""
    title: str = "主人"
    group_id: str = ""
    is_global: bool = False

    def display_tag(self) -> str:
        """生成侧边栏或消息展示用的标签，如 [主人: 哥哥] 或 [主人]"""
        if self.title and self.title.strip() not in ("主人", ""):
            return f"[主人: {self.title.strip()}]"
        return "[主人]"


class MasterManager:
    """
    主人身份与专属称谓持久化管理器。
    支持：
    1. 群聊专属主人与全局主人的独立判定与隔离；
    2. 主人专属称谓自定义（如“哥哥”、“主人”、“司令”等）；
    3. 实时从监控侧边栏通过右键设置/修改称谓，并永久保存到 config.yaml；
    4. 随时删除/取消主人身份。
    """

    def __init__(self, config_obj: Any = None, config_path: str = "config.yaml"):
        self.config_path = config_path
        self._config = config_obj

    def bind_config(self, config_obj: Any, config_path: Optional[str] = None):
        """绑定当前运行时的 AppConfig 实例"""
        self._config = config_obj
        if config_path:
            self.config_path = config_path

    def _get_app_config(self) -> Any:
        if self._config is not None:
            return self._config
        try:
            from config import AppConfig
            if os.path.exists(self.config_path):
                self._config = AppConfig.load(self.config_path)
            else:
                self._config = AppConfig()
            return self._config
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return None

    def is_master(self, group_id: str, user_id: str, nickname: str = "") -> bool:
        """判定指定用户在指定群聊或全局是否为主人"""
        cfg = self._get_app_config()
        if not cfg or not hasattr(cfg, "bot"):
            return False
        return cfg.bot.is_master(str(group_id or "").strip(), str(user_id or "").strip(), str(nickname or "").strip())

    def get_master_title(self, group_id: str = "", user_id: str = "") -> str:
        """获取指定群聊中主人的专属称谓（如：哥哥、主人），若未配置则回退为默认主人称谓"""
        cfg = self._get_app_config()
        if not cfg or not hasattr(cfg, "bot"):
            return "主人"

        # 1. 优先查本群专属称谓
        if group_id:
            grp = cfg.bot.get_group_config(str(group_id).strip())
            if grp:
                title = getattr(grp, "master_title", "") or getattr(grp, "master_name", "")
                if title and title.strip():
                    return title.strip()

        # 2. 全局称谓
        global_title = getattr(cfg.bot, "master_title", "主人")
        return str(global_title or "主人").strip()

    def get_master_record(self, group_id: str, user_id: str, nickname: str = "") -> Optional[MasterRecord]:
        """查询用户的主人记录详情"""
        cfg = self._get_app_config()
        if not cfg or not hasattr(cfg, "bot"):
            return None

        u_id = str(user_id or "").strip()
        g_id = str(group_id or "").strip()
        n_name = str(nickname or "").strip()

        # 检查群专属主人
        if g_id:
            grp = cfg.bot.get_group_config(g_id)
            if grp:
                grp_mid = str(grp.master_id or "").strip()
                grp_mnick = str(getattr(grp, "master_nickname", "") or "").strip()
                grp_mname = str(grp.master_name or "").strip()
                grp_title = str(getattr(grp, "master_title", "") or grp.master_name or "主人").strip()

                is_grp_master = False
                if grp_mid and u_id and u_id == grp_mid:
                    is_grp_master = True

                if is_grp_master:
                    return MasterRecord(
                        user_id=grp_mid or u_id,
                        nickname=grp_mnick or n_name or grp_mname,
                        title=grp_title or "主人",
                        group_id=g_id,
                        is_global=False
                    )

        # 检查全局主人
        from messaging import is_master_user
        if cfg.bot.master_id and u_id and is_master_user(cfg.bot.master_id, u_id):
            title = getattr(cfg.bot, "master_title", "主人") or "主人"
            return MasterRecord(
                user_id=u_id,
                nickname=n_name,
                title=title,
                group_id="",
                is_global=True
            )

        return None

    def set_master(
        self,
        user_id: str,
        nickname: str = "",
        title: str = "主人",
        group_id: str = "",
        is_global: bool = False,
        persist: bool = True
    ) -> bool:
        """
        设置主人身份与专属称谓，并永久保存至配置文件。
        :param user_id: 主人 QQ 号或 OpenID
        :param nickname: 主人的昵称/名片
        :param title: 专属称谓（例如: 哥哥 / 主人 / 司令）
        :param group_id: 群号（若指定且非全局，则设置为此群专属主人）
        :param is_global: 是否设为全局主人
        :param persist: 是否写入磁盘保存
        """
        cfg = self._get_app_config()
        if not cfg or not hasattr(cfg, "bot"):
            logger.error("无法获取 AppConfig 实例，设置主人失败")
            return False

        u_id = str(user_id or "").strip()
        n_name = str(nickname or "").strip()
        c_title = str(title or "主人").strip() or "主人"
        g_id = str(group_id or "").strip()

        if is_global or not g_id:
            # 设置全局主人
            # 支持多个全局主人逗号分隔，若不存在则追加
            current_ids = [x.strip() for x in str(cfg.bot.master_id or "").replace(";", ",").split(",") if x.strip()]
            if u_id and u_id not in current_ids:
                current_ids.append(u_id)
            cfg.bot.master_id = ", ".join(current_ids)
            cfg.bot.master_title = c_title
            logger.info(f"已设置全局主人: user_id={u_id}, title={c_title}")
        else:
            # 设置单群专属主人（群聊隔离）
            from config import GroupCustomConfig
            grp = cfg.bot.get_group_config(g_id)
            if not grp:
                grp = GroupCustomConfig(
                    group_id=g_id,
                    group_name=f"群 {g_id}",
                    master_name=n_name or c_title,
                    master_id=u_id,
                    master_nickname=n_name,
                    master_title=c_title
                )
                cfg.bot.group_settings[g_id] = grp
            else:
                grp.master_id = u_id
                grp.master_name = n_name or c_title
                grp.master_nickname = n_name
                grp.master_title = c_title
            logger.info(f"已设置群 {g_id} 专属主人: user_id={u_id}, nickname={n_name}, title={c_title}")

        if persist:
            try:
                if hasattr(cfg, "save"):
                    cfg.save(self.config_path)
                elif hasattr(cfg, "save_to_file"):
                    cfg.save_to_file(self.config_path)
                logger.info(f"主人配置已永久保存至: {self.config_path}")
            except Exception as e:
                logger.error(f"保存主人配置至 {self.config_path} 失败: {e}")
                return False

        return True

    def remove_master(
        self,
        user_id: str = "",
        group_id: str = "",
        is_global: bool = False,
        persist: bool = True
    ) -> bool:
        """
        移除主人身份，并永久保存至配置文件。
        """
        cfg = self._get_app_config()
        if not cfg or not hasattr(cfg, "bot"):
            return False

        u_id = str(user_id or "").strip()
        g_id = str(group_id or "").strip()

        modified = False
        # 移除本群专属主人
        if g_id and g_id in cfg.bot.group_settings:
            grp = cfg.bot.group_settings[g_id]
            if not u_id or grp.master_id == u_id:
                grp.master_id = ""
                grp.master_name = ""
                if hasattr(grp, "master_nickname"):
                    grp.master_nickname = ""
                if hasattr(grp, "master_title"):
                    grp.master_title = ""
                modified = True
                logger.info(f"已移除群 {g_id} 的专属主人设置")

        # 移除全局主人
        if is_global or not g_id:
            current_ids = [x.strip() for x in str(cfg.bot.master_id or "").replace(";", ",").split(",") if x.strip()]
            if u_id:
                if u_id in current_ids:
                    current_ids.remove(u_id)
                    cfg.bot.master_id = ", ".join(current_ids)
                    modified = True
                    logger.info(f"已从全局主人列表中移除 user_id={u_id}")
            elif is_global:
                cfg.bot.master_id = ""
                modified = True


        if modified and persist:
            try:
                if hasattr(cfg, "save"):
                    cfg.save(self.config_path)
                elif hasattr(cfg, "save_to_file"):
                    cfg.save_to_file(self.config_path)
                logger.info(f"主人配置变更已永久保存至: {self.config_path}")
            except Exception as e:
                logger.error(f"保存配置文件失败: {e}")
                return False

        return True


default_master_manager = MasterManager()
