"""Contracts shared by the desktop and phone bookmark experiences.

The bookmark routes are intentionally exercised with an in-memory-on-disk
manager rooted in pytest's temporary directory.  The phone page assertions
are source-level contracts for the static page: they keep the mobile entry
point wired to the same bookmark API while allowing the implementation to
choose its DOM layout.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest


class _BodyElementParser(HTMLParser):
    """Collect body elements and their visible descendant text.

    This avoids asserting exact whitespace or a particular nesting shape in
    the phone page.  Script/style contents are deliberately excluded because
    a comment or a string literal is not a visible mobile control.
    """

    _IGNORED = {"script", "style"}
    _VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[dict[str, object]] = []
        self._stack: list[dict[str, object]] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._IGNORED:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        node: dict[str, object] = {
            "tag": tag,
            "attrs": dict(attrs),
            "text": [],
        }
        if tag in self._VOID:
            self.elements.append(node)
            return
        self._stack.append(node)

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        for node in self._stack:
            node["text"].append(data)  # type: ignore[union-attr]

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth or not self._stack:
            return
        # phone.html is authored as valid HTML, but tolerate an omitted
        # closing tag when collecting a contract element.
        node = self._stack.pop()
        node["text"] = "".join(node["text"]).strip()  # type: ignore[arg-type]
        self.elements.append(node)


def _phone_page() -> str:
    return (
        Path(__file__).resolve().parents[1] / "static" / "phone.html"
    ).read_text(encoding="utf-8")


def _named_js_function_body(page: str, name: str) -> str:
    """Return one named JS function body without depending on line numbers.

    The small scanner ignores strings and comments while balancing braces,
    which keeps ordering assertions local to the function even when the
    implementation contains nested ``if``/``try`` blocks.
    """

    declaration = re.search(
        rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
        page,
        re.IGNORECASE,
    )
    assert declaration, f"phone page must define {name}()"
    open_brace = page.find("{", declaration.start(), declaration.end())
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    i = open_brace
    while i < len(page):
        char = page[i]
        nxt = page[i + 1] if i + 1 < len(page) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            i += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if char == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return page[open_brace + 1:i]
        i += 1
    raise AssertionError(f"unterminated {name}() body")


def _bookmark_script_region(page: str) -> str:
    start_marker = "// ── Bookmark library"
    end_marker = "// ── Invalid capability-link gate"
    start = page.find(start_marker)
    end = page.find(end_marker, start + len(start_marker))
    assert start >= 0 and end > start, "phone bookmark implementation markers are missing"
    return page[start:end]


@pytest.fixture
def isolated_bookmark_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Build a tiny bookmark app rooted in pytest-owned storage only."""

    from fastapi import FastAPI
    from services import bookmarks as bookmark_service

    bookmark_file = tmp_path / "bookmarks.json"
    monkeypatch.setattr(bookmark_service, "BOOKMARKS_FILE", bookmark_file)

    from services.bookmarks import BookmarkManager

    manager = BookmarkManager()
    from api import bookmarks as bookmarks_api

    monkeypatch.setattr(bookmarks_api, "_bm", lambda: manager)
    test_app = FastAPI()
    test_app.include_router(bookmarks_api.router)
    return manager, bookmark_file, test_app


@pytest.fixture
async def bookmarks_client(isolated_bookmark_manager):
    _, _, test_app = isolated_bookmark_manager
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_bookmark_list_returns_categories_and_groupable_records(
    bookmarks_client: httpx.AsyncClient,
    isolated_bookmark_manager,
):
    manager, bookmark_file, _ = isolated_bookmark_manager
    category = manager.create_category("工作", "#4dd28a")

    created = await bookmarks_client.post(
        "/api/bookmarks",
        json={
            "name": "辦公室",
            "lat": 25.0330,
            "lng": 121.5654,
            "address": "台北市信義區",
            "category_id": category.id,
            "country_code": "tw",
        },
    )

    assert created.status_code == 200, created.text
    created_body = created.json()
    assert created_body["category_id"] == category.id
    assert created_body["country_code"] == "tw"

    listed = await bookmarks_client.get("/api/bookmarks")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert {cat["id"] for cat in payload["categories"]} >= {"default", category.id}
    assert next(b for b in payload["bookmarks"] if b["id"] == created_body["id"]) == created_body
    assert manager.list_bookmarks()[0].name == "辦公室"
    assert bookmark_file.exists()


async def test_bookmark_put_preserves_full_payload_fields_and_created_metadata(
    bookmarks_client: httpx.AsyncClient,
    isolated_bookmark_manager,
):
    manager, _, _ = isolated_bookmark_manager
    category = manager.create_category("旅遊")
    created = await bookmarks_client.post(
        "/api/bookmarks",
        json={
            "name": "舊名稱",
            "lat": 24.0,
            "lng": 120.0,
            "address": "舊地址",
            "category_id": "default",
            "country_code": "tw",
        },
    )
    assert created.status_code == 200, created.text
    original = created.json()

    updated = await bookmarks_client.put(
        f"/api/bookmarks/{original['id']}",
        json={
            "id": original["id"],
            "name": "新名稱",
            "lat": 35.6812,
            "lng": 139.7671,
            "address": "東京都千代田區",
            "category_id": category.id,
            "country_code": "jp",
            # The API should update editable fields only; clients may send
            # these response fields back as part of a full Bookmark object.
            "created_at": "client-must-not-replace",
            "last_used_at": "client-must-not-replace",
        },
    )

    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["id"] == original["id"]
    assert body["name"] == "新名稱"
    assert body["lat"] == pytest.approx(35.6812)
    assert body["lng"] == pytest.approx(139.7671)
    assert body["address"] == "東京都千代田區"
    assert body["category_id"] == category.id
    assert body["country_code"] == "jp"
    assert body["created_at"] == original["created_at"]
    assert body["last_used_at"] == original["last_used_at"]
    assert manager.list_bookmarks()[0].address == "東京都千代田區"


async def test_bookmark_delete_and_missing_update_delete_return_expected_status(
    bookmarks_client: httpx.AsyncClient,
    isolated_bookmark_manager,
):
    created = await bookmarks_client.post(
        "/api/bookmarks",
        json={"name": "暫存點", "lat": 25.0, "lng": 121.0},
    )
    assert created.status_code == 200, created.text
    bookmark_id = created.json()["id"]

    deleted = await bookmarks_client.delete(f"/api/bookmarks/{bookmark_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted"}

    assert (
        await bookmarks_client.put(
            f"/api/bookmarks/{bookmark_id}",
            json={"name": "不存在", "lat": 0, "lng": 0},
        )
    ).status_code == 404
    assert (
        await bookmarks_client.delete(f"/api/bookmarks/{bookmark_id}")
    ).status_code == 404


def test_phone_page_exposes_a_visible_bookmark_entry_and_modal():
    page = _phone_page()
    parser = _BodyElementParser()
    parser.feed(page)

    bookmark_entry = [
        node
        for node in parser.elements
        if "收藏" in str(node["text"])
        and re.search(
            r"(?:bookmark|favorite|收藏)",
            str(node["attrs"].get("data-kind", "")),
            re.IGNORECASE,
        )
    ]
    assert bookmark_entry, "the phone action menu must expose 收藏"
    assert any(node["tag"] == "button" for node in bookmark_entry)

    bookmark_modal = [
        node
        for node in parser.elements
        if re.search(
            r"(?:bookmark|favorite|收藏)",
            " ".join(
                [
                    str(node["attrs"].get("id", "")),
                    str(node["attrs"].get("class", "")),
                    str(node["attrs"].get("aria-label", "")),
                ]
            ),
            re.IGNORECASE,
        )
        and (
            "modal" in str(node["attrs"].get("class", "")).lower()
            or str(node["attrs"].get("role", "")).lower() == "dialog"
        )
    ]
    assert bookmark_modal, "the phone page needs a dedicated bookmark dialog/panel"

    dialog_ids = {
        str(node["attrs"].get("id", ""))
        for node in parser.elements
        if str(node["attrs"].get("role", "")).lower() == "dialog"
        and str(node["attrs"].get("aria-modal", "")).lower() == "true"
        and node["attrs"].get("aria-labelledby")
    }
    assert {
        "bookmarks-modal",
        "bookmark-action-modal",
        "bookmark-form-modal",
    } <= dialog_ids


def test_phone_bookmark_source_uses_shared_crud_api_and_phone_actions():
    page = _phone_page()
    source = page.lower()

    # Keep method assertions scoped to the bookmark implementation.  The
    # page already has POST calls for teleport/navigate, so a page-wide
    # method search would not prove the bookmark CRUD path exists.
    section_match = re.search(
        r"(?://\s*[─-]{2,}\s*bookmarks?|function\s+openBookmarks\s*\()"
        r"(?P<body>[\s\S]*?)(?://\s*[─-]{2,}\s*(?:search|invalid|boot)|</script>)",
        page,
        re.IGNORECASE,
    )
    assert section_match, "phone page must contain a bounded bookmark implementation"
    bookmark_source = section_match.group("body")
    assert re.search(r"[\"'`]\/api\/bookmarks(?:[\"'`/?$])", bookmark_source)
    for method in ("POST", "PUT"):
        assert re.search(rf"method\s*:\s*[\"']{method}[\"']", bookmark_source, re.IGNORECASE), (
            f"mobile bookmark flow must support {method}"
        )
    # Delete confirmation is shared with the general action sheet and may
    # therefore live just before the bounded bookmark-library section.
    assert re.search(
        r"\/api\/bookmarks\/[^\n]{0,240}method\s*:\s*[\"']DELETE[\"']",
        page,
        re.IGNORECASE,
    ), "mobile bookmark flow must support DELETE"

    # The bookmark click path must feed both existing confirmed actions with
    # the saved coordinate, rather than merely displaying a text list.
    action_hits = []
    for match in re.finditer(r"openSheet\s*\(", page, re.IGNORECASE):
        context = page[max(0, match.start() - 1800): match.start() + 1800]
        if re.search(r"bookmark|favorite|收藏", context, re.IGNORECASE):
            action_hits.append(context)
    assert action_hits, "bookmark interactions must use the existing action sheet"
    action_context = "\n".join(action_hits)
    assert re.search(r"openSheet\s*\(\s*[\"']teleport[\"']", action_context)
    assert re.search(r"openSheet\s*\(\s*[\"']navigate[\"']", action_context)
    assert re.search(r"\.(?:lat|lng)\b|\[['\"](?:lat|lng)['\"]\]", action_context)

    # A bookmark form must validate coordinate bounds.  Accept reuse of the
    # page's existing parseCoord helper or an inline Number.isFinite check.
    assert re.search(r"Number\.isFinite", bookmark_source)
    assert re.search(r"-90[\s\S]{0,100}90", bookmark_source)
    assert re.search(r"-180[\s\S]{0,100}180", bookmark_source)

    # Keep the shared API and phone action references in the actual script,
    # not just a visible label/comment.
    assert "/api/phone/teleport" in source
    assert "/api/phone/navigate" in source


def test_phone_bookmark_rendering_uses_text_nodes_for_user_values():
    page = _phone_page()
    bookmark_source = _bookmark_script_region(page)
    assert not re.search(
        r"\.(?:innerHTML|outerHTML)\s*=|\.insertAdjacentHTML\s*\(|document\.write\s*\(",
        bookmark_source,
        re.IGNORECASE,
    ), "bookmark code must not use HTML parsing sinks for persisted values"
    renderer_matches = list(
        re.finditer(
            r"(?:function\s+|(?:const|let|var)\s+)"
            r"([A-Za-z_$][\w$]*(?:bookmark|favorite)[\w$]*)"
            r"[^\{]*\{",
            page,
            re.IGNORECASE,
        )
    )
    assert renderer_matches, "bookmark rendering should have a named function"

    checked = False
    for match in renderer_matches:
        body = page[match.start(): match.start() + 9000]
        if not re.search(r"\.textContent\s*=", body):
            continue
        checked = True
        # Clearing a list with innerHTML is harmless; interpolating bookmark
        # fields into an HTML template is not.  Require the latter never to
        # happen in a bookmark renderer.
        assert not re.search(
            r"\.innerHTML\s*(?:\+?=)\s*`[^`]*(?:bookmark|favorite|(?:bm|item|mark)\.|\bname\b|\baddress\b|\blat\b|\blng\b)",
            body,
            re.IGNORECASE | re.DOTALL,
        ), "bookmark values must not be interpolated into innerHTML"
    assert checked, "bookmark renderer must assign user-visible values with textContent"


def test_bookmark_preview_closes_library_before_centering_map():
    page = _phone_page()
    body = _named_js_function_body(page, "openBookmarkPreview")

    close_index = body.find("bookmarksModal.classList.remove('open')")
    center_index = body.find("map.setView(")
    assert close_index >= 0, "preview must close the bookmarks modal"
    assert center_index >= 0, "preview must center the map"
    assert close_index < center_index, (
        "preview must close the library before changing map center"
    )


def test_successful_bookmark_submit_clears_busy_before_closing_form():
    page = _phone_page()
    body = _named_js_function_body(page, "submitBookmarkForm")

    clear_index = body.find("bookmarkFormBusy = false")
    close_index = body.find("closeBookmarkForm()")
    assert clear_index >= 0, "successful submit must clear bookmark form busy state"
    assert close_index >= 0, "successful submit must close the bookmark form"
    assert clear_index < close_index, (
        "closeBookmarkForm is guarded by busy state and must run after it clears"
    )
    assert re.search(r"saved\s*=\s*true", body)
    assert re.search(r"if\s*\(\s*saved\s*\)\s*closeBookmarkForm\s*\(\)", body)


def test_blank_bookmark_coordinates_are_rejected_before_numeric_conversion():
    page = _phone_page()
    body = _named_js_function_body(page, "submitBookmarkForm")

    assert re.search(
        r"latRaw\s*=\s*bookmarkLatInput\.value\.trim\(\)",
        body,
    )
    assert re.search(
        r"lngRaw\s*=\s*bookmarkLngInput\.value\.trim\(\)",
        body,
    )
    assert re.search(r"if\s*\(\s*!latRaw\s*\|\|\s*!Number\.isFinite\(lat\)", body)
    assert re.search(r"if\s*\(\s*!lngRaw\s*\|\|\s*!Number\.isFinite\(lng\)", body)


def test_confirmed_bookmark_delete_reloads_then_reopens_library_after_sheet_closes():
    page = _phone_page()
    handler_start = page.find("sheetOk.addEventListener('click'")
    handler_end = page.find("// ── Search modal", handler_start)
    assert handler_start >= 0 and handler_end > handler_start
    body = page[handler_start:handler_end]

    delete_index = body.find("/api/bookmarks/")
    reload_match = re.search(r"await\s+loadBookmarks\(\s*true\s*\)", body[delete_index:])
    reload_index = delete_index + reload_match.start() if reload_match else -1
    close_sheet_index = body.find("closeSheet()", reload_index)
    reopen_index = body.find("bookmarksModal.classList.add('open')", close_sheet_index)
    assert delete_index >= 0
    assert reload_index > delete_index, "delete must refresh shared bookmark data"
    assert close_sheet_index > reload_index, "sheet must close after delete refresh"
    assert reopen_index > close_sheet_index, (
        "the bookmark library must reopen after confirmed delete completes"
    )
    assert body.find("bookmarkSearchInput.focus()", reopen_index) > reopen_index


def test_cancelled_bookmark_action_returns_to_library():
    page = _phone_page()
    handler_start = page.find("sheetCancel.addEventListener('click'")
    handler_end = page.find("sheetOk.addEventListener('click'", handler_start)
    assert handler_start >= 0 and handler_end > handler_start
    body = page[handler_start:handler_end]

    assert re.search(r"pendingAction\s*&&\s*pendingAction\.bookmark", body)
    close_index = body.find("closeSheet()")
    reopen_index = body.find("bookmarksModal.classList.add('open')", close_index)
    assert close_index >= 0
    assert reopen_index > close_index


def test_confirm_sheet_cancel_is_disabled_while_delete_is_in_flight():
    page = _phone_page()
    handler_start = page.find("sheetOk.addEventListener('click'")
    handler_end = page.find("// ── Search modal", handler_start)
    assert handler_start >= 0 and handler_end > handler_start
    body = page[handler_start:handler_end]

    disable_index = body.find("sheetCancel.disabled = true")
    delete_index = body.find("/api/bookmarks/")
    enable_index = body.find("sheetCancel.disabled = false", delete_index)
    assert 0 <= disable_index < delete_index < enable_index


def test_failed_bookmark_actions_restore_focus_inside_the_open_dialog():
    page = _phone_page()
    sheet_start = page.find("sheetOk.addEventListener('click'")
    sheet_end = page.find("// ── Search modal", sheet_start)
    assert sheet_start >= 0 and sheet_end > sheet_start
    sheet_handler = page[sheet_start:sheet_end]

    assert "let actionFailed = false" in sheet_handler
    assert "actionFailed = true" in sheet_handler
    assert "sheet.classList.contains('open')" in sheet_handler
    assert "gate.style.display === 'none'" in sheet_handler
    assert "sheetOk.focus()" in sheet_handler

    form_handler = _named_js_function_body(page, "submitBookmarkForm")
    assert "bookmarkFormModal.classList.contains('open')" in form_handler
    assert "gate.style.display === 'none'" in form_handler
    assert "bookmarkFormSubmit.focus()" in form_handler


def test_bookmark_dialogs_move_focus_in_and_restore_it_on_close():
    page = _phone_page()
    open_action = _named_js_function_body(page, "openBookmarkAction")
    close_action = _named_js_function_body(page, "closeBookmarkAction")
    open_library = _named_js_function_body(page, "openBookmarks")

    assert "bookmarkActionReturnFocus = document.activeElement" in open_action
    assert re.search(r"bookmark-preview['\"]\)\.focus\(\)", open_action)
    assert "bookmarkActionReturnFocus.focus()" in close_action
    focus_index = open_library.find("bookmarkSearchInput.focus()")
    load_index = open_library.find("await loadBookmarks()")
    assert focus_index >= 0 and load_index > focus_index


def test_bookmark_nested_dialogs_cover_library_and_confirmations_receive_focus():
    page = _phone_page()
    cover = _named_js_function_body(page, "setBookmarkLibraryCovered")
    open_action = _named_js_function_body(page, "openBookmarkAction")
    close_action = _named_js_function_body(page, "closeBookmarkAction")
    open_form = _named_js_function_body(page, "openBookmarkForm")
    close_form = _named_js_function_body(page, "closeBookmarkForm")
    open_sheet = _named_js_function_body(page, "openSheet")
    preview = _named_js_function_body(page, "openBookmarkPreview")

    assert "bookmarksModal.inert = covered" in cover
    assert "aria-hidden" in cover
    assert "setBookmarkLibraryCovered(true)" in open_action
    assert "setBookmarkLibraryCovered(false)" in close_action
    assert "setBookmarkLibraryCovered(true)" in open_form
    assert "setBookmarkLibraryCovered(false)" in close_form
    assert "sheetCancel.focus()" in open_sheet
    assert "fab.focus()" in preview
    assert "setRemoteBackgroundInert(true)" in _named_js_function_body(page, "openBookmarks")


def test_bookmark_dialog_keyboard_and_sheet_accessibility_contracts():
    page = _phone_page()
    open_sheet = _named_js_function_body(page, "openSheet")
    close_sheet = _named_js_function_body(page, "closeSheet")

    assert re.search(r'id="fab"[^>]+aria-controls="actions"[^>]+aria-expanded="false"', page)
    assert "fab.setAttribute('aria-expanded'" in page
    assert re.search(r'id="sheet"[^>]+aria-hidden="true"[^>]+inert', page)
    assert "sheet.inert = false" in open_sheet
    assert "sheet.removeAttribute('aria-hidden')" in open_sheet
    assert "sheet.inert = true" in close_sheet
    assert "sheet.setAttribute('aria-hidden', 'true')" in close_sheet
    assert "e.key === 'Escape'" in page
    assert "e.key !== 'Tab'" in page
    assert "activeDialog.querySelectorAll" in page
    assert "setRemoteBackgroundInert" in page
    set_open = _named_js_function_body(page, "setOpen")
    assert "document.activeElement === fab" in set_open
    assert "firstAction.focus()" in set_open
    teleport = _named_js_function_body(page, "openBookmarkTeleport")
    navigate = _named_js_function_body(page, "openBookmarkNavigate")
    assert re.search(r"openSheet\(\s*['\"]teleport['\"][^;]+bookmark\s*\)", teleport)
    assert re.search(r"openSheet\(\s*['\"]navigate['\"][^;]+bookmark\s*\)", navigate)


def test_invalid_capability_gate_inerts_entire_phone_app_for_keyboard_users():
    page = _phone_page()
    show_gate = _named_js_function_body(page, "showGate")
    hide_gate = _named_js_function_body(page, "hideGate")

    assert 'id="phone-app"' in page
    assert page.find('</div><!-- /#phone-app -->') < page.find('id="gate"')
    assert re.search(r'id="gate"[^>]+role="dialog"[^>]+aria-modal="true"', page)
    assert "phoneApp.inert = true" in show_gate
    assert "gate.focus()" in show_gate
    assert "phoneApp.inert = false" in hide_gate
    assert "activeDialog === gate" in page


def test_bookmark_search_has_label_and_list_uses_compact_status_region():
    page = _phone_page()

    assert re.search(r'<label[^>]+for="bookmark-search-input"[^>]*>\s*搜尋收藏\s*</label>', page)
    assert re.search(r'id="bookmark-status"[^>]+role="status"', page)
    list_tag = re.search(r'<div[^>]+id="bookmark-list"[^>]*>', page)
    assert list_tag and "aria-live" not in list_tag.group(0)
    assert 'aria-busy="false"' in list_tag.group(0)


def test_bookmark_loading_queues_required_refresh_and_disables_mutating_controls():
    page = _phone_page()
    load = _named_js_function_body(page, "loadBookmarks")
    render = _named_js_function_body(page, "renderBookmarkList")
    submit = _named_js_function_body(page, "submitBookmarkForm")

    assert "if (bookmarkLoadPromise)" in load
    assert "bookmarkReloadRequested = true" in load
    assert "return bookmarkLoadPromise" in load
    assert re.search(r"while\s*\(\s*bookmarkReloadRequested\s*\)", load)
    assert "bookmarkAdd.disabled = bookmarkBusy" in render
    assert re.search(r"await\s+loadBookmarks\(\s*true\s*\)", submit)


def test_bookmark_overlays_render_above_fab_and_keep_toasts_visible():
    page = _phone_page()

    assert re.search(
        r"#bookmarks-modal\s*,\s*#bookmark-action-modal\s*,\s*#bookmark-form-modal\s*\{\s*z-index:\s*5200",
        page,
    )
    toast_rule = re.search(r"#toast\s*\{(?P<body>.*?)\}", page, re.DOTALL)
    assert toast_rule and re.search(r"z-index:\s*5300", toast_rule.group("body"))
    gate_rule = re.search(r"#gate\s*\{(?P<body>.*?)\}", page, re.DOTALL)
    assert gate_rule and re.search(r"z-index:\s*6000", gate_rule.group("body"))


def test_mobile_category_collapse_uses_private_local_storage_not_ui_state_api():
    page = _phone_page()
    bookmark_source = _bookmark_script_region(page)

    assert "/api/bookmarks/ui-state" not in page
    get_keys = set(
        re.findall(
            r"localStorage\.getItem\(\s*['\"]([^'\"]+)['\"]",
            bookmark_source,
        )
    )
    set_keys = set(
        re.findall(
            r"localStorage\.setItem\(\s*['\"]([^'\"]+)['\"]",
            bookmark_source,
        )
    )
    shared_keys = get_keys & set_keys
    assert any(
        re.search(r"(?:phone|mobile).*(?:bookmark|favorite)", key, re.IGNORECASE)
        for key in shared_keys
    ), "mobile collapse state must round-trip through its own localStorage key"

    # The key is used by the category toggle, not merely initialized and
    # persisted without any UI state transition.
    assert re.search(r"bookmarkCollapsed\.(?:add|delete)\(", bookmark_source)
    assert "saveBookmarkCollapsed()" in bookmark_source


def test_keyboard_category_toggle_restores_focus_after_list_rerender():
    page = _phone_page()
    render = _named_js_function_body(page, "renderBookmarkList")

    assert "heading.dataset.categoryId = groupId" in render
    assert "document.activeElement === heading" in render
    assert "el.dataset.categoryId === groupId" in render
    assert "replacement.focus()" in render
