"""Application state: database-backed repository functions shared by the API routers."""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

from . import snapshot as snap
from .config import Settings
from .core import scales as scales_mod
from .core import stats as stats_mod
from .core.cache import ArrayCache
from .core.engine import Engine, GateNode, SampleData
from .core.scales import Scale
from .db import Database, utcnow


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


class AppState:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.ensure_dirs()
        self.db = Database(settings.db_path)
        self.schema_version = self.db.migrate(settings.extra.get("journal_mode", "WAL"))
        self.cache = ArrayCache(settings.cache_mb * 1024 * 1024)
        self.engine = Engine(self.cache, settings.masks_dir)
        self._scales: dict[int, Scale] = {}
        self._scales_lock = threading.Lock()
        self.dispatcher = None  # set by app factory

    # ---- users / ownership ---------------------------------------------------
    def ensure_user(self, user_id: str) -> None:
        if self.db.one("SELECT 1 FROM users WHERE id=?", (user_id,)) is None:
            self.db.execute("INSERT OR IGNORE INTO users(id, created_at) VALUES (?,?)", (user_id, utcnow()))

    def experiment(self, exp_id: int, user_id: str):
        row = self.db.one("SELECT * FROM experiments WHERE id=? AND owner_id=?", (exp_id, user_id))
        if row is None:
            raise NotFound(f"experiment {exp_id} not found")
        return row

    def sample(self, sample_id: int, user_id: str):
        row = self.db.one(
            "SELECT s.* FROM samples s JOIN experiments e ON e.id = s.experiment_id "
            "WHERE s.id=? AND e.owner_id=?",
            (sample_id, user_id),
        )
        if row is None:
            raise NotFound(f"sample {sample_id} not found")
        return row

    def gate(self, gate_id: int, user_id: str):
        row = self.db.one(
            "SELECT g.* FROM gates g JOIN experiments e ON e.id = g.experiment_id "
            "WHERE g.id=? AND e.owner_id=?",
            (gate_id, user_id),
        )
        if row is None:
            raise NotFound(f"gate {gate_id} not found")
        return row

    def bump_revision(self, exp_id: int) -> int:
        self.db.execute("UPDATE experiments SET revision = revision + 1 WHERE id=?", (exp_id,))
        return self.db.one("SELECT revision FROM experiments WHERE id=?", (exp_id,))["revision"]

    # ---- scales --------------------------------------------------------------
    def scale_id(self, scale: Scale) -> int:
        row = self.db.one("SELECT id FROM scales WHERE hash=?", (scale.hash,))
        if row is None:
            self.db.execute(
                "INSERT OR IGNORE INTO scales(hash, kind, params) VALUES (?,?,?)",
                (scale.hash, scale.kind, json.dumps(scale.p, sort_keys=True)),
            )
            row = self.db.one("SELECT id FROM scales WHERE hash=?", (scale.hash,))
        with self._scales_lock:
            self._scales[row["id"]] = scale
        return row["id"]

    def scale(self, scale_id: int) -> Scale:
        with self._scales_lock:
            cached = self._scales.get(scale_id)
        if cached is not None:
            return cached
        row = self.db.one("SELECT kind, params FROM scales WHERE id=?", (scale_id,))
        if row is None:
            raise NotFound(f"scale {scale_id} not found")
        scale = scales_mod.make_scale(row["kind"], json.loads(row["params"]))
        with self._scales_lock:
            self._scales[scale_id] = scale
        return scale

    def channel_meta(self, exp_id: int, pnn: str):
        """Representative channel row for a PnN within an experiment (first sample that has it)."""
        return self.db.one(
            "SELECT c.*, (SELECT MAX(c2.vmax) FROM channels c2 JOIN samples s2 ON s2.id=c2.sample_id "
            "             WHERE s2.experiment_id=? AND c2.pnn=?) AS exp_vmax, "
            "            (SELECT MIN(c2.vmin) FROM channels c2 JOIN samples s2 ON s2.id=c2.sample_id "
            "             WHERE s2.experiment_id=? AND c2.pnn=?) AS exp_vmin "
            "FROM channels c JOIN samples s ON s.id=c.sample_id "
            "WHERE s.experiment_id=? AND c.pnn=? ORDER BY s.id LIMIT 1",
            (exp_id, pnn, exp_id, pnn, exp_id, pnn),
        )

    def channel_scale(self, exp_id: int, pnn: str) -> tuple[int, Scale]:
        """Current display scale of a channel; created from defaults on first use."""
        row = self.db.one("SELECT scale_id FROM channel_scales WHERE experiment_id=? AND pnn=?", (exp_id, pnn))
        if row is not None:
            return row["scale_id"], self.scale(row["scale_id"])
        meta = self.channel_meta(exp_id, pnn)
        if meta is None:
            raise NotFound(f"no sample in this experiment has channel '{pnn}'")
        scale = scales_mod.default_scale(meta["kind"], meta["pnr"], meta["exp_vmax"])
        sid = self.scale_id(scale)
        self.db.execute(
            "INSERT OR IGNORE INTO channel_scales(experiment_id, pnn, scale_id) VALUES (?,?,?)", (exp_id, pnn, sid)
        )
        return sid, scale

    def set_channel_scale(self, exp_id: int, pnn: str, scale: Scale) -> int:
        sid = self.scale_id(scale)
        self.db.execute(
            "INSERT INTO channel_scales(experiment_id, pnn, scale_id) VALUES (?,?,?) "
            "ON CONFLICT(experiment_id, pnn) DO UPDATE SET scale_id=excluded.scale_id",
            (exp_id, pnn, sid),
        )
        return sid

    # ---- snapshot dicts ------------------------------------------------------
    def sample_dict(self, row) -> dict:
        channels = [
            {"idx": c["idx"], "col": c["col"], "pnn": c["pnn"], "pns": c["pns"], "kind": c["kind"],
             "pnr": c["pnr"], "vmin": c["vmin"], "vmax": c["vmax"]}
            for c in self.db.all("SELECT * FROM channels WHERE sample_id=? ORDER BY idx", (row["id"],))
        ]
        comp = None
        if row["comp_matrix_id"] is not None:
            m = self.db.one("SELECT * FROM comp_matrices WHERE id=?", (row["comp_matrix_id"],))
            comp = {"detectors": json.loads(m["detectors"]), "matrix": json.loads(m["matrix"]), "hash": m["hash"]}
        return {
            "id": row["id"], "name": row["name"], "sha256": row["sha256"], "n_events": row["n_events"],
            "n_nonfinite": row["n_nonfinite"], "parquet_file": row["parquet_file"], "channels": channels,
            "comp": comp, "comp_state": row["comp_state"], "comp_enabled": bool(row["comp_enabled"]),
            "comp_message": row["comp_message"], "group_label": row["group_label"],
        }

    def gate_dicts(self, exp_id: int) -> list[dict]:
        overrides: dict[int, dict[str, dict]] = {}
        for o in self.db.all(
            "SELECT o.* FROM gate_overrides o JOIN gates g ON g.id=o.gate_id WHERE g.experiment_id=?", (exp_id,)
        ):
            overrides.setdefault(o["gate_id"], {})[str(o["sample_id"])] = json.loads(o["geom"])
        out = []
        for g in self.db.all("SELECT * FROM gates WHERE experiment_id=? ORDER BY id", (exp_id,)):
            dims = []
            for d in json.loads(g["dims"]):
                sc = self.scale(d["scale_id"])
                dims.append({"pnn": d["pnn"], "scale_id": d["scale_id"], "comp": d.get("comp", "sample"),
                             "scale": {"kind": sc.kind, "params": sc.p}})
            out.append({"id": g["id"], "parent_id": g["parent_id"], "name": g["name"], "kind": g["kind"],
                        "dims": dims, "geom": json.loads(g["geom"]), "overrides": overrides.get(g["id"], {})})
        return out

    def snapshot(self, exp_id: int) -> dict:
        exp = self.db.one("SELECT * FROM experiments WHERE id=?", (exp_id,))
        samples = [self.sample_dict(r) for r in
                   self.db.all("SELECT * FROM samples WHERE experiment_id=? ORDER BY id", (exp_id,))]
        return {"experiment": {"id": exp["id"], "name": exp["name"], "revision": exp["revision"]},
                "samples": samples, "gates": self.gate_dicts(exp_id)}

    # ---- engine objects ------------------------------------------------------
    def sample_data(self, row) -> SampleData:
        return snap.sample_from_dict(self.sample_dict(row), self.settings.events_dir, self.cache)

    def nodes(self, exp_id: int, sample_id: int) -> dict[int, GateNode]:
        return snap.nodes_for_sample(self.gate_dicts(exp_id), sample_id)

    # ---- statistics (cached by gate key) -------------------------------------
    def stats(self, sample_row, sdata: SampleData, gate_id: int, node: GateNode | None) -> dict:
        key = stats_mod.stats_key(self.engine, sdata, node)
        row = self.db.one(
            "SELECT gate_key, stats FROM population_stats WHERE sample_id=? AND gate_id=?",
            (sample_row["id"], gate_id),
        )
        if row is not None and row["gate_key"] == key:
            return json.loads(row["stats"])
        result = stats_mod.population_stats(self.engine, sdata, node)
        self.store_stats(sample_row["id"], gate_id, key, result)
        return result

    def store_stats(self, sample_id: int, gate_id: int, key: str, result: dict) -> None:
        self.db.execute(
            "INSERT INTO population_stats(sample_id, gate_id, gate_key, stats, computed_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(sample_id, gate_id) DO UPDATE SET gate_key=excluded.gate_key, "
            "stats=excluded.stats, computed_at=excluded.computed_at",
            (sample_id, gate_id, key, json.dumps(result), utcnow()),
        )

    # ---- deletion ------------------------------------------------------------
    def delete_sample_files(self, row) -> None:
        still_used = self.db.one(
            "SELECT COUNT(*) AS n FROM samples WHERE parquet_file=? AND id<>?", (row["parquet_file"], row["id"])
        )["n"]
        if not still_used:
            path = Path(self.settings.events_dir) / row["parquet_file"]
            path.unlink(missing_ok=True)
            sha = row["sha256"]
            shutil.rmtree(Path(self.settings.masks_dir) / sha[:2] / sha, ignore_errors=True)
            for kind in ("raw", "raw64", "comp", "scaled", "mask"):
                self.cache.drop_prefix((kind, sha))
