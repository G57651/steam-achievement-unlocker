"""最小 PE 解析：读取 DLL 的机器类型与导出表（纯标准库，无依赖）。

用途：
1. 在加载 DLL 之前预检其架构（x86/x64），避免位数不匹配导致的 WinError 193；
2. 枚举导出表，验证 steam_api64.dll 是否包含本工具所需的 flat C API 导出。

只实现只读所需的最小解析，任何解析失败都返回 None 而不是抛异常。
"""

from __future__ import annotations

from pathlib import Path

_IMAGE_FILE_MACHINE = {
    0x014C: "x86",
    0x8664: "x64",
    0xAA64: "ARM64",
}

# COFF 文件头 / 可选头 / 节表里的固定偏移（基于 PE/COFF 规范）。
_E_LFANEW_OFFSET = 0x3C
_COFF_SIZE = 20
_OPT_MAGIC_PE32 = 0x10B
_OPT_MAGIC_PE32PLUS = 0x20B
_DATA_DIR_EXPORT_INDEX = 0
_SECTION_ENTRY_SIZE = 40


def _u16(data: bytes, off: int) -> int | None:
    if off < 0 or off + 2 > len(data):
        return None
    return int.from_bytes(data[off : off + 2], "little")


def _u32(data: bytes, off: int) -> int | None:
    if off < 0 or off + 4 > len(data):
        return None
    return int.from_bytes(data[off : off + 4], "little")


def _cstring(data: bytes, off: int) -> str | None:
    if off < 0 or off >= len(data):
        return None
    end = data.find(b"\x00", off)
    if end < 0:
        return None
    return data[off:end].decode("utf-8", "replace")


def _machine(data: bytes) -> int | None:
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    e_lfanew = _u32(data, _E_LFANEW_OFFSET)
    if e_lfanew is None or e_lfanew + 4 + _COFF_SIZE > len(data):
        return None
    if data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        return None
    return _u16(data, e_lfanew + 4)


def machine_of(data: bytes) -> int | None:
    """返回 IMAGE_FILE_MACHINE 常量，解析失败返回 None。"""
    return _machine(data)


def machine_name_of(data: bytes) -> str | None:
    machine = _machine(data)
    return None if machine is None else _IMAGE_FILE_MACHINE.get(machine, f"0x{machine:04X}")


def machine_bits(name: str) -> int | None:
    """x64/ARM64 -> 64，x86 -> 32，未知 -> None。"""
    return {"x64": 64, "ARM64": 64, "x86": 32}.get(name)


def _sections(data: bytes, pe_off: int, num_sections: int, opt_size: int):
    """[(virtual_address, virtual_size, raw_pointer, raw_size), ...]"""
    table = pe_off + 4 + _COFF_SIZE + opt_size
    sections = []
    for i in range(num_sections):
        base = table + i * _SECTION_ENTRY_SIZE
        vsize = _u32(data, base + 8)
        vaddr = _u32(data, base + 12)
        raw_size = _u32(data, base + 16)
        raw_ptr = _u32(data, base + 20)
        if None in (vsize, vaddr, raw_size, raw_ptr):
            return None
        sections.append((vaddr, vsize, raw_ptr, raw_size))
    return sections


def _rva_to_offset(sections, rva: int) -> int | None:
    for vaddr, vsize, raw_ptr, raw_size in sections:
        span = max(vsize, raw_size)
        if vaddr <= rva < vaddr + span:
            return raw_ptr + (rva - vaddr)
    return None


def export_names_of(data: bytes) -> list[str] | None:
    """解析导出表的名称表，解析失败返回 None。"""
    pe_off = _u32(data, _E_LFANEW_OFFSET)
    machine = _machine(data)
    if pe_off is None or machine is None:
        return None
    coff = pe_off + 4
    num_sections = _u16(data, coff + 2) or 0
    opt_size = _u16(data, coff + 16) or 0
    opt = coff + _COFF_SIZE
    magic = _u16(data, opt)
    if magic not in (_OPT_MAGIC_PE32, _OPT_MAGIC_PE32PLUS):
        return None
    data_dir_off = opt + (112 if magic == _OPT_MAGIC_PE32PLUS else 96)
    export_rva = _u32(data, data_dir_off + _DATA_DIR_EXPORT_INDEX * 8)
    if not export_rva:
        return []
    sections = _sections(data, pe_off, num_sections, opt_size)
    if not sections:
        return None
    dir_off = _rva_to_offset(sections, export_rva)
    if dir_off is None:
        return None
    num_names = _u32(data, dir_off + 24)
    names_rva = _u32(data, dir_off + 32)
    if not num_names or not names_rva:
        return []
    names_off = _rva_to_offset(sections, names_rva)
    if names_off is None:
        return None
    names = []
    for i in range(num_names):
        name_rva = _u32(data, names_off + i * 4)
        if name_rva is None:
            return None
        name_off = _rva_to_offset(sections, name_rva)
        name = _cstring(data, name_off) if name_off is not None else None
        if name is None:
            return None
        names.append(name)
    return names


def inspect(path) -> dict | None:
    """便捷接口：返回 {machine, machine_name, bits, exports}，失败返回 None。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    name = machine_name_of(data)
    return {
        "machine_name": name,
        "bits": machine_bits(name) if name else None,
        "exports": export_names_of(data),
    }
