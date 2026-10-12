"""Second UI pass: connection dialog tabs, credential store, administration pages, import wizard."""

import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).parent / "shots"
OUT.mkdir(exist_ok=True)
BASE = "http://localhost:5173"
errors: list[str] = []
RUN = str(int(time.time()))[-4:]
RDM = (Path(__file__).resolve().parents[1] / "backend" / "tests" / "test_rdm_import.py").read_text(encoding="utf-8")
xml = RDM.split('RDM_XML = b"""', 1)[1].split('"""', 1)[0].replace("\\\\", "\\")
(OUT / "export.rdm").write_text(xml, encoding="utf-8")


def shot(page, name):
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("screenshot", name, flush=True)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_context(viewport={"width": 1500, "height": 900}).new_page()
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto(BASE)
    page.get_by_label("Username").fill("admin")
    page.get_by_label("Password").fill("admin-password-123")
    page.get_by_role("button", name="Sign in").click()
    expect(page.locator(".toolbar")).to_be_visible(timeout=15000)

    # connection dialog: new child connection under a host → inherited placeholders
    lab = page.locator(".tree-row", has_text="Lab ").last
    lab.click()
    host = page.locator(".tree-row", has_text="Test server").last
    host.click(button="right")
    page.locator(".context-menu li", has_text="New child connection").click()
    dlg = page.locator(".modal")
    expect(dlg).to_contain_text("Child of")
    expect(dlg).to_contain_text("inherited: 127.0.0.1")
    shot(page, "10-new-child-general")
    dlg.get_by_role("tab", name="Authentication").click()
    expect(dlg).to_contain_text("Inherited from parent")
    shot(page, "11-new-child-auth")
    dlg.get_by_role("tab", name="General").click()
    dlg.get_by_label("Display name").fill("RDP child")
    dlg.get_by_label("Protocol").select_option("rdp")
    dlg.get_by_role("tab", name="Protocol settings").click()
    shot(page, "12-rdp-settings")
    dlg.get_by_role("button", name="Save").click()
    expect(page.locator(".modal")).to_have_count(0, timeout=10000)
    host.dblclick()
    expect(page.locator(".tree-row", has_text="RDP child").first).to_be_visible(timeout=10000)

    # credential store
    page.get_by_role("button", name="Credentials").click()
    page.get_by_role("button", name="New credential").click()
    m = page.locator(".modal")
    m.get_by_label("Name", exact=True).fill(f"Domain admin {RUN}")
    m.get_by_label("Username", exact=True).fill("administrator")
    m.get_by_label("Password", exact=True).fill("S3cret!")
    m.get_by_role("button", name="Save").click()
    expect(page.locator(".grid")).to_contain_text(f"Domain admin {RUN}")
    page.locator("tr", has_text=f"Domain admin {RUN}").locator("button[title='Reveal secrets']").click()
    page.locator(".modal").get_by_role("button", name="Reveal").click()
    expect(page.locator(".modal input[readonly]").first).to_have_value("S3cret!")
    shot(page, "13-reveal")
    page.locator(".modal").get_by_role("button", name="Done").click()

    # administration
    page.get_by_role("button", name="Administration").click()
    shot(page, "14-admin-users")
    page.get_by_role("button", name="Import (RDM)").click()
    page.locator("input[type=file]").set_input_files(str(OUT / "export.rdm"))
    expect(page.locator(".grid")).to_contain_text("ACME DC01", timeout=10000)
    shot(page, "15-import-preview")
    page.get_by_role("button", name="Import").last.click()
    # first import: manual-migration list; repeated imports: duplicates are detected and skipped
    expect(page.locator(".import-report")).to_contain_text("Import summary", timeout=10000)
    shot(page, "16-import-report")
    page.get_by_role("button", name="Audit log").click()
    expect(page.locator(".grid")).to_contain_text("import.apply", timeout=10000)
    shot(page, "17-audit")
    for section in ["Roles & group mapping", "SSH known hosts", "Secret providers", "Global settings"]:
        page.get_by_role("button", name=section).click()
        time.sleep(0.5)
    shot(page, "18-settings")
    # dark theme
    page.get_by_role("button", name="Workspace").click()
    page.locator("button[title='Toggle theme']").click()
    shot(page, "19-dark")
    browser.close()

relevant = [e for e in errors if "401" not in e]
print("BROWSER ERRORS:", *relevant, sep="\n  ") if relevant else print("no browser errors")
