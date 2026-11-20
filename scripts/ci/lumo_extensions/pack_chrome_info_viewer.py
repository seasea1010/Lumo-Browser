#!/usr/bin/env python3
"""Pack Chrome Info Viewer into the CRX3 that Lumo preinstalls.

Chrome Info Viewer is the browser owner's own extension (see
README.md in this directory). Lumo bundles it the same way Thorium
bundles uBlock Origin: a signed CRX embedded in the browser
resources, written into new profiles and registered as an external
extension by chrome/browser/extensions/preinstalled_extensions.cc
(Lumo patch 0027).

The RSA key in chrome_info_viewer_key.pem exists ONLY to pin the
extension ID (cmcleajbmajgneoofpehpkjklnngacna) across builds: the
external-install path derives the ID from the signing key, so the
key must never change or the preinstalled extension would change
identity. It is not a security boundary -- the CRX is only ever
installed from the copy embedded in the browser itself, no update
URL is registered for the extension, and the key is committed to
this repository on purpose so the CRX can be rebuilt anywhere.

Packing is deterministic: the zip entries are stored uncompressed,
sorted by name, with a fixed 1980 timestamp, and RSA PKCS#1 v1.5
signing has no random component, so the same source tree and key
always produce byte-identical CRX output.

Usage:
  pack_chrome_info_viewer.py            rebuild the .crx next to
                                        this script from
                                        chrome_info_viewer/
  pack_chrome_info_viewer.py --verify <file.crx>
                                        parse a CRX, verify its
                                        signature and that its
                                        contents match the source
                                        tree; prints the ID.
Requires only the Python 3 standard library.
"""

import base64
import hashlib
import io
import json
import struct
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE_DIR = HERE / "chrome_info_viewer"
KEY_PEM = HERE / "chrome_info_viewer_key.pem"

# The zip epoch: the earliest date the zip format can store.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)

SHA256_DIGEST_INFO_PREFIX = bytes.fromhex(
    "3031300d060960864801650304020105000420")
RSA_ENCRYPTION_OID = bytes.fromhex("2a864886f70d010101")


# ------------------------------------------------------------------
# Minimal DER reader/writer (just enough for RSA keys)
# ------------------------------------------------------------------

def _read_tlv(data, offset):
    """Return (tag, contents, next_offset) for one DER element."""
    tag = data[offset]
    offset += 1
    length = data[offset]
    offset += 1
    if length & 0x80:
        nbytes = length & 0x7F
        length = int.from_bytes(data[offset:offset + nbytes], "big")
        offset += nbytes
    contents = data[offset:offset + length]
    if len(contents) != length:
        raise ValueError("truncated DER")
    return tag, contents, offset + length


def _children(sequence_contents):
    out = []
    offset = 0
    while offset < len(sequence_contents):
        tag, contents, offset = _read_tlv(sequence_contents, offset)
        out.append((tag, contents))
    return out


def _der_length(length):
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _der_tlv(tag, contents):
    return bytes([tag]) + _der_length(len(contents)) + contents


def _der_integer(value):
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der_tlv(0x02, raw)


def _parse_pkcs1(der):
    tag, seq, _ = _read_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("expected SEQUENCE")
    ints = [int.from_bytes(c, "big")
            for t, c in _children(seq) if t == 0x02]
    if len(ints) < 4:
        raise ValueError("not an RSA private key")
    # version, n, e, d, ...
    return ints[1], ints[2], ints[3]


def load_private_key(pem_path):
    """Return (n, e, d) from a PKCS#8 or PKCS#1 PEM file."""
    text = pem_path.read_text(encoding="ascii")
    for label in ("PRIVATE KEY", "RSA PRIVATE KEY"):
        begin = f"-----BEGIN {label}-----"
        if begin in text:
            body = text.split(begin)[1].split(f"-----END {label}-----")[0]
            der = base64.b64decode("".join(body.split()))
            break
    else:
        raise ValueError(f"no PEM block in {pem_path}")
    if label == "RSA PRIVATE KEY":
        return _parse_pkcs1(der)
    tag, seq, _ = _read_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("expected PKCS#8 SEQUENCE")
    parts = _children(seq)
    octet = [c for t, c in parts if t == 0x04]
    if len(octet) != 1:
        raise ValueError("PKCS#8 without a key octet string")
    return _parse_pkcs1(octet[0])


def spki_der(n, e):
    """SubjectPublicKeyInfo DER for an RSA public key."""
    alg = _der_tlv(0x30, _der_tlv(0x06, RSA_ENCRYPTION_OID)
                   + _der_tlv(0x05, b""))
    pubkey = _der_tlv(0x30, _der_integer(n) + _der_integer(e))
    return _der_tlv(0x30, alg + _der_tlv(0x03, b"\x00" + pubkey))


def parse_spki(der):
    """Return (n, e) from a SubjectPublicKeyInfo DER blob."""
    tag, seq, _ = _read_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("expected SPKI SEQUENCE")
    parts = _children(seq)
    bit_string = [c for t, c in parts if t == 0x03]
    if len(bit_string) != 1 or not bit_string[0] or bit_string[0][0] != 0:
        raise ValueError("bad SPKI bit string")
    tag, pubkey, _ = _read_tlv(bit_string[0][1:], 0)
    if tag != 0x30:
        raise ValueError("expected RSAPublicKey SEQUENCE")
    ints = [int.from_bytes(c, "big")
            for t, c in _children(pubkey) if t == 0x02]
    return ints[0], ints[1]


def extension_id(spki):
    """Chromium extension ID: SHA-256 of the SPKI, first 16 bytes,
    each nibble mapped to 'a'..'p'."""
    digest = hashlib.sha256(spki).digest()
    return "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 15))
                   for b in digest[:16])


# ------------------------------------------------------------------
# CRX3 packing (proto3 encoding done by hand; the messages are tiny)
# ------------------------------------------------------------------

def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _len_field(number, payload):
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _parse_fields(message):
    """Parse a protobuf message into {field_number: [payloads]}."""
    fields = {}
    offset = 0
    while offset < len(message):
        key = 0
        shift = 0
        while True:
            byte = message[offset]
            offset += 1
            key |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        number, wire = key >> 3, key & 7
        if wire == 2:
            length = 0
            shift = 0
            while True:
                byte = message[offset]
                offset += 1
                length |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            payload = message[offset:offset + length]
            offset += length
        elif wire == 0:
            payload = 0
            shift = 0
            while True:
                byte = message[offset]
                offset += 1
                payload |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
        else:
            raise ValueError(f"unsupported wire type {wire}")
        fields.setdefault(number, []).append(payload)
    return fields


def _rsa_sign_sha256(message, n, d):
    """RSA PKCS#1 v1.5 signature over SHA-256(message)."""
    k = (n.bit_length() + 7) // 8
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding = b"\xff" * (k - len(digest_info) - 3)
    encoded = b"\x00\x01" + padding + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), d, n)
    return signature.to_bytes(k, "big")


def _rsa_verify_sha256(message, signature, n, e):
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    encoded = pow(int.from_bytes(signature, "big"), e, n).to_bytes(k, "big")
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    expected = (b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3)
                + b"\x00" + digest_info)
    return encoded == expected


def build_zip(source_dir):
    """Deterministic zip of the extension source files."""
    buffer = io.BytesIO()
    names = sorted(p.name for p in source_dir.iterdir() if p.is_file())
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        for name in names:
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 0
            info.external_attr = 0
            zf.writestr(info, (source_dir / name).read_bytes())
    return buffer.getvalue()


def pack(source_dir, key_path):
    n, e, d = load_private_key(key_path)
    spki = spki_der(n, e)
    manifest = json.loads(
        (source_dir / "manifest.json").read_text(encoding="utf-8"))
    if base64.b64decode(manifest.get("key", "")) != spki:
        raise ValueError(
            "manifest.json \"key\" does not match the signing key; "
            "the extension ID would change")
    crx_id = hashlib.sha256(spki).digest()[:16]
    signed_header_data = _len_field(1, crx_id)
    zip_bytes = build_zip(source_dir)
    signing_input = (b"CRX3 SignedData\x00"
                     + struct.pack("<I", len(signed_header_data))
                     + signed_header_data + zip_bytes)
    proof = (_len_field(1, spki)
             + _len_field(2, _rsa_sign_sha256(signing_input, n, d)))
    header = _len_field(2, proof) + _len_field(10000, signed_header_data)
    return (b"Cr24" + struct.pack("<II", 3, len(header)) + header
            + zip_bytes)


def parse_crx(data):
    """Return (public_key_spki, signature, signed_header_data,
    crx_id, zip_bytes) from CRX3 bytes."""
    if data[:4] != b"Cr24":
        raise ValueError("not a CRX file")
    version, header_len = struct.unpack_from("<II", data, 4)
    if version != 3:
        raise ValueError(f"unsupported CRX version {version}")
    header = _parse_fields(data[12:12 + header_len])
    zip_bytes = data[12 + header_len:]
    proofs = header.get(2, [])
    if len(proofs) != 1:
        raise ValueError("expected exactly one sha256_with_rsa proof")
    proof = _parse_fields(proofs[0])
    signed_header_data = header[10000][0]
    signed_data = _parse_fields(signed_header_data)
    return (proof[1][0], proof[2][0], signed_header_data,
            signed_data[1][0], zip_bytes)


def verify(crx_path, source_dir):
    data = crx_path.read_bytes()
    spki, signature, signed_header_data, crx_id, zip_bytes = \
        parse_crx(data)
    n, e = parse_spki(spki)
    if crx_id != hashlib.sha256(spki).digest()[:16]:
        raise ValueError("crx_id does not match the public key")
    signing_input = (b"CRX3 SignedData\x00"
                     + struct.pack("<I", len(signed_header_data))
                     + signed_header_data + zip_bytes)
    if not _rsa_verify_sha256(signing_input, signature, n, e):
        raise ValueError("signature does not verify")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = sorted(zf.namelist())
        expected = sorted(p.name for p in source_dir.iterdir()
                          if p.is_file())
        if names != expected:
            raise ValueError(f"zip entries {names} != source {expected}")
        for name in names:
            if zf.read(name) != (source_dir / name).read_bytes():
                raise ValueError(f"zip entry {name} differs from source")
    return extension_id(spki)


def main(argv):
    if len(argv) == 3 and argv[1] == "--verify":
        ext_id = verify(Path(argv[2]), SOURCE_DIR)
        print(f"OK {argv[2]}: signature valid, contents match "
              f"the source tree, extension ID {ext_id}")
        return 0
    if len(argv) != 1:
        print(__doc__)
        return 2
    manifest = json.loads(
        (SOURCE_DIR / "manifest.json").read_text(encoding="utf-8"))
    out = HERE / f"chrome_info_viewer_{manifest['version']}.crx"
    out.write_bytes(pack(SOURCE_DIR, KEY_PEM))
    ext_id = verify(out, SOURCE_DIR)
    print(f"wrote {out} (extension ID {ext_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
