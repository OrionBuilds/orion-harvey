from harvey.brain import US_SPELLING_RULE, with_us_spelling


def test_prompt_gets_the_us_spelling_rule_once():
    once = with_us_spelling("Write a LinkedIn note to Dana.")
    assert once.startswith(US_SPELLING_RULE)
    assert once.endswith("Write a LinkedIn note to Dana.")
    assert with_us_spelling(once) == once
