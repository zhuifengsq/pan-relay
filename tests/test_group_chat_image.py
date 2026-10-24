import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from src.routes import system_config_routes
from src.services import system_config_service
from src.utils.auth_utils import create_jwt_token


PNG_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9n"
    "0cAAAAAASUVORK5CYII="
)


class GroupChatImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image_url = ""
        app = Flask(__name__)
        app.register_blueprint(system_config_routes.system_config_bp)
        self.client = app.test_client()
        self.fake_route_file = str(Path(self.tmp.name) / "src" / "routes" / "system_config_routes.py")

    def save_image(self, image_url):
        self.image_url = image_url
        return True

    def test_upload_and_clear(self):
        self.assertEqual(self.client.post("/api/frontend/group-chat/upload").status_code, 401)
        self.client.set_cookie("token", create_jwt_token())
        with patch.object(system_config_routes, "__file__", self.fake_route_file), \
             patch.object(system_config_service, "get_group_chat_image", side_effect=lambda: self.image_url), \
             patch.object(system_config_service, "save_group_chat_image", side_effect=self.save_image):
            bad = self.client.post(
                "/api/frontend/group-chat/upload",
                data={"file": (io.BytesIO(b"<script>alert(1)</script>"), "fake.png")},
            )
            self.assertEqual(bad.status_code, 400)

            uploaded = self.client.post(
                "/api/frontend/group-chat/upload",
                data={"file": (io.BytesIO(PNG_IMAGE), "group.png")},
            )
            self.assertEqual(uploaded.status_code, 200)
            image_url = uploaded.json["image_url"]
            self.assertTrue(image_url.startswith("/static/uploads/group_chat_"))
            target = Path(self.tmp.name) / image_url.lstrip("/")
            self.assertEqual(target.read_bytes(), PNG_IMAGE)

            cleared = self.client.post("/api/frontend/group-chat/clear")
            self.assertEqual(cleared.status_code, 200)
            self.assertEqual(self.image_url, "")
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
