from PIL import Image

from departureboard.display import InkyDisplay, PngDisplay


class FakeInky:
    WHITE, BLACK, YELLOW, RED = 1, 0, 2, 3

    def __init__(self):
        self.image = None
        self.shown = 0

    def set_image(self, image):
        self.image = image

    def show(self):
        self.shown += 1


class FakeMonoInky:
    WHITE, BLACK = 0, 1

    def set_image(self, image):
        pass

    def show(self):
        pass


def test_inky_palette_uses_device_indices():
    display = InkyDisplay(accent="red", device=FakeInky())
    assert (display.palette.white, display.palette.black, display.palette.accent) == (1, 0, 3)


def test_inky_yellow_accent():
    assert InkyDisplay(accent="yellow", device=FakeInky()).palette.accent == 2


def test_mono_panel_falls_back_to_black_accent():
    assert InkyDisplay(accent="red", device=FakeMonoInky()).palette.accent == 1


def test_inky_show_pushes_image():
    device = FakeInky()
    image = Image.new("P", (250, 122))
    InkyDisplay(accent="red", device=device).show(image)
    assert device.image is image
    assert device.shown == 1


def test_inky_can_rotate_for_upside_down_mounting():
    device = FakeInky()
    image = Image.new("P", (250, 122), 0)
    image.putpixel((0, 0), 3)
    InkyDisplay(accent="red", device=device, rotate_180=True).show(image)
    assert device.image.getpixel((249, 121)) == 3


def test_png_display_writes_preview(tmp_path):
    path = tmp_path / "preview.png"
    display = PngDisplay(path, accent="red")
    display.show(Image.new("P", (250, 122), display.palette.white))
    assert Image.open(path).size == (250, 122)


def test_inky_phat_is_the_default_panel():
    from departureboard.panel import PHAT

    assert InkyDisplay(device=FakeInky()).panel == PHAT


def test_other_inky_sizes_are_drawn_at_their_own_size():
    device = FakeInky()
    device.resolution = (400, 300)  # Inky wHAT
    panel = InkyDisplay(device=device).panel
    assert (panel.size, panel.scale, panel.epaper) == ((400, 300), 2, True)


def test_png_display_can_preview_another_panel(tmp_path):
    from departureboard.panel import WAVESHARE_10IN85G

    display = PngDisplay(tmp_path / "big.png", panel=WAVESHARE_10IN85G)
    assert display.panel == WAVESHARE_10IN85G


def test_open_display_refuses_an_unknown_name():
    import pytest

    from departureboard.display import open_display

    with pytest.raises(ValueError, match="inky"):
        open_display("crt", "red")


def test_every_display_setting_has_a_panel():
    from departureboard.display import DISPLAY_PANELS
    from departureboard.panel import HDMI_1280X400, PHAT, WAVESHARE_10IN85G

    assert DISPLAY_PANELS == {"inky": PHAT, "waveshare-10in85g": WAVESHARE_10IN85G, "hdmi-1280x400": HDMI_1280X400}
