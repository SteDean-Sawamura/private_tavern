"""模拟事件链可达性"""
import sqlite3, json, sys

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    db = sqlite3.connect('data/tavern.db')
    row = db.execute('SELECT content FROM scripts WHERE id="soviet_august_coup_1991"').fetchone()
    data = json.loads(row[0])

    wp = {}
    for w in data['world_properties']:
        v = w['value']
        try:
            v = int(v)
        except (ValueError, TypeError):
            try:
                v = float(v)
            except (ValueError, TypeError):
                pass
        wp[w['id']] = v
    start = '1991-08-16T06:00'

    events = []
    for e in data['one_time_events']:
        tt = e.get('trigger_time', '')
        sc = e.get('state_changes', [])
        cond = e.get('condition', '')
        events.append((tt, e['id'], cond, sc))
    events.sort(key=lambda x: x[0])

    state = dict(wp)
    current_day = 16
    problems = []

    for tt, eid, cond, sc in events:
        if tt < start:
            problems.append("BEFORE_START " + eid)
            continue

        day = int(tt[8:10])
        while current_day < day:
            current_day += 1
            days_from = current_day - 16
            if 70 <= state['coup_preparedness'] < 100:
                state['coup_preparedness'] += 3
            if state['coup_preparedness'] >= 80:
                state['coup_preparedness'] -= 2
            if state['public_unease'] >= 20:
                state['public_unease'] += 3
            if days_from % 2 == 0:
                if state['coup_preparedness'] < 100:
                    state['gorbachev_isolation'] += 5
                    state['vdv_loyalty'] += 3
                if state['coup_preparedness'] < 100 and state['yeltsin_awareness'] < 70:
                    state['yeltsin_awareness'] += 3
                    state['public_unease'] += 2

        met = True
        reason = ''
        if cond:
            pairs = [
                ('coup_preparedness >= 100', state['coup_preparedness'] >= 100),
                ('coup_preparedness >= 95', state['coup_preparedness'] >= 95),
                ('coup_preparedness >= 90', state['coup_preparedness'] >= 90),
                ('coup_preparedness >= 70', state['coup_preparedness'] >= 70),
                ('gorbachev_isolation >= 70', state['gorbachev_isolation'] >= 70),
                ('gorbachev_isolation >= 45', state['gorbachev_isolation'] >= 45),
                ('yeltsin_awareness >= 70', state['yeltsin_awareness'] >= 70),
                ('yeltsin_awareness >= 60', state['yeltsin_awareness'] >= 60),
                ('yeltsin_awareness < 40', state['yeltsin_awareness'] < 40),
                ('vdv_loyalty <= 50', state['vdv_loyalty'] <= 50),
                ('vdv_loyalty <= 30', state['vdv_loyalty'] <= 30),
                ('public_unease >= 50', state['public_unease'] >= 50),
            ]
            for pat, result in pairs:
                if pat in cond and not result:
                    met = False
                    prop = pat.split('.')[-1].split(' ')[0]
                    val = str(state.get(prop, '?'))
                    reason = prop + "=" + val + " vs " + pat
                    break

        sym = 'OK' if met else 'XX'
        suffix = " [" + reason + "]" if reason else ""
        print(sym + " " + tt[5:16] + " " + eid + suffix)
        if not met:
            problems.append("UNREACHABLE " + eid + ": " + reason)

        if met and sc:
            for s in sc:
                target = s['target'].split('.')[-1]
                if target in state:
                    if s['op'] == 'add':
                        state[target] += s['value']
                    elif s['op'] == 'set':
                        state[target] = s['value']

    print("\n=== Final state ===")
    for k, v in state.items():
        print("  " + k + ": " + str(v))

    print("\n=== PROBLEMS (" + str(len(problems)) + ") ===")
    for p in problems:
        print("  ! " + p)
    db.close()

if __name__ == "__main__":
    main()
