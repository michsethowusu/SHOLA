"""A volunteer whose language was withdrawn is asked to choose again.

The sign languages are why this exists. They were offered, they cannot be
written down, and they have been taken off the list. What must not happen is a
volunteer being stranded: the evaluate screen has to send them somewhere they
can act, and nothing may be emailed to them in the meantime.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp()
from shola import create_app
from shola.config import Config
class T(Config):
    TESTING = True; SECRET_KEY = "t"
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp}/t.db"
    SMTP_USER = "x@e.com"; SMTP_PASSWORD = "y"
app = create_app(T)
from shola.models import Volunteer, Word, db
from shola.projects import core_project
from shola.mailer import make_token

ok = []
def check(label, cond, detail=""):
    ok.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if not cond else ""))

with app.app_context():
    p = core_project()
    signer = Volunteer(name="Signer", email="s@e.com", language="gse",
                       available_days="0,1,2,3,4,5,6", time_window="anytime")
    normal = Volunteer(name="Speaker", email="n@e.com", language="twi",
                       available_days="0,1,2,3,4,5,6", time_window="anytime")
    db.session.add_all([signer, normal])
    for i in range(3):
        db.session.add(Word(phrase=f"word {i}", project_id=p.id, position=1,
                            occurrences=99, frequency=0.0, tier=1))
    db.session.commit()
    t_sign, t_norm = make_token(signer), make_token(normal)

c = app.test_client()

print("\nA withdrawn language sends them to choose again")
r = c.get(f"/w/{t_sign}", follow_redirects=False)
check("the evaluate screen redirects", r.status_code == 302, str(r.status_code))
check("to the language page", "/language" in r.headers.get("Location", ""),
      r.headers.get("Location", ""))
r = c.get(f"/w/{t_sign}/language")
check("that page renders", r.status_code == 200, str(r.status_code))
check("and names the language they lost", b"Ghanaian Sign Language" in r.data)
check("with the picker on it", b"lang-search" in r.data)

print("\nEverybody else is untouched")
r = c.get(f"/w/{t_norm}", follow_redirects=False)
check("a normal volunteer still gets their words", r.status_code == 200,
      str(r.status_code))
r = c.get(f"/w/{t_norm}/language", follow_redirects=False)
check("and cannot use the page to switch language", r.status_code == 302,
      str(r.status_code))

print("\nNothing is emailed while they are waiting")
import contextlib, io as _io
from shola.cli import send_daily as _send

def run_send():
    buf = _io.StringIO()
    with app.app_context(), contextlib.redirect_stdout(buf):
        try:
            fn = getattr(_send.callback, "__wrapped__", _send.callback)
            fn(window="all", dry_run=True, force=True)
        except SystemExit:
            pass
    return buf.getvalue()

out = run_send()
check("the send reports them as waiting",
      "waiting to pick a language again 1" in out, out.strip()[-90:])
check("and the other volunteer is still sent to", "sent 1" in out,
      out.strip()[-90:])

print("\nChoosing again puts them back to work")
r = c.post(f"/w/{t_sign}/language", data={"language": "hau"},
           follow_redirects=False)
check("the choice is accepted", r.status_code == 302, str(r.status_code))
with app.app_context():
    again = Volunteer.query.filter_by(email="s@e.com").first()
    check("and saved", again.language == "hau", again.language)
r = c.get(f"/w/{t_sign}", follow_redirects=False)
check("their words open normally now", r.status_code == 200, str(r.status_code))
out = run_send()
check("and the send covers them again", "sent 2" in out, out.strip()[-90:])

print(f"\n{len(ok)} checks, {ok.count(False)} failed")
sys.exit(1 if ok.count(False) else 0)
