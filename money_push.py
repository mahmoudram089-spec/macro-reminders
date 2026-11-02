# «محفظتي» reminders via Web Push — DRAFT, not deployed.
# Reads money/{uid} (settings, accounts, cats, push), money/{uid}/obl/*, money/{uid}/tx/* from Firestore (REST, service account)
# and sends at most one push per item per day, from 10:00 Cairo, following the user's settings.notif:
#   types: inst/gam/charity/bill/loan/goal/budget/payday (on/off) · before: N days before the due date · onDay: also on the due date.
# v11: «يوم المرتب» — on the payday weekday (settings.payday, default 'wed') at the first run after 10:00 Cairo, and once more at 20:00
#      if settings.lastPayday is still before today; the body lists the loan installment(s) I owe this week and the expected transfer to the savings account.
# Dedupe markers live in money/{uid}/meta/pushlog  (field `sent`: {key: 'YYYY-MM-DD'}), pruned after 40 days.
import os, json, datetime, calendar, requests
from zoneinfo import ZoneInfo
from pywebpush import webpush, WebPushException

CAIRO = ZoneInfo('Africa/Cairo')
SEND_FROM_H, SEND_UNTIL_H = int(os.environ.get('MONEY_FROM_H', '10')), int(os.environ.get('MONEY_UNTIL_H', '22'))
DEF_SPLIT = {'needs': 35, 'wants': 15, 'save': 40, 'charity': 10}
DEF_TYPES = {'inst': True, 'gam': True, 'charity': True, 'bill': True, 'loan': True, 'goal': True, 'budget': True, 'payday': True}
DEF_XSPLIT = {'needs': 0, 'wants': 30, 'save': 60, 'charity': 10}   # v11: split of extra income (incomeType 'extra')
PD_WEEKDAY = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
PAYDAY_AM_H, PAYDAY_PM_H = 10, 20
# built-in category → bucket (same as DEF_CATS in app.js); user cats saved in root.cats override/extend it
DEF_BUCKET = {'food': 'needs', 'trans': 'needs', 'bills': 'needs', 'home': 'needs', 'health': 'needs', 'family': 'needs', 'fee': 'needs', 'otherout': 'needs',
              'outing': 'wants', 'shop': 'wants', 'fun': 'wants', 'subs': 'wants',
              'course': 'save', 'ai': 'save', 'tool': 'save', 'book': 'save', 'saving': 'save', 'invest': 'save',
              'inst': 'needs', 'gam': 'save', 'gamdebt': 'needs', 'goal': 'save', 'gold': 'save',
              'sadaqa': 'charity', 'zakat': 'charity', 'donate': 'charity',
              'svtr': 'save', 'svout': 'save', 'loanrep': 'save'}
BK_N = {'needs': 'الأساسيات', 'wants': 'الرغبات', 'save': 'الادخار', 'charity': 'الصدقات'}


# ---------------------------------------------------------------- date helpers (mirror app.js)
def ym(d): return str(d or '')[:7]
def days_in(m): y, mo = map(int, m.split('-')); return calendar.monthrange(y, mo)[1]
def add_months(m, n):
    y, mo = map(int, m.split('-')); t = y * 12 + (mo - 1) + n
    return f'{t // 12:04d}-{t % 12 + 1:02d}'
def due_date(m, day): return f'{m}-{min(max(1, int(day or 1)), days_in(m)):02d}'
def plus_days(d, n): return (datetime.date.fromisoformat(d) + datetime.timedelta(days=n)).isoformat()
def days_between(a, b): return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
def m_idx(a, b): (y1, m1), (y2, m2) = map(int, a.split('-')), map(int, b.split('-')); return (y2 - y1) * 12 + (m2 - m1)
def end_of_m(m): return f'{m}-{days_in(m):02d}'
def r2(x): return round((float(x or 0)) * 100) / 100
def money(v, cur='EGP'):
    v = float(v or 0)
    s = f'{abs(v):,.2f}' if (cur in ('USD', 'USDT') or (abs(v) % 1 and abs(v) < 1000)) else f'{abs(v):,.0f}'
    return ('$' + s) if cur == 'USD' else (s + ' USDT') if cur == 'USDT' else (s + ' ج')


# ---------------------------------------------------------------- pure computation (testable without Firestore)
def compute(root, obl, tx, today, hour=None):
    """root: money/{uid} fields · obl: {id: doc} · tx: {id: doc} · today: 'YYYY-MM-DD' (Cairo) · hour: Cairo hour now (None = morning run).
    Returns a list of dicts: key, ty, title, body, date (due date or None), stage ('before'|'day'|'late'|'month')."""
    st = root.get('settings') or {}
    nt = st.get('notif') or {}
    types = {**DEF_TYPES, **(nt.get('types') or {})}
    before = int(nt.get('before', 2) if nt.get('before') is not None else 2)
    on_day = nt.get('onDay', True) is not False
    sp = st.get('split') or {}
    if not all(k in sp for k in DEF_SPLIT) or abs(sum(float(sp[k] or 0) for k in DEF_SPLIT) - 100) > 0.01: sp = DEF_SPLIT
    xs = {k: float((st.get('extraSplit') or {}).get(k) or 0) for k in DEF_XSPLIT}
    if not st.get('extraSplit') or any(v < 0 for v in xs.values()) or abs(sum(xs.values()) - 100) > 0.01: xs = DEF_XSPLIT
    pd_wd = PD_WEEKDAY.get(st.get('payday') or 'wed', 2)
    is_payday = datetime.date.fromisoformat(today).weekday() == pd_wd
    pd_end = plus_days(today, 6)
    pd_loans = []   # [(person, amount, cur)] installments I owe due in this payday week (payday … +6 days)
    rate = float(st.get('rate') or 55)
    accs = {a['id']: a for a in (root.get('accounts') or [])}
    bucket = dict(DEF_BUCKET)
    for c in root.get('cats') or []:
        if c.get('kind') == 'out' and c.get('bucket'): bucket[c['id']] = c['bucket']
    cur_m = ym(today)
    tx_by_obl = {}
    for tid, t in tx.items():
        if t.get('obl'): tx_by_obl.setdefault(t['obl'], []).append(t)

    def cur_of(t):
        if t.get('acc') == '__ext': return t.get('cur') or 'EGP'
        a = accs.get(t.get('acc'))
        if a: return a.get('cur') or 'EGP'
        o = obl.get(str(t.get('acc', '')).split(':', 1)[-1])
        return (o or {}).get('cur') or 'EGP' if (o or {}).get('kind') == 'loan' else 'EGP'
    def egp(v, cur, r=None): return float(v or 0) * (float(r or 0) or rate) if cur in ('USD', 'USDT') else float(v or 0)

    def balances():
        b = {a: float(x.get('open') or 0) for a, x in accs.items()}
        for t in tx.values():
            A = float(t.get('amount') or 0)
            if t.get('type') == 'in' and t.get('acc') in b: b[t['acc']] += A
            elif t.get('type') == 'out' and t.get('acc') in b: b[t['acc']] -= A
            elif t.get('type') == 'tr':
                if t.get('acc') in b: b[t['acc']] -= A + float(t.get('fee') or 0)
                if t.get('to') in b: b[t['to']] += float(A if t.get('toAmount') in (None, '') else t['toAmount'])
        return b

    def flows(m):
        inc, bk = 0.0, {'needs': 0.0, 'wants': 0.0, 'save': 0.0, 'charity': 0.0}
        incx = 0.0  # v11: extra income (split by extraSplit)
        for t in tx.values():
            if ym(t.get('date')) != m: continue
            c = cur_of(t); v = egp(t.get('amount'), c, t.get('rate'))
            if t.get('type') == 'in':
                inc += v
                if t.get('incomeType') == 'extra': incx += v
            elif t.get('type') == 'out': bk[bucket.get(t.get('cat'), 'needs') if bucket.get(t.get('cat'), 'needs') in bk else 'needs'] += v
            elif t.get('type') == 'tr':
                if float(t.get('fee') or 0) > 0: bk['needs'] += egp(t['fee'], c, t.get('rate'))
                to = str(t.get('to') or '')
                if t.get('obl') and to.startswith('g:') and t.get('oblK') == 'pay':
                    o = obl.get(t['obl']) or {}
                    after = o.get('kind') == 'gam' and o.get('start') and o.get('turn') and m_idx(o['start'], t.get('oblM') or ym(t.get('date'))) > m_idx(o['start'], o['turn'])
                    bk['needs' if after else 'save'] += v
                elif t.get('obl') and (to.startswith('v:') or to.startswith('au:')): bk['save'] += v
        return inc, bk, incx

    items = []
    def dated(key, ty, title, body, date):
        if not types.get(ty, True) or not date: return
        dd = days_between(today, date)
        stage = 'before' if dd == before and before > 0 else 'day' if dd == 0 and on_day else 'late' if dd < 0 and -dd in (1, 3, 7, 14) else None
        if stage: items.append({'key': f'{key}:{date}:{stage}', 'ty': ty, 'title': title, 'body': body, 'date': date, 'stage': stage, 'dd': dd})
    def when(date):
        dd = days_between(today, date)
        return 'النهارده' if dd == 0 else 'بكرة' if dd == 1 else f'بعد {dd} أيام' if dd > 0 else f'متأخر {-dd} يوم'

    for oid, o in obl.items():
        k, status = o.get('kind'), o.get('status') or 'active'
        L = tx_by_obl.get(oid, [])
        if k in ('inst', 'gam') and status not in ('paused', 'done'):
            gam = k == 'gam'
            N = int(float(o.get('members' if gam else 'months') or 0)); A = float(o.get('contrib' if gam else 'monthly') or 0)
            s0 = o.get('start') if gam else ym(o.get('start'))
            if not s0: continue
            paid = int(float(o.get('paidBefore') or 0)) + sum(1 for t in L if t.get('oblK') == 'pay')
            if paid < N:
                d = due_date(add_months(s0, paid), o.get('dueDay'))
                dated(f'{k}:{oid}:{paid}', k, ('💳 قسط ' if not gam else '🤝 جمعية ') + (o.get('name') or ''), f"{money(A)} — {when(d)} (قسط {paid + 1} من {N})", d)
            got = bool(o.get('gotBefore')) or any(t.get('oblK') == 'get' for t in L)
            if gam and not got and o.get('turn'):
                d = due_date(o['turn'], o.get('dueDay'))
                dated(f'gamget:{oid}', 'gam', '🎉 دورك تقبض ' + (o.get('name') or ''), f"{money(o.get('payout') or A * N)} — {when(d)}", d)
        elif k == 'bill' and status not in ('paused', 'done') and o.get('next'):
            dated(f'bill:{oid}', 'bill', '🔁 ' + (o.get('name') or 'فاتورة'), f"{money(o.get('amount'), o.get('cur') or 'EGP')} — {when(o['next'])}", o['next'])
        elif k == 'loan' and status != 'done':
            C = o.get('cur') or 'EGP'; amount = float(o.get('amount') or 0)
            reps = [t for t in L if t.get('oblK') == 'rep']
            repaid = sum(float(t.get('amount') or 0) for t in reps); rem = r2(amount - repaid)
            if rem <= 0.005: continue
            who = ('قسط ليّا عند ' if o.get('dir') == 'lend' else 'سداد سلفة لـ ') + (o.get('person') or '')
            plan = o.get('plan') or ('once' if o.get('due') else 'free')
            if plan in ('inst', 'week') and float(o.get('instN') or 0) > 0 and o.get('instStart'):
                N0 = int(float(o['instN'])); A0 = float(o.get('instAmt') or 0) or r2(amount / N0); s0 = o['instStart']; d0 = int(s0[8:])
                at0 = (lambda i: plus_days(s0, 7 * i)) if plan == 'week' else (lambda i: due_date(add_months(ym(s0), i), d0))
                sch = [{'d': x['d'], 'a': float(x.get('a') or 0)} for x in (o.get('sched') or [])] or [{'d': at0(i), 'a': A0} for i in range(N0)]
                xtra = sum(float(t.get('amount') or 0) for t in reps if t.get('extra')); reg = max(0.0, r2(repaid - xtra))
                cum, acc_ = [], 0.0
                for x in sch: acc_ = r2(acc_ + x['a']); cum.append(acc_)
                pn = 0
                while pn < len(sch) and cum[pn] <= reg + 0.01: pn += 1
                if is_payday and o.get('dir') != 'lend':
                    k = 0
                    while k < len(sch) and sch[k]['d'] <= pd_end: k += 1
                    due_wk = r2(max(0.0, min(rem, (cum[k - 1] if k else 0) - reg)))
                    if due_wk > 0.005: pd_loans.append((o.get('person') or '', due_wk, C, oid))
                if pn < len(sch):
                    d = sch[pn]['d']; amt = min(rem, r2(cum[pn] - reg))
                    dated(f'loan:{oid}:{pn}', 'loan', '🤲 ' + who, f"{money(amt, C)} — {when(d)} (قسط {pn + 1} من {len(sch)})", d)
            elif plan == 'once' and o.get('due'):
                if is_payday and o.get('dir') != 'lend' and o['due'] <= pd_end: pd_loans.append((o.get('person') or '', rem, C, oid))
                dated(f'loan:{oid}', 'loan', '🤲 ' + who, f"{money(rem, C)} — {when(o['due'])}", o['due'])
        elif k == 'goal' and o.get('deadline') and o['deadline'] >= today and types.get('goal', True):
            saved = float(o.get('startBal') or 0)
            for t in tx.values():
                if t.get('to') == 'v:' + oid: saved += float(t.get('toAmount') or t.get('amount') or 0)
                if t.get('acc') == 'v:' + oid: saved -= float(t.get('amount') or 0)
            if o.get('linkAcc') and o['linkAcc'] in accs:
                a = accs[o['linkAcc']]; saved += egp(balances().get(a['id'], 0), a.get('cur') or 'EGP')
            target = float(o.get('target') or 0); rem = max(0.0, target - saved)
            ml = m_idx(cur_m, ym(o['deadline'])) + 1
            if rem > 0 and ml > 0:
                dep = sum(egp(t.get('amount'), cur_of(t), t.get('rate')) for t in L if t.get('oblK') == 'pay' and ym(t.get('date')) == cur_m)
                need = r2(rem / ml - dep)
                if need > 1: dated(f'goal:{oid}:{cur_m}', 'goal', '🎯 هدف ' + (o.get('name') or ''), f'فاضل تحط {money(need)} الشهر ده عشان تلحق الميعاد', end_of_m(cur_m))

    inc, bk, incx = flows(cur_m)
    base = inc if inc > 0 else float(st.get('expIncome') or 0)
    # v11: monthly allowance per bucket = base income × split + extra income × extraSplit (expected income × split when nothing recorded)
    tg = {k: ((inc - incx) * float(sp[k]) + incx * float(xs[k])) / 100 if inc > 0 else base * float(sp[k]) / 100 for k in DEF_SPLIT}
    # صدقات: due from the 1st; remind on the 1st, then on the 8th/15th/22nd while not covered
    if types.get('charity', True) and base > 0 and float(sp['charity']) > 0:
        due = r2(tg['charity']); left = r2(due - bk['charity'])
        day = int(today[8:])
        if left > 0.5 and day in (1, 8, 15, 22):
            items.append({'key': f'charity:{cur_m}:{day}', 'ty': 'charity', 'title': '🤲 صدقات الشهر',
                          'body': f"مستحق {money(left)} ({sp['charity']}% من {'الدخل المتوقع' if inc <= 0 else 'دخل الشهر'})" + (f" — اتطلّع {money(bk['charity'])}" if bk['charity'] else ''),
                          'date': f'{cur_m}-01', 'stage': 'month'})
    # تجاوز الميزانية: once per bucket per month
    if types.get('budget', True) and base > 0:
        for b in ('needs', 'wants'):
            tgt = tg[b]
            if bk[b] > tgt + 0.5:
                items.append({'key': f'budget:{b}:{cur_m}', 'ty': 'budget', 'title': f'⚠️ تجاوزت ميزانية {BK_N[b]}',
                              'body': f'صرفت {money(bk[b])} من {money(tgt)} (زيادة {money(bk[b] - tgt)})', 'date': today, 'stage': 'month'})
    # يوم المرتب (v11)
    if types.get('payday', True) and is_payday and (st.get('lastPayday') or '') < today:
        pm = hour is not None and hour >= PAYDAY_PM_H
        if hour is None or hour >= PAYDAY_AM_H:
            parts = [f'قسط السلفة لـ {p} {money(a, c)}' for p, a, c, _ in pd_loans]
            # installments already paid from this payday (tx.pd == today) still come out of the save share
            paid_egp = sum(egp(t.get('amount'), cur_of(t), t.get('rate')) for t in tx.values() if t.get('pd') == today and t.get('oblK') == 'rep'
                           and str(t.get('to') or '').startswith('l:') and (obl.get(t.get('obl')) or {}).get('dir') != 'lend')
            try: base_sal = float(st.get('baseSalary') or 0)
            except (TypeError, ValueError): base_sal = 0.0
            link = st.get('link') or {}
            sv = link.get('save') if 'save' in link else next((a for a, x in accs.items() if any(w in (x.get('name') or '').lower() for w in ('الأهلي', 'الاهلي', 'ahly', 'nbe'))), '')
            loans_egp = sum(egp(a, c) for p, a, c, _ in pd_loans) + paid_egp
            if base_sal > 0 and sv in accs:
                move = r2(max(0.0, base_sal * float(sp['save']) / 100 - loans_egp))
                if move > 0: parts.append(f"حوّل حوالي {money(move)} {'لل' + accs[sv]['name'][2:] if (accs[sv].get('name') or '').startswith('ال') else 'لـ' + (accs[sv].get('name') or '')}")
            body = ('النهارده يوم المرتب: ' + ' + '.join(parts) + ' — افتح المحفظة تشوف التحويلات والمطابقة') if parts else 'النهارده يوم المرتب 💰 سجّل المرتب واعمل المطابقة'
            items.append({'key': f"payday:{today}:{'pm' if pm else 'am'}", 'ty': 'payday', 'title': '💰 لسه ما خلصتش يوم المرتب' if pm else '💰 يوم المرتب',
                          'body': body, 'date': today, 'stage': 'payday', 'url': '/money/?a=payday'})
            # the payday push already names this week's installment → drop the separate loan reminder for it today
            in_pd = {oid for *_, oid in pd_loans}
            items[:] = [x for x in items if not (x['ty'] == 'loan' and x['key'].split(':')[1] in in_pd and x['stage'] in ('before', 'day'))]
    return items


# ---------------------------------------------------------------- Firestore I/O
def run(DOCS, H, VAP, dec, enc):  # dec/enc = the Firestore (de)serializers from send.py
    uid = os.environ.get('MONEY_UID') or os.environ['MACROS_UID']
    base = f'{DOCS}/money/{uid}'
    now = datetime.datetime.now(CAIRO); today = now.strftime('%Y-%m-%d')
    if not (SEND_FROM_H <= now.hour < SEND_UNTIL_H): print('money: outside send window'); return

    def get_doc(url):
        r = requests.get(url, headers=H)
        if r.status_code == 404: return {}
        r.raise_for_status(); return {k: dec(v) for k, v in r.json().get('fields', {}).items()}
    def list_col(name):
        out, tok = {}, None
        while True:
            r = requests.get(f'{base}/{name}', headers=H, params={'pageSize': 300, **({'pageToken': tok} if tok else {})}); r.raise_for_status()
            j = r.json()
            for d in j.get('documents', []): out[d['name'].rsplit('/', 1)[1]] = {k: dec(v) for k, v in d.get('fields', {}).items()}
            tok = j.get('nextPageToken')
            if not tok: return out
    def patch_doc(url, fields):
        q = '&'.join('updateMask.fieldPaths=' + k for k in fields)
        requests.patch(url + '?' + q, headers=H, json={'fields': {k: enc(v) for k, v in fields.items()}}).raise_for_status()

    root = get_doc(base)
    subs = root.get('push') or []
    if not subs: print('money: no push subscriptions'); return
    items = compute(root, list_col('obl'), list_col('tx'), today, now.hour)
    logdoc = get_doc(f'{base}/meta/pushlog'); sent = logdoc.get('sent') or {}
    sent = {k: v for k, v in sent.items() if v >= plus_days(today, -40)}
    todo = [x for x in items if x['key'] not in sent]
    if not todo: print('money: nothing new', [x['key'] for x in items]); return
    # one push per item (tag = item key so the phone replaces, not stacks, repeats of the same item)
    alive = []
    for s in subs:
        ok = True
        for x in todo:
            p = {'title': x['title'], 'body': x['body'], 'tag': 'm-' + x['key'].split(':')[0] + '-' + x['key'].split(':')[1], 'url': x.get('url') or '/money/'}
            try:
                webpush(subscription_info={'endpoint': s['endpoint'], 'keys': s['keys']}, data=json.dumps(p, ensure_ascii=False),
                        vapid_private_key=VAP['priv'], vapid_claims={'sub': VAP['sub']}, ttl=6 * 3600)
            except WebPushException as e:
                if e.response is not None and e.response.status_code in (404, 410): ok = False; break
                print('money push error', e)
        if ok: alive.append(s)
    for x in todo: sent[x['key']] = today
    patch_doc(f'{base}/meta/pushlog', {'sent': sent})          # needs Firestore write by the service account
    if len(alive) != len(subs): patch_doc(base, {'push': alive})  # updateMask=push only — never touches accounts/settings
    print(now.isoformat(), 'money sent', [x['key'] for x in todo])
