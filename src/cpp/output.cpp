// z.output — the JSON/text writer every command prints through
//
// Part of replay_dump; the internal API is declared in common.h.

#include "common.h"

bool g_json = false;
int g_indent = 0;

//: The output format is fixed for the run (it comes from `--json` before anything else happens), so
//: the commands ask rather than read the flag: a raw global read at thirty call sites is how the
//: text and JSON paths drift apart.
bool IsJson()
{
  return g_json;
}

//: One row of an array. `g_firstRow` is reset by `ArrayOpen` so commas land between rows and never
//: after the last one -- a trailing comma is not JSON.
bool g_firstRow = true;

//: The number inside a `ResourceId`. Its value is private and RenderDoc's own stringiser for it is
//: not exported, so this copies the 8 bytes out exactly the way RenderDoc's
//: `DoStringise<ResourceId>` does (`core.cpp`) -- the struct is a `uint64_t` wrapper by design. Ids
//: then read the same way the offline tool prints them (`res1234`).
//:
//: The two static_asserts are what make that byte copy defensible rather than hopeful: `memcpy`
//: into a `uint64_t` is only defined for a trivially copyable source of the same size
//: ([basic.types], [class.mem]), and if either stops being true this fails to compile instead of
//: reading whatever the object happens to look like.
std::string JsonEscape(const char *s)
{
  std::string out;
  for(; s && *s; s++)
  {
    switch(*s)
    {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if((unsigned char)*s < 0x20)
        {
          char buf[8];
          snprintf(buf, sizeof(buf), "\\u%04x", (unsigned)(unsigned char)*s);
          out += buf;
        }
        else
        {
          out += *s;
        }
    }
  }
  return out;
}

std::string JsonEscape(const std::string &s)
{
  return JsonEscape(s.c_str());
}

std::string JsonEscape(const rdcstr &s)
{
  return JsonEscape(s.c_str());
}

void Indent()
{
  if(g_json)
    for(int i = 0; i < g_indent; i++)
      fputs("  ", stdout);
}

//: One `key: value` line in text mode, `"key": "value",` in JSON mode.
//:
//: The value is escaped: it routinely carries a Windows path (`capture`), an engine-supplied name
//: or a debug message, and a single unescaped backslash in any of them makes the whole document
//: unparseable -- which is exactly what `--json` did before this.
void Field(const char *key, const std::string &value, bool last)
{
  Indent();
  if(g_json)
    printf("\"%s\": \"%s\"%s\n", key, JsonEscape(value).c_str(), last ? "" : ",");
  else
    printf("%-18s %s\n", key, value.c_str());
}

void Field(const char *key, const rdcstr &value, bool last)
{
  Field(key, std::string(value.c_str()), last);
}

void Field(const char *key, long long value, bool last)
{
  Indent();
  if(g_json)
    printf("\"%s\": %lld%s\n", key, value, last ? "" : ",");
  else
    printf("%-18s %lld\n", key, value);
}

//: A boolean field as JSON's `true`/`false` rather than 1/0: the state blocks below are read by
//: rules that compare against `true`, and a reader of the bundle should not have to remember which
//: number means on.
void Flag(const char *key, bool value, bool last)
{
  Indent();
  if(g_json)
    printf("\"%s\": %s%s\n", key, value ? "true" : "false", last ? "" : ",");
  else
    printf("%-18s %s\n", key, value ? "on" : "off");
}

//: `g_firstRow` is the "this item needs no separator" state of the array *currently* being written.
//: Arrays nest -- a stage object holds arrays of its own -- so the state nests too: `ArrayOpen`
//: saves the enclosing array's flag and `ArrayClose` restores it. Without this, writing an *empty*
//: nested array left the enclosing array looking like it had just started, the next item was
//: written with no comma, and the document did not parse: `states/<eid>.shaders.json` was invalid
//: for the hobby capture for exactly that reason (a stage whose signature arrays were empty), and
//: `resources.json` would have hit it on any resource with an empty usage list.
std::vector<bool> g_firstRowStack;

void ArrayOpen(const char *key)
{
  Indent();
  if(g_json)
    printf("\"%s\": [\n", key);
  g_firstRowStack.push_back(g_firstRow);
  g_indent++;
  g_firstRow = true;
}

void ArrayClose(bool last)
{
  g_indent--;
  Indent();
  if(g_json)
    printf("]%s\n", last ? "" : ",");
  if(!g_firstRowStack.empty())
  {
    g_firstRow = g_firstRowStack.back();
    g_firstRowStack.pop_back();
  }
}

//: The separator in front of the next item of the current array, and the bookkeeping for the one
//: after it. Writing it *before* an item is what makes a trailing comma impossible: there is no
//: point at which the writer knows an item is last, and a comma after the last one is not JSON.
const char *TakeSeparator()
{
  const char *sep = g_firstRow ? "" : ",\n";
  g_firstRow = false;
  return sep;
}

void Row(const std::string &text)
{
  if(g_json)
  {
    Indent();
    printf("%s\"%s\"\n", TakeSeparator(), JsonEscape(text.c_str()).c_str());
  }
  else
  {
    printf("%s\n", text.c_str());
  }
}

//: An item of the enclosing array that is an object rather than a string -- `draws` writes one row
//: per event. Sharing `TakeSeparator` with `Row` is what keeps that row from carrying a trailing
//: comma, which it used to do for every event including the last.
void ObjectRow(const std::string &object)
{
  if(g_json)
  {
    Indent();
    printf("%s%s\n", TakeSeparator(), object.c_str());
  }
  else
  {
    printf("%s\n", object.c_str());
  }
}

//: The `{` of an object that is an item of the enclosing array, for an object whose members are
//: written by the calls that follow rather than assembled into one string first. The separator goes
//: in front of it exactly as for `Row`/`ObjectRow`, so an object item never carries a trailing
//: comma either, and the members inside indentation one level deeper than the brace.
void ObjectOpen()
{
  if(IsJson())
  {
    Indent();
    fputs(TakeSeparator(), stdout);
    fputs("{\n", stdout);
    g_indent++;
  }
}

//: The matching `}`. Nothing follows it: whether the *enclosing* array has more items is the next
//: item's separator to write, and whether the array is the last member is its `ArrayClose` to say.
void ObjectClose(bool last)
{
  if(IsJson())
  {
    g_indent--;
    Indent();
    printf("}%s\n", last ? "" : ",");
  }
}

//: A *named* object: `ObjectOpen` above opens an anonymous one, which is what an array element is.
//: A block of state that belongs under a key of its own (`outputMerger`) writes the key first; the
//: separator rules are the arrays': the caller says whether it is the last key by what it writes
//: next, and fields inside end on `Field(..., true)`.
void ObjectOpenKey(const char *key)
{
  if(!IsJson())
    return;
  Indent();
  printf("\"%s\": {\n", key);
  g_indent++;
}

//: printf-style formatting for the output lines. The SAL annotation makes the compiler check every
//: call site's arguments against the format string, which is the only way a varargs helper like
//: this stays honest -- a mismatch is undefined behaviour ([expr.call]: the argument must match the
//: parameter after the default argument promotions), and it is also how the tool would print
//: nonsense. The buffer grows to fit instead of truncating at a fixed size, because a truncated
//: JSON row is not valid JSON and a truncated text row is not the data the reader asked for.
std::string FmtV(_Printf_format_string_ const char *fmt, va_list args)
{
  va_list counted;
  va_copy(counted, args);
  const int needed = vsnprintf(NULL, 0, fmt, counted);
  va_end(counted);

  if(needed <= 0)
    return std::string();

  std::vector<char> buf((size_t)needed + 1);
  vsnprintf(buf.data(), buf.size(), fmt, args);
  return std::string(buf.data(), (size_t)needed);
}

std::string Fmt(_Printf_format_string_ const char *fmt, ...)
{
  va_list args;
  va_start(args, fmt);
  std::string text = FmtV(fmt, args);
  va_end(args);
  return text;
}

//: The bundle's documents are JSON whatever the terminal was asked for: they are read by the
//: offline tool, not by a person, so `dump` forces the JSON writer on for the duration of one
//: document. SHA-256 of a file, through the OS (`bcrypt`), so the manifest's hashes are not a
//: second implementation of a digest to get wrong. An empty result means it could not be read, and
//: the reason is on stderr; a bundle whose hashes are missing is a bundle nobody can check.
