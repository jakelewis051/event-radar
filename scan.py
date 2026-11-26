"""Event Radar 0.4.1: structured discovery and conservative quality filters."""
import json, re, hashlib, urllib.request, urllib.parse, concurrent.futures
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
ROOT = Path(__file__).parent
TZ = ZoneInfo('America/Los_Angeles')
class Parser(HTMLParser):
    def __init__(self):
        super().__init__(); self.active=False; self.blocks=[]; self.buf=''
    def handle_starttag(self,tag,attrs):
        if tag=='script' and dict(attrs).get('type','').lower()=='application/ld+json': self.active=True; self.buf=''
    def handle_data(self,data):
        if self.active: self.buf+=data
    def handle_endtag(self,tag):
        if tag=='script' and self.active: self.blocks.append(self.buf); self.active=False

def walk(x):
    if isinstance(x,dict):
        t=x.get('@type',[]); t=[t] if isinstance(t,str) else t or []
        if isinstance(t,list) and any(isinstance(v,str) and v.endswith('Event') for v in t): yield x
        for v in x.values(): yield from walk(v)
    elif isinstance(x,list):
        for v in x: yield from walk(v)

def clean(v):
    return ' '.join(re.sub(r'<[^>]+>', ' ', unescape(unescape(str(v or '')))).split())

def rejection(title,venue,address=''):
    if re.search(r'\b(las vegas|mesquite|henderson|clark county|pahrump|boulder city|elko|winnemucca|ely)\b',venue+' '+address,re.I) or re.search(r'\((?:[^)]*\b(?:las vegas|mesquite|henderson|mojave desert)\b[^)]*)\)',title,re.I): return 'outside region'
    if re.search(r'\b(online|virtual only|webinar|commencement|convocation|student connect|trivia|karaoke|conference|4-h)\b',title,re.I): return 'low relevance'
    return None

def normalize(x,source,now):
    name,region,page=source
    if not isinstance(x.get('name'),str) or not isinstance(x.get('startDate'),str) or 'T' not in x['startDate']: return None,'missing date or title'
    if any(s in str(x.get('eventStatus','')) for s in ('Cancelled','Postponed','Rescheduled')): return None,'status'
    try:
        dt=datetime.fromisoformat(x['startDate'].replace('Z','+00:00')); dt=dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    except ValueError: return None,'invalid date'
    if dt<now or dt.date()>now.date()+timedelta(days=30): return None,'date window'
    if dt.weekday()<5 and (dt.hour,dt.minute)<(17,30): return None,'weekday hours'
    title=clean(x['name']); desc=clean(x.get('description')); loc=x.get('location'); venue=clean(loc.get('name') or name) if isinstance(loc,dict) else name
    address=loc.get('address','') if isinstance(loc,dict) else ''
    if isinstance(address,dict): address=' '.join(str(address.get(k,'')) for k in ('addressLocality','addressRegion','postalCode'))
    if isinstance(loc,dict) and 'VirtualLocation' in str(loc.get('@type','')): return None,'online'
    reason=rejection(title,venue,str(address))
    if reason: return None,reason
    original=title
    title=re.sub(r'\s*\|\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)s?\s*\|\s*(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?\s+\d{4}\s*$', '', title, flags=re.I)
    words=(title+' '+desc).lower()
    side=bool(re.search(r'\b(immersive|burlesque|shibari|heist|interactive|risograph|pantomime|cabaret|rug tufting|unsilent night|photo walk)\b',words))
    cat=next((c for c,p in [('Workshop','workshop|printmaking|tufting|paint & sip'),('Comedy','comedy|improv'),('Theater','theat|musical|stage production|christmas carol'),('Art','gallery|exhibition|photo walk'),('Outdoor','hike|outdoor'),('Music','concert|music|band|recital|quartet|orchestra|ensemble')] if re.search(p,words)), 'Other')
    url=urllib.parse.urljoin(page,str(x.get('url') or page)); url=url if urllib.parse.urlparse(url).scheme in ('https','http') else page
    key='|'.join([re.sub(r'\W','',x['name'].strip().casefold()),dt.isoformat(),re.sub(r'\W','',str(loc.get('name',name) if isinstance(loc,dict) else name).casefold())])
    return dict(id=hashlib.sha256(key.encode()).hexdigest()[:24],name=title,date=dt.date().isoformat(),time=dt.strftime('%H:%M'),venue=venue,region=region,category=cat,rating=3 if side else 2,side=side,description=desc[:400] or 'See source for details.',url=url,source=page,checked_at=now.isoformat(),verification='structured source; not independently confirmed',address=str(address)),None

def scan(source,now):
    name,region,url=source; status=dict(venue=name,url=url,checked_at=now.isoformat())
    if not url: return [],dict(status,state='manual source needed')
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'PersonalEventRadar/0.4.1 (low frequency calendar reader)'})
        with urllib.request.urlopen(req,timeout=15) as r: html=r.read(2_000_000).decode('utf-8','replace')
        p=Parser(); p.feed(html); found=[]; raw=0; rejected=Counter()
        for block in p.blocks:
            try: data=json.loads(block)
            except ValueError: continue
            for x in walk(data):
                raw+=1; e,reason=normalize(x,source,now)
                if e: found.append(e)
                else: rejected[reason]+=1
        return found,dict(status,state='parsed' if raw else 'no structured events; manual review needed',count=len(found),raw=raw,rejected=dict(rejected))
    except Exception as e: return [],dict(status,state='fetch or parse failed',detail=type(e).__name__)

def dedup(events):
    unique={}
    for e in events:
        # Same source, title and start time; different sources/performances remain separate.
        k=(e.get('source'),re.sub(r'\W','',e['name'].casefold()),e['date'],e['time'])
        if k not in unique or len(e['description'])>len(unique[k]['description']): unique[k]=e
    return list(unique.values())

def main():
    now=datetime.now(TZ); sources=json.loads((ROOT/'sources.json').read_text()); events={}; coverage=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for rows,status in pool.map(lambda s:scan(s,now),sources):
            coverage.append(status)
            for e in rows: events[e['id']]=e
    path=ROOT/'feed.json'; old=json.loads(path.read_text()) if path.exists() else {}; failed={s['url'] for s in coverage if s['state']!='parsed' and s['url']}
    for e in old.get('events',[]):
        if e.get('source') in failed and e['date']>=now.date().isoformat() and e['id'] not in events and not rejection(e['name'],e['venue'],e.get('address','')):
            events[e['id']]=dict(e,name=clean(e['name']),description=clean(e.get('description')),verification='stale; recheck source')
    rows=dedup(events.values()); feed=dict(generated_at=now.isoformat(),events=sorted(rows,key=lambda e:(e['date'],e['time'])),coverage=coverage,scanner_version='0.4.1',duplicates_removed=len(events)-len(rows))
    path.write_text(json.dumps(feed,ensure_ascii=False,indent=2)); print(f'{len(rows)} events; {len(coverage)} source checks')
if __name__=='__main__': main()
