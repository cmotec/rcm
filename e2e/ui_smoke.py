"""Browser smoke test of the real UI against the dev stack (Chromium via Playwright)."""

import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).parent / "shots"
OUT.mkdir(exist_ok=True)
BASE = "http://localhost:5173"
RUN = str(int(time.time()))[-4:]
errors: list[str] = []


def shot(page, name):
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("screenshot", name, flush=True)


def api(page, method, path, body=None):
    csrf = next((c["value"] for c in page.context.cookies() if c["name"] == "csrf"), "")
    r = page.request.fetch(f"{BASE}{path}", method=method, data=body, headers={"X-CSRF-Token": csrf, "Origin": BASE})
    if not r.ok:
        raise RuntimeError(f"{method} {path}: {r.status} {r.text()}")
    return r.json() if r.status != 204 else None


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    page = ctx.new_page()
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error",) else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

    page.goto(BASE)
    expect(page.get_by_role("button", name="Sign in")).to_be_visible(timeout=20000)
    shot(page, "01-login")
    page.get_by_label("Username").fill("admin")
    page.get_by_label("Password").fill("admin-password-123")
    page.get_by_role("button", name="Sign in").click()
    expect(page.locator(".toolbar")).to_be_visible(timeout=15000)

    folder = api(page, "POST", "/api/v1/folders", {"name": f"Lab {RUN}", "color": "#2563eb", "icon": "server"})
    host = api(page, "POST", "/api/v1/connections", {"name": f"Test server {RUN}", "protocol": "host", "hostname": "127.0.0.1",
                                                    "folder_id": folder["id"], "auth_mode": "direct", "username": "tester",
                                                    "password": "ssh-secret-pw", "tags": ["lab"]})
    api(page, "POST", "/api/v1/connections", {"name": f"Shell {RUN}", "protocol": "ssh", "parent_id": host["id"], "port": 2222})
    api(page, "POST", "/api/v1/connections", {"name": f"Files (SFTP) {RUN}", "protocol": "sftp", "parent_id": host["id"], "port": 2222})
    api(page, "POST", "/api/v1/connections", {"name": f"FTP box {RUN}", "protocol": "ftp", "hostname": "127.0.0.1", "port": 2121,
                                              "folder_id": folder["id"], "auth_mode": "direct", "username": "ftpuser",
                                              "password": "ftp-secret-pw"})
    api(page, "POST", "/api/v1/connections", {"name": f"Windows (no guacd) {RUN}", "protocol": "rdp", "hostname": "127.0.0.1",
                                              "folder_id": folder["id"]})
    page.reload()
    page.locator(".tree-row", has_text=f"Lab {RUN}").click()
    page.locator(".tree-row", has_text=f"Test server {RUN}").dblclick()
    page.locator(".tree-row", has_text=f"Shell {RUN}").click()
    expect(page.locator(".details-panel")).to_contain_text("inherited")
    shot(page, "02-tree-details")

    # SSH: host key confirmation, then a live terminal
    page.locator(".tree-row", has_text=f"Shell {RUN}").dblclick()
    dialog = page.locator(".modal", has_text="Unknown SSH host key")
    term = page.locator(".xterm-rows")
    expect(dialog.or_(term)).to_be_visible(timeout=15000)
    if dialog.is_visible():
        shot(page, "03-hostkey")
        dialog.get_by_role("button", name="Trust and connect").click()
    term = page.locator(".xterm-rows")
    expect(term).to_contain_text("welcome tester", timeout=20000)
    page.locator(".xterm").click()
    page.keyboard.type("echo from the browser")
    page.keyboard.press("Enter")
    expect(term).to_contain_text("echo from the browser", timeout=5000)
    shot(page, "04-terminal")

    # SFTP in a second tab
    page.locator(".tree-row", has_text=f"Files (SFTP) {RUN}").dblclick()
    expect(page.locator(".fb-table")).to_contain_text("hello.txt", timeout=20000)
    assert page.locator(".tab").count() == 2, "expected two tabs"
    shot(page, "05-sftp")

    # drag the SFTP tab into the workspace centre → floating window
    body = page.locator(".workspace-body")
    box = body.bounding_box()
    page.locator(".tab", has_text=f"Files (SFTP) {RUN}").drag_to(body, target_position={"x": box["width"] / 2, "y": box["height"] / 2})
    expect(page.locator(".floating")).to_have_count(1, timeout=5000)
    shot(page, "06-floating")

    # FTP in a new tab, then drag its tab onto the right edge of the SSH pane → split view
    page.locator(".tab", has_text=f"Shell {RUN}").click()
    page.locator(".tree-row", has_text=f"FTP box {RUN}").dblclick()
    expect(page.locator(".session-frame", has_text=f"FTP box {RUN}").locator(".fb-table")).to_contain_text("readme.txt", timeout=20000)
    page.locator(".tab", has_text=f"Shell {RUN}").click()
    leaf = page.locator(".session-frame[data-leaf]").first
    lb = leaf.bounding_box()
    page.locator(".tab", has_text=f"FTP box {RUN}").drag_to(leaf, target_position={"x": lb["width"] - 20, "y": lb["height"] / 2})
    expect(page.locator(".split-row")).to_have_count(1, timeout=5000)
    expect(term).to_contain_text("resized", timeout=8000)  # terminal resized when the pane shrank
    shot(page, "07-split")

    # minimise the floating window to the taskbar and restore it
    page.locator(".floating .icon-btn[title='Minimise']").click()
    expect(page.locator(".floating")).to_have_count(0)
    page.locator(".taskbar-item").click()
    expect(page.locator(".floating")).to_have_count(1)

    # RDP without a reachable guacd: clear error, reconnect button, nothing crashes
    page.locator(".tree-row", has_text=f"Windows (no guacd) {RUN}").dblclick()
    overlay = page.locator(".session-overlay", has_text="cannot reach guacd")
    expect(overlay).to_be_visible(timeout=30000)
    shot(page, "08-rdp-error")

    # sessions listed by the API
    sessions = api(page, "GET", "/api/v1/sessions")
    print("sessions:", [(s["connection_name"], s["state"]) for s in sessions])

    # reload → restore offer
    time.sleep(2)  # layout persistence is debounced
    page.reload()
    expect(page.locator(".banner", has_text="Restore")).to_be_visible(timeout=15000)
    shot(page, "09-restore-offer")
    browser.close()

relevant = [e for e in errors if "favicon" not in e]
print("BROWSER ERRORS:", *relevant, sep="\n  ") if relevant else print("no browser errors")
sys.exit(0)
