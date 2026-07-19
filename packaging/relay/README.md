# Across Relay candidate image

Build this image from the repository root for `linux/amd64` and `linux/arm64`.
The runtime is non-root, requires an explicit listener address, accepts only TLS
1.3 mutually authenticated peers, and routes opaque end-to-end encrypted
frames. Mount certificates and an `across-relay-sessions/1.0` registration file
read-only, then pass `--serve`, `--host`, `--port`, `--certificate`,
`--private-key`, `--trust-store`, and `--sessions`.

The Relay registration file contains only session IDs, the two participant node
IDs, and TTLs. It never contains the end-to-end session key, Job content,
prompts, credentials, or Artifact bodies.
