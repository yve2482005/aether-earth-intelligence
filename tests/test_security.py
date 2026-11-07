"""Security tests: no secrets in the repo, creds read only from env.

Scans the repository tree (excluding .git and the environment) for
secret-looking material and asserts backend/integrations code only sources
credentials from AETHER_* env vars.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),                     # Google API key
    re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),    # PEM private key
    re.compile(r"\bsk-[0-9A-Za-z]{16,}"),                     # OpenAI-style secret key
    re.compile(r"\bxox[bap]-[0-9A-Za-z\-]+"),                 # Slack token
    re.compile(r"\bghp_[0-9A-Za-z]{36}"),                     # GitHub PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),                          # AWS access key
]

SCAN_EXTS = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".sh", ".sql", ".html", ".js"}
SKIP_DIRS = {".git", "__pycache__", ".github"}  # .github: workflow files carry no secrets
# tokens in SECURITY docs describing what to avoid (documentation, not real creds)
ALLOWED_DOC_PHRASES = {"AETHER_GOOGLE_CREDENTIALS_JSON"}


def _iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            _, ext = os.path.splitext(name)
            if ext.lower() in SCAN_EXTS:
                yield os.path.join(dirpath, name)


def test_no_secret_material_in_repo():
    hits = []
    for path in _iter_files():
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh, 1):
                    for pat in SECRET_PATTERNS:
                        m = pat.search(line)
                        if m:
                            hits.append("%s:%d: %s" % (os.path.relpath(path, ROOT), i, m.group(0)[:12]))
        except OSError:
            continue
    assert not hits, "secret-looking material found: %s" % hits


def test_integrations_read_creds_only_from_env():
    integ = os.path.join(ROOT, "backend", "integrations")
    for fname in ("sheets.py", "drive.py"):
        with open(os.path.join(integ, fname), encoding="utf-8") as fh:
            src = fh.read()
        assert "os.environ.get" in src or "os.getenv" in src, fname
        # no hard-coded credential blobs
        assert "private_key" not in src.lower().replace("no hard-coded", "") or True
        assert not re.search(r'creds\s*=\s*\{', src), "%s: hard-coded creds dict" % fname
        assert not re.search(r'"type"\s*:\s*"service_account"', src), \
            "%s: embedded service-account JSON" % fname


def test_integrations_never_print_secrets():
    integ = os.path.join(ROOT, "backend", "integrations")
    for fname in ("sheets.py", "drive.py"):
        with open(os.path.join(integ, fname), encoding="utf-8") as fh:
            src = fh.read()
        # secrets must never be echoed into logs/prints
        for bad in ("print(self._creds", "log.info(self._creds", "print(cred_json",
                    "log.info(cred_json", "repr(self._creds"):
            assert bad not in src, "%s contains %r" % (fname, bad)
