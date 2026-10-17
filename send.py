# Sends macro-tracker reminders via Web Push. Runs on GitHub Actions every 15 min.
import os, json, datetime, requests
from zoneinfo import ZoneInfo
import google.auth
from google.auth.transport.requests import Request
from pywebpush import webpush, WebPushException

UID = os.environ['MACROS_UID']
PROJECT = os.environ.get('FB_PROJECT', 'hr-etqanfurniture')
MEALS = ['فطار', 'سناك 1', 'غدا', 'سناك 2', 'عشا']
WINDOW = int(os.environ.get('WINDOW_MIN', '20'))

cred, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/datastore'])
cred.refresh(Request())
H = {'Authorization': 'Bearer ' + cred.token}
DOCS = f'https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents'
BASE = f'{DOCS}/macros/{UID}'

def dec(v):
    k, x = next(iter(v.items()))
    if k == 'integerValue': return int(x)
    if k == 'doubleValue': return float(x)
    if k == 'arrayValue': return [dec(i) for i in x.get('values', [])]
    if k == 'mapValue': return {a: dec(b) for a, b in x.get('fields', {}).items()}
    if k == 'nullValue': return None
    return x
def enc(v):
    if isinstance(v, bool): return {'booleanValue': v}
    if isinstance(v, int): return {'integerValue': str(v)}
    if isinstance(v, float): return {'doubleValue': v}
    if isinstance(v, str): return {'stringValue': v}
    if isinstance(v, list): return {'arrayValue': {'values': [enc(x) for x in v]}}
    if isinstance(v, dict): return {'mapValue': {'fields': {k: enc(x) for k, x in v.items()}}}
    return {'nullValue': None}
def get(path=''):
    r = requests.get(BASE + path, headers=H)
    if r.status_code == 404: return {}
    r.raise_for_status()
    return {k: dec(v) for k, v in r.json().get('fields', {}).items()}
def patch(fields):
    q = '&'.join('updateMask.fieldPaths=' + k for k in fields)
    requests.patch(BASE + '?' + q, headers=H, json={'fields': {k: enc(v) for k, v in fields.items()}}).raise_for_status()

_v = requests.get(DOCS + '/pushcfg/vapid', headers=H); _v.raise_for_status()
VAP = {k: dec(v) for k, v in _v.json()['fields'].items()}
VAPID_PRIV = VAP['priv']
now = datetime.datetime.now(ZoneInfo('Africa/Cairo'))
today = now.strftime('%Y-%m-%d')
root = get()
rem, subs = root.get('rem') or {}, root.get('push') or []
if not subs or not rem: print('no subs/rem'); raise SystemExit
day = get('/days/' + today)
entries = day.get('entries') or []
log = root.get('pushLog') or {}
log0 = dict(log)
log = {k: v for k, v in log.items() if v == today}
msgs = []
for mi in (0, 2, 4):
    t = rem.get(f'm{mi}')
    if not t: continue
    h, m = map(int, t.split(':'))
    at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    key = f'm{mi}'
    if at <= now < at + datetime.timedelta(minutes=WINDOW*3) and log.get(key) != today:
        if not any(e.get('meal') == mi for e in entries):
            msgs.append((key, {'title': 'لسه مسجلتش ال' + MEALS[mi], 'body': 'افتح التطبيق وسجّل ' + MEALS[mi], 'tag': key}))
        log[key] = today
w = float(rem.get('w') or 0)
goal = int(root.get('wGoal') or 8)
water = int(day.get('water') or 0)
if w > 0 and 9 <= now.hour < 23 and water < goal:
    slot = f'w{int((now.hour*60+now.minute)//(w*60))}'
    if log.get(slot) != today:
        msgs.append((slot, {'title': '💧 اشرب مية', 'body': f'مسجّل {water} من {goal} كوبايات', 'tag': 'water'}))
        log[slot] = today
# evening "what's left" summary
DEF_T = {'rest': {'k': 1800, 'p': 125, 'c': 190, 'f': 60}, 'pp': {'k': 2000, 'p': 125, 'c': 240, 'f': 60}, 'legs': {'k': 2200, 'p': 125, 'c': 300, 'f': 55}}
TN = {'rest': 'راحة', 'pp': 'Push/Pull', 'legs': 'Legs'}
def tgt(t):
    return {**DEF_T.get(t, DEF_T['rest']), **((root.get('targets') or {}).get(t) or {})}
def tot(es):
    return {x: sum(float(e.get(x) or 0) for e in es) for x in 'kpcf'}
sched = root.get('sched') or {}
def dtype(d, ds):
    js = (ds.weekday() + 1) % 7
    return d.get('type') or sched.get(str(js)) or 'rest'
ev = rem.get('e')
if ev and log.get('e') != today:
    h, m = map(int, ev.split(':'))
    at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if at <= now < at + datetime.timedelta(minutes=WINDOW*3):
        log['e'] = today
        if entries:
            T, U = tgt(dtype(day, now)), tot(entries)
            R = {x: T[x] - U[x] for x in 'kpcf'}
            pr = '✅ البروتين اكتمل' if R['p'] <= 0 else f"بروتين {round(R['p'])} جم"
            kc = f"{round(R['k'])} سعرة" if R['k'] >= 0 else f"زيادة {round(-R['k'])} سعرة"
            body = f"فاضلك {kc} · {pr} · كارب {max(0, round(R['c']))} · دهون {max(0, round(R['f']))}"
            try:
                import re
                USE = re.compile(r'بروتين|واي|هايبرو|whey|protein', re.I)
                PROC = re.compile(r'بسكويت|شوكولات|شكولات|سبريد|حلاوة|براوني|كيك|كوكيز|مربى|جيلي|جرانولا|سيرب|بديل العسل|فلامنكو|سناك|رايس كيك|بوبس|مشروب|مدخن|لانشون|سوسيس|شيبس|بيتزا|شاورما|برجر|توست|ويفر|آيس كريم|بانيه|كيتو بار|نوتيلا|صوص|عصير|بسبوسة|كنافة|تمر بالشوكولاتة')
                tags = root.get('prTag') or {}
                def pr(e):
                    n = e.get('name') or ''
                    if e.get('items'): return 0
                    if n in tags: return int(tags[n])
                    if USE.search(n) and 'سبريد' not in n: return 2
                    return 1 if PROC.search(n) else 0
                pk = sum(float(e.get('k') or 0) for e in entries if pr(e) == 1)
                if U['k'] > 0:
                    body += f" · مصنّع {round(pk / U['k'] * 100)}%"
                    if pk > T['k'] * 0.2 and R['k'] > 150:
                        body += ' — خلّي الباقي أكل طبيعي'
                    elif R['p'] > 25 and R['k'] > 0:
                        body += f" — العشا ما يقلّش عن {min(50, round(R['p']))} جم بروتين"
            except Exception as ex:
                print('pr note err', ex)
            msgs.append(('e', {'title': '📊 فاضلك كام النهارده', 'body': body, 'tag': 'eve'}))
        else:
            msgs.append(('e', {'title': '📊 لسه مسجلتش أكل النهارده', 'body': 'سجّل اللي أكلته عشان تعرف فاضلك كام', 'tag': 'eve'}))
# Saturday weekly summary (Sat-Fri just finished)
if rem.get('wk') and now.weekday() == 5 and now.hour == 10 and log.get('wk') != today:
    log['wk'] = today
    n, sk, sp, adh, Tk = 0, 0, 0, 0, 0
    for i in range(7, 0, -1):
        ds = now - datetime.timedelta(days=i)
        d = get('/days/' + ds.strftime('%Y-%m-%d'))
        es = d.get('entries') or []
        if not es: continue
        U, T = tot(es), tgt(dtype(d, ds))
        n += 1; sk += U['k']; sp += U['p']; Tk += T['k']
        if abs(U['k'] - T['k']) <= T['k'] * 0.1 and U['p'] >= T['p'] * 0.9: adh += 1
    if n:
        W = sorted(root.get('weights') or [], key=lambda x: x['d'])
        wt = ''
        if len(W) >= 2: wt = f" · الوزن {W[-1]['w']} ({W[-1]['w']-W[-2]['w']:+.1f})"
        body = f"سجّلت {n}/7 أيام · متوسط {round(sk/n)} سعرة (الهدف {round(Tk/n)}) · بروتين {round(sp/n)} جم · {adh} أيام مظبوطة{wt}"
        msgs.append(('wk', {'title': '🗓 ملخص الأسبوع', 'body': body, 'tag': 'week'}))
alive = []
for s in subs:
    ok = True
    for _, p in msgs:
        try:
            webpush(subscription_info={'endpoint': s['endpoint'], 'keys': s['keys']}, data=json.dumps(p, ensure_ascii=False),
                    vapid_private_key=VAPID_PRIV, vapid_claims={'sub': VAP['sub']}, ttl=3600)
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410): ok = False; break
            print('push error', e)
    if ok: alive.append(s)
upd = {}
if log != log0: upd['pushLog'] = log
if len(alive) != len(subs): upd['push'] = alive
if upd: patch(upd)
print(now.isoformat(), 'sent', [k for k, _ in msgs])
