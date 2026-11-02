"""Integer reference for the texture hardware candidate (not an SR network)."""


def bilinear_rgb8(corners, u: int, v: int, shade: int = 256) -> tuple[tuple[int, int, int], bool]:
    """Four RGB corners: top-left/right, bottom-left/right; fractions /256."""
    if (len(corners) != 4 or any(len(p) != 3 for p in corners)
            or any(type(c) is not int or not 0 <= c <= 255 for p in corners for c in p)
            or type(u) is not int or not 0 <= u <= 255
            or type(v) is not int or not 0 <= v <= 255
            or type(shade) is not int or not 0 <= shade <= 511):
        raise ValueError("four RGB8 corners, U/V uint8 and shade uint9 required")
    if shade > 256:
        return (0, 0, 0), True
    weights = ((256-u)*(256-v), u*(256-v), (256-u)*v, u*v)
    return tuple((sum(p[c]*w for p, w in zip(corners, weights))*shade + (1 << 23)) >> 24
                 for c in range(3)), False
