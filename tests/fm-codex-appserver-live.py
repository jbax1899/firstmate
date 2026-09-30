"""Real worker canary through FirstMate's production interfaces only."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

root = Path(sys.argv[1]).resolve()
lab = Path(tempfile.mkdtemp(prefix='fm-as-canary-'))
env = {k: v for k, v in os.environ.items() if not k.startswith('FM_') and k not in ('TMUX','TMUX_PANE','TASKS_AXI_FILE','TASKS_AXI_BACKEND')}
env['FM_HOME'] = str(lab)


def run(*args, check=True, timeout=60, cwd=root):
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(' '.join(args)+': '+result.stdout+result.stderr)
    return result.stdout.strip() if check else result


def fm(script, *args, **kwargs):
    return run('bash',str(root/'bin'/script),*args,**kwargs)


def wait(predicate, description, seconds=150):
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate():
            print('ok - live '+description,flush=True)
            return
        time.sleep(.25)
    raise AssertionError(description)


def status(task):
    return fm('fm-crew-state.sh',task)


def log(task):
    path=lab/'state'/f'{task}.status'
    return path.read_text() if path.exists() else ''


def capture(task):
    return run('tmux','capture-pane','-p','-J','-t','firstmate:fm-'+task,'-S','-2000')


def spawn(task, instruction, model=''):
    folder=lab/'data'/task
    folder.mkdir()
    (folder/'brief.md').write_text("# Task\n## Captain's intent\nVerify the real supervised app-server worker.\n## Firstmate spec\n"+instruction+'\n')
    args=[task,str(project),'--scout','--harness','codex','--effort','low','--backend','tmux','--codex-appserver']
    if model:
        args+=['--model',model]
    print(fm('fm-spawn.sh',*args,timeout=120),flush=True)
    tasks.append(task)


def stop(task):
    result=fm('fm-control.sh',task,'exit',check=False)
    if result.returncode:
        raise RuntimeError(result.stdout+result.stderr)
    print(result.stdout.strip(),flush=True)
    match=re.search(r'pid=(\d+) exit=0',result.stdout)
    assert match, result.stdout
    assert not Path('/proc/'+match[1]).exists(), 'orphan app-server'
    wait(lambda:'already-stopped' in fm('fm-control.sh',task,'exit'), 'idempotent exit')


tasks=[]
try:
    fm('fm-lab-home.sh','create',str(lab))
    env['TMUX_TMPDIR']=fm('fm-lab-home.sh','tmux-dir',str(lab))
    (lab/'config/backlog-backend').write_text('manual\n')
    project=lab/'projects/canary'
    project.mkdir()
    run('git','init','-b','main',cwd=project)
    (project/'README.md').write_text('Disposable app-server canary.\n')
    # Keep pool slots inside the disposable repository, never the shared pool.
    (project/'treehouse.toml').write_text('max_trees = 4\nroot = "."\n')
    run('git','add','README.md','treehouse.toml',cwd=project)
    run('git','-c','user.name=Canary','-c','user.email=canary@example.invalid','commit','-m','Initialize canary',cwd=project)
    (lab/'data/projects.md').write_text('- canary [local-only] - Disposable verification\n')
    sentinel=lab/'state/sibling.status'
    sentinel.write_text('PRESERVE\n')
    # A checked-in helper is unnecessary: the exact harmless security command
    # is part of this disposable task's instructions and its real tool trace.
    command="""python3 - <<'PYSEC'
import glob,json,socket
from pathlib import Path
result={}
Path('positive-control').write_text('WORKSPACE_OK')
result['workspace']='WORKSPACE_OK'
try:
 Path(SENTINEL).write_text('ATTACK')
 result['sibling']='WRITE_SUCCEEDED'
except OSError as e:
 result['sibling']='DENIED:'+str(e.errno)
s=socket.socket(socket.AF_UNIX)
try:
 s.connect(glob.glob(SOCKETS)[0])
 result['socket']='CONNECTED'
except OSError as e:
 result['socket']='DENIED:'+str(e.errno)
finally:
 s.close()
print(json.dumps(result))
Path('security-proof.json').write_text(json.dumps(result))
PYSEC""".replace('SENTINEL',repr(str(sentinel))).replace('SOCKETS',repr(str(lab/'state/.appserver-*.sock')))
    spawn('success','Call firstmate_report progress CANARY_STARTED. Execute sleep 15 for an active steer. Then call firstmate_report needs-decision CANARY_QUESTION and wait for the answer. Then execute this exact harmless sandbox check:\n'+command+'\nFinally report result including the steer marker, answer, and security outcomes. No commits, pushes, PRs, or direct FirstMate state writes.')
    wait(lambda:'state: working' in status('success'),'working projection')
    steer=fm('fm-send.sh','success','Include UNIQUE_STEER_418 in the final result. Continue the brief.')
    turn=steer.split()[-1]
    print('ok - live steer '+turn,flush=True)
    wait(lambda:'needs-decision' in log('success'),'decision opens')
    assert 'state: parked' in status('success')
    assert 'done' not in log('success')
    key=re.search(r'\[key=([^]]+)\]',log('success')).group(1)
    assert fm('fm-send.sh','success','late ordinary steer',check=False).returncode!=0
    fm('fm-send.sh','success','--resolve-key',key,'UNIQUE_ANSWER_73921')
    assert fm('fm-send.sh','success','--resolve-key',key,'duplicate',check=False).returncode!=0
    wait(lambda:'state: done' in status('success'),'same-turn completion')
    assert 'UNIQUE_STEER_418' in log('success') and 'UNIQUE_ANSWER_73921' in log('success')
    trace=capture('success')
    assert '"id": "'+turn+'"' in trace and '"status": "completed"' in trace
    meta=dict(line.split('=',1) for line in (lab/'state/success.meta').read_text().splitlines() if '=' in line)
    proof=json.loads((Path(meta['worktree'])/'security-proof.json').read_text())
    assert proof['workspace']=='WORKSPACE_OK'
    assert proof['sibling'].startswith('DENIED:') and proof['socket'].startswith('DENIED:')
    assert sentinel.read_text()=='PRESERVE\n'
    assert 'commandExecution' in trace and 'DENIED' in trace
    assert fm('fm-send.sh','success','after completion',check=False).returncode!=0
    (lab/'success-trace.txt').write_text(trace)
    print('ok - live kernel sandbox denial '+json.dumps(proof),flush=True)
    stop('success')
    spawn('failure','Do not execute commands.','firstmate-deliberately-unavailable')
    wait(lambda:'state: failed' in status('failure'),'failed turn never complete')
    assert 'done' not in log('failure')
    (lab/'failure-trace.txt').write_text(capture('failure'))
    stop('failure')
    spawn('cancel','Call firstmate_report needs-decision WAIT_FOR_CANCEL and wait. Do not use shell tools.')
    wait(lambda:'state: parked' in status('cancel'),'cancel pending decision')
    stop('cancel')
    assert 'state: failed' in status('cancel') and 'done' not in log('cancel')
    (lab/'cancel-trace.txt').write_text(capture('cancel'))
    assert '"status": "interrupted"' in capture('cancel')
    assert 'resolved [key=' in log('cancel')
    assert not list((lab/'state').glob('*.sock'))
    print('ok - live cancellation retires callbacks and reaps app-server',flush=True)
    print('CANARY_PASS evidence='+str(lab),flush=True)
finally:
    for task in tasks:
        fm('fm-control.sh',task,'exit',check=False)
    if 'TMUX_TMPDIR' in env:
        run('tmux','kill-server',check=False)
        fm('fm-lab-home.sh','teardown',str(lab),check=False)
    print('Retained disposable task evidence: '+str(lab),flush=True)
