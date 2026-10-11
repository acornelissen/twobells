"""WCAG contrast between a page's colour tokens, for the tests of every page's palette."""


def luminance(colour):
    """The relative luminance of a "#rrggbb" colour."""
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """The contrast ratio of two "#rrggbb" colours, from 1 to 21."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def assert_contrast(theme, tokens, text_pairs, outline_pairs):
    """Text (`text_pairs`, as (fg, bg) token names) at 4.5:1 or more, and
    outlines and edges (`outline_pairs`) at 3:1 or more, in `tokens`."""
    for pairs, need in ((text_pairs, 4.5), (outline_pairs, 3.0)):
        for fg, bg in pairs:
            ratio = contrast(tokens[fg], tokens[bg])
            assert ratio >= need, f"{theme}: --{fg} on --{bg} is {ratio:.2f}:1"
