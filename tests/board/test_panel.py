from departureboard.board.panel import HDMI_1280X400, PANELS, PHAT, WAVESHARE_10IN85G, Panel


def test_phat_is_drawn_at_one_pixel_per_dot():
    assert (PHAT.size, PHAT.scale, PHAT.epaper) == ((250, 122), 1, True)


def test_bigger_panels_scale_by_whole_pixels_from_their_height():
    assert (WAVESHARE_10IN85G.size, WAVESHARE_10IN85G.scale) == ((1360, 480), 4)
    assert (HDMI_1280X400.size, HDMI_1280X400.scale) == ((1280, 400), 3)
    assert Panel.sized("tiny", 200, 60).scale == 1


def test_the_lcd_is_not_epaper():
    assert WAVESHARE_10IN85G.epaper
    assert not HDMI_1280X400.epaper


def test_registry_names_match():
    assert all(panel.name == name for name, panel in PANELS.items())
    assert set(PANELS) == {"inky-phat", "inky-phat-v1", "waveshare-10in85g", "hdmi-1280x400"}
