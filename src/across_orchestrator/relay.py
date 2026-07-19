from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
import asyncio
import base64
import json
import ssl
import struct
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.x509.oid import NameOID

from .worker_protocol import ProtocolError, canonical_json, require_identifier, sanitize_public


RELAY_SCHEMA = "across-relay-frame/1.0"
MAX_FRAME_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class RelayFrame:
    session_id: str
    source_node_id: str
    target_node_id: str
    sequence: int
    expires_at: float
    nonce: str
    ciphertext: str
    schema_version: str = RELAY_SCHEMA

    def __post_init__(self) -> None:
        for name in ("session_id", "source_node_id", "target_node_id"):
            require_identifier(getattr(self, name), name)
        if self.sequence < 1 or self.expires_at <= 0:
            raise ProtocolError("relay frame sequence or expiry is invalid")
        try:
            nonce = base64.urlsafe_b64decode(self.nonce.encode())
            ciphertext = base64.urlsafe_b64decode(self.ciphertext.encode())
        except Exception as exc:
            raise ProtocolError("relay frame encoding is invalid") from exc
        if len(nonce) != 12 or not ciphertext or len(ciphertext) > MAX_FRAME_BYTES:
            raise ProtocolError("relay frame size is invalid")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RelayFrame":
        if value.get("schema_version") != RELAY_SCHEMA:
            raise ProtocolError("unsupported relay frame schema")
        return cls(
            session_id=str(value.get("session_id") or ""),
            source_node_id=str(value.get("source_node_id") or ""),
            target_node_id=str(value.get("target_node_id") or ""),
            sequence=int(value.get("sequence") or 0),
            expires_at=float(value.get("expires_at") or 0),
            nonce=str(value.get("nonce") or ""),
            ciphertext=str(value.get("ciphertext") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "sequence": self.sequence,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "ciphertext": self.ciphertext,
        }

    def aad(self) -> bytes:
        return canonical_json(
            {
                "schema_version": self.schema_version,
                "session_id": self.session_id,
                "source_node_id": self.source_node_id,
                "target_node_id": self.target_node_id,
                "sequence": self.sequence,
                "expires_at": self.expires_at,
            }
        ).encode("utf-8")


def seal_relay_payload(
    key: bytes,
    *,
    session_id: str,
    source_node_id: str,
    target_node_id: str,
    sequence: int,
    payload: Mapping[str, Any],
    expires_at: float | None = None,
    nonce: bytes | None = None,
) -> RelayFrame:
    if len(key) != 32:
        raise ProtocolError("relay end-to-end key must be 32 bytes")
    import os

    nonce_bytes = nonce or os.urandom(12)
    frame = RelayFrame(
        session_id=session_id,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        sequence=sequence,
        expires_at=expires_at or time.time() + 60,
        nonce=base64.urlsafe_b64encode(nonce_bytes).decode(),
        ciphertext=base64.urlsafe_b64encode(b"placeholder").decode(),
    )
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce_bytes, canonical_json(payload).encode("utf-8"), frame.aad())
    return RelayFrame(
        session_id=frame.session_id,
        source_node_id=frame.source_node_id,
        target_node_id=frame.target_node_id,
        sequence=frame.sequence,
        expires_at=frame.expires_at,
        nonce=frame.nonce,
        ciphertext=base64.urlsafe_b64encode(ciphertext).decode(),
    )


def open_relay_payload(key: bytes, frame: RelayFrame, *, now: float | None = None) -> dict[str, Any]:
    if len(key) != 32:
        raise ProtocolError("relay end-to-end key must be 32 bytes")
    if (time.time() if now is None else now) >= frame.expires_at:
        raise ProtocolError("relay frame expired")
    try:
        plaintext = ChaCha20Poly1305(key).decrypt(
            base64.urlsafe_b64decode(frame.nonce.encode()),
            base64.urlsafe_b64decode(frame.ciphertext.encode()),
            frame.aad(),
        )
        value = json.loads(plaintext)
    except Exception as exc:
        raise ProtocolError("relay frame authentication failed") from exc
    if not isinstance(value, dict):
        raise ProtocolError("relay payload must be an object")
    return value


class RelayRouter:
    """Opaque TTL router. It never receives the end-to-end session key."""

    def __init__(self, *, clock=time.time, max_sessions: int = 10_000, max_frame_bytes: int = MAX_FRAME_BYTES):
        self.clock = clock
        self.max_sessions = max(1, int(max_sessions))
        self.max_frame_bytes = max(1024, min(int(max_frame_bytes), MAX_FRAME_BYTES))
        self.sessions: dict[str, dict[str, Any]] = {}
        self._last_sequence: dict[tuple[str, str], int] = {}

    def register_session(self, session_id: str, node_ids: list[str], *, ttl_seconds: int = 300) -> dict[str, Any]:
        require_identifier(session_id, "session_id")
        participants = tuple(sorted({require_identifier(node, "node_id") for node in node_ids}))
        if len(participants) != 2:
            raise ProtocolError("relay session requires exactly two distinct participants")
        self.prune()
        if session_id not in self.sessions and len(self.sessions) >= self.max_sessions:
            raise ProtocolError("relay capacity exceeded")
        record = {"session_id": session_id, "node_ids": participants, "expires_at": self.clock() + max(30, min(int(ttl_seconds), 3600))}
        self.sessions[session_id] = record
        return {"session_id": session_id, "node_ids": list(participants), "expires_at": record["expires_at"]}

    def revoke_session(self, session_id: str) -> bool:
        removed = self.sessions.pop(session_id, None) is not None
        for key in [key for key in self._last_sequence if key[0] == session_id]:
            self._last_sequence.pop(key, None)
        return removed

    def route(self, frame: RelayFrame) -> dict[str, Any]:
        self.prune()
        record = self.sessions.get(frame.session_id)
        if not record or self.clock() >= float(record["expires_at"]):
            raise ProtocolError("relay session unavailable")
        participants = set(record["node_ids"])
        if {frame.source_node_id, frame.target_node_id} != participants:
            raise ProtocolError("cross-node relay delivery rejected")
        if self.clock() >= frame.expires_at:
            raise ProtocolError("relay frame expired")
        ciphertext_size = len(base64.urlsafe_b64decode(frame.ciphertext.encode()))
        if ciphertext_size > self.max_frame_bytes:
            raise ProtocolError("relay frame exceeds capacity limit")
        sequence_key = (frame.session_id, frame.source_node_id)
        previous = self._last_sequence.get(sequence_key, 0)
        if frame.sequence <= previous:
            raise ProtocolError("relay replay or out-of-order frame rejected")
        self._last_sequence[sequence_key] = frame.sequence
        return frame.to_dict()

    def health(self) -> dict[str, Any]:
        self.prune()
        return {"schema_version": "across-relay-health/1.0", "status": "ok", "sessions": len(self.sessions), "stores_job_content": False, "stores_artifacts": False, "stores_credentials": False}

    def prune(self) -> None:
        now = self.clock()
        expired = [session_id for session_id, record in self.sessions.items() if now >= float(record["expires_at"])]
        for session_id in expired:
            self.revoke_session(session_id)


def create_tls_context(
    *,
    server: bool,
    certificate: str | Path,
    private_key: str | Path,
    trust_store: str | Path | None,
) -> ssl.SSLContext:
    purpose = ssl.Purpose.CLIENT_AUTH if server else ssl.Purpose.SERVER_AUTH
    if server and trust_store is None:
        raise ProtocolError("relay server requires an explicit client trust store")
    context = ssl.create_default_context(purpose, cafile=str(trust_store) if trust_store else None)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(certificate), str(private_key))
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = not server
    return context


async def write_framed_json(writer: asyncio.StreamWriter, value: Mapping[str, Any]) -> None:
    payload = canonical_json(value).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("transport frame exceeds maximum size")
    writer.write(struct.pack("!I", len(payload)) + payload)
    await writer.drain()


async def read_framed_json(reader: asyncio.StreamReader) -> dict[str, Any]:
    header = await reader.readexactly(4)
    size = struct.unpack("!I", header)[0]
    if size < 2 or size > MAX_FRAME_BYTES:
        raise ProtocolError("transport frame length is invalid")
    payload = await reader.readexactly(size)
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolError("transport frame is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("transport frame must be an object")
    return value


class AsyncRelayServer:
    def __init__(self, router: RelayRouter, *, host: str, port: int, ssl_context: ssl.SSLContext, connection_registry: dict[str, asyncio.StreamWriter] | None = None):
        if host in {"", "0.0.0.0", "::"}:
            raise ProtocolError("relay listener requires an explicit interface binding")
        self.router = router
        self.host = host
        self.port = int(port)
        self.ssl_context = ssl_context
        self.server: asyncio.AbstractServer | None = None
        self.connections = connection_registry if connection_registry is not None else {}

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, host=self.host, port=self.port, ssl=self.ssl_context)

    @property
    def bound_port(self) -> int:
        if not self.server or not self.server.sockets:
            return self.port
        return int(self.server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.connections.values()):
            writer.close()
            await writer.wait_closed()
        self.connections.clear()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        node_id: str | None = None
        try:
            hello = await asyncio.wait_for(read_framed_json(reader), timeout=10)
            if hello.get("type") != "relay.hello":
                raise ProtocolError("relay connection requires hello")
            node_id = require_identifier(hello.get("node_id"), "node_id")
            peer = _tls_peer_identity(writer)
            if peer["node_id"] != node_id:
                raise ProtocolError("relay TLS identity does not match the claimed node")
            self.connections[node_id] = writer
            await write_framed_json(
                writer,
                {
                    "type": "relay.ready",
                    "node_id": node_id,
                    "certificate_fingerprint": peer["certificate_fingerprint"],
                },
            )
            while True:
                raw = await read_framed_json(reader)
                if raw.get("type") == "relay.register":
                    peer_node_id = require_identifier(raw.get("peer_node_id"), "peer_node_id")
                    session = self.router.register_session(
                        str(raw.get("session_id") or ""),
                        [node_id, peer_node_id],
                        ttl_seconds=int(raw.get("ttl_seconds") or 300),
                    )
                    await write_framed_json(
                        writer,
                        {"type": "relay.registered", **session},
                    )
                    continue
                frame = RelayFrame.from_dict(raw)
                if frame.source_node_id != node_id:
                    raise ProtocolError("relay source identity mismatch")
                routed = self.router.route(frame)
                target = self.connections.get(frame.target_node_id)
                if target is None:
                    await write_framed_json(writer, {"type": "relay.unavailable", "target_node_id": frame.target_node_id})
                    continue
                await write_framed_json(target, routed)
        except (asyncio.IncompleteReadError, ConnectionError, ProtocolError, asyncio.TimeoutError):
            pass
        finally:
            if node_id and self.connections.get(node_id) is writer:
                self.connections.pop(node_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass


class RelayEndpoint:
    """End-to-end encrypted Relay peer; the Relay only sees frame metadata and ciphertext."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        server_hostname: str,
        ssl_context: ssl.SSLContext,
        node_id: str,
        peer_node_id: str,
        session_id: str,
        session_key: bytes,
    ):
        if len(session_key) != 32:
            raise ProtocolError("relay endpoint requires a 32-byte session key")
        self.host = host
        self.port = int(port)
        self.server_hostname = server_hostname
        self.ssl_context = ssl_context
        self.node_id = require_identifier(node_id, "node_id")
        self.peer_node_id = require_identifier(peer_node_id, "peer_node_id")
        self.session_id = require_identifier(session_id, "session_id")
        self.session_key = session_key
        self.sequence = 0
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(
            host=self.host,
            port=self.port,
            ssl=self.ssl_context,
            server_hostname=self.server_hostname,
        )
        await write_framed_json(self.writer, {"type": "relay.hello", "node_id": self.node_id})
        ready = await read_framed_json(self.reader)
        if ready.get("type") != "relay.ready" or ready.get("node_id") != self.node_id:
            raise ProtocolError("relay did not accept the endpoint identity")

    async def close(self) -> None:
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass
        self.reader = None
        self.writer = None

    async def register_session(self, *, ttl_seconds: int = 300) -> dict[str, Any]:
        """Register only the opaque participant mapping; never sends the E2E key."""
        if not self.writer or not self.reader:
            raise ProtocolError("relay endpoint is not connected")
        await write_framed_json(
            self.writer,
            {
                "type": "relay.register",
                "session_id": self.session_id,
                "peer_node_id": self.peer_node_id,
                "ttl_seconds": max(30, min(int(ttl_seconds), 3600)),
            },
        )
        response = await read_framed_json(self.reader)
        if response.get("type") != "relay.registered" or response.get("session_id") != self.session_id:
            raise ProtocolError("relay session registration failed")
        return response

    async def send(self, payload: Mapping[str, Any], *, ttl_seconds: int = 60) -> RelayFrame:
        if not self.writer:
            raise ProtocolError("relay endpoint is not connected")
        self.sequence += 1
        frame = seal_relay_payload(
            self.session_key,
            session_id=self.session_id,
            source_node_id=self.node_id,
            target_node_id=self.peer_node_id,
            sequence=self.sequence,
            payload=payload,
            expires_at=time.time() + max(5, min(int(ttl_seconds), 300)),
        )
        await write_framed_json(self.writer, frame.to_dict())
        return frame

    async def receive(self, *, timeout_seconds: float = 30) -> dict[str, Any]:
        if not self.reader:
            raise ProtocolError("relay endpoint is not connected")
        raw = await asyncio.wait_for(read_framed_json(self.reader), timeout=timeout_seconds)
        if raw.get("type") == "relay.unavailable":
            raise ProtocolError("relay peer is unavailable")
        frame = RelayFrame.from_dict(raw)
        if frame.source_node_id != self.peer_node_id or frame.target_node_id != self.node_id or frame.session_id != self.session_id:
            raise ProtocolError("relay delivered a frame outside the endpoint binding")
        return open_relay_payload(self.session_key, frame)


def _tls_peer_identity(writer: asyncio.StreamWriter) -> dict[str, str]:
    ssl_object = writer.get_extra_info("ssl_object")
    encoded = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    if not encoded:
        raise ProtocolError("relay peer certificate is unavailable")
    certificate = x509.load_der_x509_certificate(encoded)
    candidates: set[str] = set()
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        for value in san.get_values_for_type(x509.UniformResourceIdentifier):
            for prefix in ("spiffe://across.local/worker/", "spiffe://across.local/node/"):
                if value.startswith(prefix) and value[len(prefix) :]:
                    candidates.add(value[len(prefix) :])
    except x509.ExtensionNotFound:
        pass
    candidates.update(
        attribute.value
        for attribute in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if attribute.value
    )
    if len(candidates) != 1:
        raise ProtocolError("relay peer certificate has an ambiguous node identity")
    return {
        "node_id": next(iter(candidates)),
        "certificate_fingerprint": certificate.fingerprint(hashes.SHA256()).hex(),
    }
