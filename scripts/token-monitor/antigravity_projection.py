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


def project(data, schema):
    out = bytearray()
    for number, wire, value, raw in fields(data):
        rule = schema.get(number)
        if rule is None: continue
        if isinstance(rule, dict):
            if wire != 2: continue
            projected = project(value, rule)
            out.extend(enc((number << 3) | 2)); out.extend(enc(len(projected))); out.extend(projected)
        elif wire in rule:
            out.extend(raw)
    return bytes(out)

V = {0}
BYTES = {2}
TIMESTAMP = {1: V, 2: V}
USAGE = {1: V, 2: V, 3: V, 4: V, 5: V, 9: V, 10: V, 11: BYTES}
CHAT = {4: USAGE, 9: {4: TIMESTAMP, 10: BYTES}, 11: TIMESTAMP,
        12: TIMESTAMP, 19: BYTES, 21: BYTES}
GEN = {1: CHAT, 2: {0, 2}}
TRAJECTORY = {1: {1: BYTES}, 2: TIMESTAMP}
STEP = {1: TIMESTAMP, 9: {11: BYTES}, 20: {3: V}}


def project_database(source, target):
    """Write a new SQLite database containing only the selected fields.

    The caller must supply a stable, already-recovered input and a new output
    path in a private execution environment. The input must never be persisted
    as part of the host snapshot; this helper does not perform capture.
    """
    from contextlib import closing
    import sqlite3

    with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as src, \
            closing(sqlite3.connect(target)) as dst, dst:
        src.execute("PRAGMA trusted_schema=OFF")
        names = {row[0] for row in src.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if "gen_metadata" not in names:
            return False
        dst.execute("CREATE TABLE gen_metadata(idx INTEGER, data BLOB)")
        for idx, data in src.execute("SELECT idx,data FROM gen_metadata ORDER BY idx"):
            dst.execute("INSERT INTO gen_metadata VALUES (?,?)", (idx, project(data, GEN)))
        if "trajectory_metadata_blob" in names:
            dst.execute("CREATE TABLE trajectory_metadata_blob(data BLOB)")
            for (data,) in src.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1"):
                dst.execute("INSERT INTO trajectory_metadata_blob VALUES (?)", (project(data, TRAJECTORY),))
        if "steps" in names:
            dst.execute("CREATE TABLE steps(idx INTEGER, step_type INTEGER, metadata BLOB)")
            for idx, kind, data in src.execute("SELECT idx,step_type,metadata FROM steps WHERE metadata IS NOT NULL"):
                dst.execute("INSERT INTO steps VALUES (?,?,?)", (idx, kind, project(data, STEP)))
    return True
