from twobells.board.names import DISPLAY_PANELS, LOOKS, themes_for
from twobells.board.panel import HDMI_1280X400, HDMI_1920X440, PHAT, PHAT_V1, WAVESHARE_10IN85G
from twobells.board.themes import THEMES


def test_every_display_setting_has_a_panel():
    assert DISPLAY_PANELS == {
        "inky": PHAT,
        "inky-phat-v1": PHAT_V1,
        "waveshare-10in85g": WAVESHARE_10IN85G,
        "hdmi-1280x400": HDMI_1280X400,
        "hdmi-1920x440": HDMI_1920X440,
    }


def test_the_looks_the_settings_offer_are_the_looks_the_board_draws_in_its_order():
    assert list(LOOKS) == list(THEMES)


def test_e_paper_is_offered_every_look_but_the_screen_only_ones():
    assert themes_for(epaper=False) == ("sign", "plain", "tft", "flap")
    assert themes_for(epaper=True) == ("sign", "plain")
