# SPDX-License-Identifier: MIT
"""Byte-interval golden for the experimental two-stream Mat row writer."""
from dataclasses import dataclass


@dataclass(frozen=True)
class MatWriteBurst:
    address: int
    words: tuple[int, ...]
    strobes: tuple[int, ...]


def mat_row_write_plan(address: int, payload: bytes) -> tuple[MatWriteBurst, ...]:
    """128-bit aligned INCR writes, <=32 beats and never across a 4KiB boundary.

    Byte addresses and strobes are independent of the RTL reservoir/control logic.
    This is neither a DMA provider nor a production memory-access API.
    """
    if type(address) is not int or not 0 <= address < 1 << 32:
        raise ValueError("32-bit byte address required")
    if type(payload) is not bytes or not 1 <= len(payload) <= 5288:
        raise ValueError("owned row bytes of length 1..5288 required")
    end = address + len(payload)
    if end > 1 << 32:
        raise ValueError("row exceeds 32-bit DMA address range")
    cursor = address & ~15
    bursts = []
    while cursor < end:
        stop = min((end + 15) & ~15, cursor + 32 * 16, (cursor & ~4095) + 4096)
        words, strobes = [], []
        for beat in range(cursor, stop, 16):
            word, strobe = 0, 0
            for lane in range(16):
                position = beat + lane
                if address <= position < end:
                    word |= payload[position - address] << (lane * 8)
                    strobe |= 1 << lane
            words.append(word)
            strobes.append(strobe)
        bursts.append(MatWriteBurst(cursor, tuple(words), tuple(strobes)))
        cursor = stop
    return tuple(bursts)
