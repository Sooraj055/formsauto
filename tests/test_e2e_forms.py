"""End-to-end Playwright + FastAPI tests against realistic Google Forms DOM fixtures."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
from httpx import ASGITransport, AsyncClient

os.environ["FORMRUSH_ALLOW_LOCAL_URLS"] = "1"

from app.main import create_app
from app.models import UserProfile
from app.storage import SecureStorage


# Realistic Google Forms DOM structure mimicking Google Forms' ARIA widgets
GOOGLE_FORM_TEMPLATE = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{form_title}</title></head>
<body>
  <div class="F9yp7e" role="heading" aria-level="1">{form_title}</div>
  {extra_banner}
  <form id="gForm">
    <div class="o3Dpx" role="list">
      {items_html}
    </div>
    <div style="margin-top:16px;">
      {buttons_html}
    </div>
  </form>
  <script>
    const submissions = window.__SUBMISSIONS = [];
    // Google Forms custom listbox toggle
    document.querySelectorAll('div[role="listbox"]').forEach(lb => {{
      const labelSpan = lb.querySelector('.lb-label');
      const opts = lb.querySelectorAll('div[role="option"]');
      opts.forEach(o => {{ o.style.display = 'none'; }});
      lb.addEventListener('click', (e) => {{
        const open = lb.getAttribute('aria-expanded') === 'true';
        lb.setAttribute('aria-expanded', String(!open));
        opts.forEach(o => {{ o.style.display = open ? 'none' : 'block'; }});
      }});
      opts.forEach(o => {{
        o.addEventListener('click', (e) => {{
          e.stopPropagation();
          opts.forEach(x => x.setAttribute('aria-selected', 'false'));
          o.setAttribute('aria-selected', 'true');
          const val = o.getAttribute('data-value') || o.innerText.trim();
          lb.dataset.selected = val;
          if (labelSpan) labelSpan.innerText = val || 'Choose';
          lb.setAttribute('aria-expanded', 'false');
          opts.forEach(x => {{ x.style.display = 'none'; }});
        }});
      }});
    }});

    // Radio button behavior
    document.querySelectorAll('div[role="radiogroup"]').forEach(rg => {{
      const radios = rg.querySelectorAll('div[role="radio"]');
      radios.forEach(r => {{
        r.addEventListener('click', () => {{
          radios.forEach(x => x.setAttribute('aria-checked', 'false'));
          r.setAttribute('aria-checked', 'true');
        }});
      }});
    }});

    // Checkbox behavior
    document.querySelectorAll('div[role="checkbox"]').forEach(cb => {{
      cb.addEventListener('click', () => {{
        const cur = cb.getAttribute('aria-checked') === 'true';
        cb.setAttribute('aria-checked', String(!cur));
      }});
    }});

    function collectData() {{
      const data = {{}};
      document.querySelectorAll('div[role="listitem"]').forEach(item => {{
        const title = item.querySelector('div[role="heading"]')?.innerText.replace('*', '').trim() || '';
        const inp = item.querySelector('input, textarea');
        const rg = item.querySelector('div[role="radiogroup"]');
        const lb = item.querySelector('div[role="listbox"]');
        const cbs = item.querySelectorAll('div[role="checkbox"]');
        if (rg) {{
          const sel = rg.querySelector('[aria-checked="true"]');
          data[title] = sel ? (sel.getAttribute('data-value') || sel.innerText.trim()) : '';
        }} else if (lb) {{
          data[title] = lb.dataset.selected || '';
        }} else if (cbs.length) {{
          data[title] = Array.from(cbs).filter(c => c.getAttribute('aria-checked') === 'true')
            .map(c => c.getAttribute('data-answer-value') || c.innerText.trim());
        }} else if (inp) {{
          data[title] = inp.value;
        }}
      }});
      return data;
    }}

    const submitBtn = document.querySelector('div[role="button"][jsname="M2UYVd"]');
    if (submitBtn) {{
      submitBtn.addEventListener('click', async () => {{
        const payload = collectData();
        await fetch('{submit_endpoint}', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        window.location.href = '{confirm_url}';
      }});
    }}

    const nextBtn = document.querySelector('div[role="button"][data-nav="next"]');
    if (nextBtn) {{
      nextBtn.addEventListener('click', () => {{
        const payload = collectData();
        sessionStorage.setItem('page1', JSON.stringify(payload));
        window.location.href = '{next_url}';
      }});
    }}
  </script>
</body>
</html>"""


def _item_short(title: str, required: bool = True) -> str:
    req = '<span class="vnumgf" aria-label="Required question">*</span>' if required else ""
    return f"""
    <div role="listitem" class="Qr7Oae">
      <div role="heading" aria-level="3">{title} {req}</div>
      <input type="text" class="whsOnd zHQkBf" />
    </div>"""


def _item_radio(title: str, options: list[str], required: bool = True) -> str:
    req = '<span class="vnumgf" aria-label="Required question">*</span>' if required else ""
    radios = "".join(
        f'<div role="radio" tabindex="0" aria-checked="false" data-value="{o}">{o}</div>'
        for o in options
    )
    return f"""
    <div role="listitem" class="Qr7Oae">
      <div role="heading" aria-level="3">{title} {req}</div>
      <div role="radiogroup">{radios}</div>
    </div>"""


def _item_dropdown(title: str, options: list[str], required: bool = True) -> str:
    req = '<span class="vnumgf" aria-label="Required question">*</span>' if required else ""
    opts = '<div role="option" data-value="">Choose</div>' + "".join(
        f'<div role="option" data-value="{o}">{o}</div>' for o in options
    )
    return f"""
    <div role="listitem" class="Qr7Oae">
      <div role="heading" aria-level="3">{title} {req}</div>
      <div role="listbox" aria-expanded="false" style="min-height:24px;padding:6px;border:1px solid #ccc;">
        <span class="lb-label">Choose</span>
        {opts}
      </div>
    </div>"""


def _item_checkbox(title: str, options: list[str], required: bool = False) -> str:
    req = '<span class="vnumgf" aria-label="Required question">*</span>' if required else ""
    cbs = "".join(
        f'<div role="checkbox" aria-checked="false" data-answer-value="{o}">{o}</div>'
        for o in options
    )
    return f"""
    <div role="listitem" class="Qr7Oae">
      <div role="heading" aria-level="3">{title} {req}</div>
      <div role="list">{cbs}</div>
    </div>"""


class FixtureServer:
    """Thread-backed local HTTP server simulating multi-scenario Google Forms."""

    def __init__(self) -> None:
        self.received_submissions: dict[str, list[dict[str, Any]]] = {}
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/forms/d/e/full_auto/viewform":
                    items = (
                        _item_short("Full Name", True)
                        + _item_short("Phone Number", True)
                        + _item_short("Email ID", True)
                        + _item_dropdown("Select your location", ["Thrissur", "Cochin", "Kozhikode"], True)
                        + _item_radio("How many tickets do you need?", ["1 Ticket", "2 Tickets", "3 Tickets", "4 Tickets", "5 Tickets"], True)
                        + _item_dropdown("Ticket quantity", ["1", "2", "3", "4", "5"], True)
                        + _item_checkbox("Refreshments", ["Popcorn", "Coke", "Nachos"], False)
                        + _item_short("Instagram Handle (Optional)", False)
                    )
                    html = GOOGLE_FORM_TEMPLATE.format(
                        form_title="8 PM Ticket Token Registration",
                        extra_banner='<div class="autosave-notice">Sign in to Google to save your progress. Learn more</div>',
                        items_html=items,
                        buttons_html='<div role="button" jsname="M2UYVd" tabindex="0">Submit</div>',
                        submit_endpoint="/submit/full_auto",
                        confirm_url="/forms/d/e/full_auto/formResponse",
                        next_url="",
                    )
                    self._send_html(html)
                elif path == "/forms/d/e/unexpected_req/viewform":
                    items = (
                        _item_short("Name", True)
                        + _item_short("Mobile no.", True)
                        + _item_radio("Required tickets", ["1 Ticket", "2 Tickets", "4 Tickets"], True)
                        + _item_short("Secret Invite Code", True)
                    )
                    html = GOOGLE_FORM_TEMPLATE.format(
                        form_title="VIP Screening Form",
                        extra_banner="",
                        items_html=items,
                        buttons_html='<div role="button" jsname="M2UYVd" tabindex="0">Submit</div>',
                        submit_endpoint="/submit/unexpected_req",
                        confirm_url="/forms/d/e/unexpected_req/formResponse",
                        next_url="",
                    )
                    self._send_html(html)
                elif path == "/forms/d/e/qty_unavailable/viewform":
                    items = (
                        _item_short("Full name", True)
                        + _item_radio("How many tickets?", ["1 Ticket", "2 Tickets", "3 Tickets"], True)
                    )
                    html = GOOGLE_FORM_TEMPLATE.format(
                        form_title="Limited Quota Form",
                        extra_banner="",
                        items_html=items,
                        buttons_html='<div role="button" jsname="M2UYVd" tabindex="0">Submit</div>',
                        submit_endpoint="/submit/qty_unavailable",
                        confirm_url="/forms/d/e/qty_unavailable/formResponse",
                        next_url="",
                    )
                    self._send_html(html)
                elif path == "/forms/d/e/multipage/viewform":
                    items = _item_short("Full name", True) + _item_short("Contact number", True)
                    html = GOOGLE_FORM_TEMPLATE.format(
                        form_title="Multi-Page Registration",
                        extra_banner="",
                        items_html=items,
                        buttons_html='<div role="button" data-nav="next" tabindex="0">Next</div>',
                        submit_endpoint="/submit/multipage",
                        confirm_url="/forms/d/e/multipage/formResponse",
                        next_url="/forms/d/e/multipage/page2",
                    )
                    self._send_html(html)
                elif path == "/forms/d/e/multipage/page2":
                    items = _item_short("Email", True) + _item_radio("Number of seats", ["2 Seats", "4 Seats"], True)
                    html = GOOGLE_FORM_TEMPLATE.format(
                        form_title="Multi-Page Registration",
                        extra_banner="",
                        items_html=items,
                        buttons_html='<div role="button" jsname="M2UYVd" tabindex="0">Submit</div>',
                        submit_endpoint="/submit/multipage",
                        confirm_url="/forms/d/e/multipage/formResponse",
                        next_url="",
                    )
                    self._send_html(html)
                elif path == "/forms/d/e/captcha_form/viewform":
                    items = _item_short("Full name", True)
                    html = GOOGLE_FORM_TEMPLATE.format(
                        form_title="Protected Form",
                        extra_banner='<div class="g-recaptcha" data-sitekey="test-key"></div>',
                        items_html=items,
                        buttons_html='<div role="button" jsname="M2UYVd" tabindex="0">Submit</div>',
                        submit_endpoint="/submit/captcha",
                        confirm_url="/forms/d/e/captcha_form/formResponse",
                        next_url="",
                    )
                    self._send_html(html)
                elif path == "/forms/d/e/auth_required/viewform":
                    self._send_html(
                        """<!DOCTYPE html><html><head><title>You need permission</title></head>
                        <body><div>You need permission</div><div>Sign in to continue to Google Forms.</div></body></html>"""
                    )
                elif path == "/forms/d/e/closed_form/viewform":
                    self._send_html(
                        """<!DOCTYPE html><html><head><title>Closed Form</title></head>
                        <body><div>This form is no longer accepting responses.</div></body></html>"""
                    )
                elif path == "/forms/d/e/delayed_confirm/viewform":
                    items = (
                        _item_short("Name", True)
                        + _item_radio("Number of tickets", ["1", "2", "3", "Four tickets"], True)
                    )
                    html = GOOGLE_FORM_TEMPLATE.format(
                        form_title="Delayed Confirm Form",
                        extra_banner='<div class="autosave-notice">Sign in to Google to save your progress.</div>',
                        items_html=items,
                        buttons_html='<div role="button" jsname="M2UYVd" tabindex="0">Submit</div>',
                        submit_endpoint="/submit/delayed_confirm",
                        confirm_url="/forms/d/e/delayed_confirm/formResponse",
                        next_url="",
                    )
                    self._send_html(html)
                elif path == "/forms/d/e/delayed_confirm/formResponse":
                    # Simulate navigation landing on /formResponse before confirmation node hydrates
                    self._send_html(
                        """<!DOCTYPE html><html><body>
                        <div id="root">Loading...</div>
                        <script>
                          setTimeout(() => {
                            document.getElementById('root').innerHTML =
                              '<div class="vHW8K">Your response has been recorded.</div><a href="viewform">Submit another response</a>';
                          }, 450);
                        </script>
                        </body></html>"""
                    )
                elif path == "/forms/d/e/stall_submit/viewform":
                    items = _item_short("Name", True) + _item_radio("Number of tickets", ["1", "2", "3", "4"], True)
                    html = GOOGLE_FORM_TEMPLATE.format(
                        form_title="Stalling Submit Form",
                        extra_banner="",
                        items_html=items,
                        buttons_html='<div role="button" jsname="M2UYVd" tabindex="0">Submit</div>',
                        submit_endpoint="/submit/stall_submit",
                        confirm_url="/forms/d/e/stall_submit/never_confirms",
                        next_url="",
                    )
                    self._send_html(html)
                elif path == "/forms/d/e/stall_submit/never_confirms":
                    self._send_html("""<!DOCTYPE html><html><body><div>Processing...</div></body></html>""")
                elif path == "/forms/d/e/broken_radio/viewform":
                    items = _item_short("Name", True) + _item_radio("Number of tickets", ["1", "2", "3", "4"], False)
                    # Override click handler so radio refuses to flip aria-checked="true"
                    html = GOOGLE_FORM_TEMPLATE.format(
                        form_title="Broken Radio Form",
                        extra_banner="",
                        items_html=items,
                        buttons_html='<div role="button" jsname="M2UYVd" tabindex="0">Submit</div>',
                        submit_endpoint="/submit/broken_radio",
                        confirm_url="/forms/d/e/broken_radio/formResponse",
                        next_url="",
                    ).replace("r.setAttribute('aria-checked', 'true');", "// broken radio")
                    self._send_html(html)
                elif path.endswith("/formResponse"):
                    self._send_html(
                        """<!DOCTYPE html><html><body>
                        <div class="vHW8K">Your response has been recorded.</div>
                        <a href="viewform">Submit another response</a>
                        </body></html>"""
                    )
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                key = self.path.split("/submit/")[-1]
                parent.received_submissions.setdefault(key, []).append(json.loads(body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def _send_html(self, html: str) -> None:
                data = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


async def _wait_for_non_running(client: AsyncClient, run_id: str, timeout: float = 12.0) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        res = await client.get(f"/api/runs/{run_id}")
        data = res.json()
        if data["status"] != "running":
            return data
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Run {run_id} did not settle within {timeout}s")


@pytest.mark.asyncio
async def test_end_to_end_google_form_scenarios(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FORMRUSH_DEFAULT_NAME", "Test User")
    monkeypatch.setenv("FORMRUSH_DEFAULT_MOBILE", "9876543210")
    monkeypatch.setenv("FORMRUSH_DEFAULT_EMAIL", "testuser@example.com")
    monkeypatch.setenv("FORMRUSH_DEFAULT_TICKETS", "4")
    monkeypatch.setenv("FORMRUSH_DEFAULT_CITY", "Cochin")
    monkeypatch.setenv("FORMRUSH_DEFAULT_ADDRESS", "Sample Locality")
    monkeypatch.setenv("FORMRUSH_DEFAULT_PIN", "682001")

    fixtures = FixtureServer()
    base_url = fixtures.start()

    storage = SecureStorage(db_path=tmp_path / "e2e.db", key_path=tmp_path / ".e2e.key")
    storage.add_or_update_custom_field("Refreshments", "Popcorn, Nachos", ["refreshments", "snacks"])

    app = create_app(storage=storage)

    try:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # 1. AUTO-FILL + AUTO-SUBMIT without manual approval
                r1 = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/full_auto/viewform"})
                assert r1.status_code == 200
                summary1 = await _wait_for_non_running(client, r1.json()["run_id"])
                assert summary1["status"] == "submitted", summary1
                assert summary1["confirmation_text"] == "Your response has been recorded."
                assert summary1["filled_count"] == 7
                assert summary1["optional_unanswered_count"] == 1

                sub = fixtures.received_submissions["full_auto"][0]
                assert sub["Full Name"] == "Test User"
                assert sub["Phone Number"] == "9876543210"
                assert sub["Email ID"] == "testuser@example.com"
                assert sub["Select your location"] == "Cochin"
                assert sub["How many tickets do you need?"] == "4 Tickets"
                assert sub["Ticket quantity"] == "4"
                assert sub["Refreshments"] == ["Popcorn", "Nachos"]
                assert sub["Instagram Handle (Optional)"] == ""

                # 2. Duplicate submission prevention on same URL
                r_dup = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/full_auto/viewform?usp=sf_link"})
                assert r_dup.status_code == 200
                assert r_dup.json()["status"] == "duplicate_blocked"

                # 3. Unexpected required question -> PAUSE then RESUME without restarting
                r2 = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/unexpected_req/viewform"})
                summary2 = await _wait_for_non_running(client, r2.json()["run_id"])
                assert summary2["status"] == "paused"
                assert len(summary2["unresolved_questions"]) == 1
                missing_q = summary2["unresolved_questions"][0]
                assert missing_q["title"] == "Secret Invite Code"

                resumed = await client.post(
                    f"/api/runs/{summary2['run_id']}/resume",
                    json={
                        "answers": [
                            {"question_id": missing_q["question_id"], "answer": "VIP-8PM-2026", "save_to_profile": True}
                        ]
                    },
                )
                assert resumed.status_code == 200
                resumed_data = resumed.json()
                assert resumed_data["status"] == "submitted"
                assert fixtures.received_submissions["unexpected_req"][0]["Secret Invite Code"] == "VIP-8PM-2026"
                assert fixtures.received_submissions["unexpected_req"][0]["Required tickets"] == "4 Tickets"

                # 4. Unavailable ticket quantity (4 missing) -> PAUSE, never guess 3
                r3 = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/qty_unavailable/viewform"})
                summary3 = await _wait_for_non_running(client, r3.json()["run_id"])
                assert summary3["status"] == "paused"
                assert summary3["unresolved_questions"][0]["title"] == "How many tickets?"
                await client.post(f"/api/runs/{summary3['run_id']}/cancel")

                # 5. Multi-page form -> Next -> Fill Page 2 -> Auto-submit
                r4 = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/multipage/viewform"})
                summary4 = await _wait_for_non_running(client, r4.json()["run_id"])
                assert summary4["status"] == "submitted"
                assert fixtures.received_submissions["multipage"][0]["Number of seats"] == "4 Seats"

                # 6. CAPTCHA blocker -> BLOCKED immediately (no bypass)
                r5 = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/captcha_form/viewform"})
                summary5 = await _wait_for_non_running(client, r5.json()["run_id"])
                assert summary5["status"] == "blocked"
                assert "CAPTCHA" in summary5["message"]

                # 7. Genuine Google Sign-In / permission gate -> BLOCKED
                r6 = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/auth_required/viewform"})
                summary6 = await _wait_for_non_running(client, r6.json()["run_id"])
                assert summary6["status"] == "blocked"
                assert "Sign-In" in summary6["message"] or "permission" in summary6["message"]

                # 8. Closed form -> BLOCKED with closed message
                r7 = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/closed_form/viewform"})
                summary7 = await _wait_for_non_running(client, r7.json()["run_id"])
                assert summary7["status"] == "blocked"
                assert "closed" in summary7["message"].lower()

                # 9. HTTP 404 -> FAILED (distinguished from auth block)
                r8 = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/missing_404/viewform"})
                summary8 = await _wait_for_non_running(client, r8.json()["run_id"])
                assert summary8["status"] == "failed"
                assert "404" in summary8["message"]

                # 10. Delayed /formResponse confirmation (no false "no form questions rendered" error) + "Four tickets" radio
                r9 = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/delayed_confirm/viewform"})
                summary9 = await _wait_for_non_running(client, r9.json()["run_id"])
                assert summary9["status"] == "submitted", summary9
                assert summary9["filled_count"] == 2
                assert summary9["diagnostics"].get("confirmation_detected") is True
                assert fixtures.received_submissions["delayed_confirm"][0]["Number of tickets"] == "Four tickets"

                # 11. Radio verification failure -> PAUSED, never pretends filled or submits
                r10 = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/broken_radio/viewform"})
                summary10 = await _wait_for_non_running(client, r10.json()["run_id"])
                assert summary10["status"] == "paused", summary10
                assert summary10["filled_count"] == 1
                assert "broken_radio" not in fixtures.received_submissions
                await client.post(f"/api/runs/{summary10['run_id']}/cancel")

                # 12. Uncertain / timed-out submission -> status UNKNOWN + blocks duplicate retry
                import app.automation as auto_mod
                orig_timeout = auto_mod.SUBMIT_TIMEOUT_MS
                auto_mod.SUBMIT_TIMEOUT_MS = 600
                engine = app.state.engine
                original_emit = engine._emit
                retry_during_emit: dict[str, Any] = {}

                async def emit_and_retry_immediately(sess: Any) -> None:
                    # A subscriber that retries inside the terminal event must not
                    # observe the in-flight lock. That lock has to be released
                    # before _emit, or this request returns HTTP 409.
                    if (
                        getattr(sess.status, "value", sess.status) == "unknown"
                        and "stall_submit" in sess.canonical_key
                        and "status_code" not in retry_during_emit
                    ):
                        retry = await client.post(
                            "/api/runs/start",
                            json={"url": f"{base_url}/forms/d/e/stall_submit/viewform"},
                        )
                        retry_during_emit["status_code"] = retry.status_code
                        retry_during_emit["body"] = retry.json()
                    await original_emit(sess)

                engine._emit = emit_and_retry_immediately
                try:
                    r11 = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/stall_submit/viewform"})
                    summary11 = await _wait_for_non_running(client, r11.json()["run_id"])
                    assert summary11["status"] == "unknown", summary11
                    assert summary11["diagnostics"].get("submission_initiated") is True
                    assert summary11["diagnostics"].get("confirmation_detected") is False
                    assert retry_during_emit.get("status_code") == 200, retry_during_emit
                    assert retry_during_emit["body"]["status"] == "duplicate_blocked"
                finally:
                    auto_mod.SUBMIT_TIMEOUT_MS = orig_timeout
                    engine._emit = original_emit

                # Retrying after UNKNOWN must trigger duplicate protection, never a second submit
                r11_retry = await client.post("/api/runs/start", json={"url": f"{base_url}/forms/d/e/stall_submit/viewform"})
                assert r11_retry.status_code == 200
                assert r11_retry.json()["status"] == "duplicate_blocked"
                # The timed-out click may have reached the form once. The retry must not send another.
                assert len(fixtures.received_submissions.get("stall_submit", [])) == 1
    finally:
        fixtures.stop()
