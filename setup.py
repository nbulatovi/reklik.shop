"""Initialize private runtime state; accepts the admin password only on stdin."""
import json
import os
import secrets
import sys
from werkzeug.security import generate_password_hash
import store

target = store.STATE / 'private.json'
if not target.exists():
    if sys.stdin.isatty():
        import termios
        old = termios.tcgetattr(sys.stdin)
        hidden = termios.tcgetattr(sys.stdin)
        hidden[3] &= ~termios.ECHO
        termios.tcsetattr(sys.stdin, termios.TCSANOW, hidden)
    print('Ready for admin password on stdin (hidden).', flush=True)
    password = sys.stdin.readline().rstrip('\n')
    if sys.stdin.isatty():
        termios.tcsetattr(sys.stdin, termios.TCSANOW, old)
    if len(password) < 6:
        raise SystemExit('An admin password of at least 6 characters is required on stdin.')
    target.write_text(json.dumps({'secret': secrets.token_urlsafe(48), 'password_hash': generate_password_hash(password)}))
    os.chmod(target, 0o600)
print('Private store state initialized.')
