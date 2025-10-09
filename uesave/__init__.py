import gzip
import struct
import zlib
from abc import ABC, abstractmethod
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


def _read_u32(data: bytes, offset: int) -> Tuple[int, int]:
    return struct.unpack_from('<I', data, offset)[0], offset + 4


def _write_u32(data: bytearray, v: int) -> None:
    data.extend(struct.pack('<I', int(v) & 0xFFFFFFFF))


def _read_i32(data: bytes, offset: int) -> Tuple[int, int]:
    return struct.unpack_from('<i', data, offset)[0], offset + 4


def _write_i32(data: bytearray, v: int) -> None:
    data.extend(struct.pack('<i', int(v)))


def _read_u16(data: bytes, offset: int) -> Tuple[int, int]:
    return struct.unpack_from('<H', data, offset)[0], offset + 2


def _write_u16(data: bytearray, v: int) -> None:
    data.extend(struct.pack('<H', int(v) & 0xFFFF))


def _read_string(data: bytes, offset: int) -> Tuple[str, int]:
    """Read UE FString: int32 length. If negative, it's UTF-16LE and -length is the character count.
    Length typically includes the null terminator; strip trailing NULs.
    """
    strlen, offset = _read_i32(data, offset)
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


def _write_string(data: bytearray, s: str) -> None:
    """Write a UE FString (length includes trailing NUL; 0 means empty)."""
    if not s:
        _write_i32(data, 0)
        return
    try:
        _write_i32(data, len(s) + 1)
        data.extend(s.encode('utf-8'))
        data.extend(b'\x00')
    except UnicodeEncodeError:
        _write_i32(data, -(len(s) * 2 + 1))
        data.extend(s.encode('utf-16-le', errors='ignore'))
        data.extend(b'\x00\x00')


def _read_guid(data: bytes, offset: int) -> Tuple[str, int]:
    """Read a 16-byte GUID and return as standard hex string."""
    guid = data[offset: offset + 16]
    offset += 16
    # UE stores GUID as raw 16 bytes; represent in canonical form
    # break into 4-2-2-2-6 bytes per RFC 4122
    if len(guid) != 16:
        return "", offset
    part1 = guid[0:4][::-1].hex()  # little-endian to big for common display
    part2 = guid[4:6][::-1].hex()
    part3 = guid[6:8][::-1].hex()
    part4 = guid[8:10].hex()
    part5 = guid[10:16].hex()
    guid = f"{part1}-{part2}-{part3}-{part4}-{part5}"
    return guid, offset


def _write_guid(data: bytearray, guid: str) -> None:
    """Write a GUID string as 16 raw bytes."""
    parts = guid.split('-')
    if len(parts) != 5:
        data.extend(b'\x00' * 16)
        return
    try:
        part1 = bytes.fromhex(parts[0])[::-1]  # big-endian to little
        part2 = bytes.fromhex(parts[1])[::-1]
        part3 = bytes.fromhex(parts[2])[::-1]
        part4 = bytes.fromhex(parts[3])
        part5 = bytes.fromhex(parts[4])
        if len(part1) != 4 or len(part2) != 2 or len(part3) != 2 or len(part4) != 2 or len(part5) != 6:
            raise ValueError("Invalid GUID part length")
        data.extend(part1)
        data.extend(part2)
        data.extend(part3)
        data.extend(part4)
        data.extend(part5)
    except Exception:
        data.extend(b'\x00' * 16)


def _read_property(data: bytes, offset: int) -> Tuple[Optional['Property'], int]:
    prop_name, offset = _read_string(data, offset)

    if prop_name == "None" or prop_name == "":
        return None, offset

    prop_type, offset = _read_string(data, offset)

    prop_size, offset = _read_u32(data, offset)
    prop_tag, offset = _read_u32(data, offset)

    prop, offset = PropertyFactory.create_property(
        name=prop_name,
        prop_type=prop_type,
        prop_size=prop_size,
        prop_tag=prop_tag,
        data=data,
        offset=offset
    )

    return prop, offset


def _write_property(data: bytearray, prop: 'Property') -> None:
    _write_string(data, getattr(prop, 'name', ''))
    ptype = prop.__class__.__name__
    _write_string(data, ptype)

    _write_u32(data, prop.size)
    _write_u32(data, prop.tag)

    prop.to_bytes(data)


class Property(ABC):
    def __init__(self, name: str, tag: int, size: int):
        self._name = name
        self._tag = tag
        self._size = size

    @property
    def name(self) -> str:
        return self._name

    @property
    def tag(self) -> int:
        return self._tag

    @property
    def size(self) -> int:
        return self._size

    @property
    @abstractmethod
    def value(self) -> Any:
        pass

    @classmethod
    @abstractmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['Property', int]:
        pass

    @abstractmethod
    def to_bytes(self, data: bytearray) -> None:
        pass


class ArrayProperty(Property):
    def __init__(self, name: str, tag: int, size: int, inner_type: str, array_size: int, values: Union[bytes, list]):
        super().__init__(name, tag, size)
        self._inner_type = inner_type
        self._array_size = array_size
        self._values = values

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
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['ArrayProperty', int]:
        inner_type, offset = _read_string(data, offset)
        assert (data[offset] == 0)
        offset += 1  # null byte
        array_size, offset = _read_u32(data, offset)
        if inner_type == "ByteProperty":
            values = data[offset: offset + prop_size]
            offset += prop_size - 4
        elif inner_type in ["StrProperty", "NameProperty"]:
            values = []
            for i in range(array_size):
                value, offset = _read_string(data, offset)
                values.append(value)
        elif inner_type == "IntProperty":
            values = []
            for i in range(array_size):
                value, offset = _read_i32(data, offset)
                values.append(value)
        elif inner_type == "StructProperty":
            values = []
            end_offset = offset + prop_size
            while offset < end_offset:
                value, offset = _read_property(data, offset)

                if value is None:
                    break

                values.append(value)
        elif inner_type == "FloatProperty":
            values = []
            for i in range(array_size):
                v = struct.unpack_from('<f', data, offset)[0]
                offset += 4
                values.append(v)
        else:
            # Fallback: store raw bytes for unknown inner types to avoid hard failure
            values = data[offset: offset + prop_size]
            offset += prop_size

        return cls(name=name, tag=prop_tag, size=prop_size, inner_type=inner_type, array_size=array_size, values=values), offset

    def to_bytes(self, data: bytearray) -> None:
        _write_string(data, self._inner_type)
        data.append(0)  # null byte
        _write_i32(data, self._array_size)
        if self._inner_type == "ByteProperty":
            if isinstance(self._values, (bytes, bytearray)):
                data.extend(self._values)
                return
            data.extend(int(v) & 0xFF for v in self._values)
            return
        elif self._inner_type in ["StrProperty", "NameProperty"]:
            for v in self._values:
                _write_string(data, str(v))
            return
        elif self._inner_type == "IntProperty":
            for v in self._values:
                _write_i32(data, int(v))
            return
        elif self._inner_type == "StructProperty":
            for val in self._values:
                _write_property(data, val)
            _write_string(data, "None")
            return
        elif self._inner_type == "FloatProperty":
            for v in self._values:
                data.extend(struct.pack('<f', float(v)))
            return
        else:
            # If values is raw bytes (fallback), write as-is
            if isinstance(self._values, (bytes, bytearray)):
                data.extend(self._values)
                return
            raise NotImplementedError(
                f"ArrayProperty inner_type {self._inner_type} serialization not implemented")

    def __str__(self):
        return f"ArrayProperty(name={self._name}, inner_type={self._inner_type}, length={len(self._values)})"


class BoolProperty(Property):
    def __init__(self, name: str, tag: int, size: int, value: bool):
        super().__init__(name, tag, size)
        self._value = value

    @property
    def size(self) -> int:
        return 0  # always 1 byte + 1 null byte, but size field is 0

    @property
    def value(self) -> bool:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['BoolProperty', int]:
        assert (prop_size == 0)
        value = bool(data[offset])
        offset += 1
        assert (data[offset] == 0)
        offset += 1  # null byte
        return cls(name=name, tag=prop_tag, size=prop_size, value=value), offset

    def to_bytes(self, data: bytearray) -> None:
        data.append(int(self._value) & 0xff)
        data.append(0)  # null byte

    def __str__(self):
        return f"BoolProperty(name={self._name}, value={self._value})"


class ByteProperty(Property):
    def __init__(self, name: str, tag: int, size: int, guid: str, value: int):
        super().__init__(name, tag, size)
        self._guid = guid
        self._value = value

    @property
    def value(self) -> Union[int, str]:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['ByteProperty', int]:
        # assert (prop_size == 1)
        guid, offset = _read_string(data, offset)
        assert (data[offset] == 0)
        offset += 1  # null byte
        if prop_size == 1:
            value = data[offset]
            offset += 1
        else:
            value, offset = _read_string(data, offset)
        return cls(name=name, tag=prop_tag, size=prop_size, guid=guid, value=value), offset

    def to_bytes(self, data: bytearray) -> None:
        data.append(0)  # null byte
        if self.size == 1:
            data.append(int(self._value) & 0xFF)
        else:
            _write_string(data, str(self._value))

    def __str__(self):
        return f"ByteProperty(name={self._name}, guid={self._guid}, value={self._value})"


class DoubleProperty(Property):
    def __init__(self, name: str, tag: int, size: int, value: float):
        super().__init__(name, tag, size)
        self._value = value

    @property
    def value(self) -> float:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['DoubleProperty', int]:
        assert (prop_size == 8)
        value = struct.unpack_from('<d', data, offset)[0]
        offset += 8
        return cls(name=name, tag=prop_tag, size=prop_size, value=value), offset

    def to_bytes(self, data: bytearray) -> None:
        data.extend(struct.pack('<d', float(self._value)))

    def __str__(self):
        return f"DoubleProperty(name={self._name}, value={self._value})"


class FloatProperty(Property):
    def __init__(self, name: str, tag: int, size: int, value: float):
        super().__init__(name, tag, size)
        self._value = value

    @property
    def size(self) -> int:
        return 4

    @property
    def value(self) -> float:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['FloatProperty', int]:
        assert (prop_size == 4)
        value = struct.unpack_from('<f', data, offset)[0]
        offset += 4
        return cls(name=name, tag=prop_tag, size=prop_size, value=value), offset

    def to_bytes(self, data: bytearray) -> None:
        data.extend(struct.pack('<f', float(self._value)))

    def __str__(self):
        return f"FloatProperty(name={self._name}, value={self._value})"


class Int64Property(Property):
    def __init__(self, name: str, tag: int, size: int, value: int):
        super().__init__(name, tag, size)
        self._value = value

    @property
    def value(self) -> int:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['Int64Property', int]:
        assert (prop_size == 8)
        value = struct.unpack_from('<q', data, offset)[0]
        offset += 8
        return cls(name=name, tag=prop_tag, size=prop_size, value=value), offset

    def to_bytes(self, data: bytearray) -> None:
        data.extend(struct.pack('<q', int(self._value)))

    def __str__(self):
        return f"Int64Property(name={self._name}, value={self._value})"


class IntProperty(Property):
    def __init__(self, name: str, tag: int, size: int, value: int, int_tag: int):
        super().__init__(name, tag, size)
        self._value = value
        self._int_tag = int_tag & 0xff

    @property
    def value(self) -> int:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['IntProperty', int]:
        assert (prop_size == 4)
        value = _read_i32(data, offset)[0]
        offset += 4
        int_tag = data[offset]
        # assert (int_tag == 0 or int_tag == 0xff)
        offset += 1  # mysterious byte
        return cls(name=name, tag=prop_tag, size=prop_size, value=value, int_tag=int_tag), offset

    def to_bytes(self, data: bytearray) -> None:
        data.extend(struct.pack('<i', int(self._value)))
        data.append(self._int_tag)  # mysterious byte

    def __str__(self):
        return f"IntProperty(name={self._name}, value={self._value})"


class MapProperty(Property):
    def __init__(self, name: str, tag: int, size: int, key_type: str, value_type: str, map_size: int, raw_bytes: bytes):
        super().__init__(name, tag, size)
        self._key_type = key_type
        self._value_type = value_type
        self._map_size = map_size
        self._raw_bytes = raw_bytes

    @property
    def value(self) -> Dict[str, Any]:
        return {"__key_type": self._key_type, "__value_type": self._value_type, "__raw": self._raw_bytes}

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['MapProperty', int]:
        key_type, offset = _read_string(data, offset)
        value_type, offset = _read_string(data, offset)
        assert (data[offset] == 0)
        offset += 1  # null byte
        map_size, offset = _read_u32(data, offset)
        # TODO: parse entries
        raw_bytes = data[offset: offset + prop_size - 5]
        offset += prop_size - 5
        assert (data[offset] == 0)
        offset += 1  # null byte
        return cls(name=name, tag=prop_tag, size=prop_size,
                   key_type=key_type, value_type=value_type,
                   map_size=map_size, raw_bytes=raw_bytes), offset

    def to_bytes(self, data: bytearray) -> None:
        _write_string(data, self._key_type)
        _write_string(data, self._value_type)
        data.append(0)  # null byte
        _write_u32(data, self._map_size)
        data.extend(self._raw_bytes)
        data.append(0)  # null byte

    def __str__(self):
        return f"MapProperty(name={self._name}, key_type={self._key_type}, value_type={self._value_type}, raw_size={len(self._raw_bytes)})"


class NameProperty(Property):
    def __init__(self, name: str, tag: int, size: int, value: str):
        super().__init__(name, tag, size)
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['NameProperty', int]:
        assert (data[offset] == 0)
        offset += 1  # null byte
        value, offset = _read_string(data, offset)
        assert ((len(value) + 4 + 1) == prop_size)
        return cls(name=name, tag=prop_tag, size=prop_size, value=value), offset

    def to_bytes(self, data: bytearray) -> None:
        data.append(0)  # null byte
        _write_string(data, self._value)

    def __str__(self):
        return f"NameProperty(name={self._name}, value={self._value})"


class ObjectProperty(Property):
    def __init__(self, name: str, tag: int, size: int, value: str):
        super().__init__(name, tag, size)
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['ObjectProperty', int]:
        assert (data[offset] == 0)
        offset += 1  # null byte
        value, offset = _read_string(data, offset)
        assert ((len(value) + 4 + 1) == prop_size)
        return cls(name=name, tag=prop_tag, size=prop_size, value=value), offset

    def to_bytes(self, data: bytearray) -> None:
        data.append(0)  # null byte
        _write_string(data, self._value)

    def __str__(self):
        return f"ObjectProperty(name={self._name}, value={self._value})"


class StrProperty(Property):
    def __init__(self, name: str, tag: int, size: int, value: str):
        super().__init__(name, tag, size)
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['StrProperty', int]:
        assert (data[offset] == 0)
        offset += 1  # null byte
        value, offset = _read_string(data, offset)
        assert (prop_size == len(value) + 4 + (1 if value else 0))
        return cls(name=name, tag=prop_tag, size=prop_size, value=value), offset

    def to_bytes(self, data: bytearray) -> None:
        data.append(0)  # null byte
        _write_string(data, self._value)

    def __str__(self):
        return f"StrProperty(name={self._name}, value={self._value})"


class StructProperty(Property):
    def __init__(self, name: str, tag: int, size: int, type: str, guid: Optional[str], fields: List[Property]):
        super().__init__(name, tag, size)
        self._type = type
        self._guid = guid
        self._fields = fields

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
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['StructProperty', int]:
        type, offset = _read_string(data, offset)
        guid, offset = _read_guid(data, offset)

        assert (data[offset] == 0)
        offset += 1  # null byte

        if type == "Quat":
            # special case: Quat is 4 floats
            assert (prop_size == 16)
            x = struct.unpack_from('<f', data, offset)[0]
            y = struct.unpack_from('<f', data, offset + 4)[0]
            z = struct.unpack_from('<f', data, offset + 8)[0]
            w = struct.unpack_from('<f', data, offset + 12)[0]
            offset += 16
            return cls(name=name, tag=prop_tag, size=prop_size, type=type, guid=guid, fields=[
                FloatProperty(name="X", tag=0, size=4, value=x),
                FloatProperty(name="Y", tag=0, size=4, value=y),
                FloatProperty(name="Z", tag=0, size=4, value=z),
                FloatProperty(name="W", tag=0, size=4, value=w),
            ]), offset
        elif type == "Vector":
            # special case: Vector is 3 floats
            assert (prop_size == 12)
            x = struct.unpack_from('<f', data, offset)[0]
            y = struct.unpack_from('<f', data, offset + 4)[0]
            z = struct.unpack_from('<f', data, offset + 8)[0]
            offset += 12
            return cls(name=name, tag=prop_tag, size=prop_size, type=type, guid=guid, fields=[
                FloatProperty(name="X", tag=0, size=4, value=x),
                FloatProperty(name="Y", tag=0, size=4, value=y),
                FloatProperty(name="Z", tag=0, size=4, value=z),
            ]), offset
        elif type == "DateTime":
            # special case: DateTime is int64 ticks
            assert (prop_size == 8)
            ticks = struct.unpack_from('<q', data, offset)[0]
            offset += 8
            return cls(name=name, tag=prop_tag, size=prop_size, type=type, guid=guid, fields=[
                Int64Property(name="Ticks", tag=0, size=8, value=ticks),
            ]), offset
        elif type == "Guid":
            # special case: Guid is 16 bytes
            assert (prop_size == 16)
            raw = data[offset: offset + 16]
            offset += 16
            guid_str = f"{raw[3:4][0]:02x}{raw[2:3][0]:02x}{raw[1:2][0]:02x}{raw[0:1][0]:02x}-" \
                       f"{raw[5:6][0]:02x}{raw[4:5][0]:02x}-" \
                       f"{raw[7:8][0]:02x}{raw[6:7][0]:02x}-" \
                       f"{raw[8:10].hex()}-" \
                       f"{raw[10:16].hex()}"
            return cls(name=name, tag=prop_tag, size=prop_size, type=type, guid=guid, fields=[
                StrProperty(name="Value", tag=0, size=36 +
                            4 + 1, value=guid_str),
            ]), offset

        fields = []
        end_offset = offset + prop_size
        while offset < end_offset:
            field, offset = _read_property(data, offset)

            if field is None:
                break

            fields.append(field)

        return cls(name=name, tag=prop_tag, size=prop_size, type=type, guid=guid, fields=fields), offset

    def to_bytes(self, data: bytearray) -> None:
        _write_string(data, self._type)
        _write_guid(data, self._guid or "")
        data.append(0)  # null byte

        if self._type == "Quat":
            # special case: Quat is 4 floats
            assert (len(self._fields) == 4)
            assert (all(isinstance(field, FloatProperty)
                    for field in self._fields))
            for field in self._fields:
                data.extend(struct.pack('<f', float(field.value)))
            return
        elif self._type == "Vector":
            # special case: Vector is 3 floats
            assert (len(self._fields) == 3)
            assert (all(isinstance(field, FloatProperty)
                    for field in self._fields))
            for field in self._fields:
                data.extend(struct.pack('<f', float(field.value)))
            return

        for field in self._fields:
            _write_property(data, field)
        if len(self._fields) > 0:
            _write_string(data, "None")

    def __str__(self):
        return f"StructProperty(name={self._name}, type={self._type}, guid={self._guid}, fields={len(self._fields)})"


class TextProperty(Property):
    def __init__(self, name: str, tag: int, size: int, value: bytes):
        super().__init__(name, tag, size)
        self._value = value

    @property
    def value(self) -> bytes:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['TextProperty', int]:
        value = data[offset: offset + prop_size]
        offset += prop_size
        offset += 1  # null byte
        return cls(name=name, tag=prop_tag, size=prop_size, value=value), offset

    def __str__(self):
        return f"TextProperty(name={self._name}, value=<bytes len={len(self._value)}>)"

    def to_bytes(self, data: bytearray) -> None:
        data.extend(self._value)
        data.append(0)  # null byte


class UInt64Property(Property):
    def __init__(self, name: str, tag: int, size: int, value: int):
        super().__init__(name, tag, size)
        self._value = value

    @property
    def value(self) -> int:
        return self._value

    @classmethod
    def from_bytes(cls, name: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple['UInt64Property', int]:
        assert (prop_size == 8)
        value = struct.unpack_from('<Q', data, offset)[0]
        offset += 8
        return cls(name=name, tag=prop_tag, size=prop_size, value=value), offset

    def __str__(self):
        return f"UInt64Property(name={self._name}, value={self._value})"

    def to_bytes(self, data: bytearray) -> None:
        data.extend(struct.pack('<Q', int(self._value)))


class PropertyFactory:
    _TYPE_MAP: Dict[str, Type[Property]] = {}

    @classmethod
    def _build_type_map(cls):
        for subclass in Property.__subclasses__():
            cls._TYPE_MAP[subclass.__name__] = subclass

    @classmethod
    def create_property(cls, name: str, prop_type: str, prop_size: int, prop_tag: int, data: bytes, offset: int) -> Tuple[Property, int]:
        if not cls._TYPE_MAP:
            cls._build_type_map()
        prop_cls = cls._TYPE_MAP.get(prop_type)
        if prop_cls is None:
            raise ValueError(f"Unknown property type: {prop_type}")
        return prop_cls.from_bytes(name, prop_size, prop_tag, data, offset)


@dataclass
class SaveFile:
    header: dict
    properties: List[Property]


def _read_gvas_header(data: bytes, offset: int = 0) -> Tuple[dict, int]:
    if data[offset: offset + 4] != MAGIC:
        raise ValueError("Not a GVAS header at given offset")
    offset += 4

    header: Dict[str, Any] = {"magic": "GVAS"}

    save_game_version, offset = _read_i32(data, offset)
    header["save_game_version"] = save_game_version

    # some saves include both UE4 and UE5 file versions. Try dual first; fallback to single.
    off_try = offset
    file_version_ue4, off_try = _read_i32(data, off_try)
    file_version_ue5, off_try = _read_i32(data, off_try)
    # peek engine version as uint16s to validate plausibility
    eng_major_try, _ = _read_u16(data, off_try)
    eng_minor_try, _ = _read_u16(data, off_try + 2)
    dual_layout_plausible = 0 <= eng_major_try <= 50 and 0 <= eng_minor_try <= 50

    if dual_layout_plausible:
        offset = off_try
        header["file_version_ue4"] = file_version_ue4
        header["file_version_ue5"] = file_version_ue5
    else:
        # fallback: only one package file version present
        file_version_single, offset = _read_i32(data, offset)
        header["package_file_version"] = file_version_single

    # engine version: uint16 major/minor/patch, uint32 changelist, branch (FString)
    eng_major, offset = _read_u16(data, offset)
    eng_minor, offset = _read_u16(data, offset)
    eng_patch, offset = _read_u16(data, offset)
    eng_changelist, offset = _read_u32(data, offset)
    eng_branch, offset = _read_string(data, offset)
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

    fmt, offset = _read_i32(data, offset)
    cnt, offset = _read_i32(data, offset)
    if not (0 <= cnt <= 10000 and 0 <= fmt <= 10):
        raise ValueError

    customs_versions: List[Dict[str, Any]] = []
    for _ in range(cnt):
        guid, offset = _read_guid(data, offset)
        version, offset = _read_i32(data, offset)
        customs_versions.append({"guid": guid, "version": version})

    cls_name, offset = _read_string(data, offset)
    assert (_plausible_class_name(cls_name))

    header["custom_versions_format"] = fmt
    header["custom_versions"] = customs_versions
    header["save_game_class_name"] = cls_name

    return header, offset


def _write_gvas_header(data: bytearray, header: Dict[str, Any]) -> None:
    # magic
    data.extend(MAGIC)

    # save game version
    _write_i32(data, header['save_game_version'])

    # dual file versions (UE4/UE5)
    if 'file_version_ue4' in header:
        assert ('file_version_ue5' in header)
        _write_i32(data, header['file_version_ue4'])
        _write_i32(data, header['file_version_ue5'])
    else:
        _write_i32(data, header['package_file_version'])

    # engine version
    ev = header['engine_version']
    _write_u16(data, ev['major'])
    _write_u16(data, ev['minor'])
    _write_u16(data, ev['patch'])
    _write_u32(data, ev['changelist'])
    _write_string(data, ev['branch'])

    # custom versions (simple format: fmt, count, (GUID+i32)*count)
    cv_fmt = header['custom_versions_format']
    custom_versions = header['custom_versions']
    _write_i32(data, cv_fmt)
    _write_i32(data, len(custom_versions))
    for entry in custom_versions:
        _write_guid(data, entry['guid'])
        _write_i32(data, entry['version'])

    # save game class name
    _write_string(data, header['save_game_class_name'])


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


def _read_properties(data: bytes, offset: int, end_offset: int) -> Tuple[List[Property], int]:
    properties = []
    while offset < end_offset:
        prop, offset = _read_property(data, offset)

        if prop is None:
            continue

        properties.append(prop)

    return properties, offset


def _write_properties(data: bytearray, properties: List[Property]) -> None:
    for prop in properties:
        _write_property(data, prop)

    _write_string(data, 'None')


def read_savefile(path: Path, compression: str = "auto") -> SaveFile:
    data = path.read_bytes()
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

    header, offset = _read_gvas_header(data, offset)

    # properties follow header until sentinel "None"
    properties, _ = _read_properties(data, offset, len(data))

    return SaveFile(header=header, properties=properties)


def write_savefile(path: Path, save: SaveFile) -> None:
    data = bytearray()
    # TODO: add support to compression
    _write_gvas_header(data, save.header or {})
    _write_properties(data, save.properties)
    Path(path).write_bytes(bytes(data))
