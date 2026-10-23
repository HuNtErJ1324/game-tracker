"""Parsers for Valve KeyValues files (text .vdf/.acf and binary .bin)."""
import re
import struct

_TOKEN = re.compile(r'"((?:\\.|[^"\\])*)"|(\{)|(\})|(//[^\n]*)|([^\s{}"]+)')
_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def _unescape(s):
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), s)


def loads(text):
    """Parse text KeyValues into nested dicts. Duplicate keys: last one wins."""
    root = {}
    stack = [root]
    key = None
    for m in _TOKEN.finditer(text):
        quoted, lbrace, rbrace, comment, bare = m.groups()
        if comment is not None:
            continue
        if lbrace:
            child = {}
            stack[-1][key if key is not None else ""] = child
            stack.append(child)
            key = None
        elif rbrace:
            if len(stack) > 1:
                stack.pop()
            key = None
        else:
            tok = _unescape(quoted) if quoted is not None else bare
            if bare is not None and bare.startswith("["):  # platform conditional, e.g. [$WIN32]
                continue
            if key is None:
                key = tok
            else:
                stack[-1][key] = tok
                key = None
    return root


def load(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return loads(f.read())


def iget(d, *keys, default=None):
    """Case-insensitive nested lookup (Steam mixes 'Software'/'software' etc.)."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        kl = k.lower()
        nxt = None
        for ck, cv in cur.items():
            if ck.lower() == kl:
                nxt = cv
                break
        if nxt is None:
            return default
        cur = nxt
    return cur


# --- binary KeyValues (appcache/stats/*.bin) ---

def _cstr(buf, pos):
    end = buf.index(b"\x00", pos)
    return buf[pos:end].decode("utf-8", errors="replace"), end + 1


def loads_binary(buf):
    pos = 0

    def parse_map():
        nonlocal pos
        out = {}
        while pos < len(buf):
            t = buf[pos]
            pos += 1
            if t in (0x08, 0x0B):
                return out
            key, pos = _cstr(buf, pos)
            if t == 0x00:
                out[key] = parse_map()
            elif t == 0x01:
                out[key], pos = _cstr(buf, pos)
            elif t in (0x02, 0x04, 0x06):
                out[key] = struct.unpack_from("<i", buf, pos)[0]
                pos += 4
            elif t == 0x03:
                out[key] = struct.unpack_from("<f", buf, pos)[0]
                pos += 4
            elif t == 0x07:
                out[key] = struct.unpack_from("<Q", buf, pos)[0]
                pos += 8
            elif t == 0x0A:
                out[key] = struct.unpack_from("<q", buf, pos)[0]
                pos += 8
            elif t == 0x05:
                end = pos
                while buf[end:end + 2] != b"\x00\x00":
                    end += 2
                out[key] = buf[pos:end].decode("utf-16-le", errors="replace")
                pos = end + 2
            else:
                raise ValueError(f"unknown binary KV type 0x{t:02x} at {pos - 1}")
        return out

    return parse_map()


def load_binary(path):
    with open(path, "rb") as f:
        return loads_binary(f.read())
