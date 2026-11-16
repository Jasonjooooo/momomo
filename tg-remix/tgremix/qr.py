"""
极简二维码生成器（字节模式、M 级纠错、版本 1–13），只用标准库，输出 SVG。
用于 Telegram 扫码登录。算法参考 Project Nayuki 的 QR Code generator（MIT）。
"""
from __future__ import annotations

# M 级纠错：每块纠错码字数、块数（版本 1–13）
_ECC_PER_BLOCK = [None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22]
_NUM_BLOCKS = [None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9]
_FORMAT_ECL_M = 0


def _raw_modules(ver: int) -> int:
    r = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        r -= (25 * n - 10) * n - 55
        if ver >= 7:
            r -= 36
    return r


def _data_capacity_bits(ver: int) -> int:
    return (_raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]) * 8


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(ver: int, size: int) -> list[int]:
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
    res = [size - 7 - i * step for i in range(n - 1)]
    return [6] + sorted(res)


class _QR:
    def __init__(self, ver: int, codewords: list[int]):
        self.ver = ver
        self.size = ver * 4 + 17
        n = self.size
        self.m = [[False] * n for _ in range(n)]
        self.fn = [[False] * n for _ in range(n)]
        self._draw_function_patterns()
        self._draw_codewords(codewords)
        best, best_pen = 0, None
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format(mask)
            pen = self._penalty()
            if best_pen is None or pen < best_pen:
                best, best_pen = mask, pen
            self._apply_mask(mask)  # 撤销（异或两次）
        self._apply_mask(best)
        self._draw_format(best)

    def _set_fn(self, x: int, y: int, dark: bool) -> None:
        self.m[y][x] = dark
        self.fn[y][x] = True

    def _draw_function_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self._set_fn(6, i, i % 2 == 0)
            self._set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self._set_fn(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment_positions(self.ver, n)
        k = len(pos)
        for i in range(k):
            for j in range(k):
                if (i == 0 and j == 0) or (i == 0 and j == k - 1) or (i == k - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set_fn(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self._set_fn(a, b, bit)
                self._set_fn(b, a, bit)

    def _draw_format(self, mask: int) -> None:
        n = self.size
        data = _FORMAT_ECL_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412

        def bit(i):
            return (bits >> i) & 1 == 1

        for i in range(6):
            self._set_fn(8, i, bit(i))
        self._set_fn(8, 7, bit(6))
        self._set_fn(8, 8, bit(7))
        self._set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self._set_fn(14 - i, 8, bit(i))
        for i in range(8):
            self._set_fn(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set_fn(8, n - 15 + i, bit(i))
        self._set_fn(8, n - 8, True)

    def _draw_codewords(self, data: list[int]) -> None:
        n = self.size
        i = 0
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < len(data) * 8:
                        self.m[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def _apply_mask(self, mask: int) -> None:
        f = [
            lambda x, y: (x + y) % 2 == 0,
            lambda x, y: y % 2 == 0,
            lambda x, y: x % 3 == 0,
            lambda x, y: (x + y) % 3 == 0,
            lambda x, y: (x // 3 + y // 2) % 2 == 0,
            lambda x, y: x * y % 2 + x * y % 3 == 0,
            lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
        ][mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(x, y):
                    self.m[y][x] = not self.m[y][x]

    def _penalty(self) -> int:
        n, m = self.size, self.m
        pen = 0
        for lines in (m, [list(c) for c in zip(*m)]):
            for row in lines:
                run, prev = 0, None
                for v in row:
                    if v == prev:
                        run += 1
                    else:
                        if run >= 5:
                            pen += run - 2
                        run, prev = 1, v
                if run >= 5:
                    pen += run - 2
                s = "".join("1" if v else "0" for v in row)
                pen += 40 * (s.count("10111010000") + s.count("00001011101"))
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    pen += 3
        dark = sum(v for row in m for v in row)
        total = n * n
        pen += abs(dark * 100 // total - 50) // 5 * 10
        return pen


def encode(text: str) -> list[list[bool]]:
    data = text.encode("utf-8")
    for ver in range(1, len(_ECC_PER_BLOCK)):
        cc_bits = 8 if ver <= 9 else 16
        if 4 + cc_bits + len(data) * 8 <= _data_capacity_bits(ver):
            break
    else:
        raise ValueError("内容太长，无法生成二维码")
    cap = _data_capacity_bits(ver)
    bits: list[int] = []

    def put(val: int, n: int) -> None:
        bits.extend((val >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)
    put(len(data), cc_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    # 分块 + 纠错码 + 交错
    nblocks, ecc_len = _NUM_BLOCKS[ver], _ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    nshort = nblocks - raw % nblocks
    short_len = raw // nblocks
    div = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(nblocks):
        dlen = short_len - ecc_len + (0 if i < nshort else 1)
        dat = words[k:k + dlen]
        k += dlen
        ecc = _rs_remainder(dat, div)
        if i < nshort:
            dat = dat + [None]
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if blk[i] is not None:
                out.append(blk[i])
    assert len(out) == raw
    return _QR(ver, out).m


def to_svg(text: str, scale: int = 8, border: int = 4, dark: str = "#000", light: str = "#fff") -> str:
    m = encode(text)
    n = len(m)
    size = (n + border * 2) * scale
    parts = [f"M{(x + border) * scale},{(y + border) * scale}h{scale}v{scale}h-{scale}z"
             for y in range(n) for x in range(n) if m[y][x]]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
            f'shape-rendering="crispEdges"><rect width="100%" height="100%" fill="{light}"/>'
            f'<path d="{"".join(parts)}" fill="{dark}"/></svg>')
