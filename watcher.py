"""マイクラ新情報ウォッチャー

Mojang 公式の更新データを見張り、新しい情報が出たら Discord に通知する。
- Java版: スナップショット / プレリリース / 正式版（Minecraft Launcher のパッチノート）
- 統合版: ベータ & プレビュー / 正式版（feedback.minecraft.net の変更履歴）
- minecraft.net の記事（ゲームドロップ名の発表や Minecraft Live など）

変更履歴は「新要素」「仕様変更」「修正・技術」に仕分けし、新要素がある回を目立たせる。
ANTHROPIC_API_KEY があれば、Claude が日本語要約と動画ネタを付ける（無くても動く）。

環境変数:
  DISCORD_WEBHOOK_URL        通知先（必須。無ければ画面に表示するだけ）
  DISCORD_WEBHOOK_URL_MINOR  修正のみの回・普通の記事の通知先（任意。無ければ上と同じ）
  NOTIFY_MINOR               "false" にすると修正のみの回は通知しない
  ANTHROPIC_API_KEY          日本語要約を付ける（任意）
  SEND_TEST                  "true" で最新の各1件をテスト通知（記録は変えない）
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import calendar_builder

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
UA = "Mozilla/5.0 (compatible; mc-update-watcher/1.0; RSS reader)"

JAVA_INDEX = "https://launchercontent.mojang.com/v2/javaPatchNotes.json"
JAVA_BASE = "https://launchercontent.mojang.com/v2/"
ZENDESK = "https://feedback.minecraft.net/api/v2/help_center/en-us/sections/{sid}/articles.json?sort_by=created_at&sort_order=desc&per_page={n}"
SECTION_BEDROCK_BETA = 360001185332
SECTION_RELEASE = 360001186971
NEWS_RSS = "https://www.minecraft.net/en-us/feeds/community-content/rss"

COLOR_NEW = 0x2ECC71      # 緑: 新要素あり
COLOR_RELEASE = 0xF1C40F  # 金: 正式リリース
COLOR_CHANGE = 0x3498DB   # 青: 仕様変更あり
COLOR_MINOR = 0x95A5A6    # 灰: 修正のみ
COLOR_NEWS = 0xE67E22     # 橙: 公式発表


# ---------------------------------------------------------------- 取得

def fetch(url: str, timeout: int = 30) -> bytes:
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"取得失敗 {url}: {last}")


def fetch_json(url: str):
    return json.loads(fetch(url).decode("utf-8"))


def iso(dt: str) -> str:
    """いろいろな日付表記を UTC の ISO 文字列にそろえる"""
    dt = dt.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dt):
        return dt + "T00:00:00Z"
    d = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(title: str) -> str:
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ---------------------------------------------------------------- 変更履歴の仕分け

class ChangelogParser(HTMLParser):
    """見出しの階層つきで <li> を取り出す（閉じタグの無い <li> にも対応）"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headings: dict[int, str] = {}
        self.h_level: int | None = None
        self.h_buf: list[str] = []
        self.depth = 0
        self.li_stack: list[tuple[int, list[str]]] = []
        self.items: list[tuple[list[str], str]] = []

    def _path(self):
        return [self.headings[k] for k in sorted(self.headings)]

    def _flush_top(self):
        if not self.li_stack:
            return
        _, buf = self.li_stack.pop()
        text = re.sub(r"\s+", " ", "".join(buf)).strip()
        if text:
            self.items.append((self._path(), text))

    def handle_starttag(self, tag, attrs):
        if re.fullmatch(r"h[1-6]", tag):
            self.h_level = int(tag[1])
            self.h_buf = []
        elif tag in ("ul", "ol"):
            self.depth += 1
        elif tag == "li":
            if self.li_stack and self.li_stack[-1][0] == self.depth:
                self._flush_top()
            self.li_stack.append((self.depth, []))

    def handle_endtag(self, tag):
        if re.fullmatch(r"h[1-6]", tag) and self.h_level is not None:
            text = re.sub(r"\s+", " ", "".join(self.h_buf)).strip()
            lvl = self.h_level
            for k in list(self.headings):
                if k >= lvl:
                    del self.headings[k]
            if text:
                self.headings[lvl] = text
            self.h_level = None
        elif tag == "li":
            self._flush_top()
        elif tag in ("ul", "ol"):
            while self.li_stack and self.li_stack[-1][0] >= self.depth:
                self._flush_top()
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.h_level is not None:
            self.h_buf.append(data)
        elif self.li_stack:
            self.li_stack[-1][1].append(data)


RE_SKIP = re.compile(r"information on|known issues?|^minecraft preview and beta", re.I)
RE_TECH = re.compile(
    r"technical|\bapi\b|add-?ons?\b|data ?pack|resource ?pack|network protocol|\beditor\b|script|shader|"
    r"component|packet|server propert|dedicated server|\btags?\b|registr|molang|ore ui|"
    r"stability and performance|\bjson\b|commands?\b",
    re.I,
)
RE_NEW_HEAD = re.compile(r"new features?|experimental features?|drop \d+ of \d{4}|^features?$|features$", re.I)
RE_FIX_HEAD = re.compile(r"\bfix", re.I)
RE_FIX_ITEM = re.compile(r"^(fixed|fixes|fix)\b|^MC(PE)?-\d+|\((MC|MCPE)-\d+\)|\bnow correctly\b", re.I)
RE_NEW_ITEM = re.compile(r"^(added|adds|new|introduced|introducing)\b", re.I)
RE_TRIVIAL = re.compile(r"language support|creative inventory|translation|narration|tooltip|capitali[sz]ed", re.I)
RE_CHANGE_ITEM = re.compile(
    r"^(changed|removed|updated|adjusted|reduced|increased|tweaked|moved|renamed|improved)\b|\bno longer\b", re.I)


def analyze(body_html: str) -> dict:
    p = ChangelogParser()
    p.feed(body_html or "")
    p.close()
    while p.li_stack:
        p._flush_top()
    out = {"new": [], "change": [], "fix": 0, "tech": 0, "experimental": False}
    for path, text in p.items:
        path_str = " > ".join(path)
        if any(RE_SKIP.search(h) for h in path):
            continue
        if "experimental" in path_str.lower():
            out["experimental"] = True
        if RE_FIX_ITEM.search(text):
            out["fix"] += 1
            continue
        if RE_TECH.search(path_str):
            out["tech"] += 1
            continue
        heads_new = any(RE_NEW_HEAD.search(h) and "bug" not in h.lower() for h in path)
        heads_fix_only = any(RE_FIX_HEAD.search(h) and "feature" not in h.lower() for h in path)
        if heads_fix_only:
            out["fix"] += 1
        elif RE_TRIVIAL.search(text) or RE_CHANGE_ITEM.search(text):
            out["change"].append(text)
        elif heads_new or RE_NEW_ITEM.search(text):
            out["new"].append(text)
        else:
            out["change"].append(text)
    if out["new"]:
        out["verdict"] = "new"
    elif out["change"]:
        out["verdict"] = "change"
    else:
        out["verdict"] = "minor"
    return out


def html_to_text(body_html: str, limit: int = 60000) -> str:
    t = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", body_html or "", flags=re.I)
    t = re.sub(r"<li[^>]*>", "\n- ", t, flags=re.I)
    t = re.sub(r"<h(\d)[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t[:limit]


# ---------------------------------------------------------------- 情報源

def src_java():
    data = fetch_json(JAVA_INDEX)
    out = []
    for e in data.get("entries", [])[:15]:
        img = e.get("image", {}).get("url", "")
        title = e.get("title", e.get("version", ""))
        version = e.get("version", "")
        # 26.3.1 のような3桁は修正版（ホットフィックス）。1.21.11 などの旧表記は正式版
        is_release = e.get("type") == "release" and not re.fullmatch(r"\d{2}\.\d+\.\d+", version)
        kind = "Java版 正式リリース" if is_release else "Java版 修正版" if e.get("type") == "release" else (
            "Java版 プレリリース" if re.search(r"-(pre|rc)", version) else "Java版 スナップショット")
        out.append({
            "source": "java",
            "id": e["id"],
            "title": title,
            "date": iso(e["date"]),
            "kind": kind,
            "is_release": is_release,
            "url": "https://www.minecraft.net/en-us/article/" + slugify(title),
            "image": ("https://launchercontent.mojang.com" + img) if img.startswith("/") else img,
            "short": e.get("shortText", ""),
            "_content": JAVA_BASE + e["contentPath"],
        })
    return out


def load_java_body(item):
    return fetch_json(item["_content"]).get("body", "")


def _zendesk(section, n, edition_filter=None, kind="", is_release=False, source=""):
    data = fetch_json(ZENDESK.format(sid=section, n=n))
    out = []
    for a in data.get("articles", []):
        if a.get("draft"):
            continue
        if edition_filter and not re.search(edition_filter, a["title"], re.I):
            continue
        out.append({
            "source": source,
            "id": str(a["id"]),
            "title": a["title"],
            "date": iso(a["created_at"]),
            "kind": kind if not re.search(r"hotfix", a["title"], re.I) else "統合版 修正版",
            "is_release": is_release and not re.search(r"hotfix", a["title"], re.I),
            "url": a["html_url"],
            "image": "",
            "short": "",
            "_body": a.get("body", ""),
        })
    return out


def src_bedrock_beta():
    return _zendesk(SECTION_BEDROCK_BETA, 10, kind="統合版 ベータ/プレビュー", source="bedrock_beta")


def src_release_changelogs():
    # 統合版の正式版（Java版の正式版はランチャーのパッチノートで拾う）
    return _zendesk(SECTION_RELEASE, 10, edition_filter=r"bedrock", kind="統合版 正式リリース",
                    is_release=True, source="bedrock_release")


RE_NEWS_DUP = re.compile(r"snapshot|pre-?release|release candidate|preview|beta|changelog", re.I)
RE_NEWS_SKIP = re.compile(
    r"marketplace|let.?s play|parents|realms|merch|spotlight|skin|watch time|charity|education|"
    r"legends|texture pack|add-on|minecoin|lego|movie|block party|creator|pass\b|sale\b|deal",
    re.I,
)
RE_NEWS_MAJOR = re.compile(
    r"game drop|\bdrop\b|minecraft live|reveal|first look|sneak peek|new features?|coming to|"
    r"\bmob\b|biome|dimension|vote|announc|update|release date|out now|available now|trailer",
    re.I,
)


def src_news():
    raw = fetch(NEWS_RSS)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif len(raw) > 1 and raw[1] == 0:
        text = raw.decode("utf-16-le")
    else:
        text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    root = ET.fromstring(text)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = (it.findtext("description") or "").strip()
        link_el = it.find("{http://www.w3.org/2005/Atom}link")
        link = link_el.get("href") if link_el is not None else (it.findtext("link") or "")
        pub = it.findtext("pubDate") or ""
        try:
            d = datetime.strptime(pub.replace(" Z", " +0000"), "%a, %d %b %Y %H:%M:%S %z")
            date = d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        blob = f"{title} {desc}"
        if RE_NEWS_DUP.search(title):
            level = "dup"
        elif RE_NEWS_SKIP.search(blob):
            level = "skip"
        elif RE_NEWS_MAJOR.search(blob):
            level = "major"
        else:
            level = "minor"
        out.append({
            "source": "news",
            "id": link or title,
            "title": title,
            "date": date,
            "kind": "公式記事",
            "is_release": False,
            "url": link,
            "image": "",
            "short": desc,
            "level": level,
        })
    return out


SOURCES = {
    "java": src_java,
    "bedrock_beta": src_bedrock_beta,
    "bedrock_release": src_release_changelogs,
    "news": src_news,
}


# ---------------------------------------------------------------- 日本語要約（任意）

SUMMARY_SYSTEM = (
    "あなたはマインクラフト解説動画の制作アシスタントです。"
    "渡された公式の更新内容（英語）を読み、日本語で次の形式だけを出力してください。見出し記号は使わず、Discord で読みやすい短い文にします。\n"
    "【要約】2〜3行\n"
    "【新要素】新しく追加されたもの（ブロック・アイテム・モブ・仕組み）を箇条書き「・」で最大8個。公式の英語名をかっこで添える\n"
    "【主な変更】動画で触れる価値のある仕様変更を最大4個（無ければ「なし」）\n"
    "【動画ネタ】この更新で作れる動画のタイトル案を3つ（検証・解説・建築など切り口を変える）\n"
    "【注意】実験的機能が必要か、統合版とJava版で違いがあるかなど撮影前に知るべきこと（無ければ省略）\n"
    "全体で900文字以内。原文に無いことは書かない。"
)


def summarize_ja(item, body_text: str) -> str | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        print("anthropic パッケージが無いため要約を省略")
        return None
    try:
        client = anthropic.Anthropic()
        resp = client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=SUMMARY_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"種類: {item['kind']}\nタイトル: {item['title']}\n\n{body_text}",
            }],
        )
        if resp.stop_reason == "refusal":
            print("要約: refusal")
            return None
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text[:3800] or None
    except anthropic.RateLimitError:
        print("要約: レート制限のため省略")
    except anthropic.APIStatusError as e:
        print(f"要約: API エラー {e.status_code} {e.message}")
    except anthropic.APIConnectionError:
        print("要約: 接続エラー")
    except Exception as e:  # noqa: BLE001  要約が失敗しても通知は必ず送る
        print(f"要約: 失敗 {type(e).__name__}: {e}")
    return None


# ---------------------------------------------------------------- Discord

def bullets(lines, limit_items, limit_chars=1000):
    out, n = [], 0
    for t in lines[:limit_items]:
        t = t if len(t) <= 180 else t[:177] + "…"
        line = f"• {t}"
        if n + len(line) + 1 > limit_chars:
            break
        out.append(line)
        n += len(line) + 1
    rest = len(lines) - len(out)
    if rest > 0:
        out.append(f"…ほか {rest} 件")
    return "\n".join(out)


def post_discord(payload: dict, minor: bool = False):
    url = os.environ.get("DISCORD_WEBHOOK_URL_MINOR") if minor else None
    url = url or os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("[DRY RUN] Discord 送信内容:\n" + json.dumps(payload, ensure_ascii=False, indent=2))
        return
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30):
                pass
            time.sleep(1.2)
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                time.sleep(wait + 0.5)
                continue
            raise


def jst(iso_s: str) -> str:
    d = datetime.fromisoformat(iso_s.replace("Z", "+00:00")).astimezone(
        timezone.utc).timestamp()
    return f"<t:{int(d)}:f>"


def build_changelog_message(item, a: dict, summary: str | None):
    edition = "Java版" if item["source"] == "java" else "統合版"
    if item["is_release"]:
        badge, color, minor = "🎉 正式リリース", COLOR_RELEASE, False
    elif a["verdict"] == "new":
        badge, color, minor = "🆕 新要素あり", COLOR_NEW, False
    elif a["verdict"] == "change":
        badge, color, minor = "🔄 仕様変更あり", COLOR_CHANGE, False
    else:
        badge, color, minor = "🔧 修正のみ", COLOR_MINOR, True
    exp = "（実験的機能あり）" if a["experimental"] else ""
    counts = f"新要素 {len(a['new'])} / 変更 {len(a['change'])} / 修正 {a['fix']} / 技術 {a['tech']}"
    content = f"{badge}【{edition}】{item['title']}{exp}"
    embed = {
        "title": item["title"][:250],
        "url": item["url"],
        "color": color,
        "description": (summary or item.get("short") or "")[:3900],
        "fields": [],
        "footer": {"text": f"{item['kind']} ・ {counts}"},
        "timestamp": item["date"],
    }
    if not summary:
        if a["new"]:
            embed["fields"].append({"name": "🆕 新要素（原文）", "value": bullets(a["new"], 12)})
        if a["change"] and not minor:
            embed["fields"].append({"name": "🔄 主な変更（原文）", "value": bullets(a["change"], 6, 800)})
    embed["fields"].append({"name": "公開", "value": jst(item["date"]), "inline": True})
    if item.get("image"):
        embed["thumbnail"] = {"url": item["image"]}
    return {"content": content[:1900], "embeds": [embed]}, minor


def build_news_message(item):
    major = item["level"] == "major"
    content = ("📢 公式発表: " if major else "📰 公式記事: ") + item["title"]
    embed = {
        "title": item["title"][:250],
        "url": item["url"],
        "color": COLOR_NEWS if major else COLOR_MINOR,
        "description": item["short"][:3900],
        "footer": {"text": "minecraft.net"},
        "timestamp": item["date"],
    }
    return {"content": content[:1900], "embeds": [embed]}, not major


# ---------------------------------------------------------------- 本体

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": {}, "history": []}


def save_state(state):
    state["seen"] = {k: v[-300:] for k, v in state["seen"].items()}
    state["history"] = sorted(state["history"], key=lambda h: h["date"])[-400:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def history_entry(item, verdict):
    return {k: item[k] for k in ("source", "id", "title", "date", "kind", "url")} | {"verdict": verdict}


def process_item(item, notify=True):
    """1件を仕分けして通知。戻り値は verdict"""
    if item["source"] == "news":
        if item["level"] in ("dup", "skip"):
            return item["level"]
        msg, minor = build_news_message(item)
        if notify and (not minor or os.environ.get("NOTIFY_MINOR", "true").lower() != "false"):
            post_discord(msg, minor)
        return item["level"]

    body = load_java_body(item) if item["source"] == "java" else item.get("_body", "")
    a = analyze(body)
    verdict = "release" if item["is_release"] else a["verdict"]
    summary = None
    if notify and verdict in ("release", "new", "change"):
        summary = summarize_ja(item, html_to_text(body))
    msg, minor = build_changelog_message(item, a, summary)
    if notify and (not minor or os.environ.get("NOTIFY_MINOR", "true").lower() != "false"):
        post_discord(msg, minor)
    return verdict


def main():
    state = load_state()
    test = os.environ.get("SEND_TEST", "").lower() == "true"
    errors = []
    first_run_sources = []

    for name, fn in SOURCES.items():
        try:
            items = fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            print(f"[警告] {name} の取得に失敗: {e}")
            continue

        if test:
            for it in [i for i in items if i.get("level") not in ("dup", "skip")][:1]:
                print(f"[TEST] {name}: {it['title']}")
                process_item(it, notify=True)
            continue

        seen = state["seen"].get(name)
        if seen is None:
            # 初回は過去分を通知せず記録だけする
            state["seen"][name] = [i["id"] for i in items]
            for it in items:
                if it.get("level") in ("dup", "skip"):
                    continue
                v = it.get("level") or ("release" if it["is_release"] else "past")
                state["history"].append(history_entry(it, v))
            first_run_sources.append(name)
            continue

        new_items = [i for i in items if i["id"] not in seen]
        for it in sorted(new_items, key=lambda i: i["date"]):
            print(f"[新着] {name}: {it['title']}")
            try:
                verdict = process_item(it, notify=True)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}/{it['title']}: {e}")
                print(f"[警告] 処理失敗: {e}")
                continue  # 記録しないので次回もう一度試す
            seen.append(it["id"])
            if verdict not in ("dup", "skip"):
                state["history"].append(history_entry(it, verdict))

    if first_run_sources and not test:
        post_discord({"content": "✅ マイクラ新情報ウォッチを開始しました。次の新着から通知します。\n見張り中: "
                      + "、".join({"java": "Java版", "bedrock_beta": "統合版ベータ/プレビュー",
                                  "bedrock_release": "統合版 正式版", "news": "minecraft.net 記事"}[s]
                                 for s in first_run_sources)})

    if not test:
        save_state(state)
        calendar_builder.build(state["history"], ROOT / "forecast.json", ROOT / "calendar.ics")

    if errors:
        print("エラー:\n  " + "\n  ".join(errors))
        # 情報源の一時的な不調でワークフローを失敗扱いにしない（記録は保存済み）


if __name__ == "__main__":
    sys.exit(main())
