import json
import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "scripts"))
from mainsail_integration import add_nginx_include, merge_navigation


class MainsailIntegrationTests(unittest.TestCase):
    def test_navigation_is_idempotent(self):
        navigation = [
            {"title": "AutoPA", "href": "/autopa/", "position": 83},
            {"title": "Local Vision", "href": "/old", "position": 10},
        ]
        entry = {
            "title": "Local Vision",
            "href": "/local-vision/",
            "position": 84,
        }
        once = merge_navigation(navigation, entry)
        twice = merge_navigation(once, entry)
        self.assertEqual(once, twice)
        self.assertEqual("/local-vision/", once[-1]["href"])
        json.dumps(once)

    def test_nginx_include_is_idempotent(self):
        source = (
            "server {\n"
            "    # AUTOPA MANAGED START\n"
            "    include /etc/nginx/snippets/autopa.conf;\n"
            "    # AUTOPA MANAGED END\n"
            "}\n")
        once = add_nginx_include(source)
        twice = add_nginx_include(once)
        self.assertEqual(once, twice)
        self.assertIn("autopa.conf", once)
        self.assertEqual(1, once.count("local-vision.conf"))


if __name__ == "__main__":
    unittest.main()
