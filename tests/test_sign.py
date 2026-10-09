from departureboard.dotfont import DotFont
from departureboard.render import HEIGHT, WIDTH, Palette
from departureboard.sign import MARGIN, render_sign, sign_layout
from departureboard.view import BoardView, Row, Section, Style

PALETTE = Palette(white=0, black=1, accent=2)
FONT = DotFont()


def board(trains=(), buses=(), leave=None, stale_trains=False, stale_buses=False):
    return BoardView(
        title="Glossop > Manchester Picc",
        trains=Section("Trains", tuple(trains), "No trains", stale_trains, leave),
        buses=Section("Buses", tuple(buses), "No buses", stale_buses),
        updated="07:03",
    )


TRAINS = [
    Row("07:12", "Manchester Piccadilly", "On time"),
    Row("07:42", "Manchester Piccadilly", "Exp 07:49", Style.ALERT),
    Row("08:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED),
]
BUSES = [
    Row("341", "Hyde", "07:24 +4", Style.ALERT, "leave 07:17", "A"),
    Row("394", "Stepping Hill Hospital via Marple and Romiley", "07:35 sch", Style.NORMAL, "leave 07:28", "B"),
]


def texts(items):
    return [i.text for i in items]


def find(items, text):
    [item] = [i for i in items if i.text == text]
    return item


def test_trains_are_numbered_like_a_station_board():
    items = sign_layout(board(TRAINS)).items
    assert {"1st", "2nd", "3rd"} <= set(texts(items))


def test_ordinal_time_destination_and_status_share_a_line():
    items = sign_layout(board(TRAINS)).items
    first = find(items, "1st")
    line = [i for i in items if i.y == first.y]
    assert texts(line) == ["1st", "07:12", "Manchester Piccadilly", "On time"]


def test_status_is_right_aligned():
    items = sign_layout(board(TRAINS)).items
    status = find(items, "On time")
    assert status.x + FONT.getlength("On time") == WIDTH - MARGIN


def test_train_times_line_up():
    items = sign_layout(board(TRAINS)).items
    assert len({find(items, t).x for t in ("07:12", "07:42", "08:12")}) == 1


def test_leave_line_sits_under_the_first_train_aligned_with_the_times():
    items = sign_layout(board(TRAINS, leave="Leave by 07:05 for the 07:12")).items
    leave = find(items, "Leave by 07:05 for the 07:12")
    assert find(items, "1st").y < leave.y < find(items, "2nd").y
    assert leave.x == find(items, "07:12").x


def test_cancelled_row_is_inverted():
    items = sign_layout(board(TRAINS)).items
    assert find(items, "Cancelled").inverted
    assert not find(items, "On time").inverted


def test_bus_rows_show_route_stop_destination_leave_and_time():
    items = sign_layout(board(buses=BUSES)).items
    route = find(items, "341")
    line = [i for i in items if i.y == route.y]
    assert texts(line) == ["341", "A", "Hyde", "leave 07:17", "07:24 +4"]


def test_bus_leave_times_line_up_and_long_destinations_give_way():
    items = sign_layout(board(buses=BUSES)).items
    assert find(items, "leave 07:17").x == find(items, "leave 07:28").x
    dest = next(i for i in items if i.text.startswith("Stepping"))
    assert dest.text.endswith("…")
    assert dest.x + FONT.getlength(dest.text) < find(items, "leave 07:28").x


def test_empty_sections_say_so():
    assert {"No trains", "No buses"} <= set(texts(sign_layout(board()).items))


def test_footer_shows_last_update_not_a_ticking_clock():
    footer = find(sign_layout(board()).items, "Updated 07:03")
    assert footer.y + 9 <= HEIGHT


def test_footer_names_feeds_that_have_stopped():
    items = texts(sign_layout(board(stale_trains=True, stale_buses=True)).items)
    assert "Not updating: trains, buses" in items
    assert "Not updating: buses" in texts(sign_layout(board(stale_buses=True)).items)


def test_everything_fits_on_the_panel():
    items = sign_layout(board(TRAINS, BUSES, leave="Leave by 07:05 for the 07:12", stale_buses=True)).items
    for item in items:
        assert 0 <= item.x and item.x + FONT.getlength(item.text) <= WIDTH
        assert 0 <= item.y and item.y + 9 <= HEIGHT


def test_rendered_board_is_amber_on_black():
    image = render_sign(board(TRAINS, BUSES), PALETTE)
    counts = {c: n for n, c in image.getcolors()}
    assert image.size == (WIDTH, HEIGHT)
    assert counts[PALETTE.black] > 0.6 * WIDTH * HEIGHT
    assert PALETTE.accent in counts
    assert PALETTE.white not in counts


def test_mono_panel_falls_back_to_white_text():
    mono = Palette(white=0, black=1, accent=1)
    counts = {c: n for n, c in render_sign(board(TRAINS), mono).getcolors()}
    assert mono.white in counts
