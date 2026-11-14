# -*- coding: utf-8 -*-
"""
Tests for Master Management and Title Customization.
Verifies MasterManager, title persistence, group isolation, deletion,
and GroupChatMonitorWidget context-menu setting logic.
"""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import os
import shutil
import tempfile
import unittest

from config import AppConfig, GroupCustomConfig
from master.manager import MasterManager, MasterRecord


class TestMasterManagement(unittest.TestCase):
    """测试主人身份管理、专属称谓定制与持久化"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cfg_file = os.path.join(self.tmp_dir, "config.yaml")
        self.app_cfg = AppConfig()
        self.app_cfg.save(self.cfg_file)
        self.master_mgr = MasterManager(config_obj=self.app_cfg, config_path=self.cfg_file)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_set_and_get_group_master_with_custom_title(self):
        # 设为群专属主人并指定专属称谓“哥哥”
        ok = self.master_mgr.set_master(
            user_id="qq_112984",
            nickname="雪村奈々子",
            title="哥哥",
            group_id="grp_1001",
            is_global=False,
            persist=True
        )
        self.assertTrue(ok)

        # 验证判定
        self.assertTrue(self.master_mgr.is_master("grp_1001", "qq_112984", "雪村奈々子"))
        # 验证群隔离：在群 2002 中不是主人
        self.assertFalse(self.master_mgr.is_master("grp_2002", "qq_112984", "雪村奈々子"))

        # 验证称谓获取
        self.assertEqual(self.master_mgr.get_master_title("grp_1001"), "哥哥")
        self.assertEqual(self.master_mgr.get_master_title("grp_2002"), "主人")  # 回退全局默认

        # 重新从磁盘加载验证永久保存
        reloaded_cfg = AppConfig.load(self.cfg_file)
        new_mgr = MasterManager(config_obj=reloaded_cfg, config_path=self.cfg_file)
        self.assertTrue(new_mgr.is_master("grp_1001", "qq_112984", "雪村奈々子"))
        self.assertEqual(new_mgr.get_master_title("grp_1001"), "哥哥")

    def test_set_global_master(self):
        ok = self.master_mgr.set_master(
            user_id="global_dev_999",
            nickname="开发者",
            title="司令",
            group_id="",
            is_global=True,
            persist=True
        )
        self.assertTrue(ok)

        # 全局主人的话在任何群都是主人
        self.assertTrue(self.master_mgr.is_master("grp_any", "global_dev_999", "任何昵称"))
        self.assertEqual(self.master_mgr.get_master_title("grp_any"), "司令")

    def test_remove_master(self):
        self.master_mgr.set_master(
            user_id="u_temp",
            nickname="临时主人",
            title="主人",
            group_id="grp_temp",
            persist=True
        )
        self.assertTrue(self.master_mgr.is_master("grp_temp", "u_temp", "临时主人"))

        # 删除主人
        del_ok = self.master_mgr.remove_master(user_id="u_temp", group_id="grp_temp", persist=True)
        self.assertTrue(del_ok)
        self.assertFalse(self.master_mgr.is_master("grp_temp", "u_temp", "临时主人"))

        # 磁盘持久化确认
        reloaded = AppConfig.load(self.cfg_file)
        mgr2 = MasterManager(config_obj=reloaded, config_path=self.cfg_file)
        self.assertFalse(mgr2.is_master("grp_temp", "u_temp", "临时主人"))

    def test_master_record_display_tag(self):
        r1 = MasterRecord(user_id="1", nickname="A", title="哥哥")
        self.assertEqual(r1.display_tag(), "[主人: 哥哥]")

        r2 = MasterRecord(user_id="2", nickname="B", title="主人")
        self.assertEqual(r2.display_tag(), "[主人]")

        r3 = MasterRecord(user_id="3", nickname="C", title="")
        self.assertEqual(r3.display_tag(), "[主人]")

    def test_sidebar_right_click_master_rendering(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from ui.components.monitor_sidebar import GroupChatMonitorWidget

        app = QApplication.instance() or QApplication([])
        widget = GroupChatMonitorWidget()

        # 绑定测试管理器
        from master import default_master_manager
        default_master_manager.bind_config(self.app_cfg, self.cfg_file)
        default_master_manager.set_master("qq_test", "奈々子", "哥哥", group_id="grp_sidebar", persist=True)

        widget.handle_feed_event("msg", {
            "group_id": "grp_sidebar",
            "sender": "奈々子",
            "user_id": "qq_test",
            "content": "帮我网络搜索初雪樱女主的立绘发到群里",
            "is_at_bot": True
        })

        rendered_html = widget.feed_browser.toHtml()
        self.assertIn("奈々子", rendered_html)
        self.assertIn("[主人: 哥哥]", rendered_html)
        self.assertIn("[@提及]", rendered_html)

    def test_sidebar_block_user_data_accuracy(self):
        """测试使用 MessageBlockData 解决右键错位问题，确保多条消息精确定位发送者"""
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from ui.components.monitor_sidebar import GroupChatMonitorWidget, MessageBlockData

        app = QApplication.instance() or QApplication([])
        widget = GroupChatMonitorWidget()

        # 发送多条不同用户的消息
        widget.handle_feed_event("msg", {
            "group_id": "grp_acc",
            "sender": "用户Alpha",
            "user_id": "qq_alpha",
            "content": "第一条消息"
        })
        widget.handle_feed_event("msg", {
            "group_id": "grp_acc",
            "sender": "用户Beta",
            "user_id": "qq_beta",
            "content": "第二条消息"
        })
        widget.handle_feed_event("msg", {
            "group_id": "grp_acc",
            "sender": "用户Gamma",
            "user_id": "qq_gamma",
            "content": "第三条消息"
        })

        # 遍历 document block 检查绑定的 MessageBlockData
        doc = widget.feed_browser.document()
        block_senders = []
        for i in range(doc.blockCount()):
            b = doc.findBlockByNumber(i)
            ud = b.userData()
            if isinstance(ud, MessageBlockData):
                block_senders.append((ud.record.get("sender"), ud.record.get("user_id")))

        # 确认按顺序精准绑定，绝无错位或全部指向最后一个用户的问题
        self.assertEqual(len(block_senders), 3)
        self.assertEqual(block_senders[0], ("用户Alpha", "qq_alpha"))
        self.assertEqual(block_senders[1], ("用户Beta", "qq_beta"))
        self.assertEqual(block_senders[2], ("用户Gamma", "qq_gamma"))


if __name__ == "__main__":
    unittest.main()
