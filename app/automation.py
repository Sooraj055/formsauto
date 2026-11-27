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
                        await self._emit(sess)
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

    async def _check_blockers(self, page: Page) -> tuple[bool, str]:
        """Detect Google sign-in, closed forms, CAPTCHAs, or permission blocks."""
        current_url = page.url.lower()
        if "accounts.google.com" in current_url:
            return True, "Google Sign-In is required to access this form. Open it manually in your browser."

        info = await page.evaluate(
            """() => {
                const bodyText = (document.body?.innerText || '').toLowerCase();
                const hasRecaptchaIframe = !!document.querySelector('iframe[src*="recaptcha"], .g-recaptcha, [data-sitekey]');
                const title = document.querySelector('[role="heading"][aria-level="1"], .F9yp7e')?.innerText?.trim() || document.title || '';
                return { bodyText: bodyText.slice(0, 4000), hasRecaptchaIframe, title };
            }"""
        )

        body = info.get("bodyText", "")
        if info.get("hasRecaptchaIframe"):
            return True, "CAPTCHA detected on this form. Please complete verification manually in Chrome."

        if "no longer accepting responses" in body or "this form is closed" in body:
            return True, "This Google Form is closed and no longer accepting responses."

        if "you need permission" in body or "sign in to continue" in body or "sign in to google" in body:
            return True, "Form requires Google Sign-In or special permission. Please open manually."

        return False, ""

    async def _extract_page_questions(self, page: Page, page_index: int) -> tuple[str, list[DetectedQuestion]]:
        """Extract all visible questions, types, required flags, and options on the current page."""
        # Pre-open any Google Forms custom dropdowns without options in DOM so we can inspect choices
        listbox_count = await page.locator('div[role="listitem"] div[role="listbox"]').count()
        for i in range(listbox_count):
            lb = page.locator('div[role="listitem"] div[role="listbox"]').nth(i)
            opt_count = await lb.locator('[role="option"]').count()
            if opt_count == 0:
                try:
                    await lb.click(timeout=1500)
                    await page.wait_for_timeout(120)
                    await page.keyboard.press("Escape")
                except Exception:
                    pass

        raw = await page.evaluate(
            """(pageIndex) => {
                const titleEl = document.querySelector('.F9yp7e, div[role="heading"][aria-level="1"]');
                const formTitle = titleEl ? titleEl.innerText.trim() : (document.title || 'Google Form');

                const items = Array.from(document.querySelectorAll('div[role="listitem"]'));
                const results = [];
                let qIdx = 0;

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
            page_index,
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
                    if any(normalize_text(cand) == norm_target for cand in (dv, al, txt) if cand):
                        await radio.click(timeout=2000)
                        checked = await radio.get_attribute("aria-checked")
                        return checked == "true"
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
                    await r.check(timeout=2000)
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
                        await native_sel.first.select_option(value=val_attr if val_attr else txt)
                        return True
                return False

            listbox = container.locator('div[role="listbox"]').first
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
                    await page.wait_for_timeout(120)
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

    async def _check_confirmation(self, page: Page) -> tuple[bool, str]:
        """Verify genuine Google Forms submission confirmation (never assume click == success)."""
        url = page.url.lower()
        result = await page.evaluate(
            """() => {
                const confEl = document.querySelector(
                    '.vHW8K, .freebirdFormviewerViewResponseConfirmationMessage, [data-response-confirmation]'
                );
                const confText = confEl ? confEl.innerText.trim() : '';
                const another = document.querySelector('a[href*="viewform"]');
                const body = (document.body?.innerText || '').trim();
                const errorAlerts = Array.from(document.querySelectorAll('.RHiWt, [role="alert"], [aria-invalid="true"]'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => (el.innerText || '').trim())
                    .filter(Boolean);
                return {
                    confText,
                    hasAnotherLink: !!another,
                    body: body.slice(0, 1500),
                    errorAlerts,
                };
            }"""
        )

        if result["errorAlerts"]:
            return False, ""

        conf_text = result["confText"]
        body_lower = result["body"].lower()
        if conf_text:
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

                await page.goto(sess.form_url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
                # Give Google Forms JS a brief moment to hydrate controls
                try:
                    await page.wait_for_selector(
                        'div[role="listitem"], form, iframe[src*="recaptcha"]',
                        timeout=5000,
                    )
                except PlaywrightTimeoutError:
                    pass

                blocked, block_reason = await self._check_blockers(page)
                if blocked:
                    sess.status = RunStatus.BLOCKED
                    sess.message = block_reason
                    self._persist_history(sess)
                    await self._emit(sess)
                    await self._close_session_browser(sess)
                    self._inflight_keys.discard(sess.canonical_key)
                    return

                self._advance_stage(sess, ProgressStage.FORM_OPENED, "Form opened successfully.")
                await self._emit(sess)

                await self._process_current_page(sess)

            except PlaywrightTimeoutError as exc:
                logger.warning("Timeout opening form %s: %s", sess.canonical_key, type(exc).__name__)
                sess.status = RunStatus.FAILED
                sess.message = "Timed out while loading the Google Form. Check your connection and try again."
                sess.error_detail = type(exc).__name__
                self._persist_history(sess)
                await self._emit(sess)
                await self._close_session_browser(sess)
                self._inflight_keys.discard(sess.canonical_key)
            except Exception as exc:
                profile = self.storage.get_profile()
                safe_err = redact_sensitive(
                    str(exc),
                    [profile.full_name, profile.mobile_number, profile.email, profile.address_locality],
                )
                logger.exception("Automation run failed for %s: %s", sess.canonical_key, safe_err)
                sess.status = RunStatus.FAILED
                sess.message = "Automation failed while accessing the form. Use manual fallback if needed."
                sess.error_detail = safe_err[:200]
                self._persist_history(sess)
                await self._emit(sess)
                await self._close_session_browser(sess)
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
            self._persist_history(sess)
            await self._emit(sess)
            await self._close_session_browser(sess)
            self._inflight_keys.discard(sess.canonical_key)
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

        # Fill all confidently matched questions immediately
        for qa in analyses:
            if qa.status == MatchStatus.MATCHED and qa.proposed_answer is not None:
                ok = await self._fill_question_on_page(page, qa, qa.proposed_answer)
                qa.filled = ok
                if not ok and qa.required:
                    qa.status = MatchStatus.MISSING
                    qa.reason = "Could not populate this required field automatically."

        self._advance_stage(sess, ProgressStage.FORM_FILLED, "Populated matched answers into the form.")
        await self._emit(sess)

        # Check if any required question on this page is unresolved / unverified
        current_page_items = [qa for qa in analyses]
        unresolved_required = [
            qa
            for qa in current_page_items
            if qa.required and (qa.status != MatchStatus.MATCHED or not qa.filled)
        ]

        if unresolved_required:
            sess.status = RunStatus.PAUSED
            sess.message = (
                f"Paused: {len(unresolved_required)} required question(s) need your input or confirmation."
            )
            self._persist_history(sess)
            await self._emit(sess)
            return

        await self._validate_and_advance_or_submit(sess)

    async def _validate_and_advance_or_submit(self, sess: RunSession) -> None:
        page = sess.page
        assert page is not None

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
            await page.wait_for_timeout(350)

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

        submit_btn = await self._find_nav_button(page, "Submit")
        if submit_btn is None:
            sess.status = RunStatus.BLOCKED
            sess.message = "Could not locate the Submit button. Complete submission manually."
            self._persist_history(sess)
            await self._emit(sess)
            await self._close_session_browser(sess)
            self._inflight_keys.discard(sess.canonical_key)
            return

        self._advance_stage(sess, ProgressStage.SUBMISSION_INITIATED, "Submitting form automatically...")
        await self._emit(sess)

        await submit_btn.click(timeout=ACTION_TIMEOUT_MS)

        # Wait for genuine confirmation or validation error
        deadline = time.monotonic() + (SUBMIT_TIMEOUT_MS / 1000.0)
        while time.monotonic() < deadline:
            try:
                confirmed, conf_text = await self._check_confirmation(page)
                if confirmed:
                    sess.status = RunStatus.SUBMITTED
                    sess.confirmation_text = conf_text
                    self._advance_stage(
                        sess,
                        ProgressStage.SUBMISSION_CONFIRMED,
                        f"Registration submitted and confirmed in {sess.elapsed_ms()} ms.",
                    )
                    self._persist_history(sess)
                    await self._emit(sess)
                    await self._close_session_browser(sess)
                    self._inflight_keys.discard(sess.canonical_key)
                    return

                val_errors = await self._get_validation_errors(page)
                if val_errors:
                    sess.status = RunStatus.PAUSED
                    sess.message = "Google Forms rejected a field during submission: " + "; ".join(val_errors)
                    self._persist_history(sess)
                    await self._emit(sess)
                    return

                blocked, block_reason = await self._check_blockers(page)
                if blocked:
                    sess.status = RunStatus.BLOCKED
                    sess.message = block_reason
                    self._persist_history(sess)
                    await self._emit(sess)
                    await self._close_session_browser(sess)
                    self._inflight_keys.discard(sess.canonical_key)
                    return
            except PlaywrightError:
                # Navigation in progress between /viewform and /formResponse
                pass

            await page.wait_for_timeout(150)

        # Timeout after clicking Submit: do NOT blindly resubmit!
        confirmed, conf_text = await self._check_confirmation(page)
        if confirmed:
            sess.status = RunStatus.SUBMITTED
            sess.confirmation_text = conf_text
            self._advance_stage(sess, ProgressStage.SUBMISSION_CONFIRMED, "Registration submitted and confirmed.")
        else:
            sess.status = RunStatus.UNCERTAIN_TIMEOUT
            sess.message = (
                "Submission timed out after clicking Submit. Duplicate protection has paused retries — "
                "verify whether your response went through before resubmitting."
            )
        self._persist_history(sess)
        await self._emit(sess)
        await self._close_session_browser(sess)
        self._inflight_keys.discard(sess.canonical_key)

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
