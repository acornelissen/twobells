import pytest
from PIL import Image

from departureboard.panel import HDMI_1280X400
from departureboard.screen import SCREEN_RGB, ScreenDisplay


class FakeScreen:
    def __init__(self):
        self.frames = []

    def blit(self, image):
        self.frames.append(image)


def board_image(display, accent_at=(0, 0)):
    image = Image.new("P", HDMI_1280X400.size, display.palette.black)
    image.putpixel(accent_at, display.palette.accent)
    return image


def test_screen_is_the_1280x400_panel_and_not_epaper():
    display = ScreenDisplay(screen=FakeScreen())
    assert display.panel == HDMI_1280X400
    assert not display.panel.epaper


def test_frames_reach_the_screen_in_screen_colours():
    screen = FakeScreen()
    display = ScreenDisplay(accent="yellow", screen=screen)
    display.show(board_image(display))
    [frame] = screen.frames
    assert frame.mode == "RGB"
    assert frame.getpixel((0, 0)) == SCREEN_RGB["yellow"]
    assert frame.getpixel((1, 0)) == SCREEN_RGB["black"]


def test_red_accent():
    screen = FakeScreen()
    display = ScreenDisplay(accent="red", screen=screen)
    display.show(board_image(display))
    assert screen.frames[0].getpixel((0, 0)) == SCREEN_RGB["red"]


@pytest.mark.parametrize(
    "rotate, size, top_left_lands_at",
    [(90, (400, 1280), (399, 0)), (180, (1280, 400), (1279, 399)), (270, (400, 1280), (0, 1279))],
)
def test_rotation_turns_the_board_clockwise(rotate, size, top_left_lands_at):
    screen = FakeScreen()
    display = ScreenDisplay(rotate=rotate, screen=screen)
    display.show(board_image(display, accent_at=(0, 0)))
    [frame] = screen.frames
    assert frame.size == size
    assert frame.getpixel(top_left_lands_at) == SCREEN_RGB["yellow"]


def test_bad_settings_are_refused():
    with pytest.raises(ValueError, match="rotate"):
        ScreenDisplay(rotate=45, screen=FakeScreen())
    with pytest.raises(ValueError, match="accent"):
        ScreenDisplay(accent="green", screen=FakeScreen())


def test_pygame_screen_shows_a_frame_headless(monkeypatch):
    pygame = pytest.importorskip("pygame")
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    from departureboard.screen import PygameScreen

    screen = PygameScreen()
    try:
        frame = Image.new("RGB", (200, 100), SCREEN_RGB["yellow"])
        screen.blit(frame)
        surface = pygame.display.get_surface()
        width, height = surface.get_size()
        assert tuple(surface.get_at((width // 2, height // 2)))[:3] == SCREEN_RGB["yellow"]
    finally:
        pygame.display.quit()


def test_without_pygame_the_error_says_how_to_install_it(monkeypatch):
    import builtins

    from departureboard.screen import PygameScreen

    real_import = builtins.__import__

    def no_pygame(name, *args, **kwargs):
        if name == "pygame":
            raise ModuleNotFoundError("No module named 'pygame'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pygame)
    with pytest.raises(RuntimeError, match="python3-pygame"):
        PygameScreen()
