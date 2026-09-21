"""Bit-level message parser driven by a versioned message definition."""

from typing import Any, Dict, List, Optional

from .checksum import compute


class ParseError(Exception):
    """Raised when a message cannot be parsed against its definition."""


class BitReader:
    """Reads bits MSB-first from a byte string."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0  # current bit position

    @property
    def remaining(self) -> int:
        return len(self.data) * 8 - self.pos

    def read(self, nbits: int) -> int:
        if nbits <= 0:
            raise ParseError(f"bit_length must be positive, got {nbits}")
        if nbits > self.remaining:
            raise ParseError(
                f"message too short: need {nbits} bits at offset {self.pos}, "
                f"only {self.remaining} bits left"
            )
        value = 0
        for _ in range(nbits):
            byte = self.data[self.pos // 8]
            bit = (byte >> (7 - (self.pos % 8))) & 1
            value = (value << 1) | bit
            self.pos += 1
        return value


_OP_SYMBOLS = {
    "eq": "==",
    "ne": "!=",
    "gt": ">",
    "ge": ">=",
    "lt": "<",
    "le": "<=",
    "in": "in",
}


def _eval_condition(condition: Dict[str, Any], values: Dict[str, Any]) -> tuple[bool, Any]:
    field = condition["field"]
    if field not in values:
        raise ParseError(
            f"cannot evaluate condition: referenced field '{field}' was not parsed "
            f"(skipped by an earlier branch)"
        )
    actual = values[field]
    op = condition["op"]
    expected = condition["value"]
    if op == "eq":
        return actual == expected, actual
    if op == "ne":
        return actual != expected, actual
    if op == "gt":
        return actual > expected, actual
    if op == "ge":
        return actual >= expected, actual
    if op == "lt":
        return actual < expected, actual
    if op == "le":
        return actual <= expected, actual
    if op == "in":
        return actual in expected, actual
    raise ParseError(f"unsupported condition operator: {op}")


def _read_numeric(reader: BitReader, field: Dict[str, Any]) -> int:
    n = field["bit_length"]
    if field.get("endianness", "big") == "little":
        # validated at registration: n % 8 == 0
        raw = 0
        for i in range(n // 8):
            raw |= reader.read(8) << (8 * i)
    else:
        raw = reader.read(n)
    if field["type"] == "int" and raw >= (1 << (n - 1)):
        raw -= 1 << n  # two's complement
    return raw


def _read_field(reader: BitReader, field: Dict[str, Any]) -> tuple[Any, Optional[int]]:
    """Returns (display_value, raw_int_for_conditions)."""
    ftype = field["type"]
    n = field["bit_length"]
    if ftype == "bool":
        raw = reader.read(1)
        return bool(raw), raw
    if ftype in ("uint", "int"):
        raw = _read_numeric(reader, field)
        value: Any = raw
        scale = field.get("scale")
        offset = field.get("offset")
        if scale is not None or offset is not None:
            value = round(raw * (scale if scale is not None else 1.0) + (offset or 0.0), 6)
        return value, raw
    if ftype == "hex":
        raw = reader.read(n)
        return f"0x{raw:0{n // 4}x}", None
    if ftype == "ascii":
        raw_bytes = bytes(reader.read(8) for _ in range(n // 8))
        return raw_bytes.decode("ascii", errors="replace"), None
    raise ParseError(f"unsupported field type: {ftype}")


def _repeat_count(repeat: Dict[str, Any], values: Dict[str, Any], name: str) -> int:
    """Repeat count = raw value of the referenced uint field; must not exceed
    the declared max."""
    count_field = repeat["field"]
    if count_field not in values:
        raise ParseError(
            f"cannot determine repeat count for '{name}': field '{count_field}' "
            f"was not parsed (skipped by an earlier branch)"
        )
    count = values[count_field]
    max_count = repeat["max"]
    if count > max_count:
        raise ParseError(
            f"repeat count for field '{name}' is {count}, exceeding the declared "
            f"max {max_count}"
        )
    return count


def parse_message(
    fields: List[Dict[str, Any]],
    checksum_cfg: Optional[Dict[str, Any]],
    data: bytes,
) -> Dict[str, Any]:
    reader = BitReader(data)
    values: Dict[str, Any] = {}  # raw values, used by conditions
    out_fields: List[Dict[str, Any]] = []
    branch_path: List[Dict[str, Any]] = []
    checksum_info: Optional[Dict[str, Any]] = None

    for field in fields:
        name = field["name"]
        condition = field.get("condition")
        if condition:
            matched, actual = _eval_condition(condition, values)
            symbol = _OP_SYMBOLS[condition["op"]]
            branch_path.append(
                {
                    "field": name,
                    "condition": f"{condition['field']} {symbol} {condition['value']}",
                    "actual_value": actual,
                    "matched": matched,
                    "action": "parsed" if matched else "skipped",
                }
            )
            if not matched:
                continue

        bit_offset = reader.pos

        repeat = field.get("repeat")
        if repeat:
            # Registration guarantees the count field is a non-repeated uint
            # and that repeat fields are never the checksum field.
            count = _repeat_count(repeat, values, name)
            items: List[Any] = []
            raws: List[int] = []
            for _ in range(count):
                item, item_raw = _read_field(reader, field)
                items.append(item)
                if item_raw is not None:
                    raws.append(item_raw)
            entry = {
                "name": name,
                "bit_offset": bit_offset,
                "bit_length": field["bit_length"],  # per-element width
                "count": count,
                "value": items,
            }
            if field["type"] in ("uint", "int"):
                entry["raw_value"] = raws
            out_fields.append(entry)
            continue

        value, raw = _read_field(reader, field)
        entry: Dict[str, Any] = {
            "name": name,
            "bit_offset": bit_offset,
            "bit_length": field["bit_length"],
            "value": value,
        }
        if raw is not None and field["type"] in ("uint", "int"):
            entry["raw_value"] = raw
        out_fields.append(entry)
        if raw is not None:
            values[name] = raw

        if checksum_cfg and name == checksum_cfg["field"]:
            if bit_offset % 8 != 0:
                raise ParseError(
                    f"checksum field '{name}' does not start on a byte boundary "
                    f"(bit offset {bit_offset})"
                )
            calculated = compute(checksum_cfg["algorithm"], data[: bit_offset // 8])
            checksum_info = {
                "field": name,
                "algorithm": checksum_cfg["algorithm"],
                "range": f"bytes [0, {bit_offset // 8})",
                "expected": raw,
                "calculated": calculated,
                "expected_hex": f"0x{raw:0{field['bit_length'] // 4}x}",
                "calculated_hex": f"0x{calculated:0{field['bit_length'] // 4}x}",
                "valid": raw == calculated,
            }

    trailing = reader.remaining
    warnings: List[str] = []
    if trailing:
        warnings.append(f"{trailing} trailing bit(s) not covered by the definition")

    return {
        "fields": out_fields,
        "branch_path": branch_path,
        "checksum": checksum_info,
        "message_bits": len(data) * 8,
        "consumed_bits": reader.pos,
        "trailing_bits": trailing,
        "warnings": warnings,
    }
