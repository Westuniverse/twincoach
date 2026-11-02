"""GPL-2.0-only byte-exact reference for ACTIVATION-LUT-AXIS v1.

Caller-generated tables own all model quantization semantics. No model, package
or device is loaded here, and a generic LUT is not an RTMPose qualification.
"""

import math


def _validate_fix_points(input_fix_point, output_fix_point):
    for value in (input_fix_point, output_fix_point):
        if type(value) is not int or not -16 <= value <= 16:
            raise ValueError("binary fix points must be integers from -16 through 16")


def _unit_interval_byte(value, output_fix_point):
    integer = math.floor(math.ldexp(value, output_fix_point) + 0.5)
    return min(127, max(-128, integer)) & 255


def generate_sigmoid_lut(*, input_fix_point, output_fix_point):
    """Generate 256 sigmoid bytes with explicit binary fixed-point scales.

    Both positions must be exact integers in [-16, 16]; bool is rejected.
    Address b denotes signed int8(b), and x = signed * 2**(-input_fix_point).
    Quantization uses floor(y * 2**output_fix_point + 0.5), saturates to signed
    int8, then returns its byte bit pattern. This is sigmoid, not SiLU.
    Stable branches keep exp's argument nonpositive, including coarse inputs.
    """
    _validate_fix_points(input_fix_point, output_fix_point)
    # For finite x, 0 < sigmoid(x) < 1. With a negative output position the
    # exact scaled value is below 0.5; do not let a rounded tail of 1 invent a
    # half-way value. All such outputs therefore round to zero.
    if output_fix_point < 0:
        return bytes(256)
    values = []
    for address in range(256):
        signed = address if address < 128 else address - 256
        x = math.ldexp(signed, -input_fix_point)
        if x >= 0:
            y = 1.0 / (1.0 + math.exp(-x))
        else:
            exponential = math.exp(x)
            y = exponential / (1.0 + exponential)
        values.append(_unit_interval_byte(y, output_fix_point))
    return bytes(values)


def generate_clip01_lut(*, input_fix_point, output_fix_point):
    """Generate clamp(x, 0, 1), with the sigmoid generator's byte/scale rules.

    This includes no HardSigmoid affine (x/6 + 0.5). Both binary positions are
    exact integers in [-16, 16]. Ties round toward positive infinity via
    floor_plus_half, followed by signed-int8 saturation.
    """
    _validate_fix_points(input_fix_point, output_fix_point)
    values = []
    for address in range(256):
        signed = address if address < 128 else address - 256
        x = math.ldexp(signed, -input_fix_point)
        values.append(_unit_interval_byte(min(1.0, max(0.0, x)), output_fix_point))
    return bytes(values)


def decode_activation_config(beats):
    """Validate one complete configuration packet of (byte, last) handshakes."""
    entries = tuple(beats)
    if len(entries) != 256:
        raise ValueError("activation config requires exactly 256 beats")
    values = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError("configuration beat must contain byte and last")
        value, last = entry
        if type(value) is not int or not 0 <= value <= 255 or type(last) is not bool:
            raise ValueError("configuration requires a byte and boolean last")
        if last != (index == 255):
            raise ValueError("configuration last must mark only index 255")
        values.append(value)
    return bytes(values)


def apply_activation_lut(table, data, keep=255, last=False):
    """Return an eight-byte mapped beat and unchanged keep/last sidebands."""
    if type(table) is not bytes or len(table) != 256:
        raise ValueError("activation table must be exactly 256 bytes")
    if type(data) is not bytes or len(data) != 8:
        raise ValueError("activation data must be exactly eight bytes")
    if type(keep) is not int or not 0 <= keep <= 255 or type(last) is not bool:
        raise ValueError("keep must be a byte and last boolean")
    mapped = data.translate(table)
    return bytes(value if keep & (1 << lane) else 0
                 for lane, value in enumerate(mapped)), keep, last
