#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ホロライブ オリ曲アーカイブ ─ 週次更新

GitHub Actions の中で走る。index.html を読み、新曲を足して、書き戻す。
標準ライブラリだけで動く（pip install なし）。

守っていること:
  * そうたさんが手で決めた c（コール判定）の値には絶対に触らない
  * c=1（推定）を新しく作らない。新曲は必ず c=0 で入る
  * 歌詞は1文字も保存しない。歌ネットの「歌い出し」は読み捨てる
  * ホロライブ以外のチャンネルに飛ぶ曲は入れない
  * 知らないチャンネルが出てきたら、入れずに「要確認」として報告する
  * --apply を付けない限り、ファイルは書き換えない

使い方:
  python3 tools/update.py            # 下見だけ（何も書き換えない）
  python3 tools/update.py --apply    # 実際に index.html を書き換える
"""

import argparse
import html as htmllib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
}

LIST_URL = "https://db.holoplayer.jp/song/list/"
SETLIST_INDEX = "https://db.holoplayer.jp/feature/setlist/"
SETLIST_BASE = "https://db.holoplayer.jp"
OEMBED = "https://www.youtube.com/oembed?url=%s&format=json"
UTANET_SEARCH = "https://www.uta-net.com/search/?Aselect=2&Bselect=3&Keyword=%s"
ITUNES = ("https://itunes.apple.com/search?term=%s&country=jp"
          "&media=music&entity=song&limit=5")

import os

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNELS_OK = os.path.join(HERE, "channels-ok.txt")
MEMBERS_TXT = os.path.join(HERE, "members.txt")
UNITS_TXT = os.path.join(HERE, "units.txt")

MAX_UTANET_NEW = 40      # 新曲の歌詞リンク探し
MAX_UTANET_FILL = 35     # 既存曲の歌詞リンク埋め戻し
MAX_ITUNES = 25          # Apple Music（iTunesのAPIは毎分20回程度が目安）
PAUSE = 1.0              # 歌ネットへの間隔
PAUSE_APPLE = 3.0        # Apple への間隔。毎分20回を超えないように
MAX_ERRORS = 5           # 続けてこの回数失敗したら、その相手は今回あきらめる


# ───────────────────────────── 下請け ─────────────────────────────

def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or "utf-8"
        return raw.decode(enc, "replace")


def fetch_json(url, timeout=30):
    return json.loads(fetch(url, timeout))


def norm(s):
    """比較用に曲名をそろえる。全角半角・大文字小文字・飾り記号を無視する。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = re.sub(r"[\s　]+", "", s)
    s = re.sub(r"[!！?？★☆♪♡♥〜~\-–—_/／\\|｜:：;；,，.。、·・'\"“”'’`()（）\[\]【】"
               r"<>〈〉《》«»⇄⇔↔→←+＋*＊=＝#＃&＆@＠%％$￥^〜]", "", s)
    return s


def _why(e):
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %s %s" % (e.code, e.reason)
    if isinstance(e, urllib.error.URLError):
        return "URLError %s" % (e.reason,)
    return "%s: %s" % (type(e).__name__, e)


def read_config_lines(path):
    """# から始まる行と空行を落として返す。ファイルが無ければ空。"""
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f
                    if ln.strip() and not ln.lstrip().startswith("#")]
    except FileNotFoundError:
        return []


def read_units():
    """ユニット名 → メンバー一覧。照合は NFKC でそろえた名前で行う。"""
    out = {}
    for ln in read_config_lines(UNITS_TXT):
        if "=" not in ln:
            continue
        name, rest = ln.split("=", 1)
        mem = [x.strip() for x in rest.split(",") if x.strip()]
        if name.strip() and mem:
            out[unicodedata.normalize("NFKC", name.strip())] = mem
    return out


def strip_tags(s):
    s = re.sub(r"(?s)<[^>]*>", "", s)
    return htmllib.unescape(s).strip()


def base_title(t):
    """
    「○○ (アキ・ローゼンタール SOLO ver.)」→「○○」
    「○○ – TeddyLoid Neon Noir Remix」   →「○○」
    「○○ Lo-fi ver.」                    →「○○」
    別バージョンの歌詞は本体と同じなので、歌詞ページを探すときの二の矢に使う。
    元の曲名と同じになるなら None を返す。
    """
    b = t
    b = re.sub(r"\s*[(（][^()（）]*[)）]\s*$", "", b)
    b = re.sub(r"\s+[-–—]\s+.*$", "", b)
    b = re.sub(r"(?i)\s+[\w'-]*\s*ver\.?\s*$", "", b)
    b = re.sub(r"(?i)\s+remix\s*$", "", b)
    b = b.strip()
    return b if b and b != t.strip() else None


# ──────────────────── index.html から DATA を取り出す ────────────────────

def read_data(path):
    src = open(path, encoding="utf-8").read()
    i = src.find("const DATA = ")
    if i < 0:
        raise SystemExit("index.html の中に 'const DATA = ' が見つからない")
    j = src.find("{", i)
    depth, k, instr, esc = 0, j, False, False
    while k < len(src):
        ch = src[k]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        else:
            if ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
        k += 1
    blob = src[j:k]
    return src, j, k, json.loads(blob)


def write_data(path, src, j, k, data):
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    open(path, "w", encoding="utf-8").write(src[:j] + blob + src[k:])


# ────────────── 今あるデータから「知っていること」を学ぶ ──────────────

def learn(songs):
    """
    手打ちしないための導出。
      known_ch   : これまで使われたチャンネル名（ここに無いものは要確認）
      mem_branch : メンバー名 → 支部コード
      mem_scope  : メンバー名 → "jp" / "intl"
    """
    known_ch, mem_branch, mem_scope = set(), {}, {}
    for s in songs:
        if s.get("ch"):
            known_ch.add(s["ch"])
        mem = s.get("m") or []
        if len(mem) == 1:
            b = s.get("b") or []
            if len(b) == 1:
                mem_branch.setdefault(mem[0], b[0])
            if s.get("s"):
                mem_scope.setdefault(mem[0], s["s"])
    # 複数人の曲からも、まだ知らないメンバーを拾っておく
    for s in songs:
        mem, b = s.get("m") or [], s.get("b") or []
        if len(b) == 1:
            for m in mem:
                mem_branch.setdefault(m, b[0])
        if s.get("s"):
            for m in mem:
                mem_scope.setdefault(m, s["s"])

    # 手で許可したチャンネルを足す
    extra_ch = read_config_lines(CHANNELS_OK)
    known_ch.update(extra_ch)

    # データにまだ1曲も無い新メンバーを足す
    extra_mem = 0
    mem_info = {}          # 名前 → {"gen":…, "ord":…}
    for ln in read_config_lines(MEMBERS_TXT):
        parts = [x.strip() for x in ln.split(",")]
        if len(parts) >= 2 and parts[0]:
            mem_branch[parts[0]] = parts[1]
            mem_scope[parts[0]] = parts[2] if len(parts) > 2 and parts[2] else "jp"
            info = {}
            if len(parts) > 3 and parts[3]:
                info["gen"] = parts[3]
            if len(parts) > 4 and parts[4].lstrip("-").isdigit():
                info["ord"] = int(parts[4])
            mem_info[parts[0]] = info
            extra_mem += 1

    return known_ch, mem_branch, mem_scope, len(extra_ch), extra_mem, mem_info


# ──────────────────── ホロ曲DB の一覧を読む ────────────────────

def parse_list(html):
    """最新50曲ぶんの行を読む。歌唱者は省略のない sl-artist 側を使う。"""
    rows = []
    # sl-row ごとに切る
    chunks = re.split(r'<div class="sl-row"', html)[1:]
    for ch in chunks:
        title_m = re.search(r'<a class="sl-title-link"[^>]*>(.*?)</a>', ch, re.S)
        vid_m = re.search(r'youtu\.be/([A-Za-z0-9_-]{11})', ch) or \
                re.search(r'img\.youtube\.com/vi/([A-Za-z0-9_-]{11})/', ch)
        date_m = re.search(r'<div class="sl-c sl-date">([\d/]+)</div>', ch)
        song_m = re.search(r'href="/song/(\d+)/"', ch)
        if not (title_m and vid_m):
            continue

        # 歌唱者: sl-artist の中のリンク文字列（「, 」区切りで全員入っている）
        art_m = re.search(r'<div class="sl-c sl-artist">(.*?)</div>', ch, re.S)
        members = []
        if art_m:
            txt = strip_tags(art_m.group(1))
            members = [p.strip() for p in txt.split(",") if p.strip()]

        d = date_m.group(1).replace("/", "-") if date_m else ""
        if d:
            y, mo, da = (d.split("-") + ["", ""])[:3]
            if y and mo and da:
                d = "%s-%02d-%02d" % (y, int(mo), int(da))

        rows.append({
            "t": strip_tags(title_m.group(1)),
            "v": vid_m.group(1),
            "d": d,
            "m": members,
            "song_id": song_m.group(1) if song_m else "",
        })
    return rows


# ──────────────────── YouTube のチャンネルを確かめる ────────────────────

def channel_of(vid):
    url = OEMBED % urllib.parse.quote(
        "https://www.youtube.com/watch?v=" + vid, safe="")
    try:
        return fetch_json(url).get("author_name", "")
    except Exception as e:
        return "!!" + type(e).__name__


# ──────────────────── 歌ネットで曲ページIDを探す ────────────────────

def utanet_once(title, members):
    """
    曲名で1回検索する。戻り値は ("ok", id) / ("none", None) / ("error", None)。
    「見つからなかった」と「取りに行けなかった」を区別するのが肝。
    取りに行けなかっただけの曲に「無い」と印を付けると、二度と調べ直さなくなる。
    「歌い出し」は読まずに捨てる。曲名・歌手名・IDだけを使う。
    """
    url = UTANET_SEARCH % urllib.parse.quote(title)
    try:
        page = fetch(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # 歌ネットは検索結果がゼロのとき404を返すらしい。
            # つまりこれは障害ではなく「その曲名では当たらない」という答え。
            # 障害として数えない。全体を打ち切る理由にもしない。
            return "404", None
        if e.code in (429, 503):
            time.sleep(6.0)        # 混んでいるだけかもしれないので一度だけ待つ
            try:
                page = fetch(url)
            except Exception as e2:
                return "error", "%s → やり直しても %s" % (_why(e), _why(e2))
        else:
            return "error", "%s / %s" % (_why(e), url)
    except Exception as e:
        return "error", "%s / %s" % (_why(e), url)

    # 本当の検索結果は tbody.songlist-table-body の中だけ。
    # 横のランキング枠を拾わないよう、ここで切り出す。
    body = re.search(r'(?s)<tbody class="songlist-table-body">(.*?)</tbody>', page)
    if not body:
        # 検索結果の枠そのものが無い。こういうページは
        #   ・拒否された / 何かのお知らせページを返された
        #   ・ページの作りが変わった
        # のどちらかで、「この曲は歌ネットに無い」という意味ではない。
        # ここで "none" を返すと、読めなかっただけの曲に永久の印が付く。
        t = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
        return "error", ("検索結果の枠が無い / %d文字 / title=%s"
                         % (len(page), (strip_tags(t.group(1))[:60] if t else "なし")))
    body = body.group(1)

    want_t = norm(title)
    want_m = {norm(m) for m in members}

    cands = []
    for tr in re.split(r"<tr\b", body)[1:]:
        sid = re.search(r'href="/song/(\d+)/"', tr)
        tt = re.search(r'<span class="[^"]*songlist-title[^"]*">(.*?)</span>', tr, re.S)
        if not (sid and tt):
            continue
        if norm(strip_tags(tt.group(1))) != want_t:
            continue
        artists = {norm(strip_tags(a)) for a in
                   re.findall(r'<a href="/artist/\d+/"[^>]*>(.*?)</a>', tr, re.S)}
        cands.append((sid.group(1), artists))
        # ここで tr の残り（歌い出しを含む）は使わずに捨てる

    if not cands:
        return "none", None
    if len(cands) == 1:
        return "ok", cands[0][0]
    for sid, artists in cands:        # 歌手名が一致するものを選ぶ
        if artists & want_m:
            return "ok", sid
    return "none", None                # 決められないなら付けない


def utanet_id(title, members):
    """
    曲名そのままで探し、当たらなければ別バージョンの飾りを落として、もう一度。
    「Everlasting knife Lo-fi ver.」が無くても「Everlasting knife」はある、
    という場合をここで拾う。別バージョンの歌詞は本体と同じなので正しい。
    """
    st, sid = utanet_once(title, members)
    if st in ("ok", "error"):
        return st, sid
    b = base_title(title)        # st は "none" か "404"
    if b:
        time.sleep(PAUSE)
        st2, sid2 = utanet_once(b, members)
        if st2 in ("ok", "error"):
            return st2, sid2
        return ("none", None) if "none" in (st, st2) else ("404", None)
    return ("none", None) if st == "none" else ("404", None)


# ──────────────────── Apple Music の配信日と直リンク ────────────────────

def itunes_lookup(title, members):
    """("ok", 配信日, 曲ID) / ("none", None, None) / ("error", None, None)"""
    who = members[0] if members else ""
    term = (title + " " + who).strip()
    try:
        res = fetch_json(ITUNES % urllib.parse.quote(term)).get("results", [])
    except urllib.error.HTTPError as e:
        return "error", "HTTP %s %s" % (e.code, e.reason), None
    except Exception as e:
        return "error", "%s: %s" % (type(e).__name__, e), None
    want = norm(title)
    for r in res:
        if norm(r.get("trackName", "")) != want:
            continue
        rel = (r.get("releaseDate") or "")[:10]
        tid = r.get("trackId")
        return "ok", (rel or None), (str(tid) if tid else None)
    return "none", None, None


# ──────────────────────────── 集計 ────────────────────────────

# ───────────────── フェスのセットリスト ─────────────────

def parse_setlist(html):
    """
    ホロ曲DBのセットリストページ1枚を読む。
    返すのは {"title":…, "stages":[{"name":…,"date":…,"songs":[…]}]}。
    歌詞は無い（曲名・歌った人・曲順・動画IDだけ）。
    """
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", html)

    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", body)
    title = strip_tags(m.group(1)) if m else ""
    title = re.sub(r"\s*セットリスト\s*$", "", title).strip()

    # ページの後半には「ほかの全体ライブ」の日付が並ぶ。混ざらないよう切る。
    cut = len(body)
    for mark in ('id="others"', 'id="sources"', ">出典<"):
        i = body.find(mark)
        if i > 0:
            cut = min(cut, i)
    main = body[:cut]

    # 公演の名前は、ページ上部の目次から取る（#stage-1 → 「hololive STAGE1」）
    names = {}
    for sid, label in re.findall(r'href="#(stage-\d+)"[^>]*>(.*?)</a>', body, re.S):
        names.setdefault(sid, strip_tags(label))

    # 公演の区切り
    marks = [(m.start(), m.group(1))
             for m in re.finditer(r'id="(stage-\d+)"', main)]
    if not marks:
        marks = [(0, "stage-1")]
    marks.append((len(main), None))

    stages = []
    for idx in range(len(marks) - 1):
        a, sid = marks[idx]
        b = marks[idx + 1][0]
        chunk = main[a:b]
        songs = []
        for li in re.findall(
                r'(?is)<li class="break-inside-avoid[^"]*">(.*?)</li>', chunk):
            t = re.search(r'(?is)<div class="text-sm"[^>]*>(.*?)</div>', li)
            if not t:
                continue
            name = strip_tags(t.group(1))
            if not name:
                continue
            no = re.search(r'(?is)<span class="w-6[^"]*">\s*(\d+)\s*</span>', li)
            vid = re.search(r'img\.youtube\.com/vi/([A-Za-z0-9_-]{11})/', li)
            who = re.search(r'(?is)<div class="text-xs text-ink/60"[^>]*>(.*?)</div>', li)
            rec = {"n": int(no.group(1)) if no else len(songs) + 1, "t": name}
            w = strip_tags(who.group(1)) if who else ""
            w = re.sub(r"\s*\n\s*", "、", w).strip()
            if w:
                rec["by"] = w
            if vid:
                rec["v"] = vid.group(1)
            songs.append(rec)
        if not songs:
            continue
        d = re.search(r"(20\d\d)/(\d{1,2})/(\d{1,2})", chunk)
        stages.append({
            "name": names.get(sid) or ("公演%d" % (len(stages) + 1)),
            "date": ("%s-%02d-%02d" % (d.group(1), int(d.group(2)), int(d.group(3))))
                    if d else "",
            "songs": songs,
        })
    return {"title": title, "stages": stages}


def update_setlists(data, loud=True):
    """
    フェスのセットリスト一覧を見に行き、まだ持っていないフェスを取り込む。
    一度取り込んだフェスは二度と取りに行かない（セトリは後から変わらない）。
    新しいフェスのページができれば、次の週に自動で入る。
    """
    fes = data.setdefault("fes", [])
    have = {f["key"] for f in fes}
    try:
        idx = fetch(SETLIST_INDEX)
    except Exception as e:
        print("  !! 一覧を取りに行けなかった（%s）。今回は何もしない" % _why(e))
        return []

    keys = []
    for href in dict.fromkeys(re.findall(r'href="(/feature/setlist/([^"/]+)/)"', idx)):
        path, key = href
        keys.append((key, SETLIST_BASE + path))

    added = []
    for key, url in keys:
        if key in have:
            continue
        if not key.endswith("-fes"):
            # 「Bloom,」などフェス以外。入れるかどうかは持ち主が決めること。
            if loud:
                print("  ・フェス以外なので見送り: %s" % key)
            continue
        try:
            page = fetch(url)
        except Exception as e:
            print("  !! %s を取りに行けなかった（%s）" % (key, _why(e)))
            continue
        time.sleep(PAUSE)
        got = parse_setlist(page)
        if not got["stages"]:
            print("  !! %s からセットリストを読み取れなかった。作りが変わった可能性" % key)
            continue
        fes.append({"key": key, "title": got["title"], "url": url,
                    "stages": got["stages"]})
        added.append(key)
        if loud:
            tot = sum(len(st["songs"]) for st in got["stages"])
            print("  ○ %s ─ %s" % (key, got["title"]))
            for st in got["stages"]:
                print("      %s  %s  %d曲 / 1曲目: %s"
                      % (st["date"] or "日付なし", st["name"],
                         len(st["songs"]), st["songs"][0]["t"]))
            print("      合計 %d曲" % tot)

    # 開催日の古い順に並べる
    fes.sort(key=lambda f: (f["stages"][0]["date"] if f["stages"] else ""))
    return added


def fold_solo_versions(data, loud=True):
    """
    「◯◯ SOLO ver.」のように、人の名前が入った別バージョンを、本体の曲に畳む。

    同じ曲が人数ぶん並ぶのを避けるため。畳んだ側は曲としては消えるが、
    その子だけが歌っている動画へのリンクは本体の sv に残すので、辿れる。

    畳む条件は「歌っている人の名前が、曲名の飾りの部分に入っていること」。
      神ツモ☆ドラドラ伝説 (白上フブキ SOLO ver.)  → 畳む（白上フブキが飾りにいる）
      Everlasting knife Lo-fi ver.             → 畳まない（人の名前が無い）
      スキスキDie… – TeddyLoid Neon Noir Remix   → 畳まない（同上）
    何度走らせても同じ結果になる（既に畳んだものは対象にならない）。
    """
    songs = data["songs"]
    by_title = {}
    for x in songs:
        by_title.setdefault(norm(x["t"]), []).append(x)

    folded = []
    for x in list(songs):
        mem = x.get("m") or []
        if len(mem) != 1:
            continue
        b = base_title(x["t"])
        if not b:
            continue
        who = norm(mem[0])
        if not who or who in norm(b) or who not in norm(x["t"]):
            continue               # 飾りの部分に名前が無いなら別の曲
        host = next((h for h in by_title.get(norm(b), []) if h is not x), None)
        if host is None:
            continue               # 本体が無いなら、そのまま1曲として残す
        sv = host.setdefault("sv", [])
        if not any(e.get("v") == x["v"] for e in sv):
            sv.append({"n": mem[0], "v": x["v"]})
        songs.remove(x)
        by_title[norm(x["t"])].remove(x)
        folded.append((x["t"], host["t"]))
        if loud:
            print("  畳んだ: %s → %s" % (x["t"], host["t"]))

    # 本体のメンバーの並び順に合わせて並べ替える
    for h in songs:
        if h.get("sv"):
            order = {n: i for i, n in enumerate(h.get("m") or [])}
            h["sv"].sort(key=lambda e: order.get(e["n"], 99))
    return folded


def fix_roster(data, mem_branch, mem_info, loud=True):
    """
    メンバー一覧（roster）を曲の実態に合わせる。
      ・各メンバーの曲数を数え直す
      ・曲に出てくるのに一覧にいない人を足す
    ここを忘れると、曲は増えたのにメンバーから探せない、という穴があく。
    """
    songs = data["songs"]
    roster = data.setdefault("roster", [])
    actual = {}
    for s in songs:
        for m in (s.get("m") or []):
            actual[m] = actual.get(m, 0) + 1

    fixed = 0
    for r in roster:
        n = actual.get(r["name"], 0)
        if r.get("count") != n:
            if loud:
                print("  曲数を直した: %-18s %s → %d" % (r["name"], r.get("count"), n))
            r["count"] = n
            fixed += 1

    have = {r["name"] for r in roster}
    # 支部ごとの一番後ろの並び順。新しい人の置き場所を決めるのに使う。
    tail = {}
    for r in roster:
        tail[r["branch"]] = max(tail.get(r["branch"], 0), r.get("ord", 0))

    added = []
    for name in sorted(actual, key=lambda n: -actual[n]):
        if name in have:
            continue
        br = mem_branch.get(name) or "JP"
        info = mem_info.get(name, {})
        if "ord" in info:
            o = info["ord"]
        else:
            o = tail.get(br, 0) + 1
            tail[br] = o
        roster.append({"name": name, "branch": br, "count": actual[name],
                       "gen": info.get("gen", "そのほか"), "ord": o})
        added.append(name)
        if loud:
            mark = "" if info else "（期生の指定が無いので「そのほか」に置いた）"
            print("  一覧に足した: %s / %s / %d曲 / 並び順%d %s"
                  % (name, br, actual[name], o, mark))

    # 並び順で整える。支部ごとの番号帯が重ならないので、これで支部も期生もまとまる。
    roster.sort(key=lambda r: (r.get("ord", 0), r["name"]))
    return fixed, added


def recount(data):
    songs = data["songs"]
    meta = data["meta"]
    dates = sorted(s["d"] for s in songs if s.get("d"))
    meta["total"] = len(songs)
    meta["range"] = [dates[0], dates[-1]] if dates else ["", ""]
    # ページ側のコメントのとおり、calls / calls2 / calls3 は「そのボタンで
    # 実際に出てくる曲数」＝累計。calls2 は c>=2 であって c==2 ではない。
    meta["calls"] = sum(1 for s in songs if (s.get("c") or 0) >= 1)
    meta["calls2"] = sum(1 for s in songs if (s.get("c") or 0) >= 2)
    meta["calls3"] = sum(1 for s in songs if (s.get("c") or 0) >= 3)
    meta["solos"] = sum(1 for s in songs if len(s.get("m") or []) == 1)
    meta["groups"] = sum(1 for s in songs if s.get("w"))
    meta["dated"] = sum(1 for s in songs if s.get("r"))
    meta["dated_apple"] = sum(1 for s in songs if s.get("rs") == "apple")
    meta["updated"] = time.strftime("%Y-%m-%d", time.gmtime())
    return meta


# ──────────────────────────── 本体 ────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="index.html")
    ap.add_argument("--apply", action="store_true",
                    help="付けると実際に書き換える。付けないと下見だけ")
    args = ap.parse_args()

    src, j, k, data = read_data(args.file)
    songs = data["songs"]
    n_before = len(songs)
    bytes_before = len(src)
    print("今のデータ: %d曲 / 最終更新 %s" % (len(songs), data["meta"].get("updated")))

    known_ch, mem_branch, mem_scope, n_ch, n_mem, mem_info = learn(songs)
    print("学習: チャンネル %d種 / メンバー %d人分の支部"
          % (len(known_ch), len(mem_branch)))
    print("      （うち手で許可したチャンネル %d件 / 手で足したメンバー %d人）"
          % (n_ch, n_mem))

    by_vid = {s["v"]: s for s in songs if s.get("v")}
    by_title = {}
    for s in songs:
        by_title.setdefault(norm(s["t"]), []).append(s)

    # ── 1. ホロ曲DB の最新50曲を見る ──
    print("\n[1] ホロ曲DB の最新一覧を読む")
    try:
        rows = parse_list(fetch(LIST_URL))
    except Exception as e:
        print("  !! 取得できなかった: %s: %s" % (type(e).__name__, e))
        return 1
    print("  %d行を読んだ" % len(rows))
    if not rows:
        print("  !! 0行。ページの作りが変わった可能性がある。中止する。")
        return 1

    known = [r for r in rows if r["v"] in by_vid]
    fresh = [r for r in rows if r["v"] not in by_vid]
    print("  うち既にあるもの: %d / 無いもの: %d" % (len(known), len(fresh)))
    print("  ※ 既にあるものが多いほど、この一覧が私たちの対象と合っている証拠")

    # ── 2. 無かった曲を1件ずつ確かめる ──
    print("\n[2] 無かった曲を確かめる")
    to_add, hold, dup = [], [], []
    for r in fresh:
        same = by_title.get(norm(r["t"]))
        if same:
            dup.append(r)
            print("  ・%s → 同じ曲名が既にある（動画IDだけ違う）。触らない" % r["t"])
            continue
        ch = channel_of(r["v"])
        time.sleep(0.4)
        if ch.startswith("!!"):
            hold.append((r, "YouTubeを確認できなかった(%s)" % ch[2:]))
            continue
        if ch not in known_ch:
            hold.append((r, "知らないチャンネル「%s」" % ch))
            continue
        r["ch"] = ch
        to_add.append(r)

    for r in to_add:
        print("  ○ 追加する: %s / %s / %s / %s"
              % (r["d"], r["t"], "・".join(r["m"]) or "?", r["ch"]))
    for r, why in hold:
        print("  △ 保留: %s / %s / %s ─ %s"
              % (r["d"], r["t"], "・".join(r["m"]) or "?", why))

    # ── 3. レコードを組み立てる ──
    units = read_units()
    if units:
        print("  （ユニット表を %d件 読んだ）" % len(units))

    added = []
    for r in to_add:
        mem = r["m"]
        # 歌唱者欄がユニット名1つだけなら、メンバーに開く
        if len(mem) == 1:
            key = unicodedata.normalize("NFKC", mem[0])
            if key in units:
                print("  ※ %s を %d人に開いた: %s"
                      % (mem[0], len(units[key]), "・".join(units[key])))
                mem = list(units[key])
        branches, scopes = [], []
        unknown_mem = []
        for m in mem:
            if m in mem_branch:
                if mem_branch[m] not in branches:
                    branches.append(mem_branch[m])
                scopes.append(mem_scope.get(m, "jp"))
            else:
                unknown_mem.append(m)
        if unknown_mem:
            print("  △ 知らないメンバー: %s（%s）→ 支部を空にしておく"
                  % ("・".join(unknown_mem), r["t"]))
        rec = {
            "d": r["d"], "t": r["t"], "a": "", "m": mem, "g": [],
            "b": branches, "s": (scopes[0] if scopes else "jp"),
            "v": r["v"], "n": len(mem),
            "c": 0,                       # 推定はしない。必ず0から始める
            "o": 1 if len(mem) == 1 else 0,
            "w": 0,
            "ch": r["ch"],
        }
        added.append(rec)

    # ── 4. 歌ネットの曲ページID ──
    print("\n[3] 歌ネットで歌詞ページを探す")
    got = filled = u_err = u_404 = 0
    streak = 0

    # 最初の1回だけ、歌ネットが何を返してくるかを確かめて出す。
    # 「見つからない」が続くとき、相手が拒否しているのか本当に無いのかを
    # 取り違えないため。
    # 3通り試して、404の原因を切り分ける。
    #   A 確実に当たる言葉        → これが通れば歌ネット自体は正常
    #   B 既にリンクが付いている曲 → 普通の曲名でも404になるかを見る
    #   C 今回うまくいかない曲     → 新しい曲名だけの問題かを見る
    hit = next((x for x in songs if x.get("l")), None)
    probes = [("A 確実に当たる言葉", "コヨリニウム")]
    if hit:
        probes.append(("B 既にリンクのある曲", hit["t"]))
    if added:
        probes.append(("C 今回だめだった曲", added[0]["t"]))
    # D は C と同じ曲名を、空白を + で送る形にしたもの。
    # ブラウザのフォームはこちらの形なので、違いが出るなら原因はここ。
    probes.append(("E でたらめな言葉", "zzqqxxyy12345"))
    if added:
        b = base_title(added[0]["t"])
        if b:
            probes.append(("F Cの飾りを落とした曲名", b))

    for label, word in probes:
        url = UTANET_SEARCH % urllib.parse.quote(word)
        try:
            page = fetch(url)
            pt = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
            has = "あり" if '<tbody class="songlist-table-body">' in page else "なし"
            print("  診断 %s「%s」: HTTP 200 / %d文字 / 枠=%s / title=%s"
                  % (label, word, len(page), has,
                     (strip_tags(pt.group(1))[:50] if pt else "なし")))
            if has == "なし":
                print("       中身 → %s" % re.sub(r"\s+", " ", strip_tags(page))[:200])
        except Exception as e:
            print("  診断 %s「%s」: %s" % (label, word, _why(e)))
            print("       このURLをブラウザで開いてみてください:")
            print("       %s" % url)
        time.sleep(PAUSE)

    def utanet_apply(rec, loud):
        """見つかれば l を付ける。無いと分かったときだけ lx を付ける。"""
        nonlocal u_err, streak
        nonlocal u_404
        st, sid = utanet_id(rec["t"], rec.get("m") or [])
        time.sleep(PAUSE)
        if st == "ok":
            rec["l"] = sid
            streak = 0
            if loud:
                print("  ○ %s → /song/%s/" % (rec["t"], sid))
            return True
        if st == "none":
            rec["lx"] = 1          # 枠はあったが一致しなかった＝無い
            streak = 0
            if loud:
                print("  － %s → 見つからない" % rec["t"])
            return False
        if st == "404":
            rec["lx"] = 1          # 検索結果ゼロ＝歌ネットに無い
            streak = 0             # 障害ではないので打ち切りに数えない
            u_404 += 1
            if loud:
                print("  － %s → 歌ネットに無い（404）" % rec["t"])
            return False
        # error: 本当に取りに行けなかった。印は付けない。来週また試す
        u_err += 1
        streak += 1
        print("  ! %s → 取りに行けなかった（%s）。印は付けない" % (rec["t"], sid))
        return False

    for rec in added[:MAX_UTANET_NEW]:
        if streak >= MAX_ERRORS:
            print("  !! 続けて%d回失敗。歌ネットは今回あきらめる" % MAX_ERRORS)
            break
        if utanet_apply(rec, True):
            got += 1

    # lx が付いていない曲だけを順に試す。これで毎週少しずつ前へ進む。
    need = [s for s in songs if not s.get("l") and not s.get("lx")][:MAX_UTANET_FILL]
    tried = 0
    for s in need:
        if streak >= MAX_ERRORS:
            print("  !! 続けて%d回失敗。埋め戻しは今回ここまで" % MAX_ERRORS)
            break
        tried += 1
        if utanet_apply(s, False):
            filled += 1
    rest = sum(1 for s in songs if not s.get("l") and not s.get("lx"))
    print("  新曲で見つかった: %d件 / 既存曲の埋め戻し: %d件（%d件試した / 未着手あと%d件）"
          % (got, filled, tried, rest))
    if u_404:
        print("  歌ネットに無かった: %d件（404。印を付けたので来週は飛ばす）" % u_404)
    if u_err:
        print("  取りに行けなかった: %d件（印を付けていないので来週また試す）" % u_err)

    # ── 5. Apple Music ──
    print("\n[4] Apple Music の配信日と直リンクを探す")
    pool = [s for s in songs if not s.get("am") and not s.get("amx")]
    targets = (added + pool)[:MAX_ITUNES]
    a_got = a_date = a_err = a_tried = 0
    a_streak = 0
    for s in targets:
        if a_streak >= MAX_ERRORS:
            print("  !! 続けて%d回失敗。Apple は今回あきらめる" % MAX_ERRORS)
            break
        a_tried += 1
        st, rel, tid = itunes_lookup(s["t"], s.get("m") or [])
        time.sleep(PAUSE_APPLE)
        if st == "error":
            a_err += 1
            a_streak += 1
            if a_err <= 3:
                print("  ! %s → 取りに行けなかった（%s）" % (s["t"], rel))
            continue           # 印を付けない。来週また試す
        a_streak = 0
        if tid:
            s["am"] = tid
            a_got += 1
        else:
            s["amx"] = 1       # 無いと確かめた印
        if rel and not s.get("r"):
            s["r"], s["rs"] = rel, "apple"
            a_date += 1
    a_rest = sum(1 for s in songs if not s.get("am") and not s.get("amx"))
    print("  直リンク %d件 / 配信日 %d件（%d件試した / 未着手あと%d件）"
          % (a_got, a_date, a_tried, a_rest))
    if a_err:
        print("  取りに行けなかった: %d件（印を付けていないので来週また試す）" % a_err)

    # ── 6. 反映 ──
    if added:
        songs.extend(added)
        # 日付だけで並べ替える。Pythonの並べ替えは安定なので、同じ日の曲の
        # 既存の順番は保たれる。差分を無駄に大きくしないため。
        songs.sort(key=lambda s: s.get("d") or "", reverse=True)
    print("\n[5] フェスのセットリスト")
    new_fes = update_setlists(data)
    allfes = data.get("fes", [])
    print("  持っているフェス: %d件 / 今回入れた: %d件" % (len(allfes), len(new_fes)))

    print("\n[6] 同じ曲の別バージョンを畳む")
    folded = fold_solo_versions(data)
    if not folded:
        print("  畳むものなし")

    print("\n[7] メンバー一覧をそろえる")
    n_fixed, n_added = fix_roster(data, mem_branch, mem_info)
    if not n_fixed and not n_added:
        print("  直すところなし")

    meta = recount(data)

    print("\n[8] まとめ")
    print("  追加: %d曲 / 保留: %d曲 / 曲名重複で見送り: %d曲"
          % (len(added), len(hold), len(dup)))
    print("  合計 %d曲 / コール 確定%d・公式まで%d・推定まで%d"
          % (meta["total"], meta["calls3"], meta["calls2"], meta["calls"]))
    print("  歌詞リンク %d件 / Apple直リンク %d件"
          % (sum(1 for s in songs if s.get("l")),
             sum(1 for s in songs if s.get("am"))))

    if not args.apply:
        print("\n下見だけなので、ファイルは書き換えていません。")
        print("（本番にするには --apply を付けて走らせます）")
        return 0

    if not (added or got or filled or a_got or a_date or n_fixed or n_added
            or folded or new_fes):
        print("\n変わったところが無いので、書き換えません。")
        return 0

    # ── 安全装置 ──
    # 無人で走るので、おかしな結果をそのまま公開しないように止める。
    # 畳んだぶんは正当な減少。それ以外で減っていたらおかしいので止める。
    if len(songs) < n_before - len(folded):
        print("\n!! 曲数が %d から %d に減った（畳んだのは%d曲）。"
              "説明がつかないので中止する。" % (n_before, len(songs), len(folded)))
        return 1
    if not data["meta"].get("range", [""])[0]:
        print("\n!! 日付の範囲が空になった。中止する。")
        return 1

    write_data(args.file, src, j, k, data)
    after = len(open(args.file, encoding="utf-8").read())
    if after < bytes_before * 0.9:
        print("\n!! 書いたファイルが %d → %d と大きく縮んだ。元に戻して中止する。"
              % (bytes_before, after))
        open(args.file, "w", encoding="utf-8").write(src)
        return 1

    print("\n%s を書き換えました。(%d → %d 文字)" % (args.file, bytes_before, after))
    return 0


if __name__ == "__main__":
    sys.exit(main())
