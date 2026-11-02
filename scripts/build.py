#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""レンタルスペースの予約・売上ダッシュボードを作る。

使い方:
    python3 build.py <DATA_DIR> [today=YYYY-MM-DD]

<DATA_DIR> に必要なもの:
    config.json            拠点・カレンダー・固定費・手数料（config.example.json を参照）
    events/<calId>.json    collect.py が作るカレンダーの予定
    amounts.json           任意。collect.py が Gmail から拾ったスペースマーケットの {"予約ID": 金額}
    amounts_manual.json    任意。手で調べた {"予約ID": 金額}（upnow・Yoyappin など）。こちらが優先
出力:
    <DATA_DIR>/dashboard.html   そのまま開ける1枚のHTML
    <DATA_DIR>/data.json        集計結果（STUDIODATA）
    標準出力に検査結果。NG があれば終了コード 2（dashboard.html は作るが、公開は止めること）

today 以前の予約＝実績、today より後＝先の予約（見込み）。
"""
import json, os, re, sys, math, calendar as _cal
from datetime import datetime, date
from collections import defaultdict, Counter

HERE = os.path.dirname(os.path.abspath(__file__))
NEED = {}   # チャネル -> 金額がまだ分からない予約ID（Gmail で探す対象）
WD = ['月', '火', '水', '木', '金', '土', '日']
SLOTS = ['午前(〜12)', '午後(12-17)', '夜間(17〜)']
SLOTNM = {'午前(〜12)': '午前（〜12時）', '午後(12-17)': '午後（12〜17時）', '夜間(17〜)': '夜間（17時〜）'}
SLOTSHORT = {'午前(〜12)': '午前', '午後(12-17)': '午後', '夜間(17〜)': '夜間'}
PPLORDER = ['1〜2名', '3〜5名', '6〜10名', '11〜20名', '21名〜']

# カレンダー連携で入る予定のタイトル先頭 → チャネル。config の channel_rules で追加・上書きできる。
DEFAULT_RULES = [
    {'prefix': '【インスタベース】予約確定', 'channel': 'インスタベース'},
    {'prefix': '【予約完了】', 'channel': 'スペースマーケット'},
    {'prefix': '【upnow】', 'channel': 'アップナウ'},
    {'prefix': '[よやクル', 'channel': 'よやクル'},
    {'prefix': '[Yoyappin]', 'channel': 'Yoyappin'},
    {'prefix': '[仮予約]', 'channel': 'Yoyappin'},
]
DEFAULT_FEES = {'インスタベース': 0.35, 'スペースマーケット': 0.30, 'よやクル': 0.036,
                'Yoyappin': 0.385, 'アップナウ': 'upnow', '直販': 0}
DEFAULT_EXCLUDE = ['キャンセル', '清掃', '掃除', '片付け', '営業停止', 'ブロック', '設営', 'メンテ',
                   '予約リクエスト', '貸出不可', '予約不可', '見学', '内見', '自社', 'テスト']
DEFAULT_DIRECT = ['よやクル', '直販']


def ym_label(m):            # "2026-08" -> "2026/8月"
    return m[:4] + '/' + str(int(m[5:7])) + '月'


def ymd(x):
    p = x.split('-')
    return f"{p[0]}/{int(p[1])}/{int(p[2])}"


class Config:
    def __init__(self, d):
        self.d = d
        self.fees = dict(DEFAULT_FEES, **(d.get('fees') or {}))
        self.rules = (d.get('channel_rules') or []) + DEFAULT_RULES
        self.exclude = d.get('exclude_keywords') or DEFAULT_EXCLUDE
        self.direct = d.get('direct_channels') or DEFAULT_DIRECT
        self.start = d.get('start_date') or '2000-01-01'
        self.sites = d['sites']

    def net_of(self, plat, amt):
        if not amt:
            return 0
        g = int(amt)
        f = self.fees.get(plat, 0.35)
        if f == 'upnow':        # 決済2.98%＋手数料3.5%＋99円/件、すべて税別
            fee = (math.ceil(g * 0.0298) + math.ceil(g * 0.035) + 99) * 1.1
            return max(0, round(g - fee))
        return round(g * (1 - float(f)))


def classify(cfg, site, s, desc):
    for r in cfg.rules:
        if s.startswith(r['prefix']) or (r.get('in_description') and r['prefix'] in desc):
            return r['channel']
    if 'yoyappin.westjr' in desc:
        return 'Yoyappin'
    if site.get('direct'):      # 手入力の直販予約（「山田様」「直販」など）
        if '直販' in s or '直販' in desc:
            return '直販'
        if re.search(r'(様|さん)', s) and not s.startswith('【') and not s.startswith('['):
            return '直販'
    return None


def rid_of(plat, s, desc, eid):
    if plat == 'インスタベース':
        m = re.search(r'予約ID[:：]\s*(\d+)', s) or re.search(r'bookings/(\d+)', desc)
        return 'IB' + m.group(1) if m else 'EID' + eid
    if plat == 'よやクル':
        m = re.search(r'支払いID[:：]\s*(\S+)', desc)
        return 'YK' + m.group(1) if m else 'EID' + eid
    if plat == 'Yoyappin':
        m = re.search(r'pre_bookings/(\d+)', desc) or re.search(r'予約No[:：]?\s*(\d+)', s) or re.search(r'(\d{6,})', s)
        return m.group(1) if m else 'EID' + eid
    if plat == '直販':
        return 'EID' + eid
    m = re.search(r'(\d{6,})', s)
    return m.group(1) if m else 'EID' + eid


def field(desc, *pats):
    for p in pats:
        m = re.search(p, desc)
        if m:
            return m.group(1).strip()
    return ''


def numlabel(p):
    m = re.search(r'(\d+)', p or '')
    if not m:
        return None
    n = int(m.group(1))
    return '1〜2名' if n <= 2 else '3〜5名' if n <= 5 else '6〜10名' if n <= 10 else '11〜20名' if n <= 20 else '21名〜'


def parse_site(cfg, site, data_dir, amounts):
    """拠点1つ分の予約行。複数カレンダー(部屋)は (チャネル, 予約ID) で重複を除き、部屋の集合を持たせる。"""
    rows, index = [], {}
    stats = Counter()
    for room, cal in site['calendars'].items():
        safe = re.sub(r'[^A-Za-z0-9@._-]', '_', cal)
        path = os.path.join(data_dir, 'events', safe + '.json')
        if not os.path.exists(path):
            stats['missing_calendar'] += 1
            print(f'  ！ {site["name"]} / {room}: {path} がありません')
            continue
        d = json.load(open(path, encoding='utf-8'))
        evs = d.get('events', d) if isinstance(d, dict) else d
        for e in evs:
            s = e.get('summary', '') or ''
            desc = e.get('description', '') or ''
            eid = e.get('id', '') or ''
            start = (e.get('start') or {}).get('dateTime')
            end = (e.get('end') or {}).get('dateTime')
            if not start:
                continue
            stats['events'] += 1
            if e.get('status') == 'cancelled' or e.get('transparency') == 'transparent':
                continue
            if any(k in s for k in cfg.exclude):
                continue
            since = site.get('start_date') or cfg.start
            if start[:10] < since:
                continue
            plat = classify(cfg, site, s, desc)
            if not plat:
                stats['unclassified'] += 1
                continue
            rid = rid_of(plat, s, desc, eid)
            key = (plat, rid)
            if key in index:                       # 同じ予約が別の部屋のカレンダーにも入っている＝貸切
                index[key]['rooms'].add(room)
                continue
            sdt = datetime.fromisoformat(start.replace('Z', '+00:00'))
            edt = datetime.fromisoformat(end.replace('Z', '+00:00')) if end else None
            dur = round((edt - sdt).total_seconds() / 3600, 2) if edt else 0
            slot = SLOTS[0] if sdt.hour < 12 else SLOTS[1] if sdt.hour < 17 else SLOTS[2]
            purpose = people = amt = ''
            cust = None
            if plat == 'インスタベース':
                purpose = field(desc, r'利用用途[:：][^\S\n]*([^\n]*)')
                people = field(desc, r'利用人数[:：][^\S\n]*([^\n]*)')
                amt = field(desc, r'予約金額[:：]\s*([\d,]+)').replace(',', '')
                m = re.search(r'- (.+?)様', s); cust = m.group(1) if m else None
            elif plat == 'よやクル':
                purpose = field(desc, r'利用目的[:：][^\S\n]*([^\n]*)')
                people = field(desc, r'利用人数[:：][^\S\n]*([^\n]*)')
                amt = field(desc, r'予約料金[:：]\s*([\d,]+)').replace(',', '')
                cust = field(desc, r'お名前[:：][^\S\n]*([^\n]+)') or None
            elif plat == 'スペースマーケット':
                purpose = field(desc, r'\[利用目的\][^\S\n]*([^\n]*)')
                people = field(desc, r'\[人数\][^\S\n]*([^\n]*)')
                m = re.search(r'】(.+?)様', s); cust = m.group(1) if m else None
            elif plat == '直販':
                amt = field(desc, r'料金[:：]\s*([\d,]+)円').replace(',', '')
                if not amt and site.get('direct_rate'):
                    amt = str(round(site['direct_rate'] * dur))
                m = re.search(r'([^\s　【】\[\]（）()]+?)(様|さん)', s); cust = m.group(1) if m else None
            else:
                purpose = field(desc, r'利用目的[:：][^\S\n]*([^\n]*)', r'用途[:：][^\S\n]*([^\n]*)')
                people = field(desc, r'人数[:：][^\S\n]*([^\n]*)')
            if not amt:
                for k in (rid, rid.replace('IB', '').replace('YK', '')):
                    if amounts.get(k):
                        amt = str(int(str(amounts[k]).replace(',', '')))
                        break
            if not amt and plat in ('スペースマーケット', 'アップナウ', 'Yoyappin'):
                stats['need'] += 1
                NEED.setdefault(plat, []).append(rid)
            if not amt and plat == 'スペースマーケット' and site.get('sm_hourly'):
                h = site['sm_hourly']
                amt = str(round((h.get('weekend', h.get('weekday', 0)) if sdt.weekday() >= 5 else h.get('weekday', 0)) * dur))
                stats['sm_estimated'] += 1
            cust = re.sub(r'[\s　]+', '', cust or '').lower() or None
            r = dict(date=sdt.strftime('%Y-%m-%d'), start=start[:16], dow=WD[sdt.weekday()], slot=slot, dur=dur,
                     plat=plat, people=people, purpose=purpose, amount=amt, rid=rid,
                     net=cfg.net_of(plat, amt), cust=cust, rooms={room})
            index[key] = r
            rows.append(r)
    return rows, stats


def agg(sub, plats):
    n = len(sub)
    ct = defaultdict(lambda: [0, 0, 0])
    for r in sub:
        ct[r['dow']][SLOTS.index(r['slot'])] += 1
    stack = [[ct[d][0], ct[d][1], ct[d][2]] for d in WD]
    uc = Counter(r['purpose'] for r in sub if r['purpose']).most_common()
    uses = ([[k, v] for k, v in uc[:8]] + [['その他', sum(v for _, v in uc[8:])]]) if len(uc) > 8 else [[k, v] for k, v in uc]
    pc = Counter(numlabel(r['people']) for r in sub if numlabel(r['people']))
    ppl = [[k, pc[k]] for k in PPLORDER if pc[k]]
    plat = []
    for p in plats:
        ps = [r for r in sub if r['plat'] == p]
        if ps:
            plat.append([p, len(ps), sum(int(r['amount']) for r in ps if r['amount']), sum(r['net'] for r in ps)])
    dur = []
    for sl in SLOTS:
        ds = [r['dur'] for r in sub if r['slot'] == sl]
        dur.append([SLOTNM[sl], round(sum(ds) / len(ds), 1) if ds else 0, len(ds)])
    known = [int(r['amount']) for r in sub if r['amount']]
    dowc = Counter(r['dow'] for r in sub); slotc = Counter(r['slot'] for r in sub)
    bd = dowc.most_common(1)[0] if dowc else ('-', 0)
    bs = slotc.most_common(1)[0] if slotc else ('-', 0)
    return dict(n=n, stack=stack, uses=uses, ppl=ppl, plat=plat, dur=dur,
                revenue=sum(known), hoursKnown=round(sum(r['dur'] for r in sub if r['amount']), 1),
                net=sum(r['net'] for r in sub), knownN=len(known), unknownN=n - len(known),
                useN=sum(1 for r in sub if r['purpose']), pplN=sum(1 for r in sub if numlabel(r['people'])),
                bd=bd[0], bdn=bd[1], bs=SLOTSHORT.get(bs[0], bs[0]), bsn=bs[1], kaigi=0)


def repeat_block(rows, actual, months, today, nrooms, plats, direct):
    """新規／リピート。顧客は予約者名で名寄せ（空白除去・小文字化）。表記ゆれは別人扱い。"""
    if not months:
        return None
    hist = sorted([r for r in rows if r['cust'] and r['date'] <= today], key=lambda r: r['start'])
    if len(hist) < 10:
        return None
    first = {}
    for r in hist:
        first.setdefault(r['cust'], r)
    cnt = Counter(r['cust'] for r in hist)
    rp = [p for p in plats if any(r['plat'] == p for r in hist)]
    mrows = []
    for m in months[-8:]:
        sub = [r for r in actual if r['date'][:7] == m]
        named = [r for r in sub if r['cust']]
        newn = sum(1 for r in named if first.get(r['cust']) is r)
        y, mo = int(m[:4]), int(m[5:7])
        days = int(today[8:10]) if m == today[:7] else _cal.monthrange(y, mo)[1]
        hrs = sum(r['dur'] * r['_w'] for r in sub)
        newh = sum(r['dur'] for r in named if first.get(r['cust']) is r)
        reph = sum(r['dur'] for r in named if first.get(r['cust']) is not r)
        mrows.append([ym_label(m), len(named), newn, len(named) - newn, round(newh, 1), round(reph, 1),
                      round(100 * hrs / (nrooms * 24 * days)), round(100 * hrs / (nrooms * 15 * days))])
    win = months[-5:]
    W = [r for r in actual if r['date'][:7] in win and r['cust']]
    src = []
    for sp in rp:
        reps = [r for r in W if first[r['cust']] is not r and first[r['cust']]['plat'] == sp]
        if reps:
            src.append([sp, len(reps)] + [sum(1 for r in reps if r['plat'] == p) for p in rp])
    price = []
    for lab, isnew in (('新規', True), ('リピート', False)):
        for p in [None] + rp:
            sub = [r for r in W if (first[r['cust']] is r) == isnew and (p is None or r['plat'] == p)]
            if not sub:
                continue
            k = [r for r in sub if r['amount']]
            kh = sum(r['dur'] for r in k)
            price.append([lab, p or '全体', len(sub), round(sum(r['dur'] for r in sub) / len(sub), 2),
                          round(sum(int(r['amount']) for r in k) / len(k)) if k else None,
                          round(sum(int(r['amount']) for r in k) / kh) if kh else None,
                          round(sum(r['net'] for r in k) / kh) if kh else None])
    cm = months[-7:-2]
    ret = []
    for sp in rp:
        cs = [c for c, f in first.items() if f['plat'] == sp and f['date'][:7] in cm]
        if not cs:
            continue
        back = [c for c in cs if cnt[c] >= 2]
        moved = None if sp in direct else sum(
            1 for c in back if any(r['cust'] == c and r['plat'] in direct and first[c] is not r for r in hist))
        ret.append([sp, len(cs), len(back), round(sum(cnt[c] for c in cs) / len(cs), 1), moved])
    return dict(months=mrows, src=src, price=price, ret=ret, plats=rp,
                win=ym_label(win[0]) + '〜' + ym_label(win[-1]),
                retWin=(ym_label(cm[0]) + '〜' + ym_label(cm[-1])) if cm else '',
                custN=len(cnt), oneN=sum(1 for v in cnt.values() if v == 1),
                unnamed=sum(1 for r in actual if not r['cust']), since=ymd(hist[0]['date']))


def build_tab(rows, today, plats, fixed, fixed_since, note, nrooms, direct, label):
    actual = [r for r in rows if r['date'] <= today]
    future = [r for r in rows if r['date'] > today]
    months = sorted(set(r['date'][:7] for r in actual))
    scopes = {'全期間': agg(actual, plats)}
    for m in months:
        scopes[m] = agg([r for r in actual if r['date'][:7] == m], plats)
    trend = [[ym_label(m), scopes[m]['revenue'], scopes[m]['n'], scopes[m]['knownN'], scopes[m]['unknownN'], scopes[m]['net']]
             for m in months]
    cutoff = int(today[8:10])
    mtd = []
    for m in months:
        sb = [r for r in actual if r['date'][:7] == m and int(r['date'][8:10]) <= cutoff]
        mtd.append([ym_label(m), sum(int(r['amount']) for r in sb if r['amount']), len(sb), sum(r['net'] for r in sb)])
    fby = []
    for m in sorted(set(r['date'][:7] for r in future)):
        sb = [r for r in future if r['date'][:7] == m]
        fby.append([ym_label(m), sum(int(r['amount']) for r in sb if r['amount']), len(sb), sum(r['net'] for r in sb)])
    fplat = []
    for p in plats:
        ps = [r for r in future if r['plat'] == p]
        if ps:
            fplat.append([p, len(ps), sum(int(r['amount']) for r in ps if r['amount']), sum(r['net'] for r in ps)])
    fknown = [int(r['amount']) for r in future if r['amount']]
    FUTURE = dict(n=len(future), revenue=sum(fknown), net=sum(r['net'] for r in future),
                  knownN=len(fknown), unknownN=len(future) - len(fknown), byMonth=fby, plat=fplat,
                  horizon=(max(r['date'] for r in future) if future else ''))
    dmin = min(r['date'] for r in actual) if actual else ''
    be_by = [[ym_label(m), fixed if (fixed and (not fixed_since or m >= fixed_since)) else 0] for m in months]
    META = dict(rangeLabel=(f"{ymd(dmin)}〜{ymd(today)}" if actual else ''), stamp='最終更新: ' + today,
                cutoff=cutoff, be=dict(monthly=fixed or 0, byMonth=be_by, label=label,
                                       provisional=not fixed, note=note))
    return dict(MONTHS=months, TREND=trend, SCOPES=scopes, META=META, MTD=mtd, FUTURE=FUTURE,
                REPEAT=repeat_block(rows, actual, months, today, nrooms, plats, direct))


# ── 複数拠点の合算（全社合計タブ） ───────────────────────────────
def merge_scope(scopes):
    scopes = [s for s in scopes if s]
    if not scopes:
        return None
    stack = [[0, 0, 0] for _ in range(7)]
    for s in scopes:
        for i, row in enumerate(s.get('stack', [])):
            for j in range(3):
                stack[i][j] += row[j]
    uc = Counter()
    for s in scopes:
        for k, v in s.get('uses', []):
            uc[k] += v
    top = uc.most_common()
    uses = ([[k, v] for k, v in top[:8]] + [['その他', sum(v for _, v in top[8:])]]) if len(top) > 8 else [[k, v] for k, v in top]
    pc = Counter()
    for s in scopes:
        for k, v in s.get('ppl', []):
            pc[k] += v
    ppl = [[k, pc[k]] for k in PPLORDER if pc[k]]
    pl = defaultdict(lambda: [0, 0, 0]); order = []
    for s in scopes:
        for name, n, g, nt in s.get('plat', []):
            if name not in order:
                order.append(name)
            pl[name][0] += n; pl[name][1] += g; pl[name][2] += nt
    dur = []
    for i, label in enumerate(SLOTNM.values()):
        tot = cnt = 0
        for s in scopes:
            d = s.get('dur', [])
            if len(d) > i:
                tot += d[i][1] * d[i][2]; cnt += d[i][2]
        dur.append([label, round(tot / cnt, 1) if cnt else 0, cnt])
    add = lambda f: sum(s.get(f, 0) or 0 for s in scopes)
    dowtot = [sum(r) for r in stack]
    slottot = [sum(stack[d][j] for d in range(7)) for j in range(3)]
    bi = dowtot.index(max(dowtot)) if any(dowtot) else 0
    si = slottot.index(max(slottot)) if any(slottot) else 0
    return dict(n=add('n'), stack=stack, uses=uses, ppl=ppl, plat=[[k] + pl[k] for k in order], dur=dur,
                revenue=add('revenue'), hoursKnown=round(add('hoursKnown'), 1), net=add('net'),
                knownN=add('knownN'), unknownN=add('unknownN'), useN=add('useN'), pplN=add('pplN'),
                bd=WD[bi], bdn=dowtot[bi], bs=['午前', '午後', '夜間'][si], bsn=slottot[si], kaigi=0)


def merge_rows(lists, nkeys):
    acc = defaultdict(lambda: [0] * nkeys); seen = []
    for rows in lists:
        for row in rows or []:
            if row[0] not in seen:
                seen.append(row[0])
            for i in range(nkeys):
                acc[row[0]][i] += row[1 + i]
    seen.sort(key=lambda ym: tuple(int(x) for x in ym.replace('月', '').split('/')))
    return [[ym] + acc[ym] for ym in seen]


def merge_sites(sites, today):
    months = sorted({m for s in sites for m in s['MONTHS']})
    scopes = {'全期間': merge_scope([s['SCOPES'].get('全期間') for s in sites])}
    for m in months:
        sc = merge_scope([s['SCOPES'].get(m) for s in sites])
        if sc:
            scopes[m] = sc
    fs = [s['FUTURE'] for s in sites]
    fpl = defaultdict(lambda: [0, 0, 0]); forder = []
    for f in fs:
        for name, n, g, nt in f.get('plat', []):
            if name not in forder:
                forder.append(name)
            fpl[name][0] += n; fpl[name][1] += g; fpl[name][2] += nt
    FUTURE = dict(n=sum(f['n'] for f in fs), revenue=sum(f['revenue'] for f in fs), net=sum(f['net'] for f in fs),
                  knownN=sum(f['knownN'] for f in fs), unknownN=sum(f['unknownN'] for f in fs),
                  byMonth=merge_rows([f['byMonth'] for f in fs], 3), plat=[[k] + fpl[k] for k in forder],
                  horizon=max([f['horizon'] for f in fs] or ['']))
    firsts = [s['META']['rangeLabel'].split('〜')[0] for s in sites if s['META']['rangeLabel']]
    first = min(firsts, key=lambda d: [int(v) for v in d.split('/')]) if firsts else ''
    # 固定費は拠点ごとの byMonth を月で足す。予約ゼロの月も家賃は出るので直近の値を持ち越す。
    labels = [ym_label(m) for m in months]
    acc = defaultdict(int)
    for s in sites:
        bm = dict(s['META']['be'].get('byMonth') or [])
        last = 0
        for lbl in labels:
            if lbl in bm:
                last = bm[lbl]
            acc[lbl] += last
    be_by = [[l, acc[l]] for l in labels]
    miss = sum(1 for s in sites if not s['META']['be'].get('monthly'))
    BE = dict(monthly=be_by[-1][1] if be_by else 0, byMonth=be_by, label='各拠点の月次固定費の合計',
              provisional=bool(miss), note=('%d拠点が固定費未設定' % miss) if miss else '')
    META = dict(rangeLabel=(f"{first}〜{ymd(today)}" if first else ''), stamp='最終更新: ' + today,
                cutoff=int(today[8:10]), be=BE)
    return dict(MONTHS=months, TREND=merge_rows([s['TREND'] for s in sites], 5), SCOPES=scopes,
                META=META, MTD=merge_rows([s['MTD'] for s in sites], 3), FUTURE=FUTURE, REPEAT=None)


def fee_note(cfg, used):
    parts = []
    for p in used:
        f = cfg.fees.get(p, 0.35)
        if f == 'upnow':
            parts.append(p + '＝実費(約6.5%＋99円/件)')
        elif float(f) == 0:
            parts.append(p + '＝手数料なし')
        else:
            parts.append(p + '%g%%' % round(float(f) * 100, 1))
    return '／'.join(parts)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    data_dir = os.path.expanduser(sys.argv[1])
    today = sys.argv[2] if len(sys.argv) > 2 and re.match(r'\d{4}-\d{2}-\d{2}$', sys.argv[2]) else date.today().isoformat()
    cfg = Config(json.load(open(os.path.join(data_dir, 'config.json'), encoding='utf-8')))
    amounts = {}
    for fn in ('amounts.json', 'amounts_manual.json'):          # 手入力（manual）を後に読んで優先
        ap = os.path.join(data_dir, fn)
        if os.path.exists(ap):
            amounts.update({str(k): v for k, v in json.load(open(ap, encoding='utf-8')).items()})

    STUDIO = {}
    site_alls = []
    used_plats = []
    checks = []
    for site in cfg.sites:
        rows, st = parse_site(cfg, site, data_dir, amounts)
        plats = []
        for r in rows:
            if r['plat'] not in plats:
                plats.append(r['plat'])
        for p in plats:
            if p not in used_plats:
                used_plats.append(p)
        rooms = list(site['calendars'].keys())
        fixed = site.get('fixed_cost') or 0
        since = site.get('fixed_since') or ''
        label = site.get('fixed_label') or ('月次固定費' if fixed else '未設定')
        for r in rows:
            r['_w'] = len(r['rooms'])
        tab_all = build_tab(rows, today, plats, fixed, since, '', len(rooms), cfg.direct, label)
        site_alls.append(tab_all)
        name = site['name']
        tabs = [(name, tab_all)]
        if len(rooms) > 1:
            split = site.get('room_split') or {}
            for room in rooms:
                sub = [dict(r, _w=1) for r in rows if room in r['rooms']]
                share = split.get(room, 1 / len(rooms))
                tabs.append((name + ' ' + room, build_tab(sub, today, plats, round(fixed * share), since,
                                                         '%d%%按分' % round(share * 100), 1, cfg.direct, label)))
        for k, v in tabs:
            STUDIO[k] = v
        a = tab_all['SCOPES']['全期間']
        print(f'■ {name}: 予定{st["events"]}件 → 予約{a["n"]}件（先の予約{tab_all["FUTURE"]["n"]}件）'
              f' 売上¥{a["revenue"]:,} 手取り¥{a["net"]:,} 金額不明{a["unknownN"]}件'
              f' 未分類{st["unclassified"]}件' + (f' SM推定{st["sm_estimated"]}件' if st['sm_estimated'] else ''))
        # 検査
        if st['missing_calendar']:
            checks.append(f'{name}: カレンダーのデータが無い部屋がある')
        if a['n'] == 0:
            checks.append(f'{name}: 予約が0件（カレンダーIDか channel_rules が合っていない）')
        elif a['n'] and a['unknownN'] > a['n'] * 0.3:
            checks.append(f'{name}: 金額不明が3割超（{a["unknownN"]}/{a["n"]}件）。amounts.json を確認')
        ib = [p for p in a['plat'] if p[0] == 'インスタベース']
        if ib and ib[0][1] >= 5 and ib[0][2] == 0:
            checks.append(f'{name}: インスタベースの売上が0円（説明欄の「予約金額」が読めていない）')
        if not fixed:
            checks.append(f'{name}: 固定費が未設定（損益分岐ラインが出ない）※警告のみ')

    if len(site_alls) > 1:
        STUDIO = dict([('全社合計', merge_sites(site_alls, today))] + list(STUDIO.items()))

    names = [s['name'] for s in cfg.sites]
    title = cfg.d.get('title') or (cfg.d.get('business_name', 'レンタルスペース') + ' 予約・売上ダッシュボード')
    sub = (f'{len(names)}拠点（{"・".join(names)}）の実予約を、拠点別・月別に集計。' if len(names) > 1 else
           f'{names[0]}の実予約を月別に集計。') + \
          f'チャネル：{"／".join(used_plats)}。売上＝税込グロス、推定手取り＝手数料控除後。キャンセル・清掃・ブロック等は除外。'
    multi_room = any(len(s['calendars']) > 1 for s in cfg.sites)
    tpl = open(os.path.join(HERE, 'template.html'), encoding='utf-8').read()
    rep = {
        '{{TITLE}}': title, '{{EYEBROW}}': cfg.d.get('business_name', 'レンタルスペース事業'), '{{SUB}}': sub,
        '{{FEE_NOTE}}': fee_note(cfg, used_plats),
        '{{ROOM_NOTE}}': ('複数部屋の拠点は、部屋タブの固定費を按分。同じ予約が複数部屋のカレンダーにある場合（貸切）は拠点タブでは1件、部屋タブにはそれぞれ計上。' if multi_room else ''),
        '{{DIRECT_JSON}}': json.dumps(cfg.direct, ensure_ascii=False),
        '{{SITES_JSON}}': json.dumps(names, ensure_ascii=False),
    }
    for k, v in rep.items():
        tpl = tpl.replace(k, v)
    blob = 'const STUDIODATA=' + json.dumps(STUDIO, ensure_ascii=False) + ';'
    a, b = '//__DATA_START__', '//__DATA_END__'
    i, j = tpl.index(a), tpl.index(b)
    i = tpl.index('\n', i) + 1
    out = tpl[:i] + blob + '\n' + tpl[j:]
    # 拾えた金額（予約ID→金額のみ・個人情報なし）をページに残す。クラウドの自動更新は毎回まっさらな
    # 環境で動くので、翌日は公開ページを読んで collect.py がここから引き継ぐ。
    keep = {k: v for k, v in amounts.items()}
    out = out.replace('</body>', '') + ('\n<script type="application/json" id="rsd-amounts">'
                                       + json.dumps(keep, ensure_ascii=False) + '</script>\n')
    open(os.path.join(data_dir, 'dashboard.html'), 'w', encoding='utf-8').write(out)
    json.dump(STUDIO, open(os.path.join(data_dir, 'data.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'→ {os.path.join(data_dir, "dashboard.html")}（タブ: {"／".join(STUDIO.keys())}）')
    need_path = os.path.join(data_dir, 'missing_amounts.json')
    json.dump(NEED, open(need_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if NEED:
        print('金額未取得: ' + '／'.join(f'{k} {len(v)}件' for k, v in NEED.items()) + f' → {need_path}')
    hard = [c for c in checks if '警告のみ' not in c]
    for c in checks:
        print(('NG  ' if c in hard else '注意 ') + c)
    if hard:
        sys.exit(2)
    print('検査OK')


if __name__ == '__main__':
    main()
