"""ローカルLLM（OpenAI 互換 API）で文章づくりを手伝います。

- セリフ一覧の案（セリフ・ポーズ・表情・カテゴリ）
- ポーズ・表情の自動入力
- 申請用のタイトル・説明文
- キャラクターの説明文を整える

画像は作りません。ik_llama.cpp / llama.cpp の llama-server や Ollama など、
`/v1/chat/completions` を持つサーバーならどれでも使えます。料金はかかりません。
LLM の答えはそのまま使わず、形式・文字数・禁止事項をこちらで確かめてから返します。
"""

from __future__ import annotations

import json
import re

import httpx

from . import listing as listing_mod

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
MAX_TEXT_LEN = 15          # スタンプのセリフは短く
CHUNK = 10                 # 一度に頼む件数（多すぎると形式が崩れやすい）
KNOWN_CATEGORIES = [
    "basic", "reply", "thanks", "apology", "request", "work", "move", "greeting", "joy",
    "surprise", "think", "trouble", "tired", "care", "life", "misc",
]


class LLMError(RuntimeError):
    """ローカルLLMに接続できない・答えが読めないときに送出されます。"""


# ---------------------------------------------------------------------------
# 接続
# ---------------------------------------------------------------------------
class LocalLLM:
    def __init__(self, config, transport: httpx.BaseTransport | None = None):
        self.base_url = str(config.get("llm.base_url", DEFAULT_BASE_URL)).rstrip("/")
        self.model = str(config.get("llm.model", "local"))
        self.timeout = float(config.get("llm.timeout_sec", 600))
        self.max_tokens = int(config.get("llm.max_tokens", 4096))
        self.disable_thinking = bool(config.get("llm.disable_thinking", True))
        self._transport = transport

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(timeout=timeout, transport=self._transport)

    def status(self) -> dict:
        """サーバーが動いているか（数秒で確認します）。"""
        try:
            with self._client(5) as c:
                r = c.get(f"{self.base_url}/models")
                r.raise_for_status()
                models = [m.get("id", "") for m in r.json().get("data", [])]
            return {"ok": True, "base_url": self.base_url, "models": models}
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "base_url": self.base_url, "error": _conn_message(exc, self.base_url)}

    def chat(self, system: str, user: str, *, temperature: float = 0.7) -> str:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": self.max_tokens,
            "temperature": temperature,
        }
        if self.disable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        try:
            with self._client(self.timeout) as c:
                r = c.post(f"{self.base_url}/chat/completions", json=body)
                r.raise_for_status()
                data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(_conn_message(exc, self.base_url)) from exc
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("ローカルLLMの答えの形式が想定と違います") from exc
        if not content.strip():
            if choice.get("finish_reason") == "length":
                raise LLMError("答えの途中で文字数の上限に達しました。config の llm.max_tokens を増やしてください")
            raise LLMError("ローカルLLMから空の答えが返りました")
        return content

    def chat_json(self, system: str, user: str, *, temperature: float = 0.7):
        """JSON で答えさせて読み取ります。読めなければ1回だけ頼み直します。"""
        last = ""
        for attempt in range(2):
            prompt = user if attempt == 0 else (
                user + "\n\n前回の答えはJSONとして読めませんでした。説明文を付けず、JSONだけを出力してください。")
            last = self.chat(system, prompt, temperature=temperature)
            try:
                return extract_json(last)
            except ValueError:
                continue
        raise LLMError(f"ローカルLLMの答えをJSONとして読めませんでした: {last[:200]}")


def _conn_message(exc: Exception, base_url: str) -> str:
    if isinstance(exc, httpx.ConnectError):
        return (f"ローカルLLMのサーバーに接続できません（{base_url}）。"
                "C:\\llm\\start_llm.bat などでサーバーを起動してください")
    if isinstance(exc, httpx.TimeoutException):
        return "ローカルLLMの応答がタイムアウトしました。件数を減らすか、config の llm.timeout_sec を増やしてください"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"ローカルLLMのサーバーがエラーを返しました（{exc.response.status_code}）"
    return f"ローカルLLMとの通信に失敗しました: {exc}"


def extract_json(text: str):
    """答えの中の JSON を取り出します（```json の囲みや前後の説明文があっても読めるように）。"""
    text = re.sub(r"```(?:json)?", "", text).strip()
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    if not starts:
        raise ValueError("JSON が見つかりません")
    start = min(starts)
    end = max(text.rfind("]"), text.rfind("}"))
    if end <= start:
        raise ValueError("JSON が閉じていません")
    return json.loads(text[start:end + 1])


def _clean(s, limit: int = 60) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip().strip("「」\"'")
    return s[:limit]


def _chunks(items: list, n: int = CHUNK):
    for i in range(0, len(items), n):
        yield items[i:i + n]


# ---------------------------------------------------------------------------
# 1. セリフ一覧の案
# ---------------------------------------------------------------------------
PHRASES_SYSTEM = (
    "あなたはLINEスタンプの企画担当です。日本語で、スタンプに書く短いセリフを考えます。"
    "指示されたJSONだけを出力し、説明文は書きません。"
)


def suggest_phrases(llm: LocalLLM, *, theme: str, count: int, character: str = "",
                    existing: list[str] | None = None, examples: list[dict] | None = None) -> list[dict]:
    """テーマに合うセリフの行（text / action / expression / category）を count 件まで返します。"""
    count = max(1, min(int(count), 40))
    existing_set = {_norm(t) for t in (existing or [])}
    rows: list[dict] = []
    for _ in range(4):  # 重複で減った分を補うため、最大4回まで頼みます
        need = min(CHUNK, count - len(rows))
        if need <= 0:
            break
        taken = [r["text"] for r in rows] + list(existing or [])
        user = _phrases_prompt(theme, need, character, taken[-60:], examples or [])
        data = llm.chat_json(PHRASES_SYSTEM, user, temperature=0.9)
        for item in data if isinstance(data, list) else data.get("items", []):
            row = _phrase_row(item)
            if row and _norm(row["text"]) not in existing_set:
                existing_set.add(_norm(row["text"]))
                rows.append(row)
                if len(rows) >= count:
                    break
    return rows


def _phrases_prompt(theme: str, n: int, character: str, taken: list[str], examples: list[dict]) -> str:
    ex = examples[:4] or [
        {"text": "了解です", "action": "両手を体の横に添えて丁寧に会釈する", "expression": "真面目な表情", "category": "basic"},
        {"text": "ありがとう！", "action": "両手を合わせてお礼をする", "expression": "嬉しそうな笑顔", "category": "thanks"},
    ]
    lines = [
        f"テーマ: {theme or '毎日使える'}",
        f"キャラクター: {character[:300]}" if character else "",
        f"LINEスタンプ用のセリフを{n}個考えてください。",
        f"- セリフ(text)は{MAX_TEXT_LEN}文字以内。日常のやりとりですぐ使えるもの",
        "- action はそのセリフに合う全身のポーズ（日本語で短く）",
        "- expression は顔の表情（日本語で短く）",
        f"- category は次のどれか: {', '.join(KNOWN_CATEGORIES)}",
        "- 絵文字は使わない",
    ]
    if taken:
        lines.append("- 次のセリフと同じものは出さない: " + " / ".join(taken))
    lines.append("出力形式（JSON配列だけ）の例:")
    lines.append(json.dumps(ex, ensure_ascii=False))
    return "\n".join(line for line in lines if line)


def _phrase_row(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    text = _clean(item.get("text"), 40)
    if not text or len(text) > MAX_TEXT_LEN or _has_emoji(text):
        return None
    cat = str(item.get("category", "")).strip().lower()
    return {
        "text": text,
        "action": _clean(item.get("action")) or "胸の前で手を振る",
        "expression": _clean(item.get("expression"), 30) or "笑顔",
        "category": cat if cat in KNOWN_CATEGORIES else "misc",
    }


def _norm(text: str) -> str:
    return re.sub(r"[！!？?…。、〜ー\s]", "", text or "")


def _has_emoji(text: str) -> bool:
    return listing_mod._has_emoji(text)


# ---------------------------------------------------------------------------
# 2. ポーズ・表情の自動入力
# ---------------------------------------------------------------------------
FILL_SYSTEM = (
    "あなたはLINEスタンプのイラストの演出担当です。セリフに合う全身のポーズと顔の表情を、"
    "日本語で短く決めます。指示されたJSONだけを出力します。"
)


def fill_pose(llm: LocalLLM, texts: list[str], *, character: str = "") -> list[dict]:
    """セリフの一覧に対して、同じ順で {action, expression} を返します。"""
    out: list[dict] = []
    for part in _chunks(list(texts)):
        user = "\n".join([
            f"キャラクター: {character[:300]}" if character else "",
            "次のセリフそれぞれに合う、全身のポーズ(action)と表情(expression)を決めてください。",
            "ポーズは画像生成AIに伝わるよう具体的に（例: 片手でびしっと敬礼する）。",
            "出力は入力と同じ順・同じ件数のJSON配列: "
            '[{"text":"了解！","action":"片手でびしっと敬礼する","expression":"自信のある笑顔"}]',
            "セリフ一覧:",
            json.dumps(part, ensure_ascii=False),
        ])
        data = llm.chat_json(FILL_SYSTEM, "\n".join(x for x in user.split("\n") if x), temperature=0.4)
        items = data if isinstance(data, list) else []
        by_text = {_norm(str(i.get("text", ""))): i for i in items if isinstance(i, dict)}
        for idx, text in enumerate(part):
            item = by_text.get(_norm(text)) or (items[idx] if idx < len(items) and isinstance(items[idx], dict) else {})
            out.append({
                "action": _clean(item.get("action")),
                "expression": _clean(item.get("expression"), 30),
            })
    return out


# ---------------------------------------------------------------------------
# 3. 申請用のタイトル・説明文
# ---------------------------------------------------------------------------
LISTING_SYSTEM = (
    "あなたはLINEスタンプの販売ページの文章を書く担当です。魅力的で自然な文章を書きます。"
    "指示されたJSONだけを出力します。"
)


def write_listing(llm: LocalLLM, *, character: str, texts: list[str], volume: int = 1) -> dict:
    """タイトル・説明文（英語・日本語）を作り、LINE のルールに合うか確かめて返します。

    ルールに合わない項目があれば、理由を伝えて最大2回まで書き直させます。
    """
    sample = [t for t in texts if len(t) <= 8][:12]
    base = "\n".join([
        f"キャラクター: {character[:400]}",
        f"スタンプのセリフの例: {' / '.join(sample)}",
        f"セット番号: {volume}（2以上ならタイトルの最後に番号を付ける）" if volume > 1 else "",
        "LINE Creators Market に登録するタイトルと説明文を作ってください。",
        "- title_en: 英語のタイトル。半角英数字と記号のみ、40文字以内",
        "- desc_en: 英語の説明文。半角英数字と記号のみ、160文字以内",
        "- title_ja: 日本語のタイトル。全角20文字以内",
        "- desc_ja: 日本語の説明文。全角80文字以内",
        "- 絵文字・URL・「LINE」という言葉・発売日などの告知は入れない",
        '出力形式: {"title_en":"...","desc_en":"...","title_ja":"...","desc_ja":"..."}',
    ])
    user = "\n".join(x for x in base.split("\n") if x)
    keys = ("title_en", "desc_en", "title_ja", "desc_ja")
    result: dict = {}
    issues: dict = {}
    for _ in range(3):
        data = llm.chat_json(LISTING_SYSTEM, user, temperature=0.8)
        if not isinstance(data, dict):
            continue
        for k in keys:
            if k in result:
                continue                  # 前の回でルールに合った項目は確定（書き直しで変えない）
            text = _clean(data.get(k), 400)
            if text and not listing_mod.check_field(k, text):
                result[k] = text
            elif text:
                issues[k] = (text, listing_mod.check_field(k, text))
        missing = [k for k in keys if k not in result]
        if not missing:
            break
        fixes = [f"- {k}: 「{issues[k][0]}」は {' / '.join(issues[k][1])}" for k in missing if k in issues]
        user = base + "\n\n次の項目がルールに合いませんでした。直して、全項目をもう一度出力してください。\n" + "\n".join(fixes)
    for k in keys:  # それでも合わない項目は、文の区切りで切り詰めて返します（画面で警告が出ます）
        if k not in result and k in issues:
            result[k] = listing_mod._fit(issues[k][0], listing_mod.LIMITS[k])
    return {k: result.get(k, "") for k in keys}


# ---------------------------------------------------------------------------
# 4. キャラクターの説明文を整える
# ---------------------------------------------------------------------------
POLISH_SYSTEM = (
    "あなたはイラストの発注書を書く担当です。画像生成AIに渡すキャラクター設定を、"
    "日本語で、絵にしやすい具体的な文章に整えます。"
)


def polish_character(llm: LocalLLM, text: str) -> str:
    """キャラクターの説明文を、絵にしやすい具体的な文章に書き直します。"""
    user = "\n".join([
        "次のキャラクター設定を、画像生成AIが同じキャラを安定して描けるよう具体的に書き直してください。",
        "- 形・色・体型・服装・特徴を具体的に。元の設定と矛盾することは足さない",
        "- 1文ずつ改行し、全体で12行以内",
        "- 背景・文字・セリフ・ポーズの指示は書かない（別に指定するため）",
        "- 1行目は「何のキャラクターか」（例: ペンギンのキャラクター。）を必ず書く",
        "- 書き直した設定の本文だけを出力する（前置きや説明、括弧書きの補足は不要）",
        "",
        text.strip(),
    ])
    out = llm.chat(POLISH_SYSTEM, user, temperature=0.5)
    out = re.sub(r"```\w*", "", out).strip()
    lines = [re.sub(r"^\s*(?:[-*・•]|\d+[.)．])\s*", "", ln).strip() for ln in out.splitlines()]
    lines = [ln for ln in lines if ln and not re.match(r"^[（(].*[）)]$", ln)]  # 括弧だけの補足は捨てる
    if not lines:
        raise LLMError("書き直した説明文が空でした")
    # 何のキャラクターか（元の1行目）が抜けたら、先頭に戻します。ここが抜けると別のキャラが描かれます。
    head = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    subject = re.sub(r"(のキャラクター)?[。．.]$", "", head)
    if head and subject and not any(subject in ln for ln in lines[:2]):
        lines.insert(0, head)
    return "\n".join(lines[:12])[:500]
