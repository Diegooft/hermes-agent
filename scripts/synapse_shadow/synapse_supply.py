"""Canonical plans -> native Triage. No model, alternate queue, or graph mutation."""
import argparse, contextlib, datetime, hashlib, json, os, pathlib, re, sqlite3, subprocess
import yaml

HOME=pathlib.Path(os.environ.get('HERMES_HOME','C:/dev/hermes/synapse-shadow'))
REPO=pathlib.Path('C:/dev/ecosystem')
ACTIVE={'triage','todo','ready','scheduled'}
DONE={'DONE','LANDED_IN_STAGING','CERTIFIED'}

def identity(program,node): return 'synapse-supply:'+program+':'+node
def backlog_count(cards): return sum(c.get('status') in ACTIVE for c in cards)
class Refusal(Exception):
 """Recusa NOMEADA: codigo + classe (INFRA|POLITICA) + numero medido. Ausencia != zero."""
 def __init__(self,code,klass,measured=None):
  super().__init__(code);self.code=code;self.klass=klass;self.measured=measured or {}
 def record(self): return {'status':'refused','codigo':self.code,'classe':self.klass,'medido':self.measured}

def _utc(value,now):
 # None => sem expiracao. Valor ilegivel => levanta (o chamador trata como EXPIRADA: fail-closed).
 if value is None: return None
 if isinstance(value,datetime.datetime): d=value
 elif isinstance(value,datetime.date): d=datetime.datetime(value.year,value.month,value.day)
 else: d=datetime.datetime.fromisoformat(str(value))
 return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)

def decision_holds(decisions,ids,now=None):
 """True so se TODA decisao exigida existe, esta `ratified` e NAO expirou."""
 now=now or datetime.datetime.now(datetime.timezone.utc)
 table=decisions.get('decisions') if isinstance(decisions,dict) else None
 if not isinstance(table,dict): return False
 for did in ids:
  d=table.get(did)
  if not isinstance(d,dict) or d.get('status')!='ratified': return False
  try: exp=_utc(d.get('expires_at'),now)
  except (ValueError,TypeError): return False
  if exp is not None and exp<=now: return False
 return True

def claim_state(card,now=None):
 """NONE (sem claim) | LIVE (claim_expires futuro) | STALE (claim sem validade legivel ou vencida)."""
 now=now or datetime.datetime.now(datetime.timezone.utc)
 if not card.get('claim_lock') and card.get('claim_expires') in (None,''): return 'NONE'
 try: exp=float(card.get('claim_expires'))
 except (TypeError,ValueError): return 'STALE'
 return 'LIVE' if exp>now.timestamp() else 'STALE'

def stale_claims(cards,now=None): return [c for c in cards if claim_state(c,now)=='STALE']

def read_pinned(path):
 try: raw=pathlib.Path(path).read_bytes()
 except OSError as e: raise Refusal('FONTE_INDISPONIVEL','INFRA',{'caminho':str(path),'erro':type(e).__name__})
 if not raw.strip(): raise Refusal('FONTE_VAZIA','INFRA',{'caminho':str(path),'bytes':len(raw)})
 return raw,hashlib.sha256(raw).hexdigest()

def verify_pinned(path,expected):
 _,now_sha=read_pinned(path)
 if now_sha!=expected: raise Refusal('FONTE_MUTADA','INFRA',{'caminho':str(path),'esperado':expected,'atual':now_sha})

def eligible(graph,decisions=None,now=None):
 nodes=graph.get('nodes')
 if not isinstance(nodes,dict): raise ValueError('Graph nodes must be a mapping')
 for key,node in nodes.items():
  if not isinstance(node,dict): raise ValueError('Invalid graph node')
  deps=node.get('depends_on',[])
  if not isinstance(deps,list): raise ValueError('Invalid dependencies')
  state=str(node.get('state','')).upper()
  if state not in {'TODO','READY','NOT_STARTED'} and not (state=='BLOCKED_BY_DEPENDENCY' and deps): continue
  need=node.get('requires_decision')
  if need and not (decisions is not None and isinstance(need,list) and decision_holds(decisions,need,now)): continue
  if all(dep in nodes and str(nodes[dep].get('state','')).upper() in DONE for dep in deps): yield str(key),node

def duplicate(program,key,node,cards,prs):
 token=identity(program,key)
 node_pattern=re.compile(r'(?<![A-Za-z0-9_-])'+re.escape(key)+r'(?![A-Za-z0-9_-])')
 source_pattern=re.compile(r'(?<![A-Za-z0-9_-])'+re.escape(program+':'+key)+r'(?![A-Za-z0-9_-])')
 for card in cards:
  text=str(card.get('title',''))+'\n'+str(card.get('body',''))
  if card.get('idempotency_key')==token or source_pattern.search(text) or node_pattern.search(text): return True
 for pr in prs:
  if node.get('pr') and str(node['pr'])==str(pr.get('number')): return True
  if node_pattern.search('\n'.join(str(pr.get(k,'')) for k in ['title','body','headRefName'])): return True
 return False

def duplicate_detail(program,key,node,cards,prs,now=None):
 """Mesma regra de duplicate(); nomeia o motivo. Claim vencido CONTINUA ocupando o no
 (recriar duplicaria trabalho possivelmente vivo); sai como DUP_STALE_CLAIM para o reaper agir."""
 token=identity(program,key)
 node_pattern=re.compile(r'(?<![A-Za-z0-9_-])'+re.escape(key)+r'(?![A-Za-z0-9_-])')
 source_pattern=re.compile(r'(?<![A-Za-z0-9_-])'+re.escape(program+':'+key)+r'(?![A-Za-z0-9_-])')
 for card in cards:
  text=str(card.get('title',''))+'\n'+str(card.get('body',''))
  if card.get('idempotency_key')==token or source_pattern.search(text) or node_pattern.search(text):
   return 'DUP_STALE_CLAIM' if claim_state(card,now)=='STALE' else 'DUP_LIVE'
 return 'DUP_PR' if duplicate(program,key,node,[],prs) else None

def snapshot(home=HOME):
 paths=[home/'kanban.db',*sorted((home/'kanban/boards').glob('*/kanban.db'))]
 all_cards=[];boards={}
 for path in paths:
  board='default' if path==home/'kanban.db' else path.parent.name
  with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as conn:
   conn.row_factory=sqlite3.Row;conn.execute('PRAGMA query_only=ON')
   cards=[dict(r) for r in conn.execute('SELECT * FROM tasks')]
  boards[board]=cards;all_cards.extend(cards)
 return all_cards,boards

def open_prs():
 result=subprocess.run(['gh','pr','list','--repo','Diegooft/Synapse','--state','open','--limit','1000','--json','number,title,body,headRefName,url'],capture_output=True,text=True,encoding='utf-8',timeout=90)
 if result.returncode: raise RuntimeError('PR census unavailable; supply denied')
 rows=json.loads(result.stdout)
 if not isinstance(rows,list) or len(rows)>=1000: raise RuntimeError('PR census possibly truncated')
 return rows

def native(args,body=None):
 # Native documented switch prevents unrelated package installs during admission.
 env={**os.environ,'HERMES_HOME':str(HOME),'PYTHONIOENCODING':'utf-8','HERMES_DISABLE_LAZY_INSTALLS':'1'}
 result=subprocess.run([str(HOME/'bin/hermes.exe'),'kanban',*args],input=body,text=True,encoding='utf-8',capture_output=True,env=env,timeout=120)
 if result.returncode: raise RuntimeError('Native CLI refused: '+result.stderr[-500:])
 return json.loads(result.stdout)

def body_for(program,key,node,source):
 paths=node.get('files',node.get('paths',[]))
 if not isinstance(paths,list):paths=[]
 return '\n'.join(['fonte: '+program+':'+key, 'fonte_arquivo: '+source,
  'Objetivo: '+str(node.get('name',key)), 'Dependências: '+json.dumps(node.get('depends_on',[])),
  'Arquivos autorizados explicitamente no nó: '+json.dumps(paths,ensure_ascii=False),
  'Aceites canônicos: '+json.dumps(node.get('acceptance',[]),ensure_ascii=False),
  'Escopo limitado à fonte. Se faltarem arquivos/decisão/autonomia, bloquear e pedir informação; nunca inventar.',
  'Contrato inicial local-only: publicação/integração requer autorização nativa explícita no card.'])

def source_path(value):
 path=(REPO/str(value)).resolve()
 if not path.is_relative_to(REPO.resolve()) or not path.is_file():raise ValueError('Invalid canonical source path')
 return path

PINS={}  # caminho -> sha256 lido; reverificado antes de criar o card (fonte imutavel entre leitura e uso)

def candidates():
 registry=yaml.safe_load((REPO/'governance/programs.yaml').read_text(encoding='utf-8-sig'))
 programs=registry.get('programs')
 if not isinstance(programs,dict):raise ValueError('Invalid program registry')
 for program,data in programs.items():
  if not isinstance(data,dict) or data.get('status')!='active' or not data.get('graph'):continue
  try:source=source_path(data['graph'])
  except ValueError:raise Refusal('FONTE_INDISPONIVEL','INFRA',{'programa':program,'graph':str(data['graph'])})
  raw,sha=read_pinned(source);PINS[source]=sha
  graph=yaml.safe_load(raw.decode('utf-8-sig'))
  decisions=None
  if data.get('decisions'):
   dpath=REPO/str(data['decisions']);draw,dsha=read_pinned(dpath);PINS[dpath]=dsha
   decisions=yaml.safe_load(draw.decode('utf-8-sig'))
  for key,node in eligible(graph,decisions):yield program,key,node,str(source.relative_to(REPO))
 # Owner input is a short Markdown file, one fenced YAML mapping per objective.
 owner=HOME/'missions/objetivos.md'
 if owner.exists():
  for raw in re.findall(r'```yaml\s*\n(.*?)```',owner.read_text(encoding='utf-8-sig'),re.S):
   node=yaml.safe_load(raw)
   if not isinstance(node,dict) or not isinstance(node.get('id'),str):raise ValueError('Owner objective requires explicit id')
   if not node.get('files') or not node.get('acceptance'):raise ValueError('Owner objective requires files and acceptance')
   for key,obj in eligible({'nodes':{node['id']:node}}):yield 'owner',key,obj,str(owner)

@contextlib.contextmanager
def lock(path):
 import msvcrt
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open('a+b') as file:
  file.seek(0,2)
  if file.tell()==0:file.write(b'0');file.flush()
  file.seek(0)
  try:msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
  except OSError:raise RuntimeError('Supply singleton occupied')
  try:yield
  finally:file.seek(0);msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--dry-run',action='store_true');parser.add_argument('--test-card',action='store_true');args=parser.parse_args()
 config=yaml.safe_load((HOME/'continuity.yaml').read_text(encoding='utf-8-sig'));cfg=config['supply']
 if (HOME/'ESTOP').exists():print(json.dumps({'status':'held','reason':'native ESTOP'}));return
 state=HOME/'state/synapse-supply'
 with lock(state/'supply.lock'):
  all_cards,boards=snapshot()
  board='teste' if args.test_card else cfg['board']
  if board not in boards:raise RuntimeError('Configured native board missing')
  if not args.test_card and backlog_count(boards[board])>=int(cfg['low_watermark']):
   print(json.dumps({'status':'stock_sufficient','board':board,'backlog':backlog_count(boards[board])}));return
  prs=open_prs()
  choices=[('owner','SUPPLY-SMOKE-20261007',{'name':'Teste supply — apenas validar referência, sem implementação','state':'TODO','files':[],'acceptance':['Criar um card Triage idempotente; não alterar código nem publicar PR.']},'Diego: Plano1.2 item02')] if args.test_card else candidates()
  for program,key,node,source in choices:
   if duplicate(program,key,node,all_cards,prs):continue
   for pinned,sha in list(PINS.items()):verify_pinned(pinned,sha)
   if args.dry_run:print(json.dumps({'status':'candidate','source':program+':'+key,'board':board}));return
   card=native(['--board',board,'create',str(node.get('name',key))[:160], '--body-file','-', '--triage','--idempotency-key',identity(program,key),'--created-by','synapse-supply','--workspace','scratch','--completion-contract','local-only','--json'],body_for(program,key,node,source))
   if not isinstance(card,dict) or not card.get('id'):raise RuntimeError('Native card receipt missing')
   record={'status':'em fila','source':program+':'+key,'source_file':source,'board':board,'card_id':card['id'],'native_status':card.get('status'),'at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
   # Derived receipt only. Canonical graph and native card remain the authorities.
   target=state/(hashlib.sha256(identity(program,key).encode()).hexdigest()+'.json')
   temp=target.with_suffix('.tmp');temp.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,target)
   print(json.dumps(record,ensure_ascii=False));return
  print(json.dumps({'status':'no_eligible_unduplicated_node','board':board}))

if __name__=='__main__':
 try:main()
 except Refusal as r:
  print(json.dumps(r.record(),ensure_ascii=False));raise SystemExit(2)
