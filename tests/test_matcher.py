"""Unit tests for the rule-based intelligent question matching engine."""

from app.matcher import analyze_question
from app.models import (
    CustomProfileField,
    DetectedQuestion,
    MatchStatus,
    QuestionType,
    UserProfile,
)


def _sample_profile() -> UserProfile:
    return UserProfile(
        full_name="Test User",
        mobile_number="9876543210",
        email="testuser@example.com",
        preferred_ticket_quantity=4,
        place_city="Cochin",
        address_locality="Sample Locality",
        pin_code="682001",
        custom_fields=[
            CustomProfileField(
                key="meal_pref",
                label="Snacks Preference",
                value="Popcorn, Nachos",
                aliases=["snacks", "refreshments"],
            )
        ],
    )


def test_short_answer_synonyms_match_confidently() -> None:
    profile = _sample_profile()
    cases = [
        ("Your full name?", "full_name", "Test User"),
        ("Contact number", "mobile_number", "9876543210"),
        ("Mobile no.", "mobile_number", "9876543210"),
        ("Email ID", "email", "testuser@example.com"),
        ("Which place are you from?", "place_city", "Cochin"),
        ("Current locality", "address_locality", "Sample Locality"),
        ("Postal code", "pin_code", "682001"),
        ("How many seats are you booking?", "ticket_quantity", "4"),
    ]
    for idx, (title, expected_field, expected_val) in enumerate(cases):
        q = DetectedQuestion(
            question_id=f"q_{idx}",
            title=title,
            question_type=QuestionType.SHORT_ANSWER,
            required=True,
        )
        res = analyze_question(q, profile)
        assert res.status == MatchStatus.MATCHED, f"Failed for {title}: {res.reason}"
        assert res.matched_field == expected_field
        assert res.proposed_answer == expected_val


def test_radio_ticket_quantity_selects_four_and_pauses_when_unavailable() -> None:
    profile = _sample_profile()

    for label_set, expected in (
        (["1", "2", "3", "4"], "4"),
        (["1 Ticket", "2 Tickets", "3 Tickets", "4 Tickets", "5 Tickets"], "4 Tickets"),
        (["One ticket", "Two tickets", "Three tickets", "Four tickets"], "Four tickets"),
    ):
        q_avail = DetectedQuestion(
            question_id="q_mc_1",
            title="How many tickets do you need?",
            question_type=QuestionType.MULTIPLE_CHOICE,
            required=True,
            options=label_set,
        )
        res_avail = analyze_question(q_avail, profile)
        assert res_avail.status == MatchStatus.MATCHED
        assert res_avail.proposed_answer == expected

    # Must NEVER silently pick 3 Tickets when 4 is missing
    q_missing = DetectedQuestion(
        question_id="q_mc_2",
        title="No. of tickets",
        question_type=QuestionType.MULTIPLE_CHOICE,
        required=True,
        options=["1 Ticket", "2 Tickets", "3 Tickets"],
    )
    res_missing = analyze_question(q_missing, profile)
    assert res_missing.status == MatchStatus.MISSING
    assert res_missing.proposed_answer is None


def test_dropdown_location_matching() -> None:
    profile = _sample_profile()
    q = DetectedQuestion(
        question_id="q_dd",
        title="Select your district",
        question_type=QuestionType.DROPDOWN,
        required=True,
        options=["Thrissur", "Cochin", "Kottayam", "Kozhikode"],
    )
    res = analyze_question(q, profile)
    assert res.status == MatchStatus.MATCHED
    assert res.proposed_answer == "Cochin"


def test_checkbox_selects_only_known_preferences() -> None:
    profile = _sample_profile()
    q = DetectedQuestion(
        question_id="q_cb_1",
        title="Refreshments",
        question_type=QuestionType.CHECKBOX,
        required=False,
        options=["Popcorn", "Coke", "Nachos", "Water"],
    )
    res = analyze_question(q, profile)
    assert res.status == MatchStatus.MATCHED
    assert res.proposed_answer == ["Popcorn", "Nachos"]

    # Arbitrary checkbox should NOT auto-select every option
    q_unknown = DetectedQuestion(
        question_id="q_cb_2",
        title="Which show timings can you attend?",
        question_type=QuestionType.CHECKBOX,
        required=True,
        options=["6 PM", "9 PM", "11 PM"],
    )
    res_unk = analyze_question(q_unknown, profile)
    assert res_unk.status == MatchStatus.MISSING
    assert res_unk.proposed_answer is None


def test_exclusions_and_ambiguity_and_optional_questions() -> None:
    profile = _sample_profile()

    # "Father Name" must NOT match full_name
    q_father = DetectedQuestion(
        question_id="q_ex1",
        title="Father's Name",
        question_type=QuestionType.SHORT_ANSWER,
        required=True,
    )
    assert analyze_question(q_father, profile).status == MatchStatus.MISSING

    # "Alternate phone number" must NOT silently reuse primary mobile
    q_alt = DetectedQuestion(
        question_id="q_ex2",
        title="Alternate phone number",
        question_type=QuestionType.SHORT_ANSWER,
        required=False,
    )
    assert analyze_question(q_alt, profile).status == MatchStatus.UNCERTAIN

    # Conflicting field names -> uncertain ambiguity
    q_amb = DetectedQuestion(
        question_id="q_amb",
        title="City / Address",
        question_type=QuestionType.SHORT_ANSWER,
        required=True,
    )
    res_amb = analyze_question(q_amb, profile)
    assert res_amb.status == MatchStatus.UNCERTAIN
    assert len(res_amb.candidate_fields) == 2

    # Paragraph unknown -> pause if required, skip if optional
    q_para = DetectedQuestion(
        question_id="q_para",
        title="Why do you want to attend this premiere?",
        question_type=QuestionType.PARAGRAPH,
        required=True,
    )
    assert analyze_question(q_para, profile).status == MatchStatus.MISSING
