from .errors import InvalidProofsError
from .p2pk import P2PKSecret, SigFlags
from .secret import Secret, SecretKind


# HTLCSecret inherits properties from P2PKSecret
class HTLCSecret(P2PKSecret, Secret):
    # NUT #14: If for a `Proof`, `Proof.secret` is a `Secret` of kind `HTLC`, the hash of
    #  the lock is in `Proof.secret.data`. The preimage for unlocking the HTLC is in the
    #  witness `Proof.witness.preimage`.
    # NUT #14: All additional tags from P2PK locks can also be used here, allowing a
    #  locktime, signature flag, and multisig (see [NUT-11][11]).
    # NUT #14: The hash lock in `Secret.data` and the preimage in `Proof.witness.preimage`
    #  are treated as 32-byte data, encoded as 64-character hexadecimal strings.
    @classmethod
    def from_secret(cls, secret: Secret):
        if SecretKind(secret.kind) != SecretKind.HTLC:
            raise InvalidProofsError("Secret is not an HTLC secret")

        if secret.tags.get_tag("sigflag") and secret.tags.get_tag("sigflag") not in [
            SigFlags.SIG_INPUTS.value,
            SigFlags.SIG_ALL.value,
        ]:
            raise InvalidProofsError("Secret does not have a valid sigflag tag")
        # NOTE: exclude tags in .dict() because it doesn't deserialize it properly
        # need to add it back in manually with tags=secret.tags
        return cls(**secret.model_dump(exclude={"tags"}), tags=secret.tags)
