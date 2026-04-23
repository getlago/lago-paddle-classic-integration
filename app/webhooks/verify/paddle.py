import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature
from fastapi import HTTPException
from app.utils.config_store import get
from app.utils.logger import get_logger

logger = get_logger("webhook.verify.paddle")


def _php_serialize(data: dict) -> bytes:
    """
    Serialize a dict as a PHP associative array.
    Paddle Classic signs the PHP-serialized form fields (excluding p_signature),
    sorted alphabetically by key.
    Format: a:N:{s:keyLen:"key";s:valLen:"value";...}
    """
    parts = []
    for key in sorted(data.keys()):
        k = str(key)
        v = str(data[key])
        parts.append(f's:{len(k)}:"{k}";s:{len(v)}:"{v}";')
    return f"a:{len(data)}:{{{''.join(parts)}}}".encode()


def verify_paddle_signature(form_data: dict) -> None:
    """
    Verify a Paddle Classic webhook signature using RSA + SHA1.

    Algorithm:
      1. Remove p_signature from the form fields
      2. Sort remaining fields alphabetically
      3. PHP-serialize the sorted dict
      4. Verify RSA-SHA1 signature (PKCS1v15) against Paddle's public key

    Skips verification if PADDLE_PUBLIC_KEY is not configured (logs a warning).
    Raises HTTP 401 if the signature is present but invalid.
    """
    public_key_pem = get("PADDLE_PUBLIC_KEY")
    if not public_key_pem:
        logger.warning("PADDLE_PUBLIC_KEY not configured — skipping webhook signature verification")
        return

    p_signature = form_data.get("p_signature")
    if not p_signature:
        raise HTTPException(status_code=401, detail="Missing p_signature in Paddle webhook")

    payload = {k: v for k, v in form_data.items() if k != "p_signature"}
    serialized = _php_serialize(payload)

    try:
        signature = base64.b64decode(p_signature)
        # Paddle dashboard gives raw base64 without PEM headers — wrap if needed
        pem = public_key_pem.strip()
        if not pem.startswith("-----"):
            pem = f"-----BEGIN PUBLIC KEY-----\n{pem}\n-----END PUBLIC KEY-----"
        public_key = serialization.load_pem_public_key(pem.encode())
        public_key.verify(signature, serialized, padding.PKCS1v15(), hashes.SHA1())
    except InvalidSignature:
        raise HTTPException(status_code=401, detail="Invalid Paddle webhook signature")
    except Exception as exc:
        logger.error("paddle signature verification failed", error=str(exc))
        raise HTTPException(status_code=401, detail="Paddle signature verification failed")
