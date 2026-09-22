"""かんたん入力（選択肢 → 日本語の説明文）のテスト。"""

from __future__ import annotations

import pytest

from src import character_profile as cp
from src.prompt_generator import DEFAULT_CHARACTER_JA


def test_office_worker_preset_reproduces_bundled_text():
    """「会社員（男性）」は同梱の説明文と一字一句同じになる（既存キャラが変わらない）。"""
    assert cp.compose(cp.PRESETS["会社員（男性）"]) == DEFAULT_CHARACTER_JA


@pytest.mark.parametrize("name", list(cp.PRESETS))
def test_every_preset_composes_clean_text(name):
    text = cp.compose(cp.PRESETS[name])
    lines = text.splitlines()
    assert len(lines) >= 4
    assert all(line.endswith("。") for line in lines), lines
    assert "None" not in text
    assert "。。" not in text


def test_presets_only_use_defined_options():
    """ひな形が選択肢に無い値を使っていると、画面で選択状態を再現できません。"""
    for name, preset in cp.PRESETS.items():
        assert cp.normalize(preset) == preset, name


def test_animal_hides_human_only_fields():
    profile = {"kind": "動物", "animal": "ねこ", "gender": "男性", "hair": "ボブ", "job": "会社員"}
    norm = cp.normalize(profile)
    assert norm == {"kind": "動物", "animal": "ねこ"}
    text = cp.compose(profile)
    assert text.startswith("ねこのキャラクター。")
    assert "ボブ" not in text and "会社員" not in text


def test_human_hides_animal_field():
    assert "animal" not in cp.normalize({"kind": "人", "animal": "ねこ"})


def test_unknown_values_are_dropped():
    norm = cp.normalize({"kind": "宇宙人", "heads": "10頭身", "mood": ["元気", "<script>"]})
    # kind と heads は不正値なので消え、mood は正しい値「元気」だけが残る
    assert norm == {"mood": ["元気"]}


def test_multi_select_order_is_stable():
    a = cp.compose({"kind": "人", "mood": ["コミカル", "親しみやすい"]})
    b = cp.compose({"kind": "人", "mood": ["親しみやすい", "コミカル"]})
    assert a == b
    assert "親しみやすく、コミカルな雰囲気。" in a


@pytest.mark.parametrize(
    "profile,expected",
    [
        ({"kind": "人（日本人）", "gender": "女性", "age": "20代"}, "20代くらいの日本人女性。"),
        ({"kind": "人（日本人）", "age": "30代", "job": "会社員"}, "30代くらいの日本人の会社員。"),
        ({"kind": "人", "gender": "男性", "age": "子ども"}, "男の子。"),
        ({"kind": "人（日本人）", "gender": "女性", "age": "シニア"}, "日本人のおばあさん。"),
        ({"kind": "人"}, "人のキャラクター。"),
        ({"kind": "動物"}, "動物のキャラクター。"),
    ],
)
def test_person_line(profile, expected):
    assert cp.compose(profile).splitlines()[0] == expected


@pytest.mark.parametrize(
    "hair,color,expected",
    [
        ("短髪", "黒", "短い黒髪。"),
        ("短髪", "茶", "短い茶色の髪。"),
        ("短髪", None, "短い髪。"),
        ("ボブ", "茶", "茶色のボブヘア。"),
        ("ロング", None, "ロングヘア。"),
        (None, "黒", "黒髪。"),
        (None, "金", "金色の髪。"),
    ],
)
def test_hair_line(hair, color, expected):
    profile = {"kind": "人"}
    if hair:
        profile["hair"] = hair
    if color:
        profile["hair_color"] = color
    assert expected in cp.compose(profile).splitlines()


def test_outfit_lines():
    assert "白いワイシャツ。\nシンプルな濃い色のネクタイ。" in cp.compose(
        {"kind": "人", "outfit": "ワイシャツとネクタイ", "outfit_color": "白"})
    assert "黄色いパーカー。" in cp.compose({"kind": "人", "outfit": "パーカー", "outfit_color": "黄"})
    # 「白い白衣」のような重複を避ける
    assert "白衣。" in cp.compose({"kind": "人", "outfit": "白衣", "outfit_color": "白"}).splitlines()


def test_extra_text_is_appended_and_limited():
    text = cp.compose({"kind": "人", "extra": "左目の下にほくろ。\n\n  いつも笑顔。 "})
    assert text.splitlines()[-2:] == ["左目の下にほくろ。", "いつも笑顔。"]
    long = cp.normalize({"extra": "あ" * 2000})
    assert len(long["extra"]) == cp.EXTRA_MAX_CHARS


def test_save_and_load_profile(tmp_path):
    path = tmp_path / "profile.json"
    cp.save_profile(path, {**cp.PRESETS["ねこ"], "bogus": "x"})
    assert cp.load_profile(path) == cp.PRESETS["ねこ"]
    # 日本語がそのまま読める形で保存される
    assert "ねこ" in path.read_text(encoding="utf-8")


def test_load_profile_missing_or_broken(tmp_path):
    assert cp.load_profile(tmp_path / "none.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert cp.load_profile(broken) is None


def test_infer_profile_from_text():
    assert cp.infer_profile(DEFAULT_CHARACTER_JA) == cp.PRESETS["会社員（男性）"]
    assert cp.infer_profile("自由に書いた説明。") is None


# --- ランキングの傾向から追加したひな形 -----------------------------------
def test_every_preset_has_category_and_reason():
    assert set(cp.PRESETS) == set(cp.PRESET_INFO)
    for name, info in cp.PRESET_INFO.items():
        assert info["category"] in cp.PRESET_CATEGORIES, name
        assert info["note"], name


def test_presets_do_not_name_existing_characters():
    """人気キャラの名前をひな形に使わない（似せたスタンプは審査で落ちる・権利の問題）。"""
    banned = ["ちいかわ", "ハチワレ", "うさまる", "ピングー", "モンチッチ", "もちにゃみ",
              "コビハム", "うるせぇトリ", "もちわさ", "プリキュア"]
    texts = [cp.compose(p) for p in cp.PRESETS.values()] + list(cp.PRESETS)
    for word in banned:
        assert not any(word in t for t in texts), word


@pytest.mark.parametrize(
    "profile,first_line",
    [
        ({"kind": "ふしぎな生き物", "creature": "おばけ"}, "おばけのキャラクター。"),
        ({"kind": "ふしぎな生き物"}, "ふしぎな生き物のキャラクター。"),
        ({"kind": "食べ物", "food": "プリン"}, "プリンに顔と手足がついたキャラクター。"),
        ({"kind": "動物", "animal": "ハシビロコウ"}, "ハシビロコウのキャラクター。"),
    ],
)
def test_new_kinds(profile, first_line):
    assert cp.compose(profile).splitlines()[0] == first_line


def test_fur_color_only_for_animals_and_creatures():
    assert "三毛もようの体。" in cp.compose({"kind": "動物", "animal": "ねこ", "fur": "三毛"})
    assert "fur" not in cp.normalize({"kind": "人", "fur": "白"})
    assert "fur" not in cp.normalize({"kind": "食べ物", "fur": "白"})


# --- おまかせ（シャッフル） ---------------------------------------------------
def test_random_profiles_are_consistent():
    import random

    rng = random.Random(0)
    for _ in range(300):
        p = cp.random_profile(rng)
        assert p == cp.normalize(p)          # 選択肢の範囲内・表示条件どおり
        assert cp.compose(p)                 # 説明文が作れる
        name = cp.profile_name(p)
        assert name and len(name) <= 20
        if p["kind"] in cp.HUMAN_KINDS:
            if p["job"] == "学生":
                assert p["age"] == "10代"
            if p["job"] in ("医師", "看護師"):
                assert p["outfit"] == "白衣"
            if p["gender"] == "男性":
                assert not set(p.get("items", [])) & {"リボン"} and p["outfit"] not in ("セーラー服", "ワンピース")
        else:
            assert "gender" not in p
        if p["kind"] == "食べ物":
            assert "ふわふわの毛" not in p.get("items", []) and "fur" not in p


def test_random_endpoint(tmp_config):
    from src.webapp import create_app

    c = create_app(tmp_config).test_client()
    chars = c.get("/api/master/random?n=6").get_json()["characters"]
    assert 1 <= len(chars) <= 6
    assert len({x["name"] for x in chars}) == len(chars)
    assert all(x["text"] and x["profile"]["kind"] for x in chars)
