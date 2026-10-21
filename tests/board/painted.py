"""The rows a plain, TFT or split-flap board paints, each with the layout it
was given: for tests that check where things go from the layout objects."""

import itertools
from unittest import mock

from twobells.board import render, splitflap, tft
from twobells.board.render import PAD, SCREEN_PALETTE, STRIP_WIDTH, Fonts, RowLayout, drawn_length
from twobells.board.view import BoardView, Row


def painted_rows(frame, view: BoardView, panel) -> list[tuple[Row, RowLayout]]:
    """Each row `frame` (board_frame, tft_frame or split_flap_frame) paints
    for `view` on `panel`, in order, with its RowLayout."""
    found: list[tuple[Row, RowLayout]] = []
    real = render.row_layout

    def recording(row, *args, **kwargs):
        layout = real(row, *args, **kwargs)
        found.append((row, layout))
        return layout

    with (mock.patch.object(render, "row_layout", recording), mock.patch.object(tft, "row_layout", recording),
          mock.patch.object(splitflap, "row_layout", recording)):
        frame(view, SCREEN_PALETTE, panel)
    return found


def row_boxes(row: Row, layout: RowLayout, fonts: Fonts, scale: int) -> list[tuple[str, float, float]]:
    """(what, start, end) of each thing on a row, in the order a row is meant
    to run: its time or route, a bus's stop, the destination, the little
    line, the where words, then the platform, leave time and status."""
    boxes = [("left", PAD * scale, PAD * scale + drawn_length(fonts.bold, layout.left))]
    if layout.stop_x is not None and layout.stop:
        boxes.append(("stop", layout.stop_x, layout.stop_x + drawn_length(fonts.regular, layout.stop)))
    boxes.append(("destination", layout.middle_x, layout.middle_x + drawn_length(fonts.regular, layout.middle)))
    if layout.strip_x is not None:
        boxes.append(("strip", layout.strip_x, layout.strip_x + STRIP_WIDTH * scale))
    if layout.where_x is not None and layout.where:
        boxes.append(("where", layout.where_x, layout.where_x + drawn_length(fonts.small, layout.where)))
    if layout.platform_x is not None and row.platform:
        boxes.append(("platform", layout.platform_x, layout.platform_x + drawn_length(fonts.regular, row.platform)))
    if layout.leave_x is not None and layout.leave:
        boxes.append(("leave", layout.leave_x, layout.leave_x + drawn_length(fonts.small, layout.leave)))
    status_x = layout.right_x - fonts.pill
    boxes.append(("status", status_x, layout.right_x + drawn_length(fonts.bold, row.right) + fonts.pill))
    return boxes


def assert_in_order(row: Row, layout: RowLayout, fonts: Fonts, scale: int) -> None:
    """Each thing on the row starts clear of the one before it, in row_boxes' order."""
    boxes = row_boxes(row, layout, fonts, scale)
    for (before, _, end), (after, start, _) in itertools.pairwise(boxes):
        assert end < start, (row.left, before, after, end, start)
