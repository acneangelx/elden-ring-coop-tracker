"""Read-only Elden Ring (.co2 / .sl2) save file parser for Seamless Co-op live tracking."""

from __future__ import annotations

import json
import os
import re
import struct
import time
import zipfile
from pathlib import Path
from typing import Any

FLAG_DIVISOR = 1000
BLOCK_SIZE = 125
EVENT_FLAGS_SIZE = 0x1BF99F
SLOT_STRIDE = 0x280010
SLOT_BASE = 0x300
OFFSET_X = -7168.0
OFFSET_Y = 16640.0


class SaveDataStore:
    """Loads static BST offsets, marker IDs, and quest auto-detection rules."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.bst: dict[int, int] = {}
        self.grace_ids: list[int] = []
        self.boss_ids: list[int] = []
        self.quest_rules: dict[str, Any] = {}
        self.quest_step_counts: dict[str, int] = {}
        self._load()

    def _read_web_text(self, rel_path: str) -> str | None:
        disk_path = self.base_dir / "web" / rel_path
        if disk_path.is_file():
            return disk_path.read_text(encoding="utf-8")
        zip_path = self.base_dir / "web.zip"
        if zip_path.is_file():
            with zipfile.ZipFile(zip_path, "r") as zf:
                if rel_path in zf.namelist():
                    return zf.read(rel_path).decode("utf-8")
        return None

    def _load(self) -> None:
        bst_path = self.base_dir / "data" / "eventflag_bst.txt"
        with bst_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "," in line:
                    k, v = line.split(",", 1)
                    self.bst[int(k)] = int(v)

        mtxt = self._read_web_text("markers.js") or ""
        self.grace_ids = [
            int(m.group(1))
            for m in re.finditer(r'id:"g(\d+)", cat:"grace"', mtxt)
        ]
        # Exact numeric boss IDs only (skip -1/-2 duplicates)
        self.boss_ids = sorted({
            int(m.group(1))
            for m in re.finditer(r'id:"b(\d+)", cat:"boss"', mtxt)
        })

        rules_path = self.base_dir / "data" / "quest_rules.json"
        if rules_path.exists():
            self.quest_rules = json.loads(rules_path.read_text(encoding="utf-8"))

        qtxt = self._read_web_text("quests.js")
        if qtxt:
            for qm in re.finditer(r'\{ id:"([^"]+)"', qtxt):
                qid = qm.group(1)
                start = qm.start()
                nxt = qtxt.find("\n  { id:", start + 1)
                block = qtxt[start : nxt if nxt != -1 else len(qtxt)]
                steps = [int(s.group(1)) for s in re.finditer(r'\{ n:(\d+),', block)]
                self.quest_step_counts[qid] = max(steps) if steps else 0


def discover_save_files() -> list[dict[str, Any]]:
    """Find .co2 and .sl2 save files in %APPDATA%/EldenRing."""
    root = Path(os.path.expandvars(r"%APPDATA%\EldenRing"))
    if not root.exists():
        return []

    found: list[dict[str, Any]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in (".co2", ".sl2"):
            continue
        is_backup = "backup" in str(p.parent).lower() or "bak" in p.name.lower()
        try:
            st = p.stat()
            found.append({
                "path": str(p),
                "name": f"{p.parent.name}/{p.name}",
                "ext": ext,
                "is_coop": ext == ".co2",
                "is_backup": is_backup,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        except OSError:
            continue

    found.sort(key=lambda x: (x["is_backup"], not x["is_coop"], -x["mtime"]))
    return found


def read_save_bytes(path: str, retries: int = 4) -> tuple[bytes, int, int]:
    """Read save bytes safely with retry against brief game-write locks."""
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            st = os.stat(path)
            with open(path, "rb") as f:
                data = f.read()
            if len(data) < 0x1900000 or data[:4] != b"BND4":
                raise ValueError("Файл не является корректным сохранением Elden Ring (BND4).")
            return data, st.st_mtime_ns, st.st_size
        except (OSError, ValueError) as exc:
            last_err = exc
            time.sleep(0.06)
    raise RuntimeError(f"Не удалось прочитать сохранение {path}: {last_err}")


def get_event_flag(event_flags: bytes, bst: dict[int, int], event_id: int) -> bool:
    """Check whether a single persistent event flag bit is set."""
    block = event_id // FLAG_DIVISOR
    index = event_id - block * FLAG_DIVISOR
    res = bst.get(block)
    if res is None:
        return False
    offset = res * BLOCK_SIZE
    byte_idx = index // 8
    pos = offset + byte_idx
    if pos >= len(event_flags):
        return False
    bit_idx = 7 - (index - byte_idx * 8)
    return ((event_flags[pos] >> bit_idx) & 1) == 1


def _decode_inventory_block(
    data: bytes,
    pos: int,
    common_cap: int,
    key_cap: int,
    gaitems: dict[int, int],
    goods_out: set[int],
    talismans_out: set[int],
    weapons_out: set[int],
) -> int:
    """Extract owned goods, talismans, and base weapon IDs from an inventory section."""
    common_cnt = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    for i in range(min(common_cnt, common_cap)):
        h, qty, _ = struct.unpack_from("<III", data, pos + i * 12)
        if h == 0 or qty == 0:
            continue
        top = h & 0xF0000000
        if top == 0x80000000:
            iid = gaitems.get(h, 0) & 0x0FFFFFFF
            if iid:
                weapons_out.add((iid // 10000) * 10000)
                weapons_out.add((iid // 100) * 100)
        elif top == 0xA0000000:
            talismans_out.add(h & 0x0FFFFFFF)
        elif top == 0xB0000000:
            goods_out.add(h & 0x0FFFFFFF)
    pos += common_cap * 12

    key_cnt = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    for i in range(min(key_cnt, key_cap)):
        h, qty, _ = struct.unpack_from("<III", data, pos + i * 12)
        if h == 0 or qty == 0:
            continue
        if (h & 0xF0000000) == 0xB0000000:
            goods_out.add(h & 0x0FFFFFFF)
    pos += key_cap * 12
    pos += 8  # equip_index_counter, acquisition_index_counter
    return pos


def _project_player_pos(map_bytes: bytes, x: float, y: float, z: float) -> dict[str, Any]:
    """Project overworld/DLC coordinates onto master map pixels (px, py)."""
    b0, b1, b2, b3 = map_bytes[0], map_bytes[1], map_bytes[2], map_bytes[3]
    map_code = f"m{b3:02d}_{b2:02d}_{b1:02d}_{b0:02d}"
    if b3 in (60, 61) and b0 <= 2:
        size = 256.0 * (2 ** b0)
        wx = b2 * size + size / 2.0 + x
        wz = b1 * size + size / 2.0 + z
        master = "M00" if b3 == 60 else "M10"
        return {
            "master": master,
            "px": round(wx + OFFSET_X, 1),
            "py": round(OFFSET_Y - wz, 1),
            "map_code": map_code,
            "in_dungeon": False,
        }
    master_guess = "M01" if b3 == 12 else ("M10" if 20 <= b3 <= 28 or b3 in (40, 41, 42, 43) else "M00")
    return {
        "master": master_guess,
        "px": None,
        "py": None,
        "map_code": map_code,
        "in_dungeon": True,
    }


def list_slots(data: bytes) -> list[dict[str, Any]]:
    """Read basic metadata (name, level) for slots 0..9 from ProfileSummary + UserDataX."""
    slots: list[dict[str, Any]] = []
    for i in range(10):
        # ProfileSummary slot header is quick to inspect
        off = 0x1901D0E + i * 0x24C
        if off + 32 > len(data):
            continue
        name = data[off : off + 32].decode("utf-16-le", errors="ignore").split("\x00")[0].strip()
        if not name:
            continue
        # Read level from UserDataX
        base = SLOT_BASE + i * SLOT_STRIDE
        pos = base + 0x10
        version = struct.unpack_from("<I", data, pos)[0]
        pos += 4 + 4 + 0x8 + 0x10
        gaitem_count = 0x13FE if version <= 81 else 0x1400
        for _ in range(gaitem_count):
            handle, _ = struct.unpack_from("<II", data, pos)
            pos += 8
            top = handle & 0xF0000000
            if not (handle == 0 or top == 0xC0000000):
                pos += 8
            if not (handle == 0 or top != 0x80000000):
                pos += 5
        level = struct.unpack_from("<I", data, pos + 0x60)[0]
        slots.append({"slot": i, "name": name, "level": level})
    return slots


def _evaluate_quests(
    store: SaveDataStore,
    event_flags: bytes,
    unlocked_graces: set[int],
    defeated_bosses: set[int],
    goods: set[int],
    talismans: set[int],
    weapons: set[int],
) -> dict[str, Any]:
    """Compute auto-detected progress and NPC status for all questlines."""
    out: dict[str, Any] = {}
    for qid, total_steps in store.quest_step_counts.items():
        rule = store.quest_rules.get(qid, {})
        npc_base = rule.get("npc_flag_base")
        npc_status = "unknown"
        if npc_base is not None:
            if get_event_flag(event_flags, store.bst, npc_base + 3):
                npc_status = "dead"
            elif get_event_flag(event_flags, store.bst, npc_base + 1) or get_event_flag(
                event_flags, store.bst, npc_base + 2
            ):
                npc_status = "hostile"
            elif get_event_flag(event_flags, store.bst, npc_base):
                npc_status = "alive"

        step_rules = rule.get("steps", {})
        direct_done: dict[int, str] = {}
        optional_steps: set[int] = set()

        for s_str, sr in step_rules.items():
            sn = int(s_str)
            if sr.get("optional"):
                optional_steps.add(sn)
            reasons: list[str] = []
            for fid in sr.get("flags", []):
                if get_event_flag(event_flags, store.bst, fid):
                    reasons.append("флаг события")
                    break
            if not reasons:
                for bid in sr.get("bosses", []):
                    if bid in defeated_bosses or get_event_flag(event_flags, store.bst, bid):
                        reasons.append("босс повержен")
                        break
            if not reasons:
                for gid in sr.get("graces", []):
                    if gid in unlocked_graces or get_event_flag(event_flags, store.bst, gid):
                        reasons.append("благодать открыта")
                        break
            if not reasons:
                for iid in sr.get("goods", []):
                    if iid in goods:
                        reasons.append("предмет в инвентаре")
                        break
            if not reasons:
                for tid in sr.get("talismans", []):
                    if tid in talismans:
                        reasons.append("талисман получен")
                        break
            if not reasons:
                for wid in sr.get("weapons", []):
                    if wid in weapons:
                        reasons.append("оружие получено")
                        break
            if reasons:
                label = sr.get("label", "")
                direct_done[sn] = f"{reasons[0]} ({label})" if label else reasons[0]

        inferred_done: dict[int, str] = {}
        if rule.get("linear_infer") and direct_done:
            max_done = max(direct_done.keys())
            for sn in range(1, max_done):
                if sn not in direct_done and sn not in optional_steps:
                    inferred_done[sn] = f"пройдено по цепочке (выполнен шаг {max_done})"

        all_done = set(direct_done.keys()) | set(inferred_done.keys())
        current_step = 0
        for sn in range(1, total_steps + 1):
            if sn not in all_done:
                current_step = sn
                break

        out[qid] = {
            "npc_status": npc_status,
            "total": total_steps,
            "direct": {str(k): v for k, v in sorted(direct_done.items())},
            "inferred": {str(k): v for k, v in sorted(inferred_done.items())},
            "current_step": current_step,
        }
    return out


def parse_save_state(store: SaveDataStore, path: str, slot_idx: int = 0) -> dict[str, Any]:
    """Parse a character slot from a .co2/.sl2 save file into a JSON-serializable state."""
    data, mtime_ns, size = read_save_bytes(path)
    slots = list_slots(data)
    if not slots:
        raise ValueError("В файле сохранения не найдено активных персонажей.")

    valid_indices = [s["slot"] for s in slots]
    if slot_idx not in valid_indices:
        slot_idx = valid_indices[0]

    base = SLOT_BASE + slot_idx * SLOT_STRIDE
    pos = base + 0x10
    version = struct.unpack_from("<I", data, pos)[0]
    pos += 4 + 4 + 0x8 + 0x10

    gaitem_count = 0x13FE if version <= 81 else 0x1400
    gaitems: dict[int, int] = {}
    for _ in range(gaitem_count):
        handle, item_id = struct.unpack_from("<II", data, pos)
        pos += 8
        top = handle & 0xF0000000
        if not (handle == 0 or top == 0xC0000000):
            pos += 8
        if not (handle == 0 or top != 0x80000000):
            pos += 5
        if handle != 0:
            gaitems[handle] = item_id

    pg_start = pos
    level = struct.unpack_from("<I", data, pg_start + 0x60)[0]
    runes = struct.unpack_from("<I", data, pg_start + 0x64)[0]
    name = (
        data[pg_start + 0x94 : pg_start + 0x94 + 32]
        .decode("utf-16-le", errors="ignore")
        .split("\x00")[0]
        .strip()
    )
    pos = pg_start + 0x1B0
    pos += 0xD * 16
    pos += (22 + 7 + 22 + 22) * 4

    goods: set[int] = set()
    talismans: set[int] = set()
    weapons: set[int] = set()

    pos = _decode_inventory_block(data, pos, 0xA80, 0x180, gaitems, goods, talismans, weapons)
    pos += 14 * 8 + 4
    pos += 10 * 8 + 4 + 6 * 8 + 8
    pos += 6 * 4
    proj_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4 + proj_count * 8
    pos += 39 * 4 + 3 * 4
    pos += 303  # FaceData (in_profile_summary=false)
    pos = _decode_inventory_block(data, pos, 0x780, 0x80, gaitems, goods, talismans, weapons)
    pos += 0x40 * 4
    reg_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4 + reg_count * 4
    pos += 40 + 1 + 68 + 8
    menu_size = struct.unpack_from("<I", data, pos + 4)[0]
    pos += 8 + menu_size + 52 + 8 + 7000 * 16
    tut_size = struct.unpack_from("<I", data, pos + 4)[0]
    pos += 8 + tut_size + 3
    total_deaths = struct.unpack_from("<I", data, pos)[0]
    pos += 26

    event_flags = data[pos : pos + EVENT_FLAGS_SIZE]
    terminator = data[pos + EVENT_FLAGS_SIZE]
    if terminator != 0:
        raise ValueError("Смещение event_flags не совпало (возможно, файл перезаписывается игрой).")

    pos_after_ef = pos + EVENT_FLAGS_SIZE + 1
    player_pos = None
    try:
        for _ in range(5):  # FieldArea, WorldArea, WorldGeomMan1, WorldGeomMan2, RendMan
            sz = struct.unpack_from("<i", data, pos_after_ef)[0]
            pos_after_ef += 4 + max(0, sz)
        px, py, pz = struct.unpack_from("<fff", data, pos_after_ef)
        map_bytes = data[pos_after_ef + 12 : pos_after_ef + 16]
        player_pos = _project_player_pos(map_bytes, px, py, pz)
    except Exception:
        player_pos = None

    unlocked_graces = {
        gid for gid in store.grace_ids if get_event_flag(event_flags, store.bst, gid)
    }
    defeated_bosses = {
        bid for bid in store.boss_ids if get_event_flag(event_flags, store.bst, bid)
    }

    quests = _evaluate_quests(
        store, event_flags, unlocked_graces, defeated_bosses, goods, talismans, weapons
    )

    world_state = {
        "radahn_defeated": get_event_flag(event_flags, store.bst, 9130)
        or get_event_flag(event_flags, store.bst, 310),
        "erdtree_burning": get_event_flag(event_flags, store.bst, 300)
        or get_event_flag(event_flags, store.bst, 118),
        "study_hall_inverted": get_event_flag(event_flags, store.bst, 320),
        "frenzied_flame": get_event_flag(event_flags, store.bst, 108),
    }

    return {
        "file": path,
        "mtime_ns": str(mtime_ns),
        "size": size,
        "slots": slots,
        "slot": slot_idx,
        "character": {
            "name": name,
            "level": level,
            "runes": runes,
            "deaths": total_deaths,
        },
        "player_pos": player_pos,
        "world_state": world_state,
        "graces": sorted(f"g{gid}" for gid in unlocked_graces),
        "bosses": sorted(f"b{bid}" for bid in defeated_bosses),
        "counts": {
            "graces": len(unlocked_graces),
            "graces_total": len(store.grace_ids),
            "bosses": len(defeated_bosses),
            "bosses_total": len(store.boss_ids),
        },
        "quests": quests,
    }
