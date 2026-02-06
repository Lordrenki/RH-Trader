from rh_trader.bot import _extract_explicit_rep_target


def test_extract_explicit_rep_target_requires_direct_mention():
    assert _extract_explicit_rep_target("+rep <@123>") == ("+", 123)
    assert _extract_explicit_rep_target("-rep <@!456> scam") == ("-", 456)


def test_extract_explicit_rep_target_ignores_non_direct_or_missing_mentions():
    assert _extract_explicit_rep_target("+rep thanks") is None
    assert _extract_explicit_rep_target("+rep\n<@123>") is None
    assert _extract_explicit_rep_target("reply +rep") is None
