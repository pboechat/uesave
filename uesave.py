import gzip
import struct
import zlib
from abc import ABC, abstractmethod
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path
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


class Property(ABC):
    def __init__(self, name: str):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    @abstractmethod
    def size(self) -> int:
        pass

    @property
    @abstractmethod
    def value(self) -> Any:
        pass

    @classmethod
    @abstractmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['Property', int]:
        pass


class ArrayProperty(Property):
    def __init__(self, name: str, inner_type: str, values: Any):
        super().__init__(name)
        self._inner_type = inner_type
        self._values = list(values)

    @property
    def size(self) -> int:
        return sum(value.size for value in self._values)

    @property
    def value(self) -> Dict[str, Any]:
        return {"__array_type": self._inner_type, "__values": self._values}

    @property
    def inner_type(self) -> str:
        return self._inner_type

    def __getitem__(self, index: int) -> Property:
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self):
        return iter(self._values)

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['ArrayProperty', int]:
        inner_type, offset = read_string(data, offset)
        offset += 1
        _, offset = read_u32_le(data, offset)
        if inner_type == "ByteProperty":
            values = data[offset: offset + prop_size]
            offset += prop_size - 4
        elif inner_type == "StructProperty":
            values = []
            while offset < prop_size:
                val_prop_name, offset = read_string(data, offset)

                if val_prop_name == "None":
                    break

                val_prop_type, offset = read_string(data, offset)

                val_prop_size, offset = read_u32_le(data, offset)
                _, offset = read_u32_le(data, offset)

                value, offset = PropertyFactory.create_property(
                    name=val_prop_name,
                    prop_type=val_prop_type,
                    prop_size=val_prop_size,
                    data=data,
                    offset=offset
                )

                values.append(value)
        else:
            raise NotImplementedError(
                f"ArrayProperty of type {inner_type} not implemented")

        return cls(name=name, inner_type=inner_type, values=values), offset

    def __str__(self):
        return f"ArrayProperty(name={self._name}, inner_type={self._inner_type}, length={len(self._values)})"


class BoolProperty(Property):
    def __init__(self, name: str, value: bool):
        super().__init__(name)
        self._value = value

    @property
    def size(self) -> int:
        return 1

    @property
    def value(self) -> bool:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['BoolProperty', int]:
        assert (prop_size == 0)
        value = bool(data[offset])
        offset += 1
        offset += 1  # ?
        return cls(name=name, value=value), offset

    def __str__(self):
        return f"BoolProperty(name={self._name}, value={self._value})"


class ByteProperty(Property):
    def __init__(self, name: str, guid: str, value: int):
        super().__init__(name)
        self._guid = guid
        self._value = value

    @property
    def size(self) -> int:
        return 1

    @property
    def value(self) -> int:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['ByteProperty', int]:
        assert (prop_size == 1)
        guid, offset = read_string(data, offset)
        offset += 1
        value = data[offset]
        offset += 1
        return cls(name=name, guid=guid, value=value), offset

    def __str__(self):
        return f"ByteProperty(name={self._name}, guid={self._guid}, value={self._value})"


class DoubleProperty(Property):
    def __init__(self, name: str, value: float):
        super().__init__(name)
        self._value = value

    @property
    def size(self) -> int:
        return 8

    @property
    def value(self) -> float:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['DoubleProperty', int]:
        assert (prop_size == 8)
        value = struct.unpack_from('<d', data, offset)[0]
        offset += 8
        return cls(name=name, value=value), offset

    def __str__(self):
        return f"DoubleProperty(name={self._name}, value={self._value})"


class FloatProperty(Property):
    def __init__(self, name: str, value: float):
        super().__init__(name)
        self._value = value

    @property
    def size(self) -> int:
        return 4

    @property
    def value(self) -> float:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['FloatProperty', int]:
        assert (prop_size == 4)
        value = struct.unpack_from('<f', data, offset)[0]
        offset += 4
        return cls(name=name, value=value), offset

    def __str__(self):
        return f"FloatProperty(name={self._name}, value={self._value})"


class Int64Property(Property):
    def __init__(self, name: str, value: int):
        super().__init__(name)
        self._value = value

    @property
    def size(self) -> int:
        return 8

    @property
    def value(self) -> int:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['Int64Property', int]:
        assert (prop_size == 8)
        value = struct.unpack_from('<q', data, offset)[0]
        offset += 8
        return cls(name=name, value=value), offset

    def __str__(self):
        return f"Int64Property(name={self._name}, value={self._value})"


class IntProperty(Property):
    def __init__(self, name: str, value: int):
        super().__init__(name)
        self._value = value

    @property
    def size(self) -> int:
        return 4

    @property
    def value(self) -> int:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['IntProperty', int]:
        assert (prop_size == 4)
        value = read_i32_le(data, offset)[0]
        offset += 4
        offset += 1
        return cls(name=name, value=value), offset

    def __str__(self):
        return f"IntProperty(name={self._name}, value={self._value})"


class MapProperty(Property):
    def __init__(self, name: str, key_type: str, value_type: str, raw_bytes: bytes):
        super().__init__(name)
        self._key_type = key_type
        self._value_type = value_type
        self._raw_bytes = raw_bytes

    @property
    def size(self) -> int:
        return len(self._raw_bytes)

    @property
    def value(self) -> Dict[str, Any]:
        return {"__key_type": self._key_type, "__value_type": self._value_type, "__raw": self._raw_bytes}

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['MapProperty', int]:
        key_type, offset = read_string(data, offset)
        value_type, offset = read_string(data, offset)
        offset += 1
        map_size, offset = read_u32_le(data, offset)
        prop_size -= 5
        raw_bytes = data[offset: offset + prop_size]
        offset += prop_size
        offset += 1
        return cls(name=name, key_type=key_type, value_type=value_type, raw_bytes=raw_bytes), offset

    def __str__(self):
        return f"MapProperty(name={self._name}, key_type={self._key_type}, value_type={self._value_type}, raw_size={len(self._raw_bytes)})"


class NameProperty(Property):
    def __init__(self, name: str, value: str):
        super().__init__(name)
        self._value = value

    @property
    def size(self) -> int:
        return len(self._value) + 4 + 1

    @property
    def value(self) -> str:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['NameProperty', int]:
        offset += 1  # null byte
        value, offset = read_string(data, offset)
        assert ((len(value) + 4 + 1) == prop_size)
        return cls(name=name, value=value), offset

    def __str__(self):
        return f"NameProperty(name={self._name}, value={self._value})"


class ObjectProperty(Property):
    def __init__(self, name: str, value: str):
        super().__init__(name)
        self._value = value

    @property
    def size(self) -> int:
        return len(self._value) + 4 + 1

    @property
    def value(self) -> str:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['ObjectProperty', int]:
        offset += 1  # null byte
        value, offset = read_string(data, offset)
        assert ((len(value) + 4 + 1) == prop_size)
        return cls(name=name, value=value), offset

    def __str__(self):
        return f"ObjectProperty(name={self._name}, value={self._value})"


class StrProperty(Property):
    def __init__(self, name: str, value: str):
        super().__init__(name)
        self._value = value

    @property
    def size(self) -> int:
        return len(self._value) + 4 + (1 if self._value else 0)

    @property
    def value(self) -> str:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['StrProperty', int]:
        offset += 1  # null byte
        value, offset = read_string(data, offset)
        assert (prop_size == len(value) + 4 + (1 if value else 0))
        return cls(name=name, value=value), offset

    def __str__(self):
        return f"StrProperty(name={self._name}, value={self._value})"


class StructProperty(Property):
    def __init__(self, name: str, type: str, guid: Optional[str], fields: List[Property]):
        super().__init__(name)
        self._type = type
        self._guid = guid
        self._fields = fields

    @property
    def size(self) -> int:
        return sum(field.size for field in self._fields)

    @property
    def value(self) -> Dict[str, Any]:
        return {
            "__type": self._type,
            "__guid": self._guid,
            "__fields": self._fields,
        }

    @property
    def type(self) -> str:
        return self._type

    @property
    def guid(self) -> Optional[str]:
        return self._guid

    @property
    def fields(self) -> List[Property]:
        return self._fields

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['StructProperty', int]:
        type, offset = read_string(data, offset)

        guid = data[offset: offset + 16].hex()

        offset += 16
        offset += 1

        if type == "Quat":
            # special case: Quat is 4 floats
            assert (prop_size == 16)
            x = struct.unpack_from('<f', data, offset)[0]
            y = struct.unpack_from('<f', data, offset + 4)[0]
            z = struct.unpack_from('<f', data, offset + 8)[0]
            w = struct.unpack_from('<f', data, offset + 12)[0]
            offset += 16
            return cls(name=name, type=type, guid=guid, fields=[
                FloatProperty(name="X", value=x),
                FloatProperty(name="Y", value=y),
                FloatProperty(name="Z", value=z),
                FloatProperty(name="W", value=w),
            ]), offset
        elif type == "Vector":
            # special case: Vector is 3 floats
            assert (prop_size == 12)
            x = struct.unpack_from('<f', data, offset)[0]
            y = struct.unpack_from('<f', data, offset + 4)[0]
            z = struct.unpack_from('<f', data, offset + 8)[0]
            offset += 12
            return cls(name=name, type=type, guid=guid, fields=[
                FloatProperty(name="X", value=x),
                FloatProperty(name="Y", value=y),
                FloatProperty(name="Z", value=z),
            ]), offset

        fields = []
        while offset < prop_size:
            field_prop_name, offset = read_string(data, offset)

            if field_prop_name == "None":
                break

            field_prop_type, offset = read_string(data, offset)

            field_prop_size, offset = read_u32_le(data, offset)
            _, offset = read_u32_le(data, offset)

            field, offset = PropertyFactory.create_property(
                name=field_prop_name,
                prop_type=field_prop_type,
                prop_size=field_prop_size,
                data=data,
                offset=offset
            )

            fields.append(field)

        return cls(name=name, type=type, guid=guid, fields=fields), offset

    def __str__(self):
        return f"StructProperty(name={self._name}, type={self._type}, guid={self._guid}, fields={len(self._fields)})"


class TextProperty(Property):
    def __init__(self, name: str, value: bytes):
        super().__init__(name)
        self._value = value

    @property
    def size(self) -> int:
        return len(self._value)

    @property
    def value(self) -> bytes:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['TextProperty', int]:
        value = data[offset: offset + prop_size]
        offset += prop_size
        return cls(name=name, value=value), offset

    def __str__(self):
        return f"TextProperty(name={self._name}, value=<bytes len={len(self._value)}>)"


class UInt64Property(Property):
    def __init__(self, name: str, value: int):
        super().__init__(name)
        self._value = value

    @property
    def size(self) -> int:
        return 8

    @property
    def value(self) -> int:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, data: bytes, offset: int) -> Tuple['UInt64Property', int]:
        assert (prop_size == 8)
        value = struct.unpack_from('<Q', data, offset)[0]
        offset += 8
        return cls(name=name, value=value), offset

    def __str__(self):
        return f"UInt64Property(name={self._name}, value={self._value})"


class PropertyFactory:
    _TYPE_MAP: Dict[str, Type[Property]] = {}

    @classmethod
    def _build_type_map(cls):
        for subclass in Property.__subclasses__():
            cls._TYPE_MAP[subclass.__name__] = subclass

    @classmethod
    def create_property(cls, name: str, prop_type: str, prop_size: int, data: bytes, offset: int) -> Tuple[Property, int]:
        if not cls._TYPE_MAP:
            cls._build_type_map()
        prop_cls = cls._TYPE_MAP.get(prop_type)
        if prop_cls is None:
            raise ValueError(f"Unknown property type: {prop_type}")
        return prop_cls.from_bytes(name, prop_size, data, offset)


@dataclass
class SaveFile:
    version: int
    header: dict
    properties: List[Property]


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
        # fallback: only one package file version present
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

    # 4) try zlib
    out = _try_zlib(raw_bytes)
    if out is not None:
        return out

    # 5) try raw deflate
    out = _try_deflate_raw(raw_bytes)
    if out is not None:
        return out

    # 6) try gzip even if magic absent
    out = _try_gzip(raw_bytes)
    if out is not None:
        return out

    # 7) try lz4/zstd as last resorts
    out = _try_lz4(raw_bytes)
    if out is not None:
        return out
    out = _try_zstd(raw_bytes)
    if out is not None:
        return out

    raise DecompressionError(
        "Could not decompress payload. Try --compression none|zlib|deflate|gzip|lz4|zstd."
    )


def parse_properties(data: bytes, offset: int, end_offset: int) -> Tuple[List[Property], int]:
    properties = []
    while offset < end_offset:
        prop_name, offset = read_string(data, offset)

        if prop_name == "None":
            break

        prop_type, offset = read_string(data, offset)

        prop_size, offset = read_u32_le(data, offset)
        _, offset = read_u32_le(data, offset)

        prop, offset = PropertyFactory.create_property(
            name=prop_name,
            prop_type=prop_type,
            prop_size=prop_size,
            data=data,
            offset=offset
        )

        properties.append(prop)

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
        print(results)
        return

    if not args.savefile:
        raise SystemExit('Please provide --savefile')

    save_file = load_savefile(
        args.savefile,
        compression=args.compression
    )

    print("Header:")
    print("Magic:", save_file.header.get("magic", ""))
    print("Version:", save_file.header.get("version", 0))
    print("File Versions:")
    if "package_file_version" in save_file.header:
        print("  Package:", save_file.header["package_file_version"])
    print("Engine Version:")
    ev = save_file.header.get("engine_version", {})
    print(f"  {ev.get('major', 0)}.{ev.get('minor', 0)}.{ev.get('patch', 0)} "
          f"(changelist {ev.get('changelist', 0)}, branch '{ev.get('branch', '')}')")
    print("SaveGame Class Name:",
          save_file.header.get("save_game_class_name", ""))

    def print_prop(prop: Property, indent: int = 0):
        prefix = ' ' * indent
        print(f"{prefix}{prop}")
        if isinstance(prop, StructProperty):
            for f in prop.fields:
                print_prop(f, indent + 4)
        elif isinstance(prop, ArrayProperty):
            if prop.inner_type == "ByteProperty":
                print(f"{prefix}    <{len(prop)} bytes>")
            elif prop.inner_type == "StructProperty":
                for i in range(0, len(prop)):
                    print_prop(prop[i], indent + 4)
            else:
                raise NotImplementedError(
                    f"ArrayProperty of type {prop.inner_type} not implemented")

    for prop in save_file.properties:
        print_prop(prop)


if __name__ == '__main__':
    main()
