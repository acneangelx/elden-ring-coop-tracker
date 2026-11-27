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

GRACE_ALT_FLAGS: dict[int, tuple[int, ...]] = {
    71100: (71120,),  # Elden Throne (Royal / Ashen)
    71101: (71121,),  # Erdtree Sanctuary
    71102: (71122,),  # East Capital Rampart
    71107: (71124,),  # Queen's Bedchamber
    71109: (71125,),  # Divine Bridge
}

BOSS_ALT_FLAGS: dict[int, tuple[int, ...]] = {
    1052520800: (1252520800, 9123),  # Fire Giant
    1252380800: (9130,),             # Starscourge Radahn
    30200800: (30200810,),           # Stray Mimic Tear
    10000800: (9101,),               # Godrick
    14000800: (9118,),               # Rennala
    16000800: (9122,),               # Rykard
    11000800: (9104,),               # Morgott
    12050800: (9112,),               # Mohg
    15000800: (9109,),               # Malenia
    13000800: (9111,),               # Maliketh
    12040800: (9108,),               # Astel
    12030850: (9115,),               # Lichdragon Fortissax
}


class SaveDataStore:
    """Loads static BST offsets, marker IDs, item/location maps, and quest rules."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.bst: dict[int, int] = {}
        self.grace_ids: list[int] = []
        self.boss_entries: list[tuple[str, int]] = []
        self.boss_ids: list[int] = []
        self.quest_rules: dict[str, Any] = {}
        self.quest_step_counts: dict[str, int] = {}
        self.item_rules: dict[str, dict[str, Any]] = {}
        self.spirit_ash_ids: set[int] = set()
        self.altered_armor_map: dict[int, int] = {}
        self.location_rules: dict[str, dict[str, Any]] = {}
        self.legacy_conv: dict[str, list[dict[str, Any]]] = {}
        self.dungeon_names: dict[str, str] = {}
        self.npc_rules: list[dict[str, Any]] = []
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
            for m in re.finditer(r'id:"g(\d+)",\s*cat:"grace"', mtxt)
        ]
        seen_boss_markers: set[str] = set()
        for m in re.finditer(r'id:"(b(\d+)(?:-\d+)?)",\s*cat:"boss"', mtxt):
            mid, bid = m.group(1), int(m.group(2))
            if mid not in seen_boss_markers:
                seen_boss_markers.add(mid)
                self.boss_entries.append((mid, bid))
        self.boss_ids = sorted({bid for _, bid in self.boss_entries})

        rules_path = self.base_dir / "data" / "quest_rules.json"
        if rules_path.exists():
            self.quest_rules = json.loads(rules_path.read_text(encoding="utf-8"))

        item_rules_path = self.base_dir / "data" / "item_rules.json"
        if item_rules_path.exists():
            ir = json.loads(item_rules_path.read_text(encoding="utf-8"))
            self.item_rules = ir.get("items", {})
            self.spirit_ash_ids = {int(x) for x in ir.get("spirit_ash_ids", ())}
            self.altered_armor_map = {
                int(k): int(v) for k, v in ir.get("altered_armor_map", {}).items()
            }

        loc_rules_path = self.base_dir / "data" / "location_rules.json"
        if loc_rules_path.exists():
            self.location_rules = json.loads(loc_rules_path.read_text(encoding="utf-8"))

        legacy_path = self.base_dir / "data" / "legacy_conv.json"
        if legacy_path.exists():
            lc = json.loads(legacy_path.read_text(encoding="utf-8"))
            for row in lc.get("conv", ()):
                self.legacy_conv.setdefault(row["srcMapId"], []).append(row)
            self.dungeon_names = lc.get("dungeon_names", {})

        npc_rules_path = self.base_dir / "data" / "npc_rules.json"
        if npc_rules_path.exists():
            self.npc_rules = json.loads(npc_rules_path.read_text(encoding="utf-8"))

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
    gaitems: dict[int, tuple[int, int]],
    spirit_ash_ids: set[int],
    altered_armor_map: dict[int, int],
    pools: dict[str, set[int]],
) -> int:
    """Extract owned weapons, armor, talismans, goods/spells/spirits, and ashes of war."""
    goods_out = pools["goods"]
    talismans_out = pools["talisman"]
    weapons_out = pools["weapon"]
    armors_out = pools["armor"]
    ashes_out = pools["ash"]

    def _record_good(raw_id: int) -> None:
        goods_out.add(raw_id)
        lvl = raw_id % 100
        if lvl > 0 and (raw_id - lvl) in spirit_ash_ids:
            goods_out.add(raw_id - lvl)

    common_cnt = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    for i in range(min(common_cnt, common_cap)):
        h, qty, _ = struct.unpack_from("<III", data, pos + i * 12)
        if h == 0 or qty == 0:
            continue
        top = h & 0xF0000000
        if top == 0x80000000:
            gi = gaitems.get(h)
            if gi:
                iid = gi[0] & 0x0FFFFFFF
                if iid:
                    weapons_out.add((iid // 10000) * 10000)
                    weapons_out.add(iid - (iid % 100))
                if gi[1] and gi[1] in gaitems:
                    ash_raw = gaitems[gi[1]][0] & 0x0FFFFFFF
                    if ash_raw:
                        ashes_out.add(ash_raw)
        elif top == 0x90000000:
            gi = gaitems.get(h)
            if gi:
                iid = gi[0] & 0x0FFFFFFF
                if iid:
                    armors_out.add(iid)
                    if iid in altered_armor_map:
                        armors_out.add(altered_armor_map[iid])
        elif top == 0xA0000000:
            talismans_out.add(h & 0x0FFFFFFF)
        elif top == 0xB0000000:
            _record_good(h & 0x0FFFFFFF)
        elif top == 0xC0000000:
            gi = gaitems.get(h)
            if gi:
                iid = gi[0] & 0x0FFFFFFF
                if iid:
                    ashes_out.add(iid)
    pos += common_cap * 12

    key_cnt = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    for i in range(min(key_cnt, key_cap)):
        h, qty, _ = struct.unpack_from("<III", data, pos + i * 12)
        if h == 0 or qty == 0:
            continue
        if (h & 0xF0000000) == 0xB0000000:
            _record_good(h & 0x0FFFFFFF)
    pos += key_cap * 12
    pos += 8  # equip_index_counter, acquisition_index_counter
    return pos


def _project_player_pos(
    store: SaveDataStore, map_bytes: bytes, x: float, y: float, z: float
) -> dict[str, Any]:
    """Project overworld, DLC, and underground/legacy dungeon coordinates onto master map pixels (px, py)."""
    b0, b1, b2, b3 = map_bytes[0], map_bytes[1], map_bytes[2], map_bytes[3]
    block = f"m{b3:02d}_{b2:02d}_{b1:02d}"
    map_code = f"{block}_{b0:02d}"
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
            "area_name": "",
            "in_dungeon": False,
        }

    conv_list = store.legacy_conv.get(block)
    if conv_list:
        best = min(conv_list, key=lambda c: (x - c["srcX"]) ** 2 + (z - c["srcZ"]) ** 2)
        return {
            "master": best["master"],
            "px": round(x + best["addX"] + OFFSET_X, 1),
            "py": round(OFFSET_Y - (z + best["addZ"]), 1),
            "map_code": map_code,
            "area_name": store.dungeon_names.get(block, ""),
            "in_dungeon": False,
        }

    master_guess = "M01" if b3 == 12 else ("M10" if 20 <= b3 <= 28 or b3 in (40, 41, 42, 43) else "M00")
    return {
        "master": master_guess,
        "px": None,
        "py": None,
        "map_code": map_code,
        "dungeon_name": store.dungeon_names.get(block, map_code),
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


def _any_flag(event_flags: bytes, bst: dict[int, int], fids: Any) -> bool:
    return any(get_event_flag(event_flags, bst, fid) for fid in fids)


def _read_npc_status(event_flags: bytes, bst: dict[int, int], npc_base: int | None) -> str:
    """Resolve persistent NPC status (alive / hostile / dead / unknown)."""
    if npc_base is None:
        return "unknown"
    if get_event_flag(event_flags, bst, npc_base + 3):
        return "dead"
    if get_event_flag(event_flags, bst, npc_base + 1) or get_event_flag(event_flags, bst, npc_base + 2):
        return "hostile"
    if get_event_flag(event_flags, bst, npc_base):
        return "alive"
    return "unknown"


def _evaluate_npcs(
    store: SaveDataStore,
    event_flags: bytes,
    unlocked_graces: set[int],
    defeated_bosses: set[int],
    quests: dict[str, Any],
    pools: dict[str, set[int]],
    world_state: dict[str, bool],
) -> dict[str, Any]:
    """Determine met NPCs and their single current active map pin."""
    active: dict[str, dict[str, Any]] = {}
    met_count = 0
    goods = pools["goods"]

    for rule in store.npc_rules:
        qid = rule.get("quest_id")
        qstate = quests.get(qid, {}) if qid else {}
        q_direct = {int(k) for k in qstate.get("direct", {})}
        q_all = q_direct | {int(k) for k in qstate.get("inferred", {})}

        met_cfg = rule.get("met", {})
        is_met = (
            any(sn in q_direct for sn in met_cfg.get("quest_steps", ()))
            or _any_flag(event_flags, store.bst, met_cfg.get("flags", ()))
            or any(gid in unlocked_graces for gid in met_cfg.get("graces", ()))
            or any(bid in defeated_bosses for bid in met_cfg.get("bosses", ()))
            or any(iid in goods for iid in met_cfg.get("goods", ()))
        )
        if not is_met:
            continue

        met_count += 1
        status = _read_npc_status(event_flags, store.bst, rule.get("npc_flag_base"))
        stages = rule.get("stages", [])
        chosen = stages[-1] if stages else None

        for st in stages:
            has_cond = any(k in st for k in ("quest_any_step", "flags", "bosses", "world_state"))
            if not has_cond:
                chosen = st
                break
            matched = (
                any(sn in q_all for sn in st.get("quest_any_step", ()))
                or _any_flag(event_flags, store.bst, st.get("flags", ()))
                or any(bid in defeated_bosses or get_event_flag(event_flags, store.bst, bid) for bid in st.get("bosses", ()))
                or (bool(st.get("world_state")) and bool(world_state.get(st["world_state"])))
            )
            if matched:
                chosen = st
                break

        if chosen:
            active[chosen["marker_id"]] = {
                "id": rule["id"],
                "name": rule["name"],
                "stage": chosen["stage"],
                "status": status,
                "quest_id": qid or "",
            }

    return {
        "active": active,
        "met_count": met_count,
        "total_npcs": len(store.npc_rules),
    }


def _evaluate_quests(
    store: SaveDataStore,
    event_flags: bytes,
    unlocked_graces: set[int],
    defeated_bosses: set[int],
    pools: dict[str, set[int]],
) -> dict[str, Any]:
    """Compute auto-detected progress and NPC status for all questlines."""
    goods = pools["goods"]
    talismans = pools["talisman"]
    weapons = pools["weapon"]
    armors = pools["armor"]
    ashes = pools["ash"]

    out: dict[str, Any] = {}
    for qid, total_steps in store.quest_step_counts.items():
        rule = store.quest_rules.get(qid, {})
        npc_status = _read_npc_status(event_flags, store.bst, rule.get("npc_flag_base"))

        step_rules = rule.get("steps", {})
        direct_done: dict[int, str] = {}
        optional_steps: set[int] = set()
        no_infer_steps: set[int] = set()

        for s_str, sr in step_rules.items():
            sn = int(s_str)
            if sr.get("optional"):
                optional_steps.add(sn)
            if sr.get("no_infer"):
                no_infer_steps.add(sn)

            # Negative guards (e.g., item handed over so no longer in inventory, or flag not set)
            not_flags = sr.get("not_flags", [])
            if not_flags and _any_flag(event_flags, store.bst, not_flags):
                continue
            not_goods = sr.get("not_goods", [])
            if not_goods and any(gid in goods for gid in not_goods):
                continue

            reasons: list[str] = []
            all_flags = sr.get("all_flags", [])
            if all_flags and all(get_event_flag(event_flags, store.bst, fid) for fid in all_flags):
                reasons.append("флаг события")
            if not reasons:
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
            if not reasons:
                for aid in sr.get("armors", []):
                    if aid in armors:
                        reasons.append("броня получена")
                        break
            if not reasons:
                for ash_id in sr.get("ashes", []):
                    if ash_id in ashes:
                        reasons.append("пепел войны получен")
                        break
            if reasons:
                label = sr.get("label", "")
                direct_done[sn] = f"{reasons[0]} ({label})" if label else reasons[0]

        inferred_done: dict[int, str] = {}
        if rule.get("linear_infer") and direct_done:
            infer_candidates = [sn for sn in direct_done if sn not in no_infer_steps]
            if infer_candidates:
                max_done = max(infer_candidates)
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


def read_slot_checksum(path: str, slot_idx: int = 0) -> str:
    """Quickly read the 16-byte slot MD5 header at SLOT_BASE + slot * SLOT_STRIDE."""
    try:
        offset = SLOT_BASE + max(0, min(9, slot_idx)) * SLOT_STRIDE
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(16).hex()
    except OSError:
        return ""


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
    checksum = data[base : base + 16].hex()
    pos = base + 0x10
    version = struct.unpack_from("<I", data, pos)[0]
    pos += 4 + 4 + 0x8 + 0x10

    gaitem_count = 0x13FE if version <= 81 else 0x1400
    gaitems: dict[int, tuple[int, int]] = {}
    for _ in range(gaitem_count):
        handle, item_id = struct.unpack_from("<II", data, pos)
        pos += 8
        top = handle & 0xF0000000
        gem_handle = 0
        if not (handle == 0 or top == 0xC0000000):
            pos += 8
        if not (handle == 0 or top != 0x80000000):
            gem_handle = struct.unpack_from("<I", data, pos)[0]
            pos += 5
        if handle != 0:
            gaitems[handle] = (item_id, gem_handle)

    pg_start = pos
    level = struct.unpack_from("<I", data, pg_start + 0x60)[0]
    runes = struct.unpack_from("<I", data, pg_start + 0x64)[0]
    name = (
        data[pg_start + 0x94 : pg_start + 0x94 + 32]
        .decode("utf-16-le", errors="ignore")
        .split("\x00")[0]
        .strip()
    )
    scadutree_blessing = data[pg_start + 0xF4]
    revered_blessing = data[pg_start + 0xF5]
    crimson_flasks = data[pg_start + 0xF9]
    cerulean_flasks = data[pg_start + 0xFA]
    pos = pg_start + 0x1B0
    pos += 0xD * 16
    pos += (22 + 7 + 22 + 22) * 4

    pools: dict[str, set[int]] = {
        "weapon": set(),
        "armor": set(),
        "talisman": set(),
        "goods": set(),
        "ash": set(),
    }

    pos = _decode_inventory_block(
        data, pos, 0xA80, 0x180, gaitems, store.spirit_ash_ids, store.altered_armor_map, pools
    )
    pos += 14 * 8 + 4
    pos += 10 * 8 + 4 + 6 * 8 + 8
    pos += 6 * 4
    proj_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4 + proj_count * 8
    pos += 39 * 4 + 3 * 4
    pos += 303  # FaceData (in_profile_summary=false)
    pos = _decode_inventory_block(
        data, pos, 0x780, 0x80, gaitems, store.spirit_ash_ids, store.altered_armor_map, pools
    )
    # Include all 0xC0000000 ash entries in gaitem_map (e.g., equipped/slotted Ashes of War)
    for h, (giid, _) in gaitems.items():
        if (h & 0xF0000000) == 0xC0000000:
            ash_raw = giid & 0x0FFFFFFF
            if ash_raw:
                pools["ash"].add(ash_raw)

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
        player_pos = _project_player_pos(store, map_bytes, px, py, pz)
    except Exception:
        player_pos = None

    def _is_grace_unlocked(gid: int) -> bool:
        if get_event_flag(event_flags, store.bst, gid):
            return True
        return any(get_event_flag(event_flags, store.bst, alt) for alt in GRACE_ALT_FLAGS.get(gid, ()))

    def _is_boss_defeated(bid: int) -> bool:
        if get_event_flag(event_flags, store.bst, bid):
            return True
        return any(get_event_flag(event_flags, store.bst, alt) for alt in BOSS_ALT_FLAGS.get(bid, ()))

    unlocked_graces = {gid for gid in store.grace_ids if _is_grace_unlocked(gid)}
    defeated_bosses = {bid for bid in store.boss_ids if _is_boss_defeated(bid)}
    defeated_boss_markers = sorted(mid for mid, bid in store.boss_entries if bid in defeated_bosses)

    collected_items: list[str] = []
    for mid, irule in store.item_rules.items():
        pool = pools.get(irule.get("pool", ""), set())
        all_fl = irule.get("all_flags", ())
        if any(iid in pool for iid in irule.get("ids", ())):
            collected_items.append(mid)
        elif all_fl and all(get_event_flag(event_flags, store.bst, fid) for fid in all_fl):
            collected_items.append(mid)
        elif any(get_event_flag(event_flags, store.bst, fid) for fid in irule.get("flags", ())):
            collected_items.append(mid)
    collected_items.sort()
    collected_items_set = set(collected_items)

    found_locations: list[str] = []
    for lid, lrule in store.location_rules.items():
        bfs = lrule.get("boss_flags", ())
        fls = lrule.get("flags", ())
        if bfs:
            done = any(bid in defeated_bosses or _is_boss_defeated(bid) for bid in bfs) or any(
                get_event_flag(event_flags, store.bst, fid) for fid in fls
            )
        else:
            gfs = lrule.get("grace_flags", ())
            its = lrule.get("items", ())
            done = (
                any(gf in unlocked_graces or _is_grace_unlocked(gf) for gf in gfs)
                or any(get_event_flag(event_flags, store.bst, fid) for fid in fls)
                or any(mid in collected_items_set for mid in its)
            )
        if done:
            found_locations.append(lid)
    found_locations.sort()

    quests = _evaluate_quests(
        store, event_flags, unlocked_graces, defeated_bosses, pools
    )

    world_state = {
        "radahn_defeated": get_event_flag(event_flags, store.bst, 1252380800)
        or get_event_flag(event_flags, store.bst, 9130)
        or get_event_flag(event_flags, store.bst, 310),
        "erdtree_burning": get_event_flag(event_flags, store.bst, 300)
        or get_event_flag(event_flags, store.bst, 118),
        "study_hall_inverted": get_event_flag(event_flags, store.bst, 320),
        "frenzied_flame": get_event_flag(event_flags, store.bst, 108),
    }

    npcs = _evaluate_npcs(
        store, event_flags, unlocked_graces, defeated_bosses, quests, pools, world_state
    )

    return {
        "file": path,
        "mtime_ns": str(mtime_ns),
        "size": size,
        "checksum": checksum,
        "slots": slots,
        "slot": slot_idx,
        "character": {
            "name": name,
            "level": level,
            "runes": runes,
            "deaths": total_deaths,
            "scadutree_blessing": scadutree_blessing if scadutree_blessing <= 20 else 0,
            "revered_blessing": revered_blessing if revered_blessing <= 10 else 0,
            "crimson_flasks": crimson_flasks,
            "cerulean_flasks": cerulean_flasks,
        },
        "player_pos": player_pos,
        "world_state": world_state,
        "graces": sorted(f"g{gid}" for gid in unlocked_graces),
        "bosses": defeated_boss_markers,
        "items": collected_items,
        "locations": found_locations,
        "npcs": npcs,
        "counts": {
            "graces": len(unlocked_graces),
            "graces_total": len(store.grace_ids),
            "bosses": len(defeated_boss_markers),
            "bosses_total": len(store.boss_entries),
            "items": len(collected_items),
            "items_total": len(store.item_rules),
            "locations": len(found_locations),
            "locations_total": len(store.location_rules),
            "npcs_met": npcs["met_count"],
            "npcs_total": npcs["total_npcs"],
        },
        "quests": quests,
    }
