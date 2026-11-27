"""Fast rule-based intelligent question matching and option mapping engine."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import (
    DetectedQuestion,
    MatchStatus,
    QuestionAnalysis,
    QuestionType,
    UserProfile,
)

CONFIDENCE_THRESHOLD = 0.72
AMBIGUITY_DELTA = 0.15

NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


@dataclass(frozen=True)
class FieldRule:
    field_key: str
    field_label: str
    exact_phrases: tuple[str, ...]
    partial_phrases: tuple[str, ...]
    exclude_tokens: tuple[str, ...]


FIELD_RULES: tuple[FieldRule, ...] = (
    FieldRule(
        field_key="full_name",
        field_label="Full name",
        exact_phrases=(
            "full name",
            "your full name",
            "your name",
            "name",
            "applicant name",
            "participant name",
            "candidate name",
            "attendee name",
            "booking name",
            "name of applicant",
            "name of participant",
            "enter your name",
            "first and last name",
            "student name",
            "person name",
        ),
        partial_phrases=("full name", "your name", "applicant name", "participant name", "booking name", "name"),
        exclude_tokens=(
            "father",
            "mother",
            "parent",
            "guardian",
            "spouse",
            "friend",
            "college",
            "school",
            "company",
            "organization",
            "organisation",
            "theatre",
            "theater",
            "cinema",
            "movie",
            "film",
            "show",
            "event",
            "emergency",
            "instagram",
            "twitter",
            "telegram",
            "username",
            "handle",
            "nickname",
            "pet",
            "house",
        ),
    ),
    FieldRule(
        field_key="mobile_number",
        field_label="Mobile number",
        exact_phrases=(
            "mobile number",
            "mobile no",
            "mobile",
            "phone number",
            "phone no",
            "phone",
            "contact number",
            "contact no",
            "whatsapp number",
            "whatsapp no",
            "whatsapp",
            "ph no",
            "mob no",
            "cell number",
            "cell phone",
            "telephone number",
            "telephone",
            "call number",
            "your mobile number",
            "your phone number",
            "your contact number",
        ),
        partial_phrases=(
            "mobile number",
            "mobile no",
            "phone number",
            "phone no",
            "contact number",
            "contact no",
            "whatsapp number",
            "whatsapp no",
            "ph no",
            "mob no",
            "mobile",
            "phone",
            "whatsapp",
            "contact",
        ),
        exclude_tokens=(
            "emergency",
            "alternate",
            "alternative",
            "secondary",
            "backup",
            "parent",
            "guardian",
            "father",
            "mother",
            "friend",
            "office",
            "landline",
        ),
    ),
    FieldRule(
        field_key="email",
        field_label="Email address",
        exact_phrases=(
            "email",
            "email id",
            "email address",
            "e mail",
            "e mail id",
            "e mail address",
            "your email",
            "your email address",
            "mail id",
            "mail address",
        ),
        partial_phrases=("email address", "email id", "e mail", "email", "mail id"),
        exclude_tokens=(
            "alternate",
            "alternative",
            "secondary",
            "recovery",
            "parent",
            "guardian",
            "company",
            "work",
            "friend",
        ),
    ),
    FieldRule(
        field_key="ticket_quantity",
        field_label="Preferred ticket quantity",
        exact_phrases=(
            "how many tickets",
            "how many tickets do you need",
            "how many tickets are you booking",
            "number of tickets",
            "no of tickets",
            "ticket quantity",
            "tickets required",
            "required tickets",
            "ticket count",
            "how many seats",
            "how many seats do you need",
            "how many seats are you booking",
            "number of seats",
            "no of seats",
            "seat quantity",
            "seats required",
            "required seats",
            "how many passes",
            "number of passes",
            "no of passes",
            "how many tokens",
            "number of tokens",
            "no of tokens",
            "tickets",
            "seats",
            "quantity",
            "qty",
        ),
        partial_phrases=(
            "how many tickets",
            "number of tickets",
            "no of tickets",
            "ticket quantity",
            "required tickets",
            "tickets required",
            "ticket count",
            "how many seats",
            "number of seats",
            "no of seats",
            "seat quantity",
            "how many passes",
            "how many tokens",
            "number of tokens",
            "no of tokens",
            "tickets",
            "seats",
        ),
        exclude_tokens=(
            "price",
            "amount",
            "cost",
            "fee",
            "tier",
            "category",
            "type",
            "date",
            "time",
            "timing",
            "theatre",
            "theater",
            "movie",
            "film",
            "show",
            "screen",
        ),
    ),
    FieldRule(
        field_key="place_city",
        field_label="Place / City",
        exact_phrases=(
            "place",
            "city",
            "town",
            "district",
            "location",
            "your place",
            "your city",
            "your district",
            "your location",
            "which place are you from",
            "where are you from",
            "select your district",
            "select your location",
            "select your city",
            "select district",
            "select location",
            "select city",
            "native place",
            "hometown",
            "current city",
            "current place",
            "city town",
            "place city",
        ),
        partial_phrases=(
            "which place are you from",
            "where are you from",
            "select your district",
            "select your location",
            "select your city",
            "native place",
            "hometown",
            "district",
            "location",
            "city",
            "place",
            "town",
        ),
        exclude_tokens=(
            "birth",
            "work",
            "office",
            "college",
            "school",
            "theatre",
            "theater",
            "venue",
            "cinema",
            "pickup",
            "event",
        ),
    ),
    FieldRule(
        field_key="address_locality",
        field_label="Address / Locality",
        exact_phrases=(
            "address",
            "locality",
            "current locality",
            "your locality",
            "your address",
            "full address",
            "residential address",
            "home address",
            "street address",
            "area",
            "area locality",
            "address locality",
            "current address",
            "residence",
            "landmark",
        ),
        partial_phrases=(
            "current locality",
            "residential address",
            "home address",
            "street address",
            "full address",
            "locality",
            "address",
            "area",
        ),
        exclude_tokens=(
            "email",
            "e mail",
            "ip",
            "mac",
            "web",
            "url",
            "office",
            "work",
            "billing",
            "shipping",
        ),
    ),
    FieldRule(
        field_key="pin_code",
        field_label="PIN code",
        exact_phrases=(
            "pin code",
            "pincode",
            "postal code",
            "zip code",
            "zip",
            "pin",
            "post code",
            "your pin code",
            "area pin code",
        ),
        partial_phrases=("pin code", "pincode", "postal code", "zip code", "post code", "zip"),
        exclude_tokens=("atm", "upi", "password", "otp", "passcode", "security", "cvv", "secret"),
    ),
)


def normalize_text(text: str) -> str:
    """Lowercase, remove punctuation/boilerplate tags, and collapse whitespace."""
    if not text:
        return ""
    cleaned = text.lower()
    cleaned = re.sub(r"\((required|optional|mandatory)\)", " ", cleaned)
    cleaned = cleaned.replace("*", " ")
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _get_profile_value(profile: UserProfile, field_key: str) -> str:
    if field_key == "ticket_quantity":
        return str(profile.preferred_ticket_quantity)
    if hasattr(profile, field_key):
        return str(getattr(profile, field_key))
    for cf in profile.custom_fields:
        if cf.key == field_key:
            return cf.value
    return ""


def _score_rule(norm_title: str, norm_desc: str, rule: FieldRule) -> float:
    """Compute a deterministic confidence score [0.0, 1.0] for a built-in FieldRule."""
    if not norm_title:
        return 0.0

    title_tokens = set(norm_title.split())
    for excl in rule.exclude_tokens:
        if re.search(rf"\b{re.escape(excl)}\b", norm_title):
            return 0.0

    if norm_title in rule.exact_phrases:
        return 0.99

    # Check exact phrases after stripping leading question words
    stripped = re.sub(
        r"^(enter|provide|type|write|select|choose|mention|share|your|please enter|kindly enter)\s+",
        "",
        norm_title,
    ).strip()
    if stripped in rule.exact_phrases:
        return 0.95

    best = 0.0
    for phrase in rule.partial_phrases:
        if re.search(rf"\b{re.escape(phrase)}\b", norm_title):
            # Longer multi-word phrases earn higher confidence than single generic tokens
            word_count = len(phrase.split())
            coverage = len(phrase) / max(len(norm_title), 1)
            score = 0.78 + (0.10 if word_count >= 2 else 0.0) + min(0.10, coverage * 0.12)
            # Slightly penalize generic single-word "name" or "place" in long unknown sentences
            if word_count == 1 and len(title_tokens) >= 5:
                score -= 0.14
            best = max(best, min(0.96, score))

    # Secondary check in description if title was generic (e.g. "Contact" + desc "Enter 10 digit mobile")
    if best < 0.72 and norm_desc:
        for phrase in rule.partial_phrases:
            if re.search(rf"\b{re.escape(phrase)}\b", norm_desc):
                best = max(best, 0.74)

    return round(best, 3)


def _score_custom_field(norm_title: str, label: str, aliases: list[str]) -> float:
    candidates = [normalize_text(label), *(normalize_text(a) for a in aliases if a.strip())]
    candidates = [c for c in candidates if c]
    if not candidates:
        return 0.0
    if norm_title in candidates:
        return 0.98
    best = 0.0
    for cand in candidates:
        if re.search(rf"\b{re.escape(cand)}\b", norm_title):
            score = 0.82 + min(0.12, len(cand) / max(len(norm_title), 1) * 0.12)
            best = max(best, score)
    return round(best, 3)


def _extract_integers_from_option(option: str) -> set[int]:
    nums: set[int] = set()
    norm = normalize_text(option)
    for m in re.findall(r"\b(\d+)\b", norm):
        try:
            nums.add(int(m))
        except ValueError:
            pass
    for word, val in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", norm):
            nums.add(val)
    return nums


def match_option_value(
    field_key: str,
    saved_value: str,
    options: list[str],
    question_type: QuestionType,
) -> tuple[str | list[str] | None, MatchStatus, float, str]:
    """Map a saved profile value to available radio/dropdown/checkbox options.

    Returns (mapped_answer, status, confidence, reason).
    """
    if not options:
        return None, MatchStatus.MISSING, 0.0, "No selectable options were detected."

    if field_key == "ticket_quantity":
        try:
            target_qty = int(saved_value)
        except ValueError:
            return None, MatchStatus.MISSING, 0.0, "Invalid preferred ticket quantity in profile."

        matching_opts = [opt for opt in options if target_qty in _extract_integers_from_option(opt)]
        if len(matching_opts) == 1:
            return matching_opts[0], MatchStatus.MATCHED, 0.98, f"Matched option '{matching_opts[0]}' for quantity {target_qty}."
        if len(matching_opts) > 1:
            return (
                None,
                MatchStatus.UNCERTAIN,
                0.55,
                f"Multiple options match quantity {target_qty}: {', '.join(matching_opts)}.",
            )
        return (
            None,
            MatchStatus.MISSING,
            0.0,
            f"Preferred ticket quantity ({target_qty}) is not available in options: {', '.join(options)}.",
        )

    # Multi-value support for checkboxes (comma-separated saved preference)
    if question_type == QuestionType.CHECKBOX:
        parts = [normalize_text(p) for p in saved_value.split(",") if normalize_text(p)]
        if not parts:
            return None, MatchStatus.MISSING, 0.0, "Saved preference is empty."
        selected: list[str] = []
        for opt in options:
            norm_opt = normalize_text(opt)
            for part in parts:
                if norm_opt == part or re.search(rf"\b{re.escape(part)}\b", norm_opt):
                    if opt not in selected:
                        selected.append(opt)
        if selected:
            return selected, MatchStatus.MATCHED, 0.92, f"Matched checkbox option(s): {', '.join(selected)}."
        return None, MatchStatus.MISSING, 0.0, "No checkbox option matches your saved profile value."

    # Single-select string matching (radio / dropdown)
    norm_saved = normalize_text(saved_value)
    if not norm_saved:
        return None, MatchStatus.MISSING, 0.0, "Saved profile value is empty."

    exact_matches = [opt for opt in options if normalize_text(opt) == norm_saved]
    if len(exact_matches) == 1:
        return exact_matches[0], MatchStatus.MATCHED, 0.99, f"Exact match with option '{exact_matches[0]}'."

    word_matches = [
        opt
        for opt in options
        if re.search(rf"\b{re.escape(norm_saved)}\b", normalize_text(opt))
        or (len(normalize_text(opt)) >= 3 and re.search(rf"\b{re.escape(normalize_text(opt))}\b", norm_saved))
    ]
    if len(word_matches) == 1:
        return word_matches[0], MatchStatus.MATCHED, 0.90, f"Matched option '{word_matches[0]}'."
    if len(word_matches) > 1:
        return (
            None,
            MatchStatus.UNCERTAIN,
            0.55,
            f"Multiple options match '{saved_value}': {', '.join(word_matches)}.",
        )

    return (
        None,
        MatchStatus.MISSING,
        0.0,
        f"Saved value does not match any available option ({', '.join(options[:6])}).",
    )


def analyze_question(question: DetectedQuestion, profile: UserProfile) -> QuestionAnalysis:
    """Analyze a single detected Google Form question against the user profile."""
    norm_title = normalize_text(question.title)
    norm_desc = normalize_text(question.description)

    scored: list[tuple[str, str, float]] = []
    for rule in FIELD_RULES:
        score = _score_rule(norm_title, norm_desc, rule)
        if score > 0:
            scored.append((rule.field_key, rule.field_label, score))

    for cf in profile.custom_fields:
        if not cf.value.strip():
            continue
        score = _score_custom_field(norm_title, cf.label, cf.aliases)
        if score > 0:
            scored.append((cf.key, cf.label, score))

    scored.sort(key=lambda item: item[2], reverse=True)

    if not scored or scored[0][2] < 0.45:
        status = MatchStatus.MISSING if question.required else MatchStatus.UNCERTAIN
        reason = (
            "New required question has no matching profile field."
            if question.required
            else "Optional question has no matching profile field; will leave blank."
        )
        return QuestionAnalysis(
            question_id=question.question_id,
            page_index=question.page_index,
            title=question.title,
            question_type=question.question_type,
            required=question.required,
            options=question.options,
            confidence=0.0,
            status=status,
            reason=reason,
        )

    top_key, top_label, top_score = scored[0]

    # Check ambiguity between two distinct high-scoring fields
    if len(scored) >= 2:
        second_key, second_label, second_score = scored[1]
        if second_score >= 0.65 and (top_score - second_score) < AMBIGUITY_DELTA:
            return QuestionAnalysis(
                question_id=question.question_id,
                page_index=question.page_index,
                title=question.title,
                question_type=question.question_type,
                required=question.required,
                options=question.options,
                confidence=top_score,
                status=MatchStatus.UNCERTAIN,
                reason=f"Ambiguous question: could refer to '{top_label}' or '{second_label}'.",
                candidate_fields=[
                    {"field_key": top_key, "field_label": top_label, "value": _get_profile_value(profile, top_key)},
                    {
                        "field_key": second_key,
                        "field_label": second_label,
                        "value": _get_profile_value(profile, second_key),
                    },
                ],
            )

    # Low-confidence match -> uncertain, never guess
    if top_score < CONFIDENCE_THRESHOLD:
        return QuestionAnalysis(
            question_id=question.question_id,
            page_index=question.page_index,
            title=question.title,
            question_type=question.question_type,
            required=question.required,
            options=question.options,
            matched_field=top_key,
            matched_field_label=top_label,
            confidence=top_score,
            status=MatchStatus.UNCERTAIN,
            reason=f"Low confidence match for '{top_label}' ({int(top_score * 100)}%). Confirmation needed.",
            candidate_fields=[
                {"field_key": top_key, "field_label": top_label, "value": _get_profile_value(profile, top_key)}
            ],
        )

    saved_val = _get_profile_value(profile, top_key)
    if not saved_val.strip():
        status = MatchStatus.MISSING if question.required else MatchStatus.UNCERTAIN
        return QuestionAnalysis(
            question_id=question.question_id,
            page_index=question.page_index,
            title=question.title,
            question_type=question.question_type,
            required=question.required,
            options=question.options,
            matched_field=top_key,
            matched_field_label=top_label,
            confidence=top_score,
            status=status,
            reason=f"Matched '{top_label}', but the saved value is empty.",
        )

    # Paragraph questions that match a non-address field shouldn't auto-fabricate long responses
    if question.question_type == QuestionType.PARAGRAPH and top_key not in {"address_locality", "place_city"}:
        # Only auto-fill if exact custom field or address
        if not any(cf.key == top_key for cf in profile.custom_fields):
            return QuestionAnalysis(
                question_id=question.question_id,
                page_index=question.page_index,
                title=question.title,
                question_type=question.question_type,
                required=question.required,
                options=question.options,
                matched_field=top_key,
                matched_field_label=top_label,
                confidence=top_score,
                status=MatchStatus.UNCERTAIN,
                reason="Long-answer question requires confirmation before submitting.",
                candidate_fields=[
                    {"field_key": top_key, "field_label": top_label, "value": saved_val}
                ],
            )

    # Option-based questions
    if question.question_type in {QuestionType.MULTIPLE_CHOICE, QuestionType.DROPDOWN, QuestionType.CHECKBOX}:
        mapped, opt_status, opt_conf, opt_reason = match_option_value(
            top_key, saved_val, question.options, question.question_type
        )
        # If optional checkbox has no match, leave unchecked & continue
        if not question.required and question.question_type == QuestionType.CHECKBOX and opt_status != MatchStatus.MATCHED:
            opt_status = MatchStatus.UNCERTAIN
            opt_reason = "Optional checkbox does not match saved profile; leaving unchecked."

        return QuestionAnalysis(
            question_id=question.question_id,
            page_index=question.page_index,
            title=question.title,
            question_type=question.question_type,
            required=question.required,
            options=question.options,
            matched_field=top_key,
            matched_field_label=top_label,
            proposed_answer=mapped,
            confidence=round(min(top_score, opt_conf), 3) if mapped is not None else top_score,
            status=opt_status,
            reason=opt_reason,
        )

    # Short answer / Paragraph (address)
    return QuestionAnalysis(
        question_id=question.question_id,
        page_index=question.page_index,
        title=question.title,
        question_type=question.question_type,
        required=question.required,
        options=question.options,
        matched_field=top_key,
        matched_field_label=top_label,
        proposed_answer=saved_val,
        confidence=top_score,
        status=MatchStatus.MATCHED,
        reason=f"Confidently matched '{top_label}' ({int(top_score * 100)}%).",
    )


def analyze_questions(questions: list[DetectedQuestion], profile: UserProfile) -> list[QuestionAnalysis]:
    return [analyze_question(q, profile) for q in questions]
