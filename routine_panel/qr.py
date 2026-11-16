"""QR codes with Python's standard library only: byte mode, error correction level M, versions 1 to 15 (up to 412
bytes), the mask chosen by the standard's penalty rules (ISO/IEC 18004). Enough for the panel's pairing code, which
is about a hundred characters.

    matrix("routinepanel://pair?…")  -> list of rows of booleans (True = dark), without the quiet zone
    svg("…")                          -> an SVG picture with the quiet zone, black on white"""

from __future__ import annotations

# level M, per version: (EC codewords per block, [(blocks, data codewords per block), …])
_BLOCKS_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]), 11: (30, [(1, 50), (4, 51)]), 12: (22, [(6, 36), (2, 37)]),
    13: (22, [(8, 37), (1, 38)]), 14: (24, [(4, 40), (5, 41)]), 15: (24, [(5, 41), (5, 42)]),
}
_ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
    9: [6, 26, 46], 10: [6, 28, 50], 11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62], 14: [6, 26, 46, 66],
    15: [6, 26, 48, 70],
}
_EC_M = 0b00  # level M's two bits in the format information


class QrTooLong(ValueError):
    pass


# --- Reed–Solomon over GF(256), polynomial 0x11D ---------------------------------------------------------------------
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        poly = [a ^ b for a, b in zip(poly + [0], [0] + [_mul(c, _EXP[i]) for c in poly])]
    return poly


def _ec_codewords(data: list[int], degree: int) -> list[int]:
    gen = _generator(degree)
    rest = list(data) + [0] * degree
    for i in range(len(data)):
        factor = rest[i]
        if factor:
            for j, g in enumerate(gen):
                rest[i + j] ^= _mul(g, factor)
    return rest[len(data):]


# --- the data -------------------------------------------------------------------------------------------------------
def _capacity(version: int) -> int:
    _, groups = _BLOCKS_M[version]
    return sum(count * size for count, size in groups)


def _choose_version(length: int) -> int:
    for version in range(1, 16):
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * length <= 8 * _capacity(version):
            return version
    raise QrTooLong(f"{length} bytes do not fit in a version 15-M QR code")


def _codewords(payload: bytes, version: int) -> list[int]:
    count_bits = 8 if version < 10 else 16
    bits: list[int] = []

    def put(value: int, width: int) -> None:
        bits.extend((value >> (width - 1 - i)) & 1 for i in range(width))

    put(0b0100, 4)  # byte mode
    put(len(payload), count_bits)
    for byte in payload:
        put(byte, 8)
    total_bits = 8 * _capacity(version)
    put(0, min(4, total_bits - len(bits)))  # terminator
    if len(bits) % 8:
        put(0, 8 - len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < _capacity(version):
        data.append(pad)
        pad = 0x11 if pad == 0xEC else 0xEC

    ec_size, groups = _BLOCKS_M[version]
    blocks, ecs = [], []
    position = 0
    for count, size in groups:
        for _ in range(count):
            block = data[position:position + size]
            position += size
            blocks.append(block)
            ecs.append(_ec_codewords(block, ec_size))
    out = []
    for i in range(max(len(b) for b in blocks)):
        out.extend(b[i] for b in blocks if i < len(b))
    for i in range(ec_size):
        out.extend(e[i] for e in ecs)
    return out


# --- the picture ----------------------------------------------------------------------------------------------------
def _bch(value: int, generator: int, top_bit: int) -> int:
    value <<= top_bit
    while value.bit_length() > top_bit:
        value ^= generator << (value.bit_length() - generator.bit_length())
    return value


def _format_bits(mask: int) -> int:
    data = (_EC_M << 3) | mask
    return ((data << 10) | _bch(data, 0x537, 10)) ^ 0x5412


def _version_bits(version: int) -> int:
    return (version << 12) | _bch(version, 0x1F25, 12)


_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


class _Grid:
    def __init__(self, version: int):
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set(self, r: int, c: int, dark: bool) -> None:
        self.dark[r][c] = dark
        self.fixed[r][c] = True

    def functions(self) -> None:
        n = self.size
        for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):  # finders with their separators
            for dr in range(-1, 8):
                for dc in range(-1, 8):
                    r, c = r0 + dr, c0 + dc
                    if 0 <= r < n and 0 <= c < n:
                        ring = max(abs(dr - 3), abs(dc - 3))
                        self.set(r, c, ring != 2 and ring != 4)
        for i in range(8, n - 8):  # timing
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        centres = _ALIGNMENT[self.version]
        corners = {(centres[0], centres[0]), (centres[0], centres[-1]), (centres[-1], centres[0])} if centres else set()
        for r in centres:
            for c in centres:
                if (r, c) in corners:
                    continue  # where the finders are (those on the timing lines are drawn over them)
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        self.set(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
        self.set(n - 8, 8, True)  # the dark module
        self.format(0)  # reserves the format areas; written again with the chosen mask
        if self.version >= 7:
            bits = _version_bits(self.version)
            for i in range(18):
                dark = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self.set(a, b, dark)
                self.set(b, a, dark)

    def format(self, mask: int) -> None:
        n = self.size
        bits = _format_bits(mask)
        for i in range(15):
            dark = (bits >> i) & 1 == 1
            # around the top-left finder
            if i < 6:
                self.set(i, 8, dark)
            elif i < 8:
                self.set(i + 1, 8, dark)
            else:
                self.set(8, 14 - i if i > 8 else 7, dark)
            # beside the other two finders
            if i < 8:
                self.set(8, n - 1 - i, dark)
            else:
                self.set(n - 15 + i, 8, dark)
        self.set(n - 8, 8, True)

    def place(self, codewords: list[int]) -> None:
        bits = [(byte >> (7 - i)) & 1 for byte in codewords for i in range(8)]
        n = self.size
        index = 0
        column = n - 1
        upward = True
        while column > 0:
            if column == 6:
                column -= 1
            rows = range(n - 1, -1, -1) if upward else range(n)
            for r in rows:
                for c in (column, column - 1):
                    if not self.fixed[r][c]:
                        self.dark[r][c] = index < len(bits) and bits[index] == 1
                        index += 1
            upward = not upward
            column -= 2

    def masked(self, mask: int) -> list[list[bool]]:
        test = _MASKS[mask]
        return [[self.dark[r][c] != (not self.fixed[r][c] and test(r, c)) for c in range(self.size)]
                for r in range(self.size)]


def _penalty(m: list[list[bool]]) -> int:
    n = len(m)
    score = 0
    lines = m + [list(col) for col in zip(*m)]
    for line in lines:  # rule 1: runs of five or more
        run = 1
        for i in range(1, n):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
    for r in range(n - 1):  # rule 2: 2×2 blocks
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    pattern_a = [True, False, True, True, True, False, True, False, False, False, False]
    pattern_b = pattern_a[::-1]
    for line in lines:  # rule 3: finder-like patterns
        for i in range(n - 10):
            window = line[i:i + 11]
            if window == pattern_a or window == pattern_b:
                score += 40
    dark = sum(sum(row) for row in m)  # rule 4: balance of dark and light, 10 points per 5 % away from half
    total = n * n
    score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
    return score


def matrix(text: str | bytes, version: int | None = None, mask: int | None = None) -> list[list[bool]]:
    payload = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    chosen = version or _choose_version(len(payload))
    if chosen not in _BLOCKS_M:
        raise QrTooLong(f"version {chosen} is not supported")
    grid = _Grid(chosen)
    grid.functions()
    grid.place(_codewords(payload, chosen))
    best = None
    for candidate in ([mask] if mask is not None else range(8)):
        grid.format(candidate)
        result = grid.masked(candidate)
        score = _penalty(result)
        if best is None or score < best[0]:
            best = (score, candidate, result)
    grid.format(best[1])
    return grid.masked(best[1])


def svg(text: str | bytes, quiet: int = 4, module: int = 8) -> str:
    """An SVG of the code: black squares on white, with `quiet` squares of white around it, as scanners need."""
    rows = matrix(text)
    n = len(rows) + 2 * quiet
    path = []
    for r, row in enumerate(rows):
        c = 0
        while c < len(row):
            if row[c]:
                start = c
                while c < len(row) and row[c]:
                    c += 1
                path.append(f"M{start + quiet} {r + quiet}h{c - start}v1h-{c - start}z")
            else:
                c += 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * module}" '
            f'height="{n * module}" shape-rendering="crispEdges"><rect width="{n}" height="{n}" fill="#fff"/>'
            f'<path fill="#000" d="{"".join(path)}"/></svg>')
