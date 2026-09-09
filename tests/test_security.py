import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from api import security


class SecurityTests(unittest.TestCase):
    def test_signed_session(self):
        token = security.make_token("viewer", now=100)
        self.assertEqual(security.token_role(token, now=101), "viewer")
        self.assertIsNone(security.token_role(token, now=99))
        self.assertIsNone(security.token_role(token, now=100 + security.COOKIE_TTL))
        self.assertIsNone(security.token_role(token.replace("viewer", "admin"), now=101))

    def test_empty_password_disables_login(self):
        with patch.object(security, "ADMIN_PASSWORD", ""), patch.object(security, "VIEWER_PASSWORD", ""):
            self.assertIsNone(security.password_role(""))
            self.assertIsNone(security.password_role("Coriolis"))

    def test_central_permissions_and_origin(self):
        app = FastAPI()
        security.install_security(app)

        @app.api_route("/api/test", methods=["GET", "POST"])
        def endpoint():
            return {"ok": True}

        with patch.object(security, "PUBLIC_READ", False), TestClient(app) as client:
            self.assertEqual(client.get("/api/test").status_code, 401)
            self.assertEqual(client.post("/api/test").status_code, 403)
            client.cookies.set(security.COOKIE_NAME, security.make_token("viewer"))
            self.assertEqual(client.get("/api/test").status_code, 200)
            self.assertEqual(client.post("/api/test").status_code, 403)
            client.cookies.set(security.COOKIE_NAME, security.make_token())
            self.assertEqual(client.post("/api/test").status_code, 200)
            self.assertEqual(client.post("/api/test", headers={"Origin": "https://other.example"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()