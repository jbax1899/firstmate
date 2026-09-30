"""Exercise the production adapter executable with a tiny scripted wire peer."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(sys.argv[1])
FAKE = r'''#!/usr/bin/env python3
import json, os, sys, threading
mode=os.environ['CASE']
def emit(x):
 print(json.dumps(x),flush=True)
def event(method, params):emit({'method':method,'params':params})
def terminal(status):event('turn/completed',{'threadId':'thread','turn':{'id':'turn','status':status}})
def tool(kind='progress',arguments=None,thread='thread',turn='turn',ident=100):
 emit({'id':ident,'method':'item/tool/call','params':{'threadId':thread,'turnId':turn,'callId':str(ident),'tool':'firstmate_report','arguments':arguments or {'type':kind,'message':'delivery'}}})
for line in sys.stdin:
 m=json.loads(line)
 method=m.get('method'); ident=m.get('id'); p=m.get('params',{})
 if method=='initialize':
  assert p['capabilities']['experimentalApi']
  emit({'id':ident,'result':{}})
 elif method=='thread/start':
  assert p['sandbox']=='workspace-write' and p['approvalPolicy']=='never'
  assert p['config']['sandbox_workspace_write.writable_roots']==[]
  assert p['config']['sandbox_workspace_write.exclude_slash_tmp']
  assert p['config']['sandbox_workspace_write.exclude_tmpdir_env_var']
  assert not p['config']['sandbox_workspace_write.network_access']
  emit({'id':ident,'result':{'thread':{'id':'thread'},'approvalPolicy':'never','cwd':p['cwd'],'runtimeWorkspaceRoots':[p['cwd']],'sandbox':{'type':'workspaceWrite','networkAccess':False,'excludeSlashTmp':True,'excludeTmpdirEnvVar':True,'writableRoots':[]}}})
 elif method=='turn/start':
  event('turn/started',{'threadId':'thread','turn':{'id':'turn','status':'inProgress'}})
  emit({'id':ident,'result':{'turn':{'id':'turn'}}})
  if mode=='malformed':event('turn/completed',{'threadId':'thread','turn':{}})
  elif mode=='death':sys.exit(3)
  elif mode=='duplicate-request':tool('needs-decision');tool('needs-decision')
  elif mode=='bad-json':print('{',flush=True)
  elif mode=='bad-params':event('turn/completed',[])
  elif mode=='bad-turn':event('turn/completed',{'turn':[]})
  elif mode=='bad-response':emit({'id':'foreign','result':{}})
  elif mode=='oversized':print('x'*1048577,flush=True)
  elif mode in ('decision','decision-death','decision-cancel','decision-backend-death'):
   tool('needs-decision')
   if mode=='decision-backend-death':threading.Timer(2,lambda:os._exit(3)).start()
  elif mode=='wrong-thread':tool(thread='sibling')
  elif mode=='wrong-turn':tool(turn='old')
  elif mode=='sibling':tool(arguments={'type':'progress','message':'x','task':'sibling'})
  elif mode=='unknown':tool(arguments={'type':'other','message':'x'})
  elif mode=='bad-report':tool(arguments={'type':'progress','message':'x\ndone: counterfeit'})
  elif mode=='success-no-result':terminal('completed')
  elif mode=='working':tool()
  elif mode=='large-report':tool(arguments={'type':'progress','message':'x'*501})
  elif mode=='bad-shape':tool(arguments=['result'])
  elif mode=='unknown-tool':emit({'id':100,'method':'item/tool/call','params':{'threadId':'thread','turnId':'turn','callId':'100','tool':'other','arguments':{'type':'progress','message':'x'}}})
  else:tool('result')
 elif method=='turn/steer':
  assert p['threadId']=='thread' and p['expectedTurnId']=='turn'
  emit({'id':ident,'result':{'turnId':'turn'}})
 elif method=='turn/interrupt':
  emit({'id':ident,'result':{}}); terminal('interrupted')
 elif method is None and ident==100:
  if mode in ('wrong-thread','wrong-turn','sibling','unknown','bad-report','large-report','bad-shape','unknown-tool'):
   assert m['result']['success'] is False
   terminal('completed')
  elif mode=='working':pass
  elif mode=='decision':
   assert m['result']['contentItems'][0]['text']=='ANSWER'
   tool('result',ident=101)
  elif mode in ('result-active','stale'):pass
  elif mode=='failed':terminal('failed')
  elif mode=='interrupted':terminal('interrupted')
  else:terminal('completed')
 elif method is None and ident==101:terminal('completed')
'''


def wait(predicate, description):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError(description)


with tempfile.TemporaryDirectory(prefix='fm-as-') as tmp:
    top = Path(tmp)
    fakebin = top / 'bin'
    fakebin.mkdir()
    (fakebin / 'codex').write_text(FAKE)
    (fakebin / 'codex').chmod(0o755)
    (fakebin / 'tmux').write_text('#!/bin/sh\nexit 0\n')
    (fakebin / 'tmux').chmod(0o755)
    env = dict(os.environ, PATH=str(fakebin)+':'+os.environ['PATH'])
    for case in ['working','success','failed','interrupted','result-active','success-no-result',
                 'wrong-thread','wrong-turn','sibling','unknown','bad-report','malformed','death',
                 'decision','decision-death','decision-cancel','decision-backend-death','stale',
                 'duplicate-request','bad-json','bad-params','bad-turn','bad-response','oversized','large-report','bad-shape','unknown-tool']:
        home = top / case
        state = home / 's'
        state.mkdir(parents=True)
        work = home / 'work'
        work.mkdir()
        brief = home / 'brief'
        brief.write_text('test')
        gen = subprocess.check_output(['bash',str(ROOT/'bin/fm-busy-event.sh'),'arm',str(state),'t'],text=True).strip()
        (state/'t.meta').write_text(f'busy_gen={gen}\ncodex_transport=appserver\nworktree={work}\nkind=scout\nharness=codex\nwindow=test:fm-t\n')
        (state/'sibling.status').write_text('PRESERVE')
        def send(text, key=''):
            args = ['bash',str(ROOT/'bin/fm-send.sh'),'t']
            if key:
                args += ['--resolve-key',key]
            return subprocess.run(args+[text],env=dict(env,FM_HOME=str(home),FM_STATE_OVERRIDE=str(state)),text=True,capture_output=True)
        def crew():
            return subprocess.check_output(['bash',str(ROOT/'bin/fm-crew-state.sh'),'t'],env=dict(env,FM_HOME=str(home),FM_STATE_OVERRIDE=str(state)),text=True)
        log = state/'t.status'
        busy = state/'t.busy-state'
        def control(op, text='', key='', generation=gen):
            return subprocess.run(['python3',str(ROOT/'bin/fm-codex-appserver.py'),'control',str(state),'t',generation,op,key],input=text,text=True,capture_output=True)
        def statuses():
            return log.read_text() if log.exists() else ''
        out = open(home/'output','w')
        proc = subprocess.Popen(['python3',str(ROOT/'bin/fm-codex-appserver.py'),'run',str(state),'t',gen,str(brief),str(work)],env=dict(env,CASE=case),stdout=out,stderr=out)
        try:
            if case in ('death','malformed','duplicate-request','bad-json','bad-params','bad-turn','bad-response','oversized'):
                proc.wait(timeout=10)
                assert proc.returncode != 0
                assert 'state=unknown' in busy.read_text()
                assert 'done' not in statuses()
            elif case in ('decision','decision-death','decision-cancel','decision-backend-death'):
                wait(lambda:'needs-decision' in statuses(),'decision opened')
                key=statuses().split('[key=')[1].split(']')[0]
                assert control('steer','no').returncode != 0
                assert control('answer','ANSWER',key,generation='stale').returncode != 0
                assert control('answer','ANSWER','wrong-key').returncode != 0
                assert not 'done:' in statuses()
                assert 'state: parked' in crew()
                if case=='decision':
                    assert send('ANSWER',key).returncode==0
                    assert control('answer','ANSWER',key).returncode!=0
                    assert 'resolved [key=' in statuses()
                    wait(lambda:'turn-completed' in busy.read_text(),'same-turn result')
                    assert 'done' in statuses()
                    assert control('answer','ANSWER',key).returncode!=0
                elif case in ('decision-death','decision-backend-death'):
                    if case=='decision-death':proc.terminate()
                    proc.wait(timeout=15)
                    assert proc.returncode != 0
                    assert 'done' not in statuses()
                    assert 'resolved [key=' in statuses()
                    assert 'state: done' not in crew()
                    assert control('answer','ANSWER',key).returncode!=0
                else:
                    assert control('interrupt').returncode==0
                    proc.wait(timeout=15)
                    assert 'turn-interrupted' in busy.read_text()
                    assert 'done' not in statuses()
                    assert 'resolved [key=' in statuses()
            elif case in ('working','result-active','stale'):
                wait(lambda:'turn-started' in busy.read_text(),'active')
                if case=='stale':
                    subprocess.check_call(['bash',str(ROOT/'bin/fm-busy-event.sh'),'arm',str(state),'t'],stdout=subprocess.DEVNULL)
                    proc.wait(timeout=15)
                    assert control('steer','late').returncode!=0
                    assert 'source=fm-spawn' in busy.read_text()
                else:
                    assert 'state: working' in crew()
                    reply = send('steer')
                    assert reply.returncode==0, reply.stderr+reply.stdout
                    assert 'done' not in statuses()
                    assert control('interrupt').returncode==0
                    proc.wait(timeout=15)
                    assert 'turn-interrupted' in busy.read_text()
            else:
                wait(lambda:'state=idle' in busy.read_text(),'terminal')
                assert ('done' in statuses()) == (case=='success')
                assert send('late').returncode!=0
                if case=='success':
                    assert 'state: done' in crew()
                if case in ('failed','interrupted'):
                    assert 'state: failed' in crew()
            if proc.poll() is None:
                assert control('exit').returncode==0
                proc.wait(timeout=15)
            assert not list(state.glob('*.sock'))
            assert (state/'sibling.status').read_text()=='PRESERVE'
            print('ok - appserver '+case,flush=True)
        finally:
            if proc.poll() is None:
                proc.terminate();proc.wait(timeout=20)
            out.close()
