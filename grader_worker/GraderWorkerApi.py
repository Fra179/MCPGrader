import base64
import hashlib
import json
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from requests.adapters import HTTPAdapter
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
        if max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        self.max_retries = max_retries
        self.session = self.__setup_request_session(max_retries)

    def __setup_request_session(self, max_retries: int) -> requests.Session:
        session = requests.Session()
        # Retry here, above signing: adapter retries would reuse an expired timestamp.
        adapter = HTTPAdapter(max_retries=0)
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

    def __request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict:
        body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
                if payload is not None else b"")
        url = self.base_url + path
        for attempt in range(self.max_retries + 1):
            headers = {"Content-Type": "application/json", **self.__sign(method, path, body)}
            try:
                response = self.session.request(method, url, data=body, headers=headers, timeout=self.timeout,
                                                allow_redirects=False)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise GraderWorkerException(f"Grader worker request to '{url}' failed in transit") from exc
            else:
                if response.status_code not in {429, 500, 502, 503, 504} or attempt == self.max_retries:
                    break
                response.close()
            time.sleep(2 ** attempt)
        if not 200 <= response.status_code < 300:
            try:
                decoded = response.json()
                error = decoded.get("error", response.text) if isinstance(decoded, dict) else response.text
            except ValueError:
                error = response.text
            raise GraderWorkerException(
                f"Grader worker request to '{url}' failed ({response.status_code}): {error}",
                status_code=response.status_code,
            )
        try:
            decoded = response.json()
        except ValueError as exc:
            raise GraderWorkerException("Grader worker returned invalid JSON", response.status_code) from exc
        if not isinstance(decoded, dict):
            raise GraderWorkerException("Grader worker returned a non-object response", response.status_code)
        return decoded

    def list_submissions(self, assignment_name: Optional[str] = None) -> list[dict[str, Any]]:
        """Raw submissions of an assignment (all assignments if None), oldest first."""
        payload = {"assignment_name": assignment_name} if assignment_name else {}
        return self.__request("POST", "/submissions", payload)["submissions"]

    def get_submissions(self, assignment_name: Optional[str] = None) -> list[SubmissionInfo]:
        """Retrieve the submissions of an assignment (all assignments if None) as SubmissionInfo objects."""
        return self._submission_infos(self.list_submissions(assignment_name))

    @staticmethod
    def _submission_infos(submissions: list[dict[str, Any]]) -> list[SubmissionInfo]:
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
                update_id=s.get("update_id"),
            )
            for s in submissions
        ]

    def list_updates(self) -> list[dict[str, Any]]:
        """Read pending push notifications without consuming them."""
        return self.__request("GET", "/updates")["submissions"]

    def get_updates(self, assignment_name: str | None = None) -> list[SubmissionInfo]:
        submissions = self.list_updates()
        if assignment_name is not None:
            submissions = [s for s in submissions if s["assignment_name"] == assignment_name]
        return self._submission_infos(submissions)

    def start_run(self, payload: dict[str, Any]) -> dict:
        return self.__request("POST", "/updates/grading", payload)

    def finish_run(self, payload: dict[str, Any]) -> dict:
        return self.__request("POST", "/updates/graded", payload)

    def cancel_run(self, payload: dict[str, Any]) -> dict:
        return self.__request("POST", "/updates/cancelled", payload)

    def inspect_run(self, run_id: str) -> dict:
        return self.__request("GET", "/grading?" + urlencode({"run_id": run_id}))

    def inspect_submission(self, submission_id: str) -> dict:
        return self.__request("GET", "/grading?" + urlencode({"id": submission_id}))

    def acknowledge_update(self, payload: dict[str, Any]) -> dict:
        return self.__request("POST", "/updates/acknowledged", payload)


if __name__ == "__main__":
    import sys
    from pprint import pprint

    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <worker_url> <private_key_file> [assignment_name]")
        sys.exit(1)

    api = GraderWorkerAPI(sys.argv[1], sys.argv[2])
    pprint(api.get_submissions(sys.argv[3] if len(sys.argv) > 3 else None))
