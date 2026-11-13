"""The private key of a GitHub App as the imported key material of a KMS key.

GitHub generates the private key of an App and gives it one time, as a PEM file. AWS KMS imports key material
that is encrypted for the transport: the key in the PKCS #8 form under an AES key, and the AES key under a
wrapping public key of AWS KMS (the wrapping algorithm RSA_AES_KEY_WRAP_SHA_256). After the import no principal
reads the key: AWS KMS signs with it.

The functions here never write the private key anywhere, and never return it.
"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.keywrap import aes_key_wrap_with_padding

WRAPPING_ALGORITHM = "RSA_AES_KEY_WRAP_SHA_256"
WRAPPING_KEY_SPEC = "RSA_4096"
KEY_SPEC, KEY_USAGE, ORIGIN = "RSA_2048", "SIGN_VERIFY", "EXTERNAL"


class NotImported(Exception):
    """A key that this module does not import, with the reason."""


def key_forms(pem: bytes) -> tuple[bytes, bytes]:
    """The private key of a PEM file as AWS KMS imports it (PKCS #8, DER), and its public key (DER).
    GitHub gives the PKCS #1 form; the PKCS #8 form is read too."""
    try:
        private = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as error:
        raise NotImported(f"the file is not a private key in the PEM form without a password: {error}") from None
    if not isinstance(private, rsa.RSAPrivateKey) or private.key_size != 2048:
        raise NotImported("the key is not an RSA key of 2,048 bits, the key of a GitHub App")
    return (private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()),
            private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))


def fingerprint(public_der: bytes) -> str:
    """The SHA-256 of a public key in base64: the form of the fingerprint of a private key in the settings of
    a GitHub App."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(public_der).digest()).decode()


def import_private_key(kms, key_id: str, pem: bytes) -> dict:
    """Import the private key of a PEM file into a KMS key that has no key material. Return the state of the
    KMS key, and whether its public key is the public key of the file.

    A KMS key that has key material already is not changed: the key material of an asymmetric KMS key cannot
    be replaced. The answer then says whether it is the key of the file.
    """
    private_der, public_der = key_forms(pem)
    key = kms.describe_key(KeyId=key_id)["KeyMetadata"]
    if (key["Origin"], key["KeySpec"], key["KeyUsage"]) != (ORIGIN, KEY_SPEC, KEY_USAGE):
        raise NotImported(f"the KMS key is not a key for imported key material, {KEY_SPEC}, {KEY_USAGE}")
    answer = {"key_arn": key["Arn"], "state_before": key["KeyState"], "imported": False}
    if key["KeyState"] == "PendingImport":
        parameters = kms.get_parameters_for_import(KeyId=key["KeyId"], WrappingAlgorithm=WRAPPING_ALGORITHM,
                                                   WrappingKeySpec=WRAPPING_KEY_SPEC)
        aes_key = os.urandom(32)
        oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
        # The encrypted AES key first, then the key material under the AES key (key wrap with padding).
        encrypted = (serialization.load_der_public_key(parameters["PublicKey"]).encrypt(aes_key, oaep)
                     + aes_key_wrap_with_padding(aes_key, private_der))
        kms.import_key_material(KeyId=key["KeyId"], ImportToken=parameters["ImportToken"],
                                EncryptedKeyMaterial=encrypted, ExpirationModel="KEY_MATERIAL_DOES_NOT_EXPIRE")
        answer["imported"] = True
    elif key["KeyState"] not in ("Enabled", "Disabled"):
        raise NotImported(f"the KMS key is in the state {key['KeyState']}")
    in_kms = kms.get_public_key(KeyId=key["KeyId"])["PublicKey"]
    return {**answer, "state_after": kms.describe_key(KeyId=key["KeyId"])["KeyMetadata"]["KeyState"],
            "the_kms_key_has_the_key_of_the_file": in_kms == public_der,
            "fingerprint_of_the_file": fingerprint(public_der), "fingerprint_in_kms": fingerprint(in_kms),
            "wrapping_algorithm": WRAPPING_ALGORITHM, "wrapping_key_spec": WRAPPING_KEY_SPEC,
            "key_material_bytes": len(private_der), "public_key_der": public_der}
