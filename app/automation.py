"""Playwright Chromium automation engine for Google Forms extraction, filling, and submission."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.config import (
    ACTION_TIMEOUT_MS,
    PAGE_GOTO_TIMEOUT_MS,
    PAUSED_SESSION_TTL_SECONDS,
    SUBMIT_TIMEOUT_MS,
    canonicalize_form_key,
    logger,
    redact_sensitive,
    validate_google_form_url,
)
from app.matcher import analyze_questions, normalize_text
from app.models import (
    DetectedQuestion,
    MatchStatus,
    ProgressStage,
    QuestionAnalysis,
    QuestionType,
    ResumeAnswerItem,
    RunStatus,
    RunSummary,
    STAGE_ORDER,
    UserProfile,
)
from app.storage import SecureStorage

BLOCK_RESOURCE_TYPES = {"image", "font", "media"}


@dataclass
class RunSession:
    run_id: str
    form_url: str
    canonical_key: str
    started_at: float
    status: RunStatus = RunStatus.RUNNING
    current_stage: ProgressStage = ProgressStage.LINK_RECEIVED
    completed_stages: list[ProgressStage] = field(default_factory=list)
    form_title: str = ""
    all_analyses: dict[str, QuestionAnalysis] = field(default_factory=dict)
    current_page_index: int = 0
    message: str = "Link received."
    confirmation_text: str = ""
    error_detail: str = ""
    context: BrowserContext | None = None
    page: Page | None = None
    subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_activity: float = field(default_factory=time.monotonic)
    submission_initiated: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)
    closed: bool = False

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def to_summary(self) -> RunSummary:
        questions = list(self.all_analyses.values())
        matched_count = sum(1 for q in questions if q.status == MatchStatus.MATCHED)
        filled_count = sum(1 for q in questions if q.filled)
        missing_req = sum(
            1 for q in questions if q.required and (q.status != MatchStatus.MATCHED or not q.filled)
        )
        optional_unanswered = sum(
            1 for q in questions if (not q.required) and q.status != MatchStatus.MATCHED
        )
        unresolved = [
            q
            for q in questions
            if (q.required and q.status != MatchStatus.MATCHED)
            or (not q.filled and q.status == MatchStatus.MISSING)
            or (q.required and not q.filled and self.status == RunStatus.PAUSED)
            or (q.status == MatchStatus.UNCERTAIN and q.required)
        ]
        return RunSummary(
            run_id=self.run_id,
            form_title=self.form_title,
            form_url=self.form_url,
            canonical_key=self.canonical_key,
            status=self.status,
            current_stage=self.current_stage,
            completed_stages=list(self.completed_stages),
            total_questions=len(questions),
            matched_count=matched_count,
            filled_count=filled_count,
            missing_required_count=missing_req,
            optional_unanswered_count=optional_unanswered,
            questions=questions,
            unresolved_questions=unresolved,
            message=self.message,
            confirmation_text=self.confirmation_text,
            error_detail=self.error_detail,
            diagnostics=dict(self.diagnostics),
            elapsed_ms=self.elapsed_ms(),
        )


class FormAutomationEngine:
    """Manages a warm headless Chromium instance and active Google Form runs."""

    def __init__(self, storage: SecureStorage) -> None:
        self.storage = storage
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_lock = asyncio.Lock()
        self._sessions: dict[str, RunSession] = {}
        self._inflight_keys: set[str] = set()
        self._reaper_task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        """Warm up Playwright + Chromium on app start for minimal 8 PM latency."""
        await self._ensure_browser()
        self._reaper_task = asyncio.create_task(self._reap_stale_sessions())
        logger.info("FormAutomationEngine initialized with warm Chromium browser.")

    async def shutdown(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()
        for session in list(self._sessions.values()):
            await self._close_session_browser(session)
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _ensure_browser(self) -> Browser:
        async with self._browser_lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                ],
            )
            return self._browser

    async def _reap_stale_sessions(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for run_id, sess in list(self._sessions.items()):
                if now - sess.last_activity > PAUSED_SESSION_TTL_SECONDS:
                    if sess.page or sess.context:
                        await self._close_session_browser(sess)
                    if sess.status == RunStatus.PAUSED:
                        sess.status = RunStatus.FAILED
                        sess.message = "Paused session expired due to inactivity."
                        self._persist_history(sess)
                        self._inflight_keys.discard(sess.canonical_key)
                        await self._emit(sess)
                    else:
                        self._inflight_keys.discard(sess.canonical_key)
                    # Keep summary briefly, prune very old sessions
                    if now - sess.last_activity > PAUSED_SESSION_TTL_SECONDS * 2:
                        self._sessions.pop(run_id, None)

    def get_session(self, run_id: str) -> RunSession | None:
        return self._sessions.get(run_id)

    async def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]] | None:
        sess = self._sessions.get(run_id)
        if not sess:
            return None
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        sess.subscribers.append(q)
        # Immediately push current snapshot
        await q.put(sess.to_summary().model_dump())
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        sess = self._sessions.get(run_id)
        if sess and q in sess.subscribers:
            sess.subscribers.remove(q)

    async def _emit(self, sess: RunSession) -> None:
        sess.last_activity = time.monotonic()
        payload = sess.to_summary().model_dump()
        for q in list(sess.subscribers):
            await q.put(payload)

    def _advance_stage(self, sess: RunSession, stage: ProgressStage, message: str = "") -> None:
        idx = STAGE_ORDER.index(stage)
        sess.completed_stages = STAGE_ORDER[:idx]
        sess.current_stage = stage
        if message:
            sess.message = message

    def _persist_history(self, sess: RunSession) -> None:
        summary = sess.to_summary()
        self.storage.record_attempt(
            run_id=sess.run_id,
            canonical_key=sess.canonical_key,
            form_url=sess.form_url,
            form_title=sess.form_title,
            status=sess.status.value,
            total_questions=summary.total_questions,
            matched_count=summary.matched_count,
            filled_count=summary.filled_count,
            message=sess.message,
            elapsed_ms=sess.elapsed_ms(),
        )

    async def _seal_terminal(self, sess: RunSession, *, close_browser: bool = True) -> None:
        """Persist a terminal result, release the in-flight lock, then notify.

        Pollers and SSE subscribers react as soon as ``_emit`` runs. The lock
        has to be gone before that first await, otherwise a retry observes the
        terminal status while ``start_run`` still raises HTTP 409.
        """
        self._persist_history(sess)
        self._inflight_keys.discard(sess.canonical_key)
        await self._emit(sess)
        if close_browser:
            await self._close_session_browser(sess)

    async def _close_session_browser(self, sess: RunSession) -> None:
        if sess.closed:
            return
        sess.closed = True
        try:
            if sess.page:
                await sess.page.close()
        except Exception:
            pass
        try:
            if sess.context:
                await sess.context.close()
        except Exception:
            pass
        sess.page = None
        sess.context = None

    async def start_run(self, raw_url: str, force_resubmit: bool = False) -> RunSummary:
        is_valid, normalized_url, err = validate_google_form_url(raw_url)
        if not is_valid:
            raise ValueError(err)

        canonical_key = canonicalize_form_key(normalized_url)

        # Duplicate & concurrent submission protection
        if canonical_key in self._inflight_keys:
            raise RuntimeError("A registration run for this form is already in progress.")

        if not force_resubmit:
            prev = self.storage.get_previous_form_status(canonical_key)
            if prev:
                run_id = uuid.uuid4().hex[:12]
                msg = (
                    "Previous submission timed out with an uncertain result. Check the form before retrying."
                    if prev.status == RunStatus.UNCERTAIN_TIMEOUT.value
                    else "Duplicate protection: this Google Form was already submitted successfully."
                )
                return RunSummary(
                    run_id=run_id,
                    form_title=prev.form_title,
                    form_url=normalized_url,
                    canonical_key=canonical_key,
                    status=RunStatus.DUPLICATE_BLOCKED,
                    current_stage=ProgressStage.LINK_RECEIVED,
                    message=msg,
                    duplicate_warning={
                        "previous_run_id": prev.run_id,
                        "previous_status": prev.status,
                        "submitted_at": prev.updated_at,
                    },
                )

        run_id = uuid.uuid4().hex[:12]
        sess = RunSession(
            run_id=run_id,
            form_url=normalized_url,
            canonical_key=canonical_key,
            started_at=time.monotonic(),
        )
        self._advance_stage(sess, ProgressStage.LINK_RECEIVED, "Link received. Opening form...")
        self._sessions[run_id] = sess
        self._inflight_keys.add(canonical_key)
        self._persist_history(sess)

        asyncio.create_task(self._execute_initial_run(sess))
        return sess.to_summary()

    async def _create_fast_context(self) -> tuple[BrowserContext, Page]:
        browser = await self._ensure_browser()
        context = await browser.new_context(
            viewport={"width": 412, "height": 915},
            user_agent=(
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)

        async def _route_filter(route: Any) -> None:
            if route.request.resource_type in BLOCK_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", _route_filter)
        return context, page

    async def _wait_for_form_ready(self, page: Page, timeout_ms: int = 8000) -> None:
        """Wait for Google Forms controls or an explicit restriction banner without using networkidle."""
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            url_lower = page.url.lower()
            if "accounts.google.com" in url_lower or "google.com/sorry" in url_lower:
                return
            ready = await page.evaluate(
                """() => {
                    if (document.querySelector('iframe[src*="recaptcha"], .g-recaptcha, [data-sitekey]')) return true;
                    const items = document.querySelectorAll('div[role="listitem"]');
                    for (const item of items) {
                        if (item.querySelector('input:not([type="hidden"]), textarea, select, div[role="radiogroup"], div[role="listbox"], div[role="checkbox"]')) {
                            return true;
                        }
                    }
                    const txt = (document.body?.innerText || '').toLowerCase();
                    if (
                        txt.includes('no longer accepting responses') ||
                        txt.includes('you need permission') ||
                        txt.includes('sign in to continue') ||
                        txt.includes('your response has been recorded')
                    ) {
                        return true;
                    }
                    return false;
                }"""
            )
            if ready:
                return
            await page.wait_for_timeout(120)

    async def _check_blockers(self, page: Page, http_status: int | None = None) -> tuple[bool, RunStatus, str]:
        """Distinguish genuine auth/CAPTCHA/closed blocks from HTTP/rendering failures.

        Returns (is_blocked, target_status, user_message).
        """
        current_url = page.url.lower()
        if "accounts.google.com" in current_url:
            return (
                True,
                RunStatus.BLOCKED,
                "Google Sign-In is required to access this form. Open it manually in your browser.",
            )

        if "google.com/sorry" in current_url:
            return (
                True,
                RunStatus.BLOCKED,
                "Google bot protection triggered. Please open the form manually in your browser.",
            )

        if http_status is not None and http_status >= 400:
            if http_status in {401, 403}:
                return (
                    True,
                    RunStatus.BLOCKED,
                    f"Access denied (HTTP {http_status}). This form requires permission or Google Sign-In.",
                )
            if http_status == 404:
                return (
                    True,
                    RunStatus.FAILED,
                    "Google Form not found (HTTP 404). Verify that the link is complete and valid.",
                )
            return (
                True,
                RunStatus.FAILED,
                f"Google Forms returned HTTP {http_status}. Please retry or open manually.",
            )

        info = await page.evaluate(
            """() => {
                const rawBody = (document.body?.innerText || '');
                const lowerBody = rawBody.toLowerCase();
                // Strip Google Forms' standard optional autosave notice on public forms
                const cleanBody = lowerBody
                    .replace(/sign in to google to save your progress\\.?\\s*(learn more)?/g, ' ')
                    .replace(/\\s+/g, ' ');
                const hasRecaptchaIframe = !!document.querySelector(
                    'iframe[src*="recaptcha"], .g-recaptcha, [data-sitekey]'
                );
                const title = document.querySelector('[role="heading"][aria-level="1"], .F9yp7e')?.innerText?.trim()
                    || document.title || '';

                let fillableCount = 0;
                for (const item of document.querySelectorAll('div[role="listitem"]')) {
                    if (item.querySelector(
                        'input:not([type="hidden"]), textarea, select, div[role="radiogroup"], div[role="listbox"], div[role="checkbox"], input[type="radio"], input[type="checkbox"]'
                    )) {
                        fillableCount += 1;
                    }
                }
                const hasForm = !!document.querySelector('form');
                const hasNavBtn = !!document.querySelector(
                    'div[role="button"][jsname="M2UYVd"], div[role="button"][data-nav="next"], button[type="submit"]'
                );
                return {
                    cleanBody: cleanBody.slice(0, 4000),
                    hasRecaptchaIframe,
                    title,
                    fillableCount,
                    hasForm,
                    hasNavBtn,
                };
            }"""
        )

        body = info.get("cleanBody", "")
        fillable_count = int(info.get("fillableCount", 0))

        if info.get("hasRecaptchaIframe") or "unusual traffic from your computer network" in body:
            return (
                True,
                RunStatus.BLOCKED,
                "CAPTCHA or bot verification detected on this form. Please complete verification manually in Chrome.",
            )

        if "no longer accepting responses" in body or "this form is closed" in body:
            return (
                True,
                RunStatus.BLOCKED,
                "This Google Form is closed and no longer accepting responses.",
            )

        # Only treat sign-in / permission phrases as blocking when no fillable form questions rendered
        if fillable_count == 0:
            auth_markers = (
                "you need permission",
                "sign in to continue",
                "sign in to google",
                "must be signed in",
                "only be viewed by users in the owner",
                "requires sign in",
            )
            if any(marker in body for marker in auth_markers):
                return (
                    True,
                    RunStatus.BLOCKED,
                    "Form requires Google Sign-In or special permission. Please open manually.",
                )

            if not info.get("hasForm"):
                return (
                    True,
                    RunStatus.FAILED,
                    "Page loaded, but no Google Form questions finished rendering. Retry or open manually.",
                )

        return False, RunStatus.RUNNING, ""

    async def _extract_page_questions(self, page: Page, page_index: int) -> tuple[str, list[DetectedQuestion]]:
        """Extract all visible questions, types, required flags, and options on the current page."""
        # Collect options for Google Forms custom dropdowns even if rendered in an external portal popup
        portaled_dropdown_options: dict[int, list[str]] = {}
        listbox_count = await page.locator('div[role="listitem"] div[role="listbox"]').count()
        for i in range(listbox_count):
            lb = page.locator('div[role="listitem"] div[role="listbox"]').nth(i)
            opt_count = await lb.locator('[role="option"]').count()
            if opt_count == 0:
                try:
                    await lb.click(timeout=1500)
                    await page.wait_for_timeout(150)
                    opts_loc = page.locator('div[role="option"]:visible')
                    ocount = await opts_loc.count()
                    collected: list[str] = []
                    for j in range(ocount):
                        opt = opts_loc.nth(j)
                        dv = (await opt.get_attribute("data-value")) or ""
                        txt = ((await opt.inner_text()) or "").strip()
                        val = dv if dv else txt
                        if dv == "" and txt.lower() == "choose":
                            continue
                        if val and val not in collected:
                            collected.append(val)
                    portaled_dropdown_options[i] = collected
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(80)
                except Exception:
                    pass

        raw = await page.evaluate(
            """({pageIndex, portaledOpts}) => {
                const titleEl = document.querySelector('.F9yp7e, div[role="heading"][aria-level="1"]');
                const formTitle = titleEl ? titleEl.innerText.trim() : (document.title || 'Google Form');

                const items = Array.from(document.querySelectorAll('div[role="listitem"]'));
                const results = [];
                let qIdx = 0;
                let lbIdx = 0;

                const cleanText = (s) => (s || '').replace(/\\s+/g, ' ').trim();

                for (const item of items) {
                    // Check if this listitem actually holds a form input/control
                    const radioGroup = item.querySelector('div[role="radiogroup"]');
                    const nativeRadios = item.querySelectorAll('input[type="radio"]');
                    const listbox = item.querySelector('div[role="listbox"]');
                    const nativeSelect = item.querySelector('select');
                    const ariaCheckboxes = item.querySelectorAll('div[role="checkbox"]');
                    const nativeCheckboxes = item.querySelectorAll('input[type="checkbox"]');
                    const textarea = item.querySelector('textarea');
                    const textInput = item.querySelector(
                        'input[type="text"], input[type="email"], input[type="tel"], input[type="number"], input[type="url"], input:not([type])'
                    );

                    const hasControl = !!(
                        radioGroup || nativeRadios.length || listbox || nativeSelect ||
                        ariaCheckboxes.length || nativeCheckboxes.length || textarea || textInput
                    );
                    if (!hasControl) continue;

                    const qid = `q_${pageIndex}_${qIdx}`;
                    qIdx += 1;
                    item.setAttribute('data-formrush-qid', qid);

                    const headingEl = item.querySelector('div[role="heading"], .M7eMe, legend, label');
                    let title = '';
                    let required = false;

                    if (headingEl) {
                        const clone = headingEl.cloneNode(true);
                        const reqIndicators = clone.querySelectorAll('[aria-label*="Required" i], .vnumgf');
                        if (reqIndicators.length > 0) {
                            required = true;
                            reqIndicators.forEach(el => el.remove());
                        }
                        title = cleanText(clone.innerText || clone.textContent);
                    }
                    if (!title) {
                        const labelAttr = item.getAttribute('aria-label') || '';
                        title = cleanText(labelAttr);
                    }
                    if (title.endsWith('*')) {
                        required = true;
                        title = cleanText(title.slice(0, -1));
                    }
                    if (!required) {
                        if (
                            item.querySelector('[aria-required="true"], [required], [aria-label*="Required" i], .vnumgf') ||
                            (headingEl && headingEl.innerText.includes('*'))
                        ) {
                            required = true;
                        }
                    }

                    const descEl = item.querySelector('.gubaDc, .cBGGJ, [data-description]');
                    const description = descEl ? cleanText(descEl.innerText) : '';

                    let questionType = 'unknown';
                    const options = [];
                    let hasOther = false;

                    if (radioGroup || nativeRadios.length) {
                        questionType = 'multiple_choice';
                        const radios = item.querySelectorAll('div[role="radio"]');
                        if (radios.length) {
                            for (const r of radios) {
                                const val = r.getAttribute('data-value') || r.getAttribute('aria-label') || cleanText(r.innerText);
                                if (val === '__other_option__' || val.toLowerCase() === 'other:') {
                                    hasOther = true;
                                    continue;
                                }
                                if (val && !options.includes(val)) options.push(cleanText(val));
                            }
                        } else {
                            for (const r of nativeRadios) {
                                const lbl = item.querySelector(`label[for="${r.id}"]`) || r.closest('label');
                                const val = lbl ? cleanText(lbl.innerText) : cleanText(r.value);
                                if (val && !options.includes(val)) options.push(val);
                            }
                        }
                    } else if (listbox || nativeSelect) {
                        questionType = 'dropdown';
                        if (nativeSelect) {
                            for (const opt of nativeSelect.querySelectorAll('option')) {
                                const txt = cleanText(opt.textContent);
                                if (!opt.value && (txt.toLowerCase() === 'choose' || txt.toLowerCase().startsWith('select'))) continue;
                                if (txt && !options.includes(txt)) options.push(txt);
                            }
                        } else {
                            const opts = listbox.querySelectorAll('[role="option"]');
                            for (const opt of opts) {
                                const dv = opt.getAttribute('data-value');
                                const txt = cleanText(dv !== null ? dv : (opt.getAttribute('aria-label') || opt.innerText));
                                if (dv === '' || txt.toLowerCase() === 'choose') continue;
                                if (txt && !options.includes(txt)) options.push(txt);
                            }
                            if (options.length === 0 && portaledOpts[String(lbIdx)]) {
                                for (const txt of portaledOpts[String(lbIdx)]) {
                                    if (txt && !options.includes(txt)) options.push(cleanText(txt));
                                }
                            }
                            lbIdx += 1;
                        }
                    } else if (ariaCheckboxes.length || nativeCheckboxes.length) {
                        questionType = 'checkbox';
                        if (ariaCheckboxes.length) {
                            for (const c of ariaCheckboxes) {
                                const val = c.getAttribute('data-answer-value') || c.getAttribute('aria-label') || cleanText(c.innerText);
                                if (val === '__other_option__') {
                                    hasOther = true;
                                    continue;
                                }
                                if (val && !options.includes(val)) options.push(cleanText(val));
                            }
                        } else {
                            for (const c of nativeCheckboxes) {
                                const lbl = item.querySelector(`label[for="${c.id}"]`) || c.closest('label');
                                const val = lbl ? cleanText(lbl.innerText) : cleanText(c.value);
                                if (val && !options.includes(val)) options.push(val);
                            }
                        }
                    } else if (textarea) {
                        questionType = 'paragraph';
                    } else if (textInput) {
                        questionType = 'short_answer';
                    }

                    results.push({
                        question_id: qid,
                        page_index: pageIndex,
                        title,
                        description,
                        question_type: questionType,
                        required,
                        options,
                        has_other_option: hasOther,
                    });
                }

                return { formTitle, questions: results };
            }""",
            {"pageIndex": page_index, "portaledOpts": {str(k): v for k, v in portaled_dropdown_options.items()}},
        )

        questions = [DetectedQuestion(**q) for q in raw.get("questions", [])]
        return raw.get("formTitle", "Google Form"), questions

    async def _fill_question_on_page(self, page: Page, q: QuestionAnalysis, answer: str | list[str]) -> bool:
        """Fill a single question inside the Playwright DOM and verify it was populated."""
        container = page.locator(f'div[role="listitem"][data-formrush-qid="{q.question_id}"]')
        if await container.count() == 0:
            return False

        await container.scroll_into_view_if_needed(timeout=2000)

        if q.question_type in {QuestionType.SHORT_ANSWER, QuestionType.PARAGRAPH}:
            val = answer[0] if isinstance(answer, list) else str(answer)
            field_loc = container.locator("textarea").first if q.question_type == QuestionType.PARAGRAPH else container.locator(
                'input[type="text"], input[type="email"], input[type="tel"], input[type="number"], input[type="url"], input:not([type])'
            ).first
            await field_loc.click(timeout=2000)
            await field_loc.fill(val, timeout=2000)
            actual = await field_loc.input_value(timeout=1500)
            return actual.strip() == val.strip()

        if q.question_type == QuestionType.MULTIPLE_CHOICE:
            target = answer[0] if isinstance(answer, list) else str(answer)
            norm_target = normalize_text(target)

            aria_radios = container.locator('div[role="radio"]')
            count = await aria_radios.count()
            if count > 0:
                for i in range(count):
                    radio = aria_radios.nth(i)
                    dv = (await radio.get_attribute("data-value")) or ""
                    al = (await radio.get_attribute("aria-label")) or ""
                    txt = (await radio.inner_text()) or ""
                    rid = await radio.get_attribute("id")
                    lbl_txt = ""
                    if rid:
                        lbl = container.locator(f'label[for="{rid}"]')
                        if await lbl.count() > 0:
                            lbl_txt = (await lbl.first.inner_text()) or ""

                    if any(normalize_text(cand) == norm_target for cand in (dv, al, txt, lbl_txt) if cand):
                        await radio.scroll_into_view_if_needed(timeout=2000)
                        await radio.click(timeout=2000)
                        # Wait briefly for Google Forms JS controller to update aria-checked="true"
                        for _ in range(10):
                            if (await radio.get_attribute("aria-checked")) == "true":
                                return True
                            await page.wait_for_timeout(50)

                        # Fallback: click associated <label for="..."> if clicking the 20x20 circle missed
                        if rid:
                            lbl = container.locator(f'label[for="{rid}"]')
                            if await lbl.count() > 0:
                                await lbl.first.click(timeout=2000)
                                for _ in range(10):
                                    if (await radio.get_attribute("aria-checked")) == "true":
                                        return True
                                    await page.wait_for_timeout(50)

                        # Final JS click fallback only on this exact matched element if needed
                        return (await radio.get_attribute("aria-checked")) == "true"
                return False

            native_radios = container.locator('input[type="radio"]')
            ncount = await native_radios.count()
            for i in range(ncount):
                r = native_radios.nth(i)
                rid = await r.get_attribute("id")
                val_attr = (await r.get_attribute("value")) or ""
                lbl_txt = ""
                if rid:
                    lbl = container.locator(f'label[for="{rid}"]')
                    if await lbl.count() > 0:
                        lbl_txt = await lbl.first.inner_text()
                if normalize_text(lbl_txt) == norm_target or normalize_text(val_attr) == norm_target:
                    await r.scroll_into_view_if_needed(timeout=2000)
                    await r.check(timeout=2000)
                    for _ in range(6):
                        if await r.is_checked():
                            return True
                        await page.wait_for_timeout(40)
                    return await r.is_checked()
            return False

        if q.question_type == QuestionType.DROPDOWN:
            target = answer[0] if isinstance(answer, list) else str(answer)
            norm_target = normalize_text(target)

            native_sel = container.locator("select")
            if await native_sel.count() > 0:
                opts = await native_sel.first.locator("option").all()
                for opt in opts:
                    txt = (await opt.text_content()) or ""
                    val_attr = (await opt.get_attribute("value")) or ""
                    if normalize_text(txt) == norm_target or normalize_text(val_attr) == norm_target:
                        chosen_val = val_attr if val_attr else txt
                        await native_sel.first.select_option(value=chosen_val)
                        cur = await native_sel.first.input_value()
                        return cur == chosen_val
                return False

            listbox = container.locator('div[role="listbox"]').first
            await listbox.scroll_into_view_if_needed(timeout=2000)
            await listbox.click(timeout=2000)
            await page.wait_for_timeout(150)

            # Google Forms renders open dropdown options either inside listbox or in a floating popup
            popup_options = page.locator('div[role="option"]:visible')
            pcount = await popup_options.count()
            if pcount == 0:
                popup_options = listbox.locator('[role="option"]')
                pcount = await popup_options.count()

            for i in range(pcount):
                opt = popup_options.nth(i)
                dv = (await opt.get_attribute("data-value")) or ""
                txt = (await opt.inner_text()) or ""
                if dv == "" and normalize_text(txt) == "choose":
                    continue
                if normalize_text(dv) == norm_target or normalize_text(txt) == norm_target:
                    await opt.click(timeout=2000)
                    for _ in range(8):
                        sel_txt = (await listbox.inner_text()) or ""
                        selected_opt = listbox.locator('[role="option"][aria-selected="true"]')
                        if await selected_opt.count() > 0:
                            return True
                        if norm_target and norm_target in normalize_text(sel_txt):
                            return True
                        await page.wait_for_timeout(40)
                    return True
            await page.keyboard.press("Escape")
            return False

        if q.question_type == QuestionType.CHECKBOX:
            targets = answer if isinstance(answer, list) else [x.strip() for x in str(answer).split(",") if x.strip()]
            norm_targets = {normalize_text(t) for t in targets}
            if not norm_targets:
                return False

            matched_any = False
            aria_cbs = container.locator('div[role="checkbox"]')
            count = await aria_cbs.count()
            if count > 0:
                for i in range(count):
                    cb = aria_cbs.nth(i)
                    dv = (await cb.get_attribute("data-answer-value")) or ""
                    al = (await cb.get_attribute("aria-label")) or ""
                    txt = (await cb.inner_text()) or ""
                    if any(normalize_text(cand) in norm_targets for cand in (dv, al, txt) if cand):
                        if (await cb.get_attribute("aria-checked")) != "true":
                            await cb.click(timeout=2000)
                        if (await cb.get_attribute("aria-checked")) == "true":
                            matched_any = True
                return matched_any

            native_cbs = container.locator('input[type="checkbox"]')
            ncount = await native_cbs.count()
            for i in range(ncount):
                cb = native_cbs.nth(i)
                cid = await cb.get_attribute("id")
                val_attr = (await cb.get_attribute("value")) or ""
                lbl_txt = ""
                if cid:
                    lbl = container.locator(f'label[for="{cid}"]')
                    if await lbl.count() > 0:
                        lbl_txt = await lbl.first.inner_text()
                if normalize_text(lbl_txt) in norm_targets or normalize_text(val_attr) in norm_targets:
                    if not await cb.is_checked():
                        await cb.check(timeout=2000)
                    if await cb.is_checked():
                        matched_any = True
            return matched_any

        return False

    async def _find_nav_button(self, page: Page, label: str) -> Any | None:
        """Locate Google Forms 'Submit' or 'Next' button cleanly."""
        selectors = [
            f'div[role="button"]:has-text("{label}")',
            f'button:has-text("{label}")',
        ]
        if label.lower() == "submit":
            selectors.insert(0, 'div[role="button"][jsname="M2UYVd"]')
            selectors.append('button[type="submit"]')

        for sel in selectors:
            loc = page.locator(sel)
            count = await loc.count()
            for i in range(count):
                btn = loc.nth(i)
                if await btn.is_visible():
                    txt = (await btn.inner_text() or "").strip().lower()
                    if txt == label.lower() or (label.lower() == "submit" and "submit" in txt and "clear" not in txt):
                        return btn
        return None

    async def _verify_all_populated_in_dom(self, page: Page, questions: list[QuestionAnalysis]) -> list[str]:
        """Re-verify in live DOM that every filled question actually retains its selected/entered value."""
        failed_titles: list[str] = []
        for q in questions:
            if not q.filled or q.proposed_answer is None:
                continue
            container = page.locator(f'div[role="listitem"][data-formrush-qid="{q.question_id}"]')
            if await container.count() == 0:
                failed_titles.append(q.title)
                continue
            ok = False
            if q.question_type in {QuestionType.SHORT_ANSWER, QuestionType.PARAGRAPH}:
                loc = container.locator("textarea").first if q.question_type == QuestionType.PARAGRAPH else container.locator(
                    'input[type="text"], input[type="email"], input[type="tel"], input[type="number"], input[type="url"], input:not([type])'
                ).first
                expected = q.proposed_answer[0] if isinstance(q.proposed_answer, list) else str(q.proposed_answer)
                ok = (await loc.input_value(timeout=1500)).strip() == expected.strip()
            elif q.question_type == QuestionType.MULTIPLE_CHOICE:
                target = normalize_text(
                    q.proposed_answer[0] if isinstance(q.proposed_answer, list) else str(q.proposed_answer)
                )
                checked_aria = container.locator('div[role="radio"][aria-checked="true"]')
                if await checked_aria.count() == 1:
                    r = checked_aria.first
                    dv = (await r.get_attribute("data-value")) or ""
                    al = (await r.get_attribute("aria-label")) or ""
                    txt = (await r.inner_text()) or ""
                    ok = any(normalize_text(c) == target for c in (dv, al, txt) if c)
                else:
                    native_checked = container.locator('input[type="radio"]:checked')
                    ok = (await native_checked.count()) == 1
            elif q.question_type == QuestionType.DROPDOWN:
                native_sel = container.locator("select")
                if await native_sel.count() > 0:
                    ok = bool((await native_sel.first.input_value()).strip())
                else:
                    lb = container.locator('div[role="listbox"]').first
                    sel_opt = lb.locator('[role="option"][aria-selected="true"]')
                    txt = normalize_text((await lb.inner_text()) or "")
                    ok = (await sel_opt.count()) > 0 or (txt != "" and txt != "choose")
            elif q.question_type == QuestionType.CHECKBOX:
                ok = (
                    (await container.locator('div[role="checkbox"][aria-checked="true"]').count()) > 0
                    or (await container.locator('input[type="checkbox"]:checked').count()) > 0
                )
            if not ok:
                q.filled = False
                q.status = MatchStatus.MISSING
                q.reason = "Field verification failed before submission."
                failed_titles.append(q.title)
        return failed_titles

    async def _check_post_submit_blocker(self, page: Page) -> tuple[bool, RunStatus, str]:
        """Post-submit check ONLY for auth redirects, CAPTCHA, or closed form (never checks form rendering!)."""
        url_lower = page.url.lower()
        if "accounts.google.com" in url_lower:
            return (
                True,
                RunStatus.BLOCKED,
                "Google Sign-In was requested after submission. Verify manually in your browser.",
            )
        if "google.com/sorry" in url_lower:
            return (
                True,
                RunStatus.BLOCKED,
                "Google bot protection triggered during submission. Verify manually in your browser.",
            )
        info = await page.evaluate(
            """() => {
                const hasRecaptcha = !!document.querySelector('iframe[src*="recaptcha"], .g-recaptcha, [data-sitekey]');
                const body = (document.body?.innerText || '').toLowerCase();
                return { hasRecaptcha, body: body.slice(0, 2000) };
            }"""
        )
        if info.get("hasRecaptcha") or "unusual traffic from your computer network" in info.get("body", ""):
            return (
                True,
                RunStatus.BLOCKED,
                "CAPTCHA challenge appeared during submission. Complete verification manually.",
            )
        if "no longer accepting responses" in info.get("body", ""):
            return (
                True,
                RunStatus.BLOCKED,
                "This Google Form closed before submission completed.",
            )
        return False, RunStatus.RUNNING, ""

    async def _check_confirmation(self, page: Page) -> tuple[bool, str]:
        """Verify genuine Google Forms submission confirmation (never assume click == success)."""
        url = page.url.lower()
        result = await page.evaluate(
            """() => {
                const confEl = document.querySelector(
                    '.vHW8K, .freebirdFormviewerViewResponseConfirmationMessage, [data-response-confirmation], .idZHHb'
                );
                const confText = confEl ? confEl.innerText.trim() : '';
                const another = document.querySelector('a[href*="viewform"]');
                const hasSubmitStill = !!document.querySelector('div[role="button"][jsname="M2UYVd"]');
                const body = (document.body?.innerText || '').trim();
                const errorAlerts = Array.from(document.querySelectorAll('.RHiWt, [role="alert"], [aria-invalid="true"]'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => (el.innerText || '').trim())
                    .filter(Boolean);
                return {
                    confText,
                    hasAnotherLink: !!another,
                    hasSubmitStill,
                    body: body.slice(0, 2000),
                    errorAlerts,
                };
            }"""
        )

        if result["errorAlerts"]:
            return False, ""

        conf_text = result["confText"]
        body_lower = result["body"].lower()
        if conf_text and not result["hasSubmitStill"]:
            return True, conf_text
        if "formresponse" in url and (
            "recorded" in body_lower
            or "submitted" in body_lower
            or "thank you" in body_lower
            or result["hasAnotherLink"]
        ):
            return True, result["body"].splitlines()[0] if result["body"] else "Your response has been recorded."
        if "your response has been recorded" in body_lower:
            return True, "Your response has been recorded."
        return False, ""

    async def _get_validation_errors(self, page: Page) -> list[str]:
        return await page.evaluate(
            """() => {
                const errs = [];
                for (const item of document.querySelectorAll('div[role="listitem"]')) {
                    const errEl = item.querySelector('.RHiWt, [role="alert"]');
                    if (errEl && errEl.offsetParent !== null && errEl.innerText.trim()) {
                        const heading = item.querySelector('div[role="heading"]')?.innerText?.trim() || 'Field';
                        errs.push(`${heading}: ${errEl.innerText.trim()}`);
                    }
                }
                return errs;
            }"""
        )

    async def _execute_initial_run(self, sess: RunSession) -> None:
        async with sess.lock:
            try:
                context, page = await self._create_fast_context()
                sess.context = context
                sess.page = page

                resp = await page.goto(sess.form_url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
                http_status = resp.status if resp else None
                sess.diagnostics["http_status"] = http_status
                sess.diagnostics["final_url"] = page.url.split("?")[0]
                logger.info(
                    "Navigated to form %s (HTTP %s, final_host=%s)",
                    sess.canonical_key,
                    http_status,
                    page.url.split("/")[2] if "://" in page.url else "",
                )
                await self._wait_for_form_ready(page, timeout_ms=8000)

                blocked, block_status, block_reason = await self._check_blockers(page, http_status=http_status)
                if blocked:
                    sess.status = block_status
                    sess.message = block_reason
                    await self._seal_terminal(sess)
                    return

                self._advance_stage(sess, ProgressStage.FORM_OPENED, "Form opened successfully.")
                await self._emit(sess)

                await self._process_current_page(sess)

            except PlaywrightTimeoutError as exc:
                logger.warning("Timeout opening form %s: %s", sess.canonical_key, type(exc).__name__)
                sess.status = RunStatus.FAILED
                sess.message = "Timed out while loading the Google Form. Check your connection and try again."
                sess.error_detail = type(exc).__name__
                await self._seal_terminal(sess)
            except PlaywrightError as exc:
                profile = self.storage.get_profile()
                safe_err = redact_sensitive(
                    str(exc),
                    [profile.full_name, profile.mobile_number, profile.email, profile.address_locality],
                )
                logger.warning("Browser navigation error for %s: %s", sess.canonical_key, safe_err[:150])
                sess.status = RunStatus.FAILED
                sess.message = "Browser navigation failed while opening the form. Check the link and network connection."
                sess.error_detail = safe_err[:200]
                await self._seal_terminal(sess)
            except Exception as exc:
                # A terminal seal already released the lock and recorded history.
                # Do not downgrade SUBMITTED/UNKNOWN into a retryable failure.
                if sess.status in {
                    RunStatus.SUBMITTED,
                    RunStatus.UNKNOWN,
                    RunStatus.BLOCKED,
                    RunStatus.FAILED,
                    RunStatus.PAUSED,
                }:
                    self._inflight_keys.discard(sess.canonical_key)
                    raise
                profile = self.storage.get_profile()
                safe_err = redact_sensitive(
                    str(exc),
                    [profile.full_name, profile.mobile_number, profile.email, profile.address_locality],
                )
                logger.exception("Automation run failed for %s: %s", sess.canonical_key, safe_err)
                if sess.submission_initiated:
                    sess.status = RunStatus.UNKNOWN
                    sess.message = (
                        "Submission outcome is UNKNOWN after an automation error. "
                        "Verify whether the response was recorded before retrying."
                    )
                else:
                    sess.status = RunStatus.FAILED
                    sess.message = "Automation failed while accessing the form. Use manual fallback if needed."
                sess.error_detail = safe_err[:200]
                await self._seal_terminal(sess)
            finally:
                # Paused runs stay in-flight so a second Start cannot submit in parallel.
                if sess.status != RunStatus.PAUSED:
                    self._inflight_keys.discard(sess.canonical_key)

    async def _process_current_page(self, sess: RunSession) -> None:
        page = sess.page
        assert page is not None

        profile = self.storage.get_profile()
        form_title, detected = await self._extract_page_questions(page, sess.current_page_index)
        if form_title and not sess.form_title:
            sess.form_title = form_title

        if not detected:
            sess.status = RunStatus.BLOCKED
            sess.message = "No fillable questions were detected on this page."
            await self._seal_terminal(sess)
            return

        self._advance_stage(
            sess,
            ProgressStage.QUESTIONS_DETECTED,
            f"Detected {len(detected)} question(s) on page {sess.current_page_index + 1}.",
        )
        await self._emit(sess)

        analyses = analyze_questions(detected, profile)
        for qa in analyses:
            sess.all_analyses[qa.question_id] = qa

        self._advance_stage(sess, ProgressStage.ANSWERS_MATCHED, "Matched questions against your profile.")
        await self._emit(sess)

        # Fill all confidently matched questions immediately and verify DOM state
        fill_failures: list[QuestionAnalysis] = []
        for qa in analyses:
            if qa.status == MatchStatus.MATCHED and qa.proposed_answer is not None:
                ok = await self._fill_question_on_page(page, qa, qa.proposed_answer)
                qa.filled = ok
                if not ok:
                    qa.status = MatchStatus.MISSING
                    qa.reason = f"Matched '{qa.proposed_answer}', but browser could not verify filling this field."
                    fill_failures.append(qa)
                    logger.warning(
                        "Fill verification failed for question %s (%s) on %s",
                        qa.question_id,
                        qa.question_type.value,
                        sess.canonical_key,
                    )

        sess.diagnostics["questions_detected"] = len(sess.all_analyses)
        sess.diagnostics["questions_matched"] = sum(
            1 for q in sess.all_analyses.values() if q.status == MatchStatus.MATCHED
        )
        sess.diagnostics["questions_filled"] = sum(1 for q in sess.all_analyses.values() if q.filled)

        self._advance_stage(sess, ProgressStage.FORM_FILLED, "Populated matched answers into the form.")
        await self._emit(sess)

        # Pause if ANY required question OR ANY matched-but-failed-to-fill question remains unresolved
        unresolved_items = [
            qa
            for qa in analyses
            if (qa.required and (qa.status != MatchStatus.MATCHED or not qa.filled))
            or (qa in fill_failures)
        ]

        if unresolved_items:
            sess.status = RunStatus.PAUSED
            sess.message = (
                f"Paused: {len(unresolved_items)} question(s) need input or selection verification."
            )
            self._persist_history(sess)
            await self._emit(sess)
            return

        await self._validate_and_advance_or_submit(sess)

    async def _validate_and_advance_or_submit(self, sess: RunSession) -> None:
        page = sess.page
        assert page is not None

        # Verify DOM population of every filled field on current page before validation passes
        current_page_items = [
            qa for qa in sess.all_analyses.values() if qa.page_index == sess.current_page_index
        ]
        failed_dom_checks = await self._verify_all_populated_in_dom(page, current_page_items)
        if failed_dom_checks:
            sess.status = RunStatus.PAUSED
            sess.message = "Pre-submit verification failed for: " + ", ".join(failed_dom_checks)
            self._persist_history(sess)
            await self._emit(sess)
            return

        # Verify no visible validation errors on page
        val_errors = await self._get_validation_errors(page)
        if val_errors:
            sess.status = RunStatus.PAUSED
            sess.message = "Validation error detected on the form: " + "; ".join(val_errors)
            self._persist_history(sess)
            await self._emit(sess)
            return

        self._advance_stage(sess, ProgressStage.VALIDATION_COMPLETED, "All required fields verified.")
        await self._emit(sess)

        # Check if multi-page "Next" button exists before Submit
        next_btn = await self._find_nav_button(page, "Next")
        if next_btn is not None:
            await next_btn.click(timeout=ACTION_TIMEOUT_MS)
            await page.wait_for_timeout(250)
            await self._wait_for_form_ready(page, timeout_ms=5000)

            val_errors = await self._get_validation_errors(page)
            if val_errors:
                sess.status = RunStatus.PAUSED
                sess.message = "Form validation paused progression: " + "; ".join(val_errors)
                self._persist_history(sess)
                await self._emit(sess)
                return

            sess.current_page_index += 1
            await self._process_current_page(sess)
            return

        # Duplicate click guard within the same session
        if sess.submission_initiated:
            sess.status = RunStatus.UNKNOWN
            sess.message = "Submission was already initiated for this run; refusing duplicate Submit click."
            await self._seal_terminal(sess)
            return

        submit_btn = await self._find_nav_button(page, "Submit")
        if submit_btn is None:
            sess.status = RunStatus.BLOCKED
            sess.message = "Could not locate the Submit button. Complete submission manually."
            await self._seal_terminal(sess)
            return

        sess.submission_initiated = True
        sess.diagnostics["submission_initiated"] = True
        self._advance_stage(sess, ProgressStage.SUBMISSION_INITIATED, "Submitting form automatically...")
        await self._emit(sess)

        await submit_btn.click(timeout=ACTION_TIMEOUT_MS)

        # Wait ONLY for genuine confirmation, validation error, or post-submit blocker (NEVER re-check form rendering!)
        deadline = time.monotonic() + (SUBMIT_TIMEOUT_MS / 1000.0)
        while time.monotonic() < deadline:
            try:
                confirmed, conf_text = await self._check_confirmation(page)
                if confirmed:
                    sess.status = RunStatus.SUBMITTED
                    sess.confirmation_text = conf_text
                    sess.diagnostics["confirmation_detected"] = True
                    sess.diagnostics["confirmation_url"] = page.url.split("?")[0]
                    self._advance_stage(
                        sess,
                        ProgressStage.SUBMISSION_CONFIRMED,
                        f"Registration submitted and confirmed in {sess.elapsed_ms()} ms.",
                    )
                    await self._seal_terminal(sess)
                    return

                val_errors = await self._get_validation_errors(page)
                if val_errors:
                    sess.submission_initiated = False
                    sess.status = RunStatus.PAUSED
                    sess.message = "Google Forms rejected a field during submission: " + "; ".join(val_errors)
                    self._persist_history(sess)
                    await self._emit(sess)
                    return

                blocked, block_status, block_reason = await self._check_post_submit_blocker(page)
                if blocked:
                    sess.status = block_status
                    sess.message = block_reason
                    await self._seal_terminal(sess)
                    return
            except PlaywrightError:
                # Navigation in progress between /viewform and /formResponse
                pass

            await page.wait_for_timeout(150)

        # Final confirmation check after timeout: NEVER blindly resubmit!
        try:
            confirmed, conf_text = await self._check_confirmation(page)
        except PlaywrightError:
            confirmed, conf_text = False, ""

        if confirmed:
            sess.status = RunStatus.SUBMITTED
            sess.confirmation_text = conf_text
            sess.diagnostics["confirmation_detected"] = True
            sess.diagnostics["confirmation_url"] = page.url.split("?")[0]
            self._advance_stage(sess, ProgressStage.SUBMISSION_CONFIRMED, "Registration submitted and confirmed.")
        else:
            sess.status = RunStatus.UNKNOWN
            sess.diagnostics["confirmation_detected"] = False
            sess.diagnostics["final_url"] = page.url.split("?")[0]
            sess.message = (
                "Submission outcome is UNKNOWN after waiting for confirmation. Duplicate protection has blocked "
                "automatic retries — verify whether your response went through before resubmitting."
            )
        await self._seal_terminal(sess)

    async def resume_run(self, run_id: str, answers: list[ResumeAnswerItem]) -> RunSummary:
        sess = self._sessions.get(run_id)
        if not sess:
            raise KeyError("Run session not found or already expired.")
        if sess.status != RunStatus.PAUSED or sess.page is None:
            raise RuntimeError("This run is not currently paused and waiting for input.")

        async with sess.lock:
            sess.status = RunStatus.RUNNING
            sess.message = "Applying your answers..."
            await self._emit(sess)

            page = sess.page
            assert page is not None

            try:
                for item in answers:
                    qa = sess.all_analyses.get(item.question_id)
                    if not qa:
                        continue
                    ok = await self._fill_question_on_page(page, qa, item.answer)
                    qa.proposed_answer = item.answer
                    qa.filled = ok
                    if ok:
                        qa.status = MatchStatus.MATCHED
                        qa.confidence = 1.0
                        qa.reason = "Provided manually by user."
                    else:
                        qa.status = MatchStatus.MISSING
                        qa.reason = "Could not apply the provided answer to this field."

                    if item.save_to_profile and ok:
                        val_str = ", ".join(item.answer) if isinstance(item.answer, list) else str(item.answer)
                        label = (item.profile_field_label or qa.title).strip()
                        self.storage.add_or_update_custom_field(label=label, value=val_str, aliases=[qa.title.lower()])

                # Re-check current page required questions
                current_page_reqs = [
                    qa
                    for qa in sess.all_analyses.values()
                    if qa.page_index == sess.current_page_index and qa.required
                ]
                still_unresolved = [qa for qa in current_page_reqs if qa.status != MatchStatus.MATCHED or not qa.filled]
                if still_unresolved:
                    sess.status = RunStatus.PAUSED
                    sess.message = f"{len(still_unresolved)} required question(s) still need valid input."
                    self._persist_history(sess)
                    await self._emit(sess)
                    return sess.to_summary()

                await self._validate_and_advance_or_submit(sess)
                return sess.to_summary()
            except Exception as exc:
                if sess.status in {
                    RunStatus.SUBMITTED,
                    RunStatus.UNKNOWN,
                    RunStatus.BLOCKED,
                    RunStatus.FAILED,
                    RunStatus.PAUSED,
                }:
                    if sess.status != RunStatus.PAUSED:
                        self._inflight_keys.discard(sess.canonical_key)
                    raise
                profile = self.storage.get_profile()
                safe_err = redact_sensitive(
                    str(exc),
                    [profile.full_name, profile.mobile_number, profile.email, profile.address_locality],
                )
                logger.exception("Resume failed for %s: %s", sess.canonical_key, safe_err)
                if sess.submission_initiated:
                    sess.status = RunStatus.UNKNOWN
                    sess.message = (
                        "Submission outcome is UNKNOWN after an error while resuming. "
                        "Verify whether the response was recorded before retrying."
                    )
                else:
                    sess.status = RunStatus.FAILED
                    sess.message = "Automation failed while resuming the form."
                sess.error_detail = safe_err[:200]
                await self._seal_terminal(sess)
                raise

    async def cancel_run(self, run_id: str) -> None:
        sess = self._sessions.get(run_id)
        if not sess:
            return
        await self._close_session_browser(sess)
        self._inflight_keys.discard(sess.canonical_key)
        if sess.status == RunStatus.PAUSED:
            sess.status = RunStatus.FAILED
            sess.message = "Run cancelled by user."
            self._persist_history(sess)
            await self._emit(sess)
