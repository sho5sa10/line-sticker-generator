"""E2E: 100フレーズ → 候補生成 → 評価 → 代表候補 → 40フレーズ選択 → 検証 → ZIP。

既存の本体コード（GUI の API と、CLI が呼ぶ関数）だけで最後まで到達できるかを確認します。
画像生成APIは呼びません（偽のプロバイダ。通信も遮断し、試みがあれば失敗にします）。
"""

from __future__ import annotations

import io
import json
import socket
import threading
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("flask", reason="Flask が未インストールです")

from src import image_generator as ig  # noqa: E402
from src import providers  # noqa: E402
from src import scoring  # noqa: E402
from src import variants as vr  # noqa: E402
from src.providers import openai_provider  # noqa: E402
from src.providers.base import ImageGenerationProvider  # noqa: E402
from tests.test_scoring import degrade, sticker_like  # noqa: E402

GUI = {"X-Sticker-Client": "1"}
WAIT = 120
PHRASES = 100
CANDIDATES = 2
PICK = 40
IDS = [f"{i:03d}" for i in range(1, PHRASES + 1)]


def _defect(sticker_id: str, variant_id: str) -> str | None:
    """壊れた候補を混ぜます（代表候補の決定で避けられるかを見るため）。"""
    n = int(sticker_id)
    if variant_id == "v001" and n % 4 == 0:
        return "tiny"          # gate:too_small
    if variant_id == "v002" and n % 5 == 0:
        return "cropped"       # gate:cropped
    return None


class FakeProvider(ImageGenerationProvider):
    name = "fake"

    def __init__(self, box):
        self.box = box

    def generate(self, prompt, reference_image=None, output_path=None):
        with self.box["lock"]:
            self.box["calls"] += 1
            n = self.box["calls"]
        path = Path(output_path)
        variant_id = path.name.split(".")[0]          # vNNN.png / vNNN.png.part など
        img = sticker_like(body=(20 * n % 255, 160, 200, 255))
        kind = _defect(path.parent.name, variant_id)
        if kind:
            img = degrade(img, kind)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        self._write(data, output_path)
        return data

    def estimate_cost_usd(self, count):
        return 0.011 * count


@pytest.fixture
def box(tmp_config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-dummy-not-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")
    b = {"calls": 0, "lock": threading.Lock(), "network": []}

    def create(config):
        return FakeProvider(b)

    def connect(self, address):
        b["network"].append(address)
        raise OSError(f"テスト中の通信は禁止です: {address}")

    def real_provider(*args, **kwargs):
        raise AssertionError("実プロバイダが呼ばれました")
    monkeypatch.setattr(ig, "create_provider", create)
    monkeypatch.setattr(providers, "create_provider", create)
    monkeypatch.setattr(openai_provider.OpenAIImageProvider, "generate", real_provider)
    monkeypatch.setattr(socket.socket, "connect", connect)
    yield b
    assert b["network"] == []


@pytest.fixture
def client(tmp_config, box):
    from src.webapp import create_app

    csv_path = tmp_config.root / "data" / "stickers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(f"{sid},セリフ{sid},手を振る,笑顔,basic\n" for sid in IDS)
    csv_path.write_text("id,text,action,expression,category\n" + rows, encoding="utf-8")
    tmp_config.raw["data"]["csv"] = "data/stickers.csv"
    tmp_config.raw["generation"]["retry_backoff_sec"] = 0
    tmp_config.master_image_path.parent.mkdir(parents=True, exist_ok=True)
    sticker_like().save(tmp_config.master_image_path)
    app = create_app(tmp_config)
    app.config["TESTING"] = False
    return app.test_client()


def _join_jobs():
    for t in [t for t in threading.enumerate() if t.name.startswith("job-")]:
        t.join(WAIT)
        assert not t.is_alive(), "ジョブが終わりません"


def _initial(client, ids, count):
    plan = client.post("/api/variants/initial", json={"ids": ids, "count": count, "dry_run": True},
                       headers=GUI)
    if plan.status_code != 200:
        return plan
    r = client.post("/api/variants/initial",
                    json={"ids": ids, "count": count, "expected_total": plan.get_json()["expected_total"]},
                    headers=GUI)
    _join_jobs()
    return r


def _best_variant(sticker: dict) -> dict | None:
    """代表候補: gate の無い候補のうち、quality + visibility が最も高いもの（同点は番号の若い方）。

    既存コードにこの選び方の関数は無いため、記録（variants.json の評価結果）から読み取ります。
    """
    ok = [v for v in sticker["variants"]
          if isinstance(v.get("derived_scores"), dict) and not scoring.gates(v.get("flags") or [])]
    if not ok:
        return None
    return sorted(ok, key=lambda v: (-(v["derived_scores"]["quality"] + v["derived_scores"]["visibility"]),
                                     v["variant_id"]))[0]


def test_100_phrases_to_40_sticker_zip(client, tmp_config, box):
    cfg = tmp_config

    # --- 1-3. 候補生成（GUI の初回生成）----------------------------------------
    # 100フレーズ×2案 = 200枚は、1回の上限（INITIAL_MAX_TOTAL=100）を超えるので断られます
    r = _initial(client, IDS, CANDIDATES)
    assert r.status_code == 400 and r.get_json()["total"] == PHRASES * CANDIDATES
    assert box["calls"] == 0
    # 上限の範囲に分ければ通ります（50フレーズ×2案 を2回）
    for chunk in (IDS[:50], IDS[50:]):
        r = _initial(client, chunk, CANDIDATES)
        assert r.status_code == 200, r.get_json()
        assert client.get("/api/job").get_json()["job"]["status"] == "finished"
    assert box["calls"] == PHRASES * CANDIDATES
    state = json.loads(cfg.variants_path.read_text(encoding="utf-8"))
    for sid in IDS:
        rec = state["stickers"][sid]
        assert [v["variant_id"] for v in rec["variants"]] == ["v001", "v002"]
        assert rec["adopted"] is None                      # 自動では採用しない
        assert "initial_run" not in rec
    assert not (cfg.dir_generated / "001.png").exists()     # 原画・完成画像はまだ無い
    assert not list(cfg.dir_final.glob("*.png"))

    # --- 4. 機械評価（GUI は1件ずつのみ。一括は CLI `variants score` が呼ぶ score_all）---
    results = scoring.score_all(cfg, IDS)
    assert len(results) == PHRASES * CANDIDATES
    assert all(r["status"] == "scored" for r in results)

    # --- 5. 各フレーズの代表候補（評価結果を API で読み取り、テスト側で決める）--------
    stickers = client.get("/api/variants").get_json()["stickers"]
    best = {sid: _best_variant(stickers[sid]) for sid in IDS}
    for sid in IDS:
        n = int(sid)
        if n % 20 == 0:
            assert best[sid] is None                       # 2案とも壊れている → 代表なし
        elif n % 4 == 0:
            assert best[sid]["variant_id"] == "v002"       # v001 が極小 → 避ける
        elif n % 5 == 0:
            assert best[sid]["variant_id"] == "v001"       # v002 が端切れ → 避ける
    no_best = [sid for sid in IDS if best[sid] is None]
    assert no_best == ["020", "040", "060", "080", "100"]

    # --- 5b. 代表なしのフレーズだけ再生成（regen の印 → GUI の再生成）------------
    for sid in no_best:
        r = client.post(f"/api/variants/{sid}/v001/verdict", json={"verdict": "regen"}, headers=GUI)
        assert r.status_code == 200, r.get_json()
    plan = client.post("/api/variants/generate", json={"regen_only": True, "count": 1, "dry_run": True},
                       headers=GUI).get_json()
    assert sorted(t["id"] for t in plan["targets"]) == no_best and plan["total"] == len(no_best)
    r = client.post("/api/variants/generate",
                    json={"regen_only": True, "count": 1, "expected_total": plan["total"]}, headers=GUI)
    assert r.status_code == 200, r.get_json()
    _join_jobs()
    assert box["calls"] == PHRASES * CANDIDATES + len(no_best)
    rescored = scoring.score_all(cfg, no_best)              # 評価済みは飛ばし、新しい v003 だけ計算
    assert {(r["variant_id"], r["status"]) for r in rescored} == {
        ("v001", "skipped"), ("v002", "skipped"), ("v003", "scored")}
    stickers = client.get("/api/variants").get_json()["stickers"]
    best = {sid: _best_variant(stickers[sid]) for sid in IDS}
    assert all(best[sid] is not None for sid in IDS)
    assert all(best[sid]["variant_id"] == "v003" for sid in no_best)

    # --- 6. 40フレーズを選ぶ（代表候補の点数順、同点は番号順）----------------------
    ranked = sorted(IDS, key=lambda s: (-(best[s]["derived_scores"]["quality"]
                                          + best[s]["derived_scores"]["visibility"]), s))
    picked = sorted(ranked[:PICK])
    assert len(picked) == PICK

    # --- 7. 選択の保持: 既存で保存できる印は「販売状況」だけ（data/sales.json）---------
    r = client.post("/api/sales", json={"ids": picked, "status": "review"}, headers=GUI)
    assert r.status_code == 200
    rows = client.get("/api/stickers").get_json()["stickers"]
    kept = sorted(s["id"] for s in rows if s.get("sale") == "review")
    assert kept == picked

    # --- 代表候補を採用（選んだ40件だけ。採用時に合成と検証が走る）-------------------
    for sid in kept:
        r = client.post(f"/api/variants/{sid}/{best[sid]['variant_id']}/adopt", headers=GUI)
        assert r.status_code == 200, (sid, r.get_json())
    assert sorted(p.stem for p in cfg.dir_final.glob("*.png")) == kept

    # --- 8. 検証 -------------------------------------------------------------
    v = client.post("/api/validate", headers=GUI).get_json()
    assert v["checked"] == PICK and v["errors"] == 0, v

    # --- 9. ZIP（選んだスタンプだけで1セット）---------------------------------
    plan = client.post("/api/package/plan", json={"ids": kept}, headers=GUI).get_json()
    assert not plan.get("error"), plan
    r = client.post("/api/package", json={"ids": kept}, headers=GUI)
    assert r.status_code == 200, r.get_json()
    pkgs = [p for p in r.get_json()["packages"] if p["downloadable"]]
    assert [p["name"] for p in pkgs] == [f"line_stickers_selected_{PICK}.zip"]
    assert r.get_json()["main"]["ok"] and r.get_json()["tab"]["ok"]

    # --- 10. ZIP の中身 --------------------------------------------------------
    d = client.get(f"/api/download/{pkgs[0]['name']}", headers=GUI)
    assert d.status_code == 200
    with zipfile.ZipFile(io.BytesIO(d.data)) as zf:
        names = zf.namelist()
        assert names == [f"{i:02d}.png" for i in range(1, PICK + 1)] + ["main.png", "tab.png"]
        for i, sid in enumerate(kept, start=1):         # CSV の順に 01〜40
            assert zf.read(f"{i:02d}.png") == (cfg.dir_final / f"{sid}.png").read_bytes()
