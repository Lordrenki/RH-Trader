from rh_trader.bot import _extract_explicit_rep_target


def test_extract_explicit_rep_target_accepts_text_rep_commands():
    assert _extract_explicit_rep_target("+rep <@123>") == ("+", 123)
    assert _extract_explicit_rep_target("+rep <@!456> thanks") == ("+", 456)
    assert _extract_explicit_rep_target("rep <@789>") == ("check", 789)


def test_extract_explicit_rep_target_ignores_non_direct_or_removed_commands():
    assert _extract_explicit_rep_target("+rep thanks") is None
    assert _extract_explicit_rep_target("+rep\n<@123>") is None
    assert _extract_explicit_rep_target("reply +rep <@123>") is None
    assert _extract_explicit_rep_target("-rep <@456> scam") is None
