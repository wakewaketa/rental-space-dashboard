#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直前に Google Calendar の list_events で読んだ結果を、セッション記録(JSONL)から
機械的に拾って <DATA>/events/<calendarId>.json に保存する。

なぜこうするか:
  カレンダーの結果をエージェントが Write で打ち直すと、件数が多いほど途中で化ける
  （金額の桁落ち・予約の欠落）。記録から直接取り出せば、人の転記が挟まらない。

仕組み:
  1. セッション記録の tool_use のうち名前に list_events を含むものから
     「呼び出しID → calendarId」を控える
  2. 同じIDの tool_result から events 配列を取り出す
     （結果が大きくてファイルに保存された場合は "saved to <パス>" を開く）
  3. calendarId ごとに event.id で重複を除いて保存（後に読んだ結果で上書き）

あわせて、Gmail の get_thread / get_message で読んだスペースマーケットの予約メール
（「予約ID」「合計」の表がある通知）から金額を拾い、<DATA_DIR>/amounts.json に足す。
1通に複数の予約IDがある（定期予約）ときは合計を件数で割る。
手で調べた金額（upnow・Yoyappin など）は amounts_manual.json に書く。build.py はそちらを優先する。

前回の公開ページ（Artifact の read）を読んでいれば、ページに埋め込んだ金額
（<script id="rsd-amounts">）も引き継ぐ。Gmail で新しく拾った金額の方を優先する。

使い方:
    python3 collect.py <DATA_DIR>
    → <DATA_DIR>/events/<calendarId>.json と <DATA_DIR>/amounts.json を作り、件数を表示
"""
import glob, json, os, re, sys

MAX_TRANSCRIPTS = 3
MAX_BYTES = 300 * 1024 * 1024


def transcripts():
    fs = set()
    for p in [os.path.expanduser('~/.claude/projects/*/*.jsonl'), '/root/.claude/projects/*/*.jsonl']:
        fs.update(glob.glob(p))
    fs = [f for f in fs if os.path.isfile(f) and os.path.getsize(f) < MAX_BYTES]
    fs.sort(key=os.path.getmtime, reverse=True)
    return fs[:MAX_TRANSCRIPTS]


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))
    return ''


def payloads(text):
    """ツール結果テキストから events 配列を持つ JSON を取り出す"""
    m = re.search(r'saved to (\S+?)(?:[\s"\'\]]|$)', text)
    if m and os.path.isfile(m.group(1).rstrip('.')):
        try:
            text = open(m.group(1).rstrip('.'), encoding='utf-8', errors='replace').read()
        except OSError:
            pass
    dec = json.JSONDecoder()
    i = text.find('{')
    while i != -1:
        try:
            obj, end = dec.raw_decode(text[i:])
            if isinstance(obj, dict) and isinstance(obj.get('events'), list):
                yield obj
                i = text.find('{', i + end)
                continue
            if isinstance(obj, dict) and isinstance(obj.get('items'), list):
                yield {'events': obj['items']}
                i = text.find('{', i + end)
                continue
        except ValueError:
            pass
        i = text.find('{', i + 1)


SM_ID = re.compile(r'予約ID\s*\|\s*([\d,\s]+?)\s*\|')
SM_TOTAL = re.compile(r'\|\s*合計\s*\|\s*[￥¥]\s*([\d,]+)')


def sm_amounts(text, out):
    """Gmail の結果テキストからスペースマーケット通知の 予約ID→金額 を拾う"""
    try:
        obj = json.loads(text)
    except ValueError:
        return 0
    msgs = obj.get('messages') if isinstance(obj, dict) and 'messages' in obj else [obj]
    n = 0
    for m in msgs or []:
        if not isinstance(m, dict) or 'spacemarket' not in (m.get('sender') or ''):
            continue
        body = m.get('plaintextBody') or m.get('plaintext_body') or ''
        a, b = SM_ID.search(body), SM_TOTAL.search(body)
        if not (a and b):
            continue
        ids = [x for x in re.split(r'[,\s]+', a.group(1)) if x.isdigit()]
        total = int(b.group(1).replace(',', ''))
        for i in ids:
            out[i] = round(total / len(ids))
            n += 1
    return n


KEEP = re.compile(r'<script type="application/json" id="rsd-amounts">(.*?)</script>', re.S)


def page_amounts(text, out):
    """Artifact read の結果（本文 or 保存先ファイル）から前回の金額を拾う"""
    for m in re.finditer(r'saved to (\S+?\.html)', text):
        try:
            text += open(m.group(1), encoding='utf-8', errors='replace').read()
        except OSError:
            pass
    n = 0
    for m in KEEP.finditer(text):
        raw = m.group(1).replace('\\"', '"')
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        for k, v in d.items():
            out[str(k)] = v
            n += 1
    return n


def main():
    if len(sys.argv) < 2:
        sys.exit('使い方: python3 collect.py <DATA_DIR>')
    outdir = os.path.join(os.path.expanduser(sys.argv[1]), 'events')
    os.makedirs(outdir, exist_ok=True)
    for f in transcripts():
        calls = {}                      # tool_use id -> calendarId
        mails = set()                   # Gmail を読んだ tool_use id
        pages = set()                   # 前回の公開ページを読んだ tool_use id
        kept = {}
        amounts = {}
        found = {}                      # calendarId -> {event id: event}
        ncall = {}
        with open(f, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if not any(k in line for k in ('list_events', 'tool_result', 'get_thread', 'get_message', 'Artifact')):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                content = (rec.get('message') or {}).get('content')
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get('type') == 'tool_use' and 'list_events' in (c.get('name') or ''):
                        inp = c.get('input') or {}
                        cal = inp.get('calendarId') or inp.get('calendar_id') or 'primary'
                        calls[c.get('id')] = cal
                    elif c.get('type') == 'tool_use' and re.search(r'get_thread|get_message', c.get('name') or ''):
                        mails.add(c.get('id'))
                    elif c.get('type') == 'tool_use' and c.get('name') == 'Artifact' and (c.get('input') or {}).get('action') == 'read':
                        pages.add(c.get('id'))
                    elif c.get('type') == 'tool_result' and c.get('tool_use_id') in pages:
                        page_amounts(result_text(c.get('content')), kept)
                    elif c.get('type') == 'tool_result' and c.get('tool_use_id') in mails:
                        sm_amounts(result_text(c.get('content')), amounts)
                    elif c.get('type') == 'tool_result' and c.get('tool_use_id') in calls:
                        cal = calls[c['tool_use_id']]
                        for obj in payloads(result_text(c.get('content'))):
                            ncall[cal] = ncall.get(cal, 0) + 1
                            bucket = found.setdefault(cal, {})
                            for e in obj['events']:
                                if isinstance(e, dict) and e.get('id'):
                                    bucket[e['id']] = e
        if found:
            for cal, evs in found.items():
                safe = re.sub(r'[^A-Za-z0-9@._-]', '_', cal)
                path = os.path.join(outdir, safe + '.json')
                json.dump({'calendarId': cal, 'events': list(evs.values())},
                          open(path, 'w', encoding='utf-8'), ensure_ascii=False)
                print(f'{cal}: list_events {ncall.get(cal, 0)}回 / 予定 {len(evs)}件 → {path}')
            ap = os.path.join(os.path.dirname(outdir), 'amounts.json')
            old = json.load(open(ap, encoding='utf-8')) if os.path.exists(ap) else {}
            old.update(kept)            # 前回ページの金額 → 今回 Gmail で拾った金額の順に上書き
            old.update(amounts)
            json.dump(old, open(ap, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
            print(f'スペースマーケットの金額: 前回ページから {len(kept)}件・Gmail から {len(amounts)}件 / amounts.json 計 {len(old)}件')
            return
    sys.exit('list_events の結果がセッション記録に見つかりません。先にカレンダーを読んでください。')


if __name__ == '__main__':
    main()
