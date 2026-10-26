"""Authenticated, isolated source editing with validation and rollback."""
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
import store

LOCK = threading.Lock()
VENV_PYTHON = store.STATE / 'venv/bin/python'
CODEX = Path('/home/bg/.local/bin/codex').resolve()
NODE = Path('/home/bg/.nvm/versions/node/v22.22.0/bin/node')
ALLOWED_ROOT = {'app.py','store.py','payments.py','addresses.py'}


def update(job,status=None,text=None):
    with store.connect(True) as c:
        if text:
            c.execute('UPDATE jobs SET output=substr(output || ?, -80000) WHERE id=?',(text+'\n',job))
        if status:
            c.execute('UPDATE jobs SET status=?,finished_at=? WHERE id=?',(status,store.now() if status in ('completed','failed') else None,job))


def writable(relative):
    return relative.as_posix() in ALLOWED_ROOT or (relative.parts[0] in ('templates','assets') and relative.suffix in ('.html','.txt','.css','.js','.svg','.json'))


def run(job,prompt):
    working = store.STATE / 'ai' / job
    auth = store.STATE / 'ai-auth'
    rollback = store.STATE / 'ai-rollback' / job
    try:
        auth.mkdir(parents=True,exist_ok=True,mode=0o700)
        if not (auth/'auth.json').exists():
            shutil.copy2(Path.home()/'.codex/auth.json',auth/'auth.json')
            (auth/'auth.json').chmod(0o600)
        (auth/'guard-log.jsonl').write_text('')
        (auth/'config.toml').write_text('''sandbox_mode = "workspace-write"
model_reasoning_effort = "medium"
[features]
codex_apps = false
[[hooks.PreToolUse]]
matcher = "*"
[[hooks.PreToolUse.hooks]]
type = "command"
command = "/home/bg/.nvm/versions/node/v22.22.0/bin/node /guard/editor_guard.mjs"
timeoutSec = 20
''')
        working.mkdir(parents=True,exist_ok=True)
        for source in store.ROOT.iterdir():
            if source.name in {'templates','assets','tests'}:
                shutil.copytree(source,working/source.name)
            elif source.suffix == '.py' or source.name in ('requirements.txt','README.md'):
                shutil.copy2(source,working/source.name)
        pkg = CODEX.parent.parent
        args = ['/usr/bin/bwrap','--unshare-all','--share-net','--die-with-parent','--new-session',
                '--ro-bind','/usr','/usr','--ro-bind','/etc','/etc','--symlink','usr/lib','/lib',
                '--symlink','usr/lib64','/lib64','--symlink','usr/bin','/bin','--symlink','usr/sbin','/sbin',
                '--proc','/proc','--dev','/dev','--tmpfs','/tmp','--tmpfs',str(Path.home()),
                '--bind',str(working),'/site','--bind',str(auth),'/ai-home',
                '--ro-bind',str(pkg),str(pkg),'--ro-bind',str(NODE.parent.parent),str(NODE.parent.parent),
                '--ro-bind',str(store.ROOT/'editor_guard.mjs'),'/guard/editor_guard.mjs']
        if Path('/run/systemd/resolve').exists():
            args += ['--ro-bind','/run/systemd/resolve','/run/systemd/resolve']
        args += ['--setenv','HOME',str(Path.home()),'--setenv','CODEX_HOME','/ai-home',
                 '--setenv','PATH',str(NODE.parent)+':/usr/bin:/bin','--chdir','/site','--',str(CODEX),
                 'exec','--skip-git-repo-check','--json','--dangerously-bypass-hook-trust','--ephemeral','-']
        instruction = '''You edit ReKlik.shop, a real electronics store selling refabrikovani uređaji. Work only in /site.
Use Serbian Latin spelling and the term "refabrikovani", never "obnovljeni" or "refurbished" in storefront copy. Preserve all manually entered specifications and the six inventory condition grades.
You can edit templates, assets, and the existing app.py/store.py/payments.py/addresses.py modules.
Use read-only shell commands for inspection and apply_patch for changes. Do not change infrastructure or tests.
Never read or print credentials. Customer data and production inventory are not mounted.
Preserve payment verification, stock transactions, admin authentication, CSRF, free-text Serbian addresses,
no advertising or third-party trackers, preserve first-party unique-IP item counts, minimal JavaScript,
international OFF default, clear statutory rights, and mobile accessibility.
Use Serbian Latin customer text. Keep text and controls comfortably readable. Do not invent stock, reviews or claims.
The trusted host will test your code and publish only after successful validation.
Request: ''' + prompt
        update(job,status='running',text='AI uređuje izolovanu kopiju izvornog koda…')
        env = {k:v for k,v in os.environ.items() if k in ('LANG','LC_ALL','TERM')}
        process = subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        stdout,stderr = process.communicate(instruction,timeout=600)
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get('item') or {}
            if item.get('type') == 'agent_message':
                update(job,text=item.get('text',''))
            elif item.get('type') == 'file_change':
                for change in item.get('changes',[]):
                    update(job,text='Izmena: '+str(change.get('path','')))
            elif event.get('type') == 'turn.failed':
                update(job,text='AI nije završio: '+str(event.get('error',{}))[:1500])
        if process.returncode != 0:
            raise ValueError('AI uređivanje nije završeno. '+stderr[-1500:])
        if not (auth/'guard-log.jsonl').read_text().strip():
            raise ValueError('Zaštitni hook nije potvrđen; izmene nisu primenjene.')
        update(job,text='Proveravamo izmene pre primene…')
        check_env = dict(os.environ,PYTHONPATH=str(working),REKLIK_TEST_SOURCE=str(working))
        result = subprocess.run([str(VENV_PYTHON),str(store.ROOT/'tests/test_store.py')],cwd=working,env=check_env,capture_output=True,text=True,timeout=120)
        if result.returncode:
            raise ValueError('Provere nisu prošle; produkcija nije promenjena.\n'+result.stderr[-5000:])
        changes = []
        for source in working.rglob('*'):
            relative = source.relative_to(working)
            if source.is_file() and not source.is_symlink() and writable(relative):
                target = store.ROOT / relative
                if not target.exists() or target.read_bytes()!=source.read_bytes():
                    changes.append((source,target,relative))
        rollback.mkdir(parents=True,exist_ok=True)
        for source,target,relative in changes:
            if target.exists():
                backup=rollback/relative;backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,backup)
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        try:
            import publisher
            result = publisher.publish()
        except Exception:
            for source,target,relative in changes:
                backup=rollback/relative
                if backup.exists():shutil.copy2(backup,target)
                else:target.unlink(missing_ok=True)
            raise
        update(job,status='completed',text='Provere su prošle. Izmene su objavljene. '+str(result))
        if any(relative.as_posix() in ALLOWED_ROOT for _,_,relative in changes):
            subprocess.Popen(['/bin/sh','-c','sleep 2; systemctl --user restart reklik-server.service'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        try:process.kill();process.communicate(timeout=5)
        except Exception:pass
        update(job,status='failed',text='Uređivanje je prekoračilo vreme. Produkcija nije promenjena.')
    except Exception as error:
        update(job,status='failed',text=str(error))
    finally:
        LOCK.release()


def start(prompt):
    prompt = prompt.strip()
    if not 5 <= len(prompt) <= 8000:
        raise ValueError('Opišite izmenu u 5–8000 znakova.')
    if not LOCK.acquire(blocking=False):
        raise ValueError('Jedna izmena je već u toku. Sačekajte njen završetak.')
    job = store.secrets.token_hex(12)
    with store.connect(True) as c:
        c.execute('INSERT INTO jobs(id,kind,status,created_at) VALUES(?,?,?,?)',(job,'AI uređivanje','queued',store.now()))
    threading.Thread(target=run,args=(job,prompt),daemon=True).start()
    return job
