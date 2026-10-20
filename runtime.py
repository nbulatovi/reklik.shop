"""PC-hosted server and isolated Cloudflare tunnel, managed by separate services."""
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import store


def maintenance():
    import payments
    import publisher
    last_backup = 0
    while True:
        try:
            if store.release_expired():
                publisher.schedule()
            with store.connect() as c:
                orders = [store.unpack(row) for row in c.execute("SELECT * FROM orders WHERE payment_method='card' AND status IN ('awaiting_payment','payment_error','expired') AND plan_id IS NOT NULL AND created_at>? ORDER BY created_at DESC LIMIT 60",(store.now()-86400,))]
            for order in orders:
                if payments.reconcile(order):
                    publisher.schedule()
            if store.now()-last_backup > 3600:
                backups = store.STATE / 'backups'
                backups.mkdir(exist_ok=True,mode=0o700)
                target = backups / (time.strftime('%Y%m%d-%H') + '.sqlite3')
                source = sqlite3.connect(store.DB)
                dest = sqlite3.connect(target)
                source.backup(dest)
                dest.close(); source.close()
                target.chmod(0o600)
                for old in sorted(backups.glob('*.sqlite3'))[:-48]:
                    old.unlink()
                with store.connect(True) as c:
                    c.execute('DELETE FROM sessions WHERE expires_at<?',(store.now(),))
                last_backup = store.now()
        except Exception as error:
            store.audit('maintenance_error',detail=type(error).__name__)
        time.sleep(40)


def server():
    from app import app
    from waitress import serve
    with store.connect(True) as c:
        c.execute("UPDATE jobs SET status='failed',output=output || ?,finished_at=? WHERE kind!='specifications' AND status IN ('running','queued')",('\nServer je ponovo pokrenut. Pokrenite izmenu ponovo ako je potrebno.',store.now()))
    import specifications
    specifications.recover()
    import notifications
    threading.Thread(target=notifications.run,daemon=True,name='order-emails').start()
    threading.Thread(target=maintenance,daemon=True).start()
    print('ReKlik server listening on 127.0.0.1:4340',flush=True)
    serve(app,host='127.0.0.1',port=int(os.environ.get('REKLIK_PORT',4340)),threads=12,max_request_body_size=35*1024*1024)


def tunnel():
    import publisher
    isolated_home = store.STATE / 'tunnel-home'
    isolated_home.mkdir(exist_ok=True,mode=0o700)
    config = isolated_home / 'empty.yml'
    config.write_text('')
    child = None
    def stop(signum,frame):
        if child:
            child.terminate()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    while True:
        child = subprocess.Popen(['/usr/bin/cloudflared','tunnel','--no-autoupdate','--config',str(config),'--url','http://127.0.0.1:4340'],
                                 env=dict(os.environ,HOME=str(isolated_home)),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        for line in child.stdout:
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',line)
            if match:
                endpoint = {'backend':match.group(0),'updated':store.now()}
                destination = store.STATE / 'backend.json'
                temporary = destination.with_suffix('.tmp')
                temporary.write_text(json.dumps(endpoint)); temporary.replace(destination)
                publisher.schedule()
                print('ReKlik HTTPS tunnel connected; public endpoint update queued.',flush=True)
        code = child.wait()
        print('Tunnel exited; reconnecting. Exit code: '+str(code),flush=True)
        time.sleep(5)


if __name__ == '__main__':
    {'server':server,'tunnel':tunnel}[sys.argv[1]]()
