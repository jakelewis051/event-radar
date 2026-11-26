"""Scheduled structured-data discovery. No model calls or paid services required."""
import json, re, hashlib, os, sys, urllib.request, urllib.parse, concurrent.futures
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
from pathlib import Path
ROOT=Path(__file__).parent
TZ=ZoneInfo('America/Los_Angeles')
class Parser(HTMLParser):
 def __init__(self): super().__init__(); self.active=False; self.blocks=[]; self.buf=''
 def handle_starttag(self,tag,attrs):
  if tag=='script' and dict(attrs).get('type','').lower()=='application/ld+json': self.active=True; self.buf=''
 def handle_data(self,data):
  if self.active: self.buf+=data
 def handle_endtag(self,tag):
  if tag=='script' and self.active: self.blocks.append(self.buf); self.active=False

def walk(x):
 if isinstance(x,dict):
  t=x.get('@type',[]); t=[t] if isinstance(t,str) else t
  if any(isinstance(v,str) and v.endswith('Event') for v in t): yield x
  for v in x.values(): yield from walk(v)
 elif isinstance(x,list):
  for v in x: yield from walk(v)

def normalize(x,source,now):
 name,region,page=source
 if not isinstance(x.get('name'),str) or not isinstance(x.get('startDate'),str): return None
 if 'T' not in x['startDate']: return None
 if any(s in str(x.get('eventStatus','')) for s in ('Cancelled','Postponed','Rescheduled')): return None
 try:
  dt=datetime.fromisoformat(x['startDate'].replace('Z','+00:00')); dt=dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
 except ValueError: return None
 if dt<now or dt.date()>now.date()+timedelta(days=30): return None
 if dt.weekday()<5 and (dt.hour,dt.minute)<(17,30): return None
 title=x['name'].strip(); desc=re.sub('<[^>]+>',' ',str(x.get('description',''))); words=(title+' '+desc).lower()
 if re.search(r'\b(trivia|karaoke|conference)\b',title.lower()): return None
 loc=x.get('location'); venue=loc.get('name',name) if isinstance(loc,dict) else name
 if not isinstance(venue,str): venue=name
 url=urllib.parse.urljoin(page,str(x.get('url') or page)); url=url if urllib.parse.urlparse(url).scheme in ('https','http') else page
 side=bool(re.search(r'\b(immersive|burlesque|shibari|heist|interactive|risograph|pantomime|cabaret)\b',words))
 cat=next((c for c,p in [('Theater','theat|musical|stage production'),('Comedy','comedy|improv'),('Workshop','workshop|printmaking'),('Art','gallery|exhibition'),('Outdoor','hike|outdoor'),('Music','concert|music|band')] if re.search(p,words)), 'Other')
 key='|'.join([re.sub(r'\W','',title.casefold()),dt.isoformat(),re.sub(r'\W','',venue.casefold())]); return dict(id=hashlib.sha256(key.encode()).hexdigest()[:24],name=title,date=dt.date().isoformat(),time=dt.strftime('%H:%M'),venue=venue,region=region,category=cat,rating=3 if side else 2,side=side,description=' '.join(desc.split())[:400] or 'See source for details.',url=url,source=page,checked_at=now.isoformat(),verification='structured source; not independently confirmed')

def scan(source,now):
 name,region,url=source; status=dict(venue=name,url=url,checked_at=now.isoformat())
 if not url: return [],dict(status,state='manual source needed')
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'PersonalEventRadar/0.2 (low frequency calendar reader)'})
  with urllib.request.urlopen(req,timeout=15) as r: html=r.read(2_000_000).decode('utf-8','replace')
  p=Parser(); p.feed(html); found=[]; raw=0
  for block in p.blocks:
   try: data=json.loads(block)
   except ValueError: continue
   for x in walk(data):
    raw+=1; e=normalize(x,source,now)
    if e: found.append(e)
  return found,dict(status,state='parsed' if raw else 'no structured events; manual review needed',count=len(found))
 except Exception as e: return [],dict(status,state='fetch or parse failed',detail=type(e).__name__)

def main():
 now=datetime.now(TZ); sources=json.loads((ROOT/'sources.json').read_text()); events={}; coverage=[]
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
  for rows,status in pool.map(lambda s:scan(s,now),sources):
   coverage.append(status)
   for e in rows: events[e['id']]=e
 # Preserve last known listings for failed sources, explicitly mark stale.
 path=ROOT/'feed.json'; old=json.loads(path.read_text()) if path.exists() else {}; failed={s['url'] for s in coverage if s['state']!='parsed'}
 for e in old.get('events',[]):
  if e.get('source') in failed and e['date']>=now.date().isoformat() and e['id'] not in events:
   events[e['id']]=dict(e,verification='stale; recheck source')
 feed=dict(generated_at=now.isoformat(),events=sorted(events.values(),key=lambda e:(e['date'],e['time'])),coverage=coverage)
 path.write_text(json.dumps(feed,ensure_ascii=False,indent=2)); print(f'{len(events)} events; {len(coverage)} source checks')
if __name__=='__main__': main()
