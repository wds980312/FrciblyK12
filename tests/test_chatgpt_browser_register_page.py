from __future__ import annotations

from platforms.chatgpt.browser_register import _new_page_for_registration


class FakeContext:
    def __init__(self):
        self.page = object()

    def new_page(self):
        return self.page


class FakeBrowser:
    def __init__(self):
        self.context_kwargs = None
        self.context = FakeContext()

    def new_page(self):
        raise RuntimeError(
            "Browser.new_page: Protocol error (Browser.setDefaultViewport): "
            'Found property "<root>.viewport.isMobile" - false which is not described in this scheme'
        )

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self.context


def test_new_page_for_registration_falls_back_when_viewport_schema_rejects_is_mobile():
    browser = FakeBrowser()

    page = _new_page_for_registration(browser)

    assert page is browser.context.page
    assert browser.context_kwargs == {"no_viewport": True}
