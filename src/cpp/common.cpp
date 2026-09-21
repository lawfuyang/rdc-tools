// z.common — the bodies of the types common.h declares
//
// Part of replay_dump; the internal API is declared in common.h.
//
// The header is a list of what one module may call in another, and it is read far more often than
// it is implemented: a body in the middle of that list is a reader's problem, and the declaration
// is the contract anyway. So every definition that needs more than one line lives here, and nothing
// else does -- the three types whose members a second module calls directly: `CaptureStdout`, which
// swaps the stdout descriptor for the length of a document; `ImageData`'s two members; and
// `SweepRules::Feed`, the sweep's stop rules, which the device-free selftest pins without a
// capture.
//
// They are together rather than spread over the modules that happen to use them (image.cpp,
// bundle.cpp, output.cpp) for one reason: a reader looking for the body of something declared in
// common.h would otherwise have to guess which of those files owns it, and the header cannot say,
// because these types are shared *because* more than one module uses them.
//
// A one-line body stays in the header (`CaptureStdout::Ok`, the `JsonDocument` guard, `Progress`'s
// constructor): moving a `return` out of sight of its own class is a cost with no reader's benefit.

#include "common.h"

CaptureStdout::CaptureStdout(const char *path)
{
  const int fd = _fileno(stdout);
  m_Saved = _dup(fd);
  m_File = fopen(path, "wb");
  if(m_File != NULL && m_Saved >= 0)
  {
    _dup2(_fileno(m_File), fd);
    // Buffered only when the caller has said that every engine call is behind it. Buffering a
    // document written *between* engine calls makes the host faster and changes the bundle --
    // measured both ways, see REFERENCE 9.
    if(DocumentBuffering())
    {
      setvbuf(stdout, NULL, _IOFBF, kDocBuffer);
      m_bBuffered = true;
    }
  }
}

CaptureStdout::~CaptureStdout()
{
  fflush(stdout);
  if(m_Saved >= 0)
    _dup2(m_Saved, _fileno(stdout));
  if(m_bBuffered)
    setvbuf(stdout, NULL, _IONBF, 0);    // the console, and the next command, go back to unbuffered
  if(m_File != NULL)
    fclose(m_File);
  if(m_Saved >= 0)
    _close(m_Saved);
}

bool ImageData::Valid() const
{
  return m_Width > 0 && m_Height > 0 && m_Rgba.size() == (size_t)m_Width * (size_t)m_Height * 4u;
}

void ImageData::Reset(size_t count, uint8_t value)
{
  // `rdcarray` (which `bytebuf` is) has neither `assign` nor a two-argument `resize`: clear, size, fill.
  m_Rgba.clear();
  m_Rgba.resize(count);
  for(size_t i = 0; i < m_Rgba.size(); i++)
    m_Rgba[i] = (byte)value;
}

SweepRules::Step SweepRules::Feed(bool bHasState, int eid)
{
  if(!bHasState)
  {
    // Only after something was found: a capture whose first event is id 841 must be walked to it,
    // not declared empty at id 256.
    if(m_LastEid > 0 && ++m_EmptyRun >= kEmptyRunStop)
      return StopEmptyRun;
    return Continue;
  }
  m_EmptyRun = 0;
  m_LastEid = eid;
  m_Count++;
  if(m_MaxEvents > 0 && m_Count >= (size_t)m_MaxEvents)
    return StopMaxEvents;
  if(m_Count >= m_IdBudget)
    return StopIdBudget;
  return Continue;
}
