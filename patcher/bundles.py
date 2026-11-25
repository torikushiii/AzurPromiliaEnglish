"""Text tables inside the game's UnityCN-encrypted asset bundles.

Two tables live outside the main text archives:
- "csv": the startup and loading-screen text (updatableBuiltInLangString), a CSV.
- "labels": fixed interface labels (ast_staticTextKeyConfig), key/value pairs.

The decoded bundle is edited without changing its length, and only the LZ4 blocks
that changed are rewritten, each to exactly its original compressed size, so the rest
of the file stays byte-identical. A block's LZ4 stream is kept as it is wherever the
text did not change, which keeps the rewrite deterministic.
"""

from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass, field

TAG = b"updatableBuiltInLangString"
LABELS = b"ast_staticTextKeyConfig"
BOM = b"\xef\xbb\xbf"
MIN_MATCH = 4
MAX_OFFSET = 0xFFFF


class BundleError(Exception):
    pass


@dataclass
class TextBundle:
    role: str
    kind: str
    path: str
    offset: int
    size: int
    key: str
    blocks: list[int]
    stock_sha256: str
    patched_sha256: str
    backup_start: int
    backup_end: int
    backup_sha256: str
    strings: dict[str, str] = field(default_factory=dict)

    def as_json(self) -> dict:
        return dict(self.__dict__)


class Cipher:
    """The per-bundle nibble cipher applied to LZ4 sequence headers."""

    def __init__(self, key: bytes):
        if len(key) != 16:
            raise BundleError("Bad text bundle key.")
        index = []
        for b in key[:8]:
            index += [b >> 4, b & 15]
        if sorted(index) != list(range(16)):
            raise BundleError("Bad text bundle key.")
        inv = [0] * 16
        for i, v in enumerate(index):
            inv[v] = i
        s = [0] * 16
        for i in range(4):
            b0, b1 = key[8 + 2 * i], key[9 + 2 * i]
            s[i], s[4 + i], s[8 + i], s[12 + i] = b0 >> 4, b0 & 15, b1 >> 4, b1 & 15
        r9 = [
            (
                s[12 + ((i >> 6) & 3)]
                + s[8 + ((i >> 4) & 3)]
                + s[4 + ((i >> 2) & 3)]
                + s[i & 3]
            )
            & 0xFF
            for i in range(256)
        ]
        self._dec = [
            bytes(
                (((index[v >> 4] - r) & 15) << 4) | ((index[v & 15] - r) & 15)
                for v in range(256)
            )
            for r in r9
        ]
        self._enc = [
            bytes(
                (inv[((v >> 4) + r) & 15] << 4) | inv[((v & 15) + r) & 15]
                for v in range(256)
            )
            for r in r9
        ]

    def _walk(self, buf: bytearray, block: int, tables) -> None:
        def step(p, idx):
            before = buf[p]
            buf[p] = tables[idx & 0xFF][before]
            plain = buf[p] if tables is self._dec else before
            return plain

        p, n, seq = 0, len(buf), block
        while p < n:
            idx = seq
            seq += 1
            token = step(p, idx)
            p, idx = p + 1, idx + 1
            lit = token >> 4
            if lit == 15:
                while True:
                    extra = step(p, idx)
                    p, idx = p + 1, idx + 1
                    lit += extra
                    if extra != 255:
                        break
            p += lit
            if p >= n:
                break
            step(p, idx)
            step(p + 1, idx + 1)
            p, idx = p + 2, idx + 2
            if token & 15 == 15:
                while True:
                    extra = step(p, idx)
                    p, idx = p + 1, idx + 1
                    if extra != 255:
                        break

    def decrypt(self, data: bytes, block: int) -> bytes:
        buf = bytearray(data)
        self._walk(buf, block, self._dec)
        return bytes(buf)

    def encrypt(self, data: bytes, block: int) -> bytes:
        buf = bytearray(data)
        self._walk(buf, block, self._enc)
        return bytes(buf)


def lz4_parse(src: bytes, size: int) -> tuple[list[tuple[int, int, int, int]], bytes]:
    out = bytearray()
    seqs = []
    sp, n = 0, len(src)
    try:
        while sp < n:
            token = src[sp]
            sp += 1
            lit = token >> 4
            if lit == 15:
                while True:
                    b = src[sp]
                    sp += 1
                    lit += b
                    if b != 255:
                        break
            start = len(out)
            out += src[sp : sp + lit]
            sp += lit
            if sp >= n:
                seqs.append((start, lit, 0, 0))
                break
            off = src[sp] | (src[sp + 1] << 8)
            sp += 2
            ml = (token & 15) + MIN_MATCH
            if token & 15 == 15:
                while True:
                    b = src[sp]
                    sp += 1
                    ml += b
                    if b != 255:
                        break
            if not 0 < off <= len(out):
                raise BundleError("Bad LZ4 data.")
            if off >= ml:
                out += out[-off : len(out) - off + ml]
            else:
                for _ in range(ml):
                    out.append(out[-off])
            seqs.append((start, lit, off, ml))
    except IndexError as e:
        raise BundleError("Bad LZ4 data.") from e
    if len(out) != size:
        raise BundleError("Bad LZ4 data.")
    return seqs, bytes(out)


def _length_bytes(n: int) -> int:
    return 0 if n < 15 else 1 + (n - 15) // 255


def seq_size(lit: int, ml: int) -> int:
    size = 1 + _length_bytes(lit) + lit
    if ml:
        size += 2 + _length_bytes(ml - MIN_MATCH)
    return size


def _put_length(out: bytearray, n: int) -> None:
    n -= 15
    while n >= 255:
        out.append(255)
        n -= 255
    out.append(n)


def lz4_encode(seqs, plain: bytes) -> bytes:
    out = bytearray()
    for start, lit, off, ml in seqs:
        m = ml - MIN_MATCH if ml else 0
        out.append((min(lit, 15) << 4) | min(m, 15))
        if lit >= 15:
            _put_length(out, lit)
        out += plain[start : start + lit]
        if ml:
            out += struct.pack("<H", off)
            if m >= 15:
                _put_length(out, m)
    return bytes(out)


def _match_length(plain: bytes, ref: int, p: int, limit: int) -> int:
    """How many bytes at `p` repeat those at `ref`, up to `limit`."""
    ml = 0
    while (
        ml + 32 <= limit
        and plain[ref + ml : ref + ml + 32] == plain[p + ml : p + ml + 32]
    ):
        ml += 32
    while ml < limit and plain[ref + ml] == plain[p + ml]:
        ml += 1
    return ml


def lz4_compress(plain: bytes, start: int = 0, end: int | None = None, depth: int = 64):
    """LZ4 sequences for plain[start:end], matching against all earlier bytes.

    A hash-chain search with one step of lazy matching. Returns (sequences, tail): the
    sequences each end in a match, and the trailing literals run from `tail` to `end`.
    """
    n = len(plain)
    end = n if end is None else end
    last = end == n
    match_limit = end - 5 if last else end
    start_limit = end - 12 if last else end - MIN_MATCH
    chains: dict[bytes, list[int]] = {}
    for p in range(max(0, start - MAX_OFFSET), start):
        chains.setdefault(plain[p : p + 4], []).append(p)

    def best(p):
        found_len = found_off = 0
        candidates = chains.get(plain[p : p + 4])
        if candidates:
            limit = match_limit - p
            for ref in reversed(candidates[-depth:]):
                if p - ref > MAX_OFFSET:
                    break
                ml = _match_length(plain, ref, p, limit)
                if ml > found_len:
                    found_len, found_off = ml, p - ref
                    if ml >= limit:
                        break
        return found_len, found_off

    seqs = []
    anchor = p = start
    found = best(p) if p < start_limit else (0, 0)
    while p < start_limit:
        ml, off = found
        chains.setdefault(plain[p : p + 4], []).append(p)
        if ml >= MIN_MATCH:
            nxt = best(p + 1) if p + 1 < start_limit else (0, 0)
            if nxt[0] > ml + 1:
                p += 1
                found = nxt
                continue
            seqs.append((anchor, p - anchor, off, ml))
            for q in range(p + 1, min(p + ml, start_limit)):
                chains.setdefault(plain[q : q + 4], []).append(q)
            p += ml
            anchor = p
        else:
            p += 1
        found = best(p) if p < start_limit else (0, 0)
    return seqs, anchor


def lz4_optimal(
    plain: bytes,
    start: int = 0,
    end: int | None = None,
    depth: int = 24,
    extra: dict[int, list[tuple[int, int]]] | None = None,
):
    """Like lz4_compress, but chooses the cheapest parse instead of the greedy one.

    For every position it finds the matches a hash chain offers (plus any `extra`
    (length, offset) matches given by position), then picks the sequence of literals
    and matches with the fewest encoded bytes: dynamic programming over positions,
    tracking the length of the current literal run.
    """
    n = len(plain)
    end = n if end is None else end
    last = end == n
    match_limit = end - 5 if last else end
    start_limit = end - 12 if last else end - MIN_MATCH
    size = end - start
    if size <= 0:
        return [], start
    chains: dict[bytes, list[int]] = {}
    for p in range(max(0, start - MAX_OFFSET), start):
        chains.setdefault(plain[p : p + 4], []).append(p)
    found: list[list[tuple[int, int]] | None] = [None] * size
    skip_to = start
    for p in range(start, start_limit):
        key = plain[p : p + 4]
        candidates = chains.get(key)
        if p < skip_to:
            if candidates:
                candidates.append(p)
            else:
                chains[key] = [p]
            continue
        if candidates:
            limit = match_limit - p
            best = MIN_MATCH - 1
            here = []
            for ref in reversed(candidates[-depth:]):
                if p - ref > MAX_OFFSET:
                    break
                ml = _match_length(plain, ref, p, limit)
                if ml > best:
                    best = ml
                    here.append((ml, p - ref))
                    if ml >= limit:
                        break
            if here:
                found[p - start] = here
                if best >= 256:
                    skip_to = p + best
            candidates.append(p)
        else:
            chains[key] = [p]
    for p, matches in (extra or {}).items():
        if start <= p < start_limit:
            merged = sorted(set((found[p - start] or []) + matches))
            here, best = [], MIN_MATCH - 1
            for length, off in merged:
                ml = min(length, match_limit - p)
                if ml > best:
                    here.append((ml, off))
                    best = ml
            found[p - start] = here or None
    inf = 1 << 60
    cost = [inf] * (size + 1)
    lits = [0] * (size + 1)
    via = [None] * (size + 1)
    cost[0] = 0
    for i in range(size):
        c = cost[i]
        if c >= inf:
            continue
        run = lits[i] + 1
        lc = c + 1 + (1 if run == 15 or (run > 15 and (run - 15) % 255 == 0) else 0)
        if lc < cost[i + 1]:
            cost[i + 1], lits[i + 1], via[i + 1] = lc, run, None
        here = found[i]
        if not here:
            continue
        base = c + 3 + (1 if lits[i] >= 15 else 0)
        lo = MIN_MATCH
        for ml, off in here:
            top = ml if ml <= 64 else 64
            for length in range(lo, top + 1):
                j = i + length
                mc = base + _length_bytes(length - MIN_MATCH)
                if mc < cost[j]:
                    cost[j], lits[j], via[j] = mc, 0, (length, off)
            if ml > 64:
                j = i + ml
                mc = base + _length_bytes(ml - MIN_MATCH)
                if mc < cost[j]:
                    cost[j], lits[j], via[j] = mc, 0, (ml, off)
            lo = top + 1
    i = size
    while via[i] is None and i > 0 and lits[i] > 0:
        i -= lits[i]
    tail = start + i
    seqs = []
    while i > 0:
        length, off = via[i]
        m = i - length
        lit_start = m - lits[m]
        seqs.append((start + lit_start, lits[m], off, length))
        i = lit_start
    seqs.reverse()
    return seqs, tail


def _valid_match(plain: bytes, start: int, lit: int, off: int, ml: int) -> bool:
    m = start + lit
    return off <= m and plain[m - off : m - off + ml] == plain[m : m + ml]


OPTIMAL_DEPTH = 4096


def rewrite(
    kept, plain: bytes, target: int | None, depth: int = OPTIMAL_DEPTH
) -> bytes:
    """Encode `plain` to exactly `target` bytes (or as few as it takes, for None).

    `kept` are old sequences, moved to their place in `plain`, sorted and already
    checked to still decode correctly there. Two encodings are tried: the kept
    sequences with the gaps between them compressed afresh, and one optimal parse of
    the whole block that may use the kept matches. The smaller one wins.
    """
    patched = _reuse_plan(kept, plain)
    if target is not None and sum(seq_size(q[1], q[3]) for q in patched) <= target:
        return _pack(patched, plain, target)
    extra: dict[int, list[tuple[int, int]]] = {}
    for start, lit, off, ml in kept:
        extra.setdefault(start + lit, []).append((ml, off))
    seqs, tail = lz4_optimal(plain, 0, len(plain), depth, extra)
    optimal = [*seqs, (tail, len(plain) - tail, 0, 0)]
    out = min(patched, optimal, key=lambda s: sum(seq_size(q[1], q[3]) for q in s))
    return _pack(out, plain, target)


def _reuse_plan(kept, plain: bytes) -> list:
    n = len(plain)
    out = []
    pending = None
    pos = 0

    def fill(region_start, region_end):
        nonlocal pending
        seqs, tail = lz4_compress(plain, region_start, region_end)
        out.extend(seqs)
        pending = (tail, region_end - tail)

    for kept_start, kept_lit, off, ml in kept:
        if kept_start < pos:
            continue
        if kept_start > pos:
            fill(pending[0] if pending else pos, kept_start)
        start, lit = kept_start, kept_lit
        if pending:
            lit += start - pending[0]
            start = pending[0]
            pending = None
        out.append((start, lit, off, ml))
        pos = start + lit + ml
    if pos < n:
        fill(pending[0] if pending else pos, n)
    if pending:
        out.append((pending[0], pending[1], 0, 0))
    return out


def _pack(out: list, plain: bytes, target: int | None) -> bytes:
    size = sum(seq_size(lit, ml) for _, lit, _, ml in out)
    if target is None:
        return lz4_encode(out, plain)
    if size > target:
        raise BundleError(
            f"The English text does not fit in its block ({size} > {target} bytes)."
        )
    out = list(out)
    order = sorted(
        (k for k in range(len(out)) if out[k][3] > MIN_MATCH), key=lambda k: -out[k][3]
    )
    for k in order:
        need = target - size
        if not need:
            break
        start, lit, off, ml = out[k]
        base = seq_size(lit, ml)
        best = 0
        for take in range(min(need + 2, ml - MIN_MATCH), 0, -1):
            grow = seq_size(lit + take, ml - take) - base
            if grow <= need:
                best = take
                break
        if best:
            size += seq_size(lit + best, ml - best) - base
            out[k] = (start, lit + best, off, ml - best)
    if size != target:
        raise BundleError("The English text could not be packed to its block size.")
    data = lz4_encode(out, plain)
    assert len(data) == target
    return data


@dataclass
class Block:
    usize: int
    csize: int
    flags: int
    offset: int


def _cstr(data: bytes, pos: int) -> tuple[bytes, int]:
    end = data.index(0, pos)
    return data[pos:end], end + 1


@dataclass
class Layout:
    blocks: list[Block]
    info_at: int
    info_size: int
    info_size_at: int
    info_seqs: list
    info: bytes


def parse_blocks(data: bytes) -> list[Block]:
    return parse_layout(data).blocks


def parse_layout(data: bytes) -> Layout:
    try:
        sig, pos = _cstr(data, 0)
        if sig != b"UnityFS":
            raise BundleError("A text bundle has an unexpected format.")
        (version,) = struct.unpack_from(">I", data, pos)
        _, pos = _cstr(data, pos + 4)
        _, pos = _cstr(data, pos)
        _, c_info, u_info, flags = struct.unpack_from(">qIII", data, pos)
        info_size_at = pos + 8
        pos += 20
        if version >= 7:
            pos = (pos + 15) & ~15
        if not flags & 0x400 or flags & 0x80:
            raise BundleError("A text bundle has an unexpected format.")
        info_at = (pos + 65 + 15) & ~15
        if flags & 0x3F not in (2, 3):
            raise BundleError("A text bundle has an unexpected format.")
        info_seqs, info = lz4_parse(data[info_at : info_at + c_info], u_info)
        if lz4_encode(info_seqs, info) != data[info_at : info_at + c_info]:
            raise BundleError("A text bundle uses an unexpected LZ4 layout.")
        offset = (info_at + c_info + 15) & ~15
        (count,) = struct.unpack_from(">i", info, 16)
        blocks = []
        for k in range(count):
            usize, csize, bflags = struct.unpack_from(">IIH", info, 20 + 10 * k)
            blocks.append(Block(usize, csize, bflags, offset))
            offset += csize
    except (ValueError, struct.error) as e:
        raise BundleError("A text bundle has an unexpected format.") from e
    if offset > len(data):
        raise BundleError("A text bundle has an unexpected format.")
    return Layout(blocks, info_at, c_info, info_size_at, info_seqs, info)


def read_block(data: bytes, cipher: Cipher, blocks: list[Block], index: int):
    """The block's (stored bytes, LZ4 stream, sequences, plain text)."""
    blk = blocks[index]
    if blk.flags & 0x3F not in (2, 3):
        raise BundleError("A text bundle block is not LZ4 compressed.")
    stored = data[blk.offset : blk.offset + blk.csize]
    stream = cipher.decrypt(stored, index) if blk.flags & 0x100 else stored
    seqs, plain = lz4_parse(stream, blk.usize)
    if lz4_encode(seqs, plain) != stream:
        raise BundleError("A text bundle block uses an unexpected LZ4 layout.")
    return stored, stream, seqs, plain


def find_table(plain: bytes) -> tuple[int, int]:
    """(start, length) of the CSV text in the block, including its BOM."""
    at = plain.find(TAG)
    if at < 0 or plain.find(TAG, at + 1) >= 0:
        raise BundleError("The startup text table was not found.")
    pos = (at + len(TAG) + 3) & ~3
    (length,) = struct.unpack_from("<i", plain, pos)
    if not 0 < length <= len(plain) - pos - 4:
        raise BundleError("The startup text table was not found.")
    return pos + 4, length


def csv_cells(text: str) -> list[list[tuple[str, int, int]]]:
    """Rows of (value, start, end); start/end span the cell as written, with quotes."""
    rows, row, i, n = [], [], 0, len(text)
    while i < n:
        start = i
        if text[i] == '"':
            i += 1
            value = []
            while True:
                j = text.index('"', i)
                value.append(text[i:j])
                if j + 1 < n and text[j + 1] == '"':
                    value.append('"')
                    i = j + 2
                    continue
                i = j + 1
                break
            value = "".join(value)
        else:
            while i < n and text[i] not in ",\n":
                i += 1
            value = text[start:i]
        row.append((value, start, i))
        if i < n and text[i] == ",":
            i += 1
            if i == n or text[i] == "\n":
                row.append(("", i, i))
        if i < n and text[i] == "\n":
            rows.append(row)
            row = []
            i += 1
        elif i < n and text[i] != "," and text[i - 1] != ",":
            raise BundleError("The startup text table has an unexpected format.")
    if row:
        rows.append(row)
    return rows


def _quote(value: str) -> str:
    if any(c in value for c in ',"\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


def translate_csv(raw: bytes, strings: dict[str, str]) -> bytes:
    """The CSV with each listed key's Japanese replaced, padded to the same length."""
    has_bom = raw.startswith(BOM)
    text = raw[len(BOM) :].decode("utf-8") if has_bom else raw.decode("utf-8")
    rows = csv_cells(text)
    header = [cell[0] for cell in rows[0]]
    if "key" not in header or "jp" not in header:
        raise BundleError("The startup text table has an unexpected format.")
    key_col, jp_col = header.index("key"), header.index("jp")
    edits = []
    for row in rows[1:]:
        if len(row) > jp_col and row[key_col][0] in strings:
            _, start, end = row[jp_col]
            edits.append((start, end, _quote(strings[row[key_col][0]])))
    if not text.endswith(",\n"):
        raise BundleError("The startup text table has an unexpected format.")
    parts, pos = [], 0
    for start, end, new in sorted(edits):
        parts += [text[pos:start], new]
        pos = end
    parts.append(text[pos:])
    body = "".join(parts).encode("utf-8")
    room = len(raw) - len(body) - (len(BOM) if has_bom else 0)
    if room < 0:
        raise BundleError("The English startup text is longer than the Japanese.")
    if not body.endswith(b",\n"):
        raise BundleError("The startup text table has an unexpected format.")
    body = body[:-1] + b" " * room + b"\n"
    return (BOM if has_bom else b"") + body


def translate_csv_table(plain: bytes, strings: dict[str, str]):
    """(new plain, unchanged segments); the CSV keeps its length."""
    start, length = find_table(plain)
    csv = translate_csv(plain[start : start + length], strings)
    new = plain[:start] + csv + plain[start + length :]
    diff = [k for k in range(length) if csv[k] != plain[start + k]]
    if not diff:
        return new, [(0, 0, len(plain))]
    a, b = start + diff[0], start + diff[-1] + 1
    return new, [(0, 0, a), (b, b, len(plain) - b)]


def _align4(n: int) -> int:
    return (n + 3) & ~3


def _string(data: bytes, pos: int) -> tuple[bytes, int]:
    (n,) = struct.unpack_from("<i", data, pos)
    if not 0 <= n <= len(data) - pos - 4:
        raise BundleError("The interface label table has an unexpected format.")
    return data[pos + 4 : pos + 4 + n], _align4(pos + 4 + n)


def _put_string(out: bytearray, value: bytes) -> None:
    out += struct.pack("<i", len(value)) + value
    out += bytes(_align4(len(out)) - len(out))


def read_labels(plain: bytes):
    """(object start, label start, pairs, object end, size field position)."""
    try:
        (version,) = struct.unpack_from(">I", plain, 8)
        if version < 22:
            raise BundleError("The interface label table has an unexpected format.")
        _, _, data_at = struct.unpack_from(">IQQ", plain, 20)
        names, at = [], plain.find(LABELS)
        while at >= 0:
            if (
                at >= 4
                and at % 4 == 0
                and plain[at - 4 : at] == struct.pack("<i", len(LABELS))
            ):
                names.append(at)
            at = plain.find(LABELS, at + 1)
        if len(names) != 1:
            raise BundleError("The interface label table was not found.")
        at = names[0]
        _, pos = _string(plain, at - 4)
        lang_at = pos
        _, pos = _string(plain, pos)
        (count,) = struct.unpack_from("<i", plain, pos)
        pos += 4
        pairs = []
        for _ in range(count):
            key, pos = _string(plain, pos)
            value, pos = _string(plain, pos)
            pairs.append((key.decode("utf-8"), value.decode("utf-8")))
    except (struct.error, UnicodeDecodeError) as e:
        raise BundleError("The interface label table has an unexpected format.") from e
    entry = struct.pack("<qI", 0, pos - data_at)
    size_at = plain.find(entry, 0, data_at)
    if (
        not data_at < at < pos
        or size_at < 0
        or plain.find(entry, size_at + 1, data_at) >= 0
    ):
        raise BundleError("The interface label table has an unexpected format.")
    return data_at, lang_at, pairs, pos, size_at + 8


def translate_labels(plain: bytes, strings: dict[str, str]):
    """(new plain, unchanged segments): listed labels replaced, the object shrunk.

    The object is followed by zeros up to its old end, so nothing after it moves; its
    size in the file's object table is updated.
    """
    start, lang_at, pairs, end, size_at = read_labels(plain)
    new = bytearray(plain[:lang_at])
    segs = [(0, 0, size_at), (size_at + 4, size_at + 4, lang_at - size_at - 4)]
    run = lang_at

    def flush(upto):
        if upto > run:
            segs.append((run, len(new), upto - run))
            new.extend(plain[run:upto])

    _, pos = _string(plain, lang_at)
    pos += 4
    for key, value in pairs:
        _, pos = _string(plain, pos)
        value_at = pos
        _, pos = _string(plain, pos)
        english = strings.get(key)
        if english is not None and english != value:
            flush(value_at)
            _put_string(new, english.encode("utf-8"))
            run = pos
    flush(end)
    size = len(new) - start
    if size > end - start:
        raise BundleError("The English labels are longer than the Japanese.")
    new += bytes(end - len(new))
    segs.append((end, end, len(plain) - end))
    new += plain[end:]
    struct.pack_into("<I", new, size_at, size)
    return bytes(new), [s for s in segs if s[2] > 0]


EDITORS = {"csv": translate_csv_table, "labels": translate_labels}


def _bundle(data: bytes, info: TextBundle) -> bytes:
    sub = data[info.offset : info.offset + info.size]
    if len(sub) != info.size:
        raise BundleError("A text bundle has an unexpected format.")
    return sub


def decode(data: bytes, info: TextBundle):
    """(layout, cipher, {block: (stored, sequences, plain)}) for the table's blocks."""
    sub = _bundle(data, info)
    cipher = Cipher(bytes.fromhex(info.key))
    layout = parse_layout(sub)
    wanted = list(info.blocks)
    if not wanted or wanted != list(range(wanted[0], wanted[-1] + 1)):
        raise BundleError("A text bundle has an unexpected format.")
    if wanted[-1] >= len(layout.blocks):
        raise BundleError("A text bundle has an unexpected format.")
    parts = {}
    for i in wanted:
        stored, _, seqs, plain = read_block(sub, cipher, layout.blocks, i)
        parts[i] = (stored, seqs, plain)
    return layout, cipher, parts


def table_plain(data: bytes, info: TextBundle) -> bytes:
    return b"".join(p[2] for p in decode(data, info)[2].values())


def _moved_sequences(chunks, segs):
    """Old sequences (as positions in the new plain text) whose bytes only moved.

    `chunks` are (decoded size, sequences) of the consecutive blocks.
    """
    olds = [s[0] for s in segs]

    def new_place(g):
        k = bisect.bisect_right(olds, g) - 1
        if k < 0:
            return None, None
        old_at, new_at, length = segs[k]
        return (
            (g - old_at + new_at, old_at + length)
            if g < old_at + length
            else (None, None)
        )

    moved, base = [], 0
    for usize, seqs in chunks:
        for start, seq_lit, off, ml in seqs:
            if not ml:
                continue
            g, lit = base + start, seq_lit
            at, seg_end = new_place(g)
            if at is None or g + lit + ml > seg_end:
                g, lit = g + lit, 0
                at, seg_end = new_place(g)
                if at is None or g + ml > seg_end:
                    continue
            src, _ = new_place(g + lit - off)
            offs = [off]
            if src is not None and src < at + lit and at + lit - src != off:
                offs.append(at + lit - src)
            moved.append((at, lit, offs, ml))
        base += usize
    return moved


def _kept(moved, new: bytes, pos: int) -> list:
    """The moved sequences that fall inside this block and still decode correctly."""
    n = len(new)
    kept = []
    for s, lit, offs, ml in moved:
        if pos <= s and s + lit + ml <= pos + n - 5 and s + lit <= pos + n - 12:
            for off in offs:
                if off <= MAX_OFFSET and _valid_match(new, s - pos, lit, off, ml):
                    kept.append((s - pos, lit, off, ml))
                    break
    return kept


def _same_length_segments(old: bytes, new: bytes):
    diff = [k for k in range(len(old)) if old[k] != new[k]]
    if not diff:
        return [(0, 0, len(old))]
    a, b = diff[0], diff[-1] + 1
    return [(0, 0, a), (b, b, len(old) - b)]


def changed_range(data: bytes, info: TextBundle) -> tuple[int, int, bytes]:
    """(start, end, new bytes) of the file range the patch rewrites.

    Changed blocks share the compressed space of the blocks they span: all but the
    last take what they need, and the last is packed to exactly what is left, so the
    bundle keeps its length. If block sizes change, the block table is rewritten too,
    to exactly its old compressed size.
    """
    sub = _bundle(data, info)
    layout, cipher, parts = decode(data, info)
    blocks = layout.blocks
    new_plain, segs = EDITORS[info.kind](
        b"".join(p[2] for p in parts.values()), info.strings
    )
    moved = _moved_sequences([(blocks[i].usize, parts[i][1]) for i in parts], segs)
    news, kept, changed, pos = {}, {}, [], 0
    for i in parts:
        blk = blocks[i]
        new = new_plain[pos : pos + blk.usize]
        if len(new) != blk.usize:
            raise BundleError("A text bundle changed length.")
        news[i] = new
        if new != parts[i][2]:
            changed.append(i)
            kept[i] = _kept(moved, new, pos)
        pos += blk.usize
    if not changed:
        return 0, 0, b""
    first, last = changed[0], changed[-1]
    span = range(first, last + 1)
    streams = {i: rewrite(kept[i], news[i], None) for i in changed[:-1]}
    used = sum(len(s) for s in streams.values()) + sum(
        blocks[i].csize for i in span if i not in changed
    )
    streams[last] = rewrite(
        kept[last], news[last], sum(blocks[i].csize for i in span) - used
    )
    stored, sizes = [], {}
    for i in span:
        if i not in streams:
            stored.append(parts[i][0])
            continue
        if lz4_parse(streams[i], blocks[i].usize)[1] != news[i]:
            raise BundleError("A rebuilt text block did not verify.")
        sizes[i] = len(streams[i])
        flags = blocks[i].flags
        stored.append(cipher.encrypt(streams[i], i) if flags & 0x100 else streams[i])
    start, prefix = blocks[first].offset, b""
    if any(sizes[i] != blocks[i].csize for i in sizes):
        table = bytearray(layout.info)
        for i, size in sizes.items():
            struct.pack_into(">I", table, 20 + 10 * i + 4, size)
        table = bytes(table)
        table_moved = _moved_sequences(
            [(len(table), layout.info_seqs)], _same_length_segments(layout.info, table)
        )
        table_kept = _kept(table_moved, table, 0)
        data_at = blocks[0].offset
        try:
            packed = rewrite(table_kept, table, layout.info_size)
            start = layout.info_at
            prefix = (
                packed + sub[layout.info_at + layout.info_size : blocks[first].offset]
            )
        except BundleError:
            packed = rewrite(table_kept, table, None)
            if layout.info_at + len(packed) > data_at:
                raise
            start = layout.info_size_at
            prefix = (
                struct.pack(">I", len(packed))
                + sub[layout.info_size_at + 4 : layout.info_at]
                + packed
                + bytes(data_at - layout.info_at - len(packed))
                + sub[data_at : blocks[first].offset]
            )
    end = blocks[last].offset + blocks[last].csize
    return info.offset + start, info.offset + end, prefix + b"".join(stored)


def patched_bytes(data: bytes, info: TextBundle) -> bytes:
    start, end, new = changed_range(data, info)
    out = data[:start] + new + data[end:]
    expected = EDITORS[info.kind](table_plain(data, info), info.strings)[0]
    if len(out) != len(data) or table_plain(out, info) != expected:
        raise BundleError("A rebuilt text bundle did not verify.")
    return out


def restored_bytes(data: bytes, info: TextBundle, backup: bytes) -> bytes:
    if len(backup) != info.backup_end - info.backup_start:
        raise BundleError("The text backup does not match.")
    return data[: info.backup_start] + backup + data[info.backup_end :]
