"""
Guards for UI defects found in the cross-cutting template/CSS/JS review.

Each test pins one specific failure that was silent in the browser: a control
that did nothing, a value that was dropped on the way to the server, a script
that stopped the rest of the page. They are cheap string/structure assertions
because that is exactly the level the bugs lived at.
"""
import re
from pathlib import Path

from models import User

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"
STATIC = REPO_ROOT / "static"


def _login(client, db_session, username):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post("/login", data={"username": username, "password": "testpassword"})
    return user


def _read(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class TestLoginPasswordToggle:
    """
    The login page carried a data-toggle-password button, but the only code
    that implements it lives in app_ui.js and the page loaded no scripts at
    all. Pressing the button did nothing.
    """

    def test_login_page_loads_the_script_that_implements_its_toggle(self, client):
        html = client.get("/login").get_data(as_text=True)

        assert "data-toggle-password" in html
        assert "js/app_ui.js" in html

    def test_app_ui_still_implements_the_toggle(self):
        assert "data-toggle-password" in _read("static/js/app_ui.js")


class TestExportSettingsAreNotDuplicated:
    """
    The export form held a mobile copy and a desktop copy of the same fields,
    hidden from each other with CSS. Hidden inputs are still submitted, so
    every export sent markup and qty twice and Flask's .get() kept the first —
    the hidden mobile default. Editing the visible desktop field did nothing.
    """

    def test_markup_and_qty_appear_once_each(self, client, db_session):
        _login(client, db_session, "export_dupe_user")

        html = client.get("/").get_data(as_text=True)

        assert html.count('name="markup"') == 1
        assert html.count('name="qty"') == 1

    def test_no_mobile_only_duplicate_of_the_export_panel(self):
        source = _read("templates/index.html")

        assert "markup_mobile" not in source
        assert "profit_margin_mobile" not in source

    def test_export_actions_are_offered_on_every_width(self, client, db_session):
        _login(client, db_session, "export_parity_user")

        html = client.get("/").get_data(as_text=True)

        # The image ZIP button used to exist only in the desktop-only block.
        assert html.count("/export_images") == 1


class TestInlineHandlersAreDefined:
    """
    index.html referenced four oninput handlers that were never written, so
    every keystroke in the export fields threw a ReferenceError.
    """

    @staticmethod
    def _defined_function_names():
        names = set()
        sources = list(TEMPLATES.glob("*.html")) + list((STATIC / "js").glob("*.js"))
        for path in sources:
            text = path.read_text(encoding="utf-8")
            names |= set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", text))
            names |= set(
                re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:function|\()", text)
            )
            names |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", text))
        return names

    def test_every_inline_handler_resolves_to_a_definition(self):
        known_globals = {
            "return", "if", "this", "confirm", "alert", "prompt",
            "parseInt", "parseFloat", "Number", "String",
        }
        defined = self._defined_function_names()

        unresolved = {}
        for path in sorted(TEMPLATES.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            attributes = re.findall(
                r'\bon(?:click|change|input|submit|focus|blur|keyup|keydown)\s*=\s*"([^"]*)"',
                text,
            )
            for body in attributes:
                for call in re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", body):
                    if call in known_globals or call in defined:
                        continue
                    unresolved.setdefault(path.name, set()).add(call)

        assert unresolved == {}, f"inline handlers with no definition: {unresolved}"


class TestRichTextInitIsGuarded:
    """
    tinymce.init ran as the first statement of the product edit page's
    DOMContentLoaded handler. When the CDN failed the ReferenceError took the
    image grid, tag field, lightbox and dirty tracking down with it.
    """

    RICH_TEXT_TEMPLATES = (
        "templates/product_detail.html",
        "templates/pricelist_edit.html",
        "templates/manage_templates.html",
    )

    def test_no_template_calls_tinymce_init_directly(self):
        for relative_path in self.RICH_TEXT_TEMPLATES:
            assert "tinymce.init(" not in _read(relative_path), relative_path

    def test_templates_use_the_guarded_helper(self):
        for relative_path in self.RICH_TEXT_TEMPLATES:
            assert "ESPUI.initRichText(" in _read(relative_path), relative_path

    def test_helper_checks_for_the_library_before_using_it(self):
        source = _read("static/js/app_ui.js")

        assert "function initRichText(" in source
        assert 'typeof window.tinymce === "undefined"' in source

    def test_remaining_tinymce_uses_are_guarded(self):
        """Every other tinymce.* call must sit behind a typeof check nearby."""
        lines = _read("templates/product_detail.html").splitlines()

        for index, line in enumerate(lines):
            if "<script src=" in line:  # loading the library is not using it
                continue
            if "tinymce." not in line or "ESPUI" in line:
                continue
            # The guard is usually the enclosing if, a line or two above.
            context = "\n".join(lines[max(0, index - 3):index + 1])
            assert "typeof tinymce" in context, line.strip()


class TestBatchDialogDoesNotNavigate:
    """
    The batch-edit dialog's <form> had no submit handler. With a single text
    input, Enter triggered implicit submission and reloaded the page, losing
    both the dialog and the selection.
    """

    def test_batch_dialog_form_blocks_implicit_submission(self):
        source = _read("templates/index.html")
        start = source.index("function buildBatchDialogContent(")
        body = source[start:start + 1200]

        assert "addEventListener('submit'" in body
        assert "preventDefault()" in body


class TestPricingRuleNamesSurviveQuotes:
    """
    editRule() took the rule name interpolated into a single-quoted JS string.
    Jinja escapes ' to &#39;, the HTML parser turns it back into ', and the
    handler became a syntax error — that rule's edit button stopped working.
    """

    def test_edit_button_carries_values_in_data_attributes(self):
        source = _read("templates/pricing.html")

        assert "onclick=" not in source
        assert "data-rule-name=" in source

    def test_a_quoted_rule_name_does_not_break_the_markup(self, client, db_session):
        from models import PricingRule

        user = _login(client, db_session, "quote_rule_user")
        db_session.add(
            PricingRule(user_id=user.id, name="太郎's ルール", margin_rate=30,
                        shipping_cost=0, fixed_fee=0)
        )
        db_session.commit()

        html = client.get("/pricing").get_data(as_text=True)

        # Escaped inside the attribute, never as a bare quote that would end it.
        assert "data-rule-name=\"太郎&#39;s ルール\"" in html


class TestCatalogRateFetchCannotHang:
    """
    The exchange-rate fetch had no timeout. A silent API left ratesReady false
    forever, which made the currency selector a no-op with no error shown.
    """

    def test_fetch_is_aborted_after_a_timeout(self):
        source = _read("templates/catalog.html")

        assert "AbortController" in source
        assert "RATE_FETCH_TIMEOUT_MS" in source
        assert "signal: controller.signal" in source

    def test_selector_is_disabled_until_rates_are_usable(self):
        source = _read("templates/catalog.html")

        assert "function setCurrencySelectorReady(" in source
        assert "setCurrencySelectorReady(false)" in source
        assert source.count("setCurrencySelectorReady(true)") == 2


class TestResponsiveLayout:
    def test_every_table_can_scroll_sideways(self):
        """trash.html was the only template whose table had no scroll wrapper."""
        for path in sorted(TEMPLATES.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            table_count = text.count("<table")
            if not table_count:
                continue
            assert text.count("table-responsive") >= table_count, path.name

    def test_breakpoints_do_not_overlap(self):
        """
        max-width: 1024px sat beside min-width: 1024px, so both branches
        applied at exactly 1024px — iPad landscape. Same at 768px.
        """
        css = _read("static/css/style.css")
        max_widths = set(re.findall(r"@media \(max-width: (\d+)px\)", css))
        min_widths = set(re.findall(r"@media \(min-width: (\d+)px\)", css))

        assert max_widths & min_widths == set(), (
            f"breakpoints used as both max and min: {max_widths & min_widths}"
        )

    def test_image_lightbox_sits_above_the_scrape_tracker(self):
        css = _read("static/css/style.css")
        tokens = dict(re.findall(r"--z-([a-z-]+):\s*(\d+);", css))

        assert int(tokens["image-lightbox"]) > int(tokens["scrape-tracker-sheet"])
        assert int(tokens["dialog"]) > int(tokens["image-lightbox"])

    def test_lightbox_uses_the_token_rather_than_a_raw_value(self):
        css = _read("static/css/style.css")
        lightbox = css[css.index(".image-lightbox {"):]

        assert "z-index: var(--z-image-lightbox);" in lightbox[:400]


class TestAccessibility:
    def test_stylesheets_honour_reduced_motion(self):
        for name in ("style.css", "catalog.css"):
            assert "prefers-reduced-motion" in _read(f"static/css/{name}"), name

    def test_back_to_top_button_has_a_name(self):
        source = _read("templates/base.html")
        start = source.index('id="backToTop"')

        assert 'aria-label="ページ先頭へ戻る"' in source[start - 200:start + 200]

    def test_every_label_points_at_a_control(self):
        """
        ~30 labels had no for= and wrapped no input, so tapping them did not
        focus anything and screen readers announced unlabelled fields.
        """
        orphans = {}
        for path in sorted(TEMPLATES.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"<label\b([^>]*)>(.*?)</label>", text, re.S):
                attributes, inner = match.group(1), match.group(2)
                if "for=" in attributes:
                    continue
                if any(tag in inner for tag in ("<input", "<select", "<textarea")):
                    continue
                label_text = re.sub(r"<[^>]+>", "", inner).strip()[:40]
                orphans.setdefault(path.name, []).append(label_text)

        assert orphans == {}, f"labels bound to nothing: {orphans}"

    def test_tag_remove_button_keeps_a_focus_ring(self):
        css = _read("static/css/style.css")
        start = css.index(".tag-pill-chip-remove:focus-visible")
        rule = css[start:css.index("}", start)]

        assert "outline: none" not in rule
        assert "box-shadow: var(--focus-ring)" in rule

    def test_scrape_tracker_sheet_header_holds_no_nested_buttons(self):
        source = _read("templates/_scrape_tracker.html")
        start = source.index('id="globalScrapeTrackerSheetHeader"')
        header = source[start:source.index("</section>", start)]

        assert 'role="button"' not in header
        assert "<button" in header  # the real buttons are still there

    def test_tracker_announces_through_a_dedicated_status_line(self):
        """
        The whole tracker was aria-live and was rebuilt every 2s with a
        changing "経過 N秒", so screen readers never stopped talking.
        """
        markup = _read("templates/_scrape_tracker.html")
        script = _read("static/js/scrape_tracker.js")

        assert 'id="globalScrapeTrackerStatus"' in markup
        assert 'aria-live="polite"' in markup
        assert markup.count('aria-live="polite"') == 1
        assert "function renderStatusMessage(" in script

    def test_tracker_reuses_cards_instead_of_rebuilding_them(self):
        """Wiping innerHTML every poll stole focus from the 閉じる button."""
        script = _read("static/js/scrape_tracker.js")

        assert "function syncCards(" in script
        assert "function updateCard(" in script
        assert 'listEl.innerHTML = ""' not in script
        assert 'mobileListEl.innerHTML = ""' not in script


class TestDestructiveActionsUseTheAppDialog:
    """
    Four templates still used the browser's confirm(), so the same delete
    confirmation looked different depending on which screen you were on.
    """

    def test_no_template_uses_the_native_confirm(self):
        for path in sorted(TEMPLATES.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            assert "confirm('" not in text, path.name
            assert 'confirm("' not in text, path.name

    def test_delete_confirmations_name_what_is_being_deleted(self):
        # "削除しますか？" gave no clue which rule was about to go.
        source = _read("templates/pricing.html")

        assert "data-confirm-message" in source
        assert "{{ rule.name }}" in source
