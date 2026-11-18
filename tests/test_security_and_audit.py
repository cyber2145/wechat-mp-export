from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_mp_export.config import (
    ensure_private_dir,
    mask_secret,
    mask_url_credentials,
    redact_text,
    write_private_json,
)
from wechat_mp_export.privacy_audit import audit_directory


class TestSecurityAndAudit(unittest.TestCase):
    def test_private_dir_and_0600_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp) / "private_state"
            ensure_private_dir(sdir)
            self.assertTrue((sdir / ".gitignore").exists())
            sfile = sdir / "session.json"
            tok_key = "access" + "Token"
            write_private_json(sfile, {"vid": "10001", tok_key: "synthetic_" + "val_12345"})
            if os.name != "nt":
                dir_mode = stat.S_IMODE(sdir.stat().st_mode)
                file_mode = stat.S_IMODE(sfile.stat().st_mode)
                self.assertEqual(dir_mode, 0o700)
                self.assertEqual(file_mode, 0o600)

    def test_redaction_helpers(self) -> None:
        fake_pat = "pat_" + ("AbCd" * 6)
        fake_tok = "tok_" + ("12345" * 3)
        fake_rft = "rft_" + ("98765" * 3)
        tok_json = json.dumps({"access" + "Token": fake_tok, "refresh" + "Token": fake_rft})
        raw = (
            f"{tok_json} "
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload "
            f"Cookie: {'wr_skey'}=mysecretcookie; {'pass_ticket'}=pt_secret_999 "
            f"token={fake_pat}"
        )
        scrubbed = redact_text(raw)
        self.assertNotIn(fake_tok, scrubbed)
        self.assertNotIn(fake_rft, scrubbed)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", scrubbed)
        self.assertNotIn("mysecretcookie", scrubbed)
        self.assertNotIn("pt_secret_999", scrubbed)
        self.assertNotIn(fake_pat, scrubbed)
        self.assertEqual(mask_secret("1234567890", 2, 2), "12***90")
        self.assertEqual(
            mask_url_credentials("http://alice:supersecret@proxy.example.com:8080"),
            "http://***:***@proxy.example.com:8080",
        )

    def test_error_response_redaction_handles_python_dicts_and_json(self) -> None:
        fields = (
            "accessToken", "access_token", "refreshToken", "refresh_token",
            "wxAccessToken", "wx_access_token", "id_token", "skey", "wr_skey",
            "wx_code", "pass_ticket", "wap_sid2", "password", "passwd",
            "token", "api_key", "client_secret",
        )
        secret = "synthetic_" + "private_value"
        for field in fields:
            payload = {field: secret, "errcode": -1}
            for text in (str(payload), json.dumps(payload), f"{field}={secret}"):
                with self.subTest(field=field, text_format=text[0]):
                    self.assertNotIn(secret, redact_text(text))
                    self.assertIn("***REDACTED***", redact_text(text))
            self.assertIn("errcode", redact_text(str(payload)))

        # A token-exchange error uses str(response), so escaped quotes and spaces
        # must not leave part of the credential behind in the resulting message.
        for secret in ("x", "synthetic 'quoted' and \"double-quoted\" secret\nvalue"):
            for serialize in (str, json.dumps):
                payload = serialize({"password": secret, "errcode": -1})
                scrubbed = redact_text(payload)
                self.assertNotIn("synthetic", scrubbed)
                self.assertNotIn("quoted", scrubbed)
                self.assertEqual(scrubbed, '{"password": "***REDACTED***", "errcode": -1}'
                                 if serialize is json.dumps else
                                 "{'password': \"***REDACTED***\", 'errcode': -1}")

        for field in ("Authorization", "Cookie", "Set-Cookie"):
            secret = "synthetic " + "header value; second=value"
            for serialize in (str, json.dumps):
                self.assertNotIn("synthetic", redact_text(serialize({field: secret})))
                self.assertNotIn("second=value", redact_text(serialize({field: secret})))

    def test_privacy_auditor_catches_leaks_and_passes_repo(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        self.assertEqual(audit_directory(repo_root), [])

        with tempfile.TemporaryDirectory() as tmp:
            bad_dir = Path(tmp)
            (bad_dir / ".env.example").write_text("HALO_PAT=pat_xxxxxxxxxxxxxxxxxxxxxxxx\n", encoding="utf-8")
            self.assertEqual(audit_directory(bad_dir), [])

            tok_key = "access" + "Token"
            (bad_dir / "session.json").write_text(f'{{"{tok_key}": "synthetic_secret_123"}}', encoding="utf-8")
            home_str = "/" + "home/" + "johndoe" + "/workspace/data\n"
            (bad_dir / "notes.md").write_text(f"Saved under {home_str}", encoding="utf-8")
            (bad_dir / ".config.py.swp").write_bytes(b"b0VIM 9.1\x00")
            findings = audit_directory(bad_dir)
            kinds = {f.kind for f in findings}
            self.assertIn("sensitive_filename", kinds)
            self.assertIn("json_credential_value", kinds)
            self.assertIn("posix_user_home_path", kinds)


if __name__ == "__main__":
    unittest.main()
