"""Regression: typing into dialog fields must not move focus (dialogs re-render on every keystroke)."""

from playwright.sync_api import expect, sync_playwright

BASE = "http://localhost:5173"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
    page.goto(BASE)
    page.get_by_label("Username").fill("admin")
    page.get_by_label("Password").fill("admin-password-123")
    page.get_by_role("button", name="Sign in").click()
    expect(page.locator(".toolbar")).to_be_visible(timeout=15000)

    # connection dialog: authentication tab, connection-specific credentials
    page.locator("button[title='New connection']").click()
    dlg = page.locator(".modal")
    name = dlg.get_by_label("Display name")
    name.click()
    page.keyboard.type("Typing test")
    expect(name).to_have_value("Typing test")
    dlg.get_by_role("tab", name="Authentication").click()
    dlg.locator(".radio-card", has_text="Connection-specific").click()
    for label, text in (("Username", "administrator"), ("Domain", "CORP"), ("Password", "s3cret-pass")):
        field = dlg.get_by_label(label, exact=True)
        field.click()
        page.keyboard.type(text, delay=30)
        expect(field).to_have_value(text)
        expect(field).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator(".modal")).to_have_count(0)

    # credential dialog
    page.get_by_role("button", name="Credentials").click()
    page.get_by_role("button", name="New credential").click()
    dlg = page.locator(".modal")
    for label, text in (("Name", "Typing cred"), ("Domain", "CORP"), ("Password", "abc123")):
        field = dlg.get_by_label(label, exact=True)
        field.click()
        page.keyboard.type(text, delay=30)
        expect(field).to_have_value(text)
    print("typing ok")
    page.keyboard.press("Escape")

    # details panel: visible for a selected connection, hidden while a session is open, back afterwards
    csrf = next(c["value"] for c in page.context.cookies() if c["name"] == "csrf")
    page.request.post(f"{BASE}/api/v1/connections", headers={"X-CSRF-Token": csrf, "Origin": BASE}, data={
        "name": "Panel FTP", "protocol": "ftp", "hostname": "127.0.0.1", "port": 2121,
        "auth_mode": "direct", "username": "ftpuser", "password": "ftp-secret-pw"})
    page.get_by_role("button", name="Workspace").click()
    page.reload()
    row = page.locator(".tree-row", has_text="Panel FTP").first
    row.click()
    expect(page.locator(".details-panel")).to_be_visible()
    row.dblclick()
    expect(page.locator(".fb-table")).to_be_visible(timeout=15000)
    expect(page.locator(".details-panel")).to_have_count(0)
    page.locator(".session-frame .icon-btn[title='Disconnect']").click()
    expect(page.locator(".session-frame")).to_have_count(0)
    expect(page.locator(".details-panel")).to_be_visible()
    print("details panel ok")
    browser.close()
