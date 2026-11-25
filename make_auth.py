import json
lines = [l.strip() for l in open('raw.txt', encoding='utf-8') if l.strip()]
h = dict(zip(lines[0::2], lines[1::2]))
for k in ('content-length', 'content-encoding', 'accept-encoding'):
    h.pop(k, None)
json.dump(h, open('browser.json', 'w'), indent=2)
print('ok, headers:', len(h))
