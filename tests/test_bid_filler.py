"""Real headless Chrome against local copies of Freelancer's bid form fields."""
import os

import pytest

from freelance_agents.bid_filler import BidFiller

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
pytestmark = pytest.mark.skipif(not os.path.exists(CHROME), reason="Chrome not installed")

FORM = """<!doctype html><html><body>
<input id="bidAmountInput" type="number"><input id="periodInput" type="number">
<textarea id="descriptionTextArea"></textarea>
<button id="place" onclick="document.title='CLICKED'">Place Bid</button>
</body></html>"""

# Mimics Freelancer's late page load: 1.2 s after the form appears, the proposal box is wiped.
LATE_RESET_FORM = FORM.replace("</body>", """<script>
setTimeout(() => { document.querySelector('#descriptionTextArea').value = ''; }, 1200);
</script></body>""")


def run(tmp_path, html, **kw):
    page_file = tmp_path / "form.html"
    page_file.write_text(html, encoding="utf-8")
    filler = BidFiller(tmp_path / "profile", headless=True)
    try:
        filler.fill(page_file.as_uri(), 14500, 3, "My proposal text " * 10, **kw)
        page = filler._ctx.pages[-1]
        return {sel: page.input_value(sel) for sel in ("#bidAmountInput", "#periodInput", "#descriptionTextArea")}, page.title()
    finally:
        filler.close()


def test_fills_fields_and_never_clicks_place_bid(tmp_path):
    values, title = run(tmp_path, FORM, settle_ms=200)
    assert values == {"#bidAmountInput": "14500", "#periodInput": "3", "#descriptionTextArea": "My proposal text " * 10}
    assert title != "CLICKED"


def test_refills_when_page_wipes_the_proposal(tmp_path):
    values, _ = run(tmp_path, LATE_RESET_FORM, settle_ms=800)
    assert values["#descriptionTextArea"] == "My proposal text " * 10
