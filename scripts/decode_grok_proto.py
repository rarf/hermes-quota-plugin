"""Decode the captured Grok billing protobuf fully (field-by-field walk)."""
import struct
import datetime

RAW = r"C:/Users/ronal/grok_last_response.bin"


def parse(m):
    out, i = [], 0
    while i < len(m):
        key = m[i]
        i += 1
        fn, wire = key >> 3, key & 7
        if wire == 0:
            v, s = 0, 0
            while True:
                b = m[i]
                i += 1
                v |= (b & 0x7F) << s
                s += 7
                if not b & 0x80:
                    break
            out.append((fn, wire, v))
        elif wire == 2:
            ln, s = 0, 0
            while True:
                b = m[i]
                i += 1
                ln |= (b & 0x7F) << s
                s += 7
                if not b & 0x80:
                    break
            out.append((fn, wire, m[i : i + ln]))
            i += ln
        elif wire == 5:
            out.append((fn, wire, struct.unpack("<f", m[i : i + 4])[0]))
            i += 4
        elif wire == 1:
            out.append((fn, wire, int.from_bytes(m[i : i + 8], "little")))
            i += 8
        else:
            break
    return out


def iso(v):
    try:
        return datetime.datetime.fromtimestamp(v, tz=datetime.timezone.utc).isoformat()
    except Exception:
        return "?"


def dump(m, ind=""):
    for fn, w, v in parse(m):
        if w == 0:
            guess = ""
            if 1_700_000_000 < v < 2_000_000_000:
                guess = f"  -> TS {iso(v)}"
            print(f"{ind}fn{fn}: varint {v}{guess}")
        elif w == 5:
            print(f"{ind}fn{fn}: float {v}")
        elif w == 2:
            printable = all(32 <= c < 127 for c in v)
            if printable and v:
                print(f'{ind}fn{fn}: str "{v.decode()}"')
            else:
                print(f"{ind}fn{fn}: msg[{len(v)}]")
                try:
                    dump(v, ind + "  ")
                except Exception:
                    print(f"{ind}  <bin> {v.hex()}")


raw = open(RAW, "rb").read()
msg = raw
if msg[:1] == b"\x00" and len(msg) >= 5:
    ln = int.from_bytes(msg[1:5], "big")
    msg = msg[5 : 5 + ln]
dump(msg)
