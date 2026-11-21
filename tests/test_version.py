"""Version shown in the web UI footer."""

import unittest

from estyma_local import version as V


class VersionTest(unittest.TestCase):
    def test_from_build_env(self):
        env = {"ESTYMA_VERSION": "v1.2.0", "ESTYMA_REVISION": "3F2A9C1D0E" + "0" * 30}
        self.assertEqual(V.detect(env), ("v1.2.0", "3f2a9c1"))
        self.assertEqual(V.label("v1.2.0", "3f2a9c1"), "v1.2.0 (3f2a9c1)")

    def test_bad_revision_dropped(self):
        self.assertEqual(V.detect({"ESTYMA_VERSION": "main", "ESTYMA_REVISION": "<b>"}), ("main", ""))
        self.assertEqual(V.label("main", ""), "main")

    def test_describe_containing_sha_not_repeated(self):
        self.assertEqual(V.label("v1.0.0-3-g3f2a9c1", "3f2a9c1"), "v1.0.0-3-g3f2a9c1")

    def test_footer_inside_main_and_escaped(self):
        page = b"<body><main><p>x</p></main><script></script></body>"
        out = V.add_footer(page, V.footer_html("<v>", ""))
        self.assertIn(b"&lt;v&gt;</span></footer>\n</main>", out)
        self.assertEqual(V.add_footer(b"<body></body>", "<footer/>"), b"<body><footer/>\n</body>")


class FooterServedTest(unittest.TestCase):
    def test_every_page_has_footer(self):
        import tempfile
        import urllib.request
        from estyma_local.web import start_web
        d = tempfile.mkdtemp()
        ui = start_web("127.0.0.1", 0, d, "", "")
        self.addCleanup(ui.shutdown)
        port = ui.server_address[1]
        for path in ("/", "/logs", "/settings"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
                body = r.read().decode()
            self.assertEqual(body.count('<footer class="version"'), 1, path)
            self.assertIn(V.label(), body)
