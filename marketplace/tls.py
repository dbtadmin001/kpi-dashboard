"""TLS material for the marketplace coordinator.

Trino refuses to authenticate over plain HTTP, and it is right to: OAuth2 tokens
and bearer JWTs in clear text are worth about as much as no authentication at
all. So authentication needs a certificate, and on a laptop that means a
self-signed one.

This generates a private CA and a server certificate from it, rather than a bare
self-signed cert, for one practical reason: clients can be told to trust the CA
file once, instead of every tool needing "ignore certificate errors" switched on.
The CA is the thing you hand to DBeaver and to Python; the server certificate can
then be regenerated without reconfiguring anyone.

The certificate covers localhost, 127.0.0.1, the container name and
host.docker.internal, because the same coordinator is reached by all four
depending on who is asking.

    python -m marketplace.tls issue      # create CA + server cert (idempotent)
    python -m marketplace.tls issue --force
    python -m marketplace.tls show

Nothing here is suitable for production. In production the coordinator gets a
certificate from your organisation's CA and this module is deleted.
"""
import argparse
import datetime
import ipaddress
import os
import pathlib
import secrets
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from .build import OUT

TLS = OUT / "trino" / "tls"
KEYSTORE = TLS / "trino.p12"
CA_CERT = TLS / "marketplace-ca.crt"
CA_KEY = TLS / "marketplace-ca.key"
# Java clients (the Trino JDBC driver, and so DBeaver) cannot read a PEM as a
# trust store - they want a JKS or PKCS12. Handing someone the .crt and telling
# them to set SSLTrustStorePath is advice that does not work, so both are written.
TRUSTSTORE = TLS / "marketplace-truststore.p12"
PASSWORD_FILE = TLS / "keystore.password"

# Every name this one coordinator answers to.
HOSTNAMES = ("localhost", "trino-marketplace", "host.docker.internal", "marketplace.local")
ADDRESSES = ("127.0.0.1", "::1")
VALID_DAYS = 825  # The longest most clients will accept for a server certificate.


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def keystore_password():
    """Generated once and reused, so regenerating the cert does not break config."""
    if PASSWORD_FILE.exists():
        return PASSWORD_FILE.read_text(encoding="utf-8").strip()
    password = secrets.token_urlsafe(24)
    TLS.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.write_text(password, encoding="utf-8")
    os.chmod(PASSWORD_FILE, 0o600)
    return password


def internal_secret():
    """Shared secret for Trino's own node-to-node traffic.

    Required once authentication is enabled, and deliberately NOT the OIDC client
    secret: one authorises the cluster to talk to itself, the other authorises
    Trino to Keycloak, and reusing a secret across two trust boundaries means
    rotating either one breaks the other.
    """
    path = TLS / "internal.secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(32)
    TLS.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    os.chmod(path, 0o600)
    return secret


def _ca():
    """Load the CA, or mint one. Its key never leaves this directory."""
    if CA_CERT.exists() and CA_KEY.exists():
        return (x509.load_pem_x509_certificate(CA_CERT.read_bytes()),
                serialization.load_pem_private_key(CA_KEY.read_bytes(), password=None))
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NDA data marketplace"),
        x509.NameAttribute(NameOID.COMMON_NAME, "NDA marketplace development CA"),
    ])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(minutes=5))
        .not_valid_after(_now() + datetime.timedelta(days=VALID_DAYS * 2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True,
            content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256()))
    TLS.mkdir(parents=True, exist_ok=True)
    CA_CERT.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    CA_KEY.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    os.chmod(CA_KEY, 0o600)
    return certificate, key


def issue(force=False):
    """Write the PKCS12 keystore Trino loads, and the CA clients should trust."""
    if KEYSTORE.exists() and not force:
        print(f"Keystore already present: {KEYSTORE}")
        print("  Pass --force to replace it (every client must then re-trust the CA).")
        return KEYSTORE
    ca_certificate, ca_key = _ca()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    alternatives = [x509.DNSName(h) for h in HOSTNAMES]
    alternatives += [x509.IPAddress(ipaddress.ip_address(a)) for a in ADDRESSES]
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NDA data marketplace"),
            x509.NameAttribute(NameOID.COMMON_NAME, "trino-marketplace")]))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(minutes=5))
        .not_valid_after(_now() + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([
            x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256()))

    password = keystore_password()
    TLS.mkdir(parents=True, exist_ok=True)
    KEYSTORE.write_bytes(pkcs12.serialize_key_and_certificates(
        name=b"trino-marketplace", key=key, cert=certificate, cas=[ca_certificate],
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode())))
    os.chmod(KEYSTORE, 0o600)

    write_truststore(password)
    print(f"Issued a server certificate valid for {VALID_DAYS} days")
    print(f"  keystore    {KEYSTORE}          (the server)")
    print(f"  CA (PEM)    {CA_CERT}           (Python, curl, dbt)")
    print(f"  truststore  {TRUSTSTORE}        (DBeaver and anything else on the JVM)")
    print(f"  password    {password}")
    print(f"  names       {', '.join(HOSTNAMES + ADDRESSES)}")
    return KEYSTORE


def write_truststore(password):
    """Build the JVM trust store with keytool, not with a PKCS12 writer.

    A PKCS12 built by a generic library holds the CA as an ordinary certificate
    bag, and `keytool -list` reports "0 entries": Java only trusts a certificate
    carrying its own trustedKeyUsage attribute, which is a Java-specific oddity
    no general-purpose library emits. Writing one and assuming it works produces
    a trust store that looks right and trusts nothing.

    keytool comes from the Trino image, which is already present - so this needs
    no JDK on the host.
    """
    import subprocess

    TRUSTSTORE.unlink(missing_ok=True)
    import shutil
    if shutil.which("keytool"):
        subprocess.run(["keytool", "-importcert", "-noprompt", "-alias", "marketplace-ca",
                        "-file", str(CA_CERT), "-keystore", str(TRUSTSTORE),
                        "-storetype", "PKCS12", "-storepass", password], check=True,
                       capture_output=True)
        return TRUSTSTORE
    image = os.environ.get("TRINO_IMAGE", "trinodb/trino:455")
    mount = str(TLS.resolve())
    if os.name == "nt":
        mount = mount.replace(chr(92), "/")
    result = subprocess.run([
        "docker", "run", "--rm", "-v", f"{mount}:/tls", "--entrypoint", "keytool", image,
        "-importcert", "-noprompt", "-trustcacerts",
        "-alias", "nda-marketplace-ca",
        "-file", f"/tls/{CA_CERT.name}",
        "-keystore", f"/tls/{TRUSTSTORE.name}",
        "-storetype", "PKCS12", "-storepass", password,
    ], capture_output=True, text=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    if result.returncode != 0 or not TRUSTSTORE.exists():
        print("  WARNING: could not build the JVM trust store: "
              + (result.stderr or result.stdout).strip()[:200])
        print("           Java clients will need SSLUseSystemTrustStore or the PEM.")
        return None
    verify = subprocess.run([
        "docker", "run", "--rm", "-v", f"{mount}:/tls", "--entrypoint", "keytool", image,
        "-list", "-keystore", f"/tls/{TRUSTSTORE.name}",
        "-storetype", "PKCS12", "-storepass", password,
    ], capture_output=True, text=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    # Assert rather than hope: an empty trust store is the failure mode here.
    if "trustedCertEntry" not in verify.stdout:
        raise SystemExit("Trust store was written but holds no trusted certificate:"
                         + chr(10) + verify.stdout[:400])
    return TRUSTSTORE


# --------------------------------------------------------------------------
# Distributing trust
# --------------------------------------------------------------------------
def trust_command():
    """How this platform's CA gets trusted by a client machine.

    This is the step that makes single sign-on usable. Once the CA is in the
    machine's trust store, every SQL client on it connects with
    SSLUseSystemTrustStore=true and externalAuthentication=true - no trust store
    path, no password, no token, and the same connection settings for every
    person. Handing each user a trust store file and a bearer token instead is
    what you do when this step has not been done.

    On a managed fleet this belongs in configuration management, not in a person's
    hands: see the `ca_trust` tasks in infra/ansible/roles/common.
    """
    certificate = str(CA_CERT)
    if os.name == "nt":
        # -user: the current user's Root store, so no administrator rights are
        # needed. Java's SSLUseSystemTrustStore reads Windows-ROOT, which
        # includes it.
        return ["certutil", "-addstore", "-user", "Root", certificate]
    if sys.platform == "darwin":
        return ["security", "add-trusted-cert", "-d", "-r", "trustRoot",
                "-k", os.path.expanduser("~/Library/Keychains/login.keychain-db"), certificate]
    return ["sudo", "cp", certificate, "/usr/local/share/ca-certificates/nda-marketplace-ca.crt",
            "&&", "sudo", "update-ca-certificates"]


def trust(apply=False):
    if not CA_CERT.exists():
        raise SystemExit("No CA yet. Run: python -m marketplace.tls issue")
    command = trust_command()
    if not apply:
        print("This installs the marketplace CA so SQL clients trust the coordinator")
        print("without being handed a trust store file:")
        print(f"{chr(10)}  {' '.join(command)}{chr(10)}")
        print("Run with --apply to do it. To undo on Windows:")
        print("  certutil -delstore -user Root \"NDA marketplace development CA\"")
        return
    import subprocess
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("Could not install the CA: "
                         + (result.stderr or result.stdout).strip()[:300])
    print(f"Installed the marketplace CA into this machine's trust store.")
    print("SQL clients on this machine can now connect with:")
    print("  SSL=true  SSLUseSystemTrustStore=true  externalAuthentication=true")
    print(f"{chr(10)}Undo:  certutil -delstore -user Root \"NDA marketplace development CA\"")


def show():
    if not KEYSTORE.exists():
        raise SystemExit(f"No keystore yet. Run: python -m marketplace.tls issue")
    certificate = x509.load_pem_x509_certificate(CA_CERT.read_bytes())
    _, server, _ = pkcs12.load_key_and_certificates(
        KEYSTORE.read_bytes(), keystore_password().encode())
    names = server.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    addresses = server.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
    remaining = server.not_valid_after_utc - _now()
    print(f"  CA        {certificate.subject.rfc4514_string()}")
    print(f"  server    {server.subject.rfc4514_string()}")
    print(f"  names     {', '.join(names + [str(a) for a in addresses])}")
    print(f"  expires   {server.not_valid_after_utc:%Y-%m-%d} ({remaining.days} days left)")
    print(f"  trust     {CA_CERT} (PEM)")
    print(f"            {TRUSTSTORE} (JVM)")
    print(f"  password  {keystore_password()}")


def main():
    parser = argparse.ArgumentParser(description="TLS material for the marketplace coordinator")
    parser.add_argument("action", choices=["issue", "show", "trust"])
    parser.add_argument("--force", action="store_true", help="Replace an existing keystore")
    parser.add_argument("--apply", action="store_true",
                        help="With 'trust', actually install the CA rather than printing the command")
    args = parser.parse_args()
    if args.action == "issue":
        issue(args.force)
    elif args.action == "trust":
        trust(args.apply)
    else:
        show()


if __name__ == "__main__":
    main()
