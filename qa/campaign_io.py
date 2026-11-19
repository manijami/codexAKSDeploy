"""Atomic campaign checkpoints, append-only evidence and shared HTTP ceilings."""
import hashlib
import json
import os
import sqlite3
import time
import math
from email.utils import parsedate_to_datetime
from pathlib import Path


class BudgetStop(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic(path,value,private=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600 if private else 0o644)
    with os.fdopen(fd,'w') as handle:
        handle.write(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n');handle.flush();os.fsync(handle.fileno())
    os.replace(temporary,path)


def immutable(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as handle:
        handle.write(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n');handle.flush();os.fsync(handle.fileno())
    os.chmod(path,0o444)


class Ledger:
    def __init__(self,path,limits=None,expires=None):
        self.path=Path(path)
        if limits is not None:
            with self.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS caps(kind TEXT PRIMARY KEY,maximum INTEGER,expires REAL)')
                db.execute('CREATE TABLE IF NOT EXISTS recovery(acknowledged_call INTEGER)')
                if not db.execute('SELECT count(*) FROM recovery').fetchone()[0]:db.execute('INSERT INTO recovery VALUES(0)')
                db.execute('CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,kind TEXT,job TEXT,batch TEXT,started REAL,status INTEGER,retry_after TEXT,finished REAL)')
                for kind,maximum in limits.items():
                    db.execute('INSERT OR IGNORE INTO caps VALUES(?,?,?)',(kind,maximum,expires))
    def connect(self):
        return sqlite3.connect(self.path,timeout=10)
    def claim(self,kind,job,batch='',job_limit=None,batch_limit=None):
        if kind=='judge' and self.provider_stop():raise BudgetStop('Provider stop active; explicit recovery resume required')
        db=self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            cap=db.execute('SELECT maximum,expires FROM caps WHERE kind=?',(kind,)).fetchone()
            total=db.execute('SELECT count(*) FROM calls WHERE kind=?',(kind,)).fetchone()[0]
            if not cap or time.time()>=cap[1] or total>=cap[0]:raise BudgetStop('Campaign request/time ceiling reached')
            if job_limit is not None and db.execute('SELECT count(*) FROM calls WHERE kind=? AND job=?',(kind,job)).fetchone()[0]>=job_limit:raise BudgetStop('Metric request ceiling reached')
            if batch_limit is not None and db.execute('SELECT count(*) FROM calls WHERE kind=? AND batch=?',(kind,batch)).fetchone()[0]>=batch_limit:raise BudgetStop('Batch request ceiling reached')
            call=db.execute('INSERT INTO calls(kind,job,batch,started) VALUES(?,?,?,?)',(kind,job,batch,time.time())).lastrowid
            db.commit();return call
        finally:db.close()
    def finish(self,call,status,retry_after=None):
        # Persist only status and a bounded numeric Retry-After. No headers/tokens.
        retry=str(retry_after) if retry_after and str(retry_after).isdigit() else None
        if retry_after and retry is None:
            try:retry=str(max(0,math.ceil(parsedate_to_datetime(str(retry_after)).timestamp()-time.time())))
            except (ValueError,TypeError,OverflowError):pass
        with self.connect() as db:db.execute('UPDATE calls SET status=?,retry_after=?,finished=? WHERE id=?',(status,retry,time.time(),call))
    def counts(self):
        with self.connect() as db:return dict(db.execute('SELECT kind,count(*) FROM calls GROUP BY kind'))
    def remaining_seconds(self):
        with self.connect() as db:
            row=db.execute('SELECT min(expires) FROM caps').fetchone()
            return max(0,row[0]-time.time()) if row and row[0] else 0
    def provider_stop(self):
        with self.connect() as db:
            ack=db.execute('SELECT acknowledged_call FROM recovery').fetchone()[0]
            quota=db.execute('SELECT status,retry_after FROM calls WHERE id>? AND status IN (402,429) ORDER BY id DESC LIMIT 1',(ack,)).fetchone()
            if quota:return quota
            last=[r[0] for r in db.execute("SELECT status FROM calls WHERE kind='judge' AND id>? ORDER BY id DESC LIMIT 3",(ack,))]
            if len(last)>=2 and all(s in (401,403) for s in last[:2]):return ('AUTHENTICATION',None)
            if len(last)==3 and all(s is None or (type(s) is int and 500<=s<600) for s in last):return ('CONNECTIVITY_OR_UPSTREAM',None)
            return None

    def acknowledge_recovery(self):
        # Only explicit user resume acknowledges a previous provider stop.
        # Counts and failed calls remain intact; never sleep/retry automatically.
        with self.connect() as db:
            row=db.execute('SELECT id,coalesce(finished,started),retry_after FROM calls WHERE status IN (402,429) ORDER BY id DESC LIMIT 1').fetchone()
            if row and row[2] and time.time()<row[1]+int(row[2]):raise BudgetStop('Provider Retry-After has not elapsed; resume later')
            maximum=db.execute('SELECT max(id) FROM calls').fetchone()[0] or 0
            db.execute('UPDATE recovery SET acknowledged_call=?',(maximum,))
