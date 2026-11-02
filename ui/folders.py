"""Foldery wykladow: wirtualne (nie przenosza plikow), zagniezdzone. Rejestr w work/folders.json,
przypisanie wykladu do folderu w jego meta.json (pole 'folder')."""
import re
import threading
import time
import uuid
from pathlib import Path

from io_utils import atomic_write_json, read_json

MAX_DEPTH = 4
MAX_NAME = 60
_UNSET = object()


class FolderError(ValueError):
    pass


def clean_name(name):
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if not name:
        raise FolderError("Nazwa folderu nie może być pusta.")
    if len(name) > MAX_NAME:
        raise FolderError(f"Nazwa folderu jest za długa (maks. {MAX_NAME} znaków).")
    if re.search(r"[\x00-\x1f]", name):
        raise FolderError("Nazwa folderu zawiera niedozwolone znaki.")
    return name


class FolderStore:
    _lock = threading.RLock()

    def __init__(self, work_dir):
        self.path = Path(work_dir) / "folders.json"

    def _load(self):
        return (read_json(self.path, {"folders": {}}) or {"folders": {}})["folders"]

    def _save(self, folders):
        atomic_write_json(str(self.path), {"folders": folders})

    # --- odczyt ---
    def exists(self, folder_id):
        return folder_id in self._load()

    def list(self):
        """Lista folderow z pelna sciezka (nazwy od korzenia), posortowana jak drzewo."""
        folders = self._load()

        def path_of(fid):
            out, seen = [], set()
            while fid and fid in folders and fid not in seen:
                seen.add(fid)
                out.append(folders[fid]["name"])
                fid = folders[fid].get("parent")
            return list(reversed(out))
        items = [{"id": fid, "name": f["name"], "parent": f.get("parent"), "path": path_of(fid), "depth": len(path_of(fid)) - 1,
                  "created": f.get("created")} for fid, f in folders.items()]
        return sorted(items, key=lambda x: [p.casefold() for p in x["path"]])

    def descendants(self, folder_id):
        """Identyfikatory folderu i wszystkich jego podfolderow."""
        folders = self._load()
        out, stack = set(), [folder_id]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            stack += [fid for fid, f in folders.items() if f.get("parent") == cur]
        return out

    def _depth(self, folders, fid):
        d, seen = 0, set()
        while fid and fid in folders and fid not in seen:
            seen.add(fid)
            d += 1
            fid = folders[fid].get("parent")
        return d

    def _height(self, folders, fid):
        kids = [k for k, f in folders.items() if f.get("parent") == fid]
        return 1 + max((self._height(folders, k) for k in kids), default=0)

    def _check_sibling(self, folders, name, parent, own_id=None):
        for fid, f in folders.items():
            if fid != own_id and (f.get("parent") or None) == (parent or None) and f["name"].casefold() == name.casefold():
                raise FolderError("W tym miejscu istnieje już folder o takiej nazwie.")

    # --- zapis ---
    def create(self, name, parent=None):
        name = clean_name(name)
        with self._lock:
            folders = self._load()
            if parent and parent not in folders:
                raise FolderError("Nie ma folderu nadrzędnego.")
            if self._depth(folders, parent) + 1 > MAX_DEPTH:
                raise FolderError(f"Zbyt głębokie zagnieżdżenie (maks. {MAX_DEPTH} poziomy).")
            self._check_sibling(folders, name, parent)
            fid = uuid.uuid4().hex[:8]
            folders[fid] = {"name": name, "parent": parent or None, "created": time.time()}
            self._save(folders)
            return fid

    def update(self, folder_id, name=_UNSET, parent=_UNSET):
        with self._lock:
            folders = self._load()
            if folder_id not in folders:
                raise FolderError("Nie ma takiego folderu.")
            f = folders[folder_id]
            new_name = clean_name(name) if name is not _UNSET else f["name"]
            new_parent = (parent or None) if parent is not _UNSET else f.get("parent")
            if new_parent:
                if new_parent not in folders:
                    raise FolderError("Nie ma folderu docelowego.")
                if new_parent in self.descendants(folder_id):
                    raise FolderError("Nie można przenieść folderu do niego samego ani do jego podfolderu.")
                if self._depth(folders, new_parent) + self._height(folders, folder_id) > MAX_DEPTH:
                    raise FolderError(f"Zbyt głębokie zagnieżdżenie (maks. {MAX_DEPTH} poziomy).")
            self._check_sibling(folders, new_name, new_parent, own_id=folder_id)
            f["name"], f["parent"] = new_name, new_parent
            self._save(folders)

    def delete(self, folder_id):
        """Usuwa folder; podfoldery trafiaja do jego rodzica. Zwraca id rodzica (dokad przeniesc wyklady)."""
        with self._lock:
            folders = self._load()
            if folder_id not in folders:
                raise FolderError("Nie ma takiego folderu.")
            parent = folders[folder_id].get("parent")
            for fid, f in folders.items():
                if f.get("parent") == folder_id:
                    f["parent"] = parent
            del folders[folder_id]
            # po przeniesieniu podfolderow moga wystapic duplikaty nazw: dopisz numer
            seen = {}
            for fid, f in folders.items():
                if (f.get("parent") or None) == (parent or None):
                    key = f["name"].casefold()
                    seen[key] = seen.get(key, 0) + 1
                    if seen[key] > 1:
                        f["name"] = f"{f['name']} ({seen[key]})"
            self._save(folders)
            return parent
