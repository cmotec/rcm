# Browser end-to-end smoke tests

These tests drive the real UI in headless Chromium (Playwright) against the real API. The API runs
against an embedded PostgreSQL, with in-process SSH/SFTP and FTP servers. They cover: login, the
connection tree and details panel, SSH host-key confirmation, a live terminal (typing, resize),
SFTP/FTP file managers, floating windows, split view, the taskbar, guacd failure handling, workspace
restore, the connection dialog (inherited values), the credential store (reveal), the RDM import
wizard, the audit log and dark mode.

```bash
# once
pip install -e "backend[test]" playwright && python -m playwright install chromium
(cd frontend && npm ci)

# terminal 1: API + database + test servers
python e2e/devstack.py
# terminal 2: frontend dev server (proxies /api to :8000)
(cd frontend && npm run dev)
# terminal 3
python e2e/ui_smoke.py && python e2e/ui_smoke2.py
```

Screenshots are written to `e2e/shots/`. Both scripts can run repeatedly against the same stack.
