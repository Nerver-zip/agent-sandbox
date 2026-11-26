"""Project only protobuf fields read by the pinned Antigravity scanner.

This is not a complete protobuf parser. Unsupported or malformed wire data
fails closed; the upstream scanner remains responsible for interpretation.
Projection alone does not prove that accepted string fields contain no secrets.
"""

def read_varint(data, pos):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data): raise ValueError('truncated varint')
        byte = data[pos]; pos += 1
        if shift == 63 and byte > 1: raise ValueError('overflow')
        value |= (byte & 127) << shift
        if byte < 128: return value, pos
    raise ValueError('overflow')


def fields(data):
    pos = 0
    while pos < len(data):
        start = pos
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not number: raise ValueError('invalid tag')
        if wire == 0:
            _, pos = read_varint(data, pos); value = None
        elif wire == 1:
            pos += 8; value = None
        elif wire == 5:
            pos += 4; value = None
        elif wire == 2:
            length, pos = read_varint(data, pos)
            end = pos + length
            if end > len(data): raise ValueError('truncated bytes')
            value = data[pos:end]; pos = end
        else:
            raise ValueError('unsupported wire type')
        if pos > len(data): raise ValueError('truncated fixed')
        yield number, wire, value, data[start:pos]


def enc(n):
    out = bytearray()
    while n > 127: out.append((n & 127) | 128); n >>= 7
    out.append(n)
    return bytes(out)


def safe_text(value):
    """Drop known credential-shaped text from scanner-visible string fields.

    This is defense in depth, not a proof that arbitrary strings are secret-free.
    """
    import re
    from urllib.parse import unquote

    if len(value) > 4096:
        return None
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    decoded = unquote(text)
    patterns = (
        r"(?i)(access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|authorization|bearer\s|private[ _-]?key)",
        r"(?i)(github_pat_|ghp_|ya29\.|sk-[a-z0-9]{12,})",
        r"(?<![A-Za-z0-9])1//[A-Za-z0-9_-]{20,}",
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    )
    if any(re.search(pattern, decoded) for pattern in patterns):
        return None
    return value


def model_id(value):
    import re
    value = safe_text(value)
    if value is None or len(value) > 256 or not re.fullmatch(rb"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
        # Explicit unknown prevents the upstream session-level fallback from
        # assigning a different priced model to this generation.
        return b"unknown"
    return value


def model_label(value):
    value = safe_text(value)
    if value is None or len(value) > 256 or any(byte < 32 or byte == 127 for byte in value):
        return None
    return value


def workspace_uri(value):
    value = safe_text(value)
    if value is None or not value.startswith(b"file://") or any(byte < 32 or byte == 127 for byte in value):
        return None
    return value


def inferred_clock(value):
    # Upstream accepts an 8-byte scalar or nested numeric Timestamp/epoch.
    # Preserve the ambiguous 8-byte representation; project longer nested
    # messages to the numeric fields the scanner reads.
    if len(value) == 8:
        return value
    return project(value, {1: {0, 1}, 2: {0}})


def response_id(value, *, strip=False):
    """Keep stable deduplication without persisting provider response IDs."""
    import hashlib

    if len(value) > 4096:
        raise ValueError("oversized response identifier")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.strip():
        return None
    if strip:
        text = text.strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest().encode("ascii")


def project(data, schema):
    out = bytearray()
    parsed = list(fields(data))
    bad_label = schema is CHAT and any(number == 21 and wire == 2 and model_label(value) is None
                                      for number, wire, value, _ in parsed)
    model_seen = False
    for number, wire, value, raw in parsed:
        rule = schema.get(number)
        if rule is None: continue
        if isinstance(rule, dict):
            if wire != 2:
                raise ValueError("unexpected nested wire type")
            projected = project(value, rule)
            out.extend(enc((number << 3) | 2)); out.extend(enc(len(projected))); out.extend(projected)
        elif isinstance(rule, tuple):
            if wire == 0:
                out.extend(raw)
            elif wire == 2:
                number_value, _ = read_varint(value, 0)
                packed = enc(number_value)
                out.extend(enc((number << 3) | 2))
                out.extend(enc(len(packed)))
                out.extend(packed)
            else:
                raise ValueError("unexpected packed wire type")
        elif callable(rule):
            if wire != 2:
                raise ValueError("unexpected bytes wire type")
            projected = rule(value)
            if schema is CHAT and number == 19:
                model_seen = True
                if bad_label and projected.lower() == b"gemini-default":
                    projected = b"unknown"
            if projected is not None:
                out.extend(enc((number << 3) | 2))
                out.extend(enc(len(projected)))
                out.extend(projected)
        elif wire in rule:
            out.extend(raw)
        else:
            raise ValueError("unexpected scalar wire type")
    if bad_label and not model_seen:
        out.extend(enc((19 << 3) | 2)); out.extend(enc(7)); out.extend(b"unknown")
    return bytes(out)

V = {0}
BYTES = {2}
TIMESTAMP = {1: V, 2: V}
USAGE = {1: V, 2: V, 3: V, 4: V, 5: V, 9: V, 10: V, 11: response_id}
CHAT = {4: USAGE, 9: {4: TIMESTAMP, 10: inferred_clock}, 11: TIMESTAMP,
        12: TIMESTAMP, 19: model_id, 21: model_label}
GEN = {1: CHAT, 2: (0, 2)}
TRAJECTORY = {1: {1: workspace_uri}, 2: TIMESTAMP}
STEP = {1: TIMESTAMP, 9: {11: lambda value: response_id(value, strip=True)}, 20: {3: V}}


def project_database(source, target):
    """Write a new SQLite database containing only the selected fields.

    The caller must supply a stable, already-recovered input and a new output
    path in a private execution environment. The input must never be persisted
    as part of the host snapshot; this helper does not perform capture.
    """
    from contextlib import closing
    import sqlite3

    def integer(value):
        if value is not None and type(value) is not int:
            raise ValueError("invalid SQLite integer")
        return value

    def blob(value):
        if not isinstance(value, bytes) or len(value) > 16 * 1024 * 1024:
            raise ValueError("invalid SQLite blob")
        return value

    with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as src, \
            closing(sqlite3.connect(target)) as dst, dst:
        src.execute("PRAGMA trusted_schema=OFF")
        names = {row[0] for row in src.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if "gen_metadata" not in names:
            return False
        dst.execute("CREATE TABLE gen_metadata(idx INTEGER, data BLOB)")
        for idx, data in src.execute("SELECT idx,data FROM gen_metadata ORDER BY idx"):
            dst.execute("INSERT INTO gen_metadata VALUES (?,?)", (integer(idx), project(blob(data), GEN)))
        if "trajectory_metadata_blob" in names:
            dst.execute("CREATE TABLE trajectory_metadata_blob(data BLOB)")
            for (data,) in src.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1"):
                dst.execute("INSERT INTO trajectory_metadata_blob VALUES (?)", (project(blob(data), TRAJECTORY),))
        if "steps" in names:
            dst.execute("CREATE TABLE steps(idx INTEGER, step_type INTEGER, metadata BLOB)")
            for idx, kind, data in src.execute("SELECT idx,step_type,metadata FROM steps WHERE metadata IS NOT NULL"):
                dst.execute("INSERT INTO steps VALUES (?,?,?)", (integer(idx), integer(kind), project(blob(data), STEP)))
    return True


def _unique_fields(data):
    """Return projected fields only when their interpretation is unambiguous."""
    result = {}
    for number, wire, value, raw in fields(data):
        if number in result:
            raise ValueError("duplicate projected field")
        result[number] = (wire, value, raw)
    return result


def generation_identity(data):
    """Return an unambiguous projected response ID, or None."""
    try:
        root = _unique_fields(data)
        chat = root.get(1)
        if not chat or chat[0] != 2:
            return None
        usage = _unique_fields(chat[1]).get(4)
        if not usage or usage[0] != 2:
            return None
        identity = _unique_fields(usage[1]).get(11)
        if not identity or identity[0] != 2:
            return None
        value = identity[1]
        if len(value) != 64 or any(char not in b"0123456789abcdef" for char in value):
            return None
        return value
    except (TypeError, ValueError):
        return None


def session_has_created_at(data):
    """Check the positive, bounded timestamp used by Tokscale as fallback."""
    try:
        timestamp = _unique_fields(data).get(2)
        if not timestamp or timestamp[0] != 2:
            return False
        fields_by_number = _unique_fields(timestamp[1])
        seconds = fields_by_number.get(1)
        nanos = fields_by_number.get(2)
        if not seconds or seconds[0] != 0 or (nanos and nanos[0] != 0):
            return False
        def integer(raw):
            _, pos = read_varint(raw, 0)
            value, end = read_varint(raw, pos)
            if end != len(raw):
                raise ValueError("invalid timestamp")
            return value
        second = integer(seconds[2])
        nano = integer(nanos[2]) if nanos else 0
        return 0 < second <= ((1 << 63) - 1 - 999) // 1000 and nano < 1_000_000_000
    except (TypeError, ValueError):
        return False


def monotonic_generation_update(old, new):
    """Accept only the same identified generation with nondecreasing counters.

    This compares raw counter fields; Tokscale still interprets and prices the
    resulting record. Other changed fields, missing identities, and duplicate
    fields remain ambiguous and are rejected.
    """
    try:
        a = _unique_fields(old)
        b = _unique_fields(new)
        if set(a) != set(b) or 1 not in a or any(a[k][2] != b[k][2] for k in a if k != 1):
            return False
        if a[1][0] != 2 or b[1][0] != 2:
            return False
        a = _unique_fields(a[1][1])
        b = _unique_fields(b[1][1])
        if set(a) != set(b) or 4 not in a or any(a[k][2] != b[k][2] for k in a if k != 4):
            return False
        if a[4][0] != 2 or b[4][0] != 2:
            return False
        a = _unique_fields(a[4][1])
        b = _unique_fields(b[4][1])
        if 11 not in a or 11 not in b or a[11][0] != 2 or b[11][0] != 2 or a[11][1] != b[11][1]:
            return False
        identity = a[11][1]
        if len(identity) != 64 or any(c not in b"0123456789abcdef" for c in identity):
            return False
        counters = {2, 3, 4, 5, 9, 10}
        for key in (set(a) | set(b)) - counters:
            if key not in a or key not in b or a[key][2] != b[key][2]:
                return False
        def value(item):
            if item is None:
                return 0
            wire, _, raw = item
            if wire != 0:
                raise ValueError("invalid counter")
            _, pos = read_varint(raw, 0)
            number, end = read_varint(raw, pos)
            if end != len(raw):
                raise ValueError("invalid counter")
            return number
        before = {key: value(a.get(key)) for key in counters}
        after = {key: value(b.get(key)) for key in counters}
        if any(after[key] < before[key] for key in counters):
            return False
        # Tokscale falls back to total output minus reasoning when response
        # output is absent. Increasing the raw counters can still *reduce*
        # the reported response-output component in that representation.
        # Reject such representation changes instead of reimplementing the
        # scanner's output reconciliation here.
        if before[10] == 0 and before[3] > 0 and any(
                after[key] != before[key] for key in (3, 9, 10)):
            return False
        return True
    except (TypeError, ValueError):
        return False
