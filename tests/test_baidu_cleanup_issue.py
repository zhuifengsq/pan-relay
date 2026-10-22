import unittest
from unittest.mock import MagicMock, patch

from src.clients.baidu_client import BaiduPanClient
from src.db.resources import (
    delete_resource_by_id,
    get_resource_by_id,
    insert_resource_simple,
    list_expired_resources,
    update_share_link,
)
from src.pan_operator import create_share, del_share
from src.services.account_pool_manager import AccountPoolManager
from src.services.storage_cleanup_service import cleanup_expired_resources


class TestBaiduCleanupIssue(unittest.TestCase):
    def setUp(self):
        self.test_share_url = "https://pan.baidu.com/s/19ZCb_Vh76F83if20VZ5sKw?pwd=t78b"
        self.test_file_name = "搬运去重介绍"
        self.test_target_path = f"/Pan-Relay分享/{self.test_file_name}"
        self.created_res_ids = []

    def tearDown(self):
        for rid in self.created_res_ids:
            try:
                delete_resource_by_id(rid)
            except Exception:
                pass

    def test_baidu_link_extraction(self):
        """测试用户反馈的百度网盘链接解析与参数提取"""
        client = BaiduPanClient("BDUSS=mock_bduss")
        surl, pwd = client._parse_share_url(self.test_share_url)
        self.assertEqual(surl, "9ZCb_Vh76F83if20VZ5sKw")
        self.assertEqual(pwd, "t78b")

    def test_del_file_parameters_correctness(self):
        """测试 BaiduPanClient.del_file 发送的参数符合百度规范（带 channel=chunlei，无 newVerify）"""
        client = BaiduPanClient("BDUSS=mock_bduss")
        client.bdstoken = "mock_bdstoken_value"

        captured = {}

        def mock_req(method, url, params=None, data=None, headers=None):
            captured["params"] = params
            captured["data"] = data
            return {"errno": 0, "taskid": 123456}

        client._request = MagicMock(side_effect=mock_req)
        ret = client.del_file([self.test_target_path])

        self.assertTrue(ret)
        self.assertEqual(captured["params"]["opera"], "delete")
        self.assertEqual(captured["params"]["channel"], "chunlei")
        self.assertNotIn("newVerify", captured["params"])
        self.assertIn(self.test_target_path, captured["data"]["filelist"])

    def test_update_share_link_marks_relay_and_account_id(self):
        """测试转存更新后，资源记录被标记为 relay 并保存 account_id，确保定时任务能扫描到"""
        success, msg, new_id = insert_resource_simple({
            "name": self.test_file_name,
            "share_link": self.test_share_url,
            "cloud_name": "百度网盘",
        })
        self.assertTrue(success)
        self.created_res_ids.append(new_id)

        # 初始插入状态为 manual
        ok, _, rec_before = get_resource_by_id(new_id)
        self.assertTrue(ok)
        self.assertEqual(rec_before.get("origin_type"), "manual")

        # 转存更新链接
        new_share_link = "https://pan.baidu.com/s/1newsharelink?pwd=abcd"
        update_ok = update_share_link(
            new_id,
            new_share_link,
            file_id=self.test_target_path,
            account_id=12,
            origin_type="relay",
        )
        self.assertTrue(update_ok)

        ok, _, rec_after = get_resource_by_id(new_id)
        self.assertTrue(ok)
        self.assertEqual(rec_after.get("origin_type"), "relay")
        self.assertEqual(rec_after.get("account_id"), 12)
        self.assertEqual(rec_after.get("file_id"), self.test_target_path)

        # 验证定时任务能扫描到此资源
        expired_items = list_expired_resources(minutes=0, limit=100)
        found = any(item["id"] == new_id for item in expired_items)
        self.assertTrue(found, "定时删除任务应能成功扫描到该 relay 转存百度资源")

    @patch("src.pan_operator._handle_netdisk_operation", return_value=True)
    def test_cleanup_expired_resources_baidu(self, mock_op):
        """测试定时任务 cleanup_expired_resources 能够成功物理删除过期百度资源"""
        new_share = "https://pan.baidu.com/s/1expire_baidu?pwd=xyz1"
        success, msg, res_id = insert_resource_simple({
            "name": self.test_file_name,
            "share_link": new_share,
            "cloud_name": "百度网盘",
        })
        self.created_res_ids.append(res_id)

        update_share_link(
            res_id,
            new_share,
            file_id=self.test_target_path,
            account_id=1,
            origin_type="relay",
        )

        mock_client = MagicMock()
        mock_client.__class__ = BaiduPanClient
        mock_client.del_file.return_value = True

        with patch.object(AccountPoolManager, "get_instance") as mock_mgr_get:
            mgr = MagicMock()
            mock_mgr_get.return_value = mgr
            mgr.get_client_by_account_id.return_value = mock_client
            mgr.get_candidate_accounts.return_value = [{"id": 1}]
            mgr.create_client_for_account.return_value = mock_client

            cleaned = cleanup_expired_resources(retention_minutes=0, limit=100)
            self.assertGreaterEqual(cleaned, 1)

    def test_del_share_safe_when_no_file_id(self):
        """测试当未转存外部资源 file_id 为空时，del_share 平滑处理不误报异常"""
        mock_client = MagicMock()
        mock_client.__class__ = BaiduPanClient

        with patch.object(AccountPoolManager, "get_instance") as mock_mgr_get:
            mgr = MagicMock()
            mock_mgr_get.return_value = mgr
            mgr.get_candidate_accounts.return_value = [{"id": 1}]
            mgr.create_client_for_account.return_value = mock_client

            res = del_share({
                "share_url": self.test_share_url,
                "file_id": None,
                "cloud_name": "百度网盘",
            })
            self.assertTrue(res)


if __name__ == "__main__":
    unittest.main()
