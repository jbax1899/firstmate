"""Exercise the production adapter executable with a tiny scripted wire peer."""
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(sys.argv[1])
sys.path.insert(0, str(ROOT / "bin"))
from fm_codex_git import PrivateGit


def real_git(directory, *args):
    return subprocess.check_output(
        ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
         "-C", str(directory), *args], env=dict(os.environ,
         GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull))


def private_git_delivery_check():
    with tempfile.TemporaryDirectory(prefix="fm-private-git-") as tmp:
        top = Path(tmp)
        repo = top / "repo"
        repo.mkdir()
        real_git(repo, "init", "--quiet", "-b", "main")
        for key, value in (("user.name", "FirstMate Test"), ("user.email", "fm-test@example.invalid")):
            real_git(repo, "config", key, value)
        (repo / "tracked.txt").write_text("base\n")
        real_git(repo, "add", "tracked.txt")
        real_git(repo, "commit", "--quiet", "-m", "base")
        base = real_git(repo, "rev-parse", "HEAD").decode().strip()
        real_git(repo, "branch", "task-ship")
        real_git(repo, "branch", "sibling-task")
        work = top / "task-worktree"
        sibling = top / "sibling-worktree"
        real_git(repo, "worktree", "add", "--quiet", str(work), "task-ship")
        real_git(repo, "worktree", "add", "--quiet", str(sibling), "sibling-task")
        sibling_head = real_git(sibling, "rev-parse", "HEAD").decode().strip()
        private_parent = Path(tempfile.mkdtemp(prefix="fm-private-git-", dir="/dev/shm"))
        helper = None
        try:
            helper = PrivateGit(work, private_parent / "task", "task-ship")
            assert os.stat(private_parent).st_dev != os.stat(repo).st_dev
            assert real_git(work, "branch", "--show-current").decode().strip() == "task-ship"
            (work / "tracked.txt").write_text("task change\n")
            real_git(work, "add", "tracked.txt")
            real_git(work, "commit", "--quiet", "-m", "task delivery")
            oid = helper.publish(private_parent)
            assert real_git(repo, "rev-parse", "refs/heads/task-ship").decode().strip() == oid
            assert real_git(sibling, "rev-parse", "HEAD").decode().strip() == sibling_head
            assert real_git(work, "rev-parse", "HEAD").decode().strip() == oid
            assert real_git(work, "status", "--porcelain").strip() == b""
        finally:
            if helper:
                helper.restore()
            shutil.rmtree(private_parent, ignore_errors=True)
        # An extra ref is confined to the private store and makes publication fail.
        work2 = top / "unsafe-worktree"
        real_git(repo, "branch", "task-unsafe", base)
        real_git(repo, "worktree", "add", "--quiet", str(work2), "task-unsafe")
        private_parent2 = Path(tempfile.mkdtemp(prefix="fm-private-git-", dir="/dev/shm"))
        helper2 = None
        try:
            helper2 = PrivateGit(work2, private_parent2 / "task", "task-unsafe")
            (work2 / "tracked.txt").write_text("unsafe change\n")
            real_git(work2, "add", "tracked.txt")
            real_git(work2, "commit", "--quiet", "-m", "task delivery")
            private_head = real_git(work2, "rev-parse", "HEAD").decode().strip()
            real_git(work2, "update-ref", "refs/heads/unrelated", private_head)
            try:
                helper2.publish(private_parent2)
            except ValueError:
                pass
            else:
                raise AssertionError("unrelated private ref was accepted")
            assert real_git(repo, "rev-parse", "refs/heads/sibling-task").decode().strip() == sibling_head
            assert subprocess.run(["git", "-C", str(repo), "show-ref", "--verify",
                                   "refs/heads/unrelated"], capture_output=True).returncode != 0
        finally:
            if helper2:
                helper2.restore()
            shutil.rmtree(private_parent2, ignore_errors=True)


private_git_delivery_check()
FAKE = r'''#!/usr/bin/env python3
import json, os, sys, threading, subprocess
mode=os.environ['CASE']
committed=False
def commit_ship():
 global committed
 if committed or mode.startswith('scout-'): return
 work=os.environ['CASE_WORKTREE']
 with open(os.path.join(work,'tracked.txt'),'w') as f: f.write('app-server delivery\n')
 subprocess.run(['git','-C',work,'add','tracked.txt'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 subprocess.run(['git','-C',work,'commit','-m','app-server delivery'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 committed=True
def emit(x):
 print(json.dumps(x),flush=True)
def event(method, params):emit({'method':method,'params':params})
def terminal(status):event('turn/completed',{'threadId':'thread','turn':{'id':'turn','status':status}})
def tool(kind='progress',arguments=None,thread='thread',turn='turn',ident=100):
 if kind=='result': commit_ship()
 emit({'id':ident,'method':'item/tool/call','params':{'threadId':thread,'turnId':turn,'callId':str(ident),'tool':'firstmate_report','arguments':arguments or {'type':kind,'message':'delivery'}}})
for line in sys.stdin:
 m=json.loads(line)
 method=m.get('method'); ident=m.get('id'); p=m.get('params',{})
 if method=='initialize':
  assert p['capabilities']['experimentalApi']
  emit({'id':ident,'result':{}})
 elif method=='thread/start':
  assert p['sandbox']=='workspace-write' and p['approvalPolicy']=='never'
  roots=p['config']['sandbox_workspace_write.writable_roots']
  assert all(os.path.isabs(root) for root in roots)
  assert p['config']['sandbox_workspace_write.exclude_slash_tmp']
  assert p['config']['sandbox_workspace_write.exclude_tmpdir_env_var']
  assert not p['config']['sandbox_workspace_write.network_access']
  emit({'id':ident,'result':{'thread':{'id':'thread'},'approvalPolicy':'never','cwd':p['cwd'],'runtimeWorkspaceRoots':[p['cwd']]+roots,'sandbox':{'type':'workspaceWrite','networkAccess':False,'excludeSlashTmp':True,'excludeTmpdirEnvVar':True,'writableRoots':roots}}})
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
  elif mode=='oversized':print('x'*2000000,flush=True)
  elif mode in ('decision','decision-held','decision-held-legacy','decision-large','decision-death','decision-cancel','decision-backend-death','interrupt-error','interrupt-timeout'):
   tool('needs-decision')
   if mode=='decision-backend-death':threading.Timer(2,lambda:os._exit(3)).start()
  elif mode=='wrong-thread':tool(thread='sibling')
  elif mode=='wrong-turn':tool(turn='old')
  elif mode=='sibling':tool(arguments={'type':'progress','message':'x','task':'sibling'})
  elif mode=='unknown':tool(arguments={'type':'other','message':'x'})
  elif mode=='bad-report':tool(arguments={'type':'progress','message':'x\ndone: counterfeit'})
  elif mode.startswith('scout-'):
   if mode=='scout-stale':open(os.path.join(os.environ['CASE_STATE'],'t.busy-gen'),'w').write('stale\n')
   arguments={'type':'result','message':'scout conclusion'}
   if mode!='scout-missing':arguments['report']='# Findings\n'+('evidence line\n'*1000)
   if mode=='scout-path':arguments['path']='../sibling/report.md'
   if mode=='scout-large':arguments['report']='x'*262145
   tool(arguments=arguments)
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
  if mode=='interrupt-error':
   emit({'id':ident,'error':{'code':-1,'message':'interrupt rejected'}});continue
  if mode=='interrupt-timeout':continue
  emit({'id':ident,'result':{}}); terminal('interrupted')
 elif method is None and ident==100:
  if mode in ('wrong-thread','wrong-turn','sibling','unknown','bad-report','large-report','bad-shape','unknown-tool'):
   assert m['result']['success'] is False
   terminal('completed')
  elif mode in ('scout-missing','scout-path','scout-large','scout-stale','scout-preexisting'):
   assert m['result']['success'] is False
   if mode=='scout-stale':
    open(os.path.join(os.environ['CASE_STATE'],'t.busy-gen'),'w').write(os.environ['CASE_GEN']+'\n')
   terminal('completed')
  elif mode=='scout-duplicate':
   assert m['result']['success'] is True
   tool(arguments={'type':'result','message':'overwrite','report':'WRONG'},ident=101)
  elif mode=='working':pass
  elif mode in ('decision','decision-held','decision-held-legacy','decision-large'):
   assert m['result']['contentItems'][0]['text']==('😀'*2048 if mode=='decision-large' else 'ANSWER')
   tool('result',ident=101)
  elif mode in ('result-active','stale'):pass
  elif mode=='failed':terminal('failed')
  elif mode=='interrupted':terminal('interrupted')
  else:terminal('completed')
 elif method is None and ident==101:
  if mode=='scout-duplicate':assert m['result']['success'] is False
  terminal('completed')
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
    for case in ['scout-stale','scout-success','scout-missing','scout-path','scout-large','scout-duplicate','scout-preexisting','working','success','failed','interrupted','result-active','success-no-result',
                 'wrong-thread','wrong-turn','sibling','unknown','bad-report','malformed','death',
                 'decision','decision-held','decision-held-legacy','decision-large','decision-death','decision-cancel','decision-backend-death','interrupt-error','interrupt-timeout','stale',
                 'duplicate-request','bad-json','bad-params','bad-turn','bad-response','oversized','large-report','bad-shape','unknown-tool']:
        home = top / case
        state = home / 's'
        state.mkdir(parents=True)
        (home/'data').mkdir()
        (home/'ambient-data').mkdir()
        if case=='scout-preexisting':
            (home/'data'/'t').mkdir()
            (home/'data'/'t'/'report.md').write_text('preserve existing\n')
        shutil.copyfile(ROOT/'.tasks.toml', home/'.tasks.toml')
        work = home / 'work'
        is_scout = case.startswith('scout-')
        branch = ''
        if is_scout:
            work.mkdir()
        else:
            repo = home / 'repo'
            repo.mkdir()
            real_git(repo, 'init', '--quiet', '-b', 'main')
            real_git(repo, 'config', 'user.name', 'FirstMate Test')
            real_git(repo, 'config', 'user.email', 'fm-test@example.invalid')
            (repo/'tracked.txt').write_text('base\n')
            real_git(repo, 'add', 'tracked.txt')
            real_git(repo, 'commit', '--quiet', '-m', 'base')
            branch = 'fm/t'
            real_git(repo, 'branch', branch)
            real_git(repo, 'worktree', 'add', '--quiet', str(work), branch)
        brief = home / 'brief'
        brief.write_text('test')
        gen = subprocess.check_output(['bash',str(ROOT/'bin/fm-busy-event.sh'),'arm',str(state),'t'],text=True).strip()
        (state/'t.meta').write_text(f'busy_gen={gen}\ncodex_transport=appserver\nworktree={work}\nkind={'scout' if case.startswith('scout-') else 'secondmate'}\nharness=codex\nwindow=test:fm-t\n')
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
        proc = subprocess.Popen(['python3',str(ROOT/'bin/fm-codex-appserver.py'),'run',str(state),'t',gen,str(home/'data'),str(brief),str(work)],env=dict(env,CASE=case,FM_HOME=str(home),FM_DATA_OVERRIDE=str(home/'ambient-data'),CASE_STATE=str(state),CASE_GEN=gen,CASE_WORKTREE=str(work)),stdout=out,stderr=out)
        try:
            if case == 'scout-stale':
                proc.wait(timeout=10)
                assert proc.returncode != 0
                assert 'done' not in statuses()
                assert not (home/'data'/'t'/'report.md').exists()
                (state/'t.busy-gen').write_text(gen+'\n')
            elif case in ('death','malformed','duplicate-request','bad-json','bad-params','bad-turn','bad-response','oversized'):
                proc.wait(timeout=10)
                assert proc.returncode != 0
                assert 'state=unknown' in busy.read_text()
                assert 'done' not in statuses()
            elif case in ('decision','decision-held','decision-held-legacy','decision-large','decision-death','decision-cancel','decision-backend-death','interrupt-error','interrupt-timeout'):
                wait(lambda:'needs-decision' in statuses(),'decision opened')
                key=statuses().split('[key=')[1].split(']')[0]
                assert control('steer','no').returncode != 0
                assert control('answer','ANSWER',key,generation='stale').returncode != 0
                assert control('answer','ANSWER','wrong-key').returncode != 0
                assert not 'done:' in statuses()
                if case != 'decision':
                    assert 'state: parked' in crew()
                if case in ('decision','decision-held','decision-held-legacy','decision-large'):
                    answer='😀'*2048 if case=='decision-large' else 'ANSWER'
                    if case.startswith('decision-held'):
                        held = 't-decision-' + key if case.endswith('legacy') else key
                        hold_env=dict(env,FM_HOME=str(home),FM_STATE_OVERRIDE=str(state))
                        (home/'data/backlog.md').write_text('## In flight\n\n## Queued\n\n## Done\n')
                        def hold(*args):
                            return subprocess.run(['bash',str(ROOT/'bin/fm-captain-hold.sh'),*args],env=hold_env,text=True,capture_output=True)
                        created=hold('hold',held,'--title','Decision','--reason','Choose','--origin','t')
                        assert created.returncode==0, created.stderr
                        transferred=hold('complete','t',held)
                        assert transferred.returncode==0, transferred.stderr
                        real_tasks=shutil.which('tasks-axi')
                        wrapper=fakebin/'tasks-axi'
                        wrapper.write_text('#!/bin/sh\nfor arg do\n if [ "$arg" = done ]; then exit 42; fi\ndone\nexec ' + str(real_tasks) + ' "$@"\n')
                        wrapper.chmod(0o755)
                        try:
                            failed=send(answer,key)
                            assert failed.returncode!=0, failed.stdout
                            time.sleep(.5)
                            assert 'done [' not in statuses(), statuses()
                            assert 'turn-completed-result' not in busy.read_text(), busy.read_text()
                            assert hold('open',held).returncode==0
                            assert control('answer','DIFFERENT',key).returncode!=0
                        finally:
                            wrapper.unlink()
                    reply=send(answer,key)
                    assert reply.returncode==0, reply.stderr+reply.stdout
                    if case.startswith('decision-held'):
                        assert 'captain-held [key=' in statuses()
                        assert hold('open',held).returncode in (1,3)
                    else:
                        assert 'resolved [key=' in statuses()
                    wait(lambda:'turn-completed' in busy.read_text(),'same-turn result')
                    assert 'done' in statuses()
                    assert control('answer','ANSWER',key).returncode!=0
                elif case in ('decision-death','decision-backend-death'):
                    if case=='decision-death':proc.terminate()
                    proc.wait(timeout=15)
                    assert proc.returncode != 0
                    assert 'done' not in statuses()
                    if case.startswith('decision-held'):
                        assert 'captain-held [key=' in statuses()
                        assert hold('open',held).returncode in (1,3)
                    else:
                        assert 'resolved [key=' in statuses()
                    assert 'state: done' not in crew()
                    assert control('answer','ANSWER',key).returncode!=0
                elif case.startswith('interrupt-'):
                    started=time.monotonic()
                    assert control('interrupt').returncode!=0
                    proc.wait(timeout=20)
                    assert time.monotonic()-started < 55
                    assert 'state=unknown' in busy.read_text()
                    assert 'interrupt-unverified' in busy.read_text()
                    if case.startswith('decision-held'):
                        assert 'captain-held [key=' in statuses()
                        assert hold('open',held).returncode in (1,3)
                    else:
                        assert 'resolved [key=' in statuses()
                    assert 'done:' not in statuses()
                    assert control('answer','ANSWER',key).returncode!=0
                    assert 'state: done' not in crew()
                else:
                    assert control('interrupt').returncode==0
                    proc.wait(timeout=15)
                    assert 'turn-interrupted' in busy.read_text()
                    assert 'done' not in statuses()
                    if case.startswith('decision-held'):
                        assert 'captain-held [key=' in statuses()
                        assert hold('open',held).returncode in (1,3)
                    else:
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
                    for text in ('x'*8192, '\x01'*8192, '"'*8192, '\\'*8192, '\U0001f600'*2048):
                        response=control('steer',text)
                        assert response.returncode==0, response.stderr+response.stdout
                    assert control('steer','x'*8193).returncode!=0
                    reply = send('steer')
                    assert reply.returncode==0, reply.stderr+reply.stdout
                    assert 'done' not in statuses()
                    assert control('interrupt').returncode==0
                    proc.wait(timeout=15)
                    assert 'turn-interrupted' in busy.read_text()
            else:
                wait(lambda:'state=idle' in busy.read_text() or proc.poll() is not None,'terminal '+case)
                assert ('done' in statuses()) == (case in ('success','scout-success','scout-duplicate')), statuses()+chr(10)+busy.read_text()+chr(10)+(home/'output').read_text()
                report=home/'data'/'t'/'report.md'
                assert not (home/'ambient-data'/'t'/'report.md').exists()
                if case in ('scout-success','scout-duplicate'):
                    assert report.read_text()=='# Findings\n'+('evidence line\n'*1000)
                elif case=='scout-preexisting':
                    assert report.read_text()=='preserve existing\n'
                elif case.startswith('scout-'):
                    assert not report.exists()
                assert send('late').returncode!=0
                if case=='success':
                    assert 'state: done' in crew()
                if case in ('failed','interrupted'):
                    assert 'state: failed' in crew()
            if proc.poll() is None:
                assert control('exit').returncode==0
                proc.wait(timeout=15)
            output=(home/'output').read_text()
            server_pid=int(output.split('app-server started pid=')[1].splitlines()[0])
            try:
                os.kill(server_pid,0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError('orphan app-server peer')
            assert not list(state.glob('*.sock'))
            assert (state/'sibling.status').read_text()=='PRESERVE'
            print('ok - appserver '+case,flush=True)
        finally:
            if proc.poll() is None:
                proc.terminate();proc.wait(timeout=20)
            out.close()
