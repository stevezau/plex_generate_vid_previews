"""
Static guards on the JS files loaded by the dashboard.

These regressions exist because SocketIO `connect`/`disconnect` events
fire on every page (settings, servers, automation, logs, bif-viewer),
not only the dashboard. The shared loader functions called from those
handlers — ``loadJobs``/``loadJobStats``/``loadWorkerStatuses`` — must
bail when their target DOM is absent, otherwise reconnect throws
``TypeError: Cannot set properties of null`` and the user sees a noisy
stack trace in the console.

Pure string-match guards rather than a JS test runner because the
project has no JS test infra yet — fast, and the bug shape (null deref
on a known element ID) is exactly what these tests catch.

Guards run against the concatenated text of every JS file under
``web/static/js/`` so a future split (e.g. notifications.js, logs_modal.js)
keeps the protected functions reachable regardless of which module owns
them. Adding a new module to this directory automatically extends the
search; no test edit needed.
"""

from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parent.parent / "media_preview_generator" / "web" / "static" / "js"


@pytest.fixture(scope="module")
def app_js() -> str:
    """Concatenated text of every JS file under web/static/js/.

    The fixture name stays ``app_js`` for backwards-compatibility with the
    earlier single-file world; new tests that don't need the catch-all
    behaviour can read individual files directly.
    """
    parts = []
    for path in sorted(JS_DIR.glob("*.js")):
        parts.append(f"// === {path.name} ===")
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


class TestSocketReconnectNullGuards:
    def test_update_job_queue_guards_missing_tbody(self, app_js):
        """``updateJobQueue`` must early-return when ``#jobQueue`` is absent."""
        snippet = app_js.split("function updateJobQueue()", 1)[1].split("\n}", 1)[0]
        assert "if (!tbody)" in snippet, (
            "updateJobQueue must short-circuit when document.getElementById('jobQueue') is null. "
            "Without the guard, SocketIO reconnect from /settings or /servers crashes with "
            "'Cannot set properties of null (setting innerHTML)' on the first tbody.innerHTML write."
        )

    def test_update_worker_statuses_guards_missing_container(self, app_js):
        """``updateWorkerStatuses`` must early-return when ``#workerStatusContainer`` is absent."""
        snippet = app_js.split("function updateWorkerStatuses(", 1)[1].split("\n}", 1)[0]
        assert "if (!container)" in snippet, (
            "updateWorkerStatuses must short-circuit when document.getElementById"
            "('workerStatusContainer') is null. Same reconnect-from-non-dashboard crash as updateJobQueue."
        )

    def test_load_job_stats_guards_missing_stat_elements(self, app_js):
        """``loadJobStats`` must early-return when stat elements are absent."""
        snippet = app_js.split("async function loadJobStats()", 1)[1].split("\n}", 1)[0]
        assert "getElementById('statPending')" in snippet and "return" in snippet, (
            "loadJobStats must short-circuit when document.getElementById('statPending') is null. "
            "Same reconnect-from-non-dashboard crash as updateJobQueue."
        )


class TestNoLiteralScriptTagsInsideTemplateInlineScripts:
    """A literal `<script>` or `</script>` token inside an inline JS string
    or comment terminates the surrounding <script> tag at the embedded
    `</script>` (the HTML parser doesn't know about JS comments) and the
    rest of the script gets parsed as HTML. Browsers report this as
    'Unexpected end of input'.

    Real bug from CI: setup.html had `// "Movies <script>alert(1)</script>"`
    inside a JS comment explaining XSS escaping. The HTML parser closed
    the wizard's <script> at that embedded </script>, the wizard's
    handlers never attached, and the e2e folder_picker test (and every
    wizard test downstream) timed out waiting for #manualPlexUrl to
    become visible after a vendor button click.

    Only checks templates with INLINE <script> blocks (excludes
    src=-only loaders).
    """

    def test_no_literal_script_close_inside_template_inline_scripts(self):
        import re
        from pathlib import Path

        TEMPLATES = Path(__file__).resolve().parent.parent / "media_preview_generator" / "web" / "templates"
        offending = []
        # Capture text between <script ...> and </script> when the tag has
        # no src= attribute (i.e. inline script body).
        inline_re = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
        for tpl in sorted(TEMPLATES.rglob("*.html")):
            text = tpl.read_text(encoding="utf-8")
            for m in inline_re.finditer(text):
                body = m.group(1)
                # Look for either `<script` or `</script` literally inside
                # the inline body (case-insensitive — same as how the
                # HTML parser tokenizes).
                if re.search(r"<\s*/?\s*script", body, re.IGNORECASE):
                    # Pull a snippet for the failure message.
                    bad = re.search(r".{0,40}<\s*/?\s*script.{0,40}", body, re.IGNORECASE)
                    offending.append(
                        f"{tpl.relative_to(TEMPLATES.parent)} contains literal <script> token in inline JS: {bad.group(0).strip()!r}"
                    )

        assert not offending, (
            "Inline <script> blocks must not contain a literal `<script>` or `</script>` token, "
            "even inside a JS string or comment — the HTML parser doesn't know about JS comments "
            "and will terminate the script tag at the embedded </script>, breaking every handler "
            "downstream. Split the literal: 'foo</scr' + 'ipt>bar', or describe it without the "
            "literal token. Offenders:\n  " + "\n  ".join(offending)
        )


class TestServersJsBailsOnNonServersPages:
    """servers.js is loaded on /setup too (for MPGShared exports the wizard
    depends on), so its DOMContentLoaded handler must not unconditionally
    addEventListener on Edit-Server-modal elements that only exist on
    /servers — otherwise the first null-deref throws TypeError and halts
    every later script on the page, including the wizard's vendor-button
    bindings. CI caught this as a folder_picker e2e timeout (the wizard
    couldn't reveal #plexSignInPanel).
    """

    def test_dom_content_loaded_short_circuits_when_modal_absent(self):
        from pathlib import Path

        servers_js = (
            Path(__file__).resolve().parent.parent / "media_preview_generator" / "web" / "static" / "js" / "servers.js"
        ).read_text(encoding="utf-8")
        # servers.js has multiple DOMContentLoaded handlers; check that the
        # ONE that touches Edit-Server-modal anchors (#editAddPathMapping etc.)
        # also has the early-return guard. Search by the modal-specific anchor.
        idx = servers_js.find("$('#editAddPathMapping').addEventListener")
        assert idx >= 0, "servers.js no longer wires #editAddPathMapping — test selector needs updating."
        # Look back from the anchor to the enclosing DOMContentLoaded.
        block_start = servers_js.rfind("document.addEventListener('DOMContentLoaded'", 0, idx)
        assert block_start >= 0, "Couldn't find the DOMContentLoaded enclosing the Edit-Server-modal wiring."
        # Look forward to the closing `});` of that handler.
        block_end = servers_js.find("    });\n", idx)
        block = servers_js[block_start:block_end]
        assert "getElementById('editServerSave')" in block, (
            "servers.js Edit-Server-modal DOMContentLoaded handler must guard with a "
            "getElementById lookup so loading on /setup is a no-op."
        )
        assert "if (!editServerSaveBtn) return" in block or "if (!editServerSave) return" in block, (
            "servers.js Edit-Server-modal DOMContentLoaded handler must early-return when the modal "
            "anchor is null. Without the return, the next $('#editAddPathMapping').addEventListener "
            "throws TypeError on null and halts every later script on the /setup page."
        )


class TestNotificationSanitizerAllowsDisclosure:
    """B5 — sanitizeNotificationHtml must keep <details>/<summary> intact and
    must unwrap unknown tags by promoting children rather than collapsing to
    textContent. Without both, the migration card's "What changed" expander
    flattens its own <ul><li> children into one run-on text blob.
    """

    def test_details_and_summary_are_in_allow_list(self, app_js):
        snippet = app_js.split("function sanitizeNotificationHtml(", 1)[1].split("\nfunction ", 1)[0]
        assert "'DETAILS'" in snippet, (
            "sanitizeNotificationHtml must allow <details> — used by the schema-migration "
            "notification card's 'What changed' expander."
        )
        assert "'SUMMARY'" in snippet, "sanitizeNotificationHtml must allow <summary> alongside <details>."

    def test_unwrap_promotes_children_not_textcontent(self, app_js):
        """The unwrap path must move children up, not replace with textContent.

        Otherwise an unknown wrapper destroys structure of every allowed
        descendant inside it (the original B5 bug — <details> was disallowed
        and its <ul><li> children collapsed into one text node).
        """
        snippet = app_js.split("function sanitizeNotificationHtml(", 1)[1].split("\nfunction ", 1)[0]
        assert "createTextNode(el.textContent" not in snippet, (
            "sanitizeNotificationHtml's unwrap path must NOT collapse to el.textContent — that "
            "destroys the structure of every allowed child element inside the unwrapped node. "
            "Move children up to the parent instead."
        )
        assert "while (el.firstChild)" in snippet and "insertBefore" in snippet, (
            "sanitizeNotificationHtml's unwrap path must promote children via "
            "parent.insertBefore(el.firstChild, el) so allowed descendants survive."
        )


class TestRenderMarkdownBasicHandlesGitHubReleaseBodies:
    """B4 — _renderMarkdownBasic must cope with what GitHub release bodies
    actually look like: CRLF line endings, indented sub-headings under a top
    section, and inline markdown links to PRs/issues.
    """

    def test_normalizes_crlf_to_lf(self, app_js):
        snippet = app_js.split("function _renderMarkdownBasic(", 1)[1].split("\nfunction ", 1)[0]
        assert "\\r\\n" in snippet and "\\r" in snippet, (
            "_renderMarkdownBasic must normalize CRLF -> LF before any line-anchored regex "
            "runs. GitHub release bodies arrive with CRLF; without normalization, the "
            "paragraph-break regex /\\n{2,}/ never matches \\r\\n\\r\\n."
        )

    def test_h2_h3_tolerate_leading_whitespace(self, app_js):
        """Indented headings (very common under top-level sections) must render."""
        snippet = app_js.split("function _renderMarkdownBasic(", 1)[1].split("\nfunction ", 1)[0]
        assert "^[ \\t]{0,4}### " in snippet, (
            "_renderMarkdownBasic's H3 regex must allow leading whitespace — GitHub release "
            "bodies routinely indent sub-headings under a top-level section. The original "
            "/^### / pattern silently drops them as raw '### ' literals in the modal."
        )
        assert "^[ \\t]{0,4}## " in snippet, "Same reasoning for H2."

    def test_supports_markdown_links_with_safe_schemes(self, app_js):
        """[text](url) is the most common construct in any release body, and
        the URL group must be locked to safe schemes so a hand-crafted body
        can't drop a javascript:/data: link into the modal.
        """
        snippet = app_js.split("function _renderMarkdownBasic(", 1)[1].split("\nfunction ", 1)[0]
        # Hunt for the link-replace call specifically, not the surrounding
        # comment text — comments routinely mention javascript: as the
        # threat being guarded against.
        link_replace_lines = [
            line for line in snippet.splitlines() if "html.replace" in line and "[" in line and "]" in line
        ]
        assert link_replace_lines, (
            "_renderMarkdownBasic must support [text](url) markdown links — release notes "
            "are dense with PR/issue links that otherwise show as raw '[#221](https://...)' "
            "text in the What's New modal."
        )
        regex_payload = "\n".join(link_replace_lines)
        assert "https?:" in regex_payload and "mailto:" in regex_payload, (
            "The [text](url) link regex must explicitly allow https?:/mailto: schemes."
        )
        assert "javascript:" not in regex_payload, (
            "The [text](url) link regex itself must not contain javascript: — keep the URL "
            "group constrained to https?:/mailto: only so a hand-crafted body can't smuggle one in."
        )
