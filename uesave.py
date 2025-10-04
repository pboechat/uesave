import gzip
import struct
import zlib
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path
from pprint import pprint
from typing import *

# optional compressors
try:
    import lz4.frame as lz4f  # type: ignore
except Exception:  # pragma: no cover
    lz4f = None  # lazy check later

try:
    import zstandard as zstd  # type: ignore
except Exception:  # pragma: no cover
    zstd = None

MAGIC = b'GVAS'  # UE SaveGame header magic


@dataclass
class SaveFile:
    version: int
    header: dict
    properties: dict


def read_u32_le(data: bytes, offset: int) -> Tuple[int, int]:
    return struct.unpack_from('<I', data, offset)[0], offset + 4


def read_i32_le(data: bytes, offset: int) -> Tuple[int, int]:
    return struct.unpack_from('<i', data, offset)[0], offset + 4


def read_u16_le(data: bytes, offset: int) -> Tuple[int, int]:
    return struct.unpack_from('<H', data, offset)[0], offset + 2


def read_string(data: bytes, offset: int) -> Tuple[str, int]:
    """Read UE FString: int32 length. If negative, it's UTF-16LE and -length is the character count.
    Length typically includes the null terminator; strip trailing NULs.
    """
    strlen, offset = read_i32_le(data, offset)
    if strlen == 0:
        return "", offset
    if strlen < 0:
        # UTF-16LE, -strlen characters (including terminator)
        count = -strlen
        nbytes = count * 2
        raw = data[offset: offset + nbytes]
        offset += nbytes
        s = raw.decode('utf-16-le', errors='ignore')
    else:
        raw = data[offset: offset + strlen]
        offset += strlen
        s = raw.decode('utf-8', errors='ignore')
    s = s.rstrip('\x00')
    return s, offset


def read_guid(data: bytes, offset: int) -> Tuple[str, int]:
    """Read a 16-byte GUID and return as standard hex string."""
    g = data[offset: offset + 16]
    offset += 16
    # UE stores GUID as raw 16 bytes; represent in canonical form
    # Break into 4-2-2-2-6 bytes per RFC 4122
    if len(g) != 16:
        return "", offset
    part1 = g[0:4][::-1].hex()  # Little-endian to big for common display
    part2 = g[4:6][::-1].hex()
    part3 = g[6:8][::-1].hex()
    part4 = g[8:10].hex()
    part5 = g[10:16].hex()
    guid = f"{part1}-{part2}-{part3}-{part4}-{part5}"
    return guid, offset


def parse_gvas_header(data: bytes, offset: int = 0) -> Tuple[dict, int]:
    if data[offset: offset + 4] != MAGIC:
        raise ValueError("Not a GVAS header at given offset")
    offset += 4

    header: Dict[str, Any] = {"magic": "GVAS"}

    save_game_version, offset = read_i32_le(data, offset)
    header["save_game_version"] = save_game_version

    # some saves include both UE4 and UE5 file versions. Try dual first; fallback to single.
    off_try = offset
    file_version_ue4, off_try = read_i32_le(data, off_try)
    file_version_ue5, off_try = read_i32_le(data, off_try)
    # peek engine version as uint16s to validate plausibility
    eng_major_try, _ = read_u16_le(data, off_try)
    eng_minor_try, _ = read_u16_le(data, off_try + 2)
    dual_layout_plausible = 0 <= eng_major_try <= 50 and 0 <= eng_minor_try <= 50

    if dual_layout_plausible:
        offset = off_try
        header["file_version_ue4"] = file_version_ue4
        header["file_version_ue5"] = file_version_ue5
    else:
        # Fallback: only one package file version present
        file_version_single, offset = read_i32_le(data, offset)
        header["package_file_version"] = file_version_single

    # engine version: uint16 major/minor/patch, uint32 changelist, branch (FString)
    eng_major, offset = read_u16_le(data, offset)
    eng_minor, offset = read_u16_le(data, offset)
    eng_patch, offset = read_u16_le(data, offset)
    eng_changelist, offset = read_u32_le(data, offset)
    eng_branch, offset = read_string(data, offset)
    header["engine_version"] = {
        "major": eng_major,
        "minor": eng_minor,
        "patch": eng_patch,
        "changelist": eng_changelist,
        "branch": eng_branch,
    }
    # CustomVersions + SaveGameClassName (layouts vary by engine/version).

    def _plausible_class_name(s: str) -> bool:
        if not (1 <= len(s) <= 2048):
            return False
        allowed = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./\\:-$[]()<>@!%+,' \"")
        ok = sum(1 for ch in s if ch in allowed)
        if ok / max(1, len(s)) < 0.75:
            return False
        # common markers in UE class paths
        markers = ("/", ".", "_C", "BP_", "SaveGame", "Class", "/Game/")
        if any(m in s for m in markers):
            return True
        return True

    base = offset
    parsed_ok = False

    # attempt A: format(i32), count(i32), (GUID+i32)*count, class_name
    try:
        fmt, off_cv = read_i32_le(data, base)
        cnt, off_cv = read_i32_le(data, off_cv)
        if not (0 <= cnt <= 10000 and 0 <= fmt <= 10):
            raise ValueError
        customs_a: List[Dict[str, Any]] = []
        for _ in range(cnt):
            guid, off_cv = read_guid(data, off_cv)
            ver, off_cv = read_i32_le(data, off_cv)
            customs_a.append({"guid": guid, "version": ver})
        cls, off_cv = read_string(data, off_cv)
        if _plausible_class_name(cls):
            header["custom_versions_format"] = fmt
            header["custom_versions"] = customs_a
            header["save_game_class_name"] = cls
            offset = off_cv
            parsed_ok = True
    except Exception:
        parsed_ok = False

    # attempt B: format, count, (GUID+i32+FString)*count, class_name
    if not parsed_ok:
        try:
            fmt, off_cv = read_i32_le(data, base)
            cnt, off_cv = read_i32_le(data, off_cv)
            if not (0 <= cnt <= 10000 and 0 <= fmt <= 10):
                raise ValueError
            customs_b: List[Dict[str, Any]] = []
            for _ in range(cnt):
                guid, off_cv = read_guid(data, off_cv)
                ver, off_cv = read_i32_le(data, off_cv)
                fname, off_cv = read_string(data, off_cv)
                customs_b.append(
                    {"guid": guid, "version": ver, "friendly_name": fname})
            cls, off_cv = read_string(data, off_cv)
            if _plausible_class_name(cls):
                header["custom_versions_format"] = fmt
                header["custom_versions"] = customs_b
                header["save_game_class_name"] = cls
                offset = off_cv
                parsed_ok = True
        except Exception:
            parsed_ok = False

    # attempt C: count, (GUID+i32)*count, class_name
    if not parsed_ok:
        try:
            cnt, off_cv = read_i32_le(data, base)
            if not (0 <= cnt <= 10000):
                raise ValueError
            customs_c: List[Dict[str, Any]] = []
            for _ in range(cnt):
                guid, off_cv = read_guid(data, off_cv)
                ver, off_cv = read_i32_le(data, off_cv)
                customs_c.append({"guid": guid, "version": ver})
            cls, off_cv = read_string(data, off_cv)
            if _plausible_class_name(cls):
                header["custom_versions"] = customs_c
                header["save_game_class_name"] = cls
                offset = off_cv
                parsed_ok = True
        except Exception:
            parsed_ok = False

    # attempt D: count, (GUID+i32+FString)*count, class_name
    if not parsed_ok:
        try:
            cnt, off_cv = read_i32_le(data, base)
            if not (0 <= cnt <= 10000):
                raise ValueError
            customs_d: List[Dict[str, Any]] = []
            for _ in range(cnt):
                guid, off_cv = read_guid(data, off_cv)
                ver, off_cv = read_i32_le(data, off_cv)
                fname, off_cv = read_string(data, off_cv)
                customs_d.append(
                    {"guid": guid, "version": ver, "friendly_name": fname})
            cls, off_cv = read_string(data, off_cv)
            if _plausible_class_name(cls):
                header["custom_versions"] = customs_d
                header["save_game_class_name"] = cls
                offset = off_cv
                parsed_ok = True
        except Exception:
            parsed_ok = False

    # fallback: try class name only
    if not parsed_ok:
        cls, off_cv = read_string(data, base)
        if _plausible_class_name(cls):
            header["save_game_class_name"] = cls
            offset = off_cv

    return header, offset


class DecompressionError(Exception):
    pass


def _try_zlib(data: bytes) -> Optional[bytes]:
    # zlib header typically starts with 0x78 0x01/0x9C/0xDA, but not guaranteed
    try:
        return zlib.decompress(data)
    except Exception:
        return None


def _try_deflate_raw(data: bytes) -> Optional[bytes]:
    # Raw deflate (no zlib/gzip headers)
    try:
        return zlib.decompress(data, wbits=-15)
    except Exception:
        return None


def _try_gzip(data: bytes) -> Optional[bytes]:
    # GZIP magic: 1F 8B
    if len(data) >= 2 and data[0:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data)
        except Exception:
            return None
    # even if no magic, try anyway as a fallback
    try:
        return gzip.decompress(data)
    except Exception:
        return None


def _try_lz4(data: bytes) -> Optional[bytes]:
    if lz4f is None:
        return None
    try:
        return lz4f.decompress(data)
    except Exception:
        return None


def _try_zstd(data: bytes) -> Optional[bytes]:
    if zstd is None:
        return None
    try:
        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(data)
    except Exception:
        return None


def decompress_payload(raw_bytes: bytes, method: str = "auto") -> bytes:
    """
    Decompress bytes using a chosen method.

    method options:
    - 'none': return raw_bytes as-is
    - 'zlib': zlib with header
    - 'deflate': raw DEFLATE (no headers)
    - 'gzip': gzip stream
    - 'lz4': LZ4 frame (requires lz4 package)
    - 'zstd': Zstandard (requires zstandard package)
    - 'auto': try common methods heuristically in order
    """
    m = method.lower()
    if m == "none":
        return raw_bytes
    if m == "zlib":
        try:
            return zlib.decompress(raw_bytes)
        except Exception as e:
            raise DecompressionError(f"zlib failed: {e}")
    if m == "deflate":
        try:
            return zlib.decompress(raw_bytes, wbits=-15)
        except Exception as e:
            raise DecompressionError(f"deflate failed: {e}")
    if m == "gzip":
        try:
            return gzip.decompress(raw_bytes)
        except Exception as e:
            raise DecompressionError(f"gzip failed: {e}")
    if m == "lz4":
        if lz4f is None:
            raise DecompressionError(
                "lz4 not available. Install 'lz4' package.")
        try:
            return lz4f.decompress(raw_bytes)
        except Exception as e:
            raise DecompressionError(f"lz4 failed: {e}")
    if m == "zstd":
        if zstd is None:
            raise DecompressionError(
                "zstd not available. Install 'zstandard' package.")
        try:
            dctx = zstd.ZstdDecompressor()
            return dctx.decompress(raw_bytes)
        except Exception as e:
            raise DecompressionError(f"zstd failed: {e}")

    # auto heuristic: try fast header checks, then attempts
    # 1) GZIP magic
    if len(raw_bytes) >= 2 and raw_bytes[:2] == b"\x1f\x8b":
        out = _try_gzip(raw_bytes)
        if out is not None:
            return out
    # 2) Zstd frame magic: 28 B5 2F FD
    if len(raw_bytes) >= 4 and raw_bytes[:4] == b"\x28\xb5\x2f\xfd":
        out = _try_zstd(raw_bytes)
        if out is not None:
            return out
    # 3) LZ4 frame magic: 04 22 4D 18
    if len(raw_bytes) >= 4 and raw_bytes[:4] == b"\x04\x22\x4d\x18":
        out = _try_lz4(raw_bytes)
        if out is not None:
            return out

    # 4) Try zlib
    out = _try_zlib(raw_bytes)
    if out is not None:
        return out

    # 5) Try raw deflate
    out = _try_deflate_raw(raw_bytes)
    if out is not None:
        return out

    # 6) Try gzip even if magic absent
    out = _try_gzip(raw_bytes)
    if out is not None:
        return out

    # 7) Try lz4/zstd as last resorts
    out = _try_lz4(raw_bytes)
    if out is not None:
        return out
    out = _try_zstd(raw_bytes)
    if out is not None:
        return out

    raise DecompressionError(
        "Could not decompress payload. Try --compression none|zlib|deflate|gzip|lz4|zstd."
    )


def parse_properties(data: bytes, offset: int, end_offset: int) -> Tuple[dict, int]:
    properties = {}
    while offset < end_offset:
        prop_name, offset = read_string(data, offset)
        if prop_name == "None":  # sentinel
            break
        prop_type, offset = read_string(data, offset)
        # read property size, array index etc.
        prop_size, offset = read_u32_le(data, offset)
        _, offset = read_u32_le(data, offset)
        # PropertyTag extras: BoolProperty stores its value in tag;
        # property GUID flag + guid may be present.
        has_guid = None
        # Bool value byte comes before GUID flag in tag for BoolProperty
        bool_in_tag = None
        if prop_type == "BoolProperty":
            if offset < end_offset:
                bool_in_tag = bool(data[offset])
                offset += 1
        # property GUID presence flag (heuristic: 0 or 1 byte)
        if offset < end_offset and data[offset] in (0, 1):
            has_guid = data[offset]
            offset += 1
            if has_guid == 1:
                # consume 16-byte guid
                offset += 16

        # handle each type's value/body
        if prop_type == "IntProperty":
            value, offset = read_i32_le(data, offset)
        elif prop_type == "FloatProperty":
            value = struct.unpack_from('<f', data, offset)[0]
            offset += 4
        elif prop_type == "StrProperty":
            value, offset = read_string(data, offset)
        elif prop_type == "NameProperty":
            # Name is serialized as FString in SaveGame
            value, offset = read_string(data, offset)
        elif prop_type == "TextProperty":
            # TextProperty can be complex; treat as raw bytes for now
            value = data[offset: offset + prop_size]
            offset += prop_size
        elif prop_type == "ByteProperty":
            # may have enum name header (FString). if prop_size > 1, likely an enum string follows.
            # heuristic: read possible enum name header without advancing value if not plausible.
            enum_name, new_off = read_string(data, offset)
            if 0 <= len(enum_name) <= 256:
                offset = new_off
            # value can be a single byte or an FString (for EnumProperty variants)
            if prop_size == 1 and offset < end_offset:
                value = data[offset]
                offset += 1
            else:
                value, offset = read_string(data, offset)
        elif prop_type == "BoolProperty":
            value = bool_in_tag if bool_in_tag is not None else False
            # Bool payload typically omitted; prop_size often 0
        elif prop_type == "ArrayProperty":
            # ArrayProperty tag header contains inner type (FString)
            inner_type, offset = read_string(data, offset)
            # for Struct inner type, a struct name may follow;
            # we won't parse inner elements deeply yet.
            # read raw body to keep alignment; optional: parse known simple inner types
            value_bytes = data[offset: offset + prop_size]
            offset += prop_size
            value = {"__array_type": inner_type, "__raw": value_bytes}
        elif prop_type == "StructProperty":
            # StructProperty tag header: struct type (FString) and a struct GUID (16 bytes)
            struct_type, offset = read_string(data, offset)
            if offset + 16 <= end_offset:
                struct_guid = data[offset: offset + 16].hex()
                offset += 16
            else:
                struct_guid = None
            # for now, keep raw payload bytes
            value_bytes = data[offset: offset + prop_size]
            offset += prop_size
            value = {"__struct_type": struct_type,
                     "__guid": struct_guid, "__raw": value_bytes}
        elif prop_type == "Int64Property":
            value = struct.unpack_from('<q', data, offset)[0]
            offset += 8
        elif prop_type == "UInt64Property":
            value = struct.unpack_from('<Q', data, offset)[0]
            offset += 8
        elif prop_type == "DoubleProperty":
            value = struct.unpack_from('<d', data, offset)[0]
            offset += 8
        else:
            # unknown type: skip raw bytes
            value = data[offset: offset + prop_size]
            offset += prop_size

        offset += 1

        properties[prop_name] = value

    return properties, offset


def load_savefile(path: Path, compression: str = "auto") -> SaveFile:
    raw = path.read_bytes()

    data = raw
    offset = 0

    # if not starting with GVAS, try to auto-decompress the entire file first.
    if not data.startswith(MAGIC):
        try:
            candidate = decompress_payload(data, method=compression)
            if candidate.startswith(MAGIC):
                data = candidate
            # else leave as-is and try parsing below (some games embed GVAS later)
        except DecompressionError:
            # leave data as-is; header parse may still succeed if GVAS isn't at start
            pass

    # if still no magic at start, search within first 256 bytes
    if not data.startswith(MAGIC):
        idx = data.find(MAGIC, 0, 256)
        if idx != -1:
            offset = idx
        else:
            raise ValueError(
                "GVAS magic not found. This may not be a UE SaveGame file.")

    header, offset = parse_gvas_header(data, offset)

    # properties follow header until sentinel "None"
    properties, _ = parse_properties(data, offset, len(data))

    return SaveFile(version=header.get("save_game_version", 0), header=header, properties=properties)


def main():
    parser = ArgumentParser()
    parser.add_argument('--savefile', '-s', type=Path,
                        help='Path to the Unreal Engine save file')
    parser.add_argument('--compression', '-c', default='auto',
                        choices=['auto', 'none', 'zlib',
                                 'deflate', 'gzip', 'lz4', 'zstd'],
                        help='Compression method to use for payload (default: auto)')
    parser.add_argument('--selftest', action='store_true',
                        help='Run a quick decompressor self-test and exit')
    parser.add_argument('--dump-header', action='store_true',
                        help='Parse and print header only, then exit')
    args = parser.parse_args()

    if args.selftest:
        results = {}
        sample = b"hello unreal!" * 5
        results['none'] = (decompress_payload(sample, 'none') == sample)
        results['zlib'] = (decompress_payload(
            zlib.compress(sample), 'zlib') == sample)
        # deflate raw
        results['deflate'] = (decompress_payload(
            zlib.compress(sample)[2:-4], 'deflate') == sample)
        results['gzip'] = (decompress_payload(
            gzip.compress(sample), 'gzip') == sample)
        if lz4f is not None:
            results['lz4'] = (decompress_payload(
                lz4f.compress(sample), 'lz4') == sample)
        else:
            results['lz4'] = 'skipped (missing lz4)'
        if zstd is not None:
            cctx = zstd.ZstdCompressor()
            results['zstd'] = (decompress_payload(
                cctx.compress(sample), 'zstd') == sample)
        else:
            results['zstd'] = 'skipped (missing zstandard)'
        print('Self-test results:')
        pprint(results)
        return

    if not args.savefile:
        raise SystemExit('Please provide --savefile')

    save_file = load_savefile(
        args.savefile,
        compression=args.compression
    )

    if getattr(args, 'dump_header', False):
        pprint(save_file.header)
        return

    pprint(save_file)


if __name__ == '__main__':
    main()
