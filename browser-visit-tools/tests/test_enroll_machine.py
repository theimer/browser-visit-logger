"""Unit tests for enroll_machine.py."""
import datetime
import sqlite3
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).parent.parent))
import enroll_machine as em  # noqa: E402


def _mint(tmp_path, cn='laptop-a', pem=True):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer).public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(datetime.datetime(2020, 1, 1))
            .not_valid_after(datetime.datetime(2040, 1, 1))
            .sign(key, hashes.SHA256()))
    enc = serialization.Encoding.PEM if pem else serialization.Encoding.DER
    path = tmp_path / (cn + ('.crt' if pem else '.der'))
    path.write_bytes(cert.public_bytes(enc))
    return path, cert


def test_fingerprint_pem_and_der_match(tmp_path):
    pem_path, cert = _mint(tmp_path, pem=True)
    der_path = tmp_path / 'a.der'
    der_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    assert em.cert_fingerprint(pem_path) == em.cert_fingerprint(der_path)


def test_enroll_and_list_and_revoke(tmp_path, capsys):
    db = tmp_path / 'enrolled.db'
    cert_path, _ = _mint(tmp_path)
    em.enroll(db, 'laptop-a', cert_path)
    # Row present.
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM enrolled_machines").fetchone()[0]
    conn.close()
    assert n == 1
    # List prints the machine.
    em.list_all(db)
    assert 'laptop-a' in capsys.readouterr().out
    # Revoke removes it.
    em.revoke(db, 'laptop-a')
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM enrolled_machines").fetchone()[0]
    conn.close()
    assert n == 0


def test_enroll_replace(tmp_path):
    db = tmp_path / 'enrolled.db'
    c1, _ = _mint(tmp_path, 'laptop-a')
    em.enroll(db, 'laptop-a', c1)
    c2, _ = _mint(tmp_path, 'laptop-a-again')
    # Same machine_id, new cert → INSERT OR REPLACE.
    (tmp_path / 'laptop-a-again.crt').rename(tmp_path / 'replace.crt')
    em.enroll(db, 'laptop-a', tmp_path / 'replace.crt')
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM enrolled_machines").fetchone()[0]
    conn.close()
    assert n == 1


# ---- main / CLI ----

def test_main_list(tmp_path, capsys):
    db = tmp_path / 'enrolled.db'
    cert_path, _ = _mint(tmp_path)
    em.enroll(db, 'laptop-a', cert_path)
    rc = em.main(['--db', str(db), '--list'])
    assert rc == 0
    assert 'laptop-a' in capsys.readouterr().out


def test_main_enroll(tmp_path):
    db = tmp_path / 'enrolled.db'
    cert_path, _ = _mint(tmp_path)
    rc = em.main(['--db', str(db), '--machine-id', 'laptop-a', '--cert', str(cert_path)])
    assert rc == 0


def test_main_revoke(tmp_path):
    db = tmp_path / 'enrolled.db'
    cert_path, _ = _mint(tmp_path)
    em.enroll(db, 'laptop-a', cert_path)
    rc = em.main(['--db', str(db), '--machine-id', 'laptop-a', '--revoke'])
    assert rc == 0


def test_main_missing_machine_id(tmp_path, capsys):
    db = tmp_path / 'enrolled.db'
    rc = em.main(['--db', str(db)])
    assert rc == 2
    assert 'machine-id required' in capsys.readouterr().err


def test_main_enroll_missing_cert(tmp_path, capsys):
    db = tmp_path / 'enrolled.db'
    rc = em.main(['--db', str(db), '--machine-id', 'laptop-a'])
    assert rc == 2
    assert 'cert required' in capsys.readouterr().err


def test_cert_fingerprint_pem_without_cryptography(tmp_path, monkeypatch, capsys):
    pem_path, _ = _mint(tmp_path, pem=True)
    # Simulate cryptography being unavailable for the PEM branch.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith('cryptography'):
            raise ImportError('no cryptography')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', fake_import)
    with pytest.raises(SystemExit) as ei:
        em.cert_fingerprint(pem_path)
    assert ei.value.code == 2
