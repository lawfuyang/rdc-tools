"""PNG in and out, and the image comparison the offline A/B needs (`replaydiff`).

A bundle's `rt/` pictures are PNGs -- the engine's own encoder (`SaveTexture`) writes them -- while the
driver's `imgdiff` compares BMPs, because that is what its own `sheet` and `image` write. This module is
the offline half of the same two numbers: *how many pixels changed* (what a person means by "did this
change the picture") and the 64-bit difference hash (whether a human would see it), with the same shape
as `DifferenceHash`/`ImagePixelDelta` in `src/cpp/image.cpp` so a reader who knows one knows the other.

Decoding is pure Python and therefore measured, not assumed: the filter of each row has to be undone
byte by byte, and on this machine that costs ~0.15 s per megabyte (9 MB image: 1.0-1.4 s, dominated by
Paeth rows at ~230 ns/byte; an all-`Up` image is ~66 ns/byte because that row is one addition). That is
why `replaydiff` compares every aligned pass *by identity* (the file's bytes) and only decodes the ones
that differ, up to a stated cap -- see `rdc_ab`. Nothing here is a general image library: a shape this
reader does not decode is refused with what it is, never half-read, because a wrong difference looks
exactly like a real one.
"""
from __future__ import annotations

import struct
import zlib
from typing import Dict, NamedTuple, Optional, Sequence, Tuple

#: The 8 bytes every PNG starts with.
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'

#: The channel count of each colour type this reader decodes, by the type's own number.
CHANNELS: Dict[int, int] = {0: 1, 2: 3, 4: 2, 6: 4}

#: What each colour type is called, for a refusal that says what the file is.
COLOUR_NAMES: Dict[int, str] = {0: 'grey', 2: 'RGB', 3: 'palette', 4: 'grey+alpha', 6: 'RGBA'}

#: The largest difference the heat map shows as full brightness: 32 units of change is already obvious.
HEAT_SCALE = 8


class PngError(Exception):
    """A file this reader will not decode: a wrong signature, a shape it does not read, a broken stream.

    The refusal carries *what the file is* -- a 16-bit depth, an interlaced image, a palette, a colour
    type it has no channels for -- because "cannot read it" without the reason is the message that leaves
    a reader guessing whether the file or the tool is wrong.
    """


class Image(NamedTuple):
    """An 8-bit RGBA image, top-down: `width`, `height`, and exactly `width * height * 4` bytes.

    RGBA rather than the file's own channel count, because everything downstream compares *pixels*: the
    driver's BMP reader expands the same way, so a picture saved as RGB and the same picture saved as
    RGBA do not differ in the comparison by a conversion nobody asked for.
    """

    width: int
    height: int
    rgba: bytes

    def valid(self) -> bool:
        """Whether the three members agree: a picture whose buffer is the wrong size is not one."""
        return (self.width > 0 and self.height > 0
                and len(self.rgba) == self.width * self.height * 4)


class ImageDelta(NamedTuple):
    """How two images of one size differ: pixels changed, worst channel, sum of the per-pixel worsts.

    `sum_delta` is summed over the *differing* pixels only, which is what makes its ratio (`sum_delta /
    differing`) the mean of a change where it happened, rather than a number diluted by an unchanged
    background -- the same definition the driver's `imgdiff` prints as `meanDelta`.
    """

    pixels: int
    differing: int
    max_delta: int
    sum_delta: int


def _chunk(kind: bytes, body: bytes) -> bytes:
    """One PNG chunk: length, type, body, CRC over type+body (the CRC is not optional, readers check)."""
    return (struct.pack('>I', len(body)) + kind + body
            + struct.pack('>I', zlib.crc32(kind + body) & 0xFFFFFFFF))


def decode_png(data: bytes) -> Image:
    """Decode the bytes of one PNG this reader understands, or raise `PngError` saying what it is.

    8-bit grey, RGB, grey+alpha and RGBA, not interlaced, with all five row filters: that is what
    RenderDoc's own encoder writes (measured over every PNG this machine's captures produced: colour type
    6, depth 8, interlace 0). A palette, a 16-bit depth or an Adam7 image is refused by name rather than
    approximated.
    """
    if len(data) < len(PNG_SIGNATURE) or data[:8] != PNG_SIGNATURE:
        raise PngError('not a PNG (no signature)')

    width = height = bit_depth = colour_type = interlace = -1
    seen_header = False
    parts: list = []
    pos = 8
    while pos + 8 <= len(data):
        length = struct.unpack_from('>I', data, pos)[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise PngError('truncated: the %s chunk says %d byte(s) and the file ends %d short'
                           % (kind.decode('ascii', 'replace'), length, length - len(body)))
        pos += 12 + length
        if kind == b'IHDR':
            if length < 13:
                raise PngError('the header chunk is %d byte(s), not the 13 a PNG header is' % length)
            (width, height, bit_depth, colour_type, compression, filter_method,
             interlace) = struct.unpack_from('>IIBBBBB', body, 0)
            seen_header = True
            if compression != 0 or filter_method != 0:
                raise PngError('compression %d / filter method %d: neither is the 0 a PNG uses'
                               % (compression, filter_method))
        elif kind == b'IDAT':
            parts.append(body)
        elif kind == b'IEND':
            break

    if not seen_header:
        raise PngError('no header chunk (IHDR)')
    if width <= 0 or height <= 0:
        raise PngError('a %dx%d image is not one' % (width, height))
    if bit_depth != 8:
        raise PngError('a %d-bit image: only 8 bits per channel is decoded' % bit_depth)
    if colour_type not in CHANNELS:
        raise PngError('colour type %d (%s): only grey, RGB, grey+alpha and RGBA are decoded'
                       % (colour_type, COLOUR_NAMES.get(colour_type, 'unknown')))
    if interlace != 0:
        raise PngError('interlaced (Adam7): the images a capture writes are not, and this reader is not')
    if not parts:
        raise PngError('no image data (IDAT)')

    channels = CHANNELS[colour_type]
    stride = width * channels
    try:
        raw = zlib.decompress(b''.join(parts))
    except zlib.error as exc:
        raise PngError('the image data does not inflate (%s)' % exc) from exc
    if len(raw) < (stride + 1) * height:
        raise PngError('the image data holds %d of the %d byte(s) a %dx%d image needs'
                       % (len(raw), (stride + 1) * height, width, height))

    rows = _unfilter(raw, width, height, channels)
    return Image(width, height, _to_rgba(rows, width, height, colour_type))


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> bytearray:
    """Undo the per-row filters of a PNG (the five the format defines), in place, row by row.

    `raw` is the inflated stream: every row is one filter byte followed by `width * channels` bytes,
    each filtered against the row above it and the byte `channels` to its left. The result is the
    unfiltered rows concatenated, with the filter bytes dropped.
    """
    stride = width * channels
    out = bytearray(stride * height)
    previous = bytearray(stride)
    pos = 0
    for y in range(height):
        kind = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if kind == 1:
            # Sub: each byte plus the one a pixel to its left.
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif kind == 2:
            # Up: each byte plus the byte above it.
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif kind == 3:
            # Average: plus the mean of the byte above and the byte to the left, rounded down.
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:
            # Paeth: plus whichever of left/above/above-left is closest to their linear estimate.
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                above = previous[i]
                corner = previous[i - channels] if i >= channels else 0
                estimate = left + above - corner
                to_left = abs(estimate - left)
                to_above = abs(estimate - above)
                to_corner = abs(estimate - corner)
                if to_left <= to_above and to_left <= to_corner:
                    predicted = left
                elif to_above <= to_corner:
                    predicted = above
                else:
                    predicted = corner
                line[i] = (line[i] + predicted) & 0xFF
        elif kind != 0:
            raise PngError('row %d carries filter type %d, which the PNG format does not define'
                           % (y, kind))
        out[y * stride:(y + 1) * stride] = line
        previous = line
    return out


def _to_rgba(rows: bytearray, width: int, height: int, colour_type: int) -> bytes:
    """The unfiltered rows as RGBA, whatever channel count they arrived in.

    A channel the file does not carry is filled with what "there is no such channel" means: alpha 255
    (opaque) and, for grey, the one value in all three colour channels. Filling anything else would be a
    picture this tool invented.
    """
    if colour_type == 6:
        return bytes(rows)
    pixels = width * height
    rgba = bytearray(pixels * 4)
    if colour_type == 2:
        for i in range(pixels):
            rgba[i * 4:i * 4 + 3] = rows[i * 3:i * 3 + 3]
            rgba[i * 4 + 3] = 255
    elif colour_type == 4:
        for i in range(pixels):
            grey = rows[i * 2]
            rgba[i * 4] = grey
            rgba[i * 4 + 1] = grey
            rgba[i * 4 + 2] = grey
            rgba[i * 4 + 3] = rows[i * 2 + 1]
    else:
        for i in range(pixels):
            grey = rows[i]
            rgba[i * 4] = grey
            rgba[i * 4 + 1] = grey
            rgba[i * 4 + 2] = grey
            rgba[i * 4 + 3] = 255
    return bytes(rgba)


def read_png(path: str) -> Image:
    """Read one PNG file, or raise `PngError` with the reason (`decode_png` does the work)."""
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except OSError as exc:
        raise PngError('cannot read %s (%s)' % (path, exc)) from exc
    try:
        return decode_png(data)
    except PngError as exc:
        raise PngError('%s: %s' % (path, exc)) from exc


def encode_png(image: Image) -> bytes:
    """One 8-bit RGBA PNG, no interlacing, every row written with filter 0.

    Filter 0 rather than a heuristic: this writer exists for pictures this tool *synthesised* (a
    difference map), where the rows change everywhere and a filter search would cost more than it saves.
    The result is a file any reader opens, which is the whole point of writing it.
    """
    if not image.valid():
        raise PngError('a %dx%d image with %d byte(s) is not an image to write'
                       % (image.width, image.height, len(image.rgba)))
    stride = image.width * 4
    rows = bytearray()
    for y in range(image.height):
        rows.append(0)
        rows += image.rgba[y * stride:(y + 1) * stride]
    header = struct.pack('>IIBBBBB', image.width, image.height, 8, 6, 0, 0, 0)
    return (PNG_SIGNATURE + _chunk(b'IHDR', header)
            + _chunk(b'IDAT', zlib.compress(bytes(rows), 6)) + _chunk(b'IEND', b''))


def write_png(path: str, image: Image) -> None:
    """Write one image as a PNG, or raise `PngError` (the caller reports the path it could not write)."""
    try:
        with open(path, 'wb') as handle:
            handle.write(encode_png(image))
    except OSError as exc:
        raise PngError('cannot write %s (%s)' % (path, exc)) from exc


def image_delta(a: Image, b: Image, heat: bool = False) -> Optional[Tuple[ImageDelta, Optional[Image]]]:
    """How two images differ, or None when they cannot be compared pixel by pixel.

    Two images of different sizes are not "0% different": a 1920x1080 picture against a 1280x720 one is
    a different question, so the caller is given None and reports both sizes. All four channels count --
    a change in alpha alone is a change -- and a pixel's own difference is its worst channel.

    With `heat`, the second member of the pair is a picture of the difference: black where nothing moved,
    brighter the further it did. Nothing is written to disk here; the caller decides where a file goes.
    """
    if not a.valid() or not b.valid() or a.width != b.width or a.height != b.height:
        return None

    pixels = a.width * a.height
    differing = 0
    max_delta = 0
    sum_delta = 0
    map_rgba = bytearray(pixels * 4) if heat else None
    for px in range(pixels):
        i = px * 4
        delta = 0
        for channel in range(4):
            diff = a.rgba[i + channel] - b.rgba[i + channel]
            if diff < 0:
                diff = -diff
            if diff > delta:
                delta = diff
        if delta:
            differing += 1
            sum_delta += delta
            if delta > max_delta:
                max_delta = delta
        if map_rgba is not None:
            level = delta * HEAT_SCALE
            map_rgba[i] = 0
            map_rgba[i + 1] = 255 if level > 255 else level
            map_rgba[i + 2] = 255 if level > 255 else level
            map_rgba[i + 3] = 255
    heat_map_image = Image(a.width, a.height, bytes(map_rgba)) if map_rgba is not None else None
    return ImageDelta(pixels, differing, max_delta, sum_delta), heat_map_image


def downscale(image: Image, max_width: int, max_height: int) -> Image:
    """`image` scaled down to fit `max_width` x `max_height`, aspect kept, by averaging source pixels.

    Averaging rather than picking the nearest pixel, and never scaling *up* (a thumbnail bigger than its
    source is a lie about detail): both are the driver's `DownscaleImage` rules, because the hash below
    is only comparable with the one `imgdiff` prints if the two reduce the picture the same way.
    """
    if not image.valid() or max_width <= 0 or max_height <= 0:
        return image
    if image.width <= max_width and image.height <= max_height:
        return image

    scale = min(max_width / image.width, max_height / image.height)
    width = max(1, int(image.width * scale))
    height = max(1, int(image.height * scale))
    out = bytearray(width * height * 4)
    for y in range(height):
        y0 = y * image.height // height
        y1 = max(y0 + 1, (y + 1) * image.height // height)
        for x in range(width):
            x0 = x * image.width // width
            x1 = max(x0 + 1, (x + 1) * image.width // width)
            totals = [0, 0, 0, 0]
            count = 0
            for sy in range(y0, min(y1, image.height)):
                row = sy * image.width * 4
                for sx in range(x0, min(x1, image.width)):
                    base = row + sx * 4
                    for channel in range(4):
                        totals[channel] += image.rgba[base + channel]
                    count += 1
            dst = (y * width + x) * 4
            for channel in range(4):
                out[dst + channel] = totals[channel] // count if count else 0
    return Image(width, height, bytes(out))


#: The size the difference hash reduces an image to: 9x8, one bit per horizontally adjacent pair.
HASH_SIZE = (9, 8)


def difference_hash(image: Image) -> int:
    """A 64-bit perceptual hash of an image: 9x8 grey, one bit per neighbouring pair of pixels.

    Two images that look the same differ in a couple of bits; the same image re-rendered on a different
    driver typically differs in a handful. It is a *perceptual* number, so it belongs beside the exact one
    (how many pixels changed) rather than instead of it: an image that changed everywhere but kept its
    layout has a small distance and a large difference count, and both are true.

    Zero for a picture there is nothing to hash: a real hash of an all-dark 1x1 image is also zero, and
    the caller knows the size it asked about.
    """
    if not image.valid():
        return 0
    grey = downscale(image, HASH_SIZE[0], HASH_SIZE[1])
    if grey.width < 2 or grey.height < 1:
        return 0

    bits = 0
    bit = 0
    for y in range(grey.height):
        base = y * grey.width * 4
        for x in range(grey.width - 1):
            if bit >= 64:
                break
            left = base + x * 4
            right = left + 4
            # Rec. 601 luma, integer: what the eye weights, so a blue/red change counts like it looks.
            luma_left = (77 * grey.rgba[left] + 150 * grey.rgba[left + 1]
                         + 29 * grey.rgba[left + 2]) >> 8
            luma_right = (77 * grey.rgba[right] + 150 * grey.rgba[right + 1]
                          + 29 * grey.rgba[right + 2]) >> 8
            if luma_left > luma_right:
                bits |= 1 << bit
            bit += 1
    return bits


def hash_distance(a: int, b: int) -> int:
    """How many of the 64 bits of two difference hashes disagree (0 = the two look the same)."""
    return bin(a ^ b).count('1')


def percentile(values: Sequence[int], fraction: float) -> int:
    """The value at `fraction` through a sorted copy of `values` (0 when there are none).

    Used for the summary of a whole frame's image differences: the *median* of the per-image percentages
    is a more honest sentence than their mean, which one 100%-changed pass drags to whatever it likes.
    """
    if not values:
        return 0
    ordered = sorted(values)
    index = int(len(ordered) * fraction)
    return ordered[min(index, len(ordered) - 1)]


__all__ = [
    'CHANNELS',
    'COLOUR_NAMES',
    'HASH_SIZE',
    'HEAT_SCALE',
    'Image',
    'ImageDelta',
    'PNG_SIGNATURE',
    'PngError',
    'decode_png',
    'difference_hash',
    'downscale',
    'encode_png',
    'hash_distance',
    'image_delta',
    'percentile',
    'read_png',
    'write_png',
]
