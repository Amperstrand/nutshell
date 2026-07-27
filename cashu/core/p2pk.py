import hashlib
from enum import Enum
from typing import Union

from coincurve import PublicKeyXOnly

from .crypto.secp import PrivateKey, PublicKey
from .errors import InvalidProofsError
from .secret import Secret, SecretKind


class SigFlags(Enum):
    # NUT #11: `SIG_INPUTS` requires valid signatures on all inputs independently. It is
    #  the default signature flag and will be applied if the `sigflag` tag is absent.
    SIG_INPUTS = "SIG_INPUTS"
    # NUT #11: `SIG_ALL` requires valid signatures on all inputs and on all outputs of a
    #  transaction.
    SIG_ALL = "SIG_ALL"


class P2PKSecret(Secret):
    @classmethod
    def from_secret(cls, secret: Secret):
        if SecretKind(secret.kind) != SecretKind.P2PK:
            raise InvalidProofsError("Secret is not a P2PK secret")
        # NUT #11: If a P2PK secret has any other signature flag value, the P2PK secret is
        #  malformed and the Proof **MUST** be rejected as unspendable.
        if secret.tags.get_tag("sigflag") and secret.tags.get_tag("sigflag") not in [
            SigFlags.SIG_INPUTS.value,
            SigFlags.SIG_ALL.value,
        ]:
            raise InvalidProofsError("Secret does not have a valid sigflag tag")
        # NOTE: exclude tags in .dict() because it doesn't deserialize it properly
        # need to add it back in manually with tags=secret.tags
        return cls(**secret.model_dump(exclude={"tags"}), tags=secret.tags)

    @property
    def locktime(self) -> Union[None, int]:
        locktime = self.tags.get_tag("locktime")
        return int(locktime) if locktime else None

    @property
    def sigflag(self) -> SigFlags:
        sigflag = self.tags.get_tag("sigflag")
        return SigFlags(sigflag) if sigflag else SigFlags.SIG_INPUTS

    @property
    def n_sigs(self) -> int:
        n_sigs = self.tags.get_tag_int("n_sigs")
        return int(n_sigs) if n_sigs else 1

    @property
    def n_sigs_refund(self) -> Union[None, int]:
        n_sigs_refund = self.tags.get_tag_int("n_sigs_refund")
        return n_sigs_refund


def schnorr_sign(message: bytes, private_key: PrivateKey) -> bytes:
    # NUT #11: We use `libsecp256k1`'s serialized 64 byte Schnorr signatures on the SHA256
    #  hash of the message to sign.
    signature = private_key.sign_schnorr(
        hashlib.sha256(message).digest(),
        None,  # type: ignore
    )
    return signature


def verify_schnorr_signature(
    message: bytes, pubkey: PublicKey, signature: bytes
) -> bool:
    xonly_pubkey: PublicKeyXOnly = PublicKeyXOnly(pubkey.format()[1:])
    return xonly_pubkey.verify(
        signature,
        hashlib.sha256(message).digest(),
    )
