import base64
import hashlib
import json
import time
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from .structs import SubmissionInfo
from .exceptions import GraderWorkerException


class GraderWorkerAPI:
    """Thin client for the admin API of the grader Cloudflare Worker.

    Admin requests are signed with an ECDSA P-256 private key (PEM file, as created by
    ``scripts/admin-keys.sh new`` in the worker repository) whose public half is listed
    in the worker's ``ADMIN_PUBLIC_KEYS``.
    """

    def __init__(self, base_url: str, private_key_file: str, timeout: int = 30, max_retries: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        with open(private_key_file, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise GraderWorkerException(f"{private_key_file} is not an ECDSA P-256 private key")
        self.private_key = key
        self.timeout = timeout
        self.session = self.__setup_request_session(max_retries)

    def __setup_request_session(self, max_retries: int) -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],  # only read-only endpoints are called
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "Accept": "application/json",
        })
        return session

    def __sign(self, method: str, path: str, body: bytes) -> dict[str, str]:
        """Headers authenticating the request (see the signing procedure in the worker's API docs)."""
        timestamp = str(int(time.time()))
        message = "\n".join([method.upper(), path, timestamp, hashlib.sha256(body).hexdigest()])
        der = self.private_key.sign(message.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)  # the worker wants raw r||s (WebCrypto format), not DER
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return {
            "X-Grader-Timestamp": timestamp,
            "X-Grader-Signature": base64.b64encode(signature).decode("ascii"),
        }

    def __post_request(self, path: str, payload: dict[str, Any]) -> dict:
        body = json.dumps(payload).encode("utf-8")  # serialized once, so the signed bytes are the sent bytes
        headers = {"Content-Type": "application/json", **self.__sign("POST", path, body)}
        url = self.base_url + path
        response = self.session.post(url, data=body, headers=headers, timeout=self.timeout)
        if not response.ok:
            try:
                error = response.json().get("error", response.text)
            except ValueError:
                error = response.text
            raise GraderWorkerException(
                f"Grader worker request to '{url}' failed ({response.status_code}): {error}"
            )
        return response.json()

    def list_submissions(self, assignment_name: Optional[str] = None) -> list[dict[str, Any]]:
        """Raw submissions of an assignment (all assignments if None), oldest first."""
        payload = {"assignment_name": assignment_name} if assignment_name else {}
        return self.__post_request("/submissions", payload)["submissions"]

    def get_submissions(self, assignment_name: Optional[str] = None) -> list[SubmissionInfo]:
        """Retrieve the submissions of an assignment (all assignments if None) as SubmissionInfo objects."""
        return [
            SubmissionInfo(
                id=s["id"],
                assignment_name=s["assignment_name"],
                student_email=s["email"],
                student_id=s["student_id"],
                repo_url=s["repo_url"],
                ssh_url=s["repo_ssh_url"],
                deploy_key=s["private_key"],
                last_push=s.get("last_push"),
                last_commit=s.get("last_commit"),
            )
            for s in self.list_submissions(assignment_name)
        ]


if __name__ == "__main__":
    import sys
    from pprint import pprint

    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <worker_url> <private_key_file> [assignment_name]")
        sys.exit(1)

    api = GraderWorkerAPI(sys.argv[1], sys.argv[2])
    pprint(api.get_submissions(sys.argv[3] if len(sys.argv) > 3 else None))
