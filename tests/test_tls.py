"""HTTPS between device and cloud, including the wrong-clock fallback:
a certificate rejected only for its dates is accepted if its fingerprint is pinned."""
import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

from hbpipe.config import Config
from hbpipe.daemon import Shared
from hbpipe.uplink import CloudClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CNF = """
[ca]
default_ca = CA_default
[CA_default]
dir = .
database = index.txt
new_certs_dir = .
serial = serial
default_md = sha256
policy = pol
copy_extensions = copy
unique_subject = no
[pol]
commonName = supplied
[req]
distinguished_name = dn
prompt = no
[dn]
CN = localhost
[ext]
subjectAltName = DNS:localhost,IP:127.0.0.1
basicConstraints = critical,CA:TRUE
keyUsage = digitalSignature,keyEncipherment,keyCertSign
"""


def make_cert(d, name, start, end):
    """Self-signed certificate valid from start to end (YYYYMMDDHHMMSSZ)."""
    work = os.path.join(d, name)
    os.makedirs(work)
    with open(os.path.join(work, "c.cnf"), "w") as f:
        f.write(CNF)
    open(os.path.join(work, "index.txt"), "w").close()
    with open(os.path.join(work, "serial"), "w") as f:
        f.write("01\n")
    run = lambda *a: subprocess.run(a, cwd=work, check=True, capture_output=True)
    run("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "k.pem", "-out", "r.csr",
        "-config", "c.cnf")
    run("openssl", "ca", "-batch", "-selfsign", "-config", "c.cnf", "-keyfile", "k.pem", "-in", "r.csr",
        "-out", "c.pem", "-startdate", start, "-enddate", end, "-extensions", "ext", "-notext")
    with open(os.path.join(work, "c.pem")) as f:
        fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert(f.read())).hexdigest()
    return os.path.join(work, "c.pem"), os.path.join(work, "k.pem"), fp


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@unittest.skipUnless(shutil.which("openssl"), "openssl not available")
class TlsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.good = make_cert(cls.dir, "good", "20250101000000Z", "20991231000000Z")
        cls.expired = make_cert(cls.dir, "expired", "20200101000000Z", "20200201000000Z")
        cls.servers = []

    @classmethod
    def tearDownClass(cls):
        for p in cls.servers:
            p.kill()
            p.wait()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def serve(self, cert, key):
        port = free_port()
        p = subprocess.Popen([sys.executable, "-m", "cloud.server", "--db", os.path.join(self.dir, f"{port}.db"),
                              "--port", str(port), "--tls-cert", cert, "--tls-key", key], cwd=ROOT,
                             env=dict(os.environ, HBCLOUD_TOKEN="t"),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.servers.append(p)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        return port

    def client(self, port, ca, pins=""):
        cfg = Config(cloud_url=f"https://localhost:{port}", cloud_token="t", ca_file=ca, tls_pins=pins)
        shared = Shared()
        return CloudClient(cfg, shared), shared

    def test_https_with_verified_certificate(self):
        port = self.serve(self.good[0], self.good[1])
        c, shared = self.client(port, self.good[0])
        self.assertTrue(c.health())
        ack = c.ingest({"chunks": []})
        self.assertTrue(ack["ok"])
        self.assertIsNone(shared.tls_error)

    def test_expired_certificate_rejected_without_pin(self):
        port = self.serve(self.expired[0], self.expired[1])
        c, shared = self.client(port, self.expired[0])
        self.assertFalse(c.health())
        self.assertIn("expired", shared.tls_error)

    def test_expired_certificate_accepted_when_pinned(self):
        port = self.serve(self.expired[0], self.expired[1])
        c, shared = self.client(port, self.expired[0], pins=self.expired[2])
        self.assertTrue(c.health())
        self.assertIn("pinned", shared.tls_error)

    def test_wrong_pin_rejected(self):
        port = self.serve(self.expired[0], self.expired[1])
        c, shared = self.client(port, self.expired[0], pins="00" * 32)
        self.assertFalse(c.health())


if __name__ == "__main__":
    unittest.main()
