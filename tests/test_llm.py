"""ローカルLLM連携（src/llm.py）のテスト。

本物のサーバーは使わず、httpx.MockTransport で「LLMの答え」を差し替えて確かめます。
"""

from __future__ import annotations

import json

import httpx
import pytest

from src import listing as L
from src import llm


def _llm(tmp_config, replies, seen=None):
    """replies の文字列を順番に返す偽のLLMサーバー。"""
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fake.gguf"}]})
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                      "message": {"content": queue.pop(0)}}]})

    return llm.LocalLLM(tmp_config, transport=httpx.MockTransport(handler))


def test_extract_json_handles_fences_and_chatter():
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('はい、どうぞ。\n[{"text": "OK"}]\n以上です') == [{"text": "OK"}]
    with pytest.raises(ValueError):
        llm.extract_json("JSONはありません")


def test_status_ok_and_down(tmp_config):
    assert _llm(tmp_config, []).status()["ok"] is True

    def down(request):
        raise httpx.ConnectError("refused")

    s = llm.LocalLLM(tmp_config, transport=httpx.MockTransport(down)).status()
    assert s["ok"] is False and "start_llm.bat" in s["error"]


def test_thinking_is_disabled_by_default(tmp_config):
    seen = []
    _llm(tmp_config, ["ok"], seen).chat("sys", "user")
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_empty_answer_at_length_limit_explains_fix(tmp_config):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": ""}}]})

    with pytest.raises(llm.LLMError, match="max_tokens"):
        llm.LocalLLM(tmp_config, transport=httpx.MockTransport(handler)).chat("s", "u")


def test_suggest_phrases_filters_bad_rows(tmp_config):
    answer = json.dumps([
        {"text": "作業再開します", "action": "PCに向かう", "expression": "決意した表情", "category": "work"},
        {"text": "了解です！", "action": "a", "expression": "e", "category": "basic"},        # 既存と同じ
        {"text": "これはとても長すぎるセリフなので使えません", "action": "a", "expression": "e"},  # 長すぎ
        {"text": "やったね\U0001F389", "action": "a", "expression": "e"},                  # 絵文字
        {"text": "休憩しよ", "action": "伸びをする", "expression": "笑顔", "category": "???"},  # 不明カテゴリ
    ], ensure_ascii=False)
    rows = llm.suggest_phrases(_llm(tmp_config, [answer, "[]", "[]", "[]"]), theme="在宅",
                               count=5, existing=["了解です"])
    assert [r["text"] for r in rows] == ["作業再開します", "休憩しよ"]
    assert rows[1]["category"] == "misc"


def test_chat_json_retries_once_when_unreadable(tmp_config):
    seen = []
    got = _llm(tmp_config, ["すみません、うまく作れませんでした", '[{"text":"OK"}]'], seen).chat_json("s", "u")
    assert got == [{"text": "OK"}] and len(seen) == 2


def test_fill_pose_keeps_order_by_text(tmp_config):
    answer = json.dumps([
        {"text": "やったー！", "action": "両拳を突き上げる", "expression": "満面の笑み"},
        {"text": "おつかれさま！", "action": "お辞儀する", "expression": "微笑み"},
    ], ensure_ascii=False)
    out = llm.fill_pose(_llm(tmp_config, [answer]), ["おつかれさま！", "やったー！"])
    assert out[0]["action"] == "お辞儀する" and out[1]["expression"] == "満面の笑み"


def test_write_listing_retries_until_rules_pass(tmp_config):
    first = json.dumps({"title_en": "Cute LINE Bird", "desc_en": "A cute bird.",
                        "title_ja": "かわいいことりのとても長いタイトルのスタンプ", "desc_ja": "ことりです。"},
                       ensure_ascii=False)
    second = json.dumps({"title_en": "Cute Bird", "desc_en": "x", "title_ja": "かわいいことり", "desc_ja": "y"},
                        ensure_ascii=False)
    seen = []
    out = llm.write_listing(_llm(tmp_config, [first, second], seen), character="ことり", texts=["了解"])
    assert out == {"title_en": "Cute Bird", "desc_en": "A cute bird.",
                   "title_ja": "かわいいことり", "desc_ja": "ことりです。"}
    assert all(not L.check_field(k, v) for k, v in out.items())
    assert "ルールに合いませんでした" in seen[1]["messages"][1]["content"]


def test_polish_keeps_what_the_character_is(tmp_config):
    answer = "・3頭身のちび。\n・茶色の体。\n（※補足です）"
    out = llm.polish_character(_llm(tmp_config, [answer]), "ペンギンのキャラクター。\n茶色の体。")
    assert out.splitlines() == ["ペンギンのキャラクター。", "3頭身のちび。", "茶色の体。"]


# --- GUI のエンドポイント ------------------------------------------------------
class _FakeLLM:
    def __init__(self, config, transport=None):
        pass

    def status(self):
        return {"ok": True, "base_url": "fake", "models": []}


def test_endpoints_return_results_and_errors(tmp_config, monkeypatch):
    from src.webapp import create_app

    monkeypatch.setattr(llm, "LocalLLM", _FakeLLM)
    monkeypatch.setattr(llm, "fill_pose", lambda _l, texts, character="": [{"action": "a", "expression": "e"}] * len(texts))

    def boom(*a, **k):
        raise llm.LLMError("サーバーに接続できません")

    monkeypatch.setattr(llm, "polish_character", boom)
    c = create_app(tmp_config).test_client()
    assert c.get("/api/llm/status").get_json()["ok"] is True
    assert c.post("/api/llm/fill", json={"texts": ["了解", "OK"]}).get_json()["results"][1]["expression"] == "e"
    assert c.post("/api/llm/fill", json={"texts": []}).status_code == 400
    r = c.post("/api/llm/polish", json={"character": "ねこ"})
    assert r.status_code == 502 and "接続" in r.get_json()["error"]
