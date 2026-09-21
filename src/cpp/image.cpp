// z.image — BMP in and out, thumbnails, montages, differences and a perceptual hash
//
// Part of replay_dump; the internal API is declared in common.h.
//
// Why this exists rather than a library: the driver links nothing but the RenderDoc DLL and bcrypt,
// and the two things it needs an image for are small *synthesised* pictures -- a contact sheet of
// thumbnails and a difference heat map. A 24-bit BMP is a 54-byte header and bottom-up BGR rows,
// which is less code than the build step a dependency would need, and the engine's own encoder is
// available for *captured* textures (`textures --save`, `SaveTexture` with PNG).
//
// `WriteBMP` is a straight move of the writer that has always written the bundle's `rt/` images:
// the manifest hashes those files, so its bytes are a contract and not an implementation detail.

#include "common.h"

namespace
{
void PutLE16(uint8_t *dst, uint16_t value)
{
  dst[0] = (uint8_t)(value & 0xffu);
  dst[1] = (uint8_t)((value >> 8) & 0xffu);
}

void PutLE32(uint8_t *dst, uint32_t value)
{
  dst[0] = (uint8_t)(value & 0xffu);
  dst[1] = (uint8_t)((value >> 8) & 0xffu);
  dst[2] = (uint8_t)((value >> 16) & 0xffu);
  dst[3] = (uint8_t)((value >> 24) & 0xffu);
}

uint32_t GetLE32(const uint8_t *src)
{
  return (uint32_t)src[0] | ((uint32_t)src[1] << 8) | ((uint32_t)src[2] << 16) |
         ((uint32_t)src[3] << 24);
}

uint16_t GetLE16(const uint8_t *src)
{
  return (uint16_t)((uint32_t)src[0] | ((uint32_t)src[1] << 8));
}
}    // namespace

//: A 24-bit BMP of the texture display at one event. BMP rather than PNG because it needs no
//: encoder: the pixels come back as RGBA and the header is 54 bytes.
//:
//: The size arithmetic is done in `size_t` and checked before it is used, because the width and
//: height come from the engine as `int32_t`: `width * height * 4` in `int` can overflow
//: ([expr.mul], [ub:expr.mul.representable.type.result]), and the wrapped value is exactly what a
//: bounds check would then be trusting. Every write is checked too -- a short write leaves a
//: truncated image that otherwise looks like success.
bool WriteBMP(const char *path, const bytebuf &rgba, int32_t width, int32_t height)
{
  if(width <= 0 || height <= 0)
    return false;

  const size_t w = (size_t)width;
  const size_t h = (size_t)height;

  // The buffer has to hold w*h pixels of 4 bytes; the division detects a wrapped product.
  const size_t needed = w * h * 4;
  if(needed / 4 / h != w || rgba.size() < needed)
    return false;

  const size_t rowBytes = w * 3;
  const size_t pad = (4 - (rowBytes % 4)) % 4;
  const size_t imageSize = (rowBytes + pad) * h;
  if(imageSize > 0xffffffffu - 54u)
    return false;    // the header's size fields are 32-bit

  uint8_t header[54] = {};
  header[0] = 'B';
  header[1] = 'M';
  PutLE32(header + 2, (uint32_t)(54u + imageSize));
  PutLE32(header + 10, 54u);
  PutLE32(header + 14, 40u);
  PutLE32(header + 18, (uint32_t)w);
  PutLE32(header + 22, (uint32_t)h);
  PutLE16(header + 26, 1u);
  PutLE16(header + 28, 24u);
  PutLE32(header + 34, (uint32_t)imageSize);

  FILE *f = fopen(path, "wb");
  if(f == NULL)
    return false;

  bool bOk = fwrite(header, 1, sizeof(header), f) == sizeof(header);
  std::vector<uint8_t> row(rowBytes + pad, 0);
  for(size_t line = 0; line < h && bOk; line++)
  {
    const size_t y = h - 1 - line;    // BMP rows are bottom-up
    const uint8_t *px = rgba.data() + y * w * 4;
    for(size_t x = 0; x < w; x++)
    {
      row[x * 3 + 0] = px[x * 4 + 2];
      row[x * 3 + 1] = px[x * 4 + 1];
      row[x * 3 + 2] = px[x * 4 + 0];
    }
    bOk = fwrite(row.data(), 1, row.size(), f) == row.size();
  }
  return (fclose(f) == 0) && bOk;
}

bool WriteBMPImage(const char *path, const ImageData &img)
{
  if(!img.Valid())
    return false;
  return WriteBMP(path, img.m_Rgba, img.m_Width, img.m_Height);
}

//: Reads the BMPs this tool writes, and the ones a viewer writes back out: uncompressed 24- or 32-bit,
//: bottom-up or top-down, with row padding. Anything else -- palettes, RLE, 16-bit -- is refused with
//: what it is, because a half-read image would be a wrong difference rather than a missing one.
bool ReadBMPImage(const char *path, ImageData &img, std::string &why)
{
  FILE *f = fopen(path, "rb");
  if(f == NULL)
  {
    why = Fmt("cannot read %s", path);
    return false;
  }

  std::vector<uint8_t> bytes;
  {
    uint8_t chunk[64 * 1024];
    size_t got = 0;
    while((got = fread(chunk, 1, sizeof(chunk), f)) > 0)
      bytes.insert(bytes.end(), chunk, chunk + got);
    const bool bReadError = ferror(f) != 0;
    fclose(f);
    if(bReadError)
    {
      why = Fmt("cannot read %s", path);
      return false;
    }
  }

  if(bytes.size() < 54 || bytes[0] != 'B' || bytes[1] != 'M')
  {
    why = Fmt("%s is not a BMP (no BM signature)", path);
    return false;
  }
  const uint32_t dataOffset = GetLE32(&bytes[10]);
  const uint32_t headerSize = GetLE32(&bytes[14]);
  const int32_t width = (int32_t)GetLE32(&bytes[18]);
  const int32_t rawHeight = (int32_t)GetLE32(&bytes[22]);
  const uint16_t planes = GetLE16(&bytes[26]);
  const uint16_t bpp = GetLE16(&bytes[28]);
  if(headerSize < 40 || width <= 0 || rawHeight == 0 || planes != 1)
  {
    why = Fmt("%s is not a plain BMP this tool can read (header %u, %dx%d, %u plane(s))", path,
              (unsigned)headerSize, (int)width, (int)rawHeight, (unsigned)planes);
    return false;
  }
  if(bpp != 24 && bpp != 32)
  {
    why = Fmt("%s is a %u-bit BMP; only uncompressed 24- and 32-bit are read", path, (unsigned)bpp);
    return false;
  }

  const bool bTopDown = rawHeight < 0;
  const int32_t height = bTopDown ? -rawHeight : rawHeight;
  const size_t pixelBytes = (size_t)bpp / 8;
  const size_t rowBytes = (size_t)width * pixelBytes;
  const size_t stride = (rowBytes + 3u) & ~(size_t)3u;
  const size_t need = (size_t)dataOffset + stride * (size_t)(height - 1) + rowBytes;
  if(height <= 0 || need > bytes.size())
  {
    why = Fmt("%s is truncated (%d row(s) of %u bytes do not fit in %u bytes)", path, (int)height,
              (unsigned)rowBytes, (unsigned)bytes.size());
    return false;
  }

  img.m_Width = (int)width;
  img.m_Height = (int)height;
  img.Reset((size_t)width * (size_t)height * 4u, 255);
  for(int y = 0; y < (int)height; y++)
  {
    const size_t srcLine = bTopDown ? (size_t)y : (size_t)(height - 1 - y);
    const uint8_t *src = bytes.data() + (size_t)dataOffset + srcLine * stride;
    uint8_t *dst = img.m_Rgba.data() + (size_t)y * (size_t)width * 4u;
    for(int x = 0; x < (int)width; x++)
    {
      dst[x * 4 + 0] = src[x * pixelBytes + 2];
      dst[x * 4 + 1] = src[x * pixelBytes + 1];
      dst[x * 4 + 2] = src[x * pixelBytes + 0];
      if(pixelBytes == 4)
        dst[x * 4 + 3] = src[x * 4 + 3];
    }
  }
  return true;
}

//: `src` scaled down to fit `maxWidth` x `maxHeight`, the aspect ratio kept, by averaging the source
//: pixels of each destination pixel (a box filter).
//:
//: Averaging rather than picking the nearest pixel because a contact sheet is looked at for *shape*:
//: nearest-neighbour turns a 1920-wide pass into a mess of dropped columns, and the average is what the
//: eye reconstructs anyway. Never scales up -- a thumbnail bigger than its source is a lie about detail.
ImageData DownscaleImage(const ImageData &src, int maxWidth, int maxHeight)
{
  ImageData out;
  if(!src.Valid() || maxWidth <= 0 || maxHeight <= 0)
    return out;
  if(src.m_Width <= maxWidth && src.m_Height <= maxHeight)
    return src;

  const double scale = std::min((double)maxWidth / src.m_Width, (double)maxHeight / src.m_Height);
  const int w = std::max(1, (int)(src.m_Width * scale));
  const int h = std::max(1, (int)(src.m_Height * scale));
  out.m_Width = w;
  out.m_Height = h;
  out.Reset((size_t)w * (size_t)h * 4u, 255);

  for(int y = 0; y < h; y++)
  {
    const int y0 = (int)((int64_t)y * src.m_Height / h);
    int y1 = (int)((int64_t)(y + 1) * src.m_Height / h);
    if(y1 <= y0)
      y1 = y0 + 1;
    for(int x = 0; x < w; x++)
    {
      const int x0 = (int)((int64_t)x * src.m_Width / w);
      int x1 = (int)((int64_t)(x + 1) * src.m_Width / w);
      if(x1 <= x0)
        x1 = x0 + 1;
      uint32_t sum[4] = {0, 0, 0, 0};
      uint32_t count = 0;
      for(int sy = y0; sy < y1 && sy < src.m_Height; sy++)
      {
        const uint8_t *px = src.m_Rgba.data() + ((size_t)sy * src.m_Width + x0) * 4u;
        for(int sx = x0; sx < x1 && sx < src.m_Width; sx++, px += 4)
        {
          sum[0] += px[0];
          sum[1] += px[1];
          sum[2] += px[2];
          sum[3] += px[3];
          count++;
        }
      }
      uint8_t *dst = out.m_Rgba.data() + ((size_t)y * w + x) * 4u;
      for(int k = 0; k < 4; k++)
        dst[k] = count == 0 ? 0 : (uint8_t)(sum[k] / count);
    }
  }
  return out;
}

//: A grid of `tiles`, each drawn at its own size into a `tileWidth` x `tileHeight` cell, on a light
//: grey background with `gutter` pixels between cells and around the edge.
//:
//: Tiles are *not* rescaled here: the caller makes thumbnails that already fit, so a tile keeps the
//: aspect ratio the pass rendered at and the sheet says which pass it was by shape as well as by
//: the index that goes beside it.
ImageData MakeMontage(const std::vector<ImageData> &tiles, int columns, int tileWidth,
                      int tileHeight, int gutter)
{
  ImageData sheet;
  if(tiles.empty() || columns <= 0 || tileWidth <= 0 || tileHeight <= 0 || gutter < 0)
    return sheet;

  const int rows = ((int)tiles.size() + columns - 1) / columns;
  sheet.m_Width = columns * tileWidth + (columns + 1) * gutter;
  sheet.m_Height = rows * tileHeight + (rows + 1) * gutter;
  sheet.Reset((size_t)sheet.m_Width * (size_t)sheet.m_Height * 4u, 255);
  for(size_t i = 0; i < sheet.m_Rgba.size(); i += 4)
  {
    sheet.m_Rgba[i + 0] = 32;
    sheet.m_Rgba[i + 1] = 32;
    sheet.m_Rgba[i + 2] = 32;
  }

  for(size_t i = 0; i < tiles.size(); i++)
  {
    const ImageData &tile = tiles[i];
    if(!tile.Valid())
      continue;
    const int cellX = gutter + (int)(i % (size_t)columns) * (tileWidth + gutter);
    const int cellY = gutter + (int)(i / (size_t)columns) * (tileHeight + gutter);
    const int w = std::min(tile.m_Width, tileWidth);
    const int h = std::min(tile.m_Height, tileHeight);
    for(int y = 0; y < h; y++)
    {
      const uint8_t *src = tile.m_Rgba.data() + (size_t)y * (size_t)tile.m_Width * 4u;
      uint8_t *dst =
          sheet.m_Rgba.data() + ((size_t)(cellY + y) * (size_t)sheet.m_Width + (size_t)cellX) * 4u;
      memcpy(dst, src, (size_t)w * 4u);
    }
  }
  return sheet;
}

//: A 64-bit difference hash of an image: 9x8 greyscale, one bit per horizontally adjacent pair of
//: pixels. Two images that look the same differ in a couple of bits; the same image re-rendered on
//: a different driver typically differs in a handful. It is a *perceptual* number, so it is
//: reported beside the exact one (how many pixels changed) rather than instead of it.
uint64_t DifferenceHash(const ImageData &img)
{
  if(!img.Valid())
    return 0;
  const ImageData grey = DownscaleImage(img, 9, 8);
  if(grey.m_Width < 2 || grey.m_Height < 1)
    return 0;

  uint64_t hash = 0;
  int bit = 0;
  for(int y = 0; y < grey.m_Height && bit < 64; y++)
  {
    for(int x = 0; x + 1 < grey.m_Width && bit < 64; x++, bit++)
    {
      const uint8_t *a = grey.m_Rgba.data() + ((size_t)y * grey.m_Width + x) * 4u;
      const uint8_t *b = a + 4;
      // Rec. 601 luma, integer: what the eye weights, so a blue/red change counts like it looks.
      const int la = (int)((77 * a[0] + 150 * a[1] + 29 * a[2]) >> 8);
      const int lb = (int)((77 * b[0] + 150 * b[1] + 29 * b[2]) >> 8);
      if(la > lb)
        hash |= (uint64_t)1 << bit;
    }
  }
  return hash;
}

//: How many pixels differ, the largest single-channel difference, and the sum of every per-pixel
//: difference. `heat` (optional) receives a picture of the difference: black where nothing changed,
//: brighter the further it moved.
//:
//: Two images of different sizes cannot be compared pixel by pixel, and reporting "0% differ" for
//: them would be a lie, so that is a failure the caller reports.
long long ImagePixelDelta(const ImageData &a, const ImageData &b, int &maxDelta,
                          long long &sumDelta, ImageData *heat)
{
  maxDelta = 0;
  sumDelta = 0;
  if(!a.Valid() || !b.Valid() || a.m_Width != b.m_Width || a.m_Height != b.m_Height)
    return -1;

  long long differing = 0;
  long long sum = 0;
  if(heat != NULL)
  {
    heat->m_Width = a.m_Width;
    heat->m_Height = a.m_Height;
    heat->Reset(a.m_Rgba.size(), 255);
  }

  for(size_t i = 0, px = 0; i + 4 <= a.m_Rgba.size(); i += 4, px++)
  {
    // All four channels: a change in alpha alone is a change, and the schema promises "any channel
    // difference" -- comparing three of four would be a number that disagrees with its own contract.
    int delta = 0;
    for(int k = 0; k < 4; k++)
    {
      const int d = (int)a.m_Rgba[i + k] - (int)b.m_Rgba[i + k];
      const int mag = d < 0 ? -d : d;
      if(mag > delta)
        delta = mag;
    }
    if(delta != 0)
    {
      differing++;
      sum += delta;
      if(delta > maxDelta)
        maxDelta = delta;
    }
    if(heat != NULL)
    {
      uint8_t *dst = heat->m_Rgba.data() + px * 4u;
      // Scaled so a small change is visible at all: 32 units of difference is full red.
      const int level = delta * 8;
      dst[0] = 0;
      dst[1] = (uint8_t)(level > 255 ? 255 : level);
      dst[2] = (uint8_t)(level > 255 ? 255 : level);
    }
  }
  sumDelta = sum;
  return differing;
}

//: The picture a render target shows at the current event, read back as RGBA.
//:
//: The same path `image` and the bundle's `rt/` images use (`TextureDisplay` +
//: `ReadbackOutputTexture`), so a contact sheet shows what those commands show rather than a second
//: interpretation of the texture: the display mapping is what makes a UNORM, float or sRGB target
//: viewable at all.
bool ReadTargetImage(IReplayController *ctrl, ResourceId target, ImageData &img, std::string &why)
{
  if(target == ResourceId::Null())
  {
    why = "no render target is bound at that event";
    return false;
  }
  IReplayOutput *out =
      ctrl->CreateOutput(CreateHeadlessWindowingData(256, 256), ReplayOutputType::Texture);
  if(out == NULL)
  {
    why = Fmt("could not create a texture output for res%s", IdText(target).c_str());
    return false;
  }

  TextureDisplay disp;
  disp.resourceId = target;
  disp.typeCast = CompType::Typeless;
  disp.rangeMin = 0.0f;
  disp.rangeMax = 1.0f;
  out->SetTextureDisplay(disp);
  out->Display();

  const rdcpair<int32_t, int32_t> dims = out->GetDimensions();
  const bytebuf raw = out->ReadbackOutputTexture();
  out->Shutdown();

  img.m_Width = dims.first;
  img.m_Height = dims.second;
  const size_t pixels = (size_t)img.m_Width * (size_t)img.m_Height;
  if(pixels == 0 || (raw.size() != pixels * 4u && raw.size() != pixels * 3u))
  {
    why = Fmt("the engine returned %d byte(s) for a %dx%d image of res%s", (int)raw.size(),
              dims.first, dims.second, IdText(target).c_str());
    img.m_Width = img.m_Height = 0;
    img.m_Rgba.clear();
    return false;
  }

  if(raw.size() == pixels * 4u)
  {
    img.m_Rgba =
        raw;    // RGBA, which is what the display packs when the format has an alpha channel
  }
  else
  {
    // Three bytes per pixel: the display dropped the alpha channel (measured: a 256x256 target of
    // `desktop-2` comes back as 196,608 bytes). Expanded as R,G,B with an opaque alpha -- the
    // bytes are the format's own order and a swap would be *visible* in the picture rather than
    // silent, which is why this is done rather than refusing the target outright.
    img.Reset(pixels * 4u, 255);
    for(size_t i = 0; i < pixels; i++)
    {
      img.m_Rgba[i * 4u + 0] = raw[i * 3u + 0];
      img.m_Rgba[i * 4u + 1] = raw[i * 3u + 1];
      img.m_Rgba[i * 4u + 2] = raw[i * 3u + 2];
    }
  }
  return true;
}

//: The first bound render target of a state, in slot order -- what a pass "produced". A pass whose
//: slot 0 is unbound (a depth-only pass, say) still shows something in a later slot, which is more
//: useful than treating it as no output at all.
ResourceId FirstRenderTarget(const D3D12Pipe::State *st)
{
  if(st == NULL)
    return ResourceId::Null();
  for(size_t slot = 0; slot < st->outputMerger.renderTargets.size(); slot++)
  {
    if(st->outputMerger.renderTargets[slot].resource != ResourceId::Null())
      return st->outputMerger.renderTargets[slot].resource;
  }
  return ResourceId::Null();
}
