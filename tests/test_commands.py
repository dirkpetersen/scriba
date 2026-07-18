from scriba.text.commands import apply_commands


def test_standalone_period_en():
    assert apply_commands("period") == "."


def test_standalone_period_with_whisper_punctuation():
    assert apply_commands("Period.") == "."


def test_trailing_period_en():
    assert apply_commands("let's meet tomorrow period") == "let's meet tomorrow."


def test_trailing_and_period_en():
    assert apply_commands("buy milk and period") == "buy milk."


def test_trailing_comma_en():
    assert apply_commands("eggs milk bread comma") == "eggs milk bread,"


def test_question_mark_en():
    assert apply_commands("are you coming question mark") == "are you coming?"


def test_exclamation_mark_en():
    assert apply_commands("watch out exclamation mark") == "watch out!"


def test_new_line_en():
    assert apply_commands("insert the code new line") == "insert the code\n"


def test_new_paragraph_en():
    assert apply_commands("the end new paragraph") == "the end\n\n"


def test_colon_en():
    assert apply_commands("shopping list colon") == "shopping list:"


def test_hit_enter():
    assert apply_commands("hit enter") == "\n"


def test_hit_enter_trailing():
    assert apply_commands("run the command hit enter") == "run the command\n"


def test_case_insensitive_matching():
    assert apply_commands("HIT ENTER") == "\n"
    assert apply_commands("Let's go Period") == "Let's go."


def test_german_punkt():
    assert apply_commands("wir sehen uns morgen punkt") == "wir sehen uns morgen."


def test_german_und_punkt():
    assert apply_commands("wir sehen uns morgen und punkt") == "wir sehen uns morgen."


def test_german_komma():
    assert apply_commands("eier milch brot komma") == "eier milch brot,"


def test_german_fragezeichen():
    assert apply_commands("kommst du fragezeichen") == "kommst du?"


def test_german_neue_zeile():
    assert apply_commands("erste zeile neue zeile") == "erste zeile\n"


def test_german_neuer_absatz():
    assert apply_commands("ende neuer absatz") == "ende\n\n"


def test_german_doppelpunkt():
    assert apply_commands("einkaufsliste doppelpunkt") == "einkaufsliste:"


def test_literal_word_not_mangled_mid_sentence():
    # "period" used as an ordinary word mid-utterance must survive untouched
    assert apply_commands("the trial period is 30 days") == "the trial period is 30 days"


def test_no_command_present_returns_unchanged():
    assert apply_commands("hello world") == "hello world"


def test_empty_text_returns_unchanged():
    assert apply_commands("") == ""


def test_longest_phrase_preferred_over_shorter_suffix():
    # trailing "and period" must consume "and" too, not leave it dangling
    assert apply_commands("call me and period") == "call me."
