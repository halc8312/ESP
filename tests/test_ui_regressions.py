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


class TestArchiveGivesFeedback:
    """
    Archiving with nothing selected redirected in silence while the trash
    screen flashed a message for the same situation, so the button looked
    broken. The error path also put the raw exception text on screen.
    """

    def test_empty_selection_explains_itself(self, client, db_session):
        _login(client, db_session, "archive_feedback_user")

        response = client.post("/archive/restore", data={}, follow_redirects=True)
        html = response.get_data(as_text=True)

        assert "商品を選択してください" in html

    def test_archiving_nothing_explains_itself(self, client, db_session):
        _login(client, db_session, "archive_add_feedback_user")

        response = client.post("/archive/add", data={}, follow_redirects=True)
        html = response.get_data(as_text=True)

        assert "商品を選択してください" in html

    def test_exceptions_are_not_shown_verbatim(self):
        source = _read("routes/archive.py")

        assert "f'エラー: {e}'" not in source
        assert "current_app.logger.exception" in source


class TestUnknownPriceReadsTheSame:
    """archive.html rendered a missing price as ¥0, i.e. free."""

    def test_neither_screen_invents_a_zero(self):
        for name in ("archive.html", "trash.html"):
            source = _read(f"templates/{name}")
            assert "last_price or 0" not in source, name
            assert 'import "_money.html"' in source, name


class TestRegisterMatchesLogin:
    """
    Registration signs the account in immediately and there is no password
    reset flow, so a mistyped password locks the account at the next login.
    """

    def test_confirmation_field_is_offered(self, client):
        html = client.get("/register").get_data(as_text=True)

        assert 'name="password_confirm"' in html

    def test_password_can_be_revealed(self, client):
        html = client.get("/register").get_data(as_text=True)

        assert "data-toggle-password" in html
        assert "js/app_ui.js" in html

    def test_errors_are_announced(self, client):
        html = client.get("/register").get_data(as_text=True)
        # login.html already had this; register.html did not.
        assert 'role="alert"' in _read("templates/register.html")
        assert "autofocus" in html

    def test_mismatched_confirmation_is_rejected(self, client, db_session):
        from models import User

        response = client.post("/register", data={
            "username": "mismatchuser",
            "password": "correcthorse12",
            "password_confirm": "correcthorse13",
        })

        assert response.status_code == 400
        assert "パスワードが一致しません" in response.get_data(as_text=True)
        assert db_session.query(User).filter_by(username="mismatchuser").first() is None

    def test_username_survives_a_rejected_attempt(self, client, db_session):
        response = client.post("/register", data={
            "username": "keptname",
            "password": "short",
            "password_confirm": "short",
        })

        assert 'value="keptname"' in response.get_data(as_text=True)

    def test_matching_confirmation_still_registers(self, client, db_session):
        from models import User

        client.post("/register", data={
            "username": "confirmeduser",
            "password": "correcthorse12",
            "password_confirm": "correcthorse12",
        }, follow_redirects=True)

        assert db_session.query(User).filter_by(username="confirmeduser").first() is not None


class TestCdnScriptsAreVerifiedAndAnnounced:
    """
    None of the CDN tags carried an integrity hash, and two of the three
    libraries failed silently — the page just did less than it should.
    """

    CDN_TEMPLATES = (
        "templates/product_detail.html",
        "templates/pricelist_edit.html",
        "templates/manage_templates.html",
        "templates/pricelist_analytics.html",
    )

    def test_every_cdn_script_carries_an_integrity_hash(self):
        pattern = re.compile(r"<script[^>]*src=\"https://[^\"]+\"[^>]*>", re.S)
        for relative_path in self.CDN_TEMPLATES:
            source = _read(relative_path)
            for tag in pattern.findall(source):
                assert "integrity=" in tag, f"{relative_path}: {tag[:90]}"
                assert "crossorigin=" in tag, f"{relative_path}: {tag[:90]}"

    def test_chart_failure_is_reported(self):
        source = _read("templates/pricelist_analytics.html")

        assert "typeof Chart === 'undefined'" in source
        assert "グラフを読み込めませんでした" in source

    def test_drag_sort_failure_is_reported(self):
        source = _read("templates/product_detail.html")

        assert "typeof Sortable === 'undefined'" in source
        assert "ドラッグ並べ替えを読み込めませんでした" in source


class TestBusyButtonsRecover:
    """
    Disabling the submitter inside the submit handler drops its name/value
    from the payload, and a bfcache restore left the button stuck reading
    "処理中..." forever.
    """

    def test_submit_disables_the_button_after_the_payload_is_built(self):
        source = _read("static/js/app_ui.js")

        assert "deferDisable" in source
        assert source.count("{ deferDisable: true }") == 2

    def test_returning_to_a_cached_page_clears_the_busy_state(self):
        source = _read("static/js/app_ui.js")

        assert 'window.addEventListener("pageshow"' in source
        assert "restoreBusyButtons()" in source
        assert "hideLoading()" in source


class TestErrorPageOffersAWayOut:
    """The only link was a hardcoded href="/"."""

    def test_error_page_links_are_generated(self):
        source = _read("templates/error.html")

        assert 'href="/"' not in source
        assert "url_for('index')" in source

    def test_error_page_offers_more_than_one_destination(self, client):
        html = client.get("/no-such-page-here").get_data(as_text=True)

        assert "前のページへ戻る" in html
        assert "商品一覧へ" in html
        assert "はじめての方へ" in html


class TestCatalogAnnouncesItself:
    """
    The public catalog's filter count changed silently, and its theme button
    kept a generic aria-label that overrode the title the script updated.
    """

    def test_result_count_is_announced(self):
        source = _read("templates/catalog.html")
        start = source.index('id="resultsSummary"')

        assert 'aria-live="polite"' in source[start - 120:start + 120]

    def test_theme_button_label_tracks_its_state(self):
        source = _read("templates/catalog.html")

        # aria-label wins over title, so the script must update both.
        assert "toggleBtn.setAttribute('aria-label', nextLabel)" in source
        assert "aria-pressed" in source


class TestCatalogPriceFilterFollowsCurrency:
    """
    Min/Max always compared against the raw yen price. A viewer looking at
    $45 who typed 50 got everything under ¥50 — an empty catalog — with no
    hint that the box wanted yen.
    """

    def test_filter_converts_the_typed_amount(self):
        source = _read("templates/catalog.html")

        assert "function toJpyFilterValue(" in source
        assert "toJpyFilterValue(minPriceRaw)" in source
        assert "toJpyFilterValue(maxPriceRaw)" in source

    def test_heading_shows_the_active_currency(self):
        source = _read("templates/catalog.html")

        assert "function syncPriceFilterHeading(" in source
        assert "'Price (' + currency + ')'" in source

    def test_input_labels_follow_the_currency_too(self):
        """
        The heading switched to Price (USD) while the inputs' aria-labels
        still said "in Japanese yen", so the screen reader disagreed with
        the screen.
        """
        source = _read("templates/catalog.html")

        assert "in Japanese yen" not in source
        assert "' price in ' + currency" in source

    def test_switching_currency_refilters(self):
        source = _read("templates/catalog.html")
        start = source.index("function switchCurrency(")
        body = source[start:source.index("function formatPriceFromJpy(", start)]

        assert "syncPriceFilterHeading();" in body
        assert "filterProducts();" in body

    def test_currency_choice_is_remembered(self):
        source = _read("templates/catalog.html")

        assert "CURRENCY_STORAGE_KEY" in source
        assert "storeCurrency(event.target.value)" in source
        # A stored pick must beat the geo guess.
        assert "offered.indexOf(stored) !== -1 ? stored : detectViewerCurrency()" in source

    def test_rate_badge_uses_one_format(self):
        source = _read("templates/catalog.html")

        # "Rate: 1 USD = ..." vs "1 USD = ..." depending on which path ran.
        assert "Rate: 1 " not in source
        assert source.count("1 USD = ¥$") == 3

    def test_japanese_fallback_titles_are_marked(self):
        source = _read("templates/catalog.html")

        assert '{% if not item.title_en %} lang="ja"{% endif %}' in source
        assert 'id="modalTitle" class="modal-product-title" lang="ja"' in source


class TestSemanticColoursComeFromTokens:
    """
    The same "this is dangerous" red was written four different ways across
    the templates, so a destructive action looked different per screen.
    """

    SEMANTIC_TOKENS = (
        "--success-surface", "--success-border", "--success-text",
        "--warning-surface", "--warning-border", "--warning-text",
        "--danger-surface", "--danger-border", "--danger-text",
    )

    # Retired literals, mapped onto the tokens above.
    RETIRED = (
        "#c62828", "#b91c1c", "#991b1b", "#ef4444", "#fce4ec", "#ef9a9a",
        "#fecaca", "#ffebee", "#fee2e2", "#2e7d32", "#166534", "#bbf7d0",
        "#ecfdf3", "#e8f5e9", "#a5d6a7", "#81c784", "#9a6700", "#e65100",
        "#fff8e1", "#ffe08a", "#fff8e8", "#f7d58b", "#22c55e",
    )

    def test_tokens_are_defined(self):
        css = _read("static/css/style.css")

        for token in self.SEMANTIC_TOKENS:
            assert f"{token}:" in css, token

    def test_no_template_still_spells_a_status_colour_by_hand(self):
        offenders = {}
        for path in sorted(TEMPLATES.glob("*.html")):
            text = path.read_text(encoding="utf-8").lower()
            found = [literal for literal in self.RETIRED if literal in text]
            if found:
                offenders[path.name] = found

        assert offenders == {}, f"semantic colours written as literals: {offenders}"


class TestHardcodedColourRatchet:
    """
    164 decorative literals remain and replacing them wholesale would be an
    unreviewable diff. This does not require fixing them — it stops new ones
    arriving, so the number can only go down.
    """

    # Per-file ceilings. Lower a number when you tokenise; never raise one.
    CEILINGS = {
        "archive.html": 2,
        "import.html": 9,
        "pricelist_add_products.html": 4,
        "pricelist_analytics.html": 16,
        "pricelist_edit.html": 26,
        "pricelist_items.html": 9,
        "pricing.html": 2,
        "product_detail.html": 90,
        "scrape_result.html": 2,
        "trash.html": 4,
    }

    @staticmethod
    def _count(path):
        return len(re.findall(r"#[0-9a-fA-F]{3,8}\b", path.read_text(encoding="utf-8")))

    def test_no_file_gains_hardcoded_colours(self):
        regressions = {}
        for path in sorted(TEMPLATES.glob("*.html")):
            count = self._count(path)
            ceiling = self.CEILINGS.get(path.name, 0)
            if count > ceiling:
                regressions[path.name] = f"{count} > {ceiling}"

        assert regressions == {}, (
            "hardcoded colours increased — use a token from :root instead: "
            f"{regressions}"
        )

    def test_ceilings_do_not_drift_above_reality(self):
        """A ceiling left too high after cleanup stops catching regressions."""
        stale = {}
        for name, ceiling in self.CEILINGS.items():
            actual = self._count(TEMPLATES / name)
            if actual < ceiling:
                stale[name] = f"ceiling {ceiling}, actually {actual}"

        assert stale == {}, f"lower these ceilings to lock in the cleanup: {stale}"
